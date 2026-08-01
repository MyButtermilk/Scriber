from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scriber_polishing.git_handoff import (
    GitHandoffError,
    build_git_handoff,
    parse_porcelain_v1_z,
    scan_credentials,
    verify_git_handoff,
    verify_git_pr_evidence,
)


def _git(repository: Path, *arguments: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )
    return process.stdout


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    project = repository / "ml" / "scriber_polishing"
    (project / "src" / "scriber_polishing").mkdir(parents=True)
    (project / "src" / "scriber_polishing" / "foundation.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Git Handoff Test")
    _git(repository, "config", "user.email", "handoff@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "remote", "add", "origin", "https://github.com/example/scriber.git")
    return repository, project


def _status(repository: Path) -> bytes:
    return _git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
    )


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_porcelain_parser_preserves_rename_and_both_status_columns() -> None:
    parsed = parse_porcelain_v1_z(
        b" M ml/scriber_polishing/a.py\0"
        b"R  ml/scriber_polishing/new.py\0"
        b"ml/scriber_polishing/old.py\0"
        b"?? ml/scriber_polishing/new.yaml\0"
    )

    assert parsed == [
        {
            "path": "ml/scriber_polishing/a.py",
            "status": {"index": " ", "worktree": "M"},
        },
        {
            "path": "ml/scriber_polishing/new.py",
            "original_path": "ml/scriber_polishing/old.py",
            "status": {"index": "R", "worktree": " "},
        },
        {
            "path": "ml/scriber_polishing/new.yaml",
            "status": {"index": "?", "worktree": "?"},
        },
    ]


def test_credential_scanner_does_not_flag_its_own_auditable_source() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "scriber_polishing"
        / "git_handoff.py"
    ).read_bytes()

    assert scan_credentials(
        source,
        location="worktree",
        path="ml/scriber_polishing/src/scriber_polishing/git_handoff.py",
    ) == []


