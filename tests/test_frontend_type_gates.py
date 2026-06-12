from __future__ import annotations

import json
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


def test_startup_screen_handles_managed_backend_starting_state() -> None:
    hook_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-backend-status.tsx"
    ).read_text(encoding="utf-8")
    banner_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "BackendOfflineBanner.tsx"
    ).read_text(encoding="utf-8")

    assert "backendStarting: boolean;" in hook_source
    assert "backendMessage: string | null;" in hook_source
    assert "function isManagedBackendStarting" in hook_source
    assert "!status.managed || !status.running || status.ready" in hook_source
    assert "message.includes(\"starting\")" in hook_source
    assert "message.includes(\"process started\")" in hook_source
    assert "setBackendStarting(isManagedBackendStarting(status));" in hook_source

    assert "\"Managed backend is starting\"" in banner_source
    assert "backendStarting || (!startupGraceElapsed && isStartupRecoverable)" in banner_source
    assert "checkCount < 3" not in banner_source
    assert "Backend Not Available" in banner_source
    assert "Starting Scriber" in banner_source


def test_frontend_uses_current_svg_logo_asset() -> None:
    index_html = (REPO_ROOT / "Frontend" / "client" / "index.html").read_text(encoding="utf-8")
    favicon_svg = (REPO_ROOT / "Frontend" / "client" / "public" / "favicon.svg").read_text(
        encoding="utf-8"
    )

    assert 'type="image/svg+xml"' in index_html
    assert 'href="/favicon.svg"' in index_html
    assert 'href="/favicon.png"' not in index_html
    assert 'viewBox="5 99.4 118 76"' in favicon_svg
    assert "#253037" in favicon_svg
    assert "#CFAF6A" in favicon_svg


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


def test_settings_hotkey_recorder_uses_window_capture_listener() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    assert "function hotkeyDisplayFromKeyboardEvent" in source
    assert "const hotkeyCaptureRef = useRef<HTMLDivElement | null>(null);" in source
    assert "setGlobalHotkeyCaptureActive," in source
    assert "void setGlobalHotkeyCaptureActive(true).catch" in source
    assert "void setGlobalHotkeyCaptureActive(false).catch" in source
    assert "await setGlobalHotkeyCaptureActive(false);" in source
    assert "await refreshGlobalHotkey();" in source
    assert "hotkeyCaptureRef.current?.focus()" in source
    assert 'window.addEventListener("keydown", handleWindowKeyDown, true)' in source
    assert 'window.removeEventListener("keydown", handleWindowKeyDown, true)' in source
    assert 'event.key === "Escape"' in source
    assert 'aria-label="Hotkey capture area"' in source
    assert "onKeyDown={handleHotkeyRecord}" not in source


def test_desktop_chrome_is_dom_rendered_without_duplicate_branding() -> None:
    tauri_config = json.loads(
        (REPO_ROOT / "Frontend" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    tauri_capabilities = json.loads(
        (REPO_ROOT / "Frontend" / "src-tauri" / "capabilities" / "default.json").read_text(
            encoding="utf-8"
        )
    )
    layout_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "layout" / "AppLayout.tsx"
    ).read_text(encoding="utf-8")
    titlebar_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "DesktopTitleBar.tsx"
    ).read_text(encoding="utf-8")
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert tauri_config["app"]["windows"][0]["decorations"] is False
    permissions = set(tauri_capabilities["permissions"])
    assert "core:window:allow-close" in permissions
    assert "core:window:allow-minimize" in permissions
    assert "core:window:allow-start-dragging" in permissions
    assert "core:window:allow-toggle-maximize" in permissions
    assert "DesktopTitleBar" in layout_source
    assert "data-tauri-drag-region" in titlebar_source
    assert "getCurrentWindow" in titlebar_source
    assert "startDragging" in titlebar_source
    assert "handleDragPointerDown" in titlebar_source
    assert "minimize" in titlebar_source
    assert "toggleMaximize" in titlebar_source
    assert "close" in titlebar_source
    assert "Scriber" not in titlebar_source
    assert ".desktop-titlebar" in css
    assert "background: hsl(var(--sidebar));" in css
    assert "border-bottom: 1px solid hsl(var(--border) / 0.32);" not in css
    assert "-webkit-app-region: drag;" in css
    assert "-webkit-app-region: no-drag;" in css


def test_theme_reveal_controls_desktop_chrome_and_card_repaints() -> None:
    provider_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "theme-provider.tsx"
    ).read_text(encoding="utf-8")
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert 'THEME_REVEAL_ACTIVE_DATASET_KEY = "themeRevealActive"' in provider_source
    assert "deferredDesktopThemeRef" in provider_source
    assert "setThemeRevealActive(true)" in provider_source
    assert "setThemeRevealActive(false)" in provider_source
    assert "void transition.finished.then(finishReveal, finishReveal)" in provider_source
    assert "window.setTimeout(finishReveal, THEME_TRANSITION_DURATION_MS + 140)" in provider_source
    assert "void fallbackCircularThemeReveal(transitionOrigin, nextResolvedTheme, commitTheme)" in provider_source
    assert 'html[data-theme-reveal-active="true"] *' in css
    assert "transition-property: opacity, transform, filter !important;" in css
    assert 'html[data-theme-reveal-active="true"] .theme-reveal-overlay' in css


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
