from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.curriculum import (
    canonical_json_bytes,
    hash_identifier_sequence,
)
from scriber_polishing.hf_marker_repair_job import (
    build_hf_marker_repair_packet,
)
from scriber_polishing.marker_repair import (
    build_marker_repair_bundle,
    verify_marker_repair_bundle,
)
from scriber_polishing.model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from scriber_polishing.protected_spans import (
    build_gemma_unused_policy_evidence,
    build_lexical_keep_policy_evidence,
)
from scriber_polishing.remediation_experiment import (
    RemediationExperimentError,
    build_remediation_experiment,
    load_remediation_order_plan,
    validate_matched_remediation_plans,
    validate_remediation_experiment_config,
    validate_remediation_experiment_manifest,
    validate_remediation_order_plan,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text
from scriber_polishing.training_integrity import (
    TrainingIntegrityError,
    bind_improvement_order_plan,
    bind_parent_checkpoint,
    bind_protection_policy,
    bind_training_dataset,
    checkpoint_tree_sha256,
    load_training_config,
)
from scriber_polishing.training_runtime import (
    ResumableEpochOrderSampler,
    apply_training_protection,
    build_effective_improvement_order,
)


class _Backend:
    def to_str(self) -> str:
        return '{"model":{"vocab":{"<unused0>":300}}}'


class _Tokenizer:
    name_or_path = BASE_MODEL_ID
    vocab_size = 7000
    pad_token_id = 0
    eos_token_id = 1
    bos_token_id = 2
    unk_token_id = 3
    all_special_ids = [0, 1, 2, 3]
    chat_template = "{{ messages }}"
    backend_tokenizer = _Backend()

    def __len__(self) -> int:
        return self.vocab_size

    def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return [100 + int(marker.removeprefix("<unused").removesuffix(">"))]

    def convert_tokens_to_ids(self, marker: str) -> int:
        return 100 + int(marker.removeprefix("<unused").removesuffix(">"))

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        del skip_special_tokens
        return f"<unused{token_ids[0] - 100}>"

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> list[int]:
        assert tokenize is True
        user = messages[0]["content"]
        token_ids = [
            10,
            *[
                1000 + index
                for index in range((len(user.encode("utf-8")) + 7) // 8)
            ],
            20,
        ]
        if len(messages) == 2:
            token_ids.extend(
                2000 + index
                for index in range(
                    (len(messages[1]["content"].encode("utf-8")) + 7) // 8
                )
            )
            token_ids.append(21)
        elif not add_generation_prompt:
            token_ids.pop()
        return token_ids


class _LexicalTokenizer(_Tokenizer):
    _markers = tuple(
        [f"⟦KEEP_{chr(ord('A') + index)}⟧" for index in range(26)]
        + [f"⟦KEEP_A{chr(ord('A') + index)}⟧" for index in range(6)]
    )
    _token_ids = {
        marker: [500 + index * 8 + offset for offset in range(5)]
        for index, marker in enumerate(_markers)
    }
    _decoded = {
        tuple(token_ids): marker for marker, token_ids in _token_ids.items()
    }

    def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        if marker.startswith("<unused"):
            return super().encode(
                marker,
                add_special_tokens=add_special_tokens,
            )
        return list(self._token_ids[marker])

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        if len(token_ids) == 1:
            return super().decode(
                token_ids,
                skip_special_tokens=skip_special_tokens,
            )
        del skip_special_tokens
        return self._decoded[tuple(token_ids)]


def _prefixed_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identity() -> dict[str, object]:
    backend = _Backend().to_str().encode()
    template = (
        json.dumps(
            _Tokenizer.chat_template,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    core: dict[str, object] = {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "class": "test.Tokenizer",
        "name_or_path": BASE_MODEL_ID,
        "vocab_size": 7000,
        "special_token_ids": {
            "pad_token_id": 0,
            "eos_token_id": 1,
            "bos_token_id": 2,
            "unk_token_id": 3,
        },
        "backend_bytes": len(backend),
        "backend_sha256": _prefixed_sha256(backend),
        "chat_template_sha256": _prefixed_sha256(template),
    }
    core["identity_sha256"] = _prefixed_sha256(
        canonical_json_bytes(core)
    )
    return core


def _pair(
    example_id: str,
    profile: str,
    *,
    split: str,
    domain: str = "general_business",
) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "example_id": example_id,
        "split": split,
        "source_text": "bitte bis 31.07.2026 antworten",
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Eine Antwort ist erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Frist endet am 31.07.2026.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["31.07.2026"],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Antwortfrist"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": ["31.07.2026"],
        "risk_tags": ["date"],
        "domain": domain,
        "document_type": "Geschaeftsbrief",
        "corruption_profile": profile,
        "operations": ["lowercase_sentence_start"],
        "provenance": {
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
        },
    }


def _phase_rows(*, split: str, per_phase: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    specs = (
        ("p1", "identity", "general_business"),
        ("p2", "grammar", "general_business"),
        ("p3", "spoken_formatting_list", "general_business"),
        ("p4", "domain_specific_cleanup", "general_business"),
        ("p5", "identity", "hard_negatives"),
    )
    for prefix, profile, domain in specs:
        for index in range(per_phase):
            rows.append(
                _pair(
                    f"{split}_{prefix}_{index:02d}",
                    profile,
                    split=split,
                    domain=domain,
                )
            )
    return rows


def _url_overlap_pair(*, split: str) -> dict[str, object]:
    row = _pair(
        f"{split}_url_overlap",
        "domain_specific_cleanup",
        split=split,
    )
    value = (
        "https://example.invalid/ARC-184. und "
        "https://example.invalid/ARC-184"
    )
    row["source_text"] = value
    row["target_sst"] = f"[DOC]\n[P]{value}[/P]\n[/DOC]"
    return row


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> str:
    payload = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    ).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    train = [*_phase_rows(split="train", per_phase=4), _url_overlap_pair(split="train")]
    validation = [
        *_phase_rows(split="validation", per_phase=2),
        _url_overlap_pair(split="validation"),
    ]
    train_path = tmp_path / "train.jsonl"
    validation_path = tmp_path / "validation.jsonl"
    train_sha256 = _write_jsonl(train_path, train)
    validation_sha256 = _write_jsonl(validation_path, validation)
    pilot_manifest = {
        "schema_version": 1,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "split_counts": {
            "train": len(train),
            "validation": len(validation),
        },
        "split_sha256": {
            "train": train_sha256,
            "validation": validation_sha256,
        },
    }
    pilot_manifest_path = tmp_path / "pilot-manifest.json"
    pilot_manifest_payload = canonical_json_bytes(pilot_manifest)
    pilot_manifest_path.write_bytes(pilot_manifest_payload)

    policy_path = tmp_path / "policy.json"
    policy_payload = canonical_json_bytes(
        build_gemma_unused_policy_evidence(
            _Tokenizer(),
            tokenizer_identity=_identity(),
        )
    )
    policy_path.write_bytes(policy_payload)

    parent = tmp_path / "pilot-final"
    parent.mkdir()
    (parent / "config.json").write_text("{}", encoding="utf-8")
    (parent / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (parent / "model.safetensors").write_bytes(b"pilot")

    comparison_path = tmp_path / "curriculum-experiment.yaml"
    comparison_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "seed: 270014",
                "bootstrap_resamples: 10000",
                "confidence_level: 0.95",
                "minimum_composite_delta: 0.005",
                "maximum_eval_loss_ratio: 1.02",
                "weights:",
                "  safety_content: 0.60",
                "  task_quality: 0.25",
                "  structure: 0.10",
                "  eval_loss: 0.05",
                "require_no_new_critical_errors: true",
                "require_metric_non_regression: true",
                "",
            )
        ),
        encoding="utf-8",
    )
    config: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": "remediation_ab_test",
        "inputs": {
            "pilot_manifest_path": str(pilot_manifest_path),
            "pilot_manifest_sha256": hashlib.sha256(
                pilot_manifest_payload
            ).hexdigest(),
            "train_path": str(train_path),
            "train_sha256": train_sha256,
            "validation_path": str(validation_path),
            "validation_sha256": validation_sha256,
            "protection_policy_path": str(policy_path),
            "protection_policy_artifact_sha256": hashlib.sha256(
                policy_payload
            ).hexdigest(),
            "parent": {
                "mode": "explicit_remediation_parent",
                "candidate_id": "pilot_lr5e5",
                "learning_rate": 0.00005,
                "checkpoint_path": str(parent),
                "checkpoint_tree_sha256": checkpoint_tree_sha256(parent),
                "training_report_sha256": "sha256:" + "a" * 64,
                "external_failure_signature_sha256": "sha256:" + "b" * 64,
                "scope": "remediation_only_not_release",
                "selection_basis": "composite_then_loss_tie_breaks",
                "release_eligible": False,
            },
        },
        "training": {
            "seed": 270015,
            "epochs": 1,
            "max_updates": 2,
            "max_train_examples": 20,
            "max_sequence_length": 128,
            "micro_batch_size": 2,
            "gradient_accumulation_steps": 8,
            "effective_batch_size": 16,
            "learning_rate": 0.00001,
            "optimizer": "adamw_torch_fused",
            "scheduler": "linear",
            "eval_steps": 1,
            "save_steps": 1,
            "save_total_limit": 1,
            "warmup_ratio": 0.0,
            "weight_decay": 0.0,
            "max_grad_norm": 1.0,
            "bf16": True,
            "gradient_checkpointing": True,
            "assistant_output_loss_only": True,
            "early_stopping": False,
        },
        "parity_dev": {"seed": 270016, "per_phase": 2},
        "adoption_policy": {
            "comparison_config_path": str(comparison_path),
            "comparison_config_sha256": hashlib.sha256(
                comparison_path.read_bytes()
            ).hexdigest(),
        },
    }
    config_path = tmp_path / "remediation.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return config_path, config