def test_read_only_inventory_allows_narrow_text_files_and_groups_every_path(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    source = project / "src" / "scriber_polishing" / "foundation.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    config = project / "configs" / "training.yaml"
    config.parent.mkdir()
    config.write_text("learning_rate: 0.00005\n", encoding="utf-8")
    before_status = _status(repository)
    before_head = _git(repository, "rev-parse", "HEAD")

    report = build_git_handoff(
        repository,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    assert _status(repository) == before_status
    assert _git(repository, "rev-parse", "HEAD") == before_head
    assert report["readiness"]["inventory_safe_to_stage"] is True
    assert report["inventory"]["excluded"] == []
    included_paths = {item["path"] for item in report["inventory"]["included"]}
    assert included_paths == {
        "ml/scriber_polishing/configs/training.yaml",
        "ml/scriber_polishing/src/scriber_polishing/foundation.py",
    }
    grouped_paths = [
        path for group in report["commit_groups"] for path in group["paths"]
    ]
    assert len(grouped_paths) == len(set(grouped_paths))
    assert set(grouped_paths) == included_paths
    assert report["pull_request_draft"]["mutation_performed"] is False
    assert verify_git_handoff(
        report,
        contracts_root=Path(__file__).parents[1] / "contracts",
    ) == report


def test_read_only_inventory_allows_shell_pipeline_scripts(tmp_path: Path) -> None:
    repository, project = _repository(tmp_path)
    script = project / "scripts" / "run_remote_evaluation.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")

    report = build_git_handoff(
        repository,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    assert report["readiness"]["inventory_safe_to_stage"] is True
    assert report["inventory"]["excluded"] == []
    assert {
        (item["path"], item["category"])
        for item in report["inventory"]["included"]
    } == {
        (
            "ml/scriber_polishing/scripts/run_remote_evaluation.sh",
            "pipeline_code",
        )
    }


def test_force_staged_weight_is_inventoried_and_blocks_handoff(tmp_path: Path) -> None:
    repository, project = _repository(tmp_path)
    weight = project / "models" / "model.safetensors"
    weight.parent.mkdir()
    weight.write_bytes(b"not-a-real-model")
    _git(repository, "add", "--force", weight.relative_to(repository).as_posix())

    report = build_git_handoff(
        repository,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    assert report["readiness"]["inventory_safe_to_stage"] is False
    assert report["readiness"]["current_index_safe_to_commit"] is False
    assert report["inventory"]["included"] == []
    assert report["inventory"]["excluded"][0]["path"].endswith("model.safetensors")
    assert "forbidden_model_or_archive_extension" in report["inventory"]["excluded"][0][
        "reason_codes"
    ]
    assert "unsafe_or_out_of_scope_paths_staged" in report["readiness"]["blocker_codes"]


def test_unchanged_forbidden_file_in_tracked_index_still_blocks_push_preparation(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    weight = project / "models" / "legacy.bin"
    weight.parent.mkdir()
    weight.write_bytes(b"legacy-model")
    _git(repository, "add", weight.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", "track forbidden fixture")
    source = project / "src" / "scriber_polishing" / "foundation.py"
    source.write_text("VALUE = 3\n", encoding="utf-8")

    report = build_git_handoff(
        repository,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    assert report["inventory"]["forbidden_tracked_index_count"] == 1
    assert report["inventory"]["forbidden_tracked_index"][0]["path"].endswith(
        "legacy.bin"
    )
    assert "forbidden_files_in_tracked_index" in report["readiness"]["blocker_codes"]
    assert report["readiness"]["inventory_safe_to_stage"] is False


def test_credential_scanner_checks_staged_index_even_after_worktree_cleanup(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    config = project / "configs" / "publication.yaml"
    config.parent.mkdir()
    token = "h" + "f_" + "A7b9Cd3Ef5Gh7Jk9Lm2Np4Qr6St8Uv"
    config.write_text(f'access_token: "{token}"\n', encoding="utf-8")
    _git(repository, "add", config.relative_to(repository).as_posix())
    config.write_text('access_token: "${HF_TOKEN}"\n', encoding="utf-8")

    report = build_git_handoff(
        repository,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    assert report["credential_scan"]["passed"] is False
    assert {
        (finding["location"], finding["rule"])
        for finding in report["credential_scan"]["findings"]
    } >= {("index", "hugging_face_token")}
    assert token not in json.dumps(report)
    assert report["readiness"]["inventory_safe_to_stage"] is False
    assert not report["commit_groups"]


def test_draft_pr_uses_only_hash_bound_json_pointer_values(tmp_path: Path) -> None:
    repository, _project = _repository(tmp_path)
    evidence_path = repository / "evidence" / "release.json"
    evidence_path.parent.mkdir()
    evidence = {
        "objective": "Lokale Transkriptpolitur ohne Cloud-Inferenz.",
        "data_pipeline": {"accepted": 120000, "rejected": 2665},
        "agent_roles": ["Generator", "Critic", "Judge"],
        "training": {"method": "full SFT", "steps": 850},
        "metrics": {"critical_errors": 0},
        "risks": ["Laptop-Latenz bleibt zu verifizieren."],
        "hf_artifacts": {"visibility": "private"},
        "final_status": "READY_FOR_AI_JUDGING",
    }
    payload = (
        json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    evidence_path.write_bytes(payload)
    evidence_hash = _sha256(payload)
    manifest_path = repository / "evidence-bindings.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sections": {
                    section: {
                        "path": "evidence/release.json",
                        "sha256": evidence_hash,
                        "pointer": f"/{section}",
                    }
                    for section in evidence
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_git_handoff(
        repository,
        pr_evidence_manifest=manifest_path,
        contracts_root=Path(__file__).parents[1] / "contracts",
    )

    draft = report["pull_request_draft"]
    assert draft["complete"] is True
    assert draft["missing_evidence_sections"] == []
    for _section, heading in (
        ("objective", "Ziel"),
        ("data_pipeline", "Datenpipeline"),
        ("agent_roles", "Agentenrollen"),
        ("training", "Training"),
        ("metrics", "Metriken"),
        ("risks", "Risiken"),
        ("hf_artifacts", "Hugging-Face-Artefakte"),
        ("final_status", "Finaler Status"),
    ):
        assert f"## {heading}" in draft["body"]
    assert "READY_FOR_AI_JUDGING" in draft["body"]
    assert evidence_hash in draft["body"]

    evidence_path.write_text('{"objective":"tampered"}\n', encoding="utf-8")
    with pytest.raises(GitHandoffError, match="hash mismatch"):
        build_git_handoff(
            repository,
            pr_evidence_manifest=manifest_path,
            contracts_root=Path(__file__).parents[1] / "contracts",
        )


def test_git_pr_evidence_contract_matches_final_report_identity_pointers() -> None:
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "git_pr_evidence_schema.json").read_text(
            encoding="utf-8"
        )
    )
    evidence = {
        "schema_version": 1,
        "status": "verified",
        "branch": "codex/gemma3-270m-ai-only-polishing",
        "commit": "a" * 40,
        "remote": {
            "name": "origin",
            "url": "https://github.com/MyButtermilk/Scriber.git",
        },
        "push": {
            "verified": True,
            "remote_branch": "codex/gemma3-270m-ai-only-polishing",
            "remote_head_sha": "a" * 40,
            "verified_at_utc": "2026-07-30T10:00:00Z",
        },
        "pull_request": {
            "verified": True,
            "id": 123,
            "url": "https://github.com/MyButtermilk/Scriber/pull/123",
            "state": "OPEN",
            "draft": True,
            "base_branch": "main",
            "head_branch": "codex/gemma3-270m-ai-only-polishing",
            "head_sha": "a" * 40,
            "verified_at_utc": "2026-07-30T10:01:00Z",
        },
    }

    assert list(Draft202012Validator(schema).iter_errors(evidence)) == []
    assert evidence["branch"]
    assert evidence["commit"] == evidence["push"]["remote_head_sha"]
    assert evidence["commit"] == evidence["pull_request"]["head_sha"]
    invalid = json.loads(json.dumps(evidence))
    del invalid["pull_request"]["head_sha"]
    assert list(Draft202012Validator(schema).iter_errors(invalid))
    assert verify_git_pr_evidence(
        evidence,
        contracts_root=Path(__file__).parents[1] / "contracts",
    ) == evidence
    mismatched = json.loads(json.dumps(evidence))
    mismatched["pull_request"]["head_sha"] = "b" * 40
    with pytest.raises(GitHandoffError, match="head SHA"):
        verify_git_pr_evidence(
            mismatched,
            contracts_root=Path(__file__).parents[1] / "contracts",
        )
