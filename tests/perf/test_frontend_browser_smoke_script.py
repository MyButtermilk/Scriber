from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import smoke_frontend_browser

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
    assert payload["summary"]["routeCount"] == 11
    assert "/meetings" in payload["summary"]["routes"]
    assert payload["summary"]["criticalConsoleErrorCount"] == 0
    assert payload["summary"]["interactionCheckCount"] == 22
    assert set(payload["summary"]["interactionChecks"]) == {
        "history-search-copy-navigation",
        "youtube-history-actions",
        "youtube-thumbnails",
        "youtube-start-transcription",
        "file-history-actions",
        "parallel-file-imports",
        "file-upload-error",
        "file-drag-drop",
        "meeting-end-to-end",
        "debug-console-actions",
        "settings-persistence",
        "settings-desktop-controls",
        "transcript-processing-refresh",
        "summary-reading-track",
        "command-palette",
        "transcript-detail-actions",
        "transcript-cancel-action",
        "rapid-theme-change",
        "desktop-page-shell-layouts",
        "mobile-navigation",
        "mobile-route-layouts",
        "token-required-browser-state",
    }
    assert "/settings" in payload["summary"]["routes"]
    assert "/debug" in payload["summary"]["routes"]
    assert "/transcript/youtube-processing-smoke" in payload["summary"]["routes"]
    assert "/transcript/file-00001" in payload["summary"]["routes"]
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
        {"name": "parallel-file-imports", "ok": True},
        {"name": "file-history-actions", "ok": True},
        {"name": "file-upload-error", "ok": True},
        {"name": "file-drag-drop", "ok": True},
    ]
    meetings = next(item for item in payload["scenarios"] if item["route"] == "/meetings")
    assert meetings["interactionChecks"] == [{"name": "meeting-end-to-end", "ok": True}]
    transcript_detail = next(item for item in payload["scenarios"] if item["route"] == "/transcript/file-00001")
    summary_reading_track = transcript_detail["interactionChecks"][0]
    assert summary_reading_track["name"] == "summary-reading-track"
    assert summary_reading_track["ok"] is True
    assert summary_reading_track["viewport"] == {"width": 2048, "height": 1252, "deviceScaleFactor": 1}
    assert summary_reading_track["state"]["viewportWidth"] == 2048
    assert summary_reading_track["viewportRestored"] is True
    assert payload["commandPaletteCheck"]["name"] == "command-palette"
    assert payload["transcriptDetailActionsCheck"]["name"] == "transcript-detail-actions"
    assert payload["transcriptCancelCheck"]["name"] == "transcript-cancel-action"
    assert payload["rapidThemeChangeCheck"]["name"] == "rapid-theme-change"
    desktop_layout = payload["desktopPageShellLayoutsCheck"]
    assert desktop_layout["name"] == "desktop-page-shell-layouts"
    assert desktop_layout["viewport"] == {"width": 2048, "height": 1252, "deviceScaleFactor": 1}
    assert desktop_layout["routeCount"] == 6
    assert desktop_layout["routes"] == ["/", "/meetings", "/youtube", "/file", "/debug", "/settings"]
    assert desktop_layout["maxWidthSpread"] <= 2
    assert desktop_layout["maxContentWidthSpread"] <= 2
    assert desktop_layout["maxPaddingSpread"] <= 2
    assert desktop_layout["maxGutterImbalance"] <= 2
    assert desktop_layout["maxCenterDelta"] <= 2
    assert desktop_layout["maxIntroLeftDelta"] <= 2
    assert desktop_layout["maxIntroWidthDelta"] <= 2
    assert desktop_layout["pageIntrosAligned"] is True
    assert desktop_layout["meetingAtMostLive"] is True
    assert desktop_layout["maxWidthReached"] is True
    assert all(item["maxWidthReached"] for item in desktop_layout["results"])
    assert payload["mobileNavigationCheck"]["name"] == "mobile-navigation"
    assert payload["mobileRouteLayoutsCheck"]["name"] == "mobile-route-layouts"
    assert payload["mobileRouteLayoutsCheck"]["routeCount"] == 11
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
    assert payload["summary"]["interactionCheckCount"] == 23
    assert "fast-tab-switch" in payload["summary"]["interactionChecks"]
    assert payload["fastTabSwitchCheck"]["name"] == "fast-tab-switch"
    assert payload["fastTabSwitchCheck"]["ok"] is True
    assert payload["fastTabSwitchCheck"]["routes"] == [
        "/youtube",
        "/file",
        "/meetings",
        "/settings",
        "/",
        "/youtube",
        "/meetings",
        "/file",
        "/",
    ]


