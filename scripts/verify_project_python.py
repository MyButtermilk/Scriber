from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path


_PIPECAT_PIN = re.compile(
    r"^\s*pipecat-ai(?:\[[^\]]+\])?==([^\s;]+)",
    flags=re.IGNORECASE,
)


def _required_pipecat_version(requirements_path: Path) -> str:
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        match = _PIPECAT_PIN.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"Pipecat is not pinned in {requirements_path.name}.")


def main() -> int:
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
