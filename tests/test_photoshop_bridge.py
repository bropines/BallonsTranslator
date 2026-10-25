import json
import os
import zipfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from qtpy.QtWidgets import (
    QApplication,
    QFormLayout,
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QWidget,
)

from ballontranslator.ui.mainwindow import MainWindow
from ballontranslator.ui.ps_bridge_dialog import (
    BridgeApplyResult,
    PhotoshopBridgeDialog,
    PanelInstaller,
    PhotoshopBridgeUpdateError,
    _validated_bridge_runs,
    apply_bridge_runs,
    bridge_apply_message,
    bridge_text_runs,
    bridge_values_match,
    build_panel_package,
    bundled_panel_version,
    consume_photoshop_bridge_update,
    installed_panel_version,
    panel_install_command,
    get_photoshop_paths,
    photoshop_bridge_block_snapshot,
    project_state_matches_disk,
    validate_photoshop_bridge_updates,
)
from ballontranslator.ui.ps_bridge_server import (
    PhotoshopBridgeService,
    bridge_block_style,
    bridge_shadow,
    bridge_stroke,
    bridge_tracking,
)
from ballontranslator.ui.text_engine.annotations import apply_emphasis, to_rich_text_html
from ballontranslator.ui.text_engine.pipeline_formatting import _load_text_block_document
from ballontranslator.utils.config import PS_BRIDGE_PORTS
from ballontranslator.utils.fontformat import LineSpacingType, px2pt
from ballontranslator.utils.proj_imgtrans import ProjImgTrans, TextBlkEncoder
from ballontranslator.utils.text_effects import (
    GlowEffect,
    HollowEffect,
    ShadowEffect,
    SolidPaint,
    StrokeEffect,
    TextEffectStack,
)
from ballontranslator.utils.textblock import TextBlock


