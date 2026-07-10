from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frontend_browser_smoke_validate_only_writes_artifact(tmp_path: Path) -> None:
    output_path = tmp_path / "frontend-browser-smoke.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_frontend_browser.py",
            "--validate-only",
            "--output",
            str(output_path),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["routeCount"] == 9
    assert payload["summary"]["criticalConsoleErrorCount"] == 0
    assert payload["summary"]["interactionCheckCount"] == 17
    assert set(payload["summary"]["interactionChecks"]) == {
        "history-search-copy-navigation",
        "youtube-history-actions",
        "youtube-thumbnails",
        "youtube-start-transcription",
        "file-history-actions",
        "file-upload-error",
        "file-drag-drop",
        "debug-console-actions",
        "settings-persistence",
        "settings-desktop-controls",
        "transcript-processing-refresh",
        "command-palette",
        "transcript-detail-actions",
        "transcript-cancel-action",
        "mobile-navigation",
        "mobile-route-layouts",
        "token-required-browser-state",
    }
    assert "/settings" in payload["summary"]["routes"]
    assert "/debug" in payload["summary"]["routes"]
    assert "/transcript/youtube-processing-smoke" in payload["summary"]["routes"]
    assert "/transcript/mic-no-summary-smoke" in payload["summary"]["routes"]
    assert "/transcript/mic-summary-failed-smoke" in payload["summary"]["routes"]
    assert set(payload["summary"]["virtualizedHistoryRoutes"]) == {"/", "/youtube", "/file"}
    live_mic = next(item for item in payload["scenarios"] if item["route"] == "/")
    assert live_mic["interactionChecks"] == [{"name": "history-search-copy-navigation", "ok": True}]
    youtube = next(item for item in payload["scenarios"] if item["route"] == "/youtube")
    assert youtube["interactionChecks"] == [
        {"name": "youtube-history-actions", "ok": True},
        {"name": "youtube-thumbnails", "ok": True},
        {"name": "youtube-start-transcription", "ok": True},
    ]
    debug = next(item for item in payload["scenarios"] if item["route"] == "/debug")
    assert "Clear logs" in debug["expectedText"]
    assert debug["interactionChecks"] == [{"name": "debug-console-actions", "ok": True}]
    settings = next(item for item in payload["scenarios"] if item["route"] == "/settings")
    assert settings["expectedText"] == ["Settings", "Speech-to-text provider", "API keys"]
    assert settings["interactionChecks"] == [
        {"name": "settings-persistence", "ok": True},
        {"name": "settings-desktop-controls", "ok": True},
    ]
    file = next(item for item in payload["scenarios"] if item["route"] == "/file")
    assert file["interactionChecks"] == [
        {"name": "file-history-actions", "ok": True},
        {"name": "file-upload-error", "ok": True},
        {"name": "file-drag-drop", "ok": True},
    ]
    assert payload["commandPaletteCheck"]["name"] == "command-palette"
    assert payload["transcriptDetailActionsCheck"]["name"] == "transcript-detail-actions"
    assert payload["transcriptCancelCheck"]["name"] == "transcript-cancel-action"
    assert payload["mobileNavigationCheck"]["name"] == "mobile-navigation"
    assert payload["mobileRouteLayoutsCheck"]["name"] == "mobile-route-layouts"
    assert payload["mobileRouteLayoutsCheck"]["routeCount"] == 9
    assert payload["tokenRequiredCheck"]["name"] == "token-required-browser-state"


def test_frontend_browser_smoke_validate_only_can_include_fast_tab_switch(tmp_path: Path) -> None:
    output_path = tmp_path / "frontend-browser-smoke-fast-tabs.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_frontend_browser.py",
            "--validate-only",
            "--fast-tab-switch",
            "--output",
            str(output_path),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["interactionCheckCount"] == 18
    assert "fast-tab-switch" in payload["summary"]["interactionChecks"]
    assert payload["fastTabSwitchCheck"]["name"] == "fast-tab-switch"
    assert payload["fastTabSwitchCheck"]["ok"] is True
    assert payload["fastTabSwitchCheck"]["routes"] == [
        "/youtube",
        "/file",
        "/settings",
        "/",
        "/youtube",
        "/file",
        "/",
    ]


def test_hybrid_goal_frontend_smoke_is_documented() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts\\smoke_frontend_browser.py" in agents
    assert "scripts\\smoke_frontend_browser.py" in readme


def test_frontend_browser_smoke_suppresses_expected_windows_teardown_noise() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "run_browser_smoke_with_clean_shutdown" in script
    assert "isinstance(exc, ConnectionResetError)" in script
    assert "loop.set_exception_handler(handle_loop_exception)" in script
    assert "asyncio.run(run_browser_smoke_with_clean_shutdown(args))" in script


def test_frontend_browser_smoke_exercises_mobile_navigation() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_mobile_navigation" in script
    assert "exercise_mobile_route_layouts" in script
    assert "Emulation.setDeviceMetricsOverride" in script
    assert "button[aria-label=\"Open navigation\"]" in script
    assert "mobileNavigationCheck" in script
    assert "mobileRouteLayoutsCheck" in script
    assert "\"mobile-navigation\"" in script
    assert "\"mobile-route-layouts\"" in script
    assert "overflowX" in script


