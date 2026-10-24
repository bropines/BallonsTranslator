import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QMessageBox, QWidget

from ballontranslator.ui.ps_bridge_dialog import (
    PhotoshopBridgeDialog,
    PhotoshopBridgeUpdateError,
    photoshop_bridge_block_snapshot,
)
from ballontranslator.ui.ps_bridge_server import (
    PhotoshopBridgeServer,
    PhotoshopBridgeService,
)
from ballontranslator.utils.config import PS_BRIDGE_PORTS, pcfg
from ballontranslator.utils.proj_imgtrans import ProjImgTrans
from ballontranslator.utils.textblock import TextBlock

TOKEN = "test-token"
# The tests talk to localhost directly, never through a configured proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


class BridgeRequests:
    """Send requests from a worker thread while the Qt loop runs here."""

    app: QApplication
    port: int

    def request(self, path, data=None, method=None, headers=None, host="127.0.0.1"):
        all_headers = {"X-BT-Token": TOKEN}
        all_headers.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://{host}:{self.port}{path}",
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

    def request_json(self, path, **kwargs):
        status, _, body = self.request(path, **kwargs)
        return status, json.loads(body)


class PhotoshopBridgeServerTest(BridgeRequests, unittest.TestCase):
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
        self.confirm_pairing = Mock(return_value=TOKEN)
        self.server = PhotoshopBridgeServer(self.window, self.confirm_pairing)
        self.server.start(0, TOKEN)
        self.port = self.server.port

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_dir.cleanup()

    def test_requests_need_the_pairing_token(self) -> None:
        status, data = self.request_json("/api/v1/status", headers={"X-BT-Token": "wrong"})
        self.assertEqual(status, 401)
        self.assertIn("error", data)

        status, data = self.request_json("/api/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["app"], "BallonsTranslator")
        self.assertEqual(data["project"]["pages"], ["page.png"])
        self.assertEqual(data["project"]["current_page"], "page.png")

    def test_pairing_hands_out_the_token_only_when_the_user_agrees(self) -> None:
        status, data = self.request_json("/api/v1/pair", data={}, headers={"X-BT-Token": ""})
        self.assertEqual((status, data), (200, {"token": TOKEN}))

        self.confirm_pairing.return_value = None
        status, data = self.request_json("/api/v1/pair", data={}, headers={"X-BT-Token": ""})
        self.assertEqual(status, 403)
        self.assertNotIn("token", data)

    @unittest.skipUnless(_ipv6_available(), "IPv6 loopback is not available")
    def test_ipv6_localhost_is_served(self) -> None:
        # Photoshop resolves localhost to ::1 on macOS.
        status, _ = self.request_json("/api/v1/status", host="[::1]")
        self.assertEqual(status, 200)

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
        self.assertIsNone(self.server.port)
        self.server.start(self.port, TOKEN)
        status, _ = self.request_json("/api/v1/status")
        self.assertEqual(status, 200)


class PhotoshopBridgeServiceTest(BridgeRequests, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.saved_settings = (
            pcfg.ps_bridge.live_enabled,
            pcfg.ps_bridge.live_port,
            pcfg.ps_bridge.live_token,
        )
        pcfg.ps_bridge.live_enabled = False
        pcfg.ps_bridge.live_token = ""
        self.port = self._free_bridge_port()
        pcfg.ps_bridge.live_port = self.port
        self.window = QWidget()
        self.window.imgtrans_proj = ProjImgTrans()
        self.service = PhotoshopBridgeService(self.window)
        self.window.ps_bridge = self.service

    def tearDown(self) -> None:
        self.service.shutdown()
        self.window.deleteLater()
        (
            pcfg.ps_bridge.live_enabled,
            pcfg.ps_bridge.live_port,
            pcfg.ps_bridge.live_token,
        ) = self.saved_settings

    @staticmethod
    def _free_bridge_port() -> int:
        for port in PS_BRIDGE_PORTS:
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        raise unittest.SkipTest("No Photoshop Bridge port is free")

    def test_dialog_switch_starts_and_stops_the_server(self) -> None:
        dialog = PhotoshopBridgeDialog(self.window)
        self.assertIsNone(self.service.panel_port)

        dialog.live_checkbox.setChecked(True)

        self.assertEqual(self.service.panel_port, self.port)
        self.assertTrue(pcfg.ps_bridge.live_token)
        self.assertIn(str(self.port), dialog.live_status_label.text())

        dialog.live_checkbox.setChecked(False)
        self.assertIsNone(self.service.panel_port)
        dialog.deleteLater()

    def test_pairing_asks_the_user_and_forgetting_revokes_the_token(self) -> None:
        pcfg.ps_bridge.live_enabled = True
        self.assertIsNone(self.service.apply_live_settings())
        with patch(
            "ballontranslator.ui.ps_bridge_server.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ) as question:
            status, data = self.request_json(
                "/api/v1/pair", data={}, headers={"X-BT-Token": ""}
            )
        question.assert_called_once()
        self.assertEqual(status, 200)
        token = data["token"]
        status, _ = self.request_json("/api/v1/status", headers={"X-BT-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(self.service.panel_connected())

        self.service.forget_paired_panels()

        status, _ = self.request_json("/api/v1/status", headers={"X-BT-Token": token})
        self.assertEqual(status, 401)

    def test_busy_port_is_reported(self) -> None:
        busy = socket.socket()
        busy.bind(("127.0.0.1", self.port))
        busy.listen()
        try:
            pcfg.ps_bridge.live_enabled = True
            message = self.service.apply_live_settings()
        finally:
            busy.close()

        self.assertIn(str(self.port), message)
        self.assertIsNone(self.service.panel_port)


if __name__ == "__main__":
    unittest.main()