class PhotoshopBridgeTest(unittest.TestCase):
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
                    text=["source 1"],
                    translation="one",
                    rich_text="<p>one</p>",
                    _bounding_rect=[0, 0, 10, 10],
                ),
                TextBlock(
                    xyxy=[20, 0, 30, 10],
                    text=["source 2"],
                    translation="",
                    _bounding_rect=[20, 0, 10, 10],
                ),
                TextBlock(
                    xyxy=[40, 0, 50, 10],
                    text=["source 3"],
                    translation="three",
                    _bounding_rect=[40, 0, 10, 10],
                ),
            ]
        }
        self.project.save()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _payload(self, *indices: int) -> dict:
        updates = []
        for index in indices:
            block = self.project.pages["page.png"][index]
            updates.append({
                "block_index": index,
                "translation": f"updated {index + 1}",
                "font_size": block.font_size + index + 1,
                "base": photoshop_bridge_block_snapshot(block),
            })
        return {
            "version": 1,
            "project_path": self.project.proj_path,
            "page": "page.png",
            "block_count": 3,
            "updates": updates,
        }

    def test_noncontiguous_layer_numbers_update_the_named_blocks(self) -> None:
        payload = self._payload(0, 2)
        fake_window = SimpleNamespace(
            imgtrans_proj=self.project,
            canvas=SimpleNamespace(
                projstate_unsaved=False,
                setProjSaveState=Mock(),
                update_saved_undostep=Mock(),
            ),
            st_manager=SimpleNamespace(updateSceneTextitems=Mock()),
            tr=lambda text: text,
        )

        result = MainWindow.apply_photoshop_bridge_updates(fake_window, payload)

        self.assertEqual(result, BridgeApplyResult(2, 0))
        self.assertEqual(self.project.pages["page.png"][0].translation, "updated 1")
        self.assertEqual(self.project.pages["page.png"][0].rich_text, "")
        self.assertEqual(self.project.pages["page.png"][1].translation, "")
        self.assertEqual(self.project.pages["page.png"][2].translation, "updated 3")
        fake_window.st_manager.updateSceneTextitems.assert_called_once_with()
        self.assertTrue(os.path.isfile(self.project.proj_path + ".backup"))
        with open(
            self.project.proj_path + ".backup", "r", encoding="utf-8"
        ) as backup_file:
            backup = json.load(backup_file)
        self.assertEqual(backup["pages"]["page.png"][0]["translation"], "one")
        with open(self.project.proj_path, "r", encoding="utf-8") as project_file:
            saved = json.load(project_file)
        self.assertEqual(saved["pages"]["page.png"][0]["translation"], "updated 1")

    def test_changed_snapshot_and_duplicate_numbers_are_rejected(self) -> None:
        payload = self._payload(0)
        payload["updates"][0]["base"]["text"] = ["different source"]
        payload["updates"][0]["base"]["xyxy"] = [5, 5, 9, 9]
        with self.assertRaisesRegex(PhotoshopBridgeUpdateError, "no longer matches"):
            validate_photoshop_bridge_updates(self.project, payload)
        self.assertEqual(self.project.pages["page.png"][0].translation, "one")

        payload = self._payload(0)
        payload["updates"].append(dict(payload["updates"][0]))
        with self.assertRaises(PhotoshopBridgeUpdateError):
            validate_photoshop_bridge_updates(self.project, payload)

    def test_block_moved_or_ocr_fixed_in_the_app_is_still_the_same_block(self) -> None:
        block = self.project.pages["page.png"][0]
        payload = self._payload(0)
        block.xyxy = [3, 3, 13, 13]
        validate_photoshop_bridge_updates(self.project, payload)

        block.xyxy = [0, 0, 10, 10]
        block.text = ["fixed source"]
        validate_photoshop_bridge_updates(self.project, payload)

    def test_fields_the_update_does_not_change_may_differ(self) -> None:
        block = self.project.pages["page.png"][0]
        payload = self._payload(0)
        payload["updates"][0]["font_size"] = None
        block.font_size = 40

        _, updates = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual(updates[0].translation, "updated 1")
        self.assertIsNone(updates[0].font_size)

    def test_font_and_box_changes_are_applied(self) -> None:
        block = self.project.pages["page.png"][2]
        payload = self._payload(2)
        payload["updates"][0].update(
            translation=block.translation,
            font_size=None,
            font={"family": "Bebas Neue", "bold": True, "italic": False},
            box_delta=[5, -2, 5, 8],
        )
        fake_window = SimpleNamespace(
            imgtrans_proj=self.project,
            canvas=SimpleNamespace(
                projstate_unsaved=False,
                setProjSaveState=Mock(),
                update_saved_undostep=Mock(),
            ),
            st_manager=SimpleNamespace(updateSceneTextitems=Mock()),
            tr=lambda text: text,
        )

        result = MainWindow.apply_photoshop_bridge_updates(fake_window, payload)

        self.assertEqual(result.applied, 1)
        self.assertEqual(block.translation, "three")
        self.assertEqual(block.font_family, "Bebas Neue")
        self.assertEqual((block.fontformat.font_weight, block.fontformat.italic), (700, False))
        self.assertEqual(block._bounding_rect, [45, -2, 10, 20])
        self.assertEqual(block.xyxy, [45, -2, 55, 18])

    def test_font_size_rounded_by_extendscript_still_matches(self) -> None:
        block = self.project.pages["page.png"][0]
        block.font_size = 14.399999999999999
        payload = self._payload(0)
        # ExtendScript prints numbers with 15 significant digits.
        payload["updates"][0]["base"]["font_size"] = 14.4

        page_name, updates = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual((page_name, updates[0][0]), ("page.png", block))

    def test_a_real_font_size_change_is_still_rejected(self) -> None:
        block = self.project.pages["page.png"][0]
        block.font_size = 14.4
        payload = self._payload(0)
        payload["updates"][0]["base"]["font_size"] = 14.5

        with self.assertRaises(PhotoshopBridgeUpdateError):
            validate_photoshop_bridge_updates(self.project, payload)

    def test_unsaved_app_changes_block_the_update(self) -> None:
        payload = self._payload(0)
        fake_window = SimpleNamespace(
            imgtrans_proj=self.project,
            canvas=SimpleNamespace(projstate_unsaved=True),
            tr=lambda text: text,
        )

        with self.assertRaisesRegex(RuntimeError, "unsaved changes"):
            MainWindow.apply_photoshop_bridge_updates(fake_window, payload)
        self.assertEqual(self.project.pages["page.png"][0].translation, "one")

    def test_disk_change_is_detected_before_the_app_can_overwrite_it(self) -> None:
        self.assertTrue(project_state_matches_disk(self.project))
        self.project.current_img = "another-page.png"
        self.assertTrue(project_state_matches_disk(self.project))
        self.project.current_img = "page.png"
        with open(self.project.proj_path, "r", encoding="utf-8") as project_file:
            disk_data = json.load(project_file)
        disk_data["pages"]["page.png"][0]["translation"] = "external edit"
        with open(self.project.proj_path, "w", encoding="utf-8") as project_file:
            json.dump(disk_data, project_file, cls=TextBlkEncoder)

        self.assertFalse(project_state_matches_disk(self.project))

    def test_page_save_regenerating_rich_text_and_box_is_not_a_conflict(self) -> None:
        payload = self._payload(0)
        block = self.project.pages["page.png"][0]
        # Saving a page rebuilds both from the scene; Photoshop writes neither.
        block.rich_text = "<html><body>one</body></html>"
        block._bounding_rect = [1, 2, 30, 40]

        page_name, updates = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual((page_name, updates[0][0]), ("page.png", block))

    def test_payload_from_an_older_script_still_validates(self) -> None:
        payload = self._payload(0)
        block = self.project.pages["page.png"][0]
        payload["updates"][0]["base"].update(
            rich_text=block.rich_text,
            bounding_rect=block._bounding_rect,
        )
        payload["session_id"] = "old-session"

        page_name, _ = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual(page_name, "page.png")

    def test_edit_made_in_the_app_after_photoshop_got_it_is_named(self) -> None:
        payload = self._payload(0)
        self.project.pages["page.png"][0].translation = "edited in the app"

        with self.assertRaisesRegex(PhotoshopBridgeUpdateError, r"\(text\)"):
            validate_photoshop_bridge_updates(self.project, payload)

    def _write_update(self, payload: dict) -> str:
        update_path = self.project.proj_path + ".ps_bridge_updates.json"
        with open(update_path, "w", encoding="utf-8") as update_file:
            json.dump(payload, update_file)
        return update_path

    def test_applied_update_file_is_removed(self) -> None:
        update_path = self._write_update(self._payload(0))
        apply_updates = Mock(return_value=BridgeApplyResult(1))

        state, message = consume_photoshop_bridge_update(
            self.project.proj_path, apply_updates
        )

        self.assertEqual(state, "applied")
        self.assertIn("1", message)
        apply_updates.assert_called_once_with(self._payload(0))
        self.assertFalse(os.path.exists(update_path))

    def test_blocked_update_waits_for_the_next_check(self) -> None:
        update_path = self._write_update(self._payload(0))
        apply_updates = Mock(side_effect=RuntimeError("unsaved changes"))

        state, _ = consume_photoshop_bridge_update(self.project.proj_path, apply_updates)

        self.assertEqual(state, "blocked")
        self.assertTrue(os.path.exists(update_path))

    def test_conflicting_update_is_set_aside_so_it_cannot_block_new_sends(self) -> None:
        payload = self._payload(0)
        payload["updates"][0]["base"]["translation"] = "something else"
        update_path = self._write_update(payload)

        def apply_updates(payload: dict) -> BridgeApplyResult:
            validate_photoshop_bridge_updates(self.project, payload)
            return BridgeApplyResult(1)

        state, message = consume_photoshop_bridge_update(
            self.project.proj_path, apply_updates
        )

        self.assertEqual(state, "rejected")
        self.assertIn("Block #1", message)
        self.assertFalse(os.path.exists(update_path))
        self.assertTrue(os.path.isfile(
            self.project.proj_path + ".ps_bridge_updates.rejected.json"
        ))

    def _poller_window(self, apply_updates: Mock) -> QWidget:
        window = QWidget()
        window.imgtrans_proj = self.project
        window.ps_bridge_dialog = None
        window.apply_photoshop_bridge_updates = apply_updates
        return window

    def test_poller_applies_changes_and_reports_a_blocked_update_once(self) -> None:
        update_path = self._write_update(self._payload(0))
        window = self._poller_window(
            Mock(side_effect=RuntimeError("Save the project to apply them."))
        )
        service = PhotoshopBridgeService(window)
        with patch("ballontranslator.ui.ps_bridge_server.create_info_dialog") as info:
            service.check_updates()
            service.check_updates()
            self.assertEqual(info.call_count, 1)
            self.assertTrue(os.path.exists(update_path))

            window.apply_photoshop_bridge_updates = Mock(
                return_value=BridgeApplyResult(1)
            )
            service.check_updates()

        self.assertEqual(info.call_count, 2)
        self.assertIn("Applied 1", info.call_args.args[0])
        self.assertFalse(os.path.exists(update_path))
        window.deleteLater()

    def test_poller_leaves_reporting_to_a_visible_bridge_window(self) -> None:
        self._write_update(self._payload(0))
        window = self._poller_window(Mock(return_value=BridgeApplyResult(1)))
        dialog = PhotoshopBridgeDialog(window, project=self.project)
        window.ps_bridge_dialog = dialog
        dialog.show()
        self.app.processEvents()
        with patch("ballontranslator.ui.ps_bridge_server.create_info_dialog") as info:
            PhotoshopBridgeService(window).check_updates()

        info.assert_not_called()
        self.assertIn("Applied 1", dialog.sync_status_label.text())
        dialog.hide()
        window.deleteLater()

    def test_photoshop_soft_line_breaks_become_newlines(self) -> None:
        payload = self._payload(0)
        payload["updates"][0]["translation"] = "one\x03\x03two\rthree"

        _, updates = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual(updates[0][1], "one\n\ntwo\nthree")

    def test_installer_copies_without_elevation_when_destination_is_writable(
        self,
    ) -> None:
        photoshop_dir = os.path.join(self.temp_dir.name, "Photoshop")
        scripts_dir = os.path.join(photoshop_dir, "Presets", "Scripts")
        os.makedirs(scripts_dir)
        dialog = PhotoshopBridgeDialog(project=self.project)
        with patch(
            "ballontranslator.ui.ps_bridge_dialog.get_photoshop_paths",
            return_value=(photoshop_dir, scripts_dir, None),
        ), patch(
            "ballontranslator.ui.ps_bridge_dialog.shutil.copy2"
        ) as copy, patch.object(
            dialog, "refresh_status"
        ) as refresh, patch.object(
            dialog, "_open_manual_install_folders"
        ) as manual, patch(
            "ballontranslator.ui.ps_bridge_dialog.QMessageBox.information"
        ):
            dialog.on_install_script()

        source_jsx, target_jsx = copy.call_args.args
        self.assertEqual(os.path.basename(source_jsx), "BallonTranslator_PS_Bridge.jsx")
        self.assertEqual(target_jsx, os.path.join(scripts_dir, os.path.basename(source_jsx)))
        refresh.assert_called_once_with()
        manual.assert_not_called()
        dialog.deleteLater()

    def test_installer_falls_back_to_manual_copy_without_elevation(self) -> None:
        photoshop_dir = os.path.join(self.temp_dir.name, "Photoshop")
        scripts_dir = os.path.join(photoshop_dir, "Presets", "Scripts")
        os.makedirs(scripts_dir)
        dialog = PhotoshopBridgeDialog(project=self.project)
        with patch(
            "ballontranslator.ui.ps_bridge_dialog.get_photoshop_paths",
            return_value=(photoshop_dir, scripts_dir, None),
        ), patch(
            "ballontranslator.ui.ps_bridge_dialog.shutil.copy2",
            side_effect=PermissionError("protected"),
        ), patch.object(
            dialog, "refresh_status"
        ) as refresh, patch.object(
            dialog, "_open_manual_install_folders"
        ) as manual, patch(
            "ballontranslator.ui.ps_bridge_dialog.QMessageBox.warning"
        ) as warning:
            dialog.on_install_script()

        manual.assert_called_once()
        source_jsx, destination = manual.call_args.args
        self.assertEqual(destination, scripts_dir)
        refresh.assert_not_called()
        warning.assert_called_once()
        warning_text = warning.call_args.args[2]
        self.assertIn(os.path.normpath(source_jsx), warning_text)
        self.assertIn(os.path.normpath(scripts_dir), warning_text)
        dialog.deleteLater()

    def _make_macos_photoshop(self, release: str) -> str:
        install_dir = os.path.join(self.temp_dir.name, f"Adobe Photoshop {release}")
        os.makedirs(os.path.join(install_dir, f"Adobe Photoshop {release}.app"))
        os.makedirs(os.path.join(install_dir, "Presets", "Scripts"))
        return install_dir

    def test_macos_detection_finds_the_newest_app_bundle(self) -> None:
        self._make_macos_photoshop("CC 2019")
        newest = self._make_macos_photoshop("2026")
        pattern = os.path.join(self.temp_dir.name, "Adobe Photoshop*", "Adobe Photoshop*.app")
        with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "darwin"), patch(
            "ballontranslator.ui.ps_bridge_dialog.MACOS_PHOTOSHOP_GLOBS", (pattern,)
        ):
            ps_dir, scripts_dir, app_path = get_photoshop_paths()

        self.assertEqual(ps_dir, newest)
        self.assertEqual(scripts_dir, os.path.join(newest, "Presets", "Scripts"))
        self.assertEqual(app_path, os.path.join(newest, "Adobe Photoshop 2026.app"))

    def test_macos_without_photoshop_reports_nothing(self) -> None:
        pattern = os.path.join(self.temp_dir.name, "Adobe Photoshop*", "Adobe Photoshop*.app")
        with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "darwin"), patch(
            "ballontranslator.ui.ps_bridge_dialog.MACOS_PHOTOSHOP_GLOBS", (pattern,)
        ):
            self.assertEqual(get_photoshop_paths(), (None, None, None))

    def test_macos_launch_runs_the_bundled_script_through_applescript(self) -> None:
        install_dir = self._make_macos_photoshop("2026")
        app_path = os.path.join(install_dir, "Adobe Photoshop 2026.app")
        dialog = PhotoshopBridgeDialog(project=self.project)
        with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "darwin"), patch(
            "ballontranslator.ui.ps_bridge_dialog.get_photoshop_paths",
            return_value=(install_dir, os.path.join(install_dir, "Presets", "Scripts"), app_path),
        ), patch("ballontranslator.ui.ps_bridge_dialog.subprocess.Popen") as popen:
            dialog.on_open_in_photoshop()

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        self.assertEqual(os.path.basename(command[-1]), "BallonTranslator_PS_Bridge.jsx")
        script = " ".join(command[1:-1])
        self.assertIn('tell application "Adobe Photoshop 2026"', script)
        # Import dialogs can outlast the two-minute AppleEvent timeout.
        self.assertIn("ignoring application responses", script)
        # The path is an argument, never spliced into the AppleScript source.
        self.assertNotIn(command[-1], script)
        dialog.deleteLater()

    def test_windows_launch_keeps_the_run_switch(self) -> None:
        exe_path = os.path.join(self.temp_dir.name, "Photoshop.exe")
        open(exe_path, "w").close()
        dialog = PhotoshopBridgeDialog(project=self.project)
        with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "win32"), patch(
            "ballontranslator.ui.ps_bridge_dialog.get_photoshop_paths",
            return_value=(self.temp_dir.name, None, exe_path),
        ), patch("ballontranslator.ui.ps_bridge_dialog.subprocess.Popen") as popen:
            dialog.on_open_in_photoshop()

        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [exe_path, "-r"])
        dialog.deleteLater()

    def test_status_that_wraps_after_showing_is_not_clipped(self) -> None:
        dialog = PhotoshopBridgeDialog(project=self.project)
        dialog.setMinimumWidth(480)
        dialog.resize(480, 300)
        dialog.show()
        self.app.processEvents()
        dialog._set_sync_status(
            "Block #3 on page '0002.jpg' changed after it was opened in Photoshop.",
            "#F44336",
        )
        # One pass lays the new text out, the next applies the fitted size.
        self.app.processEvents()
        self.app.processEvents()

        label = dialog.sync_status_label
        self.assertGreater(label.heightForWidth(label.width()), label.fontMetrics().height())
        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        dialog.hide()
        dialog.deleteLater()

    def test_panel_package_holds_the_panel_files(self) -> None:
        package = build_panel_package(self.temp_dir.name)

        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
        self.assertTrue({"manifest.json", "index.html", "main.js", "core.js"} <= names)
        self.assertEqual(manifest["version"], bundled_panel_version())
        # The panel must be allowed to reach every port the app may use.
        domains = manifest["requiredPermissions"]["network"]["domains"]
        self.assertEqual(domains, [f"http://localhost:{port}" for port in PS_BRIDGE_PORTS])

    def test_newest_installed_panel_version_is_found(self) -> None:
        plugins = os.path.join(self.temp_dir.name, "Adobe", "UXP", "Plugins", "External")
        for name in ("com.ballonstranslator.psbridge_1.0.9", "com.ballonstranslator.psbridge_1.0.10",
                     "com.other.plugin_9.0.0"):
            os.makedirs(os.path.join(plugins, name))
        with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "win32"), patch.dict(
            os.environ, {"APPDATA": self.temp_dir.name}
        ):
            self.assertEqual(installed_panel_version(), "1.0.10")

    def test_panel_install_command_follows_the_platform(self) -> None:
        with patch("ballontranslator.ui.ps_bridge_dialog.os.path.exists", return_value=True):
            with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "win32"):
                self.assertEqual(panel_install_command("p.ccx")[1:], ["/install", "p.ccx"])
            with patch("ballontranslator.ui.ps_bridge_dialog.sys.platform", "darwin"):
                self.assertEqual(panel_install_command("p.ccx")[1:], ["--install", "p.ccx"])
        with patch("ballontranslator.ui.ps_bridge_dialog.os.path.exists", return_value=False):
            self.assertIsNone(panel_install_command("p.ccx"))

    def test_panel_installer_reports_adobe_installer_results(self) -> None:
        installer = PanelInstaller()
        results = []
        installer.finished.connect(lambda ok, message: results.append((ok, message)))
        outputs = [
            "Installing extension\nInstallation Successful for extension\n",
            "Installing extension\nFailed to install, status = -642!\n",
        ]
        for output in outputs:
            with patch(
                "ballontranslator.ui.ps_bridge_dialog.subprocess.run",
                return_value=SimpleNamespace(stdout=output, stderr="", returncode=0),
            ):
                installer._run(["installer", "/install", "p.ccx"])
        self.app.processEvents()
        self.assertEqual(results, [(True, ""), (False, "Failed to install, status = -642!")])

    def test_status_is_not_clipped_with_macos_form_layout(self) -> None:
        class MacFormStyle(QProxyStyle):
            # macOS keeps form fields at their size hint by default.
            def styleHint(self, hint, option=None, widget=None, return_data=None):
                if hint == QStyle.StyleHint.SH_FormLayoutFieldGrowthPolicy:
                    return QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint.value
                return super().styleHint(hint, option, widget, return_data)

        # Real glyph widths are needed; offscreen Qt may have no system fonts.
        font_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ballontranslator", "assets", "font_refresh", "Abel-Regular.ttf",
        )
        font_id = QFontDatabase.addApplicationFont(font_path)
        style_name = self.app.style().name()
        self.app.setStyle(MacFormStyle(QStyleFactory.create(style_name)))
        try:
            dialog = PhotoshopBridgeDialog(project=self.project)
            dialog.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 13))
            dialog.show()
            self.app.processEvents()
            label = dialog.sync_status_label
            message = "Applied 1 Photoshop change(s) to page 0003.jpg now. " * 3
            for length in range(20, len(message), 9):
                dialog._set_sync_status(
                    "Photoshop sent changes, but BallonsTranslator has unsaved "
                    "changes. Save the project to apply them.",
                    "#FFA000",
                )
                self.app.processEvents()
                dialog._set_sync_status(message[:length], "#4CAF50")
                self.app.processEvents()
                self.app.processEvents()
                self.assertGreaterEqual(
                    label.height(), label.heightForWidth(label.width()), length
                )
            dialog.hide()
            dialog.deleteLater()
        finally:
            self.app.setStyle(QStyleFactory.create(style_name))
            QFontDatabase.removeApplicationFont(font_id)