def test_builder_materializes_matched_safe_validation_only_bundle(
    tmp_path: Path,
) -> None:
    config_path, _ = _fixture(tmp_path)
    output = tmp_path / "bundle"

    bundle = build_remediation_experiment(config_path, output_dir=output)
    manifest = validate_remediation_experiment_manifest(bundle.manifest)

    comparison_binding = manifest["adoption_policy"]["comparison_config"]
    assert comparison_binding["filename"] == "comparison-policy.yaml"
    assert hashlib.sha256(
        (output / comparison_binding["filename"]).read_bytes()
    ).hexdigest() == comparison_binding["sha256"]
    assert manifest["selection"]["selected_examples"] == 20
    assert manifest["selection"]["phase_counts"] == {
        str(phase): 4 for phase in range(1, 6)
    }
    assert manifest["schedule"]["update_steps"] == 2
    assert manifest["schedule"]["maximum_updates"] == 2
    assert manifest["holdout_isolation"] == {
        "opened_splits": ["train", "validation"],
        "test_inputs": False,
        "challenge_inputs": False,
        "ai_gold_inputs": False,
        "decision_data": "validation_only_parity_dev",
    }
    assert manifest["safety_filters"]["train"][
        "url_overlap_rejected_example_ids"
    ] == ["train_url_overlap"]
    assert manifest["safety_filters"]["validation"][
        "url_overlap_rejected_example_ids"
    ] == ["validation_url_overlap"]
    assert (
        output / "pilot-train.safety-source.jsonl"
    ).read_bytes() == (tmp_path / "train.jsonl").read_bytes()

    shuffled, shuffled_sha = load_remediation_order_plan(
        output / "shuffled.order-plan.json"
    )
    staged, staged_sha = load_remediation_order_plan(
        output / "staged.order-plan.json"
    )
    validate_matched_remediation_plans(shuffled, staged)
    assert shuffled["safety_source"]["record_count"] == 21
    assert shuffled["source"]["record_count"] == 20
    assert shuffled_sha != staged_sha
    assert shuffled["comparison_pair_sha256"] == staged[
        "comparison_pair_sha256"
    ]
    assert shuffled["state_initialization"] == {
        "optimizer": "fresh",
        "scheduler": "fresh",
        "parent_as_resume": "forbidden",
        "same_run_resume": "identity_bound_recovery_only",
    }
    assert json.loads(
        (output / "parity-dev.manifest.json").read_text(encoding="utf-8")
    )["parent_split"] == "validation"

    training_config = load_training_config(output / "shuffled.train.yaml")
    dataset = bind_training_dataset(
        output / "dataset-manifest.json",
        train=output / "selected-train.jsonl",
        validation=output / "safe-validation.jsonl",
        train_limit=training_config.values["train_examples"],
        validation_limit=training_config.values["validation_examples"],
    )
    bound_policy = bind_protection_policy(
        training_config,
        tokenizer=_Tokenizer(),
    )
    bound_parent = bind_parent_checkpoint(training_config)
    assert bound_policy is not None
    assert bound_parent is not None
    selected_protection = apply_training_protection(
        dataset.train.records,
        policy=bound_policy.values,
    )
    bound_plan = bind_improvement_order_plan(
        training_config,
        dataset,
        protection_policy=bound_policy,
        parent_checkpoint=bound_parent,
        protection_application=selected_protection.evidence,
    )
    assert bound_plan.values["comparison_pair_sha256"] == manifest[
        "comparison_pair_sha256"
    ]

    staged_training_config = load_training_config(
        output / "staged.train.yaml"
    )
    staged_plan = bind_improvement_order_plan(
        staged_training_config,
        dataset,
        protection_policy=bound_policy,
        parent_checkpoint=bound_parent,
        protection_application=selected_protection.evidence,
    )
    selected_ids = list(selected_protection.accepted_example_ids)
    sequence_rejected_ids = selected_ids[:1]
    used_ids = selected_ids[1:]
    sequence_lengths = list(range(1, len(used_ids) + 1))
    shuffled_effective = build_effective_improvement_order(
        bound_plan.values,
        plan_binding=bound_plan.binding(),
        loaded_example_ids=selected_ids,
        safe_example_ids=selected_ids,
        policy_rejected_example_ids=[],
        policy_rejection_evidence=selected_protection.evidence,
        used_example_ids=used_ids,
        sequence_rejected_example_ids=sequence_rejected_ids,
        sequence_lengths=sequence_lengths,
    )
    staged_effective = build_effective_improvement_order(
        staged_plan.values,
        plan_binding=staged_plan.binding(),
        loaded_example_ids=selected_ids,
        safe_example_ids=selected_ids,
        policy_rejected_example_ids=[],
        policy_rejection_evidence=selected_protection.evidence,
        used_example_ids=used_ids,
        sequence_rejected_example_ids=sequence_rejected_ids,
        sequence_lengths=sequence_lengths,
    )
    assert set(shuffled_effective.epoch_indices[0]) == set(range(19))
    assert set(staged_effective.epoch_indices[0]) == set(range(19))
    assert (
        shuffled_effective.evidence["used_example_ids_sha256"]
        == staged_effective.evidence["used_example_ids_sha256"]
    )
    sampler = ResumableEpochOrderSampler(
        staged_effective.epoch_indices
    )
    sampler.set_epoch(0)
    assert tuple(sampler) == staged_effective.epoch_indices[0]
    phase_by_id = {
        item["example_id"]: item["phase"]
        for item in staged_plan.values["assignments"]
    }
    staged_ids = [
        used_ids[index] for index in staged_effective.epoch_indices[0]
    ]
    assert [phase_by_id[example_id] for example_id in staged_ids] == sorted(
        phase_by_id[example_id] for example_id in staged_ids
    )

    safety_source = output / "pilot-train.safety-source.jsonl"
    safety_payload = safety_source.read_bytes()
    safety_source.write_bytes(
        safety_payload.replace(
            b'"source_text":"bitte',
            b'"source_text":"Bitte',
            1,
        )
    )
    with pytest.raises(
        TrainingIntegrityError,
        match="safety source differs",
    ):
        bind_improvement_order_plan(
            training_config,
            dataset,
            protection_policy=bound_policy,
            parent_checkpoint=bound_parent,
            protection_application=selected_protection.evidence,
        )

    with pytest.raises(RemediationExperimentError, match="existing"):
        build_remediation_experiment(config_path, output_dir=output)


