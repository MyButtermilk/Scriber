from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVIDENCE_CONTRACT = "ScriberFrozenBackendImportStartupAMDV2"
POLICY_NAME = "scriber-python-runtime-policy"
POLICY_FILE = "python-runtime-manifest.json"
RUNTIME_MANIFEST_FILE = "runtime-layer-manifest.json"
APP_MANIFEST = Path("app") / "app-layer-manifest.json"
VARIANT_POLICY: dict[str, tuple[str, bool, bool]] = {
    "O0": ("Official", False, False),
    "O1": ("Official", True, False),
    "C0": ("ClangPgo", False, False),
    "C1": ("ClangPgo", True, False),
    "T0": ("ClangPgoTail", False, True),
    "T1": ("ClangPgoTail", True, True),
}
ALLOWED_VARIANTS = tuple(VARIANT_POLICY)
WARMUPS = 5
SAMPLES = 30


class BenchmarkError(RuntimeError):
    """Raised when a candidate or measurement is not trustworthy."""


@dataclass(frozen=True, slots=True)
class Candidate:
    variant_id: str
    executable: Path
    policy_path: Path
    identity: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{label} must contain a JSON object")
    return value


def _locked_file(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkError(f"{label} is missing")
    stat = path.stat()
    return {
        "name": path.name,
        "sizeBytes": stat.st_size,
        "sha256": _sha256_file(path),
    }


def load_candidate(variant_id: str, executable: Path) -> Candidate:
    if variant_id not in VARIANT_POLICY:
        raise BenchmarkError(f"unsupported variant {variant_id!r}")
    exe = executable.resolve()
    if not exe.is_file() or exe.name.casefold() != "scriber-backend.exe":
        raise BenchmarkError(f"{variant_id} does not point to a frozen scriber-backend.exe")
    root = exe.parent
    policy_path = root / POLICY_FILE
    policy = _load_object(policy_path, f"{variant_id} runtime policy")
    if policy.get("schemaVersion") != 1 or policy.get("name") != POLICY_NAME:
        raise BenchmarkError(f"{variant_id} runtime policy identity is invalid")
    if policy.get("variantId") != variant_id:
        raise BenchmarkError(f"{variant_id} runtime policy selects another variant")
    python = policy.get("python")
    jit = policy.get("jit")
    if not isinstance(python, dict) or not isinstance(jit, dict):
        raise BenchmarkError(f"{variant_id} runtime policy is incomplete")
    if python.get("version") != "3.14.6" or python.get("cacheTag") != "cpython-314":
        raise BenchmarkError(f"{variant_id} is not CPython 3.14.6")
    expected_flavor, expected_jit, expected_tail = VARIANT_POLICY[variant_id]
    if jit.get("available") is not True:
        raise BenchmarkError(f"{variant_id} requires a JIT-capable CPython 3.14.6 build")
    if jit.get("expected") is not expected_jit or jit.get("active") is not expected_jit:
        raise BenchmarkError(f"{variant_id} JIT policy is invalid")
    if policy.get("runtimeFlavor") != expected_flavor or policy.get("tailCallInterpreter") is not expected_tail:
        raise BenchmarkError(f"{variant_id} runtime flavor is invalid")
    dll = python.get("dll")
    if not isinstance(dll, dict) or dll.get("path") != "_internal/python314.dll":
        raise BenchmarkError(f"{variant_id} Python DLL identity is invalid")
    dll_path = root / "_internal" / "python314.dll"
    dll_file = _locked_file(dll_path, f"{variant_id} python314.dll")
    if dll.get("length") != dll_file["sizeBytes"] or dll.get("sha256") != dll_file["sha256"]:
        raise BenchmarkError(f"{variant_id} python314.dll differs from its policy")
    runtime_manifest = _locked_file(root / RUNTIME_MANIFEST_FILE, f"{variant_id} runtime manifest")
    app_manifest = _locked_file(root / APP_MANIFEST, f"{variant_id} application manifest")
    executable_file = _locked_file(exe, f"{variant_id} executable")
    identity = {
        "pythonVersion": python["version"],
        "cacheTag": python["cacheTag"],
        "variantId": variant_id,
        "runtimeFlavor": policy["runtimeFlavor"],
        "compiler": policy.get("compiler"),
        "tailCallInterpreter": expected_tail,
        "jitAvailable": jit.get("available"),
        "jitExpected": expected_jit,
        "jitActive": expected_jit,
        "runtimeLockSha256": policy.get("runtimeLockSha256"),
        "pythonDll": dll_file,
        "executable": executable_file,
        "runtimeManifest": runtime_manifest,
        "applicationManifest": app_manifest,
        "policy": _locked_file(policy_path, f"{variant_id} runtime policy"),
    }
    for field in ("compiler", "runtimeLockSha256"):
        value = identity[field]
        if not isinstance(value, str) or not value:
            raise BenchmarkError(f"{variant_id} {field} is missing")
    compiler = str(identity["compiler"]).casefold()
    if (expected_flavor == "Official" and "msc" not in compiler) or (
        expected_flavor != "Official" and "clang" not in compiler
    ):
        raise BenchmarkError(f"{variant_id} compiler does not match its runtime flavor")
    if len(identity["runtimeLockSha256"]) != 64:
        raise BenchmarkError(f"{variant_id} runtime lock SHA-256 is invalid")
    return Candidate(variant_id=variant_id, executable=exe, policy_path=policy_path, identity=identity)


def amd_host_profile() -> dict[str, Any]:
    if os.name != "nt":
        raise BenchmarkError("the AMD frozen-backend benchmark is Windows-only")
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            vendor = str(winreg.QueryValueEx(key, "VendorIdentifier")[0]).strip()
            name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError as exc:
        raise BenchmarkError("could not read the Windows CPU identity") from exc
    if vendor.casefold() != "authenticamd":
        raise BenchmarkError(f"AMD host required; found {vendor or 'unknown'}")
    profile = {
        "cpuVendor": vendor,
        "cpuName": " ".join(name.split()),
        "logicalProcessors": os.cpu_count(),
        "machine": platform.machine(),
        "windowsVersion": platform.version(),
        "windowsRelease": platform.release(),
        "pythonClock": {
            "implementation": time.get_clock_info("perf_counter").implementation,
            "resolutionSeconds": time.get_clock_info("perf_counter").resolution,
            "monotonic": time.get_clock_info("perf_counter").monotonic,
        },
    }
    return {"profile": profile, "sha256": _canonical_sha256(profile)}


def _clean_environment(candidate: Candidate) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "PYTHON_JIT",
        "PYTHON_GIL",
        "PYTHON_TLBC",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "SCRIBER_RUNTIME_MODE",
        "SCRIBER_SESSION_TOKEN",
        "SCRIBER_PYTHON_RUNTIME_LOCK_SHA256",
    ):
        environment.pop(name, None)
    environment["PYTHON_JIT"] = "1" if candidate.identity["jitExpected"] else "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["SCRIBER_PYTHON_RUNTIME_MANIFEST"] = str(candidate.policy_path)
    return environment


