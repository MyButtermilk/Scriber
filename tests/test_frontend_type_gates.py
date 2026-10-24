from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]

_SOURCE_TOKEN_PATTERN = re.compile(
    r"""
    "(?:\\.|[^"\\])*"
    |'(?:\\.|[^'\\])*'
    |`(?:\\.|[^`\\])*`
    |[A-Za-z_$][A-Za-z0-9_$]*
    |\d+(?:\.\d+)?
    |===|!==|==|!=|=>|\?\?|\?\.|&&|\|\||<=|>=|\+\+|--|\+=|-=|\*=|/=|\*\*
    |[^\s]
    """,
    re.VERBOSE,
)


def source_token_sequence_position(source: str, expected: str) -> int:
    """Find an exact token sequence while ignoring formatting-only changes."""
    expected_tokens = _SOURCE_TOKEN_PATTERN.findall(expected)
    assert expected_tokens, "Expected source snippet must contain at least one token."

    pattern_parts: list[str] = []
    for index, token in enumerate(expected_tokens):
        if index:
            previous = expected_tokens[index - 1]
            pattern_parts.append(r"(?:\s*,)?\s*" if token in {")", "]", "}"} and previous != "," else r"\s*")
        escaped = re.escape(token)
        if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token):
            escaped = rf"(?<![A-Za-z0-9_$]){escaped}(?![A-Za-z0-9_$])"
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            escaped = rf"(?<![A-Za-z0-9_$.]){escaped}(?![A-Za-z0-9_$.])"
        pattern_parts.append(escaped)

    match = re.search("".join(pattern_parts), source, flags=re.DOTALL)
    assert match, f"Expected token sequence not found: {expected}"
    return match.start()


def assert_source_contains_tokens(source: str, expected: str) -> None:
    source_token_sequence_position(source, expected)


def test_desktop_window_manifest_supports_custom_titlebar() -> None:
    config = json.loads((REPO_ROOT / "Frontend" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    window = config["app"]["windows"][0]
    assert window["decorations"] is False
    assert window["visible"] is False


def test_main_webview_reports_only_bounded_long_task_timings() -> None:
    main_source = (REPO_ROOT / "Frontend" / "client" / "src" / "main.tsx").read_text(encoding="utf-8")
    app_source = (REPO_ROOT / "Frontend" / "client" / "src" / "App.tsx").read_text(encoding="utf-8")
    performance_source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "frontend-performance.ts").read_text(
        encoding="utf-8"
    )

    assert "startFrontendLongTaskObserver()" in main_source
    assert "setFrontendPerformanceReportingEnabled(isOnline)" in app_source
    assert 'supportedEntryTypes.includes("longtask")' in performance_source
    assert "entry.duration <= LONG_TASK_THRESHOLD_MS" in performance_source
    assert "MAX_PENDING_ENTRIES = 64" in performance_source
    assert "MAX_RETRY_ATTEMPTS = 5" in performance_source
    assert "observerSupported = false" in performance_source
    assert 'apiUrl("/api/runtime/frontend-performance")' in performance_source
    assert "observer.takeRecords()" in performance_source
    assert "pendingHeartbeatSequence" in performance_source
    assert 'message.type !== "frontend_performance_flush"' in app_source
    assert "message.heartbeatSequence" in app_source
    for sensitive_field in ("attribution", "entry.name", "location.href", "document."):
        assert sensitive_field not in performance_source


def test_settings_put_commits_the_server_response_to_the_global_query_cache() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    update_section = source[source.index("const updateSettings =") : source.index("const saveCustomVocabulary =")]

    parsed_response = "const updatedSettings = (await res.json()) as SettingsResponse;"
    cache_write = 'queryClient.setQueryData<SettingsResponse>(["/api/settings"], updatedSettings);'
    bootstrap_invalidation = "invalidateSettingsBootstrap();"
    returned_response = "return updatedSettings;"

    assert parsed_response in update_section
    assert cache_write in update_section
    assert bootstrap_invalidation in update_section
    assert returned_response in update_section
    assert "settingsUpdateQueueRef.current.then(async () =>" in update_section
    assert "settingsUpdateQueueRef.current = request.then(" in update_section
    assert update_section.index(parsed_response) < update_section.index(cache_write)
    assert update_section.index(cache_write) < update_section.index(bootstrap_invalidation)
    assert update_section.index(bootstrap_invalidation) < update_section.index(returned_response)


def test_history_and_command_requests_are_abortable_and_deadlined() -> None:
    history_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-transcript-history-query.ts"
    ).read_text(encoding="utf-8")
    command_source = (REPO_ROOT / "Frontend" / "client" / "src" / "components" / "CommandPalette.tsx").read_text(
        encoding="utf-8"
    )
    live_mic_control_source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "live-mic-control.ts").read_text(
        encoding="utf-8"
    )
    tray_source = (REPO_ROOT / "Frontend" / "client" / "src" / "components" / "TrayPanel.tsx").read_text(
        encoding="utf-8"
    )

    assert "fetchWithTimeout(" in history_source
    assert "queryFn: async ({ pageParam, signal })" in history_source
    assert "signal?: AbortSignal" in history_source
    assert command_source.count("fetchWithTimeout(") >= 2
    assert "fetchWithTimeout(" in live_mic_control_source
    assert "queryFn: async ({ signal })" in command_source
    assert "fetchWithTimeout(" in tray_source


