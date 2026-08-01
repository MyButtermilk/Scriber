from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scriber_polishing.hub_publication as hub_publication
from scriber_polishing.hub_publication import (
    MANIFEST_FILENAME,
    PublicationError,
    isolated_reload,
    load_publication_config,
    offline_reload_dataset,
    offline_reload_model,
    prepare_publication_bundle,
    prepare_publication_manifest,
    publish_private_bundle,
    validate_publication_config,
)
from scriber_polishing.hub_regression import materialize_hub_regression_suite

_POLICY_ID = "gemma_lexical_v1"
_POLICY_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
_TOKENIZER_IDENTITY_SHA256 = "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"


def test_repository_access_probe_adapts_to_current_hub_signature() -> None:
    calls: list[tuple[str, str, str]] = []

    def current_auth_check(
        repo_id: str,
        *,
        repo_type: str | None = None,
        token: str | None = None,
    ) -> None:
        calls.append((repo_id, str(repo_type), str(token)))

    hub_publication._auth_check_repository_access(
        current_auth_check,
        repo_id="Buttermilk03/model",
        repo_type="model",
        token="token",
    )

    assert calls == [("Buttermilk03/model", "model", "token")]


def test_repository_access_probe_requests_write_when_supported() -> None:
    calls: list[dict[str, Any]] = []

    def legacy_auth_check(repo_id: str, **kwargs: Any) -> None:
        calls.append({"repo_id": repo_id, **kwargs})

    hub_publication._auth_check_repository_access(
        legacy_auth_check,
        repo_id="Buttermilk03/model",
        repo_type="model",
        token="token",
    )

    assert calls == [
        {
            "repo_id": "Buttermilk03/model",
            "repo_type": "model",
            "token": "token",
            "write": True,
        }
    ]


def _policy_requirement() -> dict[str, str]:
    return {
        "filename": "scriber-protection-policy.json",
        "artifact_sha256": _POLICY_ARTIFACT_SHA256,
        "policy_id": _POLICY_ID,
        "policy_sha256": _POLICY_SHA256,
    }


def _policy_binding() -> dict[str, str]:
    return {
        **_policy_requirement(),
        "tokenizer_identity_sha256": _TOKENIZER_IDENTITY_SHA256,
    }


def _prefixed_hash(character: str) -> str:
    return "sha256:" + (character * 64)


def _variant_release_report() -> dict[str, Any]:
    variants = ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"]
    summaries = {}
    for index, variant in enumerate(variants):
        character = str(index + 1)
        summaries[variant] = {
            "artifact_tree_sha256": _prefixed_hash(character),
            "artifact_manifest_sha256": _prefixed_hash(character),
            "reload_report_sha256": _prefixed_hash(character),
            "predictions_sha256": _prefixed_hash(character),
            "evaluation_report_sha256": _prefixed_hash(character),
            "delta_report_sha256": _prefixed_hash(character),
            "benchmark_report_sha256": _prefixed_hash(character),
            "prediction_count": 100,
            "maximum_absolute_metric_drop": 0.0,
            "new_critical_errors": 0,
            "persistent_warm_runtime": True,
            "evidence": [
                "artifact",
                "reload",
                "predictions",
                "evaluation",
                "delta",
                "benchmark",
            ],
        }
    return {
        "schema_version": 4,
        "status": "RELEASE_MATRIX_PASSED",
        "passed": True,
        "baseline_variant": "bf16",
        "training_fingerprint": _prefixed_hash("c"),
        "protection_policy": _policy_binding(),
        "tested_variants": variants,
        "variants": variants,
        "variant_summaries": summaries,
        "ineligible_variants": {},
        "qualification_failures": {},
        "reasons": [],
    }


def _publication_config() -> dict[str, Any]:
    return load_publication_config(Path(__file__).parents[1] / "configs" / "publication.yaml")


