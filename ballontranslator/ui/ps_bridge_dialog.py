import os
import sys
import glob
import re
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, List, Optional, Tuple

from qtpy.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QSizePolicy,
)
from qtpy.QtCore import QCoreApplication, QObject, Qt, QTimer
from qtpy.QtGui import QShowEvent

from ballontranslator.utils.logger import logger as LOGGER
from ballontranslator.utils.message import create_info_dialog
from ballontranslator.utils.proj_imgtrans import ProjImgTrans, TextBlkEncoder
from ballontranslator.utils.textblock import TextBlock


class PhotoshopBridgeUpdateError(ValueError):
    """Raised when a Photoshop update is stale or unsafe to apply."""


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, cls=TextBlkEncoder))


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _project_page_blocks(
    project: ProjImgTrans,
    page_name: str,
) -> Optional[List[TextBlock]]:
    blocks = project.pages.get(page_name)
    if blocks is None:
        blocks = getattr(project, "not_found_pages", {}).get(page_name)
    return blocks


_LINE_BREAKS = re.compile(r"\r\n|[\r\x03]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def normalize_bridge_text(text: str) -> str:
    r"""Turn Photoshop line breaks into newlines and drop other control characters.

    Photoshop ends paragraphs with CR and stores Shift+Enter breaks as ETX.

    >>> normalize_bridge_text("one\x03two\rthree\x07")
    'one\ntwo\nthree'
    """
    return _CONTROL_CHARS.sub("", _LINE_BREAKS.sub("\n", text))


def photoshop_bridge_block_snapshot(block: TextBlock) -> dict:
    """Return the block fields a Photoshop update must still find unchanged.

    Photoshop overwrites the translation and font size, so both must still be
    what it received. Source text and detection box identify the block. Rich
    text and the text box are left out: saving a page regenerates both, and
    Photoshop never writes them, so comparing them only causes false conflicts.
    """
    block_data = _json_value(block.to_dict())
    fontformat = block_data.get("fontformat") or {}
    return {
        "translation": block_data.get("translation", ""),
        "font_size": fontformat.get("font_size"),
        "text": block_data.get("text", []),
        "xyxy": block_data.get("xyxy", []),
    }


def bridge_values_match(sent: Any, current: Any) -> bool:
    """Compare a snapshot echoed by Photoshop with the project's value.

    ExtendScript prints numbers with 15 significant digits, so a font size of
    14.399999999999999 comes back as 14.4. Numbers match within that precision.

    >>> bridge_values_match({"font_size": 14.4}, {"font_size": 14.399999999999999})
    True
    >>> bridge_values_match({"font_size": 14.0}, {"font_size": 15.0})
    False
    """
    numbers = (int, float)
    if (
        isinstance(sent, numbers) and isinstance(current, numbers)
        and not isinstance(sent, bool) and not isinstance(current, bool)
    ):
        return math.isclose(sent, current, rel_tol=1e-12, abs_tol=1e-9)
    if isinstance(sent, dict) and isinstance(current, dict):
        return sent.keys() == current.keys() and all(
            bridge_values_match(sent[key], current[key]) for key in sent
        )
    if isinstance(sent, list) and isinstance(current, list):
        return len(sent) == len(current) and all(
            bridge_values_match(a, b) for a, b in zip(sent, current)
        )
    return sent == current


def project_state_matches_disk(project: ProjImgTrans) -> bool:
    """Return whether the live project still matches its canonical JSON file."""
    project_path = getattr(project, "proj_path", "")
    if not project_path or not os.path.isfile(project_path):
        return False
    try:
        with open(project_path, "r", encoding="utf-8") as project_file:
            disk_data = json.load(project_file)
        live_data = _json_value(project.to_dict())
        disk_data = _json_value(disk_data)
        # Page selection can change without dirtying project content and is
        # passed to Photoshop separately in the launch context.
        live_data.pop("current_img", None)
        disk_data.pop("current_img", None)
        return disk_data == live_data
    except (OSError, TypeError, ValueError):
        return False


def validate_photoshop_bridge_updates(
    project: ProjImgTrans,
    payload: dict,
) -> Tuple[str, List[Tuple[TextBlock, str, Optional[float]]]]:
    """Validate a complete bridge payload before any project state is changed.

    >>> from types import SimpleNamespace
    >>> from ballontranslator.utils.textblock import TextBlock
    >>> block = TextBlock(translation="old")
    >>> project = SimpleNamespace(
    ...     proj_path="project.json", pages={"page.png": [block]}
    ... )
    >>> payload = {
    ...     "version": 1,
    ...     "project_path": "project.json",
    ...     "page": "page.png",
    ...     "block_count": 1,
    ...     "updates": [{
    ...         "block_index": 0,
    ...         "translation": "new",
    ...         "font_size": None,
    ...         "base": photoshop_bridge_block_snapshot(block),
    ...     }],
    ... }
    >>> validate_photoshop_bridge_updates(project, payload)[0]
    'page.png'
    """
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise PhotoshopBridgeUpdateError("Unsupported Photoshop update format.")

    payload_path = payload.get("project_path")
    project_path = getattr(project, "proj_path", "")
    if (
        not isinstance(payload_path, str)
        or not project_path
        or _canonical_path(payload_path) != _canonical_path(project_path)
    ):
        raise PhotoshopBridgeUpdateError(
            "The Photoshop update belongs to a different project."
        )

    page_name = payload.get("page")
    if not isinstance(page_name, str):
        raise PhotoshopBridgeUpdateError("The Photoshop update has no valid page.")
    blocks = _project_page_blocks(project, page_name)
    if blocks is None:
        raise PhotoshopBridgeUpdateError(
            f"Page '{page_name}' is no longer in this project."
        )
    block_count = payload.get("block_count")
    if (
        not isinstance(block_count, int)
        or isinstance(block_count, bool)
        or block_count != len(blocks)
    ):
        raise PhotoshopBridgeUpdateError(
            f"The blocks on page '{page_name}' changed after it was opened in Photoshop."
        )

    raw_updates = payload.get("updates")
    if not isinstance(raw_updates, list):
        raise PhotoshopBridgeUpdateError("The Photoshop update list is invalid.")

    validated = []
    seen_indices = set()
    for update in raw_updates:
        if not isinstance(update, dict):
            raise PhotoshopBridgeUpdateError("A Photoshop block update is invalid.")
        block_index = update.get("block_index")
        if (
            not isinstance(block_index, int)
            or isinstance(block_index, bool)
            or block_index < 0
            or block_index >= len(blocks)
            or block_index in seen_indices
        ):
            raise PhotoshopBridgeUpdateError(
                "A Photoshop layer has an invalid or duplicate block number."
            )
        seen_indices.add(block_index)

        block = blocks[block_index]
        base = update.get("base")
        current = photoshop_bridge_block_snapshot(block)
        # Older scripts also send fields that are no longer compared.
        conflicts = [
            key for key, value in current.items()
            if not isinstance(base, dict)
            or key not in base
            or not bridge_values_match(base[key], value)
        ]
        if "text" in conflicts or "xyxy" in conflicts:
            raise PhotoshopBridgeUpdateError(
                f"Block #{block_index + 1} on page '{page_name}' no longer "
                "matches its Photoshop layer. Import the page into Photoshop "
                "again."
            )
        if conflicts:
            fields = ", ".join(
                "text" if key == "translation" else "font size"
                for key in conflicts
            )
            raise PhotoshopBridgeUpdateError(
                f"Block #{block_index + 1} on page '{page_name}' was edited in "
                f"BallonsTranslator ({fields}) after Photoshop received it. "
                "Import the page into Photoshop again."
            )

        translation = update.get("translation")
        if not isinstance(translation, str):
            raise PhotoshopBridgeUpdateError(
                f"Block #{block_index + 1} has invalid text."
            )
        font_size = update.get("font_size")
        if font_size is not None and (
            not isinstance(font_size, (int, float))
            or isinstance(font_size, bool)
            or not math.isfinite(font_size)
            or font_size <= 0
        ):
            raise PhotoshopBridgeUpdateError(
                f"Block #{block_index + 1} has an invalid font size."
            )
        validated.append(
            (
                block,
                normalize_bridge_text(translation),
                font_size,
            )
        )

    return page_name, validated


def photoshop_bridge_update_path(project_path: str) -> str:
    """Return where Photoshop leaves changes for a project."""
    return project_path + ".ps_bridge_updates.json"


def _tr(text: str) -> str:
    return QCoreApplication.translate("PhotoshopBridgeDialog", text)


def consume_photoshop_bridge_update(
    project_path: str,
    apply_updates: Callable[[dict], int],
) -> Optional[Tuple[str, str]]:
    """Apply the changes Photoshop left for a project.

    Returns None when nothing is pending, otherwise ``(state, message)``:

    - ``applied``: the changes are in the project and the file is removed.
    - ``waiting``: the file cannot be read yet; try again later.
    - ``blocked``: the app cannot apply now (for example unsaved changes); the
      file stays so a later check applies it.
    - ``rejected``: the changes conflict with the project. The file is renamed
      to ``*.rejected.json`` so it cannot block the next send from Photoshop.

    >>> consume_photoshop_bridge_update("missing/project.json", lambda p: 0) is None
    True
    """
    update_path = photoshop_bridge_update_path(project_path)
    if not os.path.isfile(update_path):
        return None
    try:
        with open(update_path, "r", encoding="utf-8") as update_file:
            payload = json.load(update_file)
    except (OSError, ValueError):
        return "waiting", _tr("Waiting for Photoshop to finish writing changes...")

    try:
        updated_count = apply_updates(payload)
    except PhotoshopBridgeUpdateError as error:
        LOGGER.warning("Photoshop Bridge update was rejected: %s", error)
        rejected_path = update_path[:-len(".json")] + ".rejected.json"
        try:
            os.replace(update_path, rejected_path)
        except OSError as move_error:
            LOGGER.warning("Could not set aside the rejected update: %s", move_error)
        return "rejected", str(error)
    except RuntimeError as error:
        return "blocked", str(error)

    try:
        os.remove(update_path)
    except OSError as error:
        LOGGER.warning("Could not remove the applied Photoshop update: %s", error)
    return "applied", _tr("Applied {count} Photoshop change(s).").format(
        count=updated_count
    )


class PhotoshopBridgeUpdatePoller(QObject):
    """Apply the changes Photoshop leaves for the open project.

    Photoshop can send changes at any time, and checking one file is cheap, so
    a short poll needs no file watcher bookkeeping when projects change. While
    the bridge window is visible it reports the outcome itself.
    """

    INTERVAL_MS = 1500

    def __init__(self, window: QObject) -> None:
        super().__init__(window)
        self._window = window
        self._blocked_message = ""
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.check)
        self._timer.start()

    def check(self) -> None:
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


