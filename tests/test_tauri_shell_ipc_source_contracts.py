from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_IPC_SOURCE = REPO_ROOT / "Frontend" / "src-tauri" / "src" / "shell_ipc.rs"


def test_shell_ipc_clipboard_uses_owner_window():
    source = SHELL_IPC_SOURCE.read_text(encoding="utf-8")

    assert "const CLIPBOARD_OWNER_CLASS: &str = \"ScriberClipboardOwner\";" in source
    assert "OpenClipboard(owner.hwnd())" in source
    assert "OpenClipboard(ptr::null_mut())" not in source
    assert "OpenClipboard(std::ptr::null_mut())" not in source
