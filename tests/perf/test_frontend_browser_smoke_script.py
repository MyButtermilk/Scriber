from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import smoke_frontend_browser

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fast_tab_switch", [False, True])
def test_frontend_browser_smoke_validate_only_writes_requested_plan(tmp_path: Path, fast_tab_switch: bool) -> None:
    output_path = tmp_path / "nested" / "frontend-browser-smoke.json"
    command = [
        sys.executable,
        "scripts/smoke_frontend_browser.py",
        "--validate-only",
        "--routes",
        "/,/settings",
        "--output",
        str(output_path),
    ]
    if fast_tab_switch:
        command.append("--fast-tab-switch")

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert json.loads(result.stdout) == payload
    assert payload["ok"] is True
    assert payload["summary"]["validateOnly"] is True
    assert payload["summary"]["routes"] == ["/", "/settings"]
    assert [item["route"] for item in payload["scenarios"]] == ["/", "/settings"]
    assert payload["summary"]["routeCount"] == len(payload["scenarios"])
    assert payload["summary"]["interactionCheckCount"] == len(payload["summary"]["interactionChecks"])
    assert ("fast-tab-switch" in payload["summary"]["interactionChecks"]) is fast_tab_switch
    if fast_tab_switch:
        assert payload["fastTabSwitchCheck"]["validateOnly"] is True
    else:
        assert payload["fastTabSwitchCheck"] is None


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
