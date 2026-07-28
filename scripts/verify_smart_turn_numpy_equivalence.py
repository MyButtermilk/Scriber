"""Compare SmartTurn results between public NumPy and Scriber's no-BLAS wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

PUBLIC_NUMPY_VERSION = "2.4.6"
SCRIBER_NUMPY_VERSION = "2.4.6+scriber.noblas.1"
PROBABILITY_ABS_TOLERANCE = 1e-7


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_core_sha256(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        members = sorted(
            name
            for name in archive.namelist()
            if name.startswith("numpy/_core/_multiarray_umath.") and name.endswith(".pyd")
        )
        if len(members) != 1:
            raise RuntimeError("NumPy wheel must contain one _multiarray_umath PYD")
        return hashlib.sha256(archive.read(members[0])).hexdigest()


def _build_dependencies() -> dict[str, str]:
    import numpy as np

    dependencies = np.__config__.CONFIG.get("Build Dependencies", {})
    return {name: str((dependencies.get(name) or {}).get("name") or "") for name in ("blas", "lapack")}


def _child_payload() -> dict[str, Any]:
    import numpy as np
    from numpy._core import _multiarray_umath
    from pipecat.audio.turn.smart_turn import _whisper_features
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )

    from src.runtime.smart_turn_mel import install_smart_turn_mel_acceleration

    indices = np.arange(128_000, dtype=np.int64)
    pcm = ((indices * 1_103_515_245 + 12_345) & 0xFFFF) - 32_768
    audio = pcm.astype(np.float32) * np.float32(0.05 / 32_768.0)
    install_smart_turn_mel_acceleration(force=True)
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
    return {
        "pythonVersion": ".".join(str(part) for part in sys.version_info[:3]),
        "pythonCacheTag": sys.implementation.cache_tag,
        "numpyVersion": np.__version__,
        "buildDependencies": _build_dependencies(),
        "numpyCoreSha256": _sha256_file(core_path),
        "features": {
            "dtype": str(features.dtype),
            "shape": list(features.shape),
            "sha256": hashlib.sha256(features.tobytes(order="C")).hexdigest(),
            "minimum": float(features.min()),
            "maximum": float(features.max()),
            "mean": float(features.mean(dtype=np.float64)),
        },
        "probability": probability,
        "prediction": int(prediction["prediction"]),
        "decision": "complete" if int(prediction["prediction"]) == 1 else "incomplete",
    }


def _run_child(python: Path, repo_root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(repo_root),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    completed = subprocess.run(
        [str(python), str(Path(__file__).resolve()), "--child"],
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
    wheel_core_sha256: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if baseline.get("numpyVersion") != PUBLIC_NUMPY_VERSION:
        errors.append("baseline_numpy_version_mismatch")
    if candidate.get("numpyVersion") != SCRIBER_NUMPY_VERSION:
        errors.append("candidate_numpy_version_mismatch")
    if (candidate.get("buildDependencies") or {}).get("blas") != "none":
        errors.append("candidate_blas_not_disabled")
    if (candidate.get("buildDependencies") or {}).get("lapack") != "none":
        errors.append("candidate_lapack_not_disabled")
    if candidate.get("numpyCoreSha256") != wheel_core_sha256:
        errors.append("candidate_numpy_core_wheel_mismatch")
    if baseline.get("pythonVersion") != candidate.get("pythonVersion"):
        errors.append("python_version_mismatch")
    if baseline.get("pythonCacheTag") != candidate.get("pythonCacheTag"):
        errors.append("python_cache_tag_mismatch")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.child:
        print(json.dumps(_child_payload(), sort_keys=True))
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
    baseline = _run_child(args.baseline_python.resolve(), repo_root)
    candidate = _run_child(args.candidate_python.resolve(), repo_root)
    wheel_core_sha256 = _wheel_core_sha256(wheel)
    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_core_sha256=wheel_core_sha256,
    )
    payload = {
        "schemaVersion": 1,
        "contract": "ScriberSmartTurnNumPyEquivalenceV1",
        "ok": ok,
        "errors": errors,
        "numpyWheel": {
            "name": wheel.name,
            "length": wheel.stat().st_size,
            "sha256": _sha256_file(wheel),
            "coreSha256": wheel_core_sha256,
        },
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
