from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_python_verifier_requires_exact_python3147() -> None:
    source = (REPO_ROOT / "scripts" / "verify_project_python.py").read_text(encoding="utf-8")

    assert "_REQUIRED_PYTHON = (3, 14, 7)" in source
    assert '_REQUIRED_CACHE_TAG = "cpython-314"' in source
    assert "actual_python != _REQUIRED_PYTHON" in source
    assert "actual_cache_tag != _REQUIRED_CACHE_TAG" in source


def test_current_python_matches_project_runtime_when_running_python314_gate() -> None:
    if sys.version_info[:3] != (3, 14, 7):
        return

    assert sys.implementation.cache_tag == "cpython-314"
    assert importlib.util.find_spec("pipecat") is not None
