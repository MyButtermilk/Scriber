from __future__ import annotations

import json
from pathlib import Path

from scripts.perf import doctor


SEGMENT = "B7-user-endpoint-p50-p95-baseline"
PROFILE_ID = "profile-b7"
BASELINE_ID = "baseline-b7"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def b7_metrics() -> dict[str, float]:
    return {
        "local_wux": 1.0,
        **{name: 100.0 for name in doctor.B7_REQUIRED_BASELINE_METRICS},
    }


def write_evaluator_surface(repo_root: Path) -> tuple[str, str]:
    for index, relative in enumerate(doctor.EVALUATOR_PATHS):
        path = repo_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"evaluator surface {index}\n", encoding="utf-8")
    scorer_hash = doctor.file_sha256(
        repo_root / "scripts" / "perf" / "evaluator" / "local_wux.py"
    )
    evaluator_hash, missing = doctor.current_evaluator_hash(repo_root)
    assert missing == []
    return scorer_hash, evaluator_hash


def coherent_b7_state(repo_root: Path) -> dict[str, Path | str]:
    scorer_hash, evaluator_hash = write_evaluator_surface(repo_root)
    baseline_path = repo_root / "benchmarks" / "results" / "baseline.json"
    baseline = {
        "schemaVersion": doctor.STATE_SCHEMA_VERSION,
        "segment": SEGMENT,
        "status": "accepted",
        "active": True,
        "metric": "local_wux",
        "baselineId": BASELINE_ID,
        "profileId": PROFILE_ID,
        "scorerHash": scorer_hash,
        "evaluatorHash": evaluator_hash,
        "metrics": b7_metrics(),
    }
    write_json(baseline_path, baseline)
    baseline_sha = doctor.file_sha256(baseline_path)

    config_path = repo_root / "autoresearch.config.json"
    write_json(
        config_path,
        {
            "schemaVersion": doctor.STATE_SCHEMA_VERSION,
            "segment": SEGMENT,
            "baseline": {
                "status": "accepted",
                "accepted": True,
                "value": 1.0,
                "baselineId": BASELINE_ID,
                "baselineSha256": baseline_sha,
                "profileId": PROFILE_ID,
                "scorerHash": scorer_hash,
                "evaluatorHash": evaluator_hash,
            },
        },
    )
    profile_path = repo_root / "benchmarks" / "results" / "profile.json"
    write_json(
        profile_path,
        {
            "schemaVersion": doctor.STATE_SCHEMA_VERSION,
            "profile_id": PROFILE_ID,
            "baselineId": BASELINE_ID,
            "baselineSha256": baseline_sha,
            "scorerHash": scorer_hash,
            "evaluatorHash": evaluator_hash,
        },
    )
    champion_path = repo_root / "benchmarks" / "results" / "champion.json"
    write_json(
        champion_path,
        {
            "schemaVersion": doctor.STATE_SCHEMA_VERSION,
            "segment": SEGMENT,
            "status": "active",
            "active": True,
            "championId": "champion-b7",
            "baselineId": BASELINE_ID,
            "baselineSha256": baseline_sha,
            "profileId": PROFILE_ID,
            "scorerHash": scorer_hash,
            "evaluatorHash": evaluator_hash,
        },
    )
    last_run_path = repo_root / ".git" / "autoresearch" / "last-run.json"
    write_json(
        last_run_path,
        {
            "schemaVersion": doctor.STATE_SCHEMA_VERSION,
            "segment": SEGMENT,
            "suite": "FastLocal",
            "exitCode": 0,
            "baselineId": BASELINE_ID,
            "baselineSha256": baseline_sha,
            "profileId": PROFILE_ID,
            "scorerHash": scorer_hash,
            "evaluatorHash": evaluator_hash,
        },
    )
    progress_path = repo_root / ".git" / "autoresearch" / "progress.json"
    write_json(
        progress_path,
        {
            "schemaVersion": doctor.STATE_SCHEMA_VERSION,
            "segment": SEGMENT,
            "baselineAccepted": True,
            "baselineId": BASELINE_ID,
            "baselineSha256": baseline_sha,
            "profileId": PROFILE_ID,
            "scorerHash": scorer_hash,
            "evaluatorHash": evaluator_hash,
            "championId": "champion-b7",
            "lastRunSha256": doctor.file_sha256(last_run_path),
        },
    )
    return {
        "baseline": baseline_path,
        "config": config_path,
        "profile": profile_path,
        "champion": champion_path,
        "last_run": last_run_path,
        "progress": progress_path,
        "scorer_hash": scorer_hash,
        "evaluator_hash": evaluator_hash,
    }


def blocking_codes(repo_root: Path) -> list[str]:
    return [
        str(item.get("code"))
        for item in doctor.check_autoresearch_state(repo_root)
        if item.get("level") == "block"
    ]


