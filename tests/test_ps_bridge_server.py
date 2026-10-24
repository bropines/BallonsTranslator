import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QWidget

from ballontranslator.ui.mainwindow import MainWindow
from ballontranslator.ui.ps_bridge_dialog import (
    PhotoshopBridgeDialog,
    PhotoshopBridgeUpdateError,
    photoshop_bridge_block_snapshot,
)
from ballontranslator.ui.ps_bridge_server import PhotoshopBridgeServer
from ballontranslator.utils.config import pcfg
from ballontranslator.utils.proj_imgtrans import ProjImgTrans
from ballontranslator.utils.textblock import TextBlock

TOKEN = "test-token"
# The tests talk to 127.0.0.1 directly, never through a configured proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PhotoshopBridgeServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = ProjImgTrans()
        self.project.directory = self.temp_dir.name
        self.project.proj_path = os.path.join(self.temp_dir.name, "project.json")
        self.project.current_img = "page.png"
        self.project.pages = {
            "page.png": [
                TextBlock(
                    xyxy=[0, 0, 10, 10],
                    text=["source"],
                    translation="one",
                    _bounding_rect=[0, 0, 10, 10],
                )
            ]
        }
        with open(os.path.join(self.temp_dir.name, "page.png"), "wb") as image:
            image.write(b"\x89PNG fake image")
        self.window = SimpleNamespace(
            imgtrans_proj=self.project,
            canvas=SimpleNamespace(projstate_unsaved=False),
            apply_photoshop_bridge_updates=Mock(return_value=1),
        )
        self.server = PhotoshopBridgeServer(self.window)
        self.server.start(0, TOKEN)
        self.port = self.server.address[1]

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_dir.cleanup()

    def request(self, path: str, data=None, method=None, headers=None):
        """Send a request from a worker thread while the Qt loop runs here."""
        all_headers = {"X-BT-Token": TOKEN}
        all_headers.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers=all_headers,
        )
        result = {}

        def send() -> None:
            try:
                with OPENER.open(request, timeout=10) as response:
                    result["response"] = (response.status, response.headers, response.read())
            except urllib.error.HTTPError as error:
                result["response"] = (error.code, error.headers, error.read())

        thread = threading.Thread(target=send)
        thread.start()
        while thread.is_alive():
            self.app.processEvents()
            thread.join(0.01)
        return result["response"]

    def request_json(self, path: str, **kwargs):
        status, _, body = self.request(path, **kwargs)
        return status, json.loads(body)

    def test_requests_need_the_pairing_token(self) -> None:
        status, data = self.request_json("/api/v1/status", headers={"X-BT-Token": "wrong"})
        self.assertEqual(status, 401)
        self.assertIn("error", data)

        status, data = self.request_json("/api/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["project"]["pages"], ["page.png"])
        self.assertEqual(data["project"]["current_page"], "page.png")

    def test_foreign_host_and_browser_preflight_are_refused(self) -> None:
        status, _ = self.request_json("/api/v1/status", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

        status, headers, _ = self.request("/api/v1/status", method="OPTIONS")
        self.assertEqual(status, 405)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_page_carries_the_values_an_update_must_match(self) -> None:
        status, data = self.request_json("/api/v1/page?name=page.png")

        self.assertEqual(status, 200)
        block = self.project.pages["page.png"][0]
        self.assertEqual(data["blocks"][0]["translation"], "one")
        self.assertEqual(data["bases"], [photoshop_bridge_block_snapshot(block)])

    def test_unsaved_project_and_unknown_page_are_errors(self) -> None:
        status, _ = self.request_json("/api/v1/page?name=missing.png")
        self.assertEqual(status, 404)

        self.window.canvas.projstate_unsaved = True
        status, data = self.request_json("/api/v1/page?name=page.png")
        self.assertEqual(status, 409)
        self.assertIn("Save the project", data["error"])

    def test_image_bytes_are_served(self) -> None:
        status, headers, body = self.request("/api/v1/image?page=page.png&kind=original")
        self.assertEqual((status, body), (200, b"\x89PNG fake image"))
        self.assertEqual(headers.get("Content-Type"), "image/png")

        status, _, _ = self.request("/api/v1/image?page=page.png&kind=inpainted")
        self.assertEqual(status, 404)
        status, _, _ = self.request("/api/v1/image?page=page.png&kind=secret")
        self.assertEqual(status, 400)

    def test_updates_go_through_the_window(self) -> None:
        payload = {"version": 1, "page": "page.png", "updates": []}

        status, data = self.request_json("/api/v1/updates", data=payload)
        self.assertEqual((status, data["state"], data["applied"]), (200, "applied", 1))
        self.window.apply_photoshop_bridge_updates.assert_called_once_with(payload)

        self.window.apply_photoshop_bridge_updates.side_effect = (
            PhotoshopBridgeUpdateError("Block #1 conflicts.")
        )
        _, data = self.request_json("/api/v1/updates", data=payload)
        self.assertEqual((data["state"], data["message"]), ("rejected", "Block #1 conflicts."))

        self.window.apply_photoshop_bridge_updates.side_effect = RuntimeError("Save first.")
        _, data = self.request_json("/api/v1/updates", data=payload)
        self.assertEqual(data["state"], "blocked")

    def test_stopping_frees_the_port(self) -> None:
        self.server.stop()
        self.assertIsNone(self.server.address)
        self.server.start(self.port, TOKEN)
        status, _ = self.request_json("/api/v1/status")
        self.assertEqual(status, 200)


class PhotoshopBridgeLiveSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.saved_settings = (
            pcfg.ps_bridge.live_enabled,
            pcfg.ps_bridge.live_port,
            pcfg.ps_bridge.live_token,
        )
        self.busy = socket.socket()
        self.busy.bind(("127.0.0.1", 0))
        self.busy.listen()
        self.free_port = self._free_port()

    def tearDown(self) -> None:
        self.busy.close()
        (
            pcfg.ps_bridge.live_enabled,
            pcfg.ps_bridge.live_port,
            pcfg.ps_bridge.live_token,
        ) = self.saved_settings

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _window(self) -> QWidget:
        window = QWidget()
        window.ps_bridge_server = None
        window.tr = lambda text: text
        window.apply_ps_bridge_live_settings = (
            lambda: MainWindow.apply_ps_bridge_live_settings(window)
        )
        return window

    def test_dialog_switch_starts_and_stops_the_server(self) -> None:
        pcfg.ps_bridge.live_enabled = False
        pcfg.ps_bridge.live_port = self.free_port
        pcfg.ps_bridge.live_token = ""
        window = self._window()
        dialog = PhotoshopBridgeDialog(window)
        self.assertIsNone(window.ps_bridge_server)

        dialog.live_checkbox.setChecked(True)

        self.assertEqual(window.ps_bridge_server.address, ("127.0.0.1", self.free_port))
        self.assertTrue(pcfg.ps_bridge.live_token)
        self.assertEqual(dialog.live_token_edit.text(), pcfg.ps_bridge.live_token)
        self.assertIn(str(self.free_port), dialog.live_status_label.text())

        dialog.live_checkbox.setChecked(False)
        self.assertIsNone(window.ps_bridge_server.address)
        dialog.deleteLater()
        window.deleteLater()

    def test_busy_port_is_reported(self) -> None:
        pcfg.ps_bridge.live_enabled = True
        pcfg.ps_bridge.live_port = self.busy.getsockname()[1]
        window = self._window()

        message = MainWindow.apply_ps_bridge_live_settings(window)

        self.assertIn(str(pcfg.ps_bridge.live_port), message)
        self.assertIsNone(window.ps_bridge_server.address)
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
