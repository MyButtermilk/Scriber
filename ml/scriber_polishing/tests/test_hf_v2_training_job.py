from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scriber_polishing.hf_v2_training_job import (
    PARENT_CHECKPOINT_TREE_SHA256,
    POLICY_ARTIFACT_SHA256,
    V1_REPLAY_MANIFEST_SHA256,
    V1_REPLAY_TRAIN_SHA256,
    V1_REPLAY_VALIDATION_SHA256,
    V2_JOB_IMAGE,
    V2_MIX_SCHEMA_PATH,
    V2_POLICY_PATH,
    V2TrainingJobError,
    _launch_contract,
    _reconstruct_git_head,
    _require_hf_job_label_syntax,
    build_train_sft_argv,
    copy_allowlisted_parent,
    package_v2_training_job,
    prepare_owned_output,
    verify_training_mix,
    verify_v2_training_packet,
)

PRODUCTION_POLICY_PATH = V2_POLICY_PATH


def test_launch_contract_labels_follow_hf_jobs_api_pattern() -> None:
    contract = _launch_contract(
        source_git_head="1" * 40,
        packet_tree_sha256="sha256:" + "2" * 64,
        manifest_sha256="sha256:" + "3" * 64,
        inventory_sha256="sha256:" + "4" * 64,
    )
    argv = contract["argv"]
    assert isinstance(argv, list)
    labels = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "--label"]
    invalid = [
        label
        for label in labels
        if "=" not in label
        or any(re.fullmatch(r"[a-zA-Z0-9._-]+", component) is None for component in label.split("=", 1))
    ]

    assert invalid == []


def test_hf_job_label_gate_rejects_a_path_before_submission() -> None:
    with pytest.raises(V2TrainingJobError, match="output-prefix"):
        _require_hf_job_label_syntax(["--label", "output-prefix=attempt13/scriber-v2-hardcase-replay-v1"])


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _assert_schema(instance_path: Path, schema_name: str) -> None:
    schema_path = Path(__file__).parents[1] / "contracts" / schema_name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(instance)


def _write_mix(root: Path) -> dict[str, object]:
    root.mkdir()
    train = b"".join(
        json.dumps({"example_id": f"train-{index:05d}"}, separators=(",", ":")).encode() + b"\n"
        for index in range(20_480)
    )
    validation = b"".join(
        json.dumps({"example_id": f"validation-{index:05d}"}, separators=(",", ":")).encode() + b"\n"
        for index in range(1_280)
    )
    (root / "train.jsonl").write_bytes(train)
    (root / "validation.jsonl").write_bytes(validation)
    digest = "sha256:" + "1" * 64
    manifest: dict[str, object] = {
        "schema_version": 1,
        "kind": "scriber_v2_training_mix",
        "training_ready": True,
        "seed": 270_023,
        "selection_algorithm": "v1_split_unique_pair_sha256_bottom_k_then_source_sha256_interleave_v2",
        "manifest_schema_sha256": _sha256(V2_MIX_SCHEMA_PATH.read_bytes()),
        "split_files": {"train": "train.jsonl", "validation": "validation.jsonl"},
        "split_counts": {"train": 20_480, "validation": 1_280},
        "split_sha256": {"train": _sha256(train), "validation": _sha256(validation)},
        "source_counts": {
            "train": {"v1_replay": 10_240, "v2_hardcase": 10_240},
            "validation": {"v1_replay": 640, "v2_hardcase": 640},
        },
        "mixed_order": {
            "example_ids_sha256": {"train": digest, "validation": digest},
            "source_assignment_sha256": {"train": digest, "validation": digest},
        },
        "quality_gates": {
            "allowed_splits_only": True,
            "duplicate_example_id_count": 0,
            "duplicate_pair_count": 0,
            "normalized_nfc": True,
            "parent_split_leakage_count": 0,
            "schema_validated_pair_count": 21_760,
            "source_provenance_binding": True,
        },
        "provenance_policy": {
            "allowed_source_splits": ["train", "validation"],
            "test_or_challenge_opened": False,
            "row_provenance_preserved": True,
            "v1_rows_modified": False,
            "v2_rows_modified": False,
            "v2_critic_evidence_required": True,
        },
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "contains_test_or_challenge_split": False,
        "direct_test_or_challenge_reuse": False,
        "sources": {
            "v2_hardcase": {
                "kind": "scriber_v2_reviewed_hardcase_dataset",
                "training_ready": True,
                "manifest_file": "manifest.json",
                "manifest_sha256": digest,
                "artifact_tree_sha256": digest,
                "source_split_files": {"train": "train.jsonl", "validation": "validation.jsonl"},
                "source_split_sha256": {"train": digest, "validation": digest},
                "source_split_counts": {"train": 10_240, "validation": 640},
                "selected_counts": {"train": 10_240, "validation": 640},
                "selected_example_ids_sha256": {"train": digest, "validation": digest},
                "review_candidate_sha256": digest,
                "review_contract_sha256": digest,
                "review_package_sha256": digest,
                "review_evidence_sha256": digest,
                "review_evidence_tree_sha256": digest,
                "reviewer_ids": ["fact_critic_01", "style_critic_01", "adversarial_critic_01", "adversarial_critic_02"],
                "role_counts": {"fact": 1, "style": 1, "adversarial": 2},
            },
            "v1_replay": {
                "kind": "scriber_v1_frozen_replay",
                "manifest_file": "dataset_manifest.json",
                "manifest_sha256": V1_REPLAY_MANIFEST_SHA256,
                "source_split_files": {
                    "train": "splits/train.jsonl",
                    "validation": "splits/validation.jsonl",
                },
                "source_split_sha256": {
                    "train": V1_REPLAY_TRAIN_SHA256,
                    "validation": V1_REPLAY_VALIDATION_SHA256,
                },
                "source_split_counts": {"train": 120_000, "validation": 10_010},
                "selected_counts": {"train": 10_240, "validation": 640},
                "selected_example_ids_sha256": {"train": digest, "validation": digest},
                "parent_split_group": "parent_canonical_document_id",
                "sealed_splits": ["test", "challenge"],
            },
        },
    }
    identity = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    manifest["artifact_tree_sha256"] = _sha256(identity)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def test_reviewed_mix_and_fixed_policy_are_training_ready(tmp_path: Path) -> None:
    mix_root = tmp_path / "mix"
    manifest = _write_mix(mix_root)

    binding = verify_training_mix(mix_root)

    assert binding["manifest"] == manifest
    assert binding["split_counts"] == {"train": 20_480, "validation": 1_280}
    assert binding["critic_evidence"]["role_counts"] == {"adversarial": 2, "fact": 1, "style": 1}
    assert _sha256(PRODUCTION_POLICY_PATH.read_bytes()) == POLICY_ARTIFACT_SHA256


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.__setitem__("training_ready", False), "schema validation|training-ready"),
        (
            lambda value: value["sources"]["v2_hardcase"].__setitem__(
                "role_counts", {"fact": 1, "style": 1, "adversarial": 1}
            ),
            "schema validation|critic",
        ),
        (
            lambda value: value.__setitem__("contains_test_or_challenge_split", True),
            "schema validation|test/challenge",
        ),
        (
            lambda value: value["source_counts"]["train"].__setitem__("v1_replay", 10_239),
            "schema validation|source counts",
        ),
        (
            lambda value: value["provenance_policy"].__setitem__("test_or_challenge_opened", True),
            "schema validation|provenance policy",
        ),
    ],
)
def test_mix_gate_rejects_unreviewed_or_holdout_inputs(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    root = tmp_path / "mix"
    manifest = copy.deepcopy(_write_mix(root))
    mutation(manifest)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(V2TrainingJobError, match=message):
        verify_training_mix(root)


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True, shell=False)
    return completed.stdout.strip()


