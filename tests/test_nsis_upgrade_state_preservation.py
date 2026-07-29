import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_CONFIG = ROOT / "Frontend" / "src-tauri" / "tauri.conf.json"
NSIS_TEMPLATE = ROOT / "Frontend" / "src-tauri" / "windows" / "installer-template.nsi"
UPSTREAM_TEMPLATE_SHA256 = "20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079"
SCRIBER_TEMPLATE_HEADER = (
    "; Vendored from tauri-apps/tauri tag tauri-cli-v2.11.3\n"
    "; source commit 6f6ab1207bb3923c2721fbc67d2fdb1c8deb0c7a.\n"
    "; Scriber delta: prefer overlay installation for version upgrades so Windows\n"
    "; shell pins and the user's autostart state remain intact.\n"
)


def _section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_tauri_uses_the_reviewed_scriber_nsis_template():
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    nsis = config["bundle"]["windows"]["nsis"]

    assert nsis["installMode"] == "currentUser"
    assert nsis["template"] == "./windows/installer-template.nsi"
    assert nsis["installerHooks"] == "./windows/installer-hooks.nsh"

    template = NSIS_TEMPLATE.read_text(encoding="utf-8")
    assert template.startswith(SCRIBER_TEMPLATE_HEADER)
    assert "{{installer_hooks}}" in template
    assert "!insertmacro NSIS_HOOK_PREINSTALL" in template
    assert "!insertmacro NSIS_HOOK_POSTINSTALL" in template
    assert "!insertmacro NSIS_HOOK_PREUNINSTALL" in template
    assert "!insertmacro NSIS_HOOK_POSTUNINSTALL" in template


def test_scriber_template_has_only_the_reviewed_delta_from_tauri():
    template = NSIS_TEMPLATE.read_text(encoding="utf-8")
    custom_block = """  ; Scriber version upgrades are always non-destructive overlays. Tauri's
  ; default maintenance page offers to run the old uninstaller first, which
  ; intentionally removes taskbar pins, Start-menu shortcuts, and autostart.
  ; Skip that page for an existing NSIS install; same-version maintenance,
  ; downgrades, and the required one-time WiX migration keep upstream behavior.
  ${If} $R0 = 1
  ${AndIf} $WixMode <> 1
    Abort
  ${EndIf}

"""

    assert template.count(custom_block) == 1
    reconstructed = template.removeprefix(SCRIBER_TEMPLATE_HEADER).replace(
        custom_block,
        "",
        1,
    )
    assert hashlib.sha256(reconstructed.encode("utf-8")).hexdigest() == (UPSTREAM_TEMPLATE_SHA256)


def test_manual_version_upgrade_uses_non_destructive_overlay_automatically():
    template = NSIS_TEMPLATE.read_text(encoding="utf-8")
    reinstall_page = _section(
        template,
        "Function PageReinstall\n",
        "Function PageReinstallUpdateSelection\n",
    )
    leave_page = _section(
        template,
        "Function PageLeaveReinstall\n",
        "; 5. Choose install directory page\n",
    )

    assert (
        """  ${ElseIf} $R0 = 1
    StrCpy $R1 "$(olderOrUnknownVersionInstalled)"
    StrCpy $R2 "$(uninstallBeforeInstalling)"
    StrCpy $R3 "$(dontUninstall)\""""
        in reinstall_page
    )
    overlay_guard = """  ${If} $R0 = 1
  ${AndIf} $WixMode <> 1
    Abort
  ${EndIf}"""
    assert overlay_guard in reinstall_page
    assert reinstall_page.index(overlay_guard) < reinstall_page.index("; Skip showing the page if passive")
    assert (
        """  ${ElseIf} $R0 = 1 ; Upgrading
    ${If} $R1 = 1              ; User chose to uninstall
      Goto reinst_uninstall
    ${Else}
      Goto reinst_done         ; User chose NOT to uninstall"""
        in leave_page
    )


def test_overlay_and_updater_paths_preserve_shortcuts_pins_and_autostart():
    template = NSIS_TEMPLATE.read_text(encoding="utf-8")
    uninstall = _section(
        template,
        "Section Uninstall\n",
        "Function CreateOrUpdateStartMenuShortcut\n",
    )

    shortcut_guard = _section(
        uninstall,
        "; Remove shortcuts if not updating\n",
        "; Remove registry information for add/remove programs\n",
    )
    assert "${If} $UpdateMode <> 1" in shortcut_guard
    assert "!insertmacro UnpinShortcut" in shortcut_guard
    assert 'Delete "$SMPROGRAMS' in shortcut_guard

    autostart_guard = _section(
        uninstall,
        "; Removes the Autostart entry for ${PRODUCTNAME}",
        "; Delete app data if the checkbox is selected",
    )
    assert "${If} $UpdateMode <> 1" in autostart_guard
    assert 'DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "${PRODUCTNAME}"' in autostart_guard
