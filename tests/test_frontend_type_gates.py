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
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(
        encoding="utf-8"
    )

    assert "export interface BackendHealthResponse" in api_types
    assert "loadBackendBaseUrlFromTauri" in source
    assert 'invoke<TauriBackendStatus>("ensure_backend_running")' in source
    assert "type BackendHealthResponse" in source
    assert "health?.apiVersion === REST_API_VERSION" in source
    assert "health.ok === true && health.ready === true" in source
    assert "Tauri backend supervisor check failed; falling back to HTTP health probe." in source
    assert "void reportFrontendReady().catch" in source
    assert "return true;" in source[source.index('invoke<TauriBackendStatus>("ensure_backend_running")'):]


def test_settings_microphones_use_shared_api_types() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    websocket_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "contexts" / "WebSocketContext.tsx"
    ).read_text(encoding="utf-8")
    device_refresh_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-device-change-refresh.ts"
    ).read_text(encoding="utf-8")
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(
        encoding="utf-8"
    )

    assert "export interface MicrophoneDevice" in api_types
    assert "export interface MicrophonesResponse" in api_types
    assert "export interface MicrophonesRefreshResponse" in api_types
    assert "import type {" in source
    assert "MicrophoneDevice," in source
    assert "MicrophonesResponse," in source
    assert "useState<MicrophoneDevice[]>([])" in source
    assert "(await micsRes.json()) as MicrophonesResponse" in source
    assert "(await res.json()) as MicrophonesResponse" in source
    assert "import type { MicrophoneDevice }" in websocket_source
    assert "devices: MicrophoneDevice[]" in websocket_source
    assert "import type { MicrophonesRefreshResponse }" in device_refresh_source
    assert "Promise<MicrophonesRefreshResponse | null>" in device_refresh_source
    assert "as { deviceId: string" not in source


def test_youtube_page_proxies_thumbnails_and_hides_completed_spinners() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(
        encoding="utf-8"
    )
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(
        encoding="utf-8"
    )

    assert "export interface YouTubeSearchItem" in api_types
    assert "export interface YouTubeSearchResponse" in api_types
    assert "TranscriptDetailResponse," in source
    assert "YouTubeSearchItem," in source
    assert "YouTubeSearchResponse," in source
    assert "(await res.json()) as YouTubeSearchItem" in source
    assert "(await res.json()) as YouTubeSearchResponse" in source
    assert "(await res.json()) as TranscriptDetailResponse" in source
    assert "type YouTubeSearchItem = {" not in source
    assert "/api/youtube/thumbnail?url=" in source
    assert "encodeURIComponent(value)" in source
    assert "decoding=\"async\"" in source
    assert "referrerPolicy=\"no-referrer\"" in source
    assert "URL.createObjectURL(blob)" not in source
    assert "function isCompletedStep" in source
    assert "function isVisiblyProcessing" in source
    assert "const isProcessing = isVisiblyProcessing(item);" in source


def test_live_mic_reconciles_active_state_and_websocket_reconnects() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx").read_text(
        encoding="utf-8"
    )

    assert "type BackendLiveStateSnapshot" in source
    assert "const applyBackendStateSnapshot = useCallback" in source
    assert "applyBackendStateSnapshot(msg);" in source
    assert "const { isConnected } = useSharedWebSocket(handleWsMessage);" in source
    assert "if (!hasActiveSession && !isConnected)" in source
    assert "hasActiveSession ? 750 : 0" in source
    assert "hasActiveSession ? window.setInterval(reconcileBackendState, 2000) : undefined" in source
    assert "applyBackendStateSnapshot(state);" in source
    assert 'recordingState !== "finalizing"' not in source


def test_debug_and_settings_controls_have_responsive_density() -> None:
    debug_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "DebugConsole.tsx").read_text(
        encoding="utf-8"
    )
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert "debug-console-actions" in debug_source
    assert "debug-console-action-button" in debug_source
    assert "debug-console-action-label" in debug_source
    assert "Download support bundle" in debug_source
    assert "Support bundle downloaded as ${filename}. Check your Downloads folder." in debug_source
    assert "was saved by the browser download manager" in debug_source
    assert 'className="compact-impact-switch"' in debug_source

    assert "settings-page" in settings_source
    assert "settings-control-row" in settings_source
    assert "settings-page .impact-echo-switch" in css
    assert "--impact-switch-track-width: 64px" in css
    assert ".debug-console-actions" in css
    assert ".debug-console-action-label" in css
    assert "grid-template-columns: repeat(5, minmax(2.25rem, 1fr))" in css
    assert ".settings-page .mic-device-dropdown-header" in css
    assert "@media (max-width: 720px)" in css


def test_transcript_detail_uses_typed_rest_queries() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx").read_text(
        encoding="utf-8"
    )
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(
        encoding="utf-8"
    )

    assert "export type TranscriptDetailResponse = TranscriptHistoryItem" in api_types
    assert "import type {" in source
    assert "SettingsResponse," in source
    assert "TranscriptDetailResponse," in source
    assert "TranscriptHistoryItem" in source
    assert "useQuery<SettingsResponse>" in source
    assert "useQuery<TranscriptDetailResponse>" in source
    assert "staleTime: 0" in source
    assert "refetchIntervalInBackground: true" in source
    assert "const data = query.state.data;" in source
    assert "const transcript: TranscriptDetailResponse" in source
    assert "(await rec.json()) as TranscriptHistoryItem" not in source
    assert "(await res.json()) as TranscriptHistoryItem" in source
    assert "query: any" not in source
    assert "const transcript: any" not in source


def test_recording_popup_uses_canvas_waveform_without_react_frame_state() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "components" / "RecordingPopup.tsx").read_text(
        encoding="utf-8"
    )

    assert "const canvas = canvasRef.current;" in source
    assert "requestAnimationFrame(draw)" in source
    assert "setAudioLevels" not in source
