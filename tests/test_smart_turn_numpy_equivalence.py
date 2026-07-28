from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from scripts import verify_smart_turn_numpy_equivalence as equivalence
from scripts.verify_smart_turn_numpy_equivalence import (
    compare_payloads,
)


def _wheel_identity(*, core_sha256: str = "a" * 64) -> dict:
    return {
        "name": "numpy-2.4.6+scriber.noblas.1-cp314-cp314-win_amd64.whl",
        "length": 1234,
        "sha256": "e" * 64,
        "coreSha256": core_sha256,
        "recordSha256": "f" * 64,
        "entryCount": 928,
        "recordPath": "numpy-2.4.6+scriber.noblas.1.dist-info/RECORD",
    }


def _payload(
    version: str,
    *,
    role: str,
    core_sha256: str = "a" * 64,
    wheel_identity: dict | None = None,
) -> dict:
    wheel = wheel_identity or _wheel_identity(core_sha256=core_sha256)
    installation = None
    if role == "candidate":
        installation = {
            **wheel,
            "installedRecordSha256": "1" * 64,
            "installedRecordEntryCount": 1000,
            "wheelEntriesVerified": wheel["entryCount"] - 1,
            "installerRecordExtraCount": 72,
            "verified": True,
        }
    return {
        "role": role,
        "pythonVersion": "3.14.6",
        "pythonCacheTag": "cpython-314",
        "numpyVersion": version,
        "buildDependencies": {
            "blas": "none" if "+scriber" in version else "scipy-openblas",
            "lapack": "none" if "+scriber" in version else "scipy-openblas",
        },
        "numpyCoreSha256": core_sha256,
        "packageVersions": dict(equivalence.EXPECTED_PACKAGE_VERSIONS),
        "fixture": {
            "contract": "ScriberSmartTurnSyntheticPcmV1",
            "sampleRateHz": 16_000,
            "sampleCount": 128_000,
            "pcmS16leSha256": "2" * 64,
            "audioFloat32Sha256": "3" * 64,
        },
        "models": {
            "pipecatSmartTurn": {
                "name": "smart-turn-v3.2-cpu.onnx",
                "length": 8_679_182,
                "sha256": "4" * 64,
            },
            "melProjection": {
                "name": "smart-turn-mel-matmul.onnx",
                "length": 128_859,
                "sha256": "5" * 64,
            },
            "melFiltersSha256": "6" * 64,
        },
        "melAccelerationInstalled": role == "candidate",
        "numpyWheelInstallation": installation,
        "features": {
            "dtype": "float32",
            "shape": [80, 800],
            "valueCount": 64_000,
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
    wheel = _wheel_identity()
    baseline = _payload("2.4.6", role="baseline")
    candidate = _payload("2.4.6+scriber.noblas.1", role="candidate", wheel_identity=wheel)

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_identity=wheel,
    )

    assert ok is True
    assert errors == []


def test_feature_probability_and_decision_drift_fail_closed() -> None:
    wheel = _wheel_identity()
    baseline = _payload("2.4.6", role="baseline")
    candidate = _payload("2.4.6+scriber.noblas.1", role="candidate", wheel_identity=wheel)
    candidate["features"] = {**candidate["features"], "sha256": "c" * 64}
    candidate["probability"] = 0.7501
    candidate["prediction"] = 0
    candidate["decision"] = "incomplete"

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_identity=wheel,
    )

    assert ok is False
    assert errors == [
        "smart_turn_features_mismatch",
        "smart_turn_probability_mismatch",
        "smart_turn_prediction_mismatch",
        "smart_turn_decision_mismatch",
    ]


def test_fixture_model_package_and_acceleration_drift_fail_closed() -> None:
    wheel = _wheel_identity()
    baseline = _payload("2.4.6", role="baseline")
    candidate = _payload("2.4.6+scriber.noblas.1", role="candidate", wheel_identity=wheel)
    candidate["packageVersions"] = {**candidate["packageVersions"], "onnxruntime": "0.0.0"}
    candidate["fixture"] = {**candidate["fixture"], "pcmS16leSha256": "7" * 64}
    candidate["models"] = {**candidate["models"], "melFiltersSha256": "8" * 64}
    candidate["melAccelerationInstalled"] = False

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_identity=wheel,
    )

    assert ok is False
    assert errors == [
        "candidate_package_versions_mismatch",
        "smart_turn_fixture_mismatch",
        "smart_turn_model_identity_mismatch",
        "candidate_mel_acceleration_not_installed",
    ]