def test_desktop_shell_commands_have_deadlines() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts").read_text(encoding="utf-8")

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


def test_windows_taskbar_identity_uses_the_contrast_safe_tray_artwork() -> None:
    tauri_root = REPO_ROOT / "Frontend" / "src-tauri"
    config = json.loads((tauri_root / "tauri.conf.json").read_text(encoding="utf-8"))
    bundle_icon = tauri_root / "icons" / "icon.ico"
    master_svg = (tauri_root / "icons" / "windows-app-icon.svg").read_text(encoding="utf-8")
    dark_app_svg = (REPO_ROOT / "Frontend" / "client" / "public" / "favicon-dark.svg").read_text(encoding="utf-8")

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
        for taskbar_pixel, tray_pixel in zip(taskbar_frame.getdata(), tray_frame.getdata(), strict=True)
        for taskbar_channel, tray_channel in zip(taskbar_pixel, tray_pixel, strict=True)
    ) / (32 * 32 * 4)
    assert mean_channel_delta < 1.0
    assert (
        sum(
            alpha >= 220 and red >= 235 and green >= 235 and blue >= 235
            for red, green, blue, alpha in taskbar_frame.getdata()
        )
        >= 450
    )
    feather_pixels = [
        (x, y)
        for y in range(taskbar_frame.height)
        for x in range(taskbar_frame.width)
        if taskbar_frame.getpixel((x, y))[3] >= 200 and max(taskbar_frame.getpixel((x, y))[:3]) < 100
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


def test_all_tray_states_preserve_white_disc_identity_and_semantic_badges() -> None:
    icon_dir = REPO_ROOT / "Frontend" / "src-tauri" / "icons"
    icons: dict[str, Image.Image] = {}

    for size in (16, 20, 24, 28, 32, 36, 40, 48):
        for state in ("normal", "update", "recording"):
            rgba = (icon_dir / f"tray-{state}-{size}.rgba").read_bytes()
            assert len(rgba) == size * size * 4
            icon = Image.frombytes("RGBA", (size, size), rgba)
            assert icon.getpixel((0, 0))[3] == 0

        normal = Image.frombytes("RGBA", (size, size), (icon_dir / f"tray-normal-{size}.rgba").read_bytes())
        assert sum(
            alpha >= 200 and red >= 225 and green >= 225 and blue >= 225 for red, green, blue, alpha in normal.getdata()
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
            red >= 205 and green >= 205 and blue >= 205 for red, green, blue, _alpha in composited.getdata()
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
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    websocket_source = (REPO_ROOT / "Frontend" / "client" / "src" / "contexts" / "WebSocketContext.tsx").read_text(
        encoding="utf-8"
    )
    device_refresh_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "hooks" / "use-device-change-refresh.ts"
    ).read_text(encoding="utf-8")
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(encoding="utf-8")

    assert "export interface MicrophoneDevice" in api_types
    assert "export interface MicrophonesResponse" in api_types
    assert "export interface MicrophonesRefreshResponse" in api_types
    assert "import type {" in source
    assert "MicrophoneDevice," in source
    assert "MicrophonesResponse," in source
    assert "useState<MicrophoneDevice[]>([])" in source
    assert "(await response.json()) as MicrophonesResponse" in source
    assert "(await res.json()) as MicrophonesResponse" in source
    assert "let microphonePayload = microphones;" in source
    assert "if (!Array.isArray(microphonePayload.devices))" in source
    assert "fetchWithTimeout(" in source
    assert 'apiUrl("/api/microphones")' in source
    assert 'aria-label={t("Select input device")}' in source
    assert 'aria-label={t("Select microphone {{microphone}}", { microphone: deviceLabel })}' in source
    assert 't("Loading devices...")' in source
    assert "const previousDeviceId = selectedDeviceId;" in source
    assert "setSelectedDeviceId(previousDeviceId);" in source
    assert "const saved = await handleMicDeviceChange(deviceId);" in source
    assert "if (!saved)" in source
    assert 'favoriteMicRestored && typeof msg.restoredDeviceId === "string"' in source
    assert "import type { MicrophoneDevice }" in websocket_source
    assert "devices: MicrophoneDevice[]" in websocket_source
    assert "const closeCurrentSocket = useCallback" in websocket_source
    assert "if (wsRef.current !== ws)" in websocket_source
    assert_source_contains_tokens(websocket_source, "clearReconnectTimeout(); if (!enabled)")
    assert "import type { MicrophonesRefreshResponse }" in device_refresh_source
    assert "Promise<MicrophonesRefreshResponse | null>" in device_refresh_source
    assert "as { deviceId: string" not in source


def test_settings_provider_help_links_are_safe_external_links() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")

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
        assert_source_contains_tokens(source, link)

    assert "const API_KEY_HELP_LINKS = {" in source
    assert "type ApiKeyHelpKey = keyof typeof API_KEY_HELP_LINKS;" in source
    assert "const help = API_KEY_HELP_LINKS[helpKey];" in source
    assert "href={help.href}" in source
    assert 'target="_blank"' in source
    assert 'rel="noreferrer"' in source
    assert "title={t(help.label)}" in source
    assert "void openExternalHelpUrl(help.href);" in source
    assert 'const { openUrl } = await import("@tauri-apps/plugin-opener");' in source
    assert "await openUrl(url);" in source
    assert 'value: "minimax/minimax-m3:nitro"' in source
    assert 'value: "z-ai/glm-5.3-flash:nitro"' in source
    assert 'label: "GLM 5.3 Flash Nitro"' in source
    assert "aaLanguageBenchmarkDetail(0.1, 57, localeTag, t)" in source
    assert 'provider="OpenRouter"' in source
    assert 'if (provider === "OpenRouter") apiKeys.openrouter = openRouterKey;' in source


def test_youtube_page_proxies_thumbnails_and_hides_completed_spinners() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(encoding="utf-8")
    api_types = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "api-types.ts").read_text(encoding="utf-8")

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
    assert 'decoding="async"' in source
    assert 'referrerPolicy="no-referrer"' in source
    assert "URL.createObjectURL(blob)" not in source
    assert "function isCompletedStep" in source
    assert "function isVisiblyProcessing" in source
    assert 'item.summaryStatus === "pending"' in source
    assert 'type YoutubeHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";' in source
    assert "function youtubeHistoryStatus(item: TranscriptHistoryItem): YoutubeHistoryStatus" in source
    assert 'if (item.summaryStatus === "failed") return "summary_failed";' in source
    assert 'historyStatus === "summary_failed"' in source
    assert "TranscriptSummaryRetryButton" in source
    assert "onSummaryRetryComplete" in source
    assert 'historyStatus === "failed"' in source
    assert "onTranscriptionRetry(event, item)" in source
    assert 't("Retry transcription")' in source
    assert 'apiUrl("/api/youtube/transcribe")' in source
    assert 'description: t("No source URL is available for this video."),' in source
    assert (
        'description: t("A new transcription attempt for “{{title}}” has been queued.", { title: item.title }),'
        in source
    )
    assert "isRetryingTranscription={retryingTranscriptId === item.id}" in source
    assert source.count('? t("Summarizing…")') == 2
    assert "border-red-200 bg-red-50/95" in source
    assert "const isProcessing = isVisiblyProcessing(item);" not in source
    assert "youtubePreferCaptions" in api_types
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert 'label={t("YouTube captions first")}' in settings_source
    assert "youtubePreferCaptions" in settings_source
    assert settings_source.index('id="settings-summaries"') < settings_source.index(
        'label={t("YouTube captions first")}'
    )
    assert settings_source.index('label={t("Auto-summarize")}') < settings_source.index(
        'label={t("YouTube captions first")}'
    )
    assert "preferCaptions," not in source


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


