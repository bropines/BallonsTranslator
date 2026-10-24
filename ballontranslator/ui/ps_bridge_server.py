"""Background side of the Photoshop Bridge: update files and the panel server.

:class:`PhotoshopBridgeService` belongs to the main window. It applies the
changes the bridge script leaves next to the project and, when enabled, runs
the local HTTP API the Photoshop panel talks to.

The server is off by default (``pcfg.ps_bridge.live_enabled``). It listens on
localhost only, on both 127.0.0.1 and ::1 because Photoshop resolves
``localhost`` to ::1 on macOS. A panel pairs once: ``POST pair`` asks the user
in BallonsTranslator and answers with the token every other request must send
in ``X-BT-Token``. Web pages cannot send that header to another origin without
a CORS preflight, which this server never grants, and the Host check stops DNS
rebinding.

Requests run on server threads. Anything that reads or changes the project
runs on the Qt main thread through :class:`MainThreadCaller`; image bytes are
read on the server thread.

Endpoints, all under ``/api/v1/``:

- ``POST pair``: ask the user to allow a panel; answers ``{"token": ...}``.
- ``GET status``: the open project and its pages.
- ``GET page?name=<page>``: the page's blocks as saved in the project, plus
  ``bases``, the values a later update must still find
  (``photoshop_bridge_block_snapshot``).
- ``GET image?page=<page>&kind=original|inpainted|mask``: image bytes.
- ``POST updates``: the payload the bridge script writes to the update file.
  Answers ``{"state": "applied" | "blocked" | "rejected", "message": ...}``.
"""

import errno
import hmac
import json
import mimetypes
import os
import os.path as osp
import secrets
import socket
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from qtpy.QtCore import QObject, Qt, QTimer, Signal
from qtpy.QtWidgets import QMessageBox

from ballontranslator.utils import shared
from ballontranslator.utils.config import PS_BRIDGE_PORTS, pcfg
from ballontranslator.utils.logger import logger as LOGGER
from ballontranslator.utils.message import create_info_dialog
from .ps_bridge_dialog import (
    PhotoshopBridgeUpdateError,
    _json_value,
    _project_page_blocks,
    _tr,
    consume_photoshop_bridge_update,
    photoshop_bridge_block_snapshot,
    photoshop_bridge_update_path,
)

