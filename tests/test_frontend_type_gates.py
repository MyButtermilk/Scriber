from __future__ import annotations

import json
from pathlib import Path

from PIL import Image


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
    assert "checkInFlightRef.current" in source
    assert "const request = runHealthCheck();" in source
    assert "if (existing)" in source
    assert "clearTimeout(timeoutId);" in source
    assert "TAURI_ACCESS_TIMEOUT_MS" in source
    assert "TAURI_SUPERVISOR_TIMEOUT_MS" in source
    assert "withDeadline(" in source


def test_app_initial_tauri_lookup_has_deadline_and_fallback() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )

    assert "withPromiseTimeout(" in source
    assert '"Initial Tauri backend lookup"' in source
    assert "continuing with health fallback" in source
    assert "setBackendBaseReady(true)" in source

    backend_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts"
    ).read_text(encoding="utf-8")
    assert '"Tauri backend access"' in backend_source
    assert '"Tauri backend URL fallback"' in backend_source
    assert "backendAccessRetryAfterMs" in backend_source


def test_settings_bootstrap_cache_rejects_stale_inflight_results() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "settings-bootstrap.ts"
    ).read_text(encoding="utf-8")

    assert "bootstrapGeneration += 1" in source
    assert "requestGeneration === bootstrapGeneration" in source
    assert "inflightBootstrap === request" in source


def test_history_and_command_requests_are_abortable_and_deadlined() -> None:
    history_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-transcript-history-query.ts"
    ).read_text(encoding="utf-8")
    command_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "CommandPalette.tsx"
    ).read_text(encoding="utf-8")
    tray_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "TrayPanel.tsx"
    ).read_text(encoding="utf-8")

    assert "fetchWithTimeout(" in history_source
    assert "queryFn: async ({ pageParam, signal })" in history_source
    assert "signal?: AbortSignal" in history_source
    assert command_source.count("fetchWithTimeout(") >= 3
    assert "queryFn: async ({ signal })" in command_source
    assert "fetchWithTimeout(" in tray_source


def test_desktop_shell_commands_have_deadlines() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts"
    ).read_text(encoding="utf-8")

    for label in (
        "Global hotkey refresh",
        "Global hotkey capture update",
        "Global hotkey status",
        "Tray status",
        "Tray update status",
        "Tray recording state",
        "Tray action",
        "Hide tray panel",
    ):
        assert f'"{label}"' in source


def test_processing_timer_formats_long_jobs_with_hours() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx"
    ).read_text(encoding="utf-8")

    assert "Math.floor(seconds / 3600)" in source
    assert "Math.floor((seconds % 3600) / 60)" in source
    assert "if (hours > 0)" in source


def test_transcript_detail_uses_current_attempt_clock_and_truthful_clipboard_feedback() -> None:
    page_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx"
    ).read_text(encoding="utf-8")
    types_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts"
    ).read_text(encoding="utf-8")

    assert "processingStartedAt?: string;" in types_source
    assert "startedAt: transcript.processingStartedAt" in page_source
    assert "startedAt={transcript.createdAt}" not in page_source
    assert page_source.count("window.setInterval(updateElapsed, 1000)") == 1
    assert page_source.count("await navigator.clipboard.writeText") == 2
    assert 'title: "Copy failed"' in page_source
    assert page_source.count("onComplete();") == 1
    assert "enabled: needsAutoSummarySetting" in page_source
    assert "staleTime: 5 * 60_000" in page_source


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
    dark_favicon_svg = (
        REPO_ROOT / "Frontend" / "client" / "public" / "favicon-dark.svg"
    ).read_text(encoding="utf-8")
    brand_mark_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "BrandMark.tsx"
    ).read_text(encoding="utf-8")
    layout_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "layout" / "AppLayout.tsx"
    ).read_text(encoding="utf-8")

    assert 'type="image/svg+xml"' in index_html
    assert 'href="/favicon.svg"' in index_html
    assert 'href="/favicon.png"' not in index_html
    assert 'viewBox="5 99.4 118 76"' in favicon_svg
    assert "#253037" in favicon_svg
    assert "#CFAF6A" in favicon_svg
    assert 'viewBox="0 0 256 256"' in dark_favicon_svg
    assert 'fill="#FFFFFF"' in dark_favicon_svg
    assert "#253037" in dark_favicon_svg
    assert "#CFAF6A" in dark_favicon_svg
    assert brand_mark_source.count('src="/favicon.svg"') == 1
    assert brand_mark_source.count('src="/favicon-dark.svg"') == 1
    assert "dark:hidden" in brand_mark_source
    assert "dark:block" in brand_mark_source
    assert "dark:rounded-full" not in brand_mark_source
    assert "dark:bg-white" not in brand_mark_source
    assert "rounded-[10px]" not in brand_mark_source
    assert "bg-background/55" not in brand_mark_source
    assert layout_source.count("<BrandMark") == 3


def test_windows_taskbar_identity_uses_the_contrast_safe_tray_artwork() -> None:
    tauri_root = REPO_ROOT / "Frontend" / "src-tauri"
    config = json.loads((tauri_root / "tauri.conf.json").read_text(encoding="utf-8"))
    build_source = (tauri_root / "build.rs").read_text(encoding="utf-8")
    lib_source = (tauri_root / "src" / "lib.rs").read_text(encoding="utf-8")
    bundle_icon = tauri_root / "icons" / "icon.ico"
    master_svg = (tauri_root / "icons" / "windows-app-icon.svg").read_text(
        encoding="utf-8"
    )
    dark_app_svg = (
        REPO_ROOT / "Frontend" / "client" / "public" / "favicon-dark.svg"
    ).read_text(encoding="utf-8")

    assert config["bundle"]["icon"] == ["icons/icon.ico"]
    assert bundle_icon.read_bytes().startswith(b"\x00\x00\x01\x00")
    with Image.open(bundle_icon) as icon:
        assert icon.ico.sizes() == {
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        }
        taskbar_frame = icon.ico.getimage((32, 32)).convert("RGBA")
    with Image.open(tauri_root / "icons" / "tray-normal.png") as tray_icon:
        tray_frame = tray_icon.convert("RGBA")

    assert taskbar_frame.size == tray_frame.size == (32, 32)
    mean_channel_delta = sum(
        abs(taskbar_channel - tray_channel)
        for taskbar_pixel, tray_pixel in zip(taskbar_frame.getdata(), tray_frame.getdata())
        for taskbar_channel, tray_channel in zip(taskbar_pixel, tray_pixel)
    ) / (32 * 32 * 4)
    assert mean_channel_delta < 1.0
    assert sum(
        alpha >= 220 and red >= 235 and green >= 235 and blue >= 235
        for red, green, blue, alpha in taskbar_frame.getdata()
    ) >= 450
    feather_pixels = [
        (x, y)
        for y in range(taskbar_frame.height)
        for x in range(taskbar_frame.width)
        if taskbar_frame.getpixel((x, y))[3] >= 200
        and max(taskbar_frame.getpixel((x, y))[:3]) < 100
    ]
    feather_bounds = (
        min(x for x, _ in feather_pixels),
        min(y for _, y in feather_pixels),
        max(x for x, _ in feather_pixels),
        max(y for _, y in feather_pixels),
    )
    assert len(feather_pixels) >= 90
    assert feather_bounds[0] <= 5
    assert feather_bounds[1] <= 7
    assert feather_bounds[2] >= 26
    assert feather_bounds[2] - feather_bounds[0] + 1 >= 22
    assert taskbar_frame.getpixel((0, 0))[3] == 0
    assert dark_app_svg == master_svg
    assert "generate_windows_app_icon.py" in master_svg
    assert 'fill="#FFFFFF"' in master_svg
    with Image.open(tauri_root / "icons" / "window-icon.png") as window_icon:
        window_frame = window_icon.convert("RGBA")
    assert window_frame.size == (256, 256)
    assert (tauri_root / "icons" / "window-icon.rgba").read_bytes() == window_frame.tobytes()
    assert 'cargo:rerun-if-changed=icons/icon.ico' in build_source
    assert 'include_bytes!("../icons/window-icon.rgba"), 256, 256' in lib_source
    assert "CreateIconFromResourceEx" in lib_source
    assert "WM_SETICON" in lib_source
    assert "ICON_BIG as usize" in lib_source
    assert "ICON_SMALL as usize" in lib_source
    assert "native_windows_window_icons" in lib_source
    assert 'apply_desktop_window_icon_to_window(window, "initial reveal")' in lib_source
    assert 'apply_desktop_window_icon_to_window(&window, "main window restore")' in lib_source


