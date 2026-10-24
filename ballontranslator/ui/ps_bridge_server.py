"""Local HTTP API the Photoshop panel uses to talk to the running app.

The server is off by default (``pcfg.ps_bridge.live_enabled``). It listens on
127.0.0.1 only, and every request must carry the pairing token in the
``X-BT-Token`` header. Web pages cannot send that header to another origin
without a CORS preflight, which this server never grants, and the Host check
stops DNS rebinding.

Requests run on server threads. Anything that reads or changes the project
runs on the Qt main thread through :class:`MainThreadCaller`; image bytes are
read on the server thread.

Endpoints, all under ``/api/v1/``:

- ``GET status``: the open project and its pages.
- ``GET page?name=<page>``: the page's blocks as saved in the project, plus
  ``bases``, the values a later update must still find
  (``photoshop_bridge_block_snapshot``).
- ``GET image?page=<page>&kind=original|inpainted|mask``: image bytes.
- ``POST updates``: the payload the bridge script writes to the update file.
  Answers ``{"state": "applied" | "blocked" | "rejected", "message": ...}``.
"""

import hmac
import json
import mimetypes
import os
import os.path as osp
import secrets
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from qtpy.QtCore import QObject, Qt, Signal

from ballontranslator.utils.logger import logger as LOGGER
from .ps_bridge_dialog import (
    PhotoshopBridgeUpdateError,
    _json_value,
    _project_page_blocks,
    photoshop_bridge_block_snapshot,
)

API_PREFIX = "/api/v1/"
API_VERSION = 1
MAIN_THREAD_TIMEOUT = 30.0
MAX_BODY_BYTES = 8 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
IMAGE_KINDS = ("original", "inpainted", "mask")


def new_bridge_token() -> str:
    """Return a fresh pairing token.

    >>> len(new_bridge_token()) >= 24
    True
    """
    return secrets.token_urlsafe(18)