def run_once(candidate: Candidate, timeout_seconds: float) -> float:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            [str(candidate.executable), "--runtime-import-check"],
            cwd=candidate.executable.parent,
            env=_clean_environment(candidate),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(f"{candidate.variant_id} runtime import check timed out") from exc
    duration_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    if completed.returncode != 0:
        raise BenchmarkError(f"{candidate.variant_id} runtime import check exited with {completed.returncode}")
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"{candidate.variant_id} emitted invalid import-check JSON") from exc
    if result != {"ok": True, "missing": []}:
        raise BenchmarkError(f"{candidate.variant_id} runtime import check did not pass")
    return duration_ms


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise BenchmarkError("cannot summarize an empty measurement set")
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def summarize(values: Sequence[float]) -> dict[str, float | int]:
    if len(values) != SAMPLES:
        raise BenchmarkError(f"exactly {SAMPLES} measured samples are required")
    mean = statistics.fmean(values)
    if mean <= 0:
        raise BenchmarkError("measurement mean must be positive")
    median = statistics.median(values)
    absolute_deviations = [abs(value - median) for value in values]
    return {
        "count": len(values),
        "meanMs": round(mean, 6),
        "p50Ms": round(median, 6),
        "p95Ms": round(_nearest_rank(values, 0.95), 6),
        "cv": round(statistics.stdev(values) / mean, 8),
        "medianAbsoluteDeviationMs": round(statistics.median(absolute_deviations), 6),
        "minMs": round(min(values), 6),
        "maxMs": round(max(values), 6),
    }