def test_all_tray_states_preserve_white_disc_identity_and_semantic_badges() -> None:
    icon_dir = REPO_ROOT / "Frontend" / "src-tauri" / "icons"
    icons: dict[str, Image.Image] = {}

    for size in (16, 20, 24, 28, 32, 36, 40, 48):
        for state in ("normal", "update", "recording"):
            rgba = (icon_dir / f"tray-{state}-{size}.rgba").read_bytes()
            assert len(rgba) == size * size * 4
            icon = Image.frombytes("RGBA", (size, size), rgba)
            assert icon.getpixel((0, 0))[3] == 0

        normal = Image.frombytes(
            "RGBA", (size, size), (icon_dir / f"tray-normal-{size}.rgba").read_bytes()
        )
        assert sum(
            alpha >= 200 and red >= 225 and green >= 225 and blue >= 225
            for red, green, blue, alpha in normal.getdata()
        ) >= round(size * size * 0.40)

    for state in ("normal", "update", "recording"):
        png_path = icon_dir / f"tray-{state}.png"
        rgba_path = icon_dir / f"tray-{state}.rgba"
        assert png_path.read_bytes().startswith(b"\x89PNG")
        with Image.open(png_path) as source:
            icon = source.convert("RGBA")
        assert icon.size == (32, 32)
        assert rgba_path.read_bytes() == icon.tobytes()
        icons[state] = icon

    normal = icons["normal"]
    for state in ("update", "recording"):
        state_icon = icons[state]
        # State is a badge, not a replacement identity: the upper and left
        # portions remain byte-identical to the normal white-disc feather.
        for y in range(32):
            for x in range(32):
                if x < 16 or y < 16:
                    assert state_icon.getpixel((x, y)) == normal.getpixel((x, y))

        white_disc_pixels = sum(
            alpha >= 220 and red >= 235 and green >= 235 and blue >= 235
            for red, green, blue, alpha in state_icon.getdata()
        )
        assert white_disc_pixels >= 320

        # The identity remains visibly light after Windows scales a tray icon
        # to 16 px and composites it onto a representative dark taskbar.
        small = state_icon.resize((16, 16), Image.Resampling.LANCZOS)
        dark_taskbar = Image.new("RGBA", (16, 16), (32, 34, 37, 255))
        composited = Image.alpha_composite(dark_taskbar, small)
        light_pixels = sum(
            red >= 205 and green >= 205 and blue >= 205
            for red, green, blue, _alpha in composited.getdata()
        )
        assert light_pixels >= 65

    update_blue_pixels = sum(
        alpha >= 180 and blue >= 150 and blue > red * 1.4 and blue > green * 1.1
        for red, green, blue, alpha in icons["update"].getdata()
    )
    recording_red_pixels = sum(
        alpha >= 180 and red >= 160 and red > green * 1.5 and red > blue * 1.5
        for red, green, blue, alpha in icons["recording"].getdata()
    )
    assert update_blue_pixels >= 50
    assert recording_red_pixels >= 50


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
    assert "let microphonePayload = mics;" in source
    assert "if (!Array.isArray(microphonePayload.devices))" in source
    assert "fetchWithTimeout(" in source
    assert 'apiUrl("/api/microphones")' in source
    assert 'aria-label="Select input device"' in source
    assert 'aria-label={`Select microphone ${deviceLabel}`}' in source
    assert "Loading devices..." in source
    assert "const previousDeviceId = selectedDeviceId;" in source
    assert "setSelectedDeviceId(previousDeviceId);" in source
    assert "const saved = await handleMicDeviceChange(deviceId);" in source
    assert "if (!saved)" in source
    assert "favoriteMicRestored && typeof msg.restoredDeviceId === \"string\"" in source
    assert "import type { MicrophoneDevice }" in websocket_source
    assert "devices: MicrophoneDevice[]" in websocket_source
    assert "const closeCurrentSocket = useCallback" in websocket_source
    assert "if (wsRef.current !== ws)" in websocket_source
    assert "clearReconnectTimeout();\n        if (!enabled)" in websocket_source
    assert "import type { MicrophonesRefreshResponse }" in device_refresh_source
    assert "Promise<MicrophonesRefreshResponse | null>" in device_refresh_source
    assert "as { deviceId: string" not in source


def test_settings_provider_help_links_are_safe_external_links() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    expected_links = [
        'openai: { href: "https://platform.openai.com/api-keys"',
        'deepgram: { href: "https://console.deepgram.com/"',
        'assemblyai: { href: "https://www.assemblyai.com/dashboard"',
        'gemini: { href: "https://aistudio.google.com/app/apikey"',
        'openrouter: { href: "https://openrouter.ai/settings/keys"',
        'youtube: { href: "https://console.cloud.google.com/apis/credentials"',
        'soniox: { href: "https://console.soniox.com/"',
        'smallest: { href: "https://app.smallest.ai/"',
        'mistral: { href: "https://console.mistral.ai/api-keys"',
        'elevenlabs: { href: "https://elevenlabs.io/app/settings/api-keys"',
        'azure: { href: "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices"',
        'gladia: { href: "https://app.gladia.io/api-keys"',
        'groq: { href: "https://console.groq.com/keys"',
        'speechmatics: { href: "https://portal.speechmatics.com/"',
        'googleCloud: { href: "https://console.cloud.google.com/apis/credentials"',
    ]
    for link in expected_links:
        assert link in source

    assert "const API_KEY_HELP_LINKS = {" in source
    assert "type ApiKeyHelpKey = keyof typeof API_KEY_HELP_LINKS;" in source
    assert "const help = API_KEY_HELP_LINKS[helpKey];" in source
    assert "href={help.href}" in source
    assert 'target="_blank"' in source
    assert 'rel="noreferrer"' in source
    assert "title={help.label}" in source
    assert "void openExternalHelpUrl(help.href);" in source
    assert 'const { openUrl } = await import("@tauri-apps/plugin-opener");' in source
    assert "await openUrl(url);" in source
    assert 'value: "minimax/minimax-m3:nitro"' in source
    assert 'value: "z-ai/glm-5.2:nitro"' in source
    assert 'provider="OpenRouter"' in source
    assert 'if (provider === "OpenRouter") apiKeys.openrouter = openRouterKey;' in source


def test_websocket_reconnect_reports_frontend_ready() -> None:
    backend_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts"
    ).read_text(encoding="utf-8")
    websocket_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "contexts" / "WebSocketContext.tsx"
    ).read_text(encoding="utf-8")

    assert "options: { force?: boolean } = {}" in backend_source
    assert "!options.force && frontendReadyReportKey === reportKey" in backend_source
    assert "reportFrontendReady," in websocket_source
    assert "reportFrontendReady({ force: true })" in websocket_source
    assert "Frontend readiness beacon failed after WebSocket open." in websocket_source


def test_mobile_header_icon_buttons_keep_touch_targets() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "layout" / "AppLayout.tsx"
    ).read_text(encoding="utf-8")

    assert 'className="min-h-[44px] min-w-[44px]" aria-label="Open navigation"' in source
    assert 'className="min-h-[44px] min-w-[44px]"\n              onClick={handleOpenCommandPalette}' in source


def test_navigation_and_command_palette_use_bounded_internal_routes() -> None:
    layout_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "layout" / "AppLayout.tsx"
    ).read_text(encoding="utf-8")
    palette_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "CommandPalette.tsx"
    ).read_text(encoding="utf-8")
    command_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "ui" / "command.tsx"
    ).read_text(encoding="utf-8")

    for route in [
        '{ href: "/", icon: Mic, label: "Live Mic" }',
        '{ href: "/youtube", icon: Youtube, label: "YouTube" }',
        '{ href: "/file", icon: FolderOpen, label: "File" }',
        '{ href: "/debug", icon: Terminal, label: "Console" }',
        '{ href: "/settings", icon: Settings, label: "Settings" }',
    ]:
        assert route in layout_source
    assert "const isActive = location === tab.href || (tab.href !== \"/\" && location.startsWith(tab.href));" in layout_source
    assert "onPointerEnter={() => handleNavIntent(tab.href)}" in layout_source
    assert "onPointerDown={() => handleNavIntent(tab.href)}" in layout_source
    assert "onFocus={() => handleNavIntent(tab.href)}" in layout_source
    assert "onClick={onNavigate}" in layout_source
    assert "{renderNav(() => setMobileNavOpen(false))}" in layout_source

    assert 'fetchWithTimeout(apiUrl("/api/settings"), {' in palette_source
    assert 'fetchWithTimeout(apiUrl("/api/transcripts?limit=50"), {' in palette_source
    assert 'queryKey: ["/api/transcripts", { limit: 50 }],' in palette_source
    assert 'credentials: "include",' in palette_source
    assert "enabled: open" in palette_source
    assert "const transcripts = transcriptsData?.items || [];" in palette_source
    assert "transcripts.length > 0 &&" in palette_source
    assert "<CommandEmpty>Keine Ergebnisse gefunden.</CommandEmpty>" in palette_source
    assert "max-h-[300px] overflow-y-auto overflow-x-hidden" in command_source


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
    assert "youtube\\.com\\/live\\/" in source
    assert "encodeURIComponent(value)" in source
    assert "decoding=\"async\"" in source
    assert "referrerPolicy=\"no-referrer\"" in source
    assert "URL.createObjectURL(blob)" not in source
    assert "function isCompletedStep" in source
    assert "function isVisiblyProcessing" in source
    assert 'item.summaryStatus === "pending"' in source
    assert 'type YoutubeHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";' in source
    assert "function youtubeHistoryStatus(item: TranscriptHistoryItem): YoutubeHistoryStatus" in source
    assert 'if (item.summaryStatus === "failed") return "summary_failed";' in source
    assert 'historyStatus === "summary_failed"' in source
    assert "Summary failed" in source
    assert "text-red-600 border-red-200 bg-red-50" in source
    assert "const isProcessing = isVisiblyProcessing(item);" not in source
    assert "youtubePreferCaptions" in api_types
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    assert 'label="YouTube captions first"' in settings_source
    assert "youtubePreferCaptions" in settings_source
    assert settings_source.index('id="settings-summaries"') < settings_source.index('label="YouTube captions first"')
    assert settings_source.index('label="Auto-summarize"') < settings_source.index('label="YouTube captions first"')
    assert "preferCaptions," not in source


def test_live_mic_history_uses_snippets_period_sections_and_stable_virtual_rows() -> None:
    page_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx").read_text(
        encoding="utf-8"
    )
    virtual_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "virtual-transcript-history.tsx"
    ).read_text(encoding="utf-8")
    period_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "transcript-history-period.ts"
    ).read_text(encoding="utf-8")

    assert "const visibleSnippet =" in page_source
    assert "item.preview" in page_source
    assert "recordingTimeLabel(item.createdAt, item.date)" in page_source
    assert "getItemGroup={(item) => transcriptHistoryPeriod(item.createdAt)}" in page_source
    assert 'label: "Today"' in period_source
    assert 'label: "Last week"' in period_source
    assert 'label: "Last month"' in period_source
    assert 'label: "Older"' in period_source
    assert "translate3d(0, ${virtualRow.start}px, 0)" in virtual_source
    assert "layoutId=" not in virtual_source
    assert "AnimatePresence" not in virtual_source