def _source_repository(root: Path) -> tuple[str, str]:
    config = """schema_version: 1
run_kind: final
base_model: google/gemma-3-270m-it
base_revision: ac82b4e820549b854eebf28ce6dedaf9fdfa17b3
method: full_sft
seed: 270023
epochs: 1
train_examples: 20480
validation_examples: 1280
max_sequence_length: 1024
micro_batch_size: 4
gradient_accumulation_steps: 8
learning_rate: 5.0e-6
optimizer: adamw_torch_fused
eval_steps: 100
save_steps: 200
save_total_limit: 3
warmup_ratio: 0.03
weight_decay: 0.01
max_grad_norm: 1.0
bf16: true
gradient_checkpointing: true
assistant_output_loss_only: true
early_stopping: false
protection_policy_path: training-policy.json
protection_policy_sha256: 8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d
parent_checkpoint_path: parent-v1-checkpoint
parent_checkpoint_tree_sha256: 4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4
"""
    paths = {
        "ml/scriber_polishing/scripts/train_sft.py": "print('train')\n",
        "ml/scriber_polishing/scripts/run_hf_v2_training_job.py": "print('runner')\n",
        "ml/scriber_polishing/src/scriber_polishing/hf_v2_training_job.py": "VALUE = 1\n",
        "ml/scriber_polishing/configs/train_v2_hardcase_mixed.yaml": config,
        "ml/scriber_polishing/configs/training-policy-gemma-lexical-v1.json": V2_POLICY_PATH.read_text(
            encoding="utf-8"
        ),
        "ml/scriber_polishing/contracts/v2_training_mix_manifest_schema.json": V2_MIX_SCHEMA_PATH.read_text(
            encoding="utf-8"
        ),
        "ml/scriber_polishing/pyproject.toml": "[project]\nname='fixture'\nversion='1'\n",
    }
    for relative, content in paths.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "V2 Test", cwd=root)
    _git("config", "user.email", "v2-test@invalid.local", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-m", "fixture", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root), _git("rev-parse", "HEAD^{tree}", cwd=root)


def test_packet_binds_exact_git_head_data_policy_runner_and_launch_contract(tmp_path: Path) -> None:
    repository = tmp_path / "source-repository"
    repository.mkdir()
    head, git_tree = _source_repository(repository)
    mix = tmp_path / "mix"
    _write_mix(mix)
    packet = tmp_path / "packet"

    result = package_v2_training_job(
        repository_root=repository,
        dataset_root=mix,
        policy_path=PRODUCTION_POLICY_PATH,
        output_dir=packet,
    )
    verified = verify_v2_training_packet(
        packet,
        expected_packet_tree_sha256=result["packet_tree_sha256"],
        expected_manifest_sha256=result["manifest_sha256"],
        expected_launch_contract_sha256=result["launch_contract_sha256"],
    )

    assert verified["source_git_head"] == head
    assert verified["source_git_tree"] == git_tree
    assert verified["training"] == {"epochs": 1, "learning_rate": 5e-6, "bf16": True}
    assert verified["parent_checkpoint_tree_sha256"] == PARENT_CHECKPOINT_TREE_SHA256
    contract = json.loads((packet / "launch-contract.json").read_text(encoding="utf-8"))
    assert contract["image"] == V2_JOB_IMAGE
    assert contract["argv"][:4] == ["hf", "jobs", "run", "--detach"]
    assert contract["argv"].count("--detach") == 1
    assert contract["remote_output_absence_required"] is True
    _assert_schema(packet / "tree-inventory.json", "hf_v2_training_tree_inventory_schema.json")
    _assert_schema(packet / "package-manifest.json", "hf_v2_training_packet_manifest_schema.json")
    _assert_schema(packet / "launch-contract.json", "hf_v2_training_launch_contract_schema.json")
    manifest = json.loads((packet / "package-manifest.json").read_text(encoding="utf-8"))
    inventory = json.loads((packet / "tree-inventory.json").read_text(encoding="utf-8"))
    reconstructed = tmp_path / "reconstructed"
    _reconstruct_git_head(packet, reconstructed, manifest, inventory["files"])
    assert _git("rev-parse", "HEAD", cwd=reconstructed) == head
    assert _git("status", "--porcelain=v1", "--untracked-files=all", cwd=reconstructed) == ""

    (packet / "inputs" / "train.jsonl").write_bytes(b"{}\n")
    with pytest.raises(V2TrainingJobError, match="tree|SHA-256"):
        verify_v2_training_packet(
            packet,
            expected_packet_tree_sha256=result["packet_tree_sha256"],
            expected_manifest_sha256=result["manifest_sha256"],
            expected_launch_contract_sha256=result["launch_contract_sha256"],
        )


def test_parent_copy_is_an_exact_allowlist_and_excludes_remote_metadata(tmp_path: Path) -> None:
    remote = tmp_path / "remote-parent"
    remote.mkdir()
    payloads = {"config.json": b"{}\n", "model.safetensors": b"weights"}
    files = []
    for name, payload in payloads.items():
        (remote / name).write_bytes(payload)
        files.append({"path": name, "bytes": len(payload), "sha256": _sha256(payload)})
    files.sort(key=lambda item: item["path"])
    tree = _sha256((json.dumps(files, sort_keys=True, separators=(",", ":")) + "\n").encode())
    (remote / "checkpoint-inventory.json").write_text("{}\n", encoding="utf-8")
    local = tmp_path / "local-parent"

    binding = copy_allowlisted_parent(
        remote,
        local,
        expected_files=files,
        expected_tree_sha256=tree,
        ignored_remote_metadata=("checkpoint-inventory.json", "variant-artifact-manifest.json"),
    )

    assert binding["tree_sha256"] == tree
    assert sorted(path.name for path in local.iterdir()) == ["config.json", "model.safetensors"]
    (remote / "unknown.bin").write_bytes(b"unexpected")
    with pytest.raises(V2TrainingJobError, match="unexpected"):
        copy_allowlisted_parent(
            remote,
            tmp_path / "second-local-parent",
            expected_files=files,
            expected_tree_sha256=tree,
            ignored_remote_metadata=("checkpoint-inventory.json",),
        )


def test_resume_is_limited_to_the_same_permanent_launch_owner(tmp_path: Path) -> None:
    output = tmp_path / "output"
    owner = "a" * 64
    packet_tree = "sha256:" + "b" * 64

    assert prepare_owned_output(output, owner_token=owner, packet_tree_sha256=packet_tree) is None
    checkpoint = output / "training" / "checkpoint-20"
    checkpoint.mkdir(parents=True)
    assert prepare_owned_output(output, owner_token=owner, packet_tree_sha256=packet_tree) == checkpoint
    with pytest.raises(V2TrainingJobError, match="owner"):
        prepare_owned_output(output, owner_token="c" * 64, packet_tree_sha256=packet_tree)

    argv = build_train_sft_argv(
        project_root=tmp_path / "source" / "ml" / "scriber_polishing",
        output_root=output,
        resume_checkpoint=checkpoint,
    )
    assert argv[-1] == "--save-final"
    assert argv[argv.index("--resume") + 1] == str(checkpoint)
    assert Path(argv[argv.index("--config") + 1]).parts[-2:] == ("job-input", "train-v2.yaml")