def _styled_block(translation: str, styles: list, **values) -> TextBlock:
    """Return a block whose rich text BT's own document code wrote.

    ``styles`` holds ``(start, end, char_format_modifier)`` in UTF-16 offsets.
    """
    block = TextBlock(xyxy=[0, 0, 100, 50], text=["source"],
                      translation=translation, _bounding_rect=[0, 0, 100, 50], **values)
    block.font_size = 30
    block.font_family = "Arial"
    document = _load_text_block_document(block)
    cursor = QTextCursor(document)
    for start, end, modifier in styles:
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        if callable(modifier):
            modifier(cursor)
        else:
            cursor.mergeCharFormat(modifier)
    block.rich_text = to_rich_text_html(
        document,
        line_spacing_fallback=block.fontformat.line_spacing,
        line_spacing_type_fallback=block.fontformat.line_spacing_type,
    )
    return block


def _char_format(size_px=None, bold=None, color=None) -> QTextCharFormat:
    char_format = QTextCharFormat()
    if size_px is not None:
        char_format.setFontPointSize(px2pt(size_px))
    if bold is not None:
        char_format.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    if color is not None:
        char_format.setForeground(QColor(*color))
    return char_format


class PhotoshopBridgeStyleTest(unittest.TestCase):
    """Protocol v2: text styles, alignment, opacity and rotation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = ProjImgTrans()
        self.project.directory = self.temp_dir.name
        self.project.proj_path = os.path.join(self.temp_dir.name, "project.json")
        self.project.current_img = "page.png"
        # "Hello 😀 world" / "second": Hello is 48 px, bold and red.
        self.block = _styled_block(
            "Hello \U0001F600 world\nsecond",
            [(0, 5, _char_format(48, True, (200, 0, 0)))],
        )
        self.plain = TextBlock(xyxy=[200, 0, 300, 50], text=["plain"],
                               translation="plain", _bounding_rect=[200, 0, 100, 50])
        self.project.pages = {"page.png": [self.block, self.plain]}
        self.project.save()
        self.window = SimpleNamespace(
            imgtrans_proj=self.project,
            canvas=SimpleNamespace(
                projstate_unsaved=False,
                setProjSaveState=Mock(),
                update_saved_undostep=Mock(),
            ),
            st_manager=SimpleNamespace(updateSceneTextitems=Mock()),
            tr=lambda text: text,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _payload(self, index: int = 0, **changes) -> dict:
        block = self.project.pages["page.png"][index]
        update = {
            "block_index": index,
            "translation": block.translation,
            "font_size": None,
            "base": photoshop_bridge_block_snapshot(block),
        }
        update.update(changes)
        return {
            "version": 1,
            "project_path": self.project.proj_path,
            "page": "page.png",
            "block_count": 2,
            "updates": [update],
        }

    def _update_runs(self, block: TextBlock) -> list:
        return [
            {
                "start": run["start"],
                "end": run["end"],
                "font": {"family": run["font_family"], "bold": run["bold"],
                         "italic": run["italic"]},
                "font_size": run["font_size"],
                "color": run["color"],
                "underline": run["underline"],
            }
            for run in bridge_text_runs(block)[1]
        ]

    def test_rich_text_runs_use_pixels_and_utf16_offsets(self) -> None:
        text, runs = bridge_text_runs(self.block)

        self.assertEqual(text, "Hello \U0001F600 world\nsecond")
        # The emoji is two UTF-16 units; the paragraph break joins the run before it.
        self.assertEqual([(run["start"], run["end"]) for run in runs], [(0, 5), (5, 21)])
        self.assertEqual(
            [(run["font_size"], run["weight"], run["bold"], run["color"]) for run in runs],
            [(48.0, 700, True, [200, 0, 0]), (30.0, 400, False, [0, 0, 0])],
        )
        self.assertEqual(runs[1]["font_family"], "Arial")
        self.assertEqual(bridge_text_runs(self.plain)[1][0]["end"], 5)

    def test_runs_survive_a_round_trip_through_photoshop(self) -> None:
        runs_before = bridge_text_runs(self.block)[1]

        lost = apply_bridge_runs(
            self.block, self.block.translation, self._update_runs(self.block)
        )

        self.assertFalse(lost)
        self.assertTrue(bridge_values_match(bridge_text_runs(self.block)[1], runs_before))
        # The block-level format follows the first run and the smallest size.
        self.assertEqual(self.block.font_size, 30.0)
        self.assertEqual(self.block.fontformat.frgb, [200, 0, 0])

    def test_unchanged_runs_are_not_written(self) -> None:
        payload = self._payload(runs=self._update_runs(self.block))

        _, updates = validate_photoshop_bridge_updates(self.project, payload)
        result = MainWindow.apply_photoshop_bridge_updates(self.window, payload)

        self.assertIsNone(updates[0].runs)
        self.assertEqual(result, BridgeApplyResult(0, 0))

    def test_changed_runs_alignment_opacity_and_angle_are_applied(self) -> None:
        runs = self._update_runs(self.block)
        runs[1].update(color=[0, 0, 255], font_size=36.0)
        runs[1]["font"]["italic"] = True
        payload = self._payload(runs=runs, alignment=2, opacity=0.5, angle=30.0)

        result = MainWindow.apply_photoshop_bridge_updates(self.window, payload)

        self.assertEqual(result, BridgeApplyResult(1, 0))
        after = bridge_text_runs(self.block)[1]
        self.assertEqual((after[0]["font_size"], after[0]["color"]), (48.0, [200, 0, 0]))
        self.assertEqual(
            (after[1]["font_size"], after[1]["color"], after[1]["italic"]),
            (36.0, [0, 0, 255], True),
        )
        fmt = self.block.fontformat
        self.assertEqual((fmt.alignment, fmt.opacity, self.block.angle), (2, 0.5, 30.0))
        self.assertEqual(self.block.font_size, 36.0)
        # The detection box bounds the rotated text box.
        self.assertNotEqual(self.block.xyxy, [0, 0, 100, 50])
        with open(self.project.proj_path, "r", encoding="utf-8") as project_file:
            saved = json.load(project_file)["pages"]["page.png"][0]
        self.assertEqual((saved["angle"], saved["fontformat"]["alignment"]), (30.0, 2))

    def test_new_text_keeps_photoshop_styles_and_reports_lost_annotations(self) -> None:
        def emphasis(cursor: QTextCursor) -> None:
            apply_emphasis(cursor, "filled dot", "over right")

        block = _styled_block("ab", [(0, 2, emphasis)])
        self.project.pages["page.png"][0] = block
        self.project.save()
        runs = self._update_runs(block)
        runs[0]["end"] = 3
        runs[0]["color"] = [10, 20, 30]
        payload = self._payload(translation="abc", runs=runs)

        result = MainWindow.apply_photoshop_bridge_updates(self.window, payload)

        self.assertEqual(result, BridgeApplyResult(1, 1))
        self.assertIn("1 block(s)", bridge_apply_message(result))
        self.assertEqual(block.translation, "abc")
        self.assertEqual(bridge_text_runs(block)[1][0]["color"], [10, 20, 30])
        self.assertNotIn("text-emphasis", block.rich_text)

    def test_run_offsets_inside_an_emoji_or_with_gaps_are_rejected(self) -> None:
        runs = self._update_runs(self.block)
        valid = _validated_bridge_runs(runs, self.block.translation, self.block.translation)
        self.assertEqual([(run["start"], run["end"]) for run in valid], [(0, 5), (5, 21)])

        split = [dict(runs[0], end=7), dict(runs[1], start=7)]
        gap = [dict(runs[0], end=4), dict(runs[1])]
        for bad in (split, gap, runs[:1]):
            with self.assertRaisesRegex(PhotoshopBridgeUpdateError, "invalid text styles"):
                validate_photoshop_bridge_updates(self.project, self._payload(runs=bad))

    def test_photoshop_line_breaks_inside_runs_keep_offsets_aligned(self) -> None:
        runs = self._update_runs(self.block)
        # Photoshop sends CR between paragraphs and ETX for soft breaks.
        raw = "Hello \U0001F600 world\r\nsec\x03ond"
        runs[1]["end"] = 23

        _, updates = validate_photoshop_bridge_updates(
            self.project, self._payload(translation=raw, runs=runs)
        )

        self.assertEqual(updates[0].translation, "Hello \U0001F600 world\nsec\nond")
        self.assertEqual([(run["start"], run["end"]) for run in updates[0].runs],
                         [(0, 5), (5, 22)])

    def test_each_new_field_conflicts_with_an_edit_made_in_the_app(self) -> None:
        fmt = self.block.fontformat
        cases = (
            ("alignment", 2, fmt, "alignment", 1, "alignment"),
            ("opacity", 0.5, fmt, "opacity", 0.8, "opacity"),
            ("angle", 15.0, self.block, "angle", 5.0, "rotation"),
        )
        for key, value, owner, attribute, app_value, name in cases:
            with self.subTest(key=key):
                payload = self._payload(**{key: value})
                validate_photoshop_bridge_updates(self.project, payload)
                old_value = getattr(owner, attribute)
                setattr(owner, attribute, app_value)
                try:
                    with self.assertRaisesRegex(PhotoshopBridgeUpdateError, rf"\({name}\)"):
                        validate_photoshop_bridge_updates(self.project, payload)
                finally:
                    setattr(owner, attribute, old_value)

    def test_runs_conflict_with_style_or_text_edits_made_in_the_app(self) -> None:
        runs = self._update_runs(self.block)
        runs[0]["color"] = [0, 255, 0]
        payload = self._payload(runs=runs)

        rich_text = self.block.rich_text
        self.block.rich_text = _styled_block(
            self.block.translation, [(0, 5, _char_format(20))]
        ).rich_text
        with self.assertRaisesRegex(PhotoshopBridgeUpdateError, r"\(text style\)"):
            validate_photoshop_bridge_updates(self.project, payload)

        # New runs are offsets into the text Photoshop received.
        self.block.rich_text = rich_text
        self.block.translation = "edited in the app"
        with self.assertRaisesRegex(PhotoshopBridgeUpdateError, r"\(text\)"):
            validate_photoshop_bridge_updates(self.project, payload)

    def test_fields_sent_unchanged_may_differ_in_the_app(self) -> None:
        payload = self._payload(alignment=0, opacity=1.0, angle=0.0)
        self.block.fontformat.alignment = 2
        self.block.angle = 45

        _, updates = validate_photoshop_bridge_updates(self.project, payload)

        self.assertEqual(updates[0][5:], (None, None, None, None))

    def test_payload_without_the_new_fields_still_applies(self) -> None:
        payload = self._payload(1, translation="changed", font_size=40)
        base = payload["updates"][0]["base"]
        for key in ("runs", "alignment", "opacity", "angle"):
            del base[key]

        result = MainWindow.apply_photoshop_bridge_updates(self.window, payload)

        self.assertEqual(result.applied, 1)
        self.assertEqual((self.plain.translation, self.plain.font_size), ("changed", 40))

        # A new field cannot be checked against a base that lacks it.
        payload = self._payload(1, alignment=2)
        del payload["updates"][0]["base"]["alignment"]
        with self.assertRaisesRegex(PhotoshopBridgeUpdateError, r"\(alignment\)"):
            validate_photoshop_bridge_updates(self.project, payload)

    def test_failed_save_restores_every_changed_field(self) -> None:
        rich_text, xyxy = self.block.rich_text, list(self.block.xyxy)
        runs = self._update_runs(self.block)
        runs[0]["color"] = [1, 2, 3]
        payload = self._payload(
            translation="Hello", runs=[dict(runs[0], end=5)], alignment=2,
            opacity=0.3, angle=20.0,
        )

        with patch.object(self.project, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                MainWindow.apply_photoshop_bridge_updates(self.window, payload)

        fmt = self.block.fontformat
        self.assertEqual((self.block.rich_text, self.block.xyxy), (rich_text, xyxy))
        self.assertEqual((fmt.alignment, fmt.opacity, self.block.angle), (0, 1.0, 0))
        self.assertEqual(self.block.translation, "Hello \U0001F600 world\nsecond")

    def test_style_converts_effects_to_photoshop_terms(self) -> None:
        runs = bridge_text_runs(self.block)[1]
        fmt = self.block.fontformat
        fmt.text_effects = TextEffectStack(effects=(
            StrokeEffect(enabled=False, width=0.5),
            StrokeEffect(width=0.1, position="inside", opacity=0.5,
                         paint=SolidPaint((255, 255, 255))),
            ShadowEffect(shadow_type="inner", angle=10),
            ShadowEffect(angle=90, distance=0.1, blur=0.3, spread=0.1, opacity=0.75),
            GlowEffect(size=0.2, spread=0.2, paint=SolidPaint((0, 255, 0))),
        ))

        style = bridge_block_style(self.block, runs)

        # Effect sizes follow the largest run (48 px); the font follows the first.
        self.assertEqual((style["font_size"], style["weight"], style["tracking"]), (48.0, 700, 150))
        stroke = style["stroke"]
        self.assertAlmostEqual(stroke.pop("width"), 2.4)
        self.assertEqual(stroke, {"position": "inside", "color": [255, 255, 255], "opacity": 0.5})
        shadow = style["shadow"]
        self.assertEqual((shadow["angle"], shadow["opacity"]), (90.0, 0.75))
        self.assertAlmostEqual(shadow["distance"], 4.8)
        self.assertAlmostEqual(shadow["size"], 19.2)
        self.assertAlmostEqual(shadow["spread"], 25.0)
        self.assertAlmostEqual(style["glow"]["size"], 19.2)
        self.assertEqual((style["glow"]["spread"], style["glow"]["color"]), (50.0, [0, 255, 0]))
        self.assertAlmostEqual(style["leading"], 1.2 * 48)

        fmt.line_spacing, fmt.line_spacing_type = 0.5, LineSpacingType.Distance
        style = bridge_block_style(self.block, runs)
        self.assertEqual(style["line_spacing"], {"type": "distance", "value": 0.5})
        self.assertEqual(style["leading"], 53.0)

    def test_stroke_position_and_shadow_angle_conversions(self) -> None:
        def stroke(position: str, *extra) -> dict:
            stack = TextEffectStack(effects=(StrokeEffect(width=0.2, position=position), *extra))
            return bridge_stroke(stack, 30)

        self.assertEqual((stroke("outside")["position"], stroke("outside")["width"]), ("outside", 3.0))
        self.assertEqual(stroke("inside")["width"], 3.0)
        # Without Hollow the app clips a center stroke to the outside half.
        self.assertEqual((stroke("center")["position"], stroke("center")["width"]), ("outside", 3.0))
        hollow = stroke("center", HollowEffect())
        self.assertEqual((hollow["position"], hollow["width"]), ("center", 6.0))
        self.assertIsNone(bridge_stroke(TextEffectStack(), 30))

        # BT: shadow direction clockwise from +x. Photoshop: light direction,
        # counterclockwise; the shadow falls opposite the light.
        for bt_angle, ps_angle in ((0, 180.0), (90, 90.0), (180, 0.0), (270, -90.0), (315, -135.0)):
            stack = TextEffectStack(effects=(ShadowEffect(angle=bt_angle),))
            self.assertEqual(bridge_shadow(stack, 30)["angle"], ps_angle, bt_angle)
        stack = TextEffectStack(effects=(ShadowEffect(blur=0, spread=0),))
        self.assertEqual(bridge_shadow(stack, 30)["spread"], 0.0)
        self.assertEqual(bridge_tracking(1.0), 0)
        self.assertEqual(bridge_tracking(0.9), -100)


if __name__ == "__main__":
    unittest.main()