def test_doctor_accepts_one_fully_bound_b7_state(tmp_path: Path) -> None:
    coherent_b7_state(tmp_path)
    assert blocking_codes(tmp_path) == []


def test_doctor_rejects_config_baseline_sha_drift(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    config = read_json(paths["config"])
    config["baseline"]["baselineSha256"] = "0" * 64
    write_json(paths["config"], config)

    assert "autoresearch_baseline_sha_mismatch" in blocking_codes(tmp_path)


def test_doctor_rejects_schema_and_baseline_id_drift(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    last_run = read_json(paths["last_run"])
    last_run["schemaVersion"] = 0
    last_run["baselineId"] = "baseline-from-another-segment"
    write_json(paths["last_run"], last_run)
    progress = read_json(paths["progress"])
    progress["lastRunSha256"] = doctor.file_sha256(paths["last_run"])
    write_json(paths["progress"], progress)

    codes = blocking_codes(tmp_path)
    assert "autoresearch_schema_mismatch" in codes
    assert "autoresearch_baseline_id_mismatch" in codes


def test_doctor_rejects_profile_and_evaluator_identity_drift(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    profile = read_json(paths["profile"])
    profile["profile_id"] = "another-machine-profile"
    profile["evaluatorHash"] = "0" * 64
    write_json(paths["profile"], profile)

    codes = blocking_codes(tmp_path)
    assert "autoresearch_profile_id_mismatch" in codes
    assert "autoresearch_evaluator_hash_mismatch" in codes


def test_doctor_rejects_scorer_drift_after_baseline_acceptance(tmp_path: Path) -> None:
    coherent_b7_state(tmp_path)
    scorer = tmp_path / "scripts" / "perf" / "evaluator" / "local_wux.py"
    scorer.write_text("changed scorer\n", encoding="utf-8")

    codes = blocking_codes(tmp_path)
    assert "autoresearch_scorer_hash_mismatch" in codes
    assert "autoresearch_evaluator_hash_mismatch" in codes


def test_doctor_rejects_active_b4_champion_but_allows_explicit_history(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    champion = read_json(paths["champion"])
    champion["segment"] = "B4-provider-observer-ready-baseline"
    write_json(paths["champion"], champion)

    assert "autoresearch_champion_segment_mismatch" in blocking_codes(tmp_path)

    champion["status"] = "historical"
    champion["active"] = False
    write_json(paths["champion"], champion)
    progress = read_json(paths["progress"])
    progress.pop("championId")
    write_json(paths["progress"], progress)

    assert "autoresearch_champion_segment_mismatch" not in blocking_codes(tmp_path)


def test_doctor_rejects_active_b6_baseline_under_b7(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    baseline = read_json(paths["baseline"])
    baseline["segment"] = "B6-binary-version-attested-baseline"
    write_json(paths["baseline"], baseline)

    codes = blocking_codes(tmp_path)
    assert "autoresearch_baseline_segment_mismatch" in codes


def test_doctor_rejects_stale_resume_segment_and_last_run_binding(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    progress = read_json(paths["progress"])
    progress["segment"] = "B4-provider-observer-ready-baseline"
    progress["lastRunSha256"] = "0" * 64
    write_json(paths["progress"], progress)

    findings = doctor.check_autoresearch_state(tmp_path)
    assert any(
        item.get("code") == "autoresearch_segment_mismatch"
        and item.get("artifact") == "progress"
        for item in findings
    )
    assert any(
        item.get("code") == "autoresearch_resume_last_run_sha_mismatch"
        for item in findings
    )


def test_doctor_rejects_b6_config_for_the_b7_evaluator(tmp_path: Path) -> None:
    paths = coherent_b7_state(tmp_path)
    config = read_json(paths["config"])
    config["segment"] = "B6-binary-version-attested-baseline"
    write_json(paths["config"], config)
    last_run = read_json(paths["last_run"])
    last_run.pop("segment")
    write_json(paths["last_run"], last_run)

    codes = blocking_codes(tmp_path)
    assert "autoresearch_active_segment_outdated" in codes
    assert "autoresearch_resume_segment_missing" in codes


def test_check_benchmark_is_not_called_when_state_is_untrusted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(doctor, "check_static", lambda *_args: [])
    monkeypatch.setattr(
        doctor,
        "check_autoresearch_state",
        lambda *_args: [
            {
                "level": "block",
                "code": "autoresearch_active_segment_outdated",
                "message": "legacy state",
            }
        ],
    )

    def unexpected_benchmark(*_args):
        raise AssertionError("benchmark must not run for untrusted state")

    monkeypatch.setattr(doctor, "check_benchmark", unexpected_benchmark)

    assert (
        doctor.main(
            [
                "--repo-root",
                str(tmp_path),
                "--install-root",
                str(tmp_path / "install"),
                "--check-benchmark",
            ]
        )
        == 1
    )
