from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_WRAPPER_MODULES = (
    "src.assemblyai_async_stt",
    "src.azure_mai_stt",
    "src.cloud_async_stt",
    "src.gladia_stt",
    "src.mistral_stt",
    "src.modulate_stt",
    "src.smallest_stt",
)


def test_pipeline_workers_disable_unused_rtvi_and_turn_tracking() -> None:
    source = (REPO_ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PipelineWorker"
    ]

    assert len(calls) == 2
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        for name in ("enable_rtvi", "enable_turn_tracking"):
            value = keywords.get(name)
            assert isinstance(value, ast.Constant), name
            assert value.value is False, name
    assert source.count("await self.runner.add_workers(self.task)") == 2
    assert "self.runner.run(self.task)" not in source


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
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"keyboard": False, "pyautogui": False}


def test_pipeline_does_not_eagerly_import_provider_wrappers() -> None:
    module_names = json.dumps(PROVIDER_WRAPPER_MODULES)
    probe = (
        "import json, sys; "
        f"module_names = {module_names}; "
        "import src.pipeline; "
        "print(json.dumps({name: name in sys.modules for name in module_names}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {name: False for name in PROVIDER_WRAPPER_MODULES}