def test_frontend_browser_smoke_exercises_fast_tab_switch() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_fast_tab_switch" in script
    assert "FAST_TAB_SWITCH_SEQUENCE" in script
    assert "Page.captureScreenshot" in script
    assert "routeReadyMs" in script
    assert "blankSampleCount" in script
    assert "--fast-tab-switch" in script
    assert "\"fast-tab-switch\"" in script


def test_frontend_browser_smoke_exercises_command_palette() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_command_palette" in script
    assert "Debug-Konsole" in script
    assert "Synthetic Recording 00003" in script
    assert "commandPaletteCheck" in script
    assert "\"command-palette\"" in script


def test_frontend_browser_smoke_exercises_debug_console_actions() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_debug_console_interaction" in script
    assert "runtime_logs_count" in script
    assert "Refresh logs" in script
    assert "Toggle auto refresh" in script
    assert "Toggle auto scroll" in script
    assert "textContent || '').trim() === 'Reset'" in script
    assert "Debug console sample warning" in script
    assert "Copy visible logs" in script
    assert "Download support bundle" in script
    assert "synthetic-support-bundle.zip" in script
    assert "support_bundle_count" in script
    assert "\"debug-console-actions\"" in script


def test_frontend_browser_smoke_exercises_youtube_history_actions() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_youtube_history_interactions" in script
    assert "Search history" in script
    assert "Copy transcript Synthetic Video 00002" in script
    assert "Delete transcript Synthetic Video 00002" in script
    assert "\"youtube-history-actions\"" in script


def test_frontend_browser_smoke_exercises_youtube_start_transcription() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_youtube_start_transcription" in script
    assert "youtube-queued-smoke" in script
    assert "Synthetic Queued YouTube Transcription" in script
    assert "Start transcription for Synthetic YouTube Result" in script
    assert "youtube_transcribe_requests" in script
    assert "\"youtube-start-transcription\"" in script


def test_frontend_browser_smoke_exercises_file_actions() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_file_history_interactions" in script
    assert "file-processing-smoke" in script
    assert "Synthetic File Processing" in script
    assert "processing queue" in script
    assert "View transcript Synthetic File Processing" in script
    assert "Synthetic processes files in-app up to 2GB" in script
    assert "Search files" in script
    assert "Copy transcript Synthetic File 00002" in script
    assert "Delete transcript Synthetic File 00002" in script
    assert "Synthetic upload limit exceeded" in script
    assert "\"file-history-actions\"" in script
    assert "\"file-upload-error\"" in script


def test_frontend_browser_smoke_exercises_history_interactions() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_history_interactions" in script
    assert "Search recordings" in script
    assert "Copy transcript Synthetic Recording 00002" in script
    assert "Delete transcript Synthetic Recording 00002" in script
    assert "unrelatedControlDeleted" in script
    assert "/transcript/mic-00001" in script
    assert "\"history-search-copy-navigation\"" in script


def test_frontend_browser_smoke_exercises_transcript_detail_actions() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_transcript_detail_actions" in script
    assert "Synthetic No Summary Recording" in script
    assert "Synthetic Failed Summary Recording" in script
    assert "Copy Transcript" in script
    assert "Export as PDF" in script
    assert "Export as DOCX" in script
    assert "Retry Summary" in script
    assert "\"transcript-detail-actions\"" in script


def test_frontend_browser_smoke_exercises_transcript_cancel_action() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_transcript_cancel_action" in script
    assert "youtube-cancel-smoke" in script
    assert "Synthetic Cancel Processing" in script
    assert "Task cancellation requested." in script
    assert "cancel_counts" in script
    assert "\"transcript-cancel-action\"" in script


def test_frontend_browser_smoke_exercises_settings_persistence() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_settings_interactions" in script
    assert "wait_for_settings_patches" in script
    assert "exercise_settings_help_links" in script
    assert "exercise_settings_favorite_mic" in script
    assert "gemini-flash-latest" in script
    assert "gemini-3.5-flash" in script
    assert "MiniMax M3 Nitro" in script
    assert "GLM 5.2 Nitro" in script
    assert "Mistral Batch" in script
    assert "save_settings_credential" in script
    assert "https://platform.openai.com/api-keys" in script
    assert "https://openrouter.ai/settings/keys" in script
    assert "https://console.mistral.ai/api-keys" in script
    assert "Set USB Smoke Microphone as favorite" in script
    assert "usb-smoke-mic" in script
    assert "Scriber, Gemini 3.5, Quality Loop" in script
    assert "\"settings-persistence\"" in script


def test_frontend_browser_smoke_exercises_settings_desktop_controls() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "exercise_settings_desktop_controls" in script
    assert "Ctrl + Alt + H" in script
    assert "Start with Windows" in script
    assert "Push-to-talk" in script
    assert "push_to_talk" in script
    assert "autostart_requests" in script
    assert "Check for updates" in script
    assert "Desktop updates are available in the installed Windows app." in script
    assert "desktopUpdateElapsedMs" in script
    assert "\"settings-desktop-controls\"" in script


def test_frontend_browser_smoke_uses_local_date_for_debug_filter() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_frontend_browser.py").read_text(encoding="utf-8")

    assert "now.getFullYear()" in script
    assert "now.getMonth() + 1" in script
    assert "now.getDate()" in script
    assert "toISOString().slice(0, 10)" not in script
