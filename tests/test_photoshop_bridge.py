import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtGui import QFont, QFontDatabase
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
    PhotoshopBridgeDialog,
    PhotoshopBridgeUpdateError,
    consume_photoshop_bridge_update,
    get_photoshop_paths,
    photoshop_bridge_block_snapshot,
    project_state_matches_disk,
    validate_photoshop_bridge_updates,
)
from ballontranslator.ui.ps_bridge_server import PhotoshopBridgeService
from ballontranslator.utils.proj_imgtrans import ProjImgTrans, TextBlkEncoder
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

        count = MainWindow.apply_photoshop_bridge_updates(fake_window, payload)

        self.assertEqual(count, 2)
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
            font_family="Bebas Neue",
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

        count = MainWindow.apply_photoshop_bridge_updates(fake_window, payload)

        self.assertEqual(count, 1)
        self.assertEqual(block.translation, "three")
        self.assertEqual(block.font_family, "Bebas Neue")
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
        apply_updates = Mock(return_value=1)

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

        def apply_updates(payload: dict) -> int:
            validate_photoshop_bridge_updates(self.project, payload)
            return 1

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

            window.apply_photoshop_bridge_updates = Mock(return_value=1)
            service.check_updates()

        self.assertEqual(info.call_count, 2)
        self.assertIn("Applied 1", info.call_args.args[0])
        self.assertFalse(os.path.exists(update_path))
        window.deleteLater()

    def test_poller_leaves_reporting_to_a_visible_bridge_window(self) -> None:
        self._write_update(self._payload(0))
        window = self._poller_window(Mock(return_value=1))
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

if __name__ == "__main__":
    unittest.main()