def test_history_card_actions_reject_same_render_double_clicks() -> None:
    page_sources = {
        page: (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / page).read_text(encoding="utf-8")
        for page in ("LiveMic.tsx", "Youtube.tsx", "FileTranscribe.tsx")
    }

    for source in page_sources.values():
        assert "const copyingRef = useRef<string | null>(null);" in source
        assert "if (copyingRef.current) return;" in source
        assert "copyingRef.current = id;" in source
        assert "const copyResetTimerRef = useRef<number | null>(null);" in source
        assert "window.clearTimeout(copyResetTimerRef.current);" in source

    live_source = page_sources["LiveMic.tsx"]
    assert "const deletingRef = useRef<string | null>(null);" in live_source
    assert "if (deletingRef.current) return;" in live_source
    assert "const toggleRequestInFlightRef = useRef(false);" in live_source
    assert "if (toggleRequestInFlightRef.current) return;" in live_source

    youtube_source = page_sources["Youtube.tsx"]
    assert "const searchRequestInFlightRef = useRef(false);" in youtube_source
    assert "if (!q || searchRequestInFlightRef.current) return;" in youtube_source
    assert "searchRequestInFlightRef.current = true;" in youtube_source
    assert "const startRequestInFlightRef = useRef<string | null>(null);" in youtube_source
    assert "if (!item?.url || startRequestInFlightRef.current) return;" in youtube_source
    assert "startRequestInFlightRef.current = requestKey;" in youtube_source


def test_api_key_saved_feedback_replaces_stale_reset_timers() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    assert "const savedKeyResetTimersRef = useRef<Map<string, number>>(new Map());" in source
    assert "savedKeyResetTimersRef.current.get(provider)" in source
    assert "window.clearTimeout(previousResetTimer);" in source
    assert "savedKeyResetTimersRef.current.set(provider, resetTimer);" in source
    assert "savedKeyResetTimersRef.current.forEach" in source


def test_onnx_model_actions_reject_same_render_double_clicks() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    assert "const onnxModelActionInFlightRef = useRef<Set<string>>(new Set());" in source
    assert source.count("onnxModelActionInFlightRef.current.has(modelId)") == 2
    assert source.count("onnxModelActionInFlightRef.current.add(modelId);") == 2
    assert source.count("onnxModelActionInFlightRef.current.delete(modelId);") == 2


def test_onnx_progress_only_updates_the_selected_quantization() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")
    websocket_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "contexts" / "WebSocketContext.tsx"
    ).read_text(encoding="utf-8")

    assert "quantization?: string;" in websocket_source
    assert "msg.quantization !== onnxQuantization" in settings_source
    assert "[loadOnnxModels, onnxQuantization, queryClient, selectedDeviceId, toast]" in settings_source


def test_primary_page_intros_share_responsive_full_width_layout() -> None:
    component_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "page-intro.tsx"
    ).read_text(encoding="utf-8")
    page_sources = {
        page: (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / page).read_text(encoding="utf-8")
        for page in (
            "LiveMic.tsx",
            "Meetings.tsx",
            "Youtube.tsx",
            "FileTranscribe.tsx",
            "Settings.tsx",
        )
    }

    assert 'className="mt-3 max-w-[65ch] text-pretty text-[13px]' in component_source
    assert "sticky = true" in component_source
    assert 'sticky ? "sticky top-0 z-20" : "relative z-0"' in component_source
    assert "bottomContent" in component_source
    assert 'title="Settings"' in page_sources["Settings.tsx"]
    assert 'eyebrow="Workspace controls · 06"' in page_sources["Settings.tsx"]
    assert page_sources["Settings.tsx"].index("<PageIntro") < page_sources["Settings.tsx"].index(
        'aria-label="Settings sections"'
    )
    for source in page_sources.values():
        assert 'from "@/components/page-intro"' in source
        assert "<PageIntro" in source
    for page in ("LiveMic.tsx", "Meetings.tsx", "Youtube.tsx", "FileTranscribe.tsx"):
        assert "sticky={false}" in page_sources[page]


def test_primary_section_numbers_follow_navigation_order() -> None:
    page_markers = {
        "LiveMic.tsx": 'eyebrow="Voice capture · 01"',
        "Meetings.tsx": 'eyebrow="Meeting workspace · 02"',
        "Youtube.tsx": 'eyebrow="Media capture · 03"',
        "FileTranscribe.tsx": 'eyebrow="Media import · 04"',
        "DebugConsole.tsx": "System observability · 05",
        "Settings.tsx": 'eyebrow="Workspace controls · 06"',
    }

    for page, marker in page_markers.items():
        source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / page).read_text(
            encoding="utf-8"
        )
        assert marker in source


def test_settings_section_navigation_accounts_for_sticky_header() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert 'label: "Transcription"' in settings_source
    assert 'label: "Summarization"' in settings_source
    assert 'label: "Capture"' not in settings_source
    assert 'label: "Summary"' not in settings_source
    assert "Most changes save automatically" not in settings_source
    assert 'document.querySelector<HTMLElement>(".settings-page .transcription-intro")' in settings_source
    assert "target.style.scrollMarginTop = `${stickyOffset}px`" in settings_source
    assert "event.preventDefault()" in settings_source


def test_debug_console_intro_matches_primary_page_typography() -> None:
    stylesheet = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(
        encoding="utf-8"
    )

    assert ".debug-console-page {" in stylesheet
    assert 'font-family: "Switzer", ui-sans-serif, system-ui, sans-serif;' in stylesheet
    assert ".debug-console-title-row h1 {" in stylesheet
    assert "font-size: 2.8rem;" in stylesheet
    assert "font-weight: 600 !important;" in stylesheet
    assert "color: hsl(var(--muted-foreground));" in stylesheet
    assert ".debug-level-button {" in stylesheet


def test_primary_history_search_fields_share_sidebar_inset_design() -> None:
    component_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "transcript-history-search.tsx"
    ).read_text(encoding="utf-8")
    sidebar_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "ui" / "sidebar-search.tsx"
    ).read_text(encoding="utf-8")
    toolbar_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "transcription-history-toolbar.tsx"
    ).read_text(encoding="utf-8")
    page_sources = [
        (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / page).read_text(encoding="utf-8")
        for page in ("LiveMic.tsx", "Youtube.tsx", "FileTranscribe.tsx")
    ]

    assert "neu-search-inset" in sidebar_source
    assert "neu-search-inset" in component_source
    assert "neu-kbd" not in component_source
    assert 'type="search"' in component_source
    assert "transcript-history-search" in component_source
    assert 'from "@/components/transcript-history-search"' in toolbar_source
    assert "<TranscriptHistorySearch" in toolbar_source
    for source in page_sources:
        assert 'from "@/components/transcription-history-toolbar"' in source
        assert "<TranscriptionHistoryToolbar" in source
        assert "transcription-search relative" not in source
        assert "live-mic-search relative" not in source


def test_youtube_sorting_and_failed_retry_use_client_state_and_source_url() -> None:
    youtube_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(
        encoding="utf-8"
    )
    detail_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx"
    ).read_text(encoding="utf-8")

    assert 'useUrlQueryState<SortOption>("sort", "date"' in youtube_source
    assert "const sortedResults = useMemo(() =>" in youtube_source
    assert "return [...searchResults].sort((a, b) => {" in youtube_source
    assert 'case "views":' in youtube_source
    assert "return (b.viewCount || 0) - (a.viewCount || 0);" in youtube_source
    assert 'case "likes":' in youtube_source
    assert "return (b.likeCount || 0) - (a.likeCount || 0);" in youtube_source
    assert 'case "date":' in youtube_source
    assert "new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime()" in youtube_source
    assert "}, [searchResults, sortBy]);" in youtube_source
    assert 'parse: (raw) => (raw === "likes" || raw === "views" ? raw : "date"),' in youtube_source

    assert "const isFailedYoutubeTranscript =" in detail_source
    assert 'transcript?.status === "failed" && transcript?.type === "youtube"' in detail_source
    assert "const retryYoutubeTranscription = useCallback" in detail_source
    assert 'const sourceUrl = String(transcript?.sourceUrl || "").trim();' in detail_source
    assert "if (!sourceUrl) {" in detail_source
    assert 'title: "Retry unavailable",' in detail_source
    assert 'description: "No source URL is available for this transcript.",' in detail_source
    assert 'variant: "destructive",' in detail_source
    assert "return;" in detail_source
    assert "url: sourceUrl," in detail_source
    assert "title: transcript?.title," in detail_source
    assert 'setLocation(`/transcript/${rec.id}`);' in detail_source
    assert "void retryYoutubeTranscription();" in detail_source


def test_file_upload_progress_uses_route_persistent_store_before_server_processing() -> None:
    page_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "FileTranscribe.tsx").read_text(
        encoding="utf-8"
    )
    store_source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "file-upload-store.ts").read_text(
        encoding="utf-8"
    )

    assert "useSyncExternalStore" in page_source
    assert "subscribeFileUpload" in page_source
    assert "getFileUploadSnapshot" in page_source
    assert "startFileUploadBatch(selectedFiles" in page_source
    assert "isFileUploadActive()" in page_source
    assert "const currentPath = typeof window !==" in page_source
    assert 'currentPath === "/file"' in page_source
    assert "selectedFiles.length === 1" in page_source
    assert "uploadFiles(acceptedFiles)" in page_source
    assert "multiple: true" in page_source
    assert "const xhr = new XMLHttpRequest();" not in page_source
    assert "const [uploadProgress, setUploadProgress] = useState(0);" not in page_source

    assert 'export type FileUploadStatus = "idle" | "uploading" | "server_processing" | "completed" | "failed";' in store_source
    assert "export interface FileUploadQueueItem" in store_source
    assert "export function startFileUploadBatch(" in store_source
    assert "A file upload batch is already in progress." in store_source
    assert "const xhr = new XMLHttpRequest();" in store_source
    assert "xhr.withCredentials = true;" in store_source
    assert "xhr.upload.onprogress = (event) => {" in store_source
    assert "if (!event.lengthComputable || event.total <= 0) return;" in store_source
    assert "Math.round((event.loaded / event.total) * 95)" in store_source
    assert "progress: percent" in store_source
    assert "const switchToServerPhase = () => {" in store_source
    assert 'status: "server_processing"' in store_source
    assert "progress: 96" in store_source
    assert "serverProcessingLabel: getServerProcessingLabel(file)" in store_source
    assert "xhr.upload.onload = () => {" in store_source
    assert 'value={uploadProgress}' in page_source
    assert "uploadStatusText" in page_source
    assert "uploadQueueItems.map" in page_source
    assert 'type FileHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";' in page_source
    assert "function fileHistoryStatus(item: TranscriptHistoryItem): FileHistoryStatus" in page_source
    assert 'if (item.summaryStatus === "failed") return "summary_failed";' in page_source
    assert 'historyStatus === "summary_failed"' in page_source
    assert "Summary failed" in page_source
    assert "text-red-600 border-red-200 bg-red-50" in page_source