def test_youtube_async_start_only_navigates_while_the_user_is_still_on_youtube() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(encoding="utf-8")
    start_section = source[
        source.index("const startTranscription = useCallback(") : source.index("const deleteTranscript")
    ]

    assert "const [location, setLocation] = useLocation();" in source
    assert ('const currentPath = typeof window !== "undefined" ? window.location.pathname : location;') in start_section
    assert "const queuedBrowserImport =" in start_section
    assert (
        'if (currentPath === "/youtube" && !queuedBrowserImport) {\n'
        "            setLocation(`/transcript/${rec.id}`);\n"
        "          }"
    ) in start_section


def test_settings_and_youtube_mutations_use_authenticated_backend_access() -> None:
    backend_source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "backend.ts").read_text(encoding="utf-8")
    settings_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    visualizer_helper_source = (REPO_ROOT / "Frontend" / "client" / "src" / "lib" / "visualizer-settings.ts").read_text(
        encoding="utf-8"
    )
    youtube_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Youtube.tsx").read_text(encoding="utf-8")
    detail_source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "TranscriptDetail.tsx").read_text(
        encoding="utf-8"
    )

    assert "function appendSessionToken(url: string): string" in backend_source
    assert 'parsed.searchParams.set("scriberToken", backendSessionToken);' in backend_source
    assert 'parsed.pathname === "/ws" || parsed.pathname.startsWith("/api/")' in backend_source

    assert "await updateSettings({ favoriteMic: newFavorite });" in settings_source
    assert "await updateSettings({ hotkey });" in settings_source
    assert 'await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });' in settings_source
    assert "onSave={(count) => updateSettings({ visualizerBarCount: count })}" in settings_source
    assert "await onSave(count);" in settings_source
    assert "fetchWithTimeout(" in visualizer_helper_source
    assert '{ credentials: "include", signal }' in visualizer_helper_source

    assert 'fetchWithTimeout(url, { credentials: "include" }, 30_000)' in youtube_source
    assert_source_contains_tokens(youtube_source, 'fetchWithTimeout(apiUrl("/api/youtube/transcribe"), {')
    assert 'credentials: "include",' in youtube_source
    assert re.search(r'fetchWithTimeout\(\s*apiUrl\("/api/youtube/transcribe"\),\s*\{', detail_source)
    assert 'credentials: "include",' in detail_source


