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
    assert "let microphonePayload = mics;" in source
    assert "if (!Array.isArray(microphonePayload.devices))" in source
    assert 'fetch(apiUrl("/api/microphones"), { credentials: "include" })' in source
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
        'fal: { href: "https://fal.ai/dashboard/keys"',
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

    assert 'fetch(apiUrl("/api/settings"), {' in palette_source
    assert 'fetch(apiUrl("/api/transcripts?limit=50"), {' in palette_source
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
    assert 'type YoutubeHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";' in source
    assert "function youtubeHistoryStatus(item: TranscriptHistoryItem): YoutubeHistoryStatus" in source
    assert 'if (item.summaryStatus === "failed") return "summary_failed";' in source
    assert 'historyStatus === "summary_failed"' in source
    assert "Summary failed" in source
    assert "text-red-600 border-red-200 bg-red-50" in source
    assert "const isProcessing = isVisiblyProcessing(item);" not in source


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
    assert 'queryClient.invalidateQueries({ queryKey: ["/api/transcripts", msg.transcriptId], exact: true });' in source
    assert "msg.transcriptType" in source
    assert "<TranscriptHistoryInvalidationBridge />" in source


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
    assert "fetch(apiUrl(\"/api/settings\"), { credentials: \"include\", signal })" in helper_source

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
    assert 'fetch(apiUrl("/api/settings"), { credentials: "include", signal })' in visualizer_helper_source

    assert 'fetch(url, { credentials: "include" })' in youtube_source
    assert 'fetch(apiUrl("/api/youtube/transcribe"), {' in youtube_source
    assert 'credentials: "include",' in youtube_source
    assert 'fetch(apiUrl("/api/youtube/transcribe"), {' in detail_source
    assert 'credentials: "include",' in detail_source


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
    assert "const staleAvailable = Boolean(rawAvailable && !available);" in update_source
    assert 'phase: staleAvailable ? "current" : cache.phase || "idle"' in update_source
    assert "maybeNotify(cached);" in app_source


def test_settings_exposes_dedicated_post_processing_model_choice() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "const POST_PROCESSING_MODEL_OPTIONS" in settings_source
    assert 'const DEFAULT_POST_PROCESSING_MODEL = "openai/gpt-oss-120b";' in settings_source
    assert 'value: "openai/gpt-oss-120b"' in settings_source
    assert "GPT-OSS 120B Baseten" in settings_source
    assert "OpenRouter via Baseten; Cerebras fallback" in settings_source
    assert 'value: "openai/gpt-oss-120b:cerebras"' in settings_source
    assert "GPT-OSS 120B Cerebras" in settings_source
    assert "OpenRouter via Cerebras only" in settings_source
    assert 'baseten: "/provider-icons/baseten.svg"' in settings_source
    assert 'cerebras: "/provider-icons/cerebras.svg"' in settings_source
    assert 'value: "google/gemini-2.5-flash-lite:nitro"' in settings_source
    assert "Low-cost OpenRouter route" in settings_source
    assert "const [postProcessingModel, setPostProcessingModel]" in settings_source
    assert "setPostProcessingModel(settings.postProcessingModel || DEFAULT_POST_PROCESSING_MODEL);" in settings_source
    assert "const handlePostProcessingModelChange = async (value: string)" in settings_source
    assert "await updateSettings({ postProcessingModel: value });" in settings_source
    assert 'label="Post-processing model"' in settings_source
    assert 'value={postProcessingModel}' in settings_source
    assert "onValueChange={(value) => void handlePostProcessingModelChange(value)}" in settings_source
    assert "POST_PROCESSING_MODEL_OPTIONS.map((option)" in settings_source
    assert "selectedPostProcessingModelOption" in settings_source
    assert "<ProviderIcon icon={option.icon} label={option.label}" in settings_source
    assert "<SelectItem key={option.value} value={option.value} disabled={Boolean(disabledReason)}>" in settings_source
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


def test_settings_model_choices_require_saved_api_keys() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "type CredentialRequirement" in settings_source
    assert "savedKeys[provider] === true" in settings_source
    assert "const requiredCredentialForTranscriptionModel = (model: string)" in settings_source
    assert "const requiredCredentialForLanguageModel = (model: string)" in settings_source
    assert "const missingCredentialReason = (requirement: CredentialRequirement | null)" in settings_source
    assert 'title: "API key required"' in settings_source
    assert "must be saved below before this model can be selected." in settings_source
    assert "disabled={Boolean(disabledReason)}" in settings_source
    assert "<SelectItem key={option.value} value={option.value} disabled={Boolean(disabledReason)}>" in settings_source
    assert "Save the {missingPostProcessingCredentialRequirement.label} below before using the selected cleanup model." in settings_source
    assert "API key required before model selection." in settings_source
    assert "setSavedKeys({" in settings_source
    assert 'OpenRouter: hasValue(keys.openrouter)' in settings_source


def test_settings_custom_vocabulary_autosaves_without_manual_button() -> None:
    settings_source = (
        REPO_ROOT / "Frontend" / "client" / "src" / "pages" / "Settings.tsx"
    ).read_text(encoding="utf-8")

    assert "Save vocabulary" not in settings_source
    assert "const savedCustomVocabularyRef = useRef(\"\");" in settings_source
    assert "const saveCustomVocabulary = useCallback(async (nextValue: string)" in settings_source
    assert "window.setTimeout(() => {" in settings_source
    assert "void saveCustomVocabulary(customVocabulary);" in settings_source
    assert "await saveCustomVocabulary(customVocabulary);" in settings_source


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
    assert "let reconnectTimer: number | null = null;" in source
    assert "reconnectTimer = window.setTimeout(connect, 750);" in source