def test_history_updates_are_invalidated_globally_for_background_jobs() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "function TranscriptHistoryInvalidationBridge()" in source
    assert 'msg.type !== "history_updated"' in source
    assert 'queryClient.invalidateQueries({ queryKey: ["/api/transcripts", transcriptId], exact: true });' in source
    assert "pendingTranscriptTypesRef.current.add" in source
    assert "invalidateAllDetailsRef.current = true" in source
    assert 'typeof query.queryKey[1] === "string"' in source
    assert "window.setTimeout(flushInvalidations, 250)" in source
    assert "msg.transcriptType" in source
    assert "<TranscriptHistoryInvalidationBridge />" in source


def test_page_refresh_hook_does_not_duplicate_global_history_refetches() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-transcript-auto-refresh.ts"
    ).read_text(encoding="utf-8")

    assert 'msg.type === "history_updated"' not in source
    assert "refetchQueries" not in source
    assert "queryClient.invalidateQueries" in source


def test_virtual_history_releases_load_guard_for_void_and_failed_loaders() -> None:
    source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "virtual-transcript-history.tsx"
    ).read_text(encoding="utf-8")
    query_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-transcript-history-query.ts"
    ).read_text(encoding="utf-8")

    assert '"then" in result' in source
    assert ".catch((error) =>" in source
    assert "else {\n        loadInFlightRef.current = false;" in source
    assert "TRANSCRIPT_HISTORY_PAGE_SIZE = 100" in query_source
    assert "const { items, total } = useMemo(" in query_source
    assert "}), [query.data]);" in query_source
    assert "[gridColumns, items.length, rows.length, viewMode, virtualizer]" in source


def test_transcript_history_refreshes_after_websocket_reconnect() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "const hasConnectedRef = useRef(false);" in source
    assert "const wasConnectedRef = useRef(false);" in source
    assert "if (isConnected && hasConnectedRef.current && !wasConnectedRef.current)" in source
    assert "invalidateAllDetailsRef.current = true;" in source
    assert "invalidateAllHistoryRef.current = true;" in source


def test_live_mic_interim_and_final_transcript_render_distinctly() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx").read_text(
        encoding="utf-8"
    )

    assert 'const [finalText, setFinalText] = useState("");' in source
    assert 'const [interimText, setInterimText] = useState("");' in source
    assert 'case "transcript":' in source
    assert "setFinalText" in source
    assert "setInterimText" in source
    assert "text-foreground/90" in source
    assert "text-muted-foreground italic" in source
    assert "{finalText ? ' ' : ''}{interimText}" in source
    assert "(finalText || interimText)" in source


def test_live_mic_history_uses_title_when_legacy_preview_is_missing() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx").read_text(
        encoding="utf-8"
    )

    assert 'item.title.trim() || "No transcript preview available"' in source


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


def test_visualizer_bar_count_flows_to_live_mic_and_native_overlay() -> None:
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    live_mic_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx"
    ).read_text(encoding="utf-8")
    overlay_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "NativeRecordingOverlay.tsx"
    ).read_text(encoding="utf-8")
    helper_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "visualizer-settings.ts"
    ).read_text(encoding="utf-8")

    assert "await updateSettings({ visualizerBarCount: count });" in settings_source
    assert "export const DEFAULT_VISUALIZER_BAR_COUNT = 45;" in helper_source
    assert "export const MIN_VISUALIZER_BAR_COUNT = 16;" in helper_source
    assert "export const MAX_VISUALIZER_BAR_COUNT = 128;" in helper_source
    assert "Number.isFinite(numeric)" in helper_source
    assert "Math.round(numeric)" in helper_source
    assert "MIN_VISUALIZER_BAR_COUNT" in helper_source
    assert "MAX_VISUALIZER_BAR_COUNT" in helper_source
    assert "fetchWithTimeout(" in helper_source
    assert '{ credentials: "include", signal }' in helper_source

    assert "DEFAULT_VISUALIZER_BAR_COUNT," in settings_source
    assert "MAX_VISUALIZER_BAR_COUNT," in settings_source
    assert "MIN_VISUALIZER_BAR_COUNT," in settings_source
    assert "normalizeVisualizerBarCount," in settings_source
    assert "useState(DEFAULT_VISUALIZER_BAR_COUNT)" in settings_source
    assert "normalizeVisualizerBarCount(settings.visualizerBarCount)" in settings_source
    assert "normalizeVisualizerBarCount(value[0], savedVisualizerBarCount)" in settings_source
    assert "min={MIN_VISUALIZER_BAR_COUNT}" in settings_source
    assert "max={MAX_VISUALIZER_BAR_COUNT}" in settings_source
    assert "settings.visualizerBarCount || 45" not in settings_source

    assert "const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);" in live_mic_source
    assert "void refreshVisualizerBarCount();" in live_mic_source
    assert "barCount={visualizerBarCount}" in live_mic_source
    assert "const barCount = 20;" not in live_mic_source

    assert "const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);" in overlay_source
    assert "resizeBarBuffer" in overlay_source
    assert "barCount={visualizerBarCount}" in overlay_source
    assert "const BAR_COUNT =" not in overlay_source


def test_settings_and_youtube_mutations_use_authenticated_backend_access() -> None:
    backend_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts"
    ).read_text(encoding="utf-8")
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    visualizer_helper_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "visualizer-settings.ts"
    ).read_text(encoding="utf-8")
    youtube_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(
        encoding="utf-8"
    )
    detail_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx"
    ).read_text(encoding="utf-8")

    assert "function appendSessionToken(url: string): string" in backend_source
    assert 'parsed.searchParams.set("scriberToken", backendSessionToken);' in backend_source
    assert 'parsed.pathname === "/ws" || parsed.pathname.startsWith("/api/")' in backend_source

    assert "await updateSettings({ favoriteMic: newFavorite });" in settings_source
    assert "await updateSettings({ hotkey });" in settings_source
    assert 'await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });' in settings_source
    assert "await updateSettings({ visualizerBarCount: count });" in settings_source
    assert "fetchWithTimeout(" in visualizer_helper_source
    assert '{ credentials: "include", signal }' in visualizer_helper_source

    assert 'fetchWithTimeout(url, { credentials: "include" }, 30_000)' in youtube_source
    assert 'fetchWithTimeout(apiUrl("/api/youtube/transcribe"), {' in youtube_source
    assert 'credentials: "include",' in youtube_source
    assert 'fetchWithTimeout(apiUrl("/api/youtube/transcribe"), {' in detail_source
    assert 'credentials: "include",' in detail_source


def test_debug_and_settings_controls_have_responsive_density() -> None:
    debug_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "DebugConsole.tsx").read_text(
        encoding="utf-8"
    )
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert "--action-size: 44px;" in css
    assert "min-width: 44px;" in css
    assert "min-height: 44px;" in css

    assert "debug-console-actions" in debug_source
    assert "debug-console-action-button" in debug_source
    assert "debug-console-action-label" in debug_source
    assert "debug-console-stat-copy" in debug_source
    assert "debug-console-stat-icon" in debug_source
    assert 'aria-pressed={selectedLevel === level}' in debug_source
    assert "debug-level-selected-icon" in debug_source
    assert "Download support bundle" in debug_source
    assert "Support bundle downloaded as ${filename}. Check your Downloads folder." in debug_source
    assert "was saved by the browser download manager" in debug_source
    assert "/api/runtime/post-processing-diagnostics?limit=8" in debug_source
    assert "Post-processing diagnostics" in debug_source
    assert "Raw fallback" in debug_source
    assert 'className="compact-impact-switch"' in debug_source

    assert "settings-page" in settings_source
    assert "function SettingLine" in settings_source
    assert "sm:grid-cols-[minmax(0,1fr)_minmax(150px,220px)]" in settings_source
    assert "settings-page .impact-echo-switch" in css
    assert "--impact-switch-track-width: 64px" in css
    assert ".debug-console-actions" in css
    assert ".debug-console-action-label" in css
    assert ".debug-console-page .debug-level-button[aria-pressed=\"true\"]" in css
    assert "grid-template-columns: auto minmax(0, 1fr) auto" in css
    assert "padding: 1.5rem 1.5rem 1rem" in css
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
    transcript_detail_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx"
    ).read_text(encoding="utf-8")
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert tauri_config["app"]["windows"][0]["decorations"] is False
    assert tauri_config["app"]["windows"][0]["visible"] is False
    assert tauri_config["app"]["windows"][0]["backgroundColor"] == "#202225"
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
    assert "if (event.button !== 0 || event.detail > 1) return;" in titlebar_source
    assert "onMouseDown={(event) => event.stopPropagation()}" in titlebar_source
    assert "minimize" in titlebar_source
    assert "toggleMaximize" in titlebar_source
    assert "close" in titlebar_source
    assert "Scriber" not in titlebar_source
    assert "<DesktopTitleBar />" in transcript_detail_source
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
    assert 'const VALID_THEMES = new Set<Theme>(["dark", "light", "system"]);' in provider_source
    assert "function normalizeTheme" in provider_source
    assert "function readStoredTheme" in provider_source
    assert "function writeStoredTheme" in provider_source
    assert "normalizeTheme(window.localStorage.getItem(storageKey), fallback)" in provider_source
    assert "Theme preference could not be persisted." in provider_source
    assert "const normalizedTheme = normalizeTheme(theme);" in provider_source
    assert "readStoredTheme(storageKey, normalizeTheme(defaultTheme))" in provider_source
    assert "const normalizedNextTheme = normalizeTheme(nextTheme);" in provider_source
    assert "writeStoredTheme(storageKey, normalizedNextTheme);" in provider_source
    assert "setTheme(normalizedNextTheme);" in provider_source
    assert "localStorage.getItem(storageKey) as Theme" not in provider_source
    assert "localStorage.setItem(storageKey, nextTheme)" not in provider_source
    assert "deferredDesktopThemeRef" in provider_source
    assert "const revealGeneration = revealGenerationRef.current + 1;" in provider_source
    assert "if (revealGenerationRef.current !== revealGeneration) return;" in provider_source
    assert "deferredDesktopThemeRef.current = null;\n            setThemeRevealActive(false);" in provider_source
    assert "setThemeRevealActive(true)" in provider_source
    assert "setThemeRevealActive(false)" in provider_source
    assert "void transition.finished.then(finishReveal, finishReveal)" in provider_source
    assert "window.setTimeout(finishReveal, THEME_TRANSITION_DURATION_MS + 140)" in provider_source
    assert "void fallbackCircularThemeReveal(transitionOrigin, nextResolvedTheme, commitTheme)" in provider_source
    assert 'html[data-theme-reveal-active="true"] *' in css
    assert "transition-property: opacity, transform, filter !important;" in css
    assert 'html[data-theme-reveal-active="true"] .theme-reveal-overlay' in css


