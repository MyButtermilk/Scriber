from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scriber_polishing.final_report import (
    FACT_SOURCES,
    PROVENANCE_ASSERTIONS,
    FinalReportError,
    build_final_report,
    canonical_json_bytes,
    provenance_signature_value,
    provenance_signed_payload,
    render_final_report_markdown,
    sha256_bytes,
    verify_final_report,
    write_immutable_final_report,
)

_FINGERPRINT = "sha256:" + "1" * 64
_TREE = "sha256:" + "2" * 64
_CONFIG_HASH = "3" * 64
_POLICY_BINDING = {
    "filename": "scriber-protection-policy.json",
    "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
    "policy_id": "gemma_lexical_v1",
    "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
    "tokenizer_identity_sha256": ("sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"),
}


def _write_json(root: Path, name: str, value: object) -> tuple[str, str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    return name, sha256_bytes(payload)


def _hub(training_hash: str) -> dict[str, Any]:
    categories = [
        "ai_gold_smoke",
        "protected_span",
        "business_post",
        "tax_law",
        "paragraphs",
        "headings",
        "lists",
        "units",
        "legal_citations",
        "address_pronouns",
        "identity",
    ]

    def legal(kind: str) -> dict[str, Any]:
        paths = (
            ["NOTICE", "GEMMA_LICENSE.md", "MODIFICATIONS.md"]
            if kind == "model"
            else ["LICENSE", "DATASET_SOURCES.md", "DATA_LICENSES.json"]
        )
        return {
            "status": "verified",
            "kind": (
                "gemma_derived_model"
                if kind == "model"
                else "synthetic_dataset"
            ),
            "files": [
                {"path": path, "sha256": str(index + 1) * 64}
                for index, path in enumerate(paths)
            ],
        }

    def checksums(kind: str) -> dict[str, Any]:
        digit = "9" if kind == "model" else "e"
        return {
            "status": "verified",
            "manifest_sha256": digit * 64,
            "payload_tree_sha256": digit * 64,
            "file_count": 20,
        }

    def split_inventory(prefix: str) -> dict[str, dict[str, Any]]:
        return {
            split: {
                "path": f"{prefix}/{split}.jsonl",
                "record_count": 1,
                "size_bytes": 100,
                "sha256": digit * 64,
            }
            for split, digit in (
                ("train", "1"),
                ("validation", "2"),
                ("test", "3"),
                ("challenge", "4"),
            )
        }

    def artifact(kind: str, revision_digit: str) -> dict[str, Any]:
        hash_chars = {
            "manifest": "a",
            "payload": "b",
            "upload": "c",
            "file": "d",
        }
        value = {
            "repo_id": (
                "Buttermilk03/scriber-gemma3-270m-polishing-de-v1"
                if kind == "model"
                else "Buttermilk03/scriber-transcript-polishing-de-v1"
            ),
            "repo_type": kind,
            "revision": revision_digit * 40,
            "manifest_sha256": hash_chars["manifest"] * 64,
            "payload_tree_sha256": hash_chars["payload"] * 64,
            "upload_tree_sha256": hash_chars["upload"] * 64,
            "uploaded_file_count": 1,
            "remote_files": [
                {
                    "path": "README.md",
                    "size_bytes": 10,
                    "expected_sha256": hash_chars["file"] * 64,
                    "hub_hash_kind": "hub_lfs_sha256",
                    "hub_hash": hash_chars["file"] * 64,
                    "remote_size_verified": True,
                    "remote_identity_verified": True,
                    "source_sha256_verified": True,
                }
            ],
            "verification_mode": "fresh_exact_revision_download",
            "exact_revision_downloaded": True,
            "source_snapshot_verified": True,
            "remote_identity_verified": True,
            "reload": {
                "status": "passed",
                "execution_isolation": "fresh_python_isolated",
                "fresh_cache": True,
                "network_disabled": True,
                "credentials_removed": True,
                "legal_files": legal(kind),
                "checksums": checksums(kind),
            },
        }
        if kind == "model":
            value["reload"]["protection_policy"] = dict(_POLICY_BINDING)
            value["reload"]["variant_release"] = {
                "status": "verified",
                "matrix_status": "RELEASE_MATRIX_PASSED",
                "passed": True,
                "report_sha256": "sha256:" + "a" * 64,
                "training_fingerprint": _FINGERPRINT,
                "tested_variants": ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"],
                "variants": ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"],
                "ineligible_variants": {},
                "qualification_failures": {},
                "reasons": [],
                "fresh_reload_report_sha256": {
                    variant: "sha256:" + digit * 64
                    for variant, digit in (
                        ("bf16", "1"),
                        ("int8", "2"),
                        ("int4_nf4", "3"),
                        ("Q8_0", "4"),
                        ("Q4_K_M", "5"),
                    )
                },
            }
            value["reload"]["runtime_reload"] = {
                "status": "verified",
                "source_equivalence": "artifact_bytes_equal_executed_package",
                "files": [
                    {"path": path, "sha256": digit * 64}
                    for path, digit in (
                        ("runtime/inference.py", "5"),
                        ("runtime/sst_parser.py", "6"),
                        ("runtime/renderers.py", "7"),
                        ("sst_v1_schema.json", "8"),
                    )
                ],
            }
            value["reload"]["regression"] = {
                "status": "passed",
                "file": "examples/regression_sample.jsonl",
                "cases_sha256": "sha256:" + "b" * 64,
                "case_count": 110,
                "minimum_case_count": 100,
                "category_counts": {category: 10 for category in categories},
                "source_partition_counts": {
                    "synthetic_non_holdout": 100,
                    "ai_gold_validation": 10,
                },
                "required_categories": categories,
                "ai_gold_smoke_count": 10,
                "ai_gold_smoke_minimum": 10,
                "private_data": False,
                "holdout_data": False,
                "exact_output_hash_matches": 110,
                "protected_span_checks": 110,
                "sst_round_trip_checks": 110,
                "renderer_checks": {
                    renderer: 110
                    for renderer in (
                        "sst",
                        "plain_text",
                        "markdown",
                        "html",
                        "html_clipboard",
                    )
                },
                "latency_ms": {
                    "sample_count": 110,
                    "p50": 1.0,
                    "p95": 2.0,
                    "p99": 3.0,
                    "maximum": 4.0,
                },
            }
        else:
            counts = {
                "train": 1,
                "validation": 1,
                "test": 1,
                "challenge": 1,
            }
            inventories = {
                "release": split_inventory("data"),
                "ai_gold": split_inventory("ai_gold"),
            }
            value["reload"]["split_counts"] = dict(counts)
            value["reload"]["split_inventory"] = inventories["release"]
            value["reload"]["ai_gold_split_counts"] = dict(counts)
            value["reload"]["ai_gold_split_inventory"] = inventories["ai_gold"]
            value["expected_split_counts"] = {
                "release": dict(counts),
                "ai_gold": dict(counts),
            }
            value["expected_split_inventory"] = inventories
        return value

    return {
        "schema_version": 6,
        "status": "verified_private",
        "namespace": "Buttermilk03",
        "visibility": "private",
        "preflight": {
            "status": "passed",
            "token_role": "write",
            "namespace_authorized": True,
            "identity_sha256": "4" * 64,
            "repository_access_confirmed": True,
        },
        "completed_training_report_sha256": training_hash,
        "training_run_fingerprint": _FINGERPRINT,
        "bf16_final_model_tree_sha256": _TREE,
        "protection_policy": dict(_POLICY_BINDING),
        "selected_checkpoint_publication_sha256": "sha256:" + "c" * 64,
        "selected_candidate_id": "checkpoint-1100",
        "selected_checkpoint_identity_sha256": "sha256:" + "d" * 64,
        "selected_checkpoint_tree_sha256": "sha256:" + "e" * 64,
        "selected_model_sha256": "sha256:" + "f" * 64,
        "selected_config_sha256": "sha256:" + "0" * 64,
        "base_tokenizer": {
            "id": "google/gemma-3-270m-it",
            "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        },
        "local_bundle_sha256": "5" * 64,
        "publication_binding_sha256": "6" * 64,
        "artifacts": {
            "model": artifact("model", "7"),
            "dataset": artifact("dataset", "8"),
        },
    }


def _facts() -> dict[str, dict[str, object]]:
    return {
        "git_branch": {"source": "git_pr", "pointer": "/branch"},
        "git_commit": {"source": "git_pr", "pointer": "/commit"},
        "pull_request_url": {"source": "git_pr", "pointer": "/pull_request/url"},
        "base_model": {"source": "final_training", "pointer": "/base_model"},
        "base_model_revision": {
            "source": "final_training",
            "pointer": "/base_revision",
        },
        "training_method": {"source": "final_training", "pointer": "/method"},
        "gpu": {"source": "preflight", "pointer": "/gpu/name"},
        "actual_vram_mib": {
            "source": "preflight",
            "pointer": "/gpu/memory_total_mib",
        },
        "operating_system": {
            "source": "preflight",
            "select": {
                "system": "/platform/system",
                "version": "/platform/version",
            },
        },
        "python_version": {"source": "preflight", "pointer": "/python/version"},
        "cuda_version": {"source": "preflight", "pointer": "/torch/cuda_runtime"},
        "package_versions": {"source": "preflight", "pointer": "/packages"},
        "final_hyperparameters": {
            "source": "final_training",
            "pointer": "/hyperparameters",
        },
        "training_duration_seconds": {
            "source": "final_training",
            "pointer": "/elapsed_seconds",
        },
        "peak_vram_mib": {
            "source": "final_training",
            "pointer": "/peak_vram_mib",
        },
        "synthetic_dataset_size": {"source": "dataset", "pointer": "/split_counts"},
        "canonical_document_count": {
            "source": "dataset",
            "pointer": "/canonical_count",
        },
        "ai_seed_count": {"source": "dataset", "pointer": "/ai_seed_count"},
        "ai_gold_example_count": {"source": "ai_gold", "pointer": "/record_count"},
        "rejected_example_count": {
            "source": "critic",
            "pointer": "/rejected_count",
        },
        "rejection_reasons": {
            "source": "critic",
            "pointer": "/rejection_reasons",
        },
        "generator_agents": {"source": "dataset", "pointer": "/generator_agents"},
        "critic_agents": {"source": "critic", "pointer": "/critic_agents"},
        "critic_disagreement_rate": {
            "source": "critic",
            "pointer": "/disagreement_rate",
        },
        "business_mail_rate": {"source": "dataset", "pointer": "/business_mail_rate"},
        "tax_law_rate": {"source": "dataset", "pointer": "/tax_law_rate"},
        "formatting_case_count": {
            "source": "dataset",
            "pointer": "/formatting_case_count",
        },
        "unit_case_count": {"source": "dataset", "pointer": "/unit_case_count"},
        "legal_citation_case_count": {
            "source": "dataset",
            "pointer": "/legal_citation_case_count",
        },
        "address_pronoun_case_count": {
            "source": "dataset",
            "pointer": "/address_pronoun_case_count",
        },
        "orthography_variant_count": {
            "source": "dataset",
            "pointer": "/orthography_variant_count",
        },
        "data_rejection_rate": {"source": "critic", "pointer": "/rejection_rate"},
        "automatic_metrics": {"source": "evaluation", "pointer": "/metrics"},
        "ai_gold_results": {"source": "ai_gold", "pointer": "/evaluation"},
        "terra_judge_results": {"source": "terra_judge", "pointer": "/results"},
        "sol_judge_results": {"source": "sol_judge", "pointer": "/results"},
        "critical_errors": {"source": "evaluation", "pointer": "/critical_errors"},
        "quantization": {"source": "quantization", "pointer": "/variant_summaries"},
        "model_size_bytes": {"source": "benchmark", "pointer": "/artifact_size_bytes"},
        "cold_start_seconds": {"source": "benchmark", "pointer": "/cold_start_seconds"},
        "warm_start_seconds": {"source": "benchmark", "pointer": "/warm_start_seconds"},
        "latency_p50_seconds": {
            "source": "benchmark",
            "pointer": "/latency/end_to_end_seconds/p50",
        },
        "latency_p95_seconds": {
            "source": "benchmark",
            "pointer": "/latency/end_to_end_seconds/p95",
        },
        "latency_p99_seconds": {
            "source": "benchmark",
            "pointer": "/latency/end_to_end_seconds/p99",
        },
        "hf_model_repository": {
            "source": "hub_verification",
            "pointer": "/artifacts/model/repo_id",
        },
        "model_revision": {
            "source": "hub_verification",
            "pointer": "/artifacts/model/revision",
        },
        "hf_dataset_repository": {
            "source": "hub_verification",
            "pointer": "/artifacts/dataset/repo_id",
        },
        "dataset_revision": {
            "source": "hub_verification",
            "pointer": "/artifacts/dataset/revision",
        },
        "visibility": {"source": "hub_verification", "pointer": "/visibility"},
        "reload_test": {
            "source": "hub_verification",
            "pointer": "/artifacts/model/reload",
        },
        "final_status": {"source": "release_status", "pointer": "/status"},
    }


def _base_sources(root: Path) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    reports: dict[str, dict[str, Any]] = {
        "preflight": {
            "schema_version": 1,
            "platform": {"system": "Windows", "version": "11"},
            "python": {"version": "3.12.13"},
            "packages": {"torch": "2.11.0", "transformers": "4.57.6"},
            "gpu": {"name": "RTX 4070 Laptop", "memory_total_mib": 8188},
            "torch": {"cuda_runtime": "12.8"},
        },
        "dataset": {
            "schema_version": 1,
            "split_counts": {
                "train": 120000,
                "validation": 10010,
                "test": 9170,
                "challenge": 5712,
            },
            "canonical_count": 18655,
            "ai_seed_count": 2665,
            "generator_agents": ["generator-1", "generator-2", "generator-3", "generator-4"],
            "business_mail_rate": 0.7,
            "tax_law_rate": 0.16,
            "formatting_case_count": 25000,
            "unit_case_count": 18000,
            "legal_citation_case_count": 14000,
            "address_pronoun_case_count": 16000,
            "orthography_variant_count": 8000,
        },
        "critic": {
            "schema_version": 1,
            "rejected_count": 335,
            "rejection_reasons": {"fact_change": 100, "format": 235},
            "critic_agents": ["terra-fact", "terra-style", "sol-adversarial"],
            "disagreement_rate": 0.04,
            "rejection_rate": 0.112,
        },
        "ai_gold": {
            "schema_version": 1,
            "record_count": 4000,
            "evaluation": {"passed": True, "critical_errors": 0},
        },
        "final_training": {
            "schema_version": 2,
            "base_model": "google/gemma-3-270m-it",
            "base_revision": "a" * 40,
            "method": "full_sft",
            "run_fingerprint": _FINGERPRINT,
            "hyperparameters": {"learning_rate": 0.00005, "max_sequence_length": 768},
            "elapsed_seconds": 80000.5,
            "peak_vram_mib": 6732,
            "final_model": {"inventory": {"tree_sha256": _TREE}},
        },
        "evaluation": {
            "schema_version": 1,
            "candidate": "fine_tuned",
            "metrics": {"sst_valid_rate": 1.0, "protected_span_exact_rate": 1.0},
            "critical_errors": 0,
        },
        "terra_judge": {
            "schema_version": 1,
            "results": {"passed": True, "acceptable_rate": 0.99},
        },
        "sol_judge": {
            "schema_version": 1,
            "results": {"passed": True, "acceptable_rate": 0.99},
        },
        "quantization": {
            "schema_version": 1,
            "training_fingerprint": _FINGERPRINT,
            "variant_summaries": {"bf16": {"passed": True}, "Q8_0": {"passed": True}},
        },
        "benchmark": {
            "schema_version": 2,
            "training_fingerprint": _FINGERPRINT,
            "artifact_size_bytes": 575513737,
            "cold_start_seconds": {"p50": 1.2, "p95": 1.4, "p99": 1.5},
            "warm_start_seconds": {"p50": 0.2, "p95": 0.3, "p99": 0.4},
            "latency": {"end_to_end_seconds": {"p50": 2.0, "p95": 3.0, "p99": 4.0}},
        },
        "git_pr": {
            "schema_version": 1,
            "status": "verified",
            "branch": "codex/gemma3-270m-ai-only-polishing",
            "commit": "b" * 40,
            "remote": {
                "name": "origin",
                "url": "https://github.com/MyButtermilk/Scriber.git",
            },
            "push": {
                "verified": True,
                "remote_branch": "codex/gemma3-270m-ai-only-polishing",
                "remote_head_sha": "b" * 40,
                "verified_at_utc": "2026-07-30T12:00:00Z",
            },
            "pull_request": {
                "verified": True,
                "id": 123,
                "url": "https://github.com/MyButtermilk/Scriber/pull/123",
                "state": "OPEN",
                "draft": False,
                "base_branch": "main",
                "head_branch": "codex/gemma3-270m-ai-only-polishing",
                "head_sha": "b" * 40,
                "verified_at_utc": "2026-07-30T12:00:00Z",
            },
        },
    }
    sources: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for role, value in reports.items():
        path, digest = _write_json(root, f"{role}.json", value)
        sources[role] = {
            "path": path,
            "sha256": digest,
            "schema_version": value["schema_version"],
        }
        hashes[role] = digest

    hub_value = _hub(hashes["final_training"])
    path, digest = _write_json(root, "hub.json", hub_value)
    sources["hub_verification"] = {
        "path": path,
        "sha256": digest,
        "schema_version": 6,
    }
    hashes["hub_verification"] = digest

    release_value = {
        "schema_version": 2,
        "candidate": "fine_tuned",
        "evaluation_config_sha256": _CONFIG_HASH,
        "status": "RELEASED",
        "passed": True,
        "components": {
            "completed_final_training": {
                "passed": True,
                "report_sha256": hashes["final_training"],
            },
            "private_hub_publication": {
                "passed": True,
                "report_sha256": hashes["hub_verification"],
            },
        },
        "failed_components": [],
        "evaluation_slice_reports": {"test": {"passed": True}},
        "status_basis": {
            "completed_final_training_verified": True,
            "private_hub_upload_and_reload_verified": True,
            "ai_judging_pending": False,
            "all_release_components_passed": True,
        },
    }
    path, digest = _write_json(root, "release.json", release_value)
    sources["release_status"] = {
        "path": path,
        "sha256": digest,
        "schema_version": 2,
    }
    hashes["release_status"] = digest
    return sources, hashes


def _add_provenance(
    root: Path,
    sources: dict[str, dict[str, object]],
    hashes: dict[str, str],
    *,
    limitations: list[dict[str, object]] | None = None,
) -> None:
    declaration: dict[str, Any] = {
        "schema_version": 1,
        "declaration_id": "release-provenance-001",
        "issued_at_utc": "2026-07-30T12:30:00Z",
        "assertions": {key: True for key in PROVENANCE_ASSERTIONS},
        "evidence_sha256s": dict(sorted(hashes.items())),
        "known_limitations": limitations or [],
        "integration_recommendation": "Zunächst kontrolliert lokal integrieren.",
    }
    signed_hash = sha256_bytes(canonical_json_bytes(provenance_signed_payload(declaration)))
    declaration["signed_payload_sha256"] = signed_hash
    declaration["signature"] = {
        "scheme": "canonical-sha256-attestation-v1",
        "signer_id": "release-orchestrator",
        "value": provenance_signature_value(
            signed_payload_sha256=signed_hash,
            signer_id="release-orchestrator",
        ),
    }
    path, digest = _write_json(root, "provenance.json", declaration)
    sources["provenance"] = {
        "path": path,
        "sha256": digest,
        "schema_version": 1,
    }


def _manifest(root: Path) -> dict[str, object]:
    sources, hashes = _base_sources(root)
    _add_provenance(root, sources, hashes)
    return {"schema_version": 1, "sources": sources, "fact_bindings": _facts()}


def test_builds_final_evidence_bound_report_and_markdown(tmp_path: Path) -> None:
    report = build_final_report(_manifest(tmp_path), source_root=tmp_path)

    assert report["report_state"] == "FINAL"
    assert report["missing_required_sources"] == []
    assert report["missing_required_fields"] == []
    assert report["facts"]["git_commit"]["value"] == "b" * 40
    assert report["facts"]["actual_vram_mib"]["value"] == 8188
    assert all(item["value"] is True for item in report["confirmations"].values())
    assert verify_final_report(report) == report

    markdown = render_final_report_markdown(report, json_sha256="sha256:" + "9" * 64)
    assert "# Scriber Polishing – Abschlussbericht" in markdown
    assert "Berichtszustand: **FINAL**" in markdown
    assert "Tatsächlicher VRAM (MiB)" in markdown


def test_known_unknown_requires_signed_provenance_limitation(tmp_path: Path) -> None:
    sources, hashes = _base_sources(tmp_path)
    _add_provenance(
        tmp_path,
        sources,
        hashes,
        limitations=[
            {
                "id": "size-tool-limit",
                "description": "Dateigröße konnte vom gebundenen Benchmark nicht ermittelt werden.",
                "affected_fields": ["model_size_bytes"],
            }
        ],
    )
    facts = _facts()
    del facts["model_size_bytes"]
    report = build_final_report(
        {"schema_version": 1, "sources": sources, "fact_bindings": facts},
        source_root=tmp_path,
    )

    assert report["report_state"] == "FINAL"
    assert report["facts"]["model_size_bytes"]["state"] == "unknown"
    assert report["facts"]["model_size_bytes"]["limitation_id"] == "size-tool-limit"


def test_missing_value_is_not_silently_zero_or_success(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    del manifest["fact_bindings"]["training_duration_seconds"]

    report = build_final_report(manifest, source_root=tmp_path)

    assert report["report_state"] == "NOT_FINAL"
    assert "training_duration_seconds" in report["missing_required_fields"]
    assert "training_duration_seconds" not in report["facts"]


def test_rejects_tampered_source_hash(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["sources"]["dataset"]["sha256"] = "sha256:" + "0" * 64

    with pytest.raises(FinalReportError, match="dataset source hash mismatch"):
        build_final_report(manifest, source_root=tmp_path)


def test_rejects_tampered_provenance_signature(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    provenance_path = tmp_path / "provenance.json"
    declaration = json.loads(provenance_path.read_text(encoding="utf-8"))
    declaration["integration_recommendation"] = "Manipuliert"
    path, digest = _write_json(tmp_path, "provenance-tampered.json", declaration)
    manifest["sources"]["provenance"]["path"] = path
    manifest["sources"]["provenance"]["sha256"] = digest

    with pytest.raises(FinalReportError, match="signed_payload_sha256"):
        build_final_report(manifest, source_root=tmp_path)


def test_rejects_git_pr_head_that_differs_from_pushed_commit(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    git_path = tmp_path / "git_pr.json"
    git = json.loads(git_path.read_text(encoding="utf-8"))
    git["pull_request"]["head_sha"] = "c" * 40
    path, digest = _write_json(tmp_path, "git-pr-mismatch.json", git)
    manifest["sources"]["git_pr"]["path"] = path
    manifest["sources"]["git_pr"]["sha256"] = digest
    # Keep the provenance binding exact so the test reaches the semantic check.
    provenance_path = tmp_path / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["evidence_sha256s"]["git_pr"] = digest
    signed_hash = sha256_bytes(canonical_json_bytes(provenance_signed_payload(provenance)))
    provenance["signed_payload_sha256"] = signed_hash
    provenance["signature"]["value"] = provenance_signature_value(
        signed_payload_sha256=signed_hash,
        signer_id=provenance["signature"]["signer_id"],
    )
    p_path, p_digest = _write_json(tmp_path, "provenance-updated.json", provenance)
    manifest["sources"]["provenance"]["path"] = p_path
    manifest["sources"]["provenance"]["sha256"] = p_digest

    with pytest.raises(FinalReportError, match="pull-request head SHA"):
        build_final_report(manifest, source_root=tmp_path)


def test_hub_and_release_must_validate_exact_checked_in_contracts(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    hub_path = tmp_path / "hub.json"
    hub = json.loads(hub_path.read_text(encoding="utf-8"))
    hub["schema_version"] = 2
    path, digest = _write_json(tmp_path, "hub-invalid.json", hub)
    manifest["sources"]["hub_verification"].update({"path": path, "sha256": digest, "schema_version": 2})

    with pytest.raises(FinalReportError, match="hub_verification source schema failed"):
        build_final_report(manifest, source_root=tmp_path)


def test_identity_fact_cannot_bind_unrelated_pointer(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["fact_bindings"]["model_revision"]["pointer"] = "/artifacts/dataset/revision"

    with pytest.raises(FinalReportError, match="exact contract pointer"):
        build_final_report(manifest, source_root=tmp_path)


def test_immutable_writer_refuses_existing_destination(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    report = build_final_report(_manifest(source_root), source_root=source_root)
    json_path = tmp_path / "output" / "final.json"
    markdown_path = tmp_path / "output" / "final.md"

    json_hash, markdown_hash = write_immutable_final_report(
        report,
        json_path=json_path,
        markdown_path=markdown_path,
    )

    assert json_hash.startswith("sha256:")
    assert markdown_hash.startswith("sha256:")
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_state"] == "FINAL"
    with pytest.raises(FinalReportError, match="refusing to overwrite"):
        write_immutable_final_report(
            copy.deepcopy(report),
            json_path=json_path,
            markdown_path=markdown_path,
        )


def test_cli_writes_both_final_outputs(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    manifest = _manifest(source_root)
    input_path = tmp_path / "bindings.json"
    input_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "result" / "final.json"
    markdown_path = tmp_path / "result" / "final.md"
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_final_report.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(input_path),
            "--source-root",
            str(source_root),
            "--output-json",
            str(json_path),
            "--output-markdown",
            str(markdown_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "final_report=final" in completed.stdout
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_state"] == "FINAL"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Scriber Polishing – Abschlussbericht")


def test_section_49_fact_inventory_is_complete() -> None:
    assert set(FACT_SOURCES) == {
        "git_branch",
        "git_commit",
        "pull_request_url",
        "base_model",
        "base_model_revision",
        "training_method",
        "gpu",
        "actual_vram_mib",
        "operating_system",
        "python_version",
        "cuda_version",
        "package_versions",
        "final_hyperparameters",
        "training_duration_seconds",
        "peak_vram_mib",
        "synthetic_dataset_size",
        "canonical_document_count",
        "ai_seed_count",
        "ai_gold_example_count",
        "rejected_example_count",
        "rejection_reasons",
        "generator_agents",
        "critic_agents",
        "critic_disagreement_rate",
        "business_mail_rate",
        "tax_law_rate",
        "formatting_case_count",
        "unit_case_count",
        "legal_citation_case_count",
        "address_pronoun_case_count",
        "orthography_variant_count",
        "data_rejection_rate",
        "automatic_metrics",
        "ai_gold_results",
        "terra_judge_results",
        "sol_judge_results",
        "critical_errors",
        "quantization",
        "model_size_bytes",
        "cold_start_seconds",
        "warm_start_seconds",
        "latency_p50_seconds",
        "latency_p95_seconds",
        "latency_p99_seconds",
        "hf_model_repository",
        "model_revision",
        "hf_dataset_repository",
        "dataset_revision",
        "visibility",
        "reload_test",
        "final_status",
        "known_limitations",
        "integration_recommendation",
    }
