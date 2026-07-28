from __future__ import annotations

from scripts.verify_smart_turn_numpy_equivalence import compare_payloads


def _payload(version: str, *, core_sha256: str = "a" * 64) -> dict:
    return {
        "pythonVersion": "3.14.6",
        "pythonCacheTag": "cpython-314",
        "numpyVersion": version,
        "buildDependencies": {
            "blas": "none" if "+scriber" in version else "scipy-openblas",
            "lapack": "none" if "+scriber" in version else "scipy-openblas",
        },
        "numpyCoreSha256": core_sha256,
        "features": {
            "dtype": "float32",
            "shape": [80, 800],
            "sha256": "b" * 64,
            "minimum": -1.0,
            "maximum": 1.0,
            "mean": 0.0,
        },
        "probability": 0.75,
        "prediction": 1,
        "decision": "complete",
    }


def test_equivalent_public_and_no_blas_payloads_are_accepted() -> None:
    baseline = _payload("2.4.6")
    candidate = _payload("2.4.6+scriber.noblas.1")

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_core_sha256="a" * 64,
    )

    assert ok is True
    assert errors == []


def test_feature_probability_and_decision_drift_fail_closed() -> None:
    baseline = _payload("2.4.6")
    candidate = _payload("2.4.6+scriber.noblas.1")
    candidate["features"] = {**candidate["features"], "sha256": "c" * 64}
    candidate["probability"] = 0.7501
    candidate["prediction"] = 0
    candidate["decision"] = "incomplete"

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_core_sha256="a" * 64,
    )

    assert ok is False
    assert errors == [
        "smart_turn_features_mismatch",
        "smart_turn_probability_mismatch",
        "smart_turn_prediction_mismatch",
        "smart_turn_decision_mismatch",
    ]


def test_candidate_must_match_the_locked_no_blas_wheel() -> None:
    baseline = _payload("2.4.6")
    candidate = _payload("2.4.6+scriber.noblas.1")

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_core_sha256="d" * 64,
    )

    assert ok is False
    assert errors == ["candidate_numpy_core_wheel_mismatch"]