def test_candidate_must_match_the_complete_locked_no_blas_wheel() -> None:
    wheel = _wheel_identity(core_sha256="d" * 64)
    baseline = _payload("2.4.6", role="baseline")
    candidate = _payload("2.4.6+scriber.noblas.1", role="candidate")

    ok, errors = compare_payloads(
        baseline,
        candidate,
        wheel_identity=wheel,
    )

    assert ok is False
    assert errors == [
        "candidate_numpy_core_wheel_mismatch",
        "candidate_numpy_wheel_installation_mismatch",
    ]


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _record_bytes(rows: list[tuple[str, str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue().encode()


def _tiny_numpy_wheel(tmp_path: Path) -> tuple[Path, Path, str]:
    dist_info = "numpy-2.4.6+scriber.noblas.1.dist-info"
    files = {
        "numpy/__init__.py": b'__version__ = "2.4.6+scriber.noblas.1"\n',
        "numpy/_core/_multiarray_umath.cp314-win_amd64.pyd": b"test-pyd",
        f"{dist_info}/METADATA": b"Name: numpy\nVersion: 2.4.6+scriber.noblas.1\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nTag: cp314-cp314-win_amd64\n",
    }
    record_name = f"{dist_info}/RECORD"
    rows = [(name, _record_digest(data), str(len(data))) for name, data in sorted(files.items())]
    rows.append((record_name, "", ""))
    wheel_record = _record_bytes(rows)
    wheel = tmp_path / "numpy-2.4.6+scriber.noblas.1-cp314-cp314-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(record_name, wheel_record)

    site_root = tmp_path / "site-packages"
    for name, data in files.items():
        target = site_root.joinpath(*Path(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    installer = site_root / dist_info / "INSTALLER"
    installer.write_bytes(b"pip\n")
    installed_rows = [
        *rows[:-1],
        (f"{dist_info}/INSTALLER", _record_digest(installer.read_bytes()), str(installer.stat().st_size)),
        (record_name, "", ""),
    ]
    (site_root / record_name).write_bytes(_record_bytes(installed_rows))
    return wheel, site_root, dist_info


def test_candidate_record_verifier_rejects_a_tampered_wheel_file(tmp_path: Path) -> None:
    wheel, site_root, dist_info = _tiny_numpy_wheel(tmp_path)
    summary = equivalence._verify_installed_distribution_matches_wheel(
        wheel,
        site_root=site_root,
        dist_info_name=dist_info,
    )
    assert summary["verified"] is True
    assert summary["wheelEntriesVerified"] == 4

    (site_root / "numpy/_core/_multiarray_umath.cp314-win_amd64.pyd").write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="differs from the wheel"):
        equivalence._verify_installed_distribution_matches_wheel(
            wheel,
            site_root=site_root,
            dist_info_name=dist_info,
        )


def test_locked_wheel_identity_is_fail_closed(tmp_path: Path) -> None:
    wheel = _wheel_identity()
    lock_path = tmp_path / equivalence.NUMPY_WHEEL_LOCK_PATH
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "contract": "ScriberNumPyNoBlasWheelLockV1",
                "artifact": {
                    "fileName": wheel["name"],
                    "length": wheel["length"],
                    "sha256": wheel["sha256"],
                },
            }
        ),
        encoding="utf-8",
    )
    equivalence._validate_locked_wheel(tmp_path, wheel)

    wheel["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="does not match"):
        equivalence._validate_locked_wheel(tmp_path, wheel)


def _write_provenance_inputs(repo_root: Path) -> Path:
    harness = repo_root / "scripts/verify_smart_turn_numpy_equivalence.py"
    inputs = {
        harness: b"harness",
        repo_root / equivalence.NUMPY_WHEEL_LOCK_PATH: b"lock",
        repo_root / equivalence.RELEASE_CONSTRAINTS_PATH: b"release",
        repo_root / equivalence.TEST_CONSTRAINTS_PATH: b"test",
        repo_root / equivalence.MEL_MODEL_PATH: b"model",
    }
    for path, data in inputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return harness


def test_source_provenance_binds_clean_head_and_input_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _write_provenance_inputs(tmp_path)

    def fake_git(_repo_root: Path, *arguments: str) -> str:
        return "9" * 40 if arguments[0] == "rev-parse" else ""

    monkeypatch.setattr(equivalence, "_git_output", fake_git)
    provenance = equivalence._source_provenance(tmp_path, harness_path=harness)

    assert provenance["gitCommit"] == "9" * 40
    assert provenance["gitTreeClean"] is True
    assert provenance["inputs"]["harness"] == {
        "relativePath": "scripts/verify_smart_turn_numpy_equivalence.py",
        "length": 7,
        "sha256": hashlib.sha256(b"harness").hexdigest(),
    }
    assert set(provenance["inputs"]) == {
        "harness",
        "numpyWheelLock",
        "releaseConstraints",
        "testConstraints",
        "melProjectionModel",
    }


def test_source_provenance_rejects_a_dirty_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _write_provenance_inputs(tmp_path)

    def fake_git(_repo_root: Path, *arguments: str) -> str:
        return "9" * 40 if arguments[0] == "rev-parse" else " M src/runtime/smart_turn_mel.py"

    monkeypatch.setattr(equivalence, "_git_output", fake_git)

    with pytest.raises(RuntimeError, match="clean Git worktree"):
        equivalence._source_provenance(tmp_path, harness_path=harness)
