from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_tasks_disable_unused_rtvi_and_turn_tracking() -> None:
    source = (REPO_ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PipelineTask"
    ]

    assert len(calls) == 2
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        for name in ("enable_rtvi", "enable_turn_tracking"):
            value = keywords.get(name)
            assert isinstance(value, ast.Constant), name
            assert value.value is False, name


def test_injector_does_not_import_legacy_gui_fallbacks_at_module_load() -> None:
    probe = (
        "import json, sys; "
        "import src.injector; "
        "print(json.dumps({"
        "'keyboard': 'keyboard' in sys.modules, "
        "'pyautogui': 'pyautogui' in sys.modules"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"keyboard": False, "pyautogui": False}
