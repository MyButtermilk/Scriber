from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scriber_polishing.inference import load_inference_protection_policy
from scriber_polishing.model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from scriber_polishing.protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    PROTECTION_POLICY_FILENAME,
    ProtectionPolicyError,
    build_gemma_unused_policy_evidence,
    build_lexical_keep_policy_evidence,
)
from scriber_polishing.training_integrity import (
    TrainingIntegrityError,
    bind_parent_checkpoint,
    bind_protection_policy,
    checkpoint_tree_sha256,
    load_training_config,
    verify_checkpoint_protection_policy,
)
from scriber_polishing.training_runtime import apply_training_protection


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
        return list(self._token_ids[marker])

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        del skip_special_tokens
        return self._decoded[tuple(token_ids)]


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


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
    core = {
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
        "backend_sha256": _sha256(backend),
        "chat_template_sha256": _sha256(template),
    }
    core["identity_sha256"] = _sha256(
        (
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    return core


def _policy() -> dict[str, object]:
    return build_gemma_unused_policy_evidence(
        _Tokenizer(),
        tokenizer_identity=_identity(),
    )


def _lexical_policy() -> dict[str, object]:
    return build_lexical_keep_policy_evidence(
        _LexicalTokenizer(),
        tokenizer_identity=_identity(),
    )


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _improvement_config(
    policy_path: Path,
    parent: Path,
    *,
    policy_file_sha256: str,
    parent_tree_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_kind": "improvement",
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "method": "full_sft",
        "seed": 1,
        "epochs": 1,
        "minimum_train_examples": 1,
        "minimum_validation_examples": 1,
        "max_sequence_length": 128,
        "micro_batch_size": 1,
        "gradient_accumulation_steps": 16,
        "learning_rate": 0.00001,
        "bf16": True,
        "gradient_checkpointing": True,
        "optimizer": "adamw_torch_fused",
        "eval_steps": 1,
        "save_steps": 1,
        "save_total_limit": 1,
        "warmup_ratio": 0.0,
        "weight_decay": 0.0,
        "max_grad_norm": 1.0,
        "assistant_output_loss_only": True,
        "protection_policy_path": str(policy_path),
        "protection_policy_sha256": policy_file_sha256,
        "parent_checkpoint_path": str(parent),
        "parent_checkpoint_tree_sha256": parent_tree_sha256,
        "training_order_mode": "shuffled",
        "training_order_plan_path": "remediation.plan.json",
        "training_order_plan_sha256": "f" * 64,
    }


def test_training_protection_records_rejections_and_binds_transformed_pairs() -> None:
    records = [
        {
            "example_id": "accepted",
            "source_text": "Bitte 12 € prüfen.",
            "target_sst": "[DOC]\n[P]Bitte 12 € prüfen.[/P]\n[/DOC]",
        },
        {
            "example_id": "rejected",
            "source_text": "Wert 12.",
            "target_sst": "[DOC]\n[P]Wert 13.[/P]\n[/DOC]",
        },
    ]

    result = apply_training_protection(records, policy=_policy())

    assert [record["example_id"] for record in result.records] == ["accepted"]
    assert "<unused6200>" in result.records[0]["source_text"]
    assert "<unused6200>" in result.records[0]["target_sst"]
    assert result.rejected_example_ids == ("rejected",)
    assert result.evidence["accepted_record_count"] == 1
    assert result.evidence["rejected_record_count"] == 1
    assert result.evidence["rejection_reason_counts"] == {
        "protected_value_mismatch": 1
    }
    assert result.evidence["rejection_example_ids_by_reason"] == {
        "protected_value_mismatch": ["rejected"]
    }
    assert result.evidence["rejection_example_ids_sha256_by_reason"][
        "protected_value_mismatch"
    ]
    assert result.evidence["accepted_example_ids_sha256"]
    assert result.evidence["rejected_example_ids_sha256"]
    assert result.evidence["transformed_pairs_sha256"]


def test_training_protection_uses_declared_values_and_reversible_surfaces() -> None:
    records = [
        {
            "example_id": "mapped",
            "source_text": "Bitte 12 Euro sowie Punkt 1 prüfen.",
            "target_sst": "[DOC]\n[P]Bitte 12 € sowie Punkt 1 prüfen.[/P]\n[/DOC]",
            "protected_spans": ["12 €"],
            "value_mappings": [
                {
                    "kind": "unit_surface",
                    "source": "12 Euro",
                    "target": "12 €",
                }
            ],
        }
    ]

    result = apply_training_protection(records, policy=_lexical_policy())

    assert result.evidence["accepted_record_count"] == 1
    assert result.evidence["rejected_record_count"] == 0
    assert result.evidence["declared_protection_record_count"] == 1
    assert result.evidence["runtime_detector_record_count"] == 0
    assert result.evidence["mapped_protected_occurrence_count"] == 1
    assert "⟦KEEP_A⟧" in result.records[0]["source_text"]
    assert "⟦KEEP_A⟧" in result.records[0]["target_sst"]
    assert "Punkt 1" in result.records[0]["source_text"]
    assert "Punkt 1" in result.records[0]["target_sst"]


def test_declared_training_protection_rejects_unmapped_content_change() -> None:
    records = [
        {
            "example_id": "unsafe",
            "source_text": "Bitte 12 Euro prüfen.",
            "target_sst": "[DOC]\n[P]Bitte 13 € prüfen.[/P]\n[/DOC]",
            "protected_spans": ["13 €"],
            "value_mappings": [],
        }
    ]

    with pytest.raises(ValueError, match="rejected every training record"):
        apply_training_protection(records, policy=_lexical_policy())


def test_declared_training_protection_allows_only_duplicate_consolidation() -> None:
    records = [
        {
            "example_id": "duplicate",
            "source_text": "Wert 12 €, ich korrigiere: Wert 12 €.",
            "target_sst": "[DOC]\n[P]Wert 12 €.[/P]\n[/DOC]",
            "protected_spans": ["12 €"],
            "value_mappings": [],
        }
    ]

    result = apply_training_protection(records, policy=_lexical_policy())

    assert result.evidence["accepted_record_count"] == 1
    assert result.evidence["deduplicated_source_occurrence_count"] == 1
    assert result.records[0]["source_text"].count("⟦KEEP_A⟧") == 1
    assert result.records[0]["source_text"].count("12 €") == 1
    assert result.records[0]["target_sst"].count("⟦KEEP_A⟧") == 1


def test_improvement_config_binds_policy_and_parent_as_new_run_inputs(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_payload = _canonical(_policy())
    policy_path.write_bytes(policy_payload)
    parent = tmp_path / "pilot-final"
    parent.mkdir()
    (parent / "config.json").write_text("{}", encoding="utf-8")
    (parent / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (parent / "model.safetensors").write_bytes(b"pilot")
    config_path = tmp_path / "improvement.yaml"
    config_path.write_text(
        yaml.safe_dump(
            _improvement_config(
                policy_path,
                parent,
                policy_file_sha256=hashlib.sha256(policy_payload).hexdigest(),
                parent_tree_sha256=checkpoint_tree_sha256(parent),
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_training_config(config_path)
    policy = bind_protection_policy(config, tokenizer=_Tokenizer())
    bound_parent = bind_parent_checkpoint(config)

    assert policy is not None
    assert policy.values["policy_id"] == GEMMA_UNUSED_POLICY_ID
    assert bound_parent is not None
    assert bound_parent.binding()["relationship"] == (
        "warm_start_parent_not_resume"
    )
    assert bound_parent.tree_sha256 == checkpoint_tree_sha256(parent)

    invalid = _improvement_config(
        policy_path,
        parent,
        policy_file_sha256=hashlib.sha256(policy_payload).hexdigest(),
        parent_tree_sha256="0" * 64,
    )
    config_path.write_text(
        yaml.safe_dump(invalid, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(TrainingIntegrityError, match="tree SHA-256 differs"):
        bind_parent_checkpoint(load_training_config(config_path))


def test_lexical_policy_binds_identically_in_training_and_inference(
    tmp_path: Path,
) -> None:
    policy = _lexical_policy()
    policy_payload = _canonical(policy)
    policy_path = tmp_path / PROTECTION_POLICY_FILENAME
    policy_path.write_bytes(policy_payload)
    parent = tmp_path / "pilot-final"
    parent.mkdir()
    (parent / "config.json").write_text("{}", encoding="utf-8")
    (parent / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (parent / "model.safetensors").write_bytes(b"pilot")
    config_path = tmp_path / "improvement.yaml"
    config_path.write_text(
        yaml.safe_dump(
            _improvement_config(
                policy_path,
                parent,
                policy_file_sha256=hashlib.sha256(policy_payload).hexdigest(),
                parent_tree_sha256=checkpoint_tree_sha256(parent),
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    bound = bind_protection_policy(
        load_training_config(config_path),
        tokenizer=_LexicalTokenizer(),
    )

    assert bound is not None
    assert bound.values["policy_id"] == LEXICAL_KEEP_POLICY_ID
    assert bound.binding()["marker_token_sequences_sha256"] == policy[
        "marker_token_sequences_sha256"
    ]
    model = SimpleNamespace(
        config=SimpleNamespace(
            scriber_protection_policy_id=LEXICAL_KEEP_POLICY_ID,
            scriber_protection_policy_sha256=policy["policy_sha256"],
        )
    )
    loaded, binding = load_inference_protection_policy(
        model_reference=str(tmp_path),
        tokenizer=_LexicalTokenizer(),
        model=model,
        revision=None,
        local_files_only=True,
    )
    assert loaded == policy
    assert binding is not None
    assert binding["policy_id"] == LEXICAL_KEEP_POLICY_ID


def test_improvement_config_requires_one_explicit_full_epoch_order(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "improvement.yaml"
    base = _improvement_config(
        tmp_path / "policy.json",
        tmp_path / "parent",
        policy_file_sha256="a" * 64,
        parent_tree_sha256="b" * 64,
    )

    invalid_cases = (
        (
            {
                key: value
                for key, value in base.items()
                if not key.startswith("training_order_")
            },
            "explicit remediation order plan",
        ),
        ({**base, "epochs": 2}, "exactly one full epoch"),
        (
            {
                **{key: value for key, value in base.items() if key != "epochs"},
                "max_steps": 2,
            },
            "max_steps must be absent",
        ),
        (
            {
                **base,
                "early_stopping": True,
                "early_stopping_patience": 1,
            },
            "early_stopping: false",
        ),
        (
            {**base, "gradient_accumulation_steps": 8},
            "effective batch size 16",
        ),
    )
    for invalid, message in invalid_cases:
        config_path.write_text(
            yaml.safe_dump(invalid, sort_keys=False),
            encoding="utf-8",
        )
        with pytest.raises(TrainingIntegrityError, match=message):
            load_training_config(config_path)


def test_new_inference_policy_requires_verified_model_artifact(
    tmp_path: Path,
) -> None:
    policy = _policy()
    artifact = tmp_path / PROTECTION_POLICY_FILENAME
    artifact.write_bytes(_canonical(policy))
    model = SimpleNamespace(
        config=SimpleNamespace(
            scriber_protection_policy_id=GEMMA_UNUSED_POLICY_ID,
            scriber_protection_policy_sha256=policy["policy_sha256"],
        )
    )

    loaded, binding = load_inference_protection_policy(
        model_reference=str(tmp_path),
        tokenizer=_Tokenizer(),
        model=model,
        revision=None,
        local_files_only=True,
    )

    assert loaded == policy
    assert binding is not None
    assert binding["source"] == "local_model_artifact"
    assert binding["policy_id"] == GEMMA_UNUSED_POLICY_ID

    artifact.unlink()
    with pytest.raises(ProtectionPolicyError, match="no verified policy"):
        load_inference_protection_policy(
            model_reference=str(tmp_path),
            tokenizer=_Tokenizer(),
            model=model,
            revision=None,
            local_files_only=True,
        )


def test_checkpoint_policy_requires_matching_file_and_model_config(
    tmp_path: Path,
) -> None:
    policy = _policy()
    (tmp_path / PROTECTION_POLICY_FILENAME).write_bytes(_canonical(policy))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "scriber_protection_policy_id": policy["policy_id"],
                "scriber_protection_policy_sha256": policy["policy_sha256"],
            }
        ),
        encoding="utf-8",
    )

    evidence = verify_checkpoint_protection_policy(
        tmp_path,
        expected_policy=policy,
        tokenizer=_Tokenizer(),
    )

    assert evidence["policy_id"] == GEMMA_UNUSED_POLICY_ID
    assert evidence["sha256"].startswith("sha256:")

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["scriber_protection_policy_sha256"] = "sha256:" + "0" * 64
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(TrainingIntegrityError, match="does not bind"):
        verify_checkpoint_protection_policy(
            tmp_path,
            expected_policy=policy,
            tokenizer=_Tokenizer(),
        )


def test_legacy_inference_without_artifact_remains_the_implicit_default(
    tmp_path: Path,
) -> None:
    policy, binding = load_inference_protection_policy(
        model_reference=str(tmp_path),
        tokenizer=_Tokenizer(),
        model=SimpleNamespace(config=SimpleNamespace()),
        revision=None,
        local_files_only=True,
    )

    assert policy["policy_id"] == "legacy_scriber_v1"
    assert binding is None