class BridgeRequestError(Exception):
    """A request the API answers with an error status instead of data."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _MainThreadJob:
    def __init__(self, func: Callable[[], Any]) -> None:
        self.func = func
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class MainThreadCaller(QObject):
    """Run callables on the thread this object lives in and wait for them."""

    _job_posted = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._job_posted.connect(
            self._run_job, Qt.ConnectionType.QueuedConnection
        )

    def call(self, func: Callable[[], Any], timeout: float = MAIN_THREAD_TIMEOUT) -> Any:
        job = _MainThreadJob(func)
        self._job_posted.emit(job)
        if not job.done.wait(timeout):
            raise BridgeRequestError(503, "BallonsTranslator is busy. Try again.")
        if job.error is not None:
            raise job.error
        return job.result

    def _run_job(self, job: _MainThreadJob) -> None:
        try:
            job.result = job.func()
        except Exception as error:
            job.error = error
        finally:
            job.done.set()


# The functions below run on the main thread and take the main window.

def _open_project(window: Any) -> Any:
    project = window.imgtrans_proj
    if not project.proj_path or not project.directory:
        raise BridgeRequestError(409, "No project is open in BallonsTranslator.")
    return project


def _require_page(project: Any, page_name: Optional[str]) -> list:
    blocks = _project_page_blocks(project, page_name) if page_name else None
    if blocks is None:
        raise BridgeRequestError(404, f"Page '{page_name}' is not in the open project.")
    return blocks


def bridge_status(window: Any) -> Dict[str, Any]:
    project = window.imgtrans_proj
    if not project.proj_path or not project.directory:
        return {"api": API_VERSION, "project": None}
    return {
        "api": API_VERSION,
        "project": {
            "path": project.proj_path,
            "name": osp.basename(osp.normpath(project.directory)),
            "current_page": project.current_img,
            "pages": list(project.pages),
            "unsaved": bool(window.canvas.projstate_unsaved),
        },
    }


def bridge_page(window: Any, page_name: Optional[str]) -> Dict[str, Any]:
    project = _open_project(window)
    blocks = _require_page(project, page_name)
    # The current page's blocks lag behind the canvas until the page is saved,
    # and the bases must match what a later update is checked against.
    if window.canvas.projstate_unsaved:
        raise BridgeRequestError(
            409, "Save the project in BallonsTranslator, then import again."
        )
    return {
        "project_path": project.proj_path,
        "page": page_name,
        "blocks": [_json_value(block.to_dict()) for block in blocks],
        "bases": [photoshop_bridge_block_snapshot(block) for block in blocks],
    }


def bridge_image_path(window: Any, page_name: Optional[str], kind: str) -> str:
    project = _open_project(window)
    _require_page(project, page_name)
    if kind == "original":
        return osp.join(project.directory, page_name)
    if kind == "inpainted":
        return project.get_inpainted_path(page_name, get_last_modified=True)
    if kind == "mask":
        return project.get_mask_path(page_name, get_last_modified=True)
    raise BridgeRequestError(400, f"Unknown image kind '{kind}'.")


def bridge_apply_updates(window: Any, payload: Any) -> Dict[str, Any]:
    try:
        count = window.apply_photoshop_bridge_updates(payload)
    except PhotoshopBridgeUpdateError as error:
        return {"state": "rejected", "message": str(error)}
    except RuntimeError as error:
        return {"state": "blocked", "message": str(error)}
    return {
        "state": "applied",
        "applied": count,
        "message": f"Applied {count} Photoshop change(s).",
    }


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process take a port in use.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: Tuple[str, int], bridge: "PhotoshopBridgeServer") -> None:
        self.bridge = bridge
        super().__init__(address, _BridgeRequestHandler)


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    server: _BridgeHTTPServer
    server_version = "BallonsTranslatorBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("Photoshop Bridge server: " + format, *args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        # No CORS headers: browsers must not reach this API.
        self._send_json(405, {"error": "Method not allowed."})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS

    def _handle(self, method: str) -> None:
        try:
            self._check_access()
            url = urlsplit(self.path)
            if not url.path.startswith(API_PREFIX):
                raise BridgeRequestError(404, "Not found.")
            route = url.path[len(API_PREFIX):]
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            self._route(method, route, query)
        except BridgeRequestError as error:
            self._send_json(error.status, {"error": str(error)})
        except Exception as error:
            LOGGER.exception("Photoshop Bridge server request failed")
            self._send_json(500, {"error": str(error)})

    def _check_access(self) -> None:
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if hostname not in LOCAL_HOSTS:
            raise BridgeRequestError(403, "Only local connections are allowed.")
        if not self.server.bridge.token_matches(self.headers.get("X-BT-Token", "")):
            raise BridgeRequestError(401, "Pair the panel with BallonsTranslator first.")

    def _route(self, method: str, route: str, query: Dict[str, str]) -> None:
        bridge = self.server.bridge
        if method == "GET" and route == "status":
            self._send_json(200, bridge.on_main_thread(bridge_status))
        elif method == "GET" and route == "page":
            self._send_json(
                200, bridge.on_main_thread(bridge_page, query.get("name"))
            )
        elif method == "GET" and route == "image":
            kind = query.get("kind", "original")
            if kind not in IMAGE_KINDS:
                raise BridgeRequestError(400, f"Unknown image kind '{kind}'.")
            path = bridge.on_main_thread(bridge_image_path, query.get("page"), kind)
            self._send_file(path)
        elif method == "POST" and route == "updates":
            payload = self._read_json()
            self._send_json(200, bridge.on_main_thread(bridge_apply_updates, payload))
        else:
            raise BridgeRequestError(404, "Not found.")

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise BridgeRequestError(411, "Content-Length is required.")
        if not 0 <= length <= MAX_BODY_BYTES:
            raise BridgeRequestError(413, "The request is too large.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BridgeRequestError(400, "The request is not valid JSON.")

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_file(self, path: Optional[str]) -> None:
        if not path or not osp.isfile(path):
            raise BridgeRequestError(404, "The image does not exist yet.")
        with open(path, "rb") as image_file:
            body = image_file.read()
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._send_bytes(200, body, content_type, osp.basename(path))

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        filename: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("X-BT-Filename", filename)
        self.end_headers()
        self.wfile.write(body)


class PhotoshopBridgeServer:
    """Serve the local API for one main window; create it on the main thread.

    >>> from types import SimpleNamespace
    >>> server = PhotoshopBridgeServer(SimpleNamespace())
    >>> server.address is None, server.token_matches("")
    (True, False)
    """

    def __init__(self, window: Any) -> None:
        self._window = window
        self._caller = MainThreadCaller()
        self._httpd: Optional[_BridgeHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._token = ""

    @property
    def address(self) -> Optional[Tuple[str, int]]:
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return host, port

    def set_token(self, token: str) -> None:
        self._token = token

    def token_matches(self, token: str) -> bool:
        return bool(self._token) and hmac.compare_digest(
            token.encode("utf-8"), self._token.encode("utf-8")
        )

    def on_main_thread(self, func: Callable[..., Any], *args: Any) -> Any:
        return self._caller.call(partial(func, self._window, *args))

    def start(self, port: int, token: str) -> None:
        """Listen on 127.0.0.1:port; raises OSError when the port is taken."""
        self.stop()
        self._token = token
        httpd = _BridgeHTTPServer(("127.0.0.1", port), self)
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="photoshop-bridge-server",
            daemon=True,
        )
        thread.start()
        self._httpd, self._thread = httpd, thread
        LOGGER.info("Photoshop Bridge server listening on 127.0.0.1:%d", port)

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=2)
        LOGGER.info("Photoshop Bridge server stopped")