# Photoshop's install folder holds Presets/Scripts next to the .app bundle.
MACOS_PHOTOSHOP_GLOBS = (
    '/Applications/Adobe Photoshop*/Adobe Photoshop*.app',
    '~/Applications/Adobe Photoshop*/Adobe Photoshop*.app',
)


def _macos_photoshop_paths() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return the newest Photoshop folder, its Scripts folder and its .app bundle.

    >>> _macos_photoshop_paths()  # doctest: +SKIP
    ('/Applications/Adobe Photoshop 2026', '/Applications/Adobe Photoshop 2026/Presets/Scripts', '/Applications/Adobe Photoshop 2026/Adobe Photoshop 2026.app')
    """
    apps = [
        app for pattern in MACOS_PHOTOSHOP_GLOBS
        for app in glob.glob(os.path.expanduser(pattern)) if os.path.isdir(app)
    ]
    if not apps:
        return None, None, None

    def release_year(app: str) -> int:
        # "Adobe Photoshop 2026" sorts below "Adobe Photoshop CC 2019" by name.
        years = re.findall(r'\d{4}', os.path.basename(app))
        return int(years[-1]) if years else 0

    app_path = max(apps, key=release_year)
    ps_dir = os.path.dirname(app_path)
    scripts_dir = os.path.join(ps_dir, 'Presets', 'Scripts')
    return ps_dir, scripts_dir if os.path.isdir(scripts_dir) else None, app_path


def get_photoshop_paths():
    """Detect Photoshop's install folder, Scripts folder and executable.

    Windows reads the App Paths registry keys; macOS looks for the .app bundle.
    """
    ps_dir = None
    scripts_dir = None
    exe_path = None

    if sys.platform == 'darwin':
        return _macos_photoshop_paths()

    if sys.platform == 'win32':
        import winreg

        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"),
        ]
        for hive, subkey in reg_keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    try:
                        p, _ = winreg.QueryValueEx(key, "Path")
                        if os.path.isdir(p):
                            ps_dir = os.path.normpath(p)
                    except OSError:
                        pass
                    try:
                        exe, _ = winreg.QueryValueEx(key, "")
                        exe_cleaned = exe.strip().strip('"')
                        if os.path.isfile(exe_cleaned):
                            exe_path = exe_cleaned
                            if not ps_dir:
                                ps_dir = os.path.dirname(exe_cleaned)
                    except OSError:
                        pass
                    if ps_dir:
                        break
            except OSError:
                pass

        if not ps_dir:
            for pattern in [
                r"C:\Program Files\Adobe\Adobe Photoshop *",
                r"D:\Program Files\Adobe\Adobe Photoshop *",
                r"E:\Program Files\Adobe\Adobe Photoshop *",
                r"F:\Program Files\Adobe\Adobe Photoshop *",
            ]:
                for match in glob.glob(pattern):
                    if os.path.isdir(match):
                        ps_dir = os.path.normpath(match)
                        break
                if ps_dir:
                    break

        if ps_dir:
            cand_scripts = os.path.join(ps_dir, "Presets", "Scripts")
            if os.path.isdir(cand_scripts):
                scripts_dir = cand_scripts
            if not exe_path:
                cand_exe = os.path.join(ps_dir, "Photoshop.exe")
                if os.path.isfile(cand_exe):
                    exe_path = cand_exe

    return ps_dir, scripts_dir, exe_path


def open_with_system(path: str, reveal: bool = False) -> None:
    """Open a file or folder with the platform's default handler.

    ``reveal`` selects the item in Explorer or Finder instead of opening it.

    >>> open_with_system('/tmp')  # doctest: +SKIP
    """
    if sys.platform == 'win32':
        if reveal:
            subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"')
        else:
            os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', path] if reveal else ['open', path])
    else:
        subprocess.Popen(['xdg-open', os.path.dirname(path) if reveal else path])


def photoshop_script_command(exe_path: str, jsx_path: str) -> List[str]:
    """Build the command that makes Photoshop run a JSX script.

    macOS has no ``-r`` switch, so AppleScript feeds the script text to
    Photoshop; the path travels as an argument to avoid quoting issues. The
    script can wait on dialogs for minutes, so AppleScript does not wait for
    Photoshop's reply (that times out after two minutes and logs an error).

    >>> photoshop_script_command('Photoshop.exe', 'b.jsx')  # doctest: +SKIP
    ['Photoshop.exe', '-r', 'b.jsx']
    """
    if sys.platform != 'darwin':
        return [exe_path, '-r', jsx_path]
    app_name = os.path.splitext(os.path.basename(exe_path))[0].replace('"', '')
    return [
        'osascript',
        '-e', 'on run argv',
        '-e', 'set script_text to read (POSIX file (item 1 of argv)) as «class utf8»',
        '-e', 'ignoring application responses',
        '-e', f'tell application "{app_name}" to do javascript script_text',
        '-e', 'end ignoring',
        '-e', 'end run',
        jsx_path,
    ]


def extract_jsx_version(jsx_path: str) -> str:
    """Read version metadata from a JSX script header."""
    if not jsx_path or not os.path.isfile(jsx_path):
        return ""
    try:
        with open(jsx_path, 'r', encoding='utf-8', errors='ignore') as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                m = re.search(r'(?:Version:\s*|BT_BRIDGE_VERSION\s*=\s*["\'])([\d\.]+)', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def get_file_md5(file_path: str) -> str:
    """Calculate MD5 hash of file content for exact equality check."""
    if not file_path or not os.path.isfile(file_path):
        return ""
    try:
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""


class PhotoshopBridgeDialog(QDialog):
    def __init__(self, parent=None, project=None) -> None:
        super().__init__(parent)
        self.project = project
        self.setObjectName("PhotoshopBridgeDialog")
        self.setWindowTitle(self.tr("Photoshop Bridge"))
        self.setMinimumWidth(480)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        self.setWindowFlags(
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowCloseButtonHint
        )

        self.init_ui()
        self.refresh_status()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_status_fit()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 14)

        self.setStyleSheet("""
            QDialog#PhotoshopBridgeDialog QLabel,
            QDialog#PhotoshopBridgeDialog QGroupBox {
                background-color: transparent;
            }
            QPushButton#OpenPSBtn {
                background-color: rgb(30, 147, 229);
                color: #ffffff;
                font-weight: bold;
                border: 1px solid rgb(25, 125, 195);
                border-radius: 4px;
                font-size: 13px;
            }
            QPushButton#OpenPSBtn:hover {
                background-color: rgb(45, 160, 245);
            }
            QPushButton#OpenPSBtn:pressed {
                background-color: rgb(20, 120, 190);
            }
            QPushButton#OpenPSBtn:disabled {
                background-color: rgba(30, 147, 229, 60);
                color: rgba(255, 255, 255, 120);
                border: 1px solid transparent;
            }
        """)

        # 1. Status Group
        status_group = QGroupBox(self.tr("Photoshop Integration Status"))
        status_group.setObjectName("PSStatusGroup")
        status_layout = QFormLayout(status_group)
        # macOS keeps fields at their size hint by default, so a wrapped status
        # stayed narrow while its height was computed for another width.
        status_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        status_layout.setSpacing(8)
        status_layout.setContentsMargins(12, 12, 12, 12)

        lbl_ps = QLabel(self.tr("Photoshop:"))
        lbl_ps.setStyleSheet("background: transparent;")
        self.ps_status_label = QLabel(self.tr("Checking Photoshop installation..."))
        self.ps_status_label.setObjectName("PSStatusFieldLabel")
        self.ps_status_label.setStyleSheet("background: transparent;")
        self.ps_status_label.setWordWrap(True)
        status_layout.addRow(lbl_ps, self.ps_status_label)

        lbl_script = QLabel(self.tr("Bridge Script:"))
        lbl_script.setStyleSheet("background: transparent;")
        self.script_status_label = QLabel(self.tr("Checking Bridge script status..."))
        self.script_status_label.setObjectName("PSStatusFieldLabel")
        self.script_status_label.setStyleSheet("background: transparent;")
        self.script_status_label.setWordWrap(True)
        status_layout.addRow(lbl_script, self.script_status_label)

        lbl_sync = QLabel(self.tr("Photoshop Changes:"))
        lbl_sync.setStyleSheet("background: transparent;")
        self.sync_status_label = QLabel(self.tr("No pending changes"))
        self.sync_status_label.setWordWrap(True)
        self.sync_status_label.setStyleSheet(
            "background: transparent; color: gray;"
        )
        status_layout.addRow(lbl_sync, self.sync_status_label)

        self.check_update_btn = QPushButton(self.tr("Check Update"))
        self.check_update_btn.setFixedHeight(28)
        self.check_update_btn.clicked.connect(self.refresh_status)
        status_layout.addRow("", self.check_update_btn)

        layout.addWidget(status_group)

        # 2. Quick Actions Group
        actions_group = QGroupBox(self.tr("Bridge Quick Actions"))
        actions_group.setObjectName("PSActionsGroup")
        actions_layout = QVBoxLayout(actions_group)
        actions_layout.setSpacing(8)
        actions_layout.setContentsMargins(12, 12, 12, 12)

        # Launch / Open in Photoshop button
        self.open_ps_btn = QPushButton(self.tr("Open This Project in Photoshop"))
        self.open_ps_btn.setObjectName("OpenPSBtn")
        self.open_ps_btn.setFixedHeight(36)
        self.open_ps_btn.clicked.connect(self.on_open_in_photoshop)
        actions_layout.addWidget(self.open_ps_btn)

        # Install / Update script button
        self.install_btn = QPushButton(self.tr("Install / Update Script in Photoshop"))
        self.install_btn.setFixedHeight(30)
        self.install_btn.clicked.connect(self.on_install_script)
        actions_layout.addWidget(self.install_btn)

        # Open Scripts folder in Explorer
        self.open_folder_btn = QPushButton(self.tr("Show Scripts Folder"))
        self.open_folder_btn.setFixedHeight(30)
        self.open_folder_btn.clicked.connect(self.on_open_folder)
        actions_layout.addWidget(self.open_folder_btn)

        layout.addWidget(actions_group)

        # 3. Help info
        info_label = QLabel(
            self.tr(
                "Tip: Inside Photoshop, run the bridge via File -> Scripts -> BallonTranslator_PS_Bridge.\n"
                "You can export layers, edit texts/strokes, and sync changes back into BallonsTranslator."
            )
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("background: transparent; color: gray; font-size: 11px;")
        layout.addWidget(info_label)

    def refresh_status(self) -> None:
        ps_dir, scripts_dir, exe_path = get_photoshop_paths()
        source_jsx = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "export to photoshop", "BallonTranslator_PS_Bridge.jsx"
        )

        source_ver = extract_jsx_version(source_jsx) or "2.6.2"
        source_hash = get_file_md5(source_jsx)

        installed_jsx = os.path.join(scripts_dir, "BallonTranslator_PS_Bridge.jsx") if scripts_dir else None
        is_installed = bool(installed_jsx and os.path.isfile(installed_jsx))

        if ps_dir:
            ps_name = os.path.basename(ps_dir)
            self.ps_status_label.setText(f"<span style='color: #4CAF50; font-weight: bold;'>{ps_name}</span>")
            self.install_btn.setEnabled(True)
            self.open_ps_btn.setEnabled(True)
            self.open_ps_btn.setToolTip("")
        else:
            self.ps_status_label.setText(f"<span style='color: #F44336; font-weight: bold;'>{self.tr('Not detected')}</span>")
            self.install_btn.setEnabled(False)
            self.open_ps_btn.setEnabled(False)
            self.open_ps_btn.setToolTip(self.tr("Photoshop installation not detected"))

        if is_installed:
            installed_ver = extract_jsx_version(installed_jsx) or "1.0.0"
            installed_hash = get_file_md5(installed_jsx)

            is_identical = (source_hash and installed_hash and source_hash == installed_hash)

            if is_identical or installed_ver == source_ver:
                self.script_status_label.setText(
                    f"<span style='color: #4CAF50; font-weight: bold;'>{self.tr('Installed')} (v{installed_ver})</span>"
                )
                self.install_btn.setText(self.tr("Reinstall Script in Photoshop"))
            else:
                self.script_status_label.setText(
                    f"<span style='color: #FFA000; font-weight: bold;'>{self.tr('Update available')} (v{installed_ver} -> v{source_ver})</span>"
                )
                self.install_btn.setText(self.tr("Update Script in Photoshop"))
        else:
            self.script_status_label.setText(
                f"<span style='color: #F44336; font-weight: bold;'>{self.tr('Not installed')} ({self.tr('v{version} available').format(version=source_ver)})</span>"
            )
            self.install_btn.setText(self.tr("Install Script in Photoshop"))

        self._schedule_status_fit()
        self.check_for_update()

    def _set_sync_status(self, message: str, color: str) -> None:
        self.sync_status_label.setText(message)
        self.sync_status_label.setStyleSheet(
            f"background: transparent; color: {color};"
        )
        self._schedule_status_fit()

    def _schedule_status_fit(self) -> None:
        # A label's new height is known only after Qt processes its relayout.
        QTimer.singleShot(0, self._fit_wrapped_status)

    def _fit_wrapped_status(self) -> None:
        """Grow the window when a status label wraps onto more lines.

        A shown top-level window does not grow for height-for-width rows by
        itself, so a status that wraps after showing would be clipped.
        """
        if not self.isVisible():
            return
        needed = self.layout().totalHeightForWidth(self.width())
        if needed > self.height():
            self.resize(self.width(), needed)

    def check_for_update(self) -> None:
        """Apply pending Photoshop changes and show the outcome."""
        project_path = getattr(self.project, "proj_path", "")
        if not project_path:
            return
        apply_updates = getattr(
            self.parent(), "apply_photoshop_bridge_updates", None
        )
        if not callable(apply_updates):
            if os.path.isfile(photoshop_bridge_update_path(project_path)):
                self._set_sync_status(
                    self.tr("BallonsTranslator cannot apply Photoshop changes."),
                    "#F44336",
                )
            return
        result = consume_photoshop_bridge_update(project_path, apply_updates)
        if result is None:
            return
        state, message = result
        colors = {"applied": "#4CAF50", "rejected": "#F44336"}
        self._set_sync_status(message, colors.get(state, "#FFA000"))

    def on_install_script(self) -> None:
        ps_dir, scripts_dir, _ = get_photoshop_paths()
        source_jsx = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "export to photoshop", "BallonTranslator_PS_Bridge.jsx"
        )
        if not ps_dir or not os.path.isfile(source_jsx):
            QMessageBox.warning(
                self,
                self.tr("Error"),
                self.tr("Photoshop or the Bridge JSX script was not found."),
            )
            return

        scripts_dir = scripts_dir or os.path.join(ps_dir, "Presets", "Scripts")
        target_jsx = os.path.join(scripts_dir, os.path.basename(source_jsx))
        is_update = os.path.isfile(target_jsx)

        def _show_success() -> None:
            self.refresh_status()
            if is_update:
                QMessageBox.information(
                    self,
                    self.tr("Updated"),
                    self.tr("Bridge script updated. It is ready to use."),
                )
            else:
                QMessageBox.information(
                    self,
                    self.tr("Installed"),
                    self.tr("Bridge script installed. Restart Photoshop to refresh its Scripts menu."),
                )

        try:
            os.makedirs(scripts_dir, exist_ok=True)
            shutil.copy2(source_jsx, target_jsx)
        except PermissionError as error:
            LOGGER.warning("Photoshop Bridge requires manual installation: %s", error)
            QMessageBox.warning(
                self,
                self.tr("Manual Installation Required"),
                self.tr(
                    "BallonsTranslator cannot write to Photoshop's Scripts "
                    "folder without administrator permission.\n\n"
                    "Copy this file:\n{source}\n\n"
                    "To this folder:\n{destination}\n\n"
                    "Then restart Photoshop. Explorer will now open the source "
                    "file and destination location."
                ).format(
                    source=os.path.normpath(source_jsx),
                    destination=os.path.normpath(scripts_dir),
                ),
            )
            self._open_manual_install_folders(source_jsx, scripts_dir)
            return
        except OSError as error:
            LOGGER.warning("Failed to install Photoshop Bridge: %s", error)
            QMessageBox.warning(
                self,
                self.tr("Error"),
                self.tr("Failed to install the Bridge script: {error}").format(
                    error=error
                ),
            )
            return

        _show_success()

    def _open_manual_install_folders(
        self,
        source_jsx: str,
        scripts_dir: str,
    ) -> None:
        destination_dir = scripts_dir
        while destination_dir and not os.path.isdir(destination_dir):
            parent_dir = os.path.dirname(destination_dir)
            if parent_dir == destination_dir:
                break
            destination_dir = parent_dir
        try:
            open_with_system(os.path.normpath(source_jsx), reveal=True)
            if destination_dir and os.path.isdir(destination_dir):
                open_with_system(os.path.normpath(destination_dir))
        except OSError as error:
            LOGGER.warning("Failed to open manual Photoshop install folders: %s", error)

    def on_open_in_photoshop(self) -> None:
        _, scripts_dir, exe_path = get_photoshop_paths()
        bundled_jsx = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "export to photoshop", "BallonTranslator_PS_Bridge.jsx"
        )
        installed_jsx = (
            os.path.join(scripts_dir, "BallonTranslator_PS_Bridge.jsx")
            if scripts_dir else None
        )
        # The quick action must use the app-matched protocol even when an older
        # copy is still installed in Photoshop's Scripts menu.
        target_jsx = bundled_jsx if os.path.isfile(bundled_jsx) else installed_jsx

        if not target_jsx or not os.path.isfile(target_jsx):
            QMessageBox.warning(self, self.tr("Error"), self.tr("Bridge JSX script not found."))
            return

        context_written = False
        # Save through the application before Photoshop takes its conflict-check
        # snapshot, then give this launch a unique return sidecar.
        if self.project and getattr(self.project, 'proj_path', None) and os.path.isfile(self.project.proj_path):
            try:
                prepare_context = getattr(
                    self.parent(), "prepare_photoshop_bridge_context", None
                )
                if callable(prepare_context):
                    ctx_data = prepare_context()
                else:
                    ctx_data = {
                        "project_path": os.path.abspath(self.project.proj_path),
                        "active_page": getattr(
                            self.project, "current_img", ""
                        ) or "",
                    }
                context_file = os.path.join(tempfile.gettempdir(), "bt_ps_bridge_context.json")
                ctx_data.update({
                    "project_path": os.path.abspath(
                        ctx_data["project_path"]
                    ).replace("\\", "/"),
                    "timestamp": time.time(),
                })
                context_tmp = context_file + ".tmp"
                with open(context_tmp, "w", encoding="utf-8") as context_stream:
                    json.dump(ctx_data, context_stream, ensure_ascii=False, indent=2)
                os.replace(context_tmp, context_file)
                context_written = True
            except Exception as ctx_err:
                LOGGER.warning("Failed to prepare Photoshop Bridge context: %s", ctx_err)
                QMessageBox.warning(
                    self,
                    self.tr("Error"),
                    f"{self.tr('Failed to prepare Photoshop Bridge')}: {ctx_err}",
                )
                return

        # On macOS the executable is the .app bundle, which is a directory.
        if exe_path and os.path.exists(exe_path):
            try:
                subprocess.Popen(photoshop_script_command(exe_path, target_jsx))
            except Exception as e:
                LOGGER.error(f"Failed to launch Photoshop with script: {e}")
                open_with_system(target_jsx)
        else:
            try:
                open_with_system(target_jsx)
            except Exception as e:
                QMessageBox.warning(self, self.tr("Error"), f"{self.tr('Failed to open Photoshop')}: {e}")
                return

        if context_written:
            self._set_sync_status(
                self.tr("Waiting for changes from Photoshop..."),
                "#FFA000",
            )

    def on_open_folder(self):
        scripts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "export to photoshop"
        )
        if os.path.isdir(scripts_dir):
            open_with_system(scripts_dir)