def test_desktop_update_status_filters_same_version_updates() -> None:
    update_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "desktop-updates.ts"
    ).read_text(encoding="utf-8")
    app_source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    vite_config = (REPO_ROOT / "Frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert "__SCRIBER_APP_VERSION__" in vite_config
    assert 'JSON.stringify(packageJson.version || "")' in vite_config
    assert "function isVersionNewerThanCurrent" in update_source
    assert "function parseVersion" in update_source
    assert "function latestKnownCurrentVersion" in update_source
    assert "const currentVersion = latestKnownCurrentVersion(cache.currentVersion);" in update_source
    assert "status.version &&\n    isVersionNewerThanCurrent(status.version, status.currentVersion)" in update_source
    assert "status.version &&\n    isVersionNewerThanCurrent(status.version, status.currentVersion)" in update_source[
        update_source.index("export function publishDesktopUpdateStatusToTray") :
    ]
    assert "if (!isVersionNewerThanCurrent(update.version, currentVersion))" in update_source
    assert "let updateCheckInFlight: Promise<DesktopUpdateStatus> | null = null;" in update_source
    assert "let updateInstallInFlight: Promise<DesktopUpdateStatus> | null = null;" in update_source
    assert "function sharedDesktopUpdateCheck" in update_source
    assert '"Desktop update check"' in update_source
    assert '"App version lookup"' in update_source
    assert "const staleAvailable = Boolean(rawAvailable && !available);" in update_source
    assert 'phase: staleAvailable ? "current" : cache.phase || "idle"' in update_source
    assert "maybeNotify(cached);" in app_source


def test_settings_exposes_dedicated_post_processing_model_choice() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "const POST_PROCESSING_MODEL_OPTIONS" in settings_source
    assert 'const DEFAULT_POST_PROCESSING_MODEL = "cerebras/gemma-4-31b";' in settings_source
    assert 'value: "openai/gpt-oss-120b"' in settings_source
    assert "GPT-OSS 120B Baseten" in settings_source
    assert 'value: "openai/gpt-oss-120b:cerebras"' in settings_source
    assert "GPT-OSS 120B Cerebras" in settings_source
    assert 'value: "cerebras/gemma-4-31b"' in settings_source
    assert "Gemma 4 31B Cerebras" in settings_source
    assert "return `${priceText}€/M blended, ~${tokensPerSecond} Token/s`;" in settings_source
    assert "languageModelBenchmarkDetail(0.00000035, 0.00000075, 768)" in settings_source
    assert 'baseten: "/provider-icons/baseten.svg"' in settings_source
    assert 'cerebras: "/provider-icons/cerebras.svg"' in settings_source
    assert 'value: "google/gemini-2.5-flash-lite:nitro"' in settings_source
    assert 'value: "gpt-5.4-nano"' in settings_source
    assert 'value: "gpt-5.4-mini"' in settings_source
    assert 'value: "gpt-5.5"' in settings_source
    assert 'value: "gemini-3.1-pro-preview"' in settings_source
    assert "Gemini 3.0 Flash Preview" not in settings_source
    assert "Gemini 3 Pro" not in settings_source
    assert "OpenAI GPT 5.2" not in settings_source
    assert "OpenAI GPT 5 Mini" not in settings_source
    assert "OpenAI GPT 5 Nano" not in settings_source
    assert "const [postProcessingModel, setPostProcessingModel]" in settings_source
    assert "const [cerebrasKey, setCerebrasKey]" in settings_source
    assert 'provider="Cerebras"' in settings_source
    assert "setPostProcessingModel(settings.postProcessingModel || DEFAULT_POST_PROCESSING_MODEL);" in settings_source
    assert "const handlePostProcessingModelChange = async (value: string)" in settings_source
    assert "await updateSettings({ postProcessingModel: value });" in settings_source
    assert 'label="Post-processing model"' in settings_source
    assert 'value={postProcessingModel}' in settings_source
    assert "onValueChange={(value) => void handlePostProcessingModelChange(value)}" in settings_source
    assert "POST_PROCESSING_MODEL_OPTIONS.map((option)" in settings_source
    assert "selectedPostProcessingModelOption" in settings_source
    assert "<ProviderIcon icon={option.icon} label={option.label}" in settings_source
    assert "const requirement = requiredCredentialForLanguageModel(value);" in settings_source
    assert "if (!isCredentialReady(requirement))" in settings_source
    assert "openCredentialDialog(requirement);" in settings_source
    assert "{option.detail}" in settings_source
    assert "Use a low-cost, low-latency model for simple dictation cleanup." in settings_source
    assert "Beantworte keine Fragen im Transkript." in settings_source
    assert "Gliedere den Text in sinnvolle Absätze." in settings_source
    assert "Entferne Füllwörter" in settings_source
    assert "Sehr geehrter Herr Müller" in settings_source
    assert "Sehr geehrte Damen und Herren" in settings_source
    assert 'Nutze Aufzählungszeichen mit "- "' in settings_source
    assert "mehrere Punkte, Aufgaben, Beispiele, Voraussetzungen oder Argumente" in settings_source
    assert "zweitausend fünfhundert Euro -> 2.500 €" in settings_source
    assert "Euro pro Quadratmeter -> €/m²" in settings_source
    assert "Kilowattstunden pro Quadratmeter und Jahr -> kWh/m²a" in settings_source
    assert "Du bist Scribers präziser Live-Diktat-Editor." not in settings_source
    assert "Aufgabe: Glätte das folgende Speech-to-Text-Transkript" not in settings_source


def test_settings_model_choices_require_saved_api_keys() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "type CredentialRequirement" in settings_source
    assert "const isCredentialReady = (requirement: CredentialRequirement | null) => {" in settings_source
    assert "savedCredentialAvailable(requirement.provider" not in settings_source
    assert "const requiredCredentialForTranscriptionModel = (model: string)" in settings_source
    assert "const requiredCredentialForLanguageModel = (model: string)" in settings_source
    assert "const missingCredentialReason = (requirement: CredentialRequirement | null)" in settings_source
    assert "onCredentialAction={() => openCredentialDialog(requirement)}" in settings_source
    assert "openCredentialDialog(requirement);" in settings_source
    assert "disabled={Boolean(disabledReason)}" in settings_source
    assert "aria-disabled={disabled || undefined}" in settings_source
    assert "{option.detail}" in settings_source
    assert "{disabledReason ? (" in settings_source
    assert 'const MISSING_CREDENTIAL_CTA = "Add API Key";' in settings_source
    assert "openCredentialDialog(missingPostProcessingCredentialRequirement)" in settings_source
    assert "{MISSING_CREDENTIAL_CTA}" in settings_source
    assert "Credential required before model selection." in settings_source
    assert "below, or choose a model that already has credentials." in settings_source
    assert "setSavedKeys((prev) => ({ ...prev, [provider]: credentialReady }));" in settings_source
    assert "setCredentialReadyKeys((prev) => ({ ...prev, [provider]: credentialReady }));" in settings_source
    assert 'OpenRouter: hasValue(keys.openrouter)' in settings_source


def test_settings_custom_vocabulary_autosaves_without_manual_button() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "Save vocabulary" not in settings_source
    assert "const savedCustomVocabularyRef = useRef(\"\");" in settings_source
    assert "const saveCustomVocabulary = useCallback((nextValue: string): Promise<void>" in settings_source
    assert "pendingCustomVocabularyRef" in settings_source
    assert "customVocabularySaveInFlightRef" in settings_source
    assert "while (pendingCustomVocabularyRef.current !== null)" in settings_source
    assert "window.setTimeout(() => {" in settings_source
    assert "void saveCustomVocabulary(customVocabulary);" in settings_source
    assert "await saveCustomVocabulary(customVocabulary);" in settings_source


def test_settings_stt_benchmarks_remain_visible_when_api_keys_are_missing() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "return `${euroText}€/h with ${errorText}% Error`;" in settings_source
    assert "0,00€/h with model-dependent Error" in settings_source
    assert "0,00 €/h" not in settings_source
    assert " % Error" not in settings_source
    assert "{disabledReason || option.detail}" not in settings_source
    assert "title={`${option.label}: ${option.detail}${disabledReason ? ` - ${disabledReason}` : \"\"}`}" in settings_source
    assert 'const MISSING_CREDENTIAL_CTA = "Add API Key";' in settings_source
    provider_options_source = settings_source[
        settings_source.index("const PROVIDER_MODEL_OPTIONS: ProviderModelOption[]")
        : settings_source.index("function ProviderIcon")
    ]
    assert "cloud_segmented" not in provider_options_source
    assert "Cloud live / segmented" not in settings_source

    streaming_order = [
        '"elevenlabs"',
        '"assemblyai-realtime"',
        '"soniox-realtime"',
        '"google"',
        '"openai"',
        '"mistral-realtime"',
        '"smallest-realtime"',
        '"deepgram"',
        '"gladia"',
        '"speechmatics"',
    ]
    async_order = [
        '"azure_mai"',
        '"assemblyai"',
        '"mistral-async"',
        '"groq"',
        '"soniox-async"',
        '"speechmatics-async"',
        '"gladia-async"',
        '"smallest-async"',
        '"openai-async"',
        '"gemini-stt"',
        '"deepgram-async"',
    ]

    for ordered_values in (streaming_order, async_order):
        positions = [provider_options_source.index(f"value: {value}") for value in ordered_values]
        assert positions == sorted(positions)


def test_settings_embeds_local_model_management_in_local_provider_group() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "Local model files" not in settings_source
    assert "const activeLocalModelSettings =" in settings_source
    assert 'group.key === "local" && activeLocalModelSettings' in settings_source
    assert "{localModelManagement}" not in settings_source


def test_settings_summary_model_groups_do_not_render_secondary_descriptions() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "Fast Google summaries." not in settings_source
    assert "Nitro routes for long output." not in settings_source
    assert "OpenAI summary models." not in settings_source


def test_settings_paired_panels_balance_height_and_update_metadata_density() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    for section_id in ("settings-api-keys", "settings-summaries", "settings-updates", "settings-language"):
        section = settings_source[settings_source.index(f'id="{section_id}"') :]
        assert 'className="flex h-full self-stretch flex-col"' in section[:500]
    assert "grid flex-1 content-between" in settings_source
    assert "sm:grid-cols-4" in settings_source
    assert 'dateStyle: "short"' in settings_source
    assert 'timeStyle: "short"' in settings_source
    assert "toLocaleString()" not in settings_source


def test_tray_panel_exposes_direct_update_install_action() -> None:
    tray_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "TrayPanel.tsx"
    ).read_text(encoding="utf-8")

    assert "function StatusIndicator" in tray_source
    assert "showUpdateInstallBanner" in tray_source
    assert "Install Scriber" in tray_source
    assert "Download, install, and restart Scriber." in tray_source
    assert 'label={status.updateAvailable ? "Check Again" : "Check for Updates"}' in tray_source
    assert "status.updateInstalling || !status.updateAvailable" in tray_source
    assert '<Download className="h-2.5 w-2.5"' in tray_source
    assert "statusDotClass" not in tray_source

    tray_icon_dir = REPO_ROOT / "Frontend" / "src-tauri" / "icons"
    assert (tray_icon_dir / "tray-update.png").read_bytes().startswith(b"\x89PNG")
    assert len((tray_icon_dir / "tray-update.rgba").read_bytes()) == 32 * 32 * 4