API_PREFIX = "/api/v1/"
API_VERSION = 1
MAIN_THREAD_TIMEOUT = 30.0
# The user answers the pairing question in BallonsTranslator.
PAIRING_TIMEOUT = 120.0
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
        return {"app": "BallonsTranslator", "api": API_VERSION, "project": None}
    return {
        "app": "BallonsTranslator",
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

    def __init__(
        self,
        address: Tuple[str, int],
        family: int,
        bridge: "PhotoshopBridgeServer",
    ) -> None:
        self.address_family = family
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
            self._check_host()
            url = urlsplit(self.path)
            if not url.path.startswith(API_PREFIX):
                raise BridgeRequestError(404, "Not found.")
            route = url.path[len(API_PREFIX):]
            if method == "POST" and route == "pair":
                self._send_json(200, {"token": self.server.bridge.pair()})
                return
            self._check_token()
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            self._route(method, route, query)
        except BridgeRequestError as error:
            self._send_json(error.status, {"error": str(error)})
        except Exception as error:
            LOGGER.exception("Photoshop Bridge server request failed")
            self._send_json(500, {"error": str(error)})

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
        if hostname not in LOCAL_HOSTS:
            raise BridgeRequestError(403, "Only local connections are allowed.")

    def _check_token(self) -> None:
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

    ``confirm_pairing`` runs on the main thread and returns the token to hand
    to a panel, or None when the user refuses.

    >>> from types import SimpleNamespace
    >>> server = PhotoshopBridgeServer(SimpleNamespace(), lambda: None)
    >>> server.port is None, server.token_matches("")
    (True, False)
    """

    def __init__(
        self,
        window: Any,
        confirm_pairing: Callable[[], Optional[str]],
    ) -> None:
        self._window = window
        self._confirm_pairing = confirm_pairing
        self._caller = MainThreadCaller()
        self._servers: List[_BridgeHTTPServer] = []
        self._threads: List[threading.Thread] = []
        self._token = ""
        self._pairing_lock = threading.Lock()
        self.last_contact = 0.0

    @property
    def port(self) -> Optional[int]:
        if not self._servers:
            return None
        return self._servers[0].server_address[1]

    def set_token(self, token: str) -> None:
        self._token = token

    def token_matches(self, token: str) -> bool:
        matches = bool(self._token) and hmac.compare_digest(
            token.encode("utf-8"), self._token.encode("utf-8")
        )
        if matches:
            self.last_contact = time.monotonic()
        return matches

    def on_main_thread(self, func: Callable[..., Any], *args: Any) -> Any:
        return self._caller.call(partial(func, self._window, *args))

    def pair(self) -> str:
        """Ask the user to allow a panel; runs on a server thread."""
        if not self._pairing_lock.acquire(blocking=False):
            raise BridgeRequestError(
                409, "Another connection request is waiting in BallonsTranslator."
            )
        try:
            token = self._caller.call(self._confirm_pairing, PAIRING_TIMEOUT)
        finally:
            self._pairing_lock.release()
        if not token:
            raise BridgeRequestError(403, "The connection was declined in BallonsTranslator.")
        self.last_contact = time.monotonic()
        return token

    def start(self, port: int, token: str) -> None:
        """Listen on localhost:port; raises OSError when the port is taken."""
        self.stop()
        self._token = token
        servers = [_BridgeHTTPServer(("127.0.0.1", port), socket.AF_INET, self)]
        port = servers[0].server_address[1]
        try:
            servers.append(_BridgeHTTPServer(("::1", port), socket.AF_INET6, self))
        except OSError as error:
            # Without IPv6 localhost resolves to 127.0.0.1 only. Any other
            # failure means something else answers on ::1 at this port.
            if error.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                servers[0].server_close()
                raise
        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever,
                name="photoshop-bridge-server",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        self._servers = servers
        LOGGER.info("Photoshop Bridge server listening on localhost:%d", port)

    def stop(self) -> None:
        servers, threads = self._servers, self._threads
        self._servers, self._threads = [], []
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        if servers:
            LOGGER.info("Photoshop Bridge server stopped")


class PhotoshopBridgeService(QObject):
    """Photoshop Bridge background work for the main window.

    Photoshop can leave changes at any time, and checking one file is cheap,
    so a short poll needs no file watcher bookkeeping when projects change.
    While the bridge window is visible it reports the outcome itself.
    """

    POLL_INTERVAL_MS = 1500
    # A connected panel asks for the status every few seconds.
    PANEL_CONTACT_SECONDS = 10.0

    def __init__(self, window: QObject) -> None:
        super().__init__(window)
        self._window = window
        self._blocked_message = ""
        self.server: Optional[PhotoshopBridgeServer] = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.check_updates)
        self._timer.start()
        if pcfg.ps_bridge.live_enabled:
            error = self.apply_live_settings()
            if error:
                LOGGER.warning(error)

    def check_updates(self) -> None:
        project_path = self._window.imgtrans_proj.proj_path
        if not project_path or not os.path.isfile(
            photoshop_bridge_update_path(project_path)
        ):
            return
        dialog = getattr(self._window, "ps_bridge_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.check_for_update()
            return
        result = consume_photoshop_bridge_update(
            project_path, self._window.apply_photoshop_bridge_updates
        )
        if result is None or result[0] == "waiting":
            return
        state, message = result
        # A blocked update stays pending until the user saves; say so once.
        if state == "blocked" and message == self._blocked_message:
            return
        self._blocked_message = message if state == "blocked" else ""
        create_info_dialog(_tr("Photoshop Bridge") + ": " + message)

    @property
    def panel_port(self) -> Optional[int]:
        return self.server.port if self.server is not None else None

    def panel_connected(self) -> bool:
        return (
            self.server is not None
            and self.server.port is not None
            and time.monotonic() - self.server.last_contact < self.PANEL_CONTACT_SECONDS
        )

    def apply_live_settings(self) -> Optional[str]:
        """Start or stop the panel server to match the config.

        Returns a message when the server cannot listen on the chosen port.
        """
        settings = pcfg.ps_bridge
        if not settings.live_enabled or shared.HEADLESS:
            if self.server is not None:
                self.server.stop()
            return None
        if not settings.live_token:
            settings.live_token = new_bridge_token()
        if self.server is None:
            self.server = PhotoshopBridgeServer(self._window, self.confirm_pairing)
        self.server.set_token(settings.live_token)
        if self.server.port == settings.live_port:
            return None
        try:
            self.server.start(settings.live_port, settings.live_token)
        except OSError as error:
            return _tr(
                "The Photoshop panel connection cannot use port {port}: {error}"
            ).format(port=settings.live_port, error=error)
        return None

    def confirm_pairing(self) -> Optional[str]:
        answer = QMessageBox.question(
            self._window,
            _tr("Photoshop Bridge"),
            _tr(
                "A Photoshop panel wants to connect to BallonsTranslator.\n\n"
                "Allow it to read the open project and send changes to it?"
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return None
        return pcfg.ps_bridge.live_token

    def forget_paired_panels(self) -> None:
        pcfg.ps_bridge.live_token = new_bridge_token()
        if self.server is not None:
            self.server.set_token(pcfg.ps_bridge.live_token)

    def shutdown(self) -> None:
        self._timer.stop()
        if self.server is not None:
            self.server.stop()