def test_settings_hotkey_recorder_uses_window_capture_listener() -> None:
    source = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")

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
    assert 'aria-label={t("Hotkey capture area")}' in source
    assert 'useState("Ctrl + Shift + D")' in source
    assert 'useState("Ctrl + Shift + F")' in source
    assert 'useState("Ctrl + Shift + M")' in source
    assert 'settings.hotkeyRaw || "Ctrl + Shift + D"' in source
    assert 'settings.postProcessingHotkeyRaw || "Ctrl + Shift + F"' in source
    assert 'settings.meetingHotkeyRaw || "Ctrl + Shift + M"' in source
    assert "onKeyDown={handleHotkeyRecord}" not in source


def test_meeting_workspace_scopes_drafts_playback_and_imports_to_durable_state() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(encoding="utf-8")
    note_autosave = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "meeting" / "useMeetingNotesAutosave.ts"
    ).read_text(encoding="utf-8")
    note_editor = (
        REPO_ROOT / "Frontend" / "client" / "src" / "components" / "meeting" / "MeetingNotesEditor.tsx"
    ).read_text(encoding="utf-8")
    settings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")

    assert "const chatQuestionStore = useTextDraftStore();" in meetings
    assert meetings.count('chatQuestionStore.set("");') == 2
    assert "}, [chatQuestionStore, selectedId]);" in meetings
    assert "variables.id !== selectedId" in meetings
    assert 'setTranscriptSearch("");' in meetings
    assert 'from "@/components/meeting/useMeetingNotesAutosave"' in meetings
    assert 'detail?.notes.find((item) => item.id === "workspace")?.body ?? ""' in meetings
    assert "const noteAutosave = useMeetingNotesAutosave({" in meetings
    assert "initialBody: workspaceNoteBody" in meetings
    assert "meetingId: selectedId" in meetings
    assert "saveNote: saveWorkspaceMeetingNote" in meetings
    assert "applyMeetingNoteEvent(queryClient, meetingId, savedNote);" in meetings
    assert meetings.count("<MeetingNotesEditor autosave={noteAutosave}") == 2
    assert "const queues = useRef(new Map<string, MeetingNotesSaveQueue>());" in note_autosave
    assert "const existing = queues.current.get(meetingId);" in note_autosave
    assert "queues.current.set(meetingId, created);" in note_autosave
    assert "queue.hydrate(initialBody);" in note_autosave
    assert "queue.flushForTeardown();" in note_autosave
    assert "queue.flush();" in note_autosave
    assert "value={body}" in note_editor
    assert "onChange={(event) => setBody(event.target.value)}" in note_editor
    assert 'asset.kind === "playback_mix"' in meetings
    assert "src={apiUrl(`/api/meetings/${detail.id}/audio`)}" in meetings
    assert "meetingSpeakerSampleWindow(" in meetings
    assert "canPlaySpeakerSamples" in meetings
    assert "availablePlaybackSources" not in meetings
    assert "mutedSources" not in meetings
    assert "playbackSourceForSegment" not in meetings
    assert "playbackSourceForMuteState" not in meetings
    assert "Playback track controls" not in meetings
    assert "Audio is no longer retained" in meetings
    assert "meetingImportsQuery.data?.items.find" in meetings
    assert "job.meetingId" in meetings
    assert 'queryKey: ["/api/meetings/speaker-profiles"]' in settings
    # The Meeting workspace now intentionally reads the same local Voice
    # Library query as Settings so a matched generic voice can be previewed and
    # renamed in context. Keep it lazy: Meetings without a profile link must
    # not fetch the biometric-library metadata.
    assert "const speakerProfilesQuery" in meetings
    assert "enabled: Boolean(detail?.speakers.some((speaker) => speaker.profileId))" in meetings
    assert "onClick={() => setVoiceLibraryDeleteOpen(true)}" in settings
    assert "Delete all saved voice data?" in settings
    assert "Delete this saved speaker?" in settings
    assert "voiceLibraryDeletePending" in settings
    assert "toggleSpeakerPreview" in settings
    assert "apiUrl(current.preview.url)" in settings
    assert 't("Play sample")' in settings