def test_tray_panel_exposes_meetings_shortcut_and_installed_version() -> None:
    tray_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "TrayPanel.tsx"
    ).read_text(encoding="utf-8")
    shell_source = (
        REPO_ROOT / "Frontend" / "src-tauri" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")

    assert 'import("@tauri-apps/api/app")' in tray_source
    assert ".then(({ getVersion }) => getVersion())" in tray_source
    assert 'label="Meetings"' in tray_source
    assert 'detail="Open meeting workspace"' in tray_source
    assert "shortcut={meetingShortcut}" in tray_source
    assert 'runAction("open_meetings")' in tray_source
    assert "value?.meetingHotkey" in tray_source
    assert "loadRegisteredShortcuts(false)" in tray_source
    assert "const requestId = ++shortcutLoadRequestRef.current;" in tray_source
    assert "requestId === shortcutLoadRequestRef.current" in tray_source
    assert 'className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-2.5 pr-1"' in tray_source
    assert '"open_meetings" => {' in shell_source
    assert 'show_main_window_path(app, "/meetings")?;' in shell_source
    assert "const TRAY_PANEL_HEIGHT: f64 = 668.0;" in shell_source


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


def test_native_recording_overlay_uses_fixed_size_state_layers() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "components" / "NativeRecordingOverlay.tsx").read_text(
        encoding="utf-8"
    )

    assert "const WAVEFORM_CANVAS_WIDTH = 162;" in source
    assert "const STOP_BUTTON_SIZE = 31;" in source
    assert "const PILL_WIDTH = OVERLAY_CONTENT_WIDTH + PILL_PADDING * 2;" in source
    assert "const PILL_RADIUS = PILL_HEIGHT / 2;" in source
    assert "const OVERLAY_DROP_SHADOW" in source
    assert "const OVERLAY_PILL_SHADOW" in source
    assert "width: PILL_WIDTH" in source
    assert "height: PILL_HEIGHT" in source
    assert 'filter: "blur' not in source
    assert 'data-testid="native-recording-shadow"' not in source
    assert "absolute inset-0 flex items-center" in source
    assert "overlayMode" in source
    assert 'listen<OverlayEventPayload>("scriber-overlay-state"' in source
    assert 'invoke<OverlayEventPayload>("native_overlay_renderer_ready")' in source
    assert source.index('listen<OverlayEventPayload>("scriber-overlay-state"') < source.index(
        'invoke<OverlayEventPayload>("native_overlay_renderer_ready")'
    )
    assert "modeFromNativeOverlayState(snapshot)" in source
    assert "let receivedNativeEvent = false;" in source
    assert "receivedNativeEvent = true;" in source
    assert "if (!disposed && !receivedNativeEvent)" in source
    assert "let reconnectTimer: number | null = null;" in source
    assert "reconnectTimer = window.setTimeout(connect, 750);" in source


def test_meeting_states_suppress_update_prompts_and_drive_tray_state() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")
    websocket_types = (
        REPO_ROOT / "Frontend" / "client" / "src" / "contexts" / "WebSocketContext.tsx"
    ).read_text(encoding="utf-8")
    api_types = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts"
    ).read_text(encoding="utf-8")

    assert 'if (msg.type === "meeting_state")' in source
    assert '["starting", "recording", "paused", "stopping", "finalizing", "analyzing"]' in source
    assert 'mode: `meeting-${meetingState}`' in source
    assert '(msg.type === "state" && msg.voiceEnrollmentActive)' in source
    assert "voiceEnrollmentActive: boolean;" in websocket_types
    assert "export interface BackendStateResponse" in api_types
    assert "voiceEnrollmentActive: boolean;" in api_types


def test_meeting_workspace_uses_focus_canvas_and_gpu_only_live_progress() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert "Ready to start" in source
    assert "Check before a long meeting" in source
    assert "Ready for a long meeting" in source
    assert "What matters now" in source
    assert "Key outcome" in source
    assert "Render-active attenuation" in source
    assert "Reduces speaker echo" in source
    assert "I confirm I am permitted to record" not in source
    assert "Recording conversations without permission" not in source
    assert "consentConfirmed: true" not in source
    assert "h-auto min-h-9 w-full whitespace-normal" in source
    assert 'className="sr-only">Meeting workspace' in source
    assert "<PageIntro" in source
    assert "transition-[width]" not in source
    assert "transition-transform" in source
    assert "motion-reduce:transition-none" in source
    assert 'segment.revision === "canonical"' in source
    assert 'if (!hasCanonicalTranscript)' in source
    assert "TERMINAL_MEETING_STATES.has(message.meeting.state)" in source
    assert "Delete this meeting?" in source
    assert "deleteMeetingMutation" in source
    assert "neu-nav-active" in source
    assert "Soniox Realtime" not in source  # Provider/model labels come from the backend contract.
    assert "selectedProfile.stages" in source
    assert "const selectedProfileCostPerHour = selectedProfile?.costEstimate?.totalPerMeetingHour;" in source
    assert "const meetingImportFinalCostPerAudioHour = meetingImportProfile?.costEstimate?.singleTrackFinalPerAudioHour;" in source
    assert "Models used" in source
    assert "playLoadedAudio" in source
    assert "Speaker sound played" in source


def test_meeting_workspace_reconciles_after_backend_websocket_restart() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert "const meetingWsHasConnectedRef = useRef(false);" in source
    assert "const meetingWsWasConnectedRef = useRef(false);" in source
    assert "const { isConnected: meetingWsConnected } = useWebSocketContext();" in source
    assert "isMeetingWebSocketReconnect(" in source
    assert "meetingWsConnected," in source
    assert (
        "queryClient.invalidateQueries({ queryKey: MEETING_HISTORY_QUERY_KEY, exact: true })"
        in source
    )
    assert "void refreshMeetingCapabilities(queryClient);" in source
    assert "if (selectedId) void refreshMeetingDetail(queryClient, selectedId);" in source
    assert "invalidateMeetingImports();" in source
    assert "invalidateMeetings(" not in source


def test_meeting_audio_device_picker_refreshes_and_explains_inventory_fallbacks() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert 'if (message.type === "microphones_updated")' in source
    assert (
        'queryClient.invalidateQueries({ queryKey: ["/api/meetings/audio-devices"], exact: true })'
        in source
    )
    assert "const audioDeviceInitialLoading = audioDevicesQuery.isPending;" in source
    assert "disabled={microphoneSelectDisabled}" in source
    assert "disabled={renderSelectDisabled}" in source
    assert 'role="status" aria-live="polite"' in source
    assert "Looking for microphones and speakers…" in source
    assert "The device list could not be loaded." in source
    assert "Individual device selection is unavailable." in source
    assert "Windows default microphone (automatic)" in source
    assert "Windows default speakers (automatic)" in source
    assert "const microphoneCountLabel" in source
    assert "const speakerCountLabel" in source
    assert "endpoint.endpointIdHash === current" in source


def test_meeting_workspace_scopes_drafts_playback_and_imports_to_durable_state() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    assert 'setChatQuestion("");' in meetings
    assert "variables.id !== selectedId" in meetings
    assert 'setTranscriptSearch("");' in meetings
    assert "noteDraftRef.current" in meetings
    assert "draft.body !== draft.savedBody" in meetings
    assert 'body: draft.body' in meetings
    assert "availablePlaybackSources" in meetings
    assert "Audio is no longer retained" in meetings
    assert "meetingImportsQuery.data?.items.find" in meetings
    assert 'job.meetingId' in meetings
    assert 'queryKey: ["/api/meetings/speaker-profiles"]' in settings
    assert "const speakerProfilesQuery" not in meetings
    assert 'onClick={() => setVoiceLibraryDeleteOpen(true)}' in settings
    assert "Delete all saved voice data?" in settings
    assert "Delete this saved speaker?" in settings
    assert "voiceLibraryDeletePending" in settings


