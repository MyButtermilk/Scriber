"""Compare SmartTurn results between public NumPy and Scriber's no-BLAS wheel."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import importlib.resources
import io
import json
import math
import os
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

PUBLIC_NUMPY_VERSION = "2.4.6"
SCRIBER_NUMPY_VERSION = "2.4.6+scriber.noblas.1"
PROBABILITY_ABS_TOLERANCE = 1e-7
EXPECTED_PACKAGE_VERSIONS = {
    "onnxruntime": "1.24.4",
    "pipecat-ai": "1.5.0",
}
NUMPY_WHEEL_LOCK_PATH = Path("packaging/wheels/numpy-noblas-wheel-lock-v1.json")
RELEASE_CONSTRAINTS_PATH = Path("requirements-release-constraints.txt")
TEST_CONSTRAINTS_PATH = Path("requirements-test-constraints.txt")
MEL_MODEL_PATH = Path("src/runtime/smart-turn-mel-matmul.onnx")
SMART_TURN_MODEL_NAME = "smart-turn-v3.2-cpu.onnx"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    identity: dict[str, Any] = {
        "length": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
    }
    if relative_to is not None:
        identity["relativePath"] = resolved.relative_to(relative_to.resolve()).as_posix()
    else:
        identity["name"] = resolved.name
    return identity


def _record_digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _parse_record(data: bytes, *, label: str) -> dict[str, tuple[str, str]]:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RuntimeError(f"{label} RECORD is not valid UTF-8 CSV") from exc
    parsed: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            raise RuntimeError(f"{label} RECORD row must have exactly three fields")
        name, digest, size = row
        normalized = PurePosixPath(name).as_posix()
        if not name or normalized != name.replace("\\", "/") or name in parsed:
            raise RuntimeError(f"{label} RECORD contains an invalid or duplicate path")
        parsed[name] = (digest, size)
    return parsed


def _safe_wheel_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\x00" in name:
        raise RuntimeError("NumPy wheel contains an unsafe archive path")
    return path


def _wheel_identity(wheel: Path) -> dict[str, Any]:
    wheel = wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise RuntimeError("NumPy wheel contains duplicate archive paths")
        for name in names:
            _safe_wheel_member(name)
        core_members = sorted(
            name for name in names if name.startswith("numpy/_core/_multiarray_umath.") and name.endswith(".pyd")
        )
        if len(core_members) != 1:
            raise RuntimeError("NumPy wheel must contain one _multiarray_umath PYD")
        record_members = sorted(name for name in names if name.endswith(".dist-info/RECORD"))
        if len(record_members) != 1:
            raise RuntimeError("NumPy wheel must contain one dist-info RECORD")
        record_name = record_members[0]
        record_bytes = archive.read(record_name)
        record = _parse_record(record_bytes, label="wheel")
        if set(record) != set(names):
            raise RuntimeError("NumPy wheel RECORD does not enumerate the exact archive")
        for entry in entries:
            digest, size = record[entry.filename]
            if entry.filename == record_name:
                if digest or size:
                    raise RuntimeError("NumPy wheel RECORD must leave its own digest and size empty")
                continue
            data = archive.read(entry)
            if digest != _record_digest(data) or size != str(len(data)):
                raise RuntimeError(f"NumPy wheel RECORD identity mismatch for {entry.filename}")
        core_sha256 = hashlib.sha256(archive.read(core_members[0])).hexdigest()
    return {
        "name": wheel.name,
        "length": wheel.stat().st_size,
        "sha256": _sha256_file(wheel),
        "coreSha256": core_sha256,
        "recordSha256": hashlib.sha256(record_bytes).hexdigest(),
        "entryCount": len(names),
        "recordPath": record_name,
    }


def _allowed_installer_record_extra(name: str, *, dist_info_name: str) -> bool:
    if name in {
        "../../Scripts/f2py.exe",
        "../../Scripts/numpy-config.exe",
        f"{dist_info_name}/INSTALLER",
        f"{dist_info_name}/REQUESTED",
        f"{dist_info_name}/direct_url.json",
    }:
        return True
    path = PurePosixPath(name)
    return (
        name.startswith("numpy/") and "__pycache__" in path.parts and path.suffix == ".pyc" and ".." not in path.parts
    )


def _verify_recorded_file(
    *,
    site_root: Path,
    name: str,
    digest: str,
    size: str,
    allow_parent_scripts: bool,
) -> None:
    path = PurePosixPath(name)
    if ".." in path.parts:
        if not allow_parent_scripts or name not in {
            "../../Scripts/f2py.exe",
            "../../Scripts/numpy-config.exe",
        }:
            raise RuntimeError(f"Installed NumPy RECORD contains an unsafe path: {name}")
    elif path.is_absolute() or "\\" in name or ":" in name or "\x00" in name:
        raise RuntimeError(f"Installed NumPy RECORD contains an unsafe path: {name}")
    target = site_root.joinpath(*path.parts).resolve()
    if not target.is_file():
        raise RuntimeError(f"Installed NumPy RECORD target is missing: {name}")
    data = target.read_bytes()
    if digest and digest != _record_digest(data):
        raise RuntimeError(f"Installed NumPy RECORD digest mismatch: {name}")
    if size and size != str(len(data)):
        raise RuntimeError(f"Installed NumPy RECORD size mismatch: {name}")


def _verify_installed_distribution_matches_wheel(
    wheel: Path,
    *,
    site_root: Path,
    dist_info_name: str,
) -> dict[str, Any]:
    wheel = wheel.resolve()
    site_root = site_root.resolve()
    wheel_identity = _wheel_identity(wheel)
    dist_info = site_root / dist_info_name
    installed_record_path = dist_info / "RECORD"
    if not installed_record_path.is_file():
        raise RuntimeError("Installed NumPy distribution has no RECORD")
    installed_record_bytes = installed_record_path.read_bytes()
    installed_record = _parse_record(installed_record_bytes, label="installed")

    with zipfile.ZipFile(wheel) as archive:
        wheel_record = _parse_record(archive.read(wheel_identity["recordPath"]), label="wheel")
        for name, (digest, size) in wheel_record.items():
            if name == wheel_identity["recordPath"]:
                continue
            if installed_record.get(name) != (digest, size):
                raise RuntimeError(f"Installed NumPy RECORD differs from the wheel: {name}")
            target = site_root.joinpath(*PurePosixPath(name).parts).resolve()
            if not target.is_file() or target.read_bytes() != archive.read(name):
                raise RuntimeError(f"Installed NumPy file differs from the wheel: {name}")

    extras = sorted(set(installed_record) - set(wheel_record))
    unexpected_extras = [
        name for name in extras if not _allowed_installer_record_extra(name, dist_info_name=dist_info_name)
    ]
    if unexpected_extras:
        raise RuntimeError(f"Installed NumPy RECORD has unexpected entries: {unexpected_extras[:3]}")
    for name in extras:
        digest, size = installed_record[name]
        _verify_recorded_file(
            site_root=site_root,
            name=name,
            digest=digest,
            size=size,
            allow_parent_scripts=True,
        )

    recorded_paths = set(installed_record)
    for root in (site_root / "numpy", dist_info):
        if not root.is_dir():
            raise RuntimeError(f"Installed NumPy tree is missing: {root.name}")
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(site_root).as_posix()
            if relative not in recorded_paths and not ("__pycache__" in path.parts and path.suffix == ".pyc"):
                raise RuntimeError(f"Installed NumPy tree has an unrecorded file: {relative}")
    if (site_root / "numpy.libs").exists():
        raise RuntimeError("Installed no-BLAS NumPy unexpectedly contains numpy.libs")
    other_dist_info = sorted(path.name for path in site_root.glob("numpy-*.dist-info") if path.name != dist_info_name)
    if other_dist_info:
        raise RuntimeError(f"Installed NumPy has conflicting metadata directories: {other_dist_info}")

    return {
        **wheel_identity,
        "installedRecordSha256": hashlib.sha256(installed_record_bytes).hexdigest(),
        "installedRecordEntryCount": len(installed_record),
        "wheelEntriesVerified": len(wheel_record) - 1,
        "installerRecordExtraCount": len(extras),
        "verified": True,
    }


def _build_dependencies() -> dict[str, str]:
    import numpy as np

    dependencies = np.__config__.CONFIG.get("Build Dependencies", {})
    return {name: str((dependencies.get(name) or {}).get("name") or "") for name in ("blas", "lapack")}


def _git_output(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {completed.stderr[-500:]}")
    return completed.stdout.strip()


def _source_provenance(repo_root: Path, *, harness_path: Path | None = None) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    commit = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    dirty = _git_output(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise RuntimeError("SmartTurn equivalence evidence requires a clean Git worktree")
    inputs = {
        "harness": (harness_path or Path(__file__).resolve()),
        "numpyWheelLock": repo_root / NUMPY_WHEEL_LOCK_PATH,
        "releaseConstraints": repo_root / RELEASE_CONSTRAINTS_PATH,
        "testConstraints": repo_root / TEST_CONSTRAINTS_PATH,
        "melProjectionModel": repo_root / MEL_MODEL_PATH,
    }
    missing = [label for label, path in inputs.items() if not path.is_file()]
    if missing:
        raise RuntimeError("SmartTurn provenance inputs are missing: " + ", ".join(missing))
    return {
        "gitCommit": commit,
        "gitTreeClean": True,
        "inputs": {label: _file_identity(path, relative_to=repo_root) for label, path in inputs.items()},
    }


def _validate_locked_wheel(repo_root: Path, wheel_identity: Mapping[str, Any]) -> None:
    lock_path = repo_root.resolve() / NUMPY_WHEEL_LOCK_PATH
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("contract") != "ScriberNumPyNoBlasWheelLockV1":
        raise RuntimeError("Unexpected NumPy no-BLAS wheel lock contract")
    artifact = lock.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("NumPy no-BLAS wheel lock has no artifact")
    expected = {
        "name": artifact.get("fileName"),
        "length": artifact.get("length"),
        "sha256": artifact.get("sha256"),
    }
    actual = {name: wheel_identity.get(name) for name in expected}
    if actual != expected:
        raise RuntimeError("NumPy wheel does not match the tracked no-BLAS lock")


def _smart_turn_model_identity() -> dict[str, Any]:
    model = importlib.resources.files("pipecat.audio.turn.smart_turn.data").joinpath(SMART_TURN_MODEL_NAME)
    with importlib.resources.as_file(model) as model_path:
        return _file_identity(model_path)


def _child_payload(*, role: str, numpy_wheel: Path | None = None) -> dict[str, Any]:
    import numpy as np
    from numpy._core import _multiarray_umath
    from pipecat.audio.turn.smart_turn import _whisper_features
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )

    from src.runtime import smart_turn_mel

    if role not in {"baseline", "candidate"}:
        raise RuntimeError(f"Unknown SmartTurn child role: {role}")
    indices = np.arange(128_000, dtype=np.int64)
    pcm = (((indices * 1_103_515_245 + 12_345) & 0xFFFF) - 32_768).astype("<i2")
    audio = np.ascontiguousarray(pcm.astype(np.float32) * np.float32(0.05 / 32_768.0))
    mel_filters = np.ascontiguousarray(
        np.asarray(_whisper_features._MEL_FILTERS, dtype=np.float64).T,
    )
    mel_acceleration_installed = smart_turn_mel.install_smart_turn_mel_acceleration()
    features = _whisper_features.compute_whisper_log_mel_features(
        audio,
        do_normalize=True,
    )
    analyzer = LocalSmartTurnAnalyzerV3(cpu_count=1)
    prediction = analyzer._predict_endpoint(audio)
    probability = float(prediction["probability"])
    if not math.isfinite(probability):
        raise RuntimeError("SmartTurn probability is not finite")
    core_path = Path(_multiarray_umath.__file__).resolve()
    distribution = importlib.metadata.distribution("numpy")
    record_files = [
        file for file in (distribution.files or ()) if file.name == "RECORD" and ".dist-info" in file.as_posix()
    ]
    if len(record_files) != 1:
        raise RuntimeError("Installed NumPy distribution must expose one RECORD")
    installed_record_path = Path(distribution.locate_file(record_files[0])).resolve()
    wheel_installation = None
    if role == "candidate":
        if numpy_wheel is None:
            raise RuntimeError("Candidate SmartTurn child requires the NumPy wheel")
        wheel_installation = _verify_installed_distribution_matches_wheel(
            numpy_wheel,
            site_root=installed_record_path.parent.parent,
            dist_info_name=installed_record_path.parent.name,
        )
    return {
        "role": role,
        "pythonVersion": ".".join(str(part) for part in sys.version_info[:3]),
        "pythonCacheTag": sys.implementation.cache_tag,
        "numpyVersion": np.__version__,
        "buildDependencies": _build_dependencies(),
        "numpyCoreSha256": _sha256_file(core_path),
        "packageVersions": {name: importlib.metadata.version(name) for name in EXPECTED_PACKAGE_VERSIONS},
        "fixture": {
            "contract": "ScriberSmartTurnSyntheticPcmV1",
            "sampleRateHz": 16_000,
            "sampleCount": int(audio.size),
            "pcmS16leSha256": hashlib.sha256(pcm.tobytes(order="C")).hexdigest(),
            "audioFloat32Sha256": hashlib.sha256(audio.tobytes(order="C")).hexdigest(),
        },
        "models": {
            "pipecatSmartTurn": _smart_turn_model_identity(),
            "melProjection": _file_identity(Path(smart_turn_mel.__file__).with_name(smart_turn_mel._MODEL_NAME)),
            "melFiltersSha256": hashlib.sha256(mel_filters.tobytes(order="C")).hexdigest(),
        },
        "melAccelerationInstalled": mel_acceleration_installed,
        "numpyWheelInstallation": wheel_installation,
        "features": {
            "dtype": str(features.dtype),
            "shape": list(features.shape),
            "valueCount": int(features.size),
            "sha256": hashlib.sha256(features.tobytes(order="C")).hexdigest(),
            "minimum": float(features.min()),
            "maximum": float(features.max()),
            "mean": float(features.mean(dtype=np.float64)),
        },
        "probability": probability,
        "prediction": int(prediction["prediction"]),
        "decision": "complete" if int(prediction["prediction"]) == 1 else "incomplete",
    }


def _run_child(
    python: Path,
    repo_root: Path,
    *,
    role: str,
    numpy_wheel: Path | None = None,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(repo_root),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PIPECAT_SMART_TURN_LOG_DATA": "0",
        }
    )
    command = [
        str(python),
        str(Path(__file__).resolve()),
        "--child",
        "--child-role",
        role,
    ]
    if numpy_wheel is not None:
        command.extend(["--numpy-wheel", str(numpy_wheel)])
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"SmartTurn child failed with exit {completed.returncode}: {completed.stderr[-1000:]}")
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("SmartTurn child did not emit JSON")


def compare_payloads(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    wheel_identity: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if baseline.get("role") != "baseline":
        errors.append("baseline_role_mismatch")
    if candidate.get("role") != "candidate":
        errors.append("candidate_role_mismatch")
    if baseline.get("numpyVersion") != PUBLIC_NUMPY_VERSION:
        errors.append("baseline_numpy_version_mismatch")
    if candidate.get("numpyVersion") != SCRIBER_NUMPY_VERSION:
        errors.append("candidate_numpy_version_mismatch")
    if (candidate.get("buildDependencies") or {}).get("blas") != "none":
        errors.append("candidate_blas_not_disabled")
    if (candidate.get("buildDependencies") or {}).get("lapack") != "none":
        errors.append("candidate_lapack_not_disabled")
    if candidate.get("numpyCoreSha256") != wheel_identity.get("coreSha256"):
        errors.append("candidate_numpy_core_wheel_mismatch")
    expected_wheel_installation = {
        key: wheel_identity.get(key)
        for key in (
            "name",
            "length",
            "sha256",
            "coreSha256",
            "recordSha256",
            "entryCount",
            "recordPath",
        )
    }
    installation = candidate.get("numpyWheelInstallation")
    if not (
        isinstance(installation, dict)
        and installation.get("verified") is True
        and all(installation.get(key) == value for key, value in expected_wheel_installation.items())
        and installation.get("wheelEntriesVerified") == int(wheel_identity.get("entryCount") or 0) - 1
    ):
        errors.append("candidate_numpy_wheel_installation_mismatch")
    if baseline.get("pythonVersion") != candidate.get("pythonVersion"):
        errors.append("python_version_mismatch")
    if baseline.get("pythonCacheTag") != candidate.get("pythonCacheTag"):
        errors.append("python_cache_tag_mismatch")
    if baseline.get("packageVersions") != EXPECTED_PACKAGE_VERSIONS:
        errors.append("baseline_package_versions_mismatch")
    if candidate.get("packageVersions") != EXPECTED_PACKAGE_VERSIONS:
        errors.append("candidate_package_versions_mismatch")
    if baseline.get("fixture") != candidate.get("fixture"):
        errors.append("smart_turn_fixture_mismatch")
    if baseline.get("models") != candidate.get("models"):
        errors.append("smart_turn_model_identity_mismatch")
    if baseline.get("melAccelerationInstalled") is not False:
        errors.append("baseline_mel_acceleration_unexpected")
    if candidate.get("melAccelerationInstalled") is not True:
        errors.append("candidate_mel_acceleration_not_installed")
    if baseline.get("features") != candidate.get("features"):
        errors.append("smart_turn_features_mismatch")
    baseline_probability = baseline.get("probability")
    candidate_probability = candidate.get("probability")
    if not (
        isinstance(baseline_probability, (int, float))
        and not isinstance(baseline_probability, bool)
        and isinstance(candidate_probability, (int, float))
        and not isinstance(candidate_probability, bool)
        and math.isfinite(float(baseline_probability))
        and math.isfinite(float(candidate_probability))
        and abs(float(baseline_probability) - float(candidate_probability)) <= PROBABILITY_ABS_TOLERANCE
    ):
        errors.append("smart_turn_probability_mismatch")
    if baseline.get("prediction") != candidate.get("prediction"):
        errors.append("smart_turn_prediction_mismatch")
    if baseline.get("decision") != candidate.get("decision"):
        errors.append("smart_turn_decision_mismatch")
    return not errors, errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-python", type=Path)
    parser.add_argument("--candidate-python", type=Path)
    parser.add_argument("--numpy-wheel", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--child-role", choices=("baseline", "candidate"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.child:
        if args.child_role is None:
            raise SystemExit("--child-role is required with --child")
        print(
            json.dumps(
                _child_payload(
                    role=args.child_role,
                    numpy_wheel=args.numpy_wheel.resolve() if args.numpy_wheel is not None else None,
                ),
                sort_keys=True,
            )
        )
        return 0
    required = {
        "--baseline-python": args.baseline_python,
        "--candidate-python": args.candidate_python,
        "--numpy-wheel": args.numpy_wheel,
        "--output": args.output,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit("missing required arguments: " + ", ".join(missing))
    repo_root = args.repo_root.resolve()
    wheel = args.numpy_wheel.resolve()
    provenance = _source_provenance(repo_root)
    wheel_identity = _wheel_identity(wheel)
    _validate_locked_wheel(repo_root, wheel_identity)
    baseline = _run_child(
        args.baseline_python.resolve(),
        repo_root,
        role="baseline",
    )
    candidate = _run_child(
        args.candidate_python.resolve(),
        repo_root,
        role="candidate",
        numpy_wheel=wheel,
    )
    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_identity=wheel_identity,
    )
    final_provenance = _source_provenance(repo_root)
    if final_provenance != provenance:
        raise RuntimeError("SmartTurn provenance changed while equivalence was measured")
    payload = {
        "schemaVersion": 2,
        "contract": "ScriberSmartTurnNumPyEquivalenceV2",
        "ok": ok,
        "errors": errors,
        "provenance": provenance,
        "numpyWheel": wheel_identity,
        "probabilityAbsTolerance": PROBABILITY_ABS_TOLERANCE,
        "baseline": baseline,
        "candidate": candidate,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    print(json.dumps({"ok": ok, "output": str(output), "errors": errors}))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