def test_outlook_disconnect_and_speaker_assignments_require_explicit_confirmation() -> None:
    client = REPO_ROOT / "Frontend" / "client" / "src"
    settings = (client / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    meetings = (client / "pages" / "Meetings.tsx").read_text(encoding="utf-8")
    assignments = (client / "components" / "meeting" / "SpeakerAttendeeAssignments.tsx").read_text(encoding="utf-8")

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
    assert "confirmed: true" in assignments
    assert "participantId" in assignments
    assert "suggestionSource" in assignments
    assert 'contact.type === "resource"' in assignments
    assert "declined invitation" in assignments
    assert "Saved voice and account matches run first on this device." in assignments
    assert "Outlook email addresses are not sent." in assignments
    assert "Every suggestion stays unconfirmed until you approve it." in assignments
    assert "Confirmed mappings improve speaker names in the transcript." in assignments


def test_meeting_workspace_guards_async_state_and_touch_delete_controls() -> None:
    meetings = (REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Meetings.tsx").read_text(encoding="utf-8")

    assert "min-[1100px]:pointer-events-none" in meetings
    assert "min-[1100px]:group-hover:pointer-events-auto" in meetings
    assert "h-11 w-11" in meetings
    assert 'scope: { id: "meeting-action-item-updates" }' in meetings
    assert "<div key={item.id}" in meetings
    assert "<div key={item.updatedAt}" in meetings
    assert "<AnimatedCheckbox" in meetings
    assert "applyMeetingActionItem(queryClient, variables.id, item);" in meetings
    assert 'queryKey: ["/api/meetings", variables.id, "deliveries"]' in meetings