def test_meeting_defaults_and_voice_library_live_only_in_meeting_settings() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )

    meeting_settings = settings[
        settings.index('id="settings-meetings"') : settings.index('id="settings-api-keys"')
    ]
    transcription_settings = settings[
        settings.index('id="settings-transcription"') : settings.index("{speechToTextProviderPanel}")
    ]

    assert "Advanced · Retention" not in meetings
    assert "Local audio retention" not in meetings
    assert "Local Voice Library" not in meetings
    assert 'voiceLibraryEnabled: Boolean(speakerModelQuery.data?.optedIn && speakerModelQuery.data?.installed)' in meetings
    assert "audioRetentionDays: profile?.audioRetentionDays ?? 0" in meetings

    assert 'label="Meeting shortcut"' in meeting_settings
    assert 'label="Keep meeting audio"' in meeting_settings
    assert 'title="Voice Library"' in meeting_settings
    assert "People sharing the selected microphone currently appear together" in meeting_settings
    assert 'label="Recognize familiar speakers"' in meeting_settings
    assert 'aria-label="Recognize familiar speakers in future meetings"' in meeting_settings
    assert "Add voice" in meeting_settings
    assert '"/api/meetings/speaker-profiles/enroll"' in settings
    assert "Teach Scriber a voice" in settings
    assert "The recording is not saved or uploaded." in settings
    assert 'setSonioxRealtimeModel(settings.sonioxRealtimeModel || "stt-rt-v5")' in settings
    assert "sonioxRealtimeModel" in meeting_settings
    assert 'label="Meeting shortcut"' not in transcription_settings
    assert 'label="Recognize familiar speakers"' not in transcription_settings

    assert 'title="Transcription"' in meeting_settings
    assert 'title="Summaries and storage"' in meeting_settings
    assert "Protected every 30 seconds." in meeting_settings
    assert 'label="Reduce speaker echo"' in meeting_settings
    assert "checkpointed audio" not in meeting_settings
    assert "AEC3 echo control" not in meeting_settings
    assert "Voice embeddings" not in meeting_settings


def test_meeting_transcription_modes_are_configured_only_in_settings() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    meeting_settings = settings[
        settings.index('id="settings-meetings"') : settings.index('id="settings-api-keys"')
    ]

    assert 'id="meeting-profile"' not in meetings
    assert 'id="meeting-import-profile"' not in meetings
    assert "Change in Settings" in meetings
    assert "Transcript after meeting" in meeting_settings
    assert "Live text + accurate transcript" in meeting_settings
    assert 'role="radiogroup" aria-label="Meeting transcription timing"' in meeting_settings
    assert "meetingTranscriptionMode" in meeting_settings
    assert "Estimated total" in meeting_settings
    assert "Why Scriber does not upload one-minute pieces" in meeting_settings
    assert "It does not change the final transcript or its price." in meeting_settings


def test_live_meeting_transcript_follows_latest_text_without_trapping_review() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert "const [followLatest, setFollowLatest] = useState(true);" in meetings
    assert 'virtualizer.scrollToIndex(segments.length - 1, { align: "end" });' in meetings
    assert "scrollToLatest, search, segments]" in meetings
    assert "element.scrollHeight - element.clientHeight - element.scrollTop <= 40" in meetings
    assert ">Latest text" in meetings
    assert "isLive={detail.state === \"recording\" || detail.state === \"paused\"}" in meetings
    assert "key={detail.id}" in meetings
    assert "Recording safely. The transcript appears after you stop." in meetings


def test_settings_cards_follow_the_requested_two_column_order() -> None:
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    rendered = settings[settings.index("<PageIntro") :]
    navigation = rendered[rendered.index('{ section: "transcription"') : rendered.index("].map((item)")]

    assert rendered.index('id="settings-transcription"') < rendered.index("{speechToTextProviderPanel}")
    assert rendered.index("{speechToTextProviderPanel}") < rendered.index('id="settings-meetings"')
    assert rendered.index('id="settings-meetings"') < rendered.index('id="settings-api-keys"')
    assert rendered.index('id="settings-api-keys"') < rendered.index('id="settings-summaries"')
    assert rendered.index('id="settings-updates"') < rendered.index('id="settings-language"')

    assert navigation.index('section: "transcription"') < navigation.index('section: "providers"')
    assert navigation.index('section: "providers"') < navigation.index('section: "meetings"')
    assert navigation.index('section: "apiKeys"') < navigation.index('section: "summarization"')
    assert navigation.index('section: "updates"') < navigation.index('section: "language"')


def test_outlook_meeting_settings_explain_each_connection_state_plainly() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(
        encoding="utf-8"
    )
    meeting_settings = settings[
        settings.index('id="settings-meetings"') : settings.index('id="settings-api-keys"')
    ]

    assert "Outlook is connected" in meeting_settings
    assert "Outlook is ready to connect" in meeting_settings
    assert "Finish signing in with Microsoft" in meeting_settings
    assert "Outlook is not available in this release" in meeting_settings
    assert "Reinstalling the same version will not fix it." in meeting_settings
    assert "Choose Connect Outlook below." in meeting_settings
    assert "Sign in with Microsoft and allow read-only calendar access." in meeting_settings
    assert "Return to Scriber; upcoming meetings sync automatically." in meeting_settings
    assert "Help for self-built copies" in meeting_settings
    assert "SCRIBER_OUTLOOK_CLIENT_ID" in meeting_settings
    assert "Disconnect Outlook" in meeting_settings
    assert "Reconnect Outlook" in meeting_settings
    assert "Sync now" in meeting_settings
    assert "const outlookMutation" not in meetings
    assert "<OutlookMeetingPicker" in meetings
    assert 'setLocation("/settings")' in meetings


def test_outlook_meeting_picker_uses_fresh_daily_events_and_explicit_selection() -> None:
    client = REPO_ROOT / "Frontend" / "client" / "src"
    meetings = (client / "pages" / "Meetings.tsx").read_text(encoding="utf-8")
    picker = (client / "components" / "meeting" / "OutlookMeetingPicker.tsx").read_text(
        encoding="utf-8"
    )
    api_types = (client / "lib" / "api-types.ts").read_text(encoding="utf-8")

    assert '"/api/calendar/outlook/events"' in meetings
    assert "date: outlookCalendarDate" in meetings
    assert "timeZone: outlookTimeZone" in meetings
    assert "start: outlookCalendarWindow.start" in meetings
    assert "end: outlookCalendarWindow.end" in meetings
    assert "outlookQuery.data?.lastSyncAt ?? \"\"" in meetings
    assert "!outlookQuery.data.lastSyncAt && outlookQuery.data.lastError" in meetings
    assert "calendarEventId: selectedCalendarEventId || null" in meetings
    assert "setSelectedCalendarEventId(event?.id ?? \"\")" in meetings
    assert "if (calendarEvent?.id) {" in meetings
    assert "setSelectedCalendarEventId(calendarEvent.id);" in meetings
    assert "selectedCalendarSubjectRef.current = calendarEvent.subject;" in meetings
    assert "Refresh calendar" in picker
    assert "Use no calendar event" in picker
    assert "Open online meeting" in picker
    assert 'url.protocol !== "https:"' in picker
    assert "No Outlook meetings today." in picker
    assert "Today&apos;s meetings could not be loaded." in picker
    assert "event.isAllDay ? \"All day\"" in picker
    assert "event.location" in picker
    assert "participant.type === \"resource\"" in picker
    assert "events?.truncated" in picker
    assert "credentialStatusAvailable: boolean" in api_types
    assert "export interface OutlookCalendarEventsResponse" in api_types
    assert "truncated: boolean" in api_types


def test_outlook_disconnect_and_speaker_assignments_require_explicit_confirmation() -> None:
    client = REPO_ROOT / "Frontend" / "client" / "src"
    settings = (client / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    meetings = (client / "pages" / "Meetings.tsx").read_text(encoding="utf-8")
    assignments = (
        client / "components" / "meeting" / "SpeakerAttendeeAssignments.tsx"
    ).read_text(encoding="utf-8")

    assert "outlookDisconnectOpen" in settings
    assert "Disconnect Outlook?" in settings
    assert "Keep connected" in settings
    assert 'outlookMutation.mutate("disconnect")' in settings
    assert 'queryKey: ["/api/calendar/outlook/events"]' in settings
    assert 'queryClient.removeQueries({ queryKey: ["/api/calendar/outlook/events"] });' in settings
    assert "credentialStatusAvailable === false" in settings
    assert "Previously synchronized calendar entries stay on this device" in settings

    assert "<SpeakerAttendeeAssignments" in meetings
    assert "speaker-assignments/suggest" in assignments
    assert 'confirmed: true' in assignments
    assert "participantId" in assignments
    assert "suggestionSource" in assignments
    assert 'contact.type === "resource"' in assignments
    assert "declined invitation" in assignments
    assert "Saved voice and account matches run first on this device." in assignments
    assert "Outlook email addresses are not sent." in assignments
    assert "Every suggestion stays unconfirmed until you approve it." in assignments
    assert "Confirmed mappings improve speaker names in the transcript." in assignments


def test_meeting_copy_uses_plain_outcome_focused_language() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert "Your recording is saved every 30 seconds" in meetings
    assert ">Safety saves<" in meetings
    assert ">Final transcript<" in meetings
    assert "Answers use only this meeting's final transcript." in meetings
    assert "Creating your meeting brief…" in meetings
    assert "Added on this device · up to 60 min" in meetings
    assert "Render-aware AEC3" not in meetings
    assert ">Native capture<" not in meetings
    assert ">Final STT<" not in meetings
    assert "canonical transcript" not in meetings


def test_meeting_workspace_uses_the_shared_transcription_frame_and_type_scale() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    live_mic = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx").read_text(
        encoding="utf-8"
    )
    page_intro = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "page-intro.tsx"
    ).read_text(encoding="utf-8")
    styles = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(
        encoding="utf-8"
    )

    assert "transcription-page meetings-page" in meetings
    assert '<PageIntro' in meetings
    assert 'eyebrow="Meeting workspace · 02"' in meetings
    assert 'app-page-shell' in meetings
    assert 'data-page-shell="meetings"' in meetings
    assert 'max-w-[1440px]' not in meetings
    assert 'max-w-[1680px]' not in meetings
    assert 'meetings-history-rail rounded-[22px]' in meetings
    assert 'meetings-workspace-panel min-w-0 overflow-hidden rounded-[26px]' in meetings
    assert 'min-[1380px]:grid-cols-[minmax(0,1fr)_260px]' in meetings
    assert '2xl:grid-cols-[minmax(0,1fr)_300px]' in meetings
    assert '2xl:border-l 2xl:border-t-0' in meetings

    assert 'app-page-shell' in live_mic
    assert 'data-page-shell="live-mic"' in live_mic
    assert 'actions?: ReactNode' in page_intro
    assert 'text-[36px]' in page_intro
    assert 'md:text-[42px]' in page_intro
    assert '.meetings-history-rail' in styles
    assert '.meetings-workspace-panel' in styles
    assert 'background: var(--live-core);' in styles