class _SummaryReadingTrackCdp:
    def __init__(self, *, fail_phase: str = "", sticky_clear: bool = False) -> None:
        self.fail_phase = fail_phase
        self.sticky_clear = sticky_clear
        self.override_active = False
        self.viewport_width = 1280
        self.viewport_height = 900
        self.device_pixel_ratio = 1.0
        self.calls: list[str] = []
        self.mouse_events: list[dict[str, object]] = []
        self.phases: list[str] = []
        self.restore_attempted = False

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        del timeout
        self.calls.append(method)
        if method == "Emulation.setDeviceMetricsOverride":
            self.override_active = True
            assert params is not None
            self.viewport_width = int(params["width"])
            self.viewport_height = int(params["height"])
            self.device_pixel_ratio = float(params["deviceScaleFactor"])
        elif method == "Emulation.clearDeviceMetricsOverride":
            self.override_active = False
            if not self.sticky_clear:
                self.viewport_width = 1280
                self.viewport_height = 900
                self.device_pixel_ratio = 1.0
        elif method == "Input.dispatchMouseEvent":
            assert params is not None
            self.mouse_events.append(params)
        return {}

    async def evaluate(self, expression: str, *, timeout: float | None = None) -> dict[str, object]:
        del timeout
        phase = ""
        if ".restore()" in expression:
            phase = "restore"
            self.restore_attempted = True
        elif ".summary-document" in expression:
            phase = "ready"
        elif "const scrollDelta =" in expression:
            phase = "wheel"
        elif "current.hash === current.targetHref" in expression:
            phase = "navigation"
        elif "current.transcriptExpanded && current.transcriptTextVisible" in expression:
            phase = "transcript"
        if phase:
            self.phases.append(phase)
            if self.fail_phase == phase:
                raise RuntimeError(f"synthetic summary {phase} failure")
            return {
                "ok": self.viewport_width == 2048,
                "viewportWidth": self.viewport_width,
                "viewportHeight": self.viewport_height,
                "wheelTarget": {"ok": True, "x": 900, "y": 500},
                "tocTarget": {"ok": True, "x": 400, "y": 600},
                "transcriptTarget": {"ok": True, "x": 900, "y": 1150},
                "transcriptExpanded": False,
            }
        return {
            "viewportWidth": self.viewport_width,
            "viewportHeight": self.viewport_height,
            "devicePixelRatio": self.device_pixel_ratio,
        }


@pytest.mark.asyncio
async def test_summary_reading_track_measures_at_wide_viewport_and_restores_it() -> None:
    cdp = _SummaryReadingTrackCdp()

    result = await smoke_frontend_browser.exercise_summary_reading_track(cdp, timeout_sec=1)

    assert result["ok"] is True
    assert result["state"]["viewportWidth"] == 2048
    assert result["viewportRestored"] is True
    assert result["pageRestored"] is True
    assert result["originalViewport"] == result["restoredViewport"]
    assert [method for method in cdp.calls if method.startswith("Emulation.")] == [
        "Emulation.setDeviceMetricsOverride",
        "Emulation.clearDeviceMetricsOverride",
    ]
    assert cdp.mouse_events[0] == {"type": "mouseWheel", "x": 900, "y": 500, "deltaX": 0, "deltaY": 640}
    assert [event["type"] for event in cdp.mouse_events[1:]] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert cdp.phases == ["ready", "wheel", "navigation", "transcript", "restore"]
    assert cdp.override_active is False


@pytest.mark.asyncio
async def test_summary_reading_track_reapplies_original_metrics_when_clear_is_sticky() -> None:
    cdp = _SummaryReadingTrackCdp(sticky_clear=True)

    result = await smoke_frontend_browser.exercise_summary_reading_track(cdp, timeout_sec=1)

    assert result["ok"] is True
    assert result["viewportRestored"] is True
    assert result["restoredViewport"] == {
        "viewportWidth": 1280,
        "viewportHeight": 900,
        "devicePixelRatio": 1.0,
    }
    assert [method for method in cdp.calls if method.startswith("Emulation.")] == [
        "Emulation.setDeviceMetricsOverride",
        "Emulation.clearDeviceMetricsOverride",
        "Emulation.setDeviceMetricsOverride",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_phase", ["ready", "wheel", "navigation", "transcript", "restore"])
async def test_summary_reading_track_restores_page_and_viewport_after_failure(fail_phase: str) -> None:
    cdp = _SummaryReadingTrackCdp(fail_phase=fail_phase)

    with pytest.raises(RuntimeError, match=f"synthetic summary {fail_phase} failure"):
        await smoke_frontend_browser.exercise_summary_reading_track(cdp, timeout_sec=1)

    assert [method for method in cdp.calls if method.startswith("Emulation.")] == [
        "Emulation.setDeviceMetricsOverride",
        "Emulation.clearDeviceMetricsOverride",
    ]
    assert cdp.restore_attempted is True
    assert cdp.override_active is False
