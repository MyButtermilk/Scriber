from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_websocket_hook_uses_typed_contract() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-websocket.ts").read_text(
        encoding="utf-8"
    )

    assert "type ScriberWebSocketMessage" in source
    assert "isScriberWebSocketMessage" in source
    assert "data: any" not in source
    assert "JSON.parse(event.data) as unknown" in source


def test_tauri_backend_status_trusts_supervisor_readiness() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-backend-status.tsx").read_text(
        encoding="utf-8"
    )

    assert "loadBackendBaseUrlFromTauri" in source
    assert 'invoke<TauriBackendStatus>("ensure_backend_running")' in source
    assert "Tauri backend supervisor check failed; falling back to HTTP health probe." in source
    assert "void reportFrontendReady().catch" in source
    assert "return true;" in source[source.index('invoke<TauriBackendStatus>("ensure_backend_running")'):]
