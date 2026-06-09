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
    assert payload["summary"]["routeCount"] == 7
    assert payload["summary"]["criticalConsoleErrorCount"] == 0
    assert payload["summary"]["interactionCheckCount"] == 5
    assert set(payload["summary"]["interactionChecks"]) == {
        "youtube-thumbnails",
        "file-drag-drop",
        "debug-clear",
        "transcript-processing-refresh",
        "token-required-browser-state",
    }
    assert "/settings" in payload["summary"]["routes"]
    assert "/debug" in payload["summary"]["routes"]
    assert "/transcript/youtube-processing-smoke" in payload["summary"]["routes"]
    assert set(payload["summary"]["virtualizedHistoryRoutes"]) == {"/", "/youtube", "/file"}
    debug = next(item for item in payload["scenarios"] if item["route"] == "/debug")
    assert "Copy visible" in debug["expectedText"]
    assert "Support bundle" in debug["expectedText"]
    assert debug["interactionChecks"] == [{"name": "debug-clear", "ok": True}]
    assert payload["tokenRequiredCheck"]["name"] == "token-required-browser-state"


def test_hybrid_goal_frontend_smoke_is_documented() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts\\smoke_frontend_browser.py" in agents
    assert "scripts\\smoke_frontend_browser.py" in readme