def _rotated_candidates(candidates: Sequence[Candidate], round_index: int) -> list[Candidate]:
    if not candidates:
        raise BenchmarkError("candidate rotation cannot be empty")
    shift = round_index % len(candidates)
    rotated = [*candidates[shift:], *candidates[:shift]]
    if (round_index // len(candidates)) % 2:
        rotated.reverse()
    return rotated


def _comparisons_against_o0(summaries: Mapping[str, Mapping[str, float | int]]) -> dict[str, Any]:
    if "O0" not in summaries:
        return {
            "available": False,
            "referenceVariant": "O0",
            "reason": "O0-not-measured",
            "pairs": [],
        }
    reference = summaries["O0"]
    pairs: list[dict[str, Any]] = []
    for variant_id in ALLOWED_VARIANTS:
        if variant_id == "O0" or variant_id not in summaries:
            continue
        candidate = summaries[variant_id]
        metrics: dict[str, Any] = {}
        for metric in ("meanMs", "p50Ms", "p95Ms"):
            reference_value = float(reference[metric])
            candidate_value = float(candidate[metric])
            metrics[metric] = {
                "referenceMs": reference_value,
                "candidateMs": candidate_value,
                "deltaMs": round(candidate_value - reference_value, 6),
                "gainFraction": round((reference_value - candidate_value) / reference_value, 8),
            }
        metrics["cv"] = {
            "reference": float(reference["cv"]),
            "candidate": float(candidate["cv"]),
            "delta": round(float(candidate["cv"]) - float(reference["cv"]), 8),
        }
        pairs.append(
            {
                "referenceVariant": "O0",
                "candidateVariant": variant_id,
                "metrics": metrics,
            }
        )
    return {
        "available": True,
        "referenceVariant": "O0",
        "pairs": pairs,
    }


def measure(
    candidates: Mapping[str, Candidate],
    *,
    host: Mapping[str, Any],
    timeout_seconds: float,
    runner: Callable[[Candidate, float], float] = run_once,
) -> dict[str, Any]:
    if len(candidates) < 2:
        raise BenchmarkError("the benchmark requires at least two variants")
    unknown = set(candidates).difference(VARIANT_POLICY)
    if unknown:
        raise BenchmarkError(f"unsupported variants: {', '.join(sorted(unknown))}")
    ordered_candidates = [candidates[variant] for variant in ALLOWED_VARIANTS if variant in candidates]
    for round_index in range(WARMUPS):
        for candidate in _rotated_candidates(ordered_candidates, round_index):
            runner(candidate, timeout_seconds)

    samples: dict[str, list[dict[str, float | int]]] = {variant.variant_id: [] for variant in ordered_candidates}
    sequence = 0
    for round_index in range(SAMPLES):
        for candidate in _rotated_candidates(ordered_candidates, round_index):
            duration_ms = runner(candidate, timeout_seconds)
            if not math.isfinite(duration_ms) or duration_ms <= 0:
                raise BenchmarkError(f"{candidate.variant_id} produced an invalid duration")
            samples[candidate.variant_id].append(
                {
                    "sequence": sequence,
                    "round": round_index + 1,
                    "durationMs": round(duration_ms, 6),
                }
            )
            sequence += 1

    evidence: dict[str, Any] = {
        "evidenceContract": EVIDENCE_CONTRACT,
        "schemaVersion": 2,
        "runId": str(uuid.uuid4()),
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "scope": "frozen-backend-process-startup-plus-runtime-import-check",
        "hostScope": "amd-only",
        "productionPromotionAuthorized": False,
        "promotionDisclaimer": (
            "Diagnostic startup/import evidence only; this does not replace FullLocal, "
            "local_wux, provider replay, app-UX, Intel coverage, or promotion gates."
        ),
        "measurement": {
            "command": ["scriber-backend.exe", "--runtime-import-check"],
            "warmupsPerVariant": WARMUPS,
            "samplesPerVariant": SAMPLES,
            "variantOrder": [candidate.variant_id for candidate in ordered_candidates],
            "order": "cyclic-position-rotation-with-periodic-reversal",
            "timer": "time.perf_counter_ns",
            "includesProcessCreationAndFrozenLauncherValidation": True,
        },
        "host": dict(host),
        "variants": {},
    }
    summaries: dict[str, Mapping[str, float | int]] = {}
    for variant_id in samples:
        durations = [float(sample["durationMs"]) for sample in samples[variant_id]]
        summary = summarize(durations)
        summaries[variant_id] = summary
        evidence["variants"][variant_id] = {
            "identity": candidates[variant_id].identity,
            "summary": summary,
            "samples": samples[variant_id],
        }
    evidence["comparisonsAgainstO0"] = _comparisons_against_o0(summaries)
    content = json.loads(json.dumps(evidence))
    evidence["contentSha256"] = _canonical_sha256(content)
    return evidence


def _parse_variant(value: str) -> tuple[str, Path]:
    variant_id, separator, raw_path = value.partition("=")
    if not separator or not raw_path:
        raise argparse.ArgumentTypeError("variant must use VARIANT=PATH")
    return variant_id.strip().upper(), Path(raw_path)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure a CPython 3.14.6 runtime-variant subset through frozen process startup "
            "plus --runtime-import-check on AMD."
        )
    )
    parser.add_argument("--variant", action="append", type=_parse_variant, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if not math.isfinite(arguments.timeout_seconds) or arguments.timeout_seconds <= 0:
            raise BenchmarkError("timeout-seconds must be positive")
        raw_candidates = dict(arguments.variant)
        if len(raw_candidates) != len(arguments.variant):
            raise BenchmarkError("variant IDs must be unique")
        candidates = {variant_id: load_candidate(variant_id, path) for variant_id, path in raw_candidates.items()}
        evidence = measure(
            candidates,
            host=amd_host_profile(),
            timeout_seconds=arguments.timeout_seconds,
        )
        _atomic_write(arguments.output.resolve(), evidence)
    except BenchmarkError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output": arguments.output.name,
                "variants": {variant_id: variant["summary"] for variant_id, variant in evidence["variants"].items()},
                "comparisonsAgainstO0": evidence["comparisonsAgainstO0"],
                "productionPromotionAuthorized": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
