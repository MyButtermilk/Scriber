from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

_PIPECAT_PIN = re.compile(
    r"^\s*pipecat-ai(?:\[[^\]]+\])?==([^\s;]+)",
    flags=re.IGNORECASE,
)
_REQUIRED_PYTHON = (3, 14, 7)
_REQUIRED_CACHE_TAG = "cpython-314"


def _required_pipecat_version(requirements_path: Path) -> str:
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        match = _PIPECAT_PIN.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"Pipecat is not pinned in {requirements_path.name}.")


def main() -> int:
    actual_python = sys.version_info[:3]
    actual_cache_tag = sys.implementation.cache_tag
    if actual_python != _REQUIRED_PYTHON or actual_cache_tag != _REQUIRED_CACHE_TAG:
        print(
            "Scriber project Python is out of date: "
            "CPython 3.14.7 (cpython-314) is required, but "
            f"{sys.version.split()[0]} ({actual_cache_tag}) was found. "
            "Recreate venv with `py -3.14 -m venv venv` before running "
            "Scriber commands.",
            file=sys.stderr,
        )
        return 5

    repo_root = Path(__file__).resolve().parents[1]
    requirements_path = repo_root / "requirements-base.txt"
    expected = _required_pipecat_version(requirements_path)
    try:
        actual = importlib.metadata.version("pipecat-ai")
    except importlib.metadata.PackageNotFoundError:
        actual = "not installed"

    if actual != expected:
        print(
            "Scriber project Python is out of date: "
            f"pipecat-ai {expected} is required, but {actual} was found. "
            "Refresh venv from requirements.txt before running Scriber commands.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