def _write_model_artifact(root: Path) -> None:
    root.mkdir()
    (root / "README.md").write_text("# Private model\n", encoding="utf-8")
    (root / "config.json").write_text(
        json.dumps(
            {
                "model_type": "gemma3_text",
                "scriber_protection_policy_id": _POLICY_ID,
                "scriber_protection_policy_sha256": _POLICY_SHA256,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "model.safetensors").write_bytes(b"safe-model-weights")
    shutil.copyfile(
        Path(__file__).parents[1] / "artifacts" / "protection" / "gemma_lexical_v1.json",
        root / "scriber-protection-policy.json",
    )
    (root / "NOTICE").write_text(
        "Gemma is provided under and subject to the Gemma Terms of Use found "
        "at ai.google.dev/gemma/terms\n\n"
        "Scriber is a modified derivative of Google Gemma.\n",
        encoding="utf-8",
    )
    (root / "GEMMA_LICENSE.md").write_text(
        "# Gemma Terms of Use\n\n"
        "Authoritative source: https://ai.google.dev/gemma/terms\n\n"
        "Section 1: DEFINITIONS\n\n"
        "Section 2: ELIGIBILITY AND USAGE\n\n"
        "Section 3: DISTRIBUTION AND RESTRICTIONS\n\n"
        "Section 3.2 Use Restrictions\n\n"
        "Gemma Prohibited Use Policy\n\n"
        "Section 4: ADDITIONAL PROVISIONS\n\n"
        "Appendix\n\nGemma 3\n\n" + ("Fixture agreement body for structural validation.\n" * 120),
        encoding="utf-8",
    )
    (root / "MODIFICATIONS.md").write_text(
        "# Scriber modifications\n\n"
        "Fine-tuned from google/gemma-3-270m-it at revision "
        "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3 for Scriber.\n"
        "Modified files include config.json and model.safetensors.\n",
        encoding="utf-8",
    )
    (root / "generation_config.json").write_text("{}\n", encoding="utf-8")
    (root / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (root / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    shutil.copyfile(
        Path(__file__).parents[1] / "contracts" / "sst_v1_schema.json",
        root / "sst_v1_schema.json",
    )
    (root / "MODEL_CARD.md").write_text("# Private model card\n", encoding="utf-8")
    (root / "training_config.yaml").write_text("bf16: true\n", encoding="utf-8")
    (root / "package_versions.json").write_text("{}\n", encoding="utf-8")
    runtime = root / "runtime"
    runtime.mkdir()
    runtime_sources = {
        "inference.py": "inference.py",
        "sst_parser.py": "sst_parser.py",
        "renderers.py": "target_renderer.py",
    }
    for destination, source in runtime_sources.items():
        shutil.copyfile(
            Path(__file__).parents[1] / "src" / "scriber_polishing" / source,
            runtime / destination,
        )
    reports = root / "reports"
    reports.mkdir()
    for name in (
        "data_quality_report.json",
        "ai_data_factory_report.json",
        "ai_gold_report.json",
        "evaluation_report.json",
        "terra_judge_report.json",
        "sol_judge_report.json",
        "benchmark_report.json",
    ):
        (reports / name).write_text("{}\n", encoding="utf-8")
    (reports / "variant_release_matrix_report.json").write_text(
        json.dumps(
            _variant_release_report(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    examples = root / "examples"
    examples.mkdir()
    (examples / "inference.json").write_text("{}\n", encoding="utf-8")
    categories = _publication_config()["artifacts"]["model"]["reload"]["regression"]["required_categories"]
    sources = []
    predictions = []
    for index in range(110):
        category = categories[index % len(categories)]
        protected = f"AZ-{index:04d}"
        case_id = f"hub-{index:04d}"
        sources.append(
            {
                "schema_version": 1,
                "case_id": case_id,
                "source_kind": ("ai_gold_smoke" if category == "ai_gold_smoke" else "synthetic_regression"),
                "source_partition": ("ai_gold_validation" if category == "ai_gold_smoke" else "synthetic_non_holdout"),
                "category": category,
                "source_text": f"bitte bestätigen sie {protected}",
                "protected_spans": [protected],
                "private_data": False,
                "holdout": False,
            }
        )
        predictions.append(
            {
                "case_id": case_id,
                "prediction_sst": (f"[DOC]\n[P]Bitte bestätigen Sie {protected}.[/P]\n[/DOC]"),
            }
        )
    source_path = root.parent / f"{root.name}-hub-sources.jsonl"
    prediction_path = root.parent / f"{root.name}-hub-predictions.jsonl"
    source_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in sources),
        encoding="utf-8",
    )
    prediction_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in predictions),
        encoding="utf-8",
    )
    materialize_hub_regression_suite(
        source_path,
        prediction_path,
        examples / "regression_sample.jsonl",
        policy=_publication_config()["artifacts"]["model"]["reload"]["regression"],
    )
    source_path.unlink()
    prediction_path.unlink()


def _write_selected_checkpoint_publication(path: Path, model_root: Path) -> None:
    config_hash = "sha256:" + hashlib.sha256((model_root / "config.json").read_bytes()).hexdigest()
    weights = []
    for weight in sorted(model_root.glob("*.safetensors")):
        weights.append(
            {
                "path": weight.name,
                "bytes": weight.stat().st_size,
                "sha256": "sha256:" + hashlib.sha256(weight.read_bytes()).hexdigest(),
            }
        )
    model_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                weights,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    checkpoint = {
        "candidate_id": "checkpoint-10",
        "checkpoint": "checkpoint-10",
        "global_step": 10,
        "run_fingerprint": _prefixed_hash("c"),
        "identity_sha256": _prefixed_hash("e"),
        "tree_sha256": _prefixed_hash("a"),
        "config_sha256": config_hash,
        "model_sha256": model_hash,
        "file_count": 2,
        "total_bytes": (
            (model_root / "config.json").stat().st_size
            + sum(weight.stat().st_size for weight in model_root.glob("*.safetensors"))
        ),
        "candidate_evidence_sha256": _prefixed_hash("f"),
        "deterministic_automatic_report_sha256": _prefixed_hash("1"),
        "eval_loss": 0.1,
    }
    publication = {
        "schema_version": 2,
        "status": "ready_for_publication",
        "selection_handoff_sha256": _prefixed_hash("2"),
        "selected_candidate_id": "checkpoint-10",
        "checkpoint": checkpoint,
        "base_tokenizer": {
            "id": "google/gemma-3-270m-it",
            "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        },
        "fresh_reload": {
            "status": "passed",
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "tokenizer_source": "pinned_base_tokenizer",
            "model_tree_sha256": checkpoint["tree_sha256"],
            "model_config_sha256": config_hash,
            "model_sha256": model_hash,
            "generated_tokens": 8,
        },
        "fresh_inventory": {
            "status": "passed",
            "tree_sha256": checkpoint["tree_sha256"],
            "config_sha256": config_hash,
            "model_sha256": model_hash,
            "file_count": checkpoint["file_count"],
            "total_bytes": checkpoint["total_bytes"],
        },
        "integration_hooks": [
            "scriber_polishing.hub_publication.publish_private_bundle",
            "scriber_polishing.quantization.inventory_bf16_checkpoint",
            "scriber_polishing.final_report.build_final_report",
        ],
    }
    path.write_text(
        json.dumps(publication, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _selected_checkpoint_path(root: Path, model_root: Path) -> Path:
    path = root / "selected-checkpoint-publication.json"
    _write_selected_checkpoint_publication(path, model_root)
    return path


def _dataset_record(split: str, example_id: str) -> dict[str, Any]:
    return {
        "example_id": example_id,
        "split": split,
        "source_text": "wir senden den bericht",
        "target_sst": "<p>Wir senden den Bericht.</p>",
        "target_plain_text": "Wir senden den Bericht.",
        "target_ast": {"blocks": []},
        "semantic_plan": {"facts": []},
        "critic_decisions": [{"acceptable": True}],
        "validation_scores": {"deterministic": 1.0},
        "provenance": {
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
            "canonical_generator_id": "local-fixture",
            "canonical_generator_version": "1",
            "plan_generator_agent": "fixture-plan-agent",
            "target_writer_agent": "fixture-target-agent",
        },
    }


def _write_dataset_artifact(root: Path) -> None:
    root.mkdir()
    (root / "README.md").write_text("# Private synthetic dataset\n", encoding="utf-8")
    for group in ("data", "ai_gold"):
        data_root = root / group
        data_root.mkdir()
        for split in ("train", "validation", "test", "challenge"):
            payload = json.dumps(
                _dataset_record(split, f"{group}-{split}-1"),
                ensure_ascii=False,
                sort_keys=True,
            )
            (data_root / f"{split}.jsonl").write_text(
                payload + "\n",
                encoding="utf-8",
            )
    (root / "DATASET_CARD.md").write_text(
        "# Private synthetic dataset\n",
        encoding="utf-8",
    )
    (root / "LICENSE").write_text(
        "Apache License\nVersion 2.0, January 2004\n",
        encoding="utf-8",
    )
    (root / "DATASET_SOURCES.md").write_text(
        "# Sources\n\n"
        "Synthetic data only. There is no private Scriber data, no human "
        "curation, and no generative model API input.\n",
        encoding="utf-8",
    )
    (root / "DATA_LICENSES.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_license": "Apache-2.0",
                "sources": [
                    {
                        "id": "scriber-synthetic-v1",
                        "origin": "deterministic local generators",
                        "license": "Apache-2.0",
                        "synthetic_only": True,
                        "human_curation": False,
                        "private_data": False,
                        "license_reviewed": True,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    reports = root / "reports"
    reports.mkdir()
    for name in (
        "data_quality_report.json",
        "ai_data_factory_report.json",
        "ai_gold_report.json",
    ):
        (reports / name).write_text("{}\n", encoding="utf-8")


def _prepare_fixture_manifest(root: Path, kind: str) -> dict[str, Any]:
    definition = deepcopy(_publication_config()["artifacts"][kind])
    if kind == "model":
        definition["required_patterns"] = [
            pattern
            for pattern in definition["required_patterns"]
            if pattern
            not in {
                "reports/completed_final_training_binding.json",
                "reports/selected_checkpoint_binding.json",
            }
        ]
    return prepare_publication_manifest(root, definition)


def _passing_regression_result(
    snapshot: Path,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    policy = definition["reload"]["regression"]
    _cases, evidence = hub_publication.load_hub_regression_suite(
        snapshot / policy["file"],
        policy,
    )
    count = evidence["case_count"]
    return {
        "status": "passed",
        **evidence,
        "exact_output_hash_matches": count,
        "protected_span_checks": count,
        "sst_round_trip_checks": count,
        "renderer_checks": {
            renderer: count
            for renderer in (
                "sst",
                "plain_text",
                "markdown",
                "html",
                "html_clipboard",
            )
        },
        "latency_ms": {
            "sample_count": count,
            "p50": 1.0,
            "p95": 2.0,
            "p99": 3.0,
            "maximum": 4.0,
        },
    }


def _verified_reload_fixture(
    kind: str,
    snapshot: Path,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    if kind == "dataset":
        result = offline_reload_dataset(snapshot, definition)
        result["loader"] = "dataset_fixture"
        return result
    return {
        "status": "passed",
        "loader": "model_fixture",
        "protection_policy": hub_publication.verify_frozen_lexical_policy_artifact(
            snapshot,
            expected=definition["protection_policy"],
        ),
        "legal_files": hub_publication._verify_legal_files(
            snapshot,
            definition,
            artifact_kind="model",
        ),
        "checksums": hub_publication._verify_checksums(snapshot),
        "variant_release": hub_publication._variant_release_binding(
            snapshot,
            definition,
        ),
        "runtime_reload": hub_publication._verify_runtime_sources(snapshot),
        "regression": _passing_regression_result(snapshot, definition),
    }


def _write_completed_training_report(path: Path) -> None:
    fingerprint = "sha256:" + ("c" * 64)
    report = {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "run_kind": "final",
        "run_fingerprint": fingerprint,
        "bf16": True,
        "max_steps": 10,
        "early_stopping": True,
        "save_total_limit": 4,
        "checkpoint_state": {
            "global_step": 10,
            "best_metric": 0.1,
            "best_model_checkpoint": "checkpoint-10",
            "last_checkpoint": "checkpoint-10",
            "surviving_checkpoints": [
                {
                    "checkpoint": "checkpoint-10",
                    "global_step": 10,
                    "run_fingerprint": fingerprint,
                    "identity_sha256": "sha256:" + ("e" * 64),
                    "trainer_state_sha256": "sha256:" + ("f" * 64),
                }
            ],
        },
        "final_model": {
            "run_fingerprint": fingerprint,
            "inventory": {
                "tree_sha256": "sha256:" + ("a" * 64),
                "files": [
                    {
                        "path": "scriber-protection-policy.json",
                        "sha256": _POLICY_ARTIFACT_SHA256,
                    }
                ],
            },
            "reload_verification": {
                "protection_policy_sha256": _POLICY_ARTIFACT_SHA256,
            },
        },
        "protection_policy": {
            "policy_id": _POLICY_ID,
            "policy_sha256": _POLICY_SHA256,
            "sha256": _POLICY_ARTIFACT_SHA256,
        },
        "training_completed": True,
    }
    path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")


class _FakeHub:
    def __init__(self, remote_root: Path) -> None:
        self.remote_root = remote_root
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.repo_private: dict[str, bool] = {}
        self.repo_types: dict[str, str] = {}
        self.revisions: dict[str, str] = {}

    def whoami(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("whoami", dict(kwargs)))
        return {
            "name": "Buttermilk03",
            "auth": {"accessToken": {"role": "write"}},
        }

    def auth_check(self, repo_id: str, **kwargs: Any) -> None:
        self.calls.append(("auth_check", {"repo_id": repo_id, **kwargs}))

    def create_repo(self, repo_id: str, **kwargs: Any) -> str:
        self.calls.append(("create_repo", {"repo_id": repo_id, **kwargs}))
        self.repo_private.setdefault(repo_id, bool(kwargs["private"]))
        self.repo_types[repo_id] = str(kwargs["repo_type"])
        return f"https://huggingface.co/{repo_id}"

    def upload_folder(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("upload_folder", dict(kwargs)))
        repo_id = str(kwargs["repo_id"])
        source = Path(kwargs["folder_path"])
        destination = self.remote_root / repo_id.replace("/", "__")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        revision = ("a" if kwargs["repo_type"] == "model" else "b") * 40
        self.revisions[repo_id] = revision
        return SimpleNamespace(oid=revision)

    def _info(self, repo_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append((f"{self.repo_types[repo_id]}_info", {"repo_id": repo_id, **kwargs}))
        revision = self.revisions.get(repo_id, ("1" if self.repo_types[repo_id] == "model" else "2") * 40)
        remote = self.remote_root / repo_id.replace("/", "__")
        siblings = []
        if remote.exists():
            for path in sorted(remote.rglob("*")):
                if not path.is_file():
                    continue
                content = path.read_bytes()
                sha256 = hashlib.sha256(content).hexdigest()
                git_blob = hashlib.sha1(usedforsecurity=False)
                git_blob.update(f"blob {len(content)}\0".encode("ascii"))
                git_blob.update(content)
                siblings.append(
                    SimpleNamespace(
                        rfilename=path.relative_to(remote).as_posix(),
                        size=len(content),
                        blob_id=git_blob.hexdigest(),
                        lfs=(SimpleNamespace(sha256=sha256) if path.suffix == ".safetensors" else None),
                    )
                )
        return SimpleNamespace(
            id=repo_id,
            private=self.repo_private[repo_id],
            sha=revision,
            siblings=siblings,
        )

    def model_info(self, repo_id: str, **kwargs: Any) -> SimpleNamespace:
        return self._info(repo_id, **kwargs)

    def dataset_info(self, repo_id: str, **kwargs: Any) -> SimpleNamespace:
        return self._info(repo_id, **kwargs)

    def snapshot_download(self, repo_id: str, **kwargs: Any) -> str:
        self.calls.append(("snapshot_download", {"repo_id": repo_id, **kwargs}))
        assert kwargs["revision"] == self.revisions[repo_id]
        destination = Path(kwargs["local_dir"])
        shutil.copytree(self.remote_root / repo_id.replace("/", "__"), destination)
        (destination / ".gitattributes").write_text("*.safetensors filter=lfs\n", encoding="utf-8")
        cache = destination / ".cache" / "huggingface"
        cache.mkdir(parents=True)
        (cache / "download-state.json").write_text("{}\n", encoding="utf-8")
        return str(destination.resolve())


def test_checked_in_publication_config_is_fixed_private_buttermilk03_policy() -> None:
    config_path = Path(__file__).parents[1] / "configs" / "publication.yaml"

    config = load_publication_config(config_path)

    assert config["namespace"] == "Buttermilk03"
    assert config["visibility"] == "private"
    assert config["token_env"] == "HF_TOKEN"
    assert config["schema_version"] == 4
    assert config["artifacts"]["model"]["reload"]["device"] == "cuda"
    assert config["artifacts"]["model"]["protection_policy"] == _policy_requirement()
    assert config["artifacts"]["model"]["repo_id"] == ("Buttermilk03/scriber-gemma3-270m-polishing-de-v1")
    assert config["artifacts"]["dataset"]["repo_id"] == ("Buttermilk03/scriber-transcript-polishing-de-v1")
    assert config["artifacts"]["model"]["repo_type"] == "model"
    assert config["artifacts"]["dataset"]["repo_type"] == "dataset"
    model_patterns = set(config["artifacts"]["model"]["required_patterns"])
    assert {
        "MODEL_CARD.md",
        "GEMMA_LICENSE.md",
        "runtime/sst_parser.py",
        "runtime/renderers.py",
        "reports/data_quality_report.json",
        "reports/ai_data_factory_report.json",
        "reports/ai_gold_report.json",
        "reports/evaluation_report.json",
        "reports/terra_judge_report.json",
        "reports/sol_judge_report.json",
        "reports/benchmark_report.json",
        "reports/variant_release_matrix_report.json",
        "reports/completed_final_training_binding.json",
        "reports/selected_checkpoint_binding.json",
        "checksums.json",
        "examples/inference.json",
        "examples/regression_sample.jsonl",
    } <= model_patterns
    dataset_patterns = set(config["artifacts"]["dataset"]["required_patterns"])
    assert {
        "DATASET_CARD.md",
        "DATASET_SOURCES.md",
        "DATA_LICENSES.json",
        "ai_gold/train.jsonl",
        "ai_gold/validation.jsonl",
        "ai_gold/test.jsonl",
        "ai_gold/challenge.jsonl",
    } <= dataset_patterns
    assert set(config["artifacts"]["dataset"]["reload"]["ai_gold_split_files"]) == {
        "train",
        "validation",
        "test",
        "challenge",
    }
    for definition in config["artifacts"].values():
        assert definition["privacy"] == {
            "synthetic_ai_only": True,
            "human_curation": False,
            "contains_private_scriber_data": False,
            "contains_secrets": False,
        }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.update({"namespace": "someone-else"}),
        lambda config: config.update({"visibility": "public"}),
        lambda config: config["artifacts"]["model"].update({"repo_id": "someone-else/model"}),
        lambda config: config["artifacts"]["dataset"]["privacy"].update({"contains_private_scriber_data": True}),
        lambda config: config["artifacts"]["model"]["protection_policy"].update(
            {"artifact_sha256": "sha256:" + ("0" * 64)}
        ),
        lambda config: config["artifacts"]["model"]["reload"].update({"device": "cpu"}),
    ],
)
def test_publication_config_rejects_non_private_or_untrusted_targets(mutation: Any) -> None:
    config = _publication_config()
    mutation(config)

    with pytest.raises(ValueError):
        validate_publication_config(config)


def test_manifest_is_deterministic_content_bound_and_immutable(tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    definition = deepcopy(_publication_config()["artifacts"]["model"])
    definition["required_patterns"] = [
        pattern
        for pattern in definition["required_patterns"]
        if pattern
        not in {
            "reports/completed_final_training_binding.json",
            "reports/selected_checkpoint_binding.json",
        }
    ]

    first = prepare_publication_manifest(
        artifact,
        definition,
        manifest_filename=MANIFEST_FILENAME,
    )
    second = prepare_publication_manifest(
        artifact,
        definition,
        manifest_filename=MANIFEST_FILENAME,
    )

    assert first == second
    assert first["artifact_kind"] == "model"
    assert first["repo_id"] == definition["repo_id"]
    assert first["visibility"] == "private"
    assert first["protection_policy"] == _policy_binding()
    assert first["payload"]["file_count"] >= 20
    payload_paths = {item["path"] for item in first["payload"]["files"]}
    assert {
        "README.md",
        "NOTICE",
        "GEMMA_LICENSE.md",
        "MODIFICATIONS.md",
        "config.json",
        "model.safetensors",
        "scriber-protection-policy.json",
        "checksums.json",
        "examples/regression_sample.jsonl",
        "reports/variant_release_matrix_report.json",
    } <= payload_paths
    assert all(len(item["sha256"]) == 64 for item in first["payload"]["files"])
    manifest_path = artifact / MANIFEST_FILENAME
    original_bytes = manifest_path.read_bytes()

    (artifact / "config.json").write_text('{"model_type":"changed"}\n', encoding="utf-8")
    with pytest.raises(
        PublicationError,
        match="immutable (?:checksums manifest|manifest)",
    ):
        prepare_publication_manifest(
            artifact,
            definition,
            manifest_filename=MANIFEST_FILENAME,
        )
    assert manifest_path.read_bytes() == original_bytes


def test_private_candidate_variant_matrix_is_publishable_with_bound_failures(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    report_path = artifact / "reports" / "variant_release_matrix_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "status": "PRIVATE_CANDIDATE_MATRIX",
            "passed": False,
            "variants": ["bf16", "Q8_0", "Q4_K_M"],
            "ineligible_variants": {
                "int8": {
                    "stage": "reload",
                    "reason_code": "invalid_sst",
                    "evidence_sha256": _prefixed_hash("8"),
                },
                "int4_nf4": {
                    "stage": "reload",
                    "reason_code": "empty_completion",
                    "evidence_sha256": _prefixed_hash("9"),
                },
            },
            "qualification_failures": {},
            "reasons": [
                "int8:reload:invalid_sst",
                "int4_nf4:reload:empty_completion",
            ],
        }
    )
    del report["variant_summaries"]["int8"]
    del report["variant_summaries"]["int4_nf4"]
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    binding = hub_publication._variant_release_binding(
        artifact,
        _publication_config()["artifacts"]["model"],
        training_fingerprint=_prefixed_hash("c"),
    )

    assert binding["matrix_status"] == "PRIVATE_CANDIDATE_MATRIX"
    assert binding["passed"] is False
    assert binding["tested_variants"] == [
        "bf16",
        "int8",
        "int4_nf4",
        "Q8_0",
        "Q4_K_M",
    ]
    assert binding["variants"] == ["bf16", "Q8_0", "Q4_K_M"]
    assert set(binding["ineligible_variants"]) == {"int8", "int4_nf4"}
    assert binding["qualification_failures"] == {}
    assert set(binding["fresh_reload_report_sha256"]) == {
        "bf16",
        "Q8_0",
        "Q4_K_M",
    }


def test_private_candidate_variant_matrix_binds_quality_failure(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    report_path = artifact / "reports" / "variant_release_matrix_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "status": "PRIVATE_CANDIDATE_MATRIX",
            "passed": False,
            "qualification_failures": {
                "bf16": {
                    "stage": "evaluation",
                    "reason_code": "hard_quality_gates_failed",
                    "evidence_sha256": report["variant_summaries"]["bf16"]["evaluation_report_sha256"],
                }
            },
            "reasons": ["bf16:qualification:evaluation:hard_quality_gates_failed"],
        }
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    binding = hub_publication._variant_release_binding(
        artifact,
        _publication_config()["artifacts"]["model"],
        training_fingerprint=_prefixed_hash("c"),
    )

    assert binding["matrix_status"] == "PRIVATE_CANDIDATE_MATRIX"
    assert binding["passed"] is False
    assert binding["ineligible_variants"] == {}
    assert set(binding["qualification_failures"]) == {"bf16"}


@pytest.mark.parametrize("secret_kind", ["filename", "content"])
def test_manifest_refuses_secret_bearing_artifacts(tmp_path: Path, secret_kind: str) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    if secret_kind == "filename":
        (artifact / ".env").write_text("SAFE_NAME=value\n", encoding="utf-8")
    else:
        secret = "hf_" + ("x" * 32)
        (artifact / "notes.txt").write_text(f"credential={secret}\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="secret"):
        prepare_publication_manifest(
            artifact,
            _publication_config()["artifacts"]["model"],
            manifest_filename=MANIFEST_FILENAME,
        )

    assert not (artifact / MANIFEST_FILENAME).exists()


def test_manifest_scans_secret_content_beyond_first_16_mib(tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    secret = b"hf_" + (b"x" * 32)
    (artifact / "large-notes.bin").write_bytes((b"safe-padding\n" * 1_500_000) + secret)

    with pytest.raises(PublicationError, match="secret"):
        prepare_publication_manifest(
            artifact,
            _publication_config()["artifacts"]["model"],
        )

    assert not (artifact / MANIFEST_FILENAME).exists()
    assert not (artifact / "checksums.json").exists()


def test_manifest_refuses_private_local_paths(tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    (artifact / "debug.txt").write_text(
        "source=C:\\Users\\Alice\\AppData\\Local\\Scriber\\private.json\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicationError, match="private local-path"):
        prepare_publication_manifest(
            artifact,
            _publication_config()["artifacts"]["model"],
        )


def test_manifest_rejects_incomplete_gemma_legal_notice(tmp_path: Path) -> None:
    artifact = tmp_path / "model"
    _write_model_artifact(artifact)
    (artifact / "NOTICE").write_text("TODO\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="legal marker"):
        prepare_publication_manifest(
            artifact,
            _publication_config()["artifacts"]["model"],
        )


def test_private_upload_sha_pinning_fresh_download_and_offline_reload(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    selected_checkpoint = _selected_checkpoint_path(tmp_path, model)
    config = validate_publication_config(_publication_config())
    fake = _FakeHub(tmp_path / "remote")
    reload_calls: list[tuple[str, Path]] = []

    def reloader(kind: str):
        def run(snapshot: Path, _definition: dict[str, Any]) -> dict[str, Any]:
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
            assert os.environ["HF_DATASETS_OFFLINE"] == "1"
            reload_calls.append((kind, snapshot))
            return _verified_reload_fixture(kind, snapshot, _definition)

        return run

    report_path = tmp_path / "reports" / "hub-publication.json"
    report = publish_private_bundle(
        config,
        artifact_dirs={"model": model, "dataset": dataset},
        completed_training_report_path=completed_training,
        selected_checkpoint_publication_path=selected_checkpoint,
        download_root=tmp_path / "fresh-download",
        report_path=report_path,
        token="sensitive-token-never-serialize",
        api=fake,
        snapshot_download_fn=fake.snapshot_download,
        reloaders={"model": reloader("model"), "dataset": reloader("dataset")},
    )

    assert report["status"] == "verified_private"
    assert report["visibility"] == "private"
    assert report["namespace"] == "Buttermilk03"
    assert report["artifacts"]["model"]["revision"] == "a" * 40
    assert report["artifacts"]["dataset"]["revision"] == "b" * 40
    assert report["artifacts"]["model"]["reload"]["status"] == "passed"
    assert report["artifacts"]["dataset"]["reload"]["status"] == "passed"
    assert report["artifacts"]["model"]["verification_mode"] == ("fresh_exact_revision_download")
    assert report["artifacts"]["model"]["exact_revision_downloaded"] is True
    assert report["artifacts"]["dataset"]["expected_split_counts"] == {
        "release": {
            "challenge": 1,
            "test": 1,
            "train": 1,
            "validation": 1,
        },
        "ai_gold": {
            "challenge": 1,
            "test": 1,
            "train": 1,
            "validation": 1,
        },
    }
    assert report["preflight"]["token_role"] == "write"
    assert report["preflight"]["repository_access_confirmed"] is True
    assert report["completed_training_report_sha256"].startswith("sha256:")
    assert report["training_run_fingerprint"] == "sha256:" + ("c" * 64)
    assert report["bf16_final_model_tree_sha256"] == "sha256:" + ("a" * 64)
    assert report["protection_policy"] == _policy_binding()
    assert report["artifacts"]["model"]["reload"]["protection_policy"] == (_policy_binding())
    assert report["artifacts"]["model"]["reload"]["regression"]["exact_output_hash_matches"] == 110
    assert set(report["artifacts"]["model"]["reload"]["variant_release"]["variants"]) == {
        "bf16",
        "int8",
        "int4_nf4",
        "Q8_0",
        "Q4_K_M",
    }
    assert (
        report["artifacts"]["dataset"]["reload"]["split_inventory"]
        == (report["artifacts"]["dataset"]["expected_split_inventory"]["release"])
    )
    assert len(report["local_bundle_sha256"]) == 64
    assert len(report["publication_binding_sha256"]) == 64
    assert all(
        item["remote_size_verified"] and item["remote_identity_verified"] and item["source_sha256_verified"]
        for artifact in report["artifacts"].values()
        for item in artifact["remote_files"]
    )
    serialized = report_path.read_text(encoding="utf-8")
    assert json.loads(serialized) == report
    assert "sensitive-token-never-serialize" not in serialized
    assert str(tmp_path) not in serialized
    markdown = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert report["artifacts"]["model"]["revision"] in markdown
    assert report["artifacts"]["dataset"]["revision"] in markdown
    assert "sensitive-token-never-serialize" not in markdown
    assert [kind for kind, _path in reload_calls] == ["model", "dataset"]

    call_names = [name for name, _kwargs in fake.calls]
    assert call_names[0] == "whoami"
    assert max(index for index, name in enumerate(call_names) if name == "auth_check") < call_names.index(
        "upload_folder"
    )
    auth_calls = [kwargs for name, kwargs in fake.calls if name == "auth_check"]
    assert auth_calls
    assert all(call["write"] is True for call in auth_calls)
    create_calls = [kwargs for name, kwargs in fake.calls if name == "create_repo"]
    assert len(create_calls) == 2
    assert all(call["private"] is True for call in create_calls)
    assert all(call["exist_ok"] is True for call in create_calls)
    upload_calls = [kwargs for name, kwargs in fake.calls if name == "upload_folder"]
    assert len(upload_calls) == 2
    assert all(call["revision"] == "main" for call in upload_calls)
    assert all(call["create_pr"] is False for call in upload_calls)
    assert all(call["delete_patterns"] == "*" for call in upload_calls)
    assert all(MANIFEST_FILENAME in call["allow_patterns"] for call in upload_calls)
    snapshot_calls = [kwargs for name, kwargs in fake.calls if name == "snapshot_download"]
    assert [call["revision"] for call in snapshot_calls] == ["a" * 40, "b" * 40]
    assert all(call["local_files_only"] is False for call in snapshot_calls)
    assert all(call["force_download"] is True for call in snapshot_calls)
    assert all("cache_dir" in call for call in snapshot_calls)

    call_count = len(fake.calls)
    with pytest.raises(PublicationError, match="immutable report"):
        publish_private_bundle(
            config,
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=selected_checkpoint,
            download_root=tmp_path / "another-fresh-download",
            report_path=report_path,
            token="sensitive-token-never-serialize",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
            reloaders={"model": reloader("model"), "dataset": reloader("dataset")},
        )
    assert len(fake.calls) == call_count


def test_private_upload_can_verify_remote_identity_without_roundtrip_download(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    fake = _FakeHub(tmp_path / "remote")
    reloaded: list[Path] = []

    def reloader(kind: str):
        def run(snapshot: Path, definition: dict[str, Any]) -> dict[str, Any]:
            reloaded.append(snapshot)
            return _verified_reload_fixture(kind, snapshot, definition)

        return run

    def forbidden_download(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("post-upload download must not run")

    verification_root = tmp_path / "verification"
    report = publish_private_bundle(
        _publication_config(),
        artifact_dirs={"model": model, "dataset": dataset},
        completed_training_report_path=completed_training,
        selected_checkpoint_publication_path=_selected_checkpoint_path(
            tmp_path,
            model,
        ),
        download_root=verification_root,
        report_path=tmp_path / "hub-report.json",
        token="not-serialized",
        api=fake,
        snapshot_download_fn=forbidden_download,
        reloaders={"model": reloader("model"), "dataset": reloader("dataset")},
        verification_mode="local_source_remote_identity",
    )

    assert reloaded == [model.resolve(), dataset.resolve()]
    assert not any(name == "snapshot_download" for name, _kwargs in fake.calls)
    assert not (verification_root / "model").exists()
    assert not (verification_root / "dataset").exists()
    for artifact in report["artifacts"].values():
        assert artifact["verification_mode"] == "local_source_remote_identity"
        assert artifact["exact_revision_downloaded"] is False
        assert artifact["source_snapshot_verified"] is True
        assert artifact["remote_identity_verified"] is True
        assert all(item["remote_identity_verified"] for item in artifact["remote_files"])


def test_public_repo_is_rejected_before_upload(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    fake = _FakeHub(tmp_path / "remote")
    model_repo = _publication_config()["artifacts"]["model"]["repo_id"]
    fake.repo_private[model_repo] = False

    with pytest.raises(PublicationError, match="private"):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
            download_root=tmp_path / "fresh-download",
            report_path=tmp_path / "report.json",
            token="not-serialized",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
            reloaders={
                "model": lambda *_args: {"status": "passed"},
                "dataset": lambda *_args: {"status": "passed"},
            },
        )

    assert not any(name == "upload_folder" for name, _kwargs in fake.calls)
    assert not (tmp_path / "report.json").exists()


def test_snapshot_with_changed_payload_is_rejected_without_report(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    fake = _FakeHub(tmp_path / "remote")

    def tampered_snapshot(repo_id: str, **kwargs: Any) -> str:
        result = fake.snapshot_download(repo_id, **kwargs)
        if kwargs["repo_type"] == "model":
            (Path(result) / "config.json").write_text('{"tampered":true}\n', encoding="utf-8")
        return result

    report_path = tmp_path / "report.json"
    with pytest.raises(PublicationError, match="snapshot payload"):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
            download_root=tmp_path / "fresh-download",
            report_path=report_path,
            token="not-serialized",
            api=fake,
            snapshot_download_fn=tampered_snapshot,
            reloaders={
                "model": lambda *_args: {"status": "passed"},
                "dataset": lambda *_args: {"status": "passed"},
            },
        )

    assert not report_path.exists()


def test_offline_model_reload_passes_only_local_paths_to_transformers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "model"
    _write_model_artifact(snapshot)
    _prepare_fixture_manifest(snapshot, "model")
    definition = _publication_config()["artifacts"]["model"]
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        hub_publication,
        "verify_frozen_lexical_policy_artifact",
        lambda *_args, **_kwargs: _policy_binding(),
    )

    class Model:
        config = SimpleNamespace(
            model_type="gemma3_text",
            scriber_protection_policy_id=_POLICY_ID,
            scriber_protection_policy_sha256=_POLICY_SHA256,
        )

        def to(self, device: str) -> Model:
            calls.append(("model", "to", {"device": device}))
            return self

        def eval(self) -> None:
            calls.append(("model", "eval", {}))

        def num_parameters(self) -> int:
            return 270_000_000

    def tokenizer_loader(path: str, **kwargs: Any) -> SimpleNamespace:
        calls.append(("tokenizer", path, kwargs))
        return SimpleNamespace(name_or_path=path, vocab_size=262_144)

    def model_loader(path: str, **kwargs: Any) -> Model:
        calls.append(("model", path, kwargs))
        return Model()

    result = offline_reload_model(
        snapshot,
        definition,
        model_loader=model_loader,
        tokenizer_loader=tokenizer_loader,
        regression_runner=lambda root, loaded_definition, *_args: (_passing_regression_result(root, loaded_definition)),
    )

    assert result["status"] == "passed"
    assert result["loader"] == "transformers_local"
    assert result["device"] == "cuda"
    assert result["tokenizer_class"] == "SimpleNamespace"
    assert result["tokenizer_vocab_size"] == 262_144
    assert result["model_type"] == "gemma3_text"
    assert result["parameter_count"] == 270_000_000
    assert result["protection_policy"] == _policy_binding()
    assert result["regression"]["exact_output_hash_matches"] == 110
    assert result["runtime_reload"]["source_equivalence"] == ("artifact_bytes_equal_executed_package")
    assert result["checksums"]["status"] == "verified"
    assert result["legal_files"]["kind"] == "gemma_derived_model"
    assert calls[0] == (
        "tokenizer",
        str(snapshot.resolve()),
        {"local_files_only": True, "fix_mistral_regex": False},
    )
    assert calls[1] == (
        "model",
        str(snapshot.resolve()),
        {"local_files_only": True, "dtype": "auto"},
    )
    assert calls[2] == ("model", "to", {"device": "cuda"})
    assert calls[3] == ("model", "eval", {})


def test_offline_model_reload_revalidates_policy_against_loaded_tokenizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "model"
    _write_model_artifact(snapshot)
    _prepare_fixture_manifest(snapshot, "model")
    definition = _publication_config()["artifacts"]["model"]
    tokenizer = SimpleNamespace(vocab_size=262_144)
    verification_tokenizers: list[object | None] = []

    def verify_policy(
        _root: Path,
        *,
        tokenizer: object | None = None,
        expected: object | None = None,
    ) -> dict[str, str]:
        del expected
        verification_tokenizers.append(tokenizer)
        return _policy_binding()

    class Model:
        config = SimpleNamespace(
            model_type="gemma3_text",
            scriber_protection_policy_id=_POLICY_ID,
            scriber_protection_policy_sha256=_POLICY_SHA256,
        )

        def to(self, _device: str) -> Model:
            return self

        def eval(self) -> None:
            return None

        def num_parameters(self) -> int:
            return 270_000_000

    monkeypatch.setattr(
        hub_publication,
        "verify_frozen_lexical_policy_artifact",
        verify_policy,
    )

    offline_reload_model(
        snapshot,
        definition,
        tokenizer_loader=lambda *_args, **_kwargs: tokenizer,
        model_loader=lambda *_args, **_kwargs: Model(),
        regression_runner=lambda root, loaded_definition, *_args: (_passing_regression_result(root, loaded_definition)),
    )

    assert verification_tokenizers == [None, tokenizer]


def test_offline_model_reload_uses_bound_policy_for_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scriber_polishing.inference as inference

    snapshot = tmp_path / "model"
    _write_model_artifact(snapshot)
    _prepare_fixture_manifest(snapshot, "model")
    definition = _publication_config()["artifacts"]["model"]
    captured: dict[str, object] = {}

    class Model:
        config = SimpleNamespace(
            model_type="gemma3_text",
            scriber_protection_policy_id=_POLICY_ID,
            scriber_protection_policy_sha256=_POLICY_SHA256,
        )

        def to(self, _device: str) -> Model:
            return self

        def eval(self) -> None:
            return None

        def num_parameters(self) -> int:
            return 270_000_000

    def protector(text: str) -> str:
        return text

    def fake_policy_span_protector(
        policy: Mapping[str, object],
        *,
        product: bool,
    ) -> object:
        captured["policy"] = policy
        captured["product"] = product
        return protector

    def fake_generate_prediction(
        source_text: str,
        **kwargs: Any,
    ) -> SimpleNamespace:
        captured["source_text"] = source_text
        captured["span_protector"] = kwargs["span_protector"]
        return SimpleNamespace(
            prediction_sst="[DOC]\n[P]Test[/P]\n[/DOC]",
            valid_sst=True,
            restoration_error=None,
            generation_seconds=0.0,
        )

    def fake_regression_suite(
        _path: Path,
        _policy: Mapping[str, object],
        *,
        predictor: Any,
    ) -> dict[str, object]:
        result = predictor({"source_text": "test source"})
        assert result["valid_sst"] is True
        return {"status": "passed"}

    monkeypatch.setattr(
        hub_publication,
        "verify_frozen_lexical_policy_artifact",
        lambda *_args, **_kwargs: _policy_binding(),
    )
    monkeypatch.setattr(inference, "_policy_span_protector", fake_policy_span_protector)
    monkeypatch.setattr(
        inference,
        "load_inference_protection_policy",
        lambda **_kwargs: (_policy_binding(), {"source": "test"}),
    )
    monkeypatch.setattr(inference, "generate_prediction", fake_generate_prediction)
    monkeypatch.setattr(
        hub_publication,
        "run_hub_regression_suite",
        fake_regression_suite,
    )

    result = offline_reload_model(
        snapshot,
        definition,
        tokenizer_loader=lambda *_args, **_kwargs: SimpleNamespace(
            vocab_size=262_144,
            pad_token_id=0,
        ),
        model_loader=lambda *_args, **_kwargs: Model(),
    )

    assert result["regression"] == {"status": "passed"}
    assert captured == {
        "policy": _policy_binding(),
        "product": False,
        "source_text": "test source",
        "span_protector": protector,
    }


def test_offline_model_reload_rejects_policy_tampering(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "model"
    _write_model_artifact(snapshot)
    policy_path = snapshot / "scriber-protection-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["max_spans"] = 31
    policy_path.write_text(
        json.dumps(policy, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicationError, match="protection policy"):
        offline_reload_model(
            snapshot,
            _publication_config()["artifacts"]["model"],
            model_loader=lambda *_args, **_kwargs: None,
            tokenizer_loader=lambda *_args, **_kwargs: None,
        )


def test_offline_dataset_reload_requires_all_synthetic_splits(tmp_path: Path) -> None:
    snapshot = tmp_path / "dataset"
    _write_dataset_artifact(snapshot)
    definition = _publication_config()["artifacts"]["dataset"]
    _prepare_fixture_manifest(snapshot, "dataset")

    result = offline_reload_dataset(snapshot, definition)

    assert result["status"] == "passed"
    assert result["loader"] == "jsonl_splits"
    assert result["split_counts"] == {
        "challenge": 1,
        "test": 1,
        "train": 1,
        "validation": 1,
    }
    assert result["ai_gold_split_counts"] == result["split_counts"]
    assert result["checksums"]["status"] == "verified"
    assert result["legal_files"]["kind"] == "synthetic_dataset"

    record = _dataset_record("test", "test-1")
    record["provenance"]["synthetic_only"] = False
    (snapshot / "data" / "test.jsonl").write_text(
        json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PublicationError, match="synthetic"):
        offline_reload_dataset(snapshot, definition)


def test_fresh_download_root_must_not_preexist(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    download_root = tmp_path / "already-there"
    download_root.mkdir()
    fake = _FakeHub(tmp_path / "remote")

    with pytest.raises(PublicationError, match="fresh verification root"):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
            download_root=download_root,
            report_path=tmp_path / "report.json",
            token="not-serialized",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
            reloaders={
                "model": lambda *_args: {"status": "passed"},
                "dataset": lambda *_args: {"status": "passed"},
            },
        )

    assert fake.calls == []


def test_config_validation_does_not_mutate_the_input() -> None:
    config = _publication_config()
    original = deepcopy(config)

    validate_publication_config(config)

    assert config == original


def test_read_only_token_is_rejected_before_repo_creation_or_upload(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    fake = _FakeHub(tmp_path / "remote")

    def read_only_identity(**kwargs: Any) -> dict[str, Any]:
        fake.calls.append(("whoami", dict(kwargs)))
        return {
            "name": "Buttermilk03",
            "auth": {"accessToken": {"role": "read"}},
        }

    fake.whoami = read_only_identity  # type: ignore[method-assign]
    with pytest.raises(PublicationError, match="write permission"):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
            download_root=tmp_path / "fresh",
            report_path=tmp_path / "report.json",
            token="read-token-never-serialize",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
            reloaders={
                "model": lambda *_args: {"status": "passed"},
                "dataset": lambda *_args: {"status": "passed"},
            },
        )

    assert [name for name, _kwargs in fake.calls] == ["whoami"]
    assert not (model / MANIFEST_FILENAME).exists()


def test_remote_lfs_hash_mismatch_is_rejected_before_download(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    fake = _FakeHub(tmp_path / "remote")
    original_model_info = fake.model_info

    def tampered_model_info(repo_id: str, **kwargs: Any) -> SimpleNamespace:
        info = original_model_info(repo_id, **kwargs)
        if kwargs.get("revision"):
            for sibling in info.siblings:
                if sibling.rfilename == "model.safetensors":
                    sibling.lfs.sha256 = "0" * 64
        return info

    fake.model_info = tampered_model_info  # type: ignore[method-assign]
    with pytest.raises(PublicationError, match="LFS SHA-256"):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
            download_root=tmp_path / "fresh",
            report_path=tmp_path / "report.json",
            token="write-token-never-serialize",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
            reloaders={
                "model": lambda *_args: {"status": "passed"},
                "dataset": lambda *_args: {"status": "passed"},
            },
        )

    assert not any(name == "snapshot_download" for name, _kwargs in fake.calls)
    assert not (tmp_path / "report.json").exists()


def test_local_preparation_binds_schema_valid_completed_training_report(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    preparation_path = tmp_path / "preparation.json"

    result = prepare_publication_bundle(
        _publication_config(),
        artifact_dirs={"model": model, "dataset": dataset},
        completed_training_report_path=completed_training,
        selected_checkpoint_publication_path=_selected_checkpoint_path(
            tmp_path,
            model,
        ),
        report_path=preparation_path,
    )

    assert result["status"] == "prepared_private"
    assert result["completed_training_report_sha256"] == (
        "sha256:" + hashlib.sha256(completed_training.read_bytes()).hexdigest()
    )
    assert result["training_run_fingerprint"] == "sha256:" + ("c" * 64)
    assert result["bf16_final_model_tree_sha256"] == "sha256:" + ("a" * 64)
    assert result["protection_policy"] == _policy_binding()
    assert len(result["bundle_sha256"]) == 64
    assert json.loads(preparation_path.read_text(encoding="utf-8")) == result
    binding = json.loads((model / "reports" / "completed_final_training_binding.json").read_text(encoding="utf-8"))
    assert binding["completed_training_report_sha256"] == result["completed_training_report_sha256"]


def test_training_binding_rejects_mismatched_final_model_fingerprint(tmp_path: Path) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    report = json.loads(completed_training.read_text(encoding="utf-8"))
    report["final_model"]["run_fingerprint"] = "sha256:" + ("0" * 64)
    completed_training.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="final-model inventory evidence"):
        prepare_publication_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=_selected_checkpoint_path(
                tmp_path,
                model,
            ),
        )

    assert not (model / "reports" / "completed_final_training_binding.json").exists()
    assert not (model / MANIFEST_FILENAME).exists()


def test_selected_checkpoint_bytes_must_match_the_publication_model(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    selected_checkpoint = _selected_checkpoint_path(tmp_path, model)
    (model / "model.safetensors").write_bytes(b"different-model-weights")

    with pytest.raises(
        PublicationError,
        match="published model bytes differ",
    ):
        prepare_publication_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=selected_checkpoint,
        )

    assert not (model / "reports" / "selected_checkpoint_binding.json").exists()
    assert not (model / MANIFEST_FILENAME).exists()


def test_variant_release_must_match_the_completed_training_before_hub_access(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    dataset = tmp_path / "dataset"
    _write_model_artifact(model)
    _write_dataset_artifact(dataset)
    completed_training = tmp_path / "completed-training.json"
    _write_completed_training_report(completed_training)
    selected_checkpoint = _selected_checkpoint_path(tmp_path, model)
    variant_path = model / "reports" / "variant_release_matrix_report.json"
    variant = json.loads(variant_path.read_text(encoding="utf-8"))
    variant["training_fingerprint"] = _prefixed_hash("0")
    variant_path.write_text(
        json.dumps(variant, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fake = _FakeHub(tmp_path / "remote")

    with pytest.raises(
        PublicationError,
        match="differs from the completed training run",
    ):
        publish_private_bundle(
            _publication_config(),
            artifact_dirs={"model": model, "dataset": dataset},
            completed_training_report_path=completed_training,
            selected_checkpoint_publication_path=selected_checkpoint,
            download_root=tmp_path / "fresh",
            report_path=tmp_path / "report.json",
            token="write-token-never-serialize",
            api=fake,
            snapshot_download_fn=fake.snapshot_download,
        )

    assert fake.calls == []
    assert not (model / "reports" / "selected_checkpoint_binding.json").exists()
    assert not (model / MANIFEST_FILENAME).exists()


def test_dataset_reload_in_fresh_isolated_process_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "dataset"
    _write_dataset_artifact(snapshot)
    _prepare_fixture_manifest(snapshot, "dataset")
    monkeypatch.setenv("HF_TOKEN", "must-not-reach-worker")

    result = isolated_reload(
        "dataset",
        snapshot,
        _publication_config()["artifacts"]["dataset"],
        cache_root=tmp_path / "fresh-cache",
    )

    assert result["status"] == "passed"
    assert result["execution_isolation"] == "fresh_python_isolated"
    assert result["fresh_cache"] is True
    assert result["network_disabled"] is True
    assert result["credentials_removed"] is True
    assert result["split_counts"] == {
        "challenge": 1,
        "test": 1,
        "train": 1,
        "validation": 1,
    }


def test_isolated_reload_worker_rejects_aliased_duplicate_and_incomplete_roots(
    tmp_path: Path,
) -> None:
    worker = Path(__file__).parents[1] / "scripts" / "verify_hub_reload_worker.py"
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    symlink = tmp_path / "dependency-link"
    try:
        symlink.symlink_to(dependency, target_is_directory=True)
    except OSError:
        symlink = None
    base_environment = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in ("TOKEN", "API_KEY", "PASSWORD", "SECRET"))
    }
    base_environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
        }
    )
    roots_to_reject = [
        ["relative-root"],
        [str(dependency.resolve()), str(dependency.resolve())],
    ]
    if symlink is not None:
        roots_to_reject.append([str(symlink.absolute())])
    for roots in roots_to_reject:
        environment = dict(base_environment)
        environment["SCRIBER_HUB_RELOAD_DEPENDENCY_ROOTS_JSON"] = json.dumps(roots)
        completed = subprocess.run(
            [sys.executable, "-I", str(worker)],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        assert completed.returncode == 2
        assert "dependency roots are invalid" in completed.stderr

    incomplete_environment = dict(base_environment)
    incomplete_environment["SCRIBER_HUB_RELOAD_DEPENDENCY_ROOTS_JSON"] = json.dumps([str(dependency.resolve())])
    incomplete = subprocess.run(
        [sys.executable, "-I", str(worker)],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        env=incomplete_environment,
    )
    assert incomplete.returncode == 2
    assert "dependencies are incomplete" in incomplete.stderr


def test_model_isolated_reload_binds_fresh_process_and_dependency_roots(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "model"
    _write_model_artifact(snapshot)
    _prepare_fixture_manifest(snapshot, "model")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        request = json.loads(kwargs["input"])
        assert request["kind"] == "model"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "passed",
                    "loader": "transformers_local",
                    "dependency_probe": ["yaml", "jsonschema", "transformers"],
                }
            ),
            stderr="",
        )

    result = isolated_reload(
        "model",
        snapshot,
        _publication_config()["artifacts"]["model"],
        cache_root=tmp_path / "model-cache",
        run_fn=fake_run,
    )

    assert result["status"] == "passed"
    assert captured["command"][:2] == [sys.executable, "-I"]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    roots = json.loads(environment["SCRIBER_HUB_RELOAD_DEPENDENCY_ROOTS_JSON"])
    assert roots
    assert len(roots) == len(set(roots))
    assert all(Path(root).is_absolute() and Path(root).is_dir() for root in roots)


def test_dataset_reload_includes_all_ai_gold_splits_when_configured(tmp_path: Path) -> None:
    snapshot = tmp_path / "dataset"
    _write_dataset_artifact(snapshot)
    definition = deepcopy(_publication_config()["artifacts"]["dataset"])
    definition["reload"]["ai_gold_split_files"] = {
        split: f"ai_gold/{split}.jsonl" for split in ("train", "validation", "test", "challenge")
    }
    _prepare_fixture_manifest(snapshot, "dataset")

    result = offline_reload_dataset(snapshot, definition)

    assert result["ai_gold_split_counts"] == {
        "challenge": 1,
        "test": 1,
        "train": 1,
        "validation": 1,
    }