def test_primary_tabs_share_the_same_max_width_page_shell() -> None:
    styles = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(
        encoding="utf-8"
    )
    shell_rule = styles.split(".app-page-shell {", 1)[1].split("}", 1)[0]

    assert "width: 100%;" in shell_rule
    assert "max-width: 1320px;" in shell_rule
    assert "margin-inline: auto;" in shell_rule
    assert "max-width: 1380px;" not in styles

    expected_shells = {
        "LiveMic.tsx": "live-mic",
        "Meetings.tsx": "meetings",
        "Youtube.tsx": "youtube",
        "FileTranscribe.tsx": "file",
        "DebugConsole.tsx": "console",
        "Settings.tsx": "settings",
    }
    pages_dir = REPO_ROOT / "Frontend" / "client" / "src" / "pages"
    for filename, shell_name in expected_shells.items():
        source = (pages_dir / filename).read_text(encoding="utf-8")
        assert source.count("app-page-shell") == 1, filename
        assert source.count(f'data-page-shell="{shell_name}"') == 1, filename

    meetings = (pages_dir / "Meetings.tsx").read_text(encoding="utf-8")
    assert 'max-w-[1440px]' not in meetings


def test_primary_tabs_share_youtube_dark_workspace_palette() -> None:
    styles = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(
        encoding="utf-8"
    )
    dark_shell = styles.split(".dark .app-page-shell {", 1)[1].split("}", 1)[0]

    expected_tokens = {
        "--live-core: rgba(22, 26, 34, 0.88);",
        "--live-control: rgba(18, 22, 30, 0.72);",
        "--live-transcript: rgba(34, 38, 47, 0.7);",
        "--live-well: rgba(13, 17, 24, 0.72);",
        "--live-card: rgba(27, 32, 41, 0.8);",
        "--live-card-hover: rgba(34, 40, 51, 0.94);",
        "--workspace-border: rgba(255, 255, 255, 0.09);",
        "--background: 220 21% 11%;",
        "--card: 222 19% 13%;",
        "--muted: 224 15% 15%;",
    }
    for token in expected_tokens:
        assert token in dark_shell

    assert ".dark .live-mic-page,\n.dark .transcription-page" not in styles
    assert "--dc-surface: var(--live-card);" in styles
    assert "--dc-surface-raised: var(--live-card-hover);" in styles
    assert "--dc-surface-deep: var(--live-well);" in styles

    settings = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")
    live_mic = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "LiveMic.tsx"
    ).read_text(encoding="utf-8")
    assert "dark:bg-[var(--live-core)]" in settings
    assert "dark:bg-[var(--live-card)]" in settings
    assert "dark:bg-[var(--live-well)]" in settings
    assert "dark:border-[var(--workspace-border)]" in settings
    assert "dark:bg-[var(--live-card)]" in live_mic


def test_meeting_export_uses_native_save_as_and_visible_follow_up_actions() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )
    export_client = (
        REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "meeting-export.ts"
    ).read_text(encoding="utf-8")
    export_shell = (
        REPO_ROOT / "Frontend" / "src-tauri" / "src" / "export_dialog.rs"
    ).read_text(encoding="utf-8")
    shell = (REPO_ROOT / "Frontend" / "src-tauri" / "src" / "lib.rs").read_text(
        encoding="utf-8"
    )

    assert "downloadApiFile" not in meetings
    assert "Save or share" in meetings
    assert "Saved in" in meetings
    assert "Open file" in meetings
    assert "Open folder" in meetings
    assert "Save email draft" in meetings
    assert 'invoke<NativeSavedMeetingExport | null>("save_meeting_export"' in export_client
    assert 'invoke("open_meeting_export"' in export_client
    assert 'invoke("reveal_meeting_export"' in export_client
    assert ".blocking_save_file()" in export_shell
    assert "write_export_atomically" in export_shell
    assert "MAX_RECENT_EXPORTS" in export_shell
    assert "export_dialog::save_meeting_export" in shell


def test_meeting_workspace_guards_async_state_and_touch_delete_controls() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(
        encoding="utf-8"
    )

    assert "min-[1100px]:pointer-events-none" in meetings
    assert "min-[1100px]:group-hover:pointer-events-auto" in meetings
    assert "h-11 w-11" in meetings
    assert 'scope: { id: "meeting-action-item-updates" }' in meetings
    assert 'key={`${item.id}:${item.updatedAt}`}' in meetings
    assert "applyMeetingActionItem(queryClient, variables.id, item);" in meetings
    assert 'queryKey: ["/api/meetings", variables.id, "deliveries"]' in meetings


def test_boot_shell_applies_theme_before_react_and_uses_contrasting_logo() -> None:
    index = (REPO_ROOT / "Frontend" / "client" / "index.html").read_text(encoding="utf-8")
    theme = (REPO_ROOT / "Frontend" / "client" / "public" / "boot-theme.js").read_text(encoding="utf-8")
    css = (REPO_ROOT / "Frontend" / "client" / "public" / "boot.css").read_text(encoding="utf-8")

    assert '<script src="/boot-theme.js"></script>' in index
    assert '<div class="boot-shell"' in index
    assert 'src="/favicon-dark.svg"' in index
    assert 'window.localStorage.getItem("scriber-theme")' in theme
    assert 'document.documentElement.classList.toggle("dark", dark)' in theme
    assert ".dark .boot-logo-dark" in css
    assert "prefers-reduced-motion: reduce" in css


def test_frontend_motion_uses_transitions_dev_refine_and_polish_contract() -> None:
    client_src = REPO_ROOT / "Frontend" / "client" / "src"
    styles = (client_src / "index.css").read_text(encoding="utf-8")

    for token in (
        "--duration-quick: 150ms;",
        "--duration-fast: 250ms;",
        "--duration-medium: 350ms;",
        "--duration-slow: 400ms;",
        "--ease-smooth-out: cubic-bezier(0.22, 1, 0.36, 1);",
        "--scale-large: 0.96;",
        "--scale-medium: 0.97;",
        "--scale-small: 0.98;",
        "--scale-tiny: 0.99;",
    ):
        assert token in styles

    # Daily tab navigation is immediate; only newly created results reveal.
    assert "liveMicEnter" not in styles
    assert "debugConsoleEnter" not in styles
    assert "transcriptionResultReveal" in styles
    assert "animation: transcriptionResultReveal var(--duration-slow)" in styles
    assert "@media (hover: hover) and (pointer: fine)" in styles

    dialog = (client_src / "components" / "ui" / "dialog.tsx").read_text(encoding="utf-8")
    sheet = (client_src / "components" / "ui" / "sheet.tsx").read_text(encoding="utf-8")
    select = (client_src / "components" / "ui" / "select.tsx").read_text(encoding="utf-8")
    tooltip = (client_src / "components" / "ui" / "tooltip.tsx").read_text(encoding="utf-8")
    command = (client_src / "components" / "ui" / "command.tsx").read_text(encoding="utf-8")

    assert "data-[state=open]:duration-[var(--duration-fast)]" in dialog
    assert "data-[state=closed]:duration-[var(--duration-quick)]" in dialog
    assert "data-[state=open]:zoom-in-[0.96]" in dialog
    assert "data-[state=closed]:zoom-out-[0.99]" in dialog
    assert "data-[state=open]:duration-[var(--duration-slow)]" in sheet
    assert "data-[state=closed]:duration-[var(--duration-medium)]" in sheet
    assert "data-[state=open]:zoom-in-[0.97]" in select
    assert "data-[state=closed]:zoom-out-[0.99]" in select
    assert "delayDuration = 80" in tooltip
    assert "zoom-in-[0.98]" in tooltip
    assert "data-[state=closed]:duration-[50ms]" in tooltip
    assert "data-[state=open]:duration-0" in command
    assert "<DialogTitle" in command
    assert "<DialogDescription" in command

    # Refine forbids broad transitions in active frontend code.
    for path in client_src.rglob("*"):
        if path.suffix not in {".css", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "transition-all" not in source, path
        assert "transition: all" not in source, path


def test_frontend_motion_honors_reduced_motion_and_bounds_audio_visuals() -> None:
    client_src = REPO_ROOT / "Frontend" / "client" / "src"
    live_mic = (client_src / "pages" / "LiveMic.tsx").read_text(encoding="utf-8")
    overlay = (client_src / "components" / "NativeRecordingOverlay.tsx").read_text(
        encoding="utf-8"
    )
    skeleton = (client_src / "components" / "ui" / "skeleton.tsx").read_text(
        encoding="utf-8"
    )
    spinner = (client_src / "components" / "ui" / "spinner.tsx").read_text(
        encoding="utf-8"
    )
    youtube = (client_src / "pages" / "Youtube.tsx").read_text(encoding="utf-8")
    file_page = (client_src / "pages" / "FileTranscribe.tsx").read_text(encoding="utf-8")

    assert 'matchMedia?.("(prefers-reduced-motion: reduce)").matches' in live_mic
    assert "now - lastVisualFrame < 33" in live_mic
    assert "motion-reduce:transition-none" in overlay
    assert "transition-[opacity,filter]" in overlay
    assert "motion-reduce:animate-none" in skeleton
    assert "motion-reduce:animate-none" in spinner
    assert "transcription-thumbnail" in youtube
    assert "duration-700" not in youtube
    assert 'className="file-upload-mark' in file_page
    assert "duration-700" not in file_page
