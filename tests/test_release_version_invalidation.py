"""A version bump may restage application code, never rebuild unchanged tools.

Exercise the real cache-key writers and version synchronizer in an isolated Git
fixture. No tracked source or output in the active checkout is modified, even
when the rest of the suite runs with xdist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci import prepare_python_quality_environment as quality

ROOT = Path(__file__).resolve().parents[1]
KEY_NAMES = {
    "frontend-dependencies.txt",
    "rust-dependencies.txt",
    "rust-release.txt",
    "tauri-app-binary.txt",
    "rust-audio-sidecar.txt",
    "rust-diarization-sidecar.txt",
    "sherpa-onnx-archive.txt",
    "python-dependencies.txt",
    "backend-runtime.txt",
    "backend-sidecar.txt",
}
VERSIONED_APPLICATION_PRODUCTS = {"tauri-app-binary.txt", "backend-sidecar.txt"}


@pytest.fixture
def isolated_release_source(tmp_path: Path) -> Path:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release cache-key execution")
    # Copy only tracked source/tool inputs, including the locked 6 MB product
    # wheel. No model download, node_modules, venv, target or build tree enters
    # this fixture. Git's index is needed by the backend application enumerator.
    selected = (
        subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                ".node-version",
                "THIRD_PARTY_NOTICES.md",
                "requirements-*.txt",
                ".github/actions",
                ".github/workflows",
                "Frontend",
                "backend_runtime",
                "native",
                "packaging",
                "pyloudnorm",
                "scripts",
                "src",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    # Keep the regression runnable before newly added worker/tool sources are
    # staged. These exact leaves never traverse generated native target trees.
    selected.extend(
        [
            "native/scriber-audio-sidecar/Cargo.toml",
            "native/scriber-audio-sidecar/Cargo.lock",
            "native/scriber-audio-sidecar/build.rs",
            "native/scriber-audio-sidecar/windows-app-manifest.xml",
            "scripts/ci/prepare_python_quality_environment.py",
        ]
    )
    for relative in sorted(set(filter(None, selected))):
        source = ROOT / relative
        assert source.is_file() and not source.is_symlink(), relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "add", "--", "src"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _run_keys(root: Path, commit: str) -> dict[str, bytes]:
    for script, arguments in (
        ("write_release_cache_keys.ps1", []),
        (
            "finalize_release_cache_keys.ps1",
            [
                "-SourceCommit",
                commit,
                "-UpdaterPublicKey",
                "fixed-test-public-key",
                "-OutlookClientId",
                "fixed-test-client",
            ],
        ),
    ):
        subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(root / "scripts/ci" / script), *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env={key: value for key, value in os.environ.items() if key != "GITHUB_OUTPUT"},
        )
    return {path.name: path.read_bytes() for path in (root / "build/cache-keys").glob("*.txt")}


def _version_bump(root: Path) -> None:
    version = root / "src/version.py"
    source = version.read_text(encoding="utf-8")
    source, count = re.subn(r'^__version__\s*=\s*"[^"]+"', '__version__ = "99.88.77"', source, flags=re.MULTILINE)
    assert count == 1
    version.write_text(source, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GITHUB_REF_TYPE", "GITHUB_REF_NAME", "GITHUB_OUTPUT")
    }
    subprocess.run(
        [sys.executable, str(root / "scripts/sync_version.py")],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert json.loads((root / "Frontend/package.json").read_text(encoding="utf-8"))["version"] == "99.88.77"


def _cache_hash_inputs(root: Path) -> dict[str, tuple[tuple[str, str], ...]]:
    """Read every explicit Actions cache's actual hashFiles input closure."""
    result = {}
    for workflow_path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("actions/cache/"):
                    continue
                key = step.get("with", {}).get("key", "")
                paths = set()
                for expression in re.findall(r"hashFiles\(([^)]+)\)", key):
                    for pattern in re.findall(r"'([^']+)'", expression):
                        paths.update(path for path in root.glob(pattern) if path.is_file())
                result[f"{workflow_path.name}:{job_name}:{step['name']}"] = tuple(
                    (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in sorted(paths)
                )
    return result


def test_version_bump_only_invalidates_application_products_and_keeps_all_dependency_families(
    isolated_release_source: Path,
) -> None:
    root = isolated_release_source
    before = _run_keys(root, "1" * 40)
    before_quality = quality.cache_key(quality.environment_identity(root))
    before_actions = _cache_hash_inputs(root)
    _version_bump(root)
    after = _run_keys(root, "2" * 40)
    after_quality = quality.cache_key(quality.environment_identity(root))
    after_actions = _cache_hash_inputs(root)

    assert before.keys() == after.keys() == KEY_NAMES
    changed = {name for name in KEY_NAMES if before[name] != after[name]}
    assert changed == VERSIONED_APPLICATION_PRODUCTS
    assert before_quality == after_quality
    assert before_actions.keys() == after_actions.keys()
    for step, before_inputs in before_actions.items():
        paths = {path for path, _digest in before_inputs}
        if paths & {f"build/cache-keys/{name}" for name in VERSIONED_APPLICATION_PRODUCTS}:
            assert before_inputs != after_actions[step], step
        else:
            assert before_inputs == after_actions[step], step

    # A version bump stays visible to packaging and the running backend. The
    # static heavy dependency/runtime keys above remain reusable.
    assert b"file\tsrc/version.py\t" in after["backend-sidecar.txt"]
    assert b"release-runtime\tsource-commit\t" + b"2" * 40 in after["tauri-app-binary.txt"]


@pytest.mark.parametrize(
    "changed_path",
    [
        "native/scriber-audio-sidecar/Cargo.toml",
        "native/scriber-audio-sidecar/build.rs",
        "Frontend/src-tauri/src/audio_sidecar.rs",
    ],
)
def test_audio_finished_product_invalidates_when_its_own_inputs_change(
    isolated_release_source: Path,
    changed_path: str,
) -> None:
    root = isolated_release_source
    before = _run_keys(root, "1" * 40)
    changed = root / changed_path
    source = changed.read_text(encoding="utf-8")
    if changed.suffix == ".toml":
        source, count = re.subn(r'^version\s*=\s*"[^"]+"', 'version = "0.2.0"', source, count=1, flags=re.MULTILINE)
        assert count == 1
    else:
        source += "\n// Regression: changed worker build/source input.\n"
    changed.write_text(source, encoding="utf-8")
    after = _run_keys(root, "1" * 40)
    assert before["rust-audio-sidecar.txt"] != after["rust-audio-sidecar.txt"]