def test_frozen_effective_order_is_not_rebucketed_by_new_marker_lengths(
    tmp_path: Path,
) -> None:
    config_path, _ = _fixture(tmp_path)
    output = tmp_path / "bundle"
    build_remediation_experiment(config_path, output_dir=output)
    plan, _ = load_remediation_order_plan(output / "shuffled.order-plan.json")
    declared = list(plan["epoch_orders"][0]["example_ids"])
    frozen = list(reversed(declared))
    plan["effective_order_lock"] = {
        "algorithm": "frozen_source_effective_order_v1",
        "source_order_plan": {
            "filename": "source-shuffled.order-plan.json",
            "sha256": "a" * 64,
        },
        "source_training_report": {
            "filename": "source-shuffled.training-report.json",
            "sha256": "b" * 64,
        },
        "example_ids": frozen,
        "example_ids_sha256": hash_identifier_sequence(frozen),
    }
    validated = validate_remediation_order_plan(plan)
    policy = build_gemma_unused_policy_evidence(
        _Tokenizer(),
        tokenizer_identity=_identity(),
    )
    selected = [
        json.loads(line)
        for line in (output / "selected-train.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    protected = apply_training_protection(selected, policy=policy)
    used = list(protected.accepted_example_ids)

    effective = build_effective_improvement_order(
        validated,
        plan_binding={
            "path": "C:/repair/repair.order-plan.json",
            "sha256": "sha256:" + "c" * 64,
        },
        loaded_example_ids=used,
        safe_example_ids=used,
        policy_rejected_example_ids=[],
        policy_rejection_evidence=protected.evidence,
        used_example_ids=used,
        sequence_rejected_example_ids=[],
        sequence_lengths=list(range(1, len(used) + 1)),
        window_size=2,
    )
    observed = [used[index] for index in effective.epoch_indices[0]]

    assert observed == frozen
    assert effective.evidence["bucket_algorithm"] == (
        "frozen_source_effective_order_v1"
    )
    assert effective.evidence["effective_epoch_orders"][0][
        "effective_example_ids_sha256"
    ] == hash_identifier_sequence(frozen)


def test_repair_plan_preserves_old_selection_filter_while_training_new_policy(
    tmp_path: Path,
) -> None:
    config_path, _ = _fixture(tmp_path)
    output = tmp_path / "bundle"
    build_remediation_experiment(config_path, output_dir=output)
    old_plan, _ = load_remediation_order_plan(
        output / "shuffled.order-plan.json"
    )
    old_config = load_training_config(output / "shuffled.train.yaml")
    old_policy_path = Path(str(old_config.values["protection_policy_path"]))
    old_policy_payload = old_policy_path.read_bytes()
    (output / "selection-policy.json").write_bytes(old_policy_payload)
    lexical_policy = build_lexical_keep_policy_evidence(
        _LexicalTokenizer(),
        tokenizer_identity=_identity(),
    )
    lexical_payload = canonical_json_bytes(lexical_policy)
    (output / "training-policy.json").write_bytes(lexical_payload)
    repair_plan = copy.deepcopy(old_plan)
    repair_plan["comparison_pair_sha256"] = "d" * 64
    repair_plan["selection_policy"] = {
        "filename": "selection-policy.json",
        **repair_plan["protection_policy"],
    }
    repair_plan["protection_policy"] = {
        "policy_id": lexical_policy["policy_id"],
        "policy_sha256": lexical_policy["policy_sha256"],
        "artifact_sha256": hashlib.sha256(lexical_payload).hexdigest(),
    }
    repair_plan = validate_remediation_order_plan(repair_plan)
    repair_plan_payload = canonical_json_bytes(repair_plan)
    (output / "repair.order-plan.json").write_bytes(repair_plan_payload)
    repair_config = dict(old_config.values)
    repair_config["protection_policy_path"] = "training-policy.json"
    repair_config["protection_policy_sha256"] = hashlib.sha256(
        lexical_payload
    ).hexdigest()
    repair_config["training_order_plan_path"] = "repair.order-plan.json"
    repair_config["training_order_plan_sha256"] = hashlib.sha256(
        repair_plan_payload
    ).hexdigest()
    repair_config_path = output / "repair.train.yaml"
    repair_config_path.write_text(
        yaml.safe_dump(repair_config, sort_keys=False),
        encoding="utf-8",
    )
    training_config = load_training_config(repair_config_path)
    dataset = bind_training_dataset(
        output / "dataset-manifest.json",
        train=output / "selected-train.jsonl",
        validation=output / "safe-validation.jsonl",
        train_limit=training_config.values["train_examples"],
        validation_limit=training_config.values["validation_examples"],
    )
    bound_policy = bind_protection_policy(
        training_config,
        tokenizer=_LexicalTokenizer(),
    )
    bound_parent = bind_parent_checkpoint(training_config)
    assert bound_policy is not None
    assert bound_parent is not None
    selected_protection = apply_training_protection(
        dataset.train.records,
        policy=bound_policy.values,
    )

    bound_plan = bind_improvement_order_plan(
        training_config,
        dataset,
        protection_policy=bound_policy,
        parent_checkpoint=bound_parent,
        protection_application=selected_protection.evidence,
    )

    assert bound_plan.values["selection_policy"]["policy_id"] == (
        "gemma_unused_v1"
    )
    assert bound_plan.values["protection_policy"]["policy_id"] == (
        "gemma_lexical_v1"
    )


def test_marker_repair_builder_freezes_only_the_policy_mechanism_change(
    tmp_path: Path,
) -> None:
    source_config_path, _ = _fixture(tmp_path)
    source_bundle = tmp_path / "source-bundle"
    build_remediation_experiment(
        source_config_path,
        output_dir=source_bundle,
    )
    source_manifest_path = source_bundle / "experiment-manifest.json"
    source_plan, _ = load_remediation_order_plan(
        source_bundle / "shuffled.order-plan.json"
    )
    source_training_config = load_training_config(
        source_bundle / "shuffled.train.yaml"
    )
    selection_policy_path = Path(
        str(source_training_config.values["protection_policy_path"])
    )
    training_policy_path = tmp_path / "training-policy.json"
    training_policy_payload = canonical_json_bytes(
        build_lexical_keep_policy_evidence(
            _LexicalTokenizer(),
            tokenizer_identity=_identity(),
        )
    )
    training_policy_path.write_bytes(training_policy_payload)

    source_run_identity_path = tmp_path / "source-run-identity.json"
    source_run_identity_payload = canonical_json_bytes(
        {
            "schema_version": 1,
            "fingerprint_algorithm": "sha256_canonical_json_v1",
            "run_fingerprint": "sha256:" + "9" * 64,
            "bindings": {},
        }
    )
    source_run_identity_path.write_bytes(source_run_identity_payload)
    source_training_report_path = tmp_path / "source-training-report.json"
    source_training_report_payload = canonical_json_bytes(
        {
            "schema_version": 1,
            "training_completed": True,
            "run_fingerprint": "sha256:" + "9" * 64,
            "run_identity": {
                "path": str(source_run_identity_path),
                "sha256": _prefixed_sha256(source_run_identity_payload),
            },
            "remediation_order": {
                "mode": "shuffled",
                "record_count": 20,
                "bucket_algorithm": "fixed_window_length_descending_v1",
                "bucket_window": 64,
                "effective_epoch_orders": [
                    {
                        "epoch": 0,
                        "record_count": 20,
                        "effective_example_ids_sha256": source_plan[
                            "epoch_orders"
                        ][0]["example_ids_sha256"],
                    }
                ],
            },
        }
    )
    source_training_report_path.write_bytes(source_training_report_payload)

    baseline_paths: dict[str, Path] = {}
    for name, payload in (
        ("prediction_manifest", {"schema_version": 1, "kind": "predictions"}),
        ("evaluation", {"schema_version": 1, "kind": "evaluation"}),
        ("comparison", {"schema_version": 1, "kind": "comparison"}),
    ):
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_json_bytes(payload))
        baseline_paths[name] = path
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_bytes(b'{"example_id":"baseline"}\n')
    baseline_paths["predictions"] = predictions_path

    def binding(path: Path) -> dict[str, str]:
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    repair_config_path = tmp_path / "marker-repair.yaml"
    repair_config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "experiment_id": "pilot-marker-lexical-repair-v1",
                "inputs": {
                    "source_experiment_manifest": binding(
                        source_manifest_path
                    ),
                    "source_shuffled_run_identity": binding(
                        source_run_identity_path
                    ),
                    "source_shuffled_training_report": binding(
                        source_training_report_path
                    ),
                    "selection_policy": binding(selection_policy_path),
                    "training_policy": binding(training_policy_path),
                    "parent_checkpoint": {
                        "path": str(
                            source_training_config.values[
                                "parent_checkpoint_path"
                            ]
                        ),
                        "tree_sha256": source_training_config.values[
                            "parent_checkpoint_tree_sha256"
                        ],
                    },
                },
                "baseline": {
                    name: binding(path)
                    for name, path in baseline_paths.items()
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "repair-bundle"

    bundle = build_marker_repair_bundle(
        repair_config_path,
        output_dir=output,
        tokenizer=_LexicalTokenizer(),
    )

    manifest = bundle.manifest
    assert manifest["kind"] == "scriber_marker_repair_bundle"
    assert manifest["schedule"] == {
        "effective_batch_size": 16,
        "epochs": 1,
        "update_steps": 2,
    }
    assert manifest["policies"]["selection"]["policy_id"] == (
        "gemma_unused_v1"
    )
    assert manifest["policies"]["training"]["policy_id"] == (
        "gemma_lexical_v1"
    )
    assert manifest["preflight"]["train"]["accepted_record_count"] == 20
    assert manifest["preflight"]["train"]["sequence_rejected_record_count"] == 0
    assert manifest["preflight"]["validation"]["accepted_record_count"] == 10
    assert manifest["preflight"]["validation"][
        "sequence_rejected_record_count"
    ] == 0
    repair_plan, _ = load_remediation_order_plan(
        output / "frozen-effective.order-plan.json"
    )
    assert repair_plan["effective_order_lock"]["example_ids_sha256"] == (
        source_plan["epoch_orders"][0]["example_ids_sha256"]
    )
    assert repair_plan["selection_policy"]["policy_id"] == "gemma_unused_v1"
    assert repair_plan["protection_policy"]["policy_id"] == "gemma_lexical_v1"
    repair_training_config = load_training_config(output / "train.yaml")
    assert repair_training_config.values["train_examples"] == 20
    assert repair_training_config.values["validation_examples"] == 10
    assert repair_training_config.values["training_order_plan_path"] == (
        "frozen-effective.order-plan.json"
    )
    assert (
        output / "selected-train.jsonl"
    ).read_bytes() == (
        source_bundle / "selected-train.jsonl"
    ).read_bytes()
    assert (
        output / "safe-validation.jsonl"
    ).read_bytes() == (
        source_bundle / "safe-validation.jsonl"
    ).read_bytes()
    dry_bind = verify_marker_repair_bundle(
        output,
        tokenizer=_LexicalTokenizer(),
    )
    assert dry_bind["ready_for_model_load"] is True
    assert dry_bind["update_steps"] == 2
    assert dry_bind["effective_order_sha256"] == source_plan[
        "epoch_orders"
    ][0]["example_ids_sha256"]
    cloud_packet = build_hf_marker_repair_packet(
        bundle_dir=output,
        output_dir=tmp_path / "hf-job-packet",
        tokenizer=_LexicalTokenizer(),
        budget_usd=5.0,
        flavor="l4x1",
        timeout_minutes=60,
    )
    assert cloud_packet.manifest["maximum_job_cost_usd"] == 0.8
    assert cloud_packet.manifest["budget_usd"] == 5.0
    assert cloud_packet.manifest["training"]["update_steps"] == 2
    assert (
        cloud_packet.output_dir
        / "repo"
        / "ml"
        / "scriber_polishing"
        / "job-input"
        / "repair-manifest.json"
    ).is_file()
    assert (
        cloud_packet.output_dir / "run-marker-repair.sh"
    ).is_file()
    assert b"--break-system-packages" in (
        cloud_packet.output_dir / "run-marker-repair.sh"
    ).read_bytes()
    runner_payload = (
        cloud_packet.output_dir / "run-marker-repair.sh"
    ).read_bytes()
    assert b"export HF_HUB_OFFLINE=1" in runner_payload
    assert b"export TRANSFORMERS_OFFLINE=1" in runner_payload
    assert b'cp -a --no-preserve=ownership "$INPUT_ROOT" "$WORK_ROOT"' in (
        runner_payload
    )
    assert b"unset GIT_DIR GIT_WORK_TREE" in runner_payload
    assert b'git -C "$WORK_ROOT" init' in runner_payload
    assert b'"ml/scriber_polishing/src"' in runner_payload
    assert b'"ml/scriber_polishing/scripts"' in runner_payload
    assert b'"ml/scriber_polishing/contracts"' in runner_payload
    assert b"git -C \"$WORK_ROOT\" add ml/scriber_polishing\n" not in (
        runner_payload
    )
    assert b"--tokenizer-path job-input/parent-checkpoint" in runner_payload
    assert b"--gate-candidate shuffled" in runner_payload
    assert runner_payload.index(b"scripts/verify_marker_repair.py") < (
        runner_payload.index(b'print("cloud_runtime="')
    )


def test_plan_and_config_fail_closed_on_tampering(tmp_path: Path) -> None:
    config_path, config = _fixture(tmp_path)
    output = tmp_path / "bundle"
    build_remediation_experiment(config_path, output_dir=output)
    plan, _ = load_remediation_order_plan(
        output / "staged.order-plan.json"
    )

    tampered = copy.deepcopy(plan)
    tampered["epoch_orders"][0]["example_ids"] = list(
        reversed(tampered["epoch_orders"][0]["example_ids"])
    )
    with pytest.raises(RemediationExperimentError, match="hash|algorithm"):
        validate_remediation_order_plan(tampered)

    invalid_config = copy.deepcopy(config)
    invalid_config["training"]["gradient_accumulation_steps"] = 4
    with pytest.raises(RemediationExperimentError, match="batch size 16"):
        validate_remediation_experiment_config(invalid_config)

    invalid_intervals = copy.deepcopy(config)
    invalid_intervals["training"]["eval_steps"] = 3
    invalid_intervals["training"]["save_steps"] = 3
    invalid_intervals_path = tmp_path / "invalid-intervals.yaml"
    invalid_intervals_path.write_text(
        yaml.safe_dump(invalid_intervals, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(RemediationExperimentError, match="intervals exceed"):
        build_remediation_experiment(
            invalid_intervals_path,
            output_dir=tmp_path / "invalid-intervals-bundle",
        )


def test_training_core_rejects_every_cross_binding_tamper(
    tmp_path: Path,
) -> None:
    config_path, _ = _fixture(tmp_path)
    output = tmp_path / "bundle"
    build_remediation_experiment(config_path, output_dir=output)
    shuffled, _ = load_remediation_order_plan(
        output / "shuffled.order-plan.json"
    )
    staged, _ = load_remediation_order_plan(
        output / "staged.order-plan.json"
    )
    original_config = yaml.safe_load(
        (output / "shuffled.train.yaml").read_text(encoding="utf-8")
    )
    dataset = bind_training_dataset(
        output / "dataset-manifest.json",
        train=output / "selected-train.jsonl",
        validation=output / "safe-validation.jsonl",
        train_limit=original_config["train_examples"],
        validation_limit=original_config["validation_examples"],
    )
    policy = build_gemma_unused_policy_evidence(
        _Tokenizer(),
        tokenizer_identity=_identity(),
    )
    selected_protection = apply_training_protection(
        dataset.train.records,
        policy=policy,
    )

    source_tamper = copy.deepcopy(shuffled)
    source_tamper["source"]["sha256"] = "0" * 64
    policy_tamper = copy.deepcopy(shuffled)
    policy_tamper["protection_policy"]["artifact_sha256"] = "0" * 64
    parent_tamper = copy.deepcopy(shuffled)
    parent_tamper["parent_checkpoint"]["tree_sha256"] = (
        "sha256:" + "0" * 64
    )
    contract_tamper = copy.deepcopy(shuffled)
    contract_tamper["training_contract"]["learning_rate"] = 0.00002
    rejection_tamper = copy.deepcopy(shuffled)
    rejection_tamper["safety_filter"]["rejected_example_ids_sha256"] = (
        "0" * 64
    )
    cases = (
        (staged, "mode differs"),
        (source_tamper, "source differs"),
        (policy_tamper, "protection policy differs"),
        (parent_tamper, "parent differs"),
        (contract_tamper, "training contract differs"),
        (rejection_tamper, "safety filter differs"),
    )
    plan_path = output / "tampered.order-plan.json"
    training_path = output / "tampered.train.yaml"
    for tampered_plan, message in cases:
        plan_payload = canonical_json_bytes(tampered_plan)
        plan_path.write_bytes(plan_payload)
        training_value = {
            **original_config,
            "training_order_plan_path": plan_path.name,
            "training_order_plan_sha256": hashlib.sha256(
                plan_payload
            ).hexdigest(),
        }
        training_path.write_text(
            yaml.safe_dump(training_value, sort_keys=False),
            encoding="utf-8",
        )
        training_config = load_training_config(training_path)
        bound_policy = bind_protection_policy(
            training_config,
            tokenizer=_Tokenizer(),
        )
        bound_parent = bind_parent_checkpoint(training_config)
        assert bound_policy is not None
        assert bound_parent is not None
        with pytest.raises(TrainingIntegrityError, match=message):
            bind_improvement_order_plan(
                training_config,
                dataset,
                protection_policy=bound_policy,
                parent_checkpoint=bound_parent,
                protection_application=selected_protection.evidence,
            )

    interval_plan = copy.deepcopy(shuffled)
    interval_plan["training_contract"]["eval_steps"] = 3
    interval_plan["training_contract"]["save_steps"] = 3
    interval_payload = canonical_json_bytes(interval_plan)
    plan_path.write_bytes(interval_payload)
    interval_training = {
        **original_config,
        "eval_steps": 3,
        "save_steps": 3,
        "training_order_plan_path": plan_path.name,
        "training_order_plan_sha256": hashlib.sha256(
            interval_payload
        ).hexdigest(),
    }
    training_path.write_text(
        yaml.safe_dump(interval_training, sort_keys=False),
        encoding="utf-8",
    )
    interval_config = load_training_config(training_path)
    interval_policy = bind_protection_policy(
        interval_config,
        tokenizer=_Tokenizer(),
    )
    interval_parent = bind_parent_checkpoint(interval_config)
    assert interval_policy is not None
    assert interval_parent is not None
    with pytest.raises(TrainingIntegrityError, match="intervals exceed"):
        bind_improvement_order_plan(
            interval_config,
            dataset,
            protection_policy=interval_policy,
            parent_checkpoint=interval_parent,
            protection_application=selected_protection.evidence,
        )


def test_cli_builds_bundle_without_training_or_holdout_arguments(
    tmp_path: Path,
) -> None:
    config_path, _ = _fixture(tmp_path)
    output = tmp_path / "cli-bundle"
    project_root = Path(__file__).parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_remediation_experiment.py"),
            "--config",
            str(config_path),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "holdout_inputs=false" in completed.stdout
    assert (output / "experiment-manifest.json").is_file()
