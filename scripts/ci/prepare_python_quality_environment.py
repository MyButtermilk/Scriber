"""Cache only the exact, validated Windows quality-test venv.

The packaged Python runtime has a separate identity and is never used here.
GitHub only saves this cache on a main push. The exact key binds the dependency
closure, setup code, runner image, interpreter bytes, and absolute venv location;
there are no prefix restores. A mismatched restore is rebuilt before any test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "requirements-base.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-test-constraints.txt",
    "requirements-release-constraints.txt",
    ".github/actions/setup-python-quality/action.yml",
    "scripts/ci/prepare_python_quality_environment.py",
)
STAMP = ".scriber-quality-environment.json"
INVENTORY_CODE = (
    "import importlib.metadata,json,sys; "
    "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
    "'version':sys.version,'packages':[(d.metadata['Name'],d.version) "
    "for d in importlib.metadata.distributions()]}))"
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def environment_identity(root: Path) -> dict[str, Any]:
    interpreter = Path(sys.executable).resolve()
    return {
        "schema": 1,
        "interpreter": str(interpreter),
        "interpreterSha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "pythonVersion": sys.version,
        "basePrefix": str(Path(sys.base_prefix).resolve()),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "imageOS": os.environ.get("IMAGEOS", ""),
        "imageVersion": os.environ.get("IMAGEVERSION", ""),
        "venv": str(root.resolve() / "venv"),
        "inputs": {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest() for relative in INPUTS},
    }


def cache_key(identity: dict[str, Any]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "scriber-python-quality-venv-v1-" + hashlib.sha256(encoded).hexdigest()


def pinned_packages(root: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (root / "requirements-test-constraints.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+-]+)", line)
        if match is None or _canonical(match[1]) in pins:
            raise ValueError("Test constraints must contain unique exact package pins")
        pins[_canonical(match[1])] = match[2]
    if not pins:
        raise ValueError("Test constraints are empty")
    return pins


def validate_inventory(inventory: dict[str, Any], identity: dict[str, Any], pins: dict[str, str]) -> None:
    if (
        Path(inventory["prefix"]).resolve() != Path(identity["venv"])
        or Path(inventory["base_prefix"]).resolve() != Path(identity["basePrefix"])
        or inventory["version"] != identity["pythonVersion"]
    ):
        raise ValueError("Cached interpreter identity differs")
    installed: dict[str, str] = {}
    for name, version in inventory["packages"]:
        canonical = _canonical(name)
        if canonical in installed:
            raise ValueError(f"Duplicate installed distribution: {canonical}")
        installed[canonical] = version
    if installed != pins:
        missing = sorted(pins.keys() - installed.keys())
        extra = sorted(installed.keys() - pins.keys())
        changed = sorted(name for name in installed.keys() & pins.keys() if installed[name] != pins[name])
        raise ValueError(f"Installed package closure differs: missing={missing}, extra={extra}, changed={changed}")


def _run(command: list[str], root: Path, *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, check=True, text=True, capture_output=capture, timeout=900)


def validate_environment(root: Path, identity: dict[str, Any]) -> None:
    environment = root / "venv"
    if json.loads((environment / STAMP).read_text(encoding="utf-8")) != identity:
        raise ValueError("Cached environment stamp differs")
    executable = environment / "Scripts" / "python.exe"
    inventory = json.loads(_run([str(executable), "-I", "-c", INVENTORY_CODE], root, capture=True).stdout)
    validate_inventory(inventory, identity, pinned_packages(root))
    _run([str(executable), "-I", "-m", "pip", "check"], root)


def _remove_environment(root: Path) -> None:
    """Never delete a cache path outside this checkout or follow a junction."""
    root = root.resolve()
    environment = root / "venv"
    if environment.is_symlink() or environment.is_junction() or environment.resolve() != root / "venv":
        raise ValueError("Refusing to remove a redirected test environment")
    if environment.exists():
        shutil.rmtree(environment)


def prepare_environment(root: Path, identity: dict[str, Any]) -> str:
    try:
        validate_environment(root, identity)
        return "validated-cache"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Test environment requires a cold install: {error}", flush=True)

    _remove_environment(root)
    _run([sys.executable, "-m", "venv", "venv"], root)
    executable = str(root / "venv" / "Scripts" / "python.exe")
    _run([executable, "-m", "pip", "install", "pip==26.1.2"], root)
    _run(
        [
            executable,
            "-m",
            "pip",
            "install",
            "-c",
            "requirements-test-constraints.txt",
            "-c",
            "requirements-release-constraints.txt",
            "-r",
            "requirements-base.txt",
            "-r",
            "requirements-test.txt",
        ],
        root,
    )
    (root / "venv" / STAMP).write_text(json.dumps(identity, sort_keys=True), encoding="utf-8")
    validate_environment(root, identity)
    return "cold-install"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("identity", "prepare"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32" or sys.prefix != sys.base_prefix:
        raise SystemExit("Run quality environment setup with the Windows setup-python base interpreter")
    identity = environment_identity(ROOT)
    if not identity["imageOS"] or not identity["imageVersion"]:
        raise SystemExit("Hosted-runner image identity is required for quality environment reuse")
    if args.operation == "identity":
        key = cache_key(identity)
        print(key)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"cache-key={key}\n")
        return
    started = time.monotonic()
    result = prepare_environment(ROOT, identity)
    summary = f"Python quality environment: **{result}**, validation/setup {time.monotonic() - started:.1f}s.\n"
    print(summary)
    if args.github_summary:
        with args.github_summary.open("a", encoding="utf-8") as output:
            output.write(summary)


if __name__ == "__main__":
    main()
