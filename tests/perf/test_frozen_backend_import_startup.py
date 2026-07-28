from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.measure_frozen_backend_import_startup import (
    ALLOWED_VARIANTS,
    SAMPLES,
    BenchmarkError,
    Candidate,
    _clean_environment,
    load_candidate,
    measure,
    summarize,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate(tmp_path: Path, variant_id: str) -> Candidate:
    root = tmp_path / variant_id
    internal = root / "_internal"
    app = root / "app"
    internal.mkdir(parents=True)
    app.mkdir()
    executable = root / "scriber-backend.exe"
    executable.write_bytes(b"exe-" + variant_id.encode())
    dll = b"dll-" + variant_id.encode()
    (internal / "python314.dll").write_bytes(dll)
    (root / "runtime-layer-manifest.json").write_text("{}", encoding="utf-8")
    (app / "app-layer-manifest.json").write_text("{}", encoding="utf-8")
    flavor = "Official" if variant_id.startswith("O") else "ClangPgoTail" if variant_id.startswith("T") else "ClangPgo"
    jit = variant_id.endswith("1")
    policy = {
        "schemaVersion": 1,
        "name": "scriber-python-runtime-policy",
        "python": {
            "version": "3.14.6",
            "cacheTag": "cpython-314",
            "dll": {
                "path": "_internal/python314.dll",
                "length": len(dll),
                "sha256": _sha(dll),
            },
        },
        "runtimeFlavor": flavor,
        "variantId": variant_id,
        "compiler": "MSC test" if variant_id.startswith("O") else "Clang 19.1.7",
        "tailCallInterpreter": variant_id.startswith("T"),
        "jit": {"available": True, "expected": jit, "active": jit},
        "runtimeLockSha256": "a" * 64,
    }
    (root / "python-runtime-manifest.json").write_text(json.dumps(policy), encoding="utf-8")
    return load_candidate(variant_id, executable)


def test_complete_matrix_rotates_positions_and_summarizes_every_pair_against_o0(
    tmp_path: Path,
) -> None:
    candidates = {variant_id: _candidate(tmp_path, variant_id) for variant_id in ALLOWED_VARIANTS}
    calls: list[str] = []

    def runner(candidate: Candidate, _timeout: float) -> float:
        calls.append(candidate.variant_id)
        base = 100.0 - ALLOWED_VARIANTS.index(candidate.variant_id) * 2.0
        return base + (len(calls) % 3) / 10

    evidence = measure(
        candidates,
        host={"profile": {"cpuVendor": "AuthenticAMD"}, "sha256": "b" * 64},
        timeout_seconds=10,
        runner=runner,
    )

    assert calls[:12] == [
        "O0",
        "O1",
        "C0",
        "C1",
        "T0",
        "T1",
        "O1",
        "C0",
        "C1",
        "T0",
        "T1",
        "O0",
    ]
    assert len(calls) == (5 + 30) * 6
    assert evidence["productionPromotionAuthorized"] is False
    assert evidence["hostScope"] == "amd-only"
    assert "does not replace FullLocal" in evidence["promotionDisclaimer"]
    assert set(evidence["variants"]) == set(ALLOWED_VARIANTS)
    assert all(variant["summary"]["count"] == 30 for variant in evidence["variants"].values())
    comparisons = evidence["comparisonsAgainstO0"]
    assert comparisons["available"] is True
    assert [pair["candidateVariant"] for pair in comparisons["pairs"]] == [
        "O1",
        "C0",
        "C1",
        "T0",
        "T1",
    ]
    assert comparisons["pairs"][0]["metrics"]["p50Ms"]["gainFraction"] > 0
    assert len(evidence["contentSha256"]) == 64


def test_summary_uses_nearest_rank_p95_and_sample_cv() -> None:
    values = [float(index) for index in range(1, SAMPLES + 1)]
    summary = summarize(values)
    assert summary["p50Ms"] == 15.5
    assert summary["p95Ms"] == 29.0
    assert summary["count"] == 30
    assert summary["cv"] > 0


def test_candidate_rejects_policy_dll_drift(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "O0")
    (candidate.executable.parent / "_internal" / "python314.dll").write_bytes(b"changed")
    with pytest.raises(BenchmarkError, match="differs from its policy"):
        load_candidate("O0", candidate.executable)


def test_measurement_accepts_allowed_subset_without_o0_and_marks_comparison_unavailable(
    tmp_path: Path,
) -> None:
    variants = ("O1", "C0", "T1")
    evidence = measure(
        {variant_id: _candidate(tmp_path, variant_id) for variant_id in variants},
        host={"profile": {"cpuVendor": "AuthenticAMD"}, "sha256": "b" * 64},
        timeout_seconds=10,
        runner=lambda candidate, _timeout: 10.0 + variants.index(candidate.variant_id),
    )
    assert list(evidence["variants"]) == list(variants)
    assert evidence["comparisonsAgainstO0"] == {
        "available": False,
        "referenceVariant": "O0",
        "reason": "O0-not-measured",
        "pairs": [],
    }


def test_measurement_requires_at_least_two_variants(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="at least two variants"):
        measure(
            {"O0": _candidate(tmp_path, "O0")},
            host={"profile": {"cpuVendor": "AuthenticAMD"}, "sha256": "b" * 64},
            timeout_seconds=10,
            runner=lambda _candidate, _timeout: 1.0,
        )


@pytest.mark.parametrize("variant_id", ALLOWED_VARIANTS)
def test_candidate_enforces_flavor_jit_and_tail_policy(
    tmp_path: Path,
    variant_id: str,
) -> None:
    candidate = _candidate(tmp_path, variant_id)
    expected_jit = variant_id.endswith("1")
    assert candidate.identity["jitExpected"] is expected_jit
    assert candidate.identity["jitActive"] is expected_jit
    assert candidate.identity["tailCallInterpreter"] is variant_id.startswith("T")


def test_jit_environment_is_explicit_and_incompatible_modes_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHON_JIT", "0")
    monkeypatch.setenv("PYTHON_GIL", "0")
    monkeypatch.setenv("PYTHON_TLBC", "1")
    environment = _clean_environment(_candidate(tmp_path, "C1"))
    assert environment["PYTHON_JIT"] == "1"
    assert "PYTHON_GIL" not in environment
    assert "PYTHON_TLBC" not in environment
