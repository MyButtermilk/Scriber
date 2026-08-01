"""Fail-closed packaging and runtime contracts for the Scriber V2 HF job."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

V2_TRAIN_EXAMPLES = 20_480
V2_VALIDATION_EXAMPLES = 1_280
POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
PARENT_VARIANT_TREE_SHA256 = "sha256:5f6caf237fc3e1c3092363d6c17d405463f1cf99a84b0bbbec1720cf125fe3e2"
PARENT_CHECKPOINT_TREE_SHA256 = "sha256:4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
PARENT_REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt11/"
    "scriber-full-120k-v2/training/final-84458762af7b00ac"
)
V2_JOB_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
V2_JOB_FLAVOR = "l4x1"
V2_JOB_TIMEOUT_MINUTES = 240
V2_JOB_NAME = "scriber-v2-hardcase-replay-v1"
V2_PACKET_REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt12/packets/scriber-v2-hardcase-replay-v1"
)
V2_OUTPUT_MOUNT_URI = "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt12"
V2_OUTPUT_RELATIVE = "scriber-v2-hardcase-replay-v1"
V2_OUTPUT_ROOT = f"/outputs/{V2_OUTPUT_RELATIVE}"
V2_OUTPUT_REMOTE_URI = f"{V2_OUTPUT_MOUNT_URI}/{V2_OUTPUT_RELATIVE}"
V2_LAUNCH_CLAIM_PLACEHOLDER = "__SCRIBER_V2_LAUNCH_CLAIM_OWNER__"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V2_MIX_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_training_mix_manifest_schema.json"
V2_POLICY_PATH = PROJECT_ROOT / "configs" / "training-policy-gemma-lexical-v1.json"
V2_MIX_SEED = 270_023
V2_TRAIN_SOURCE_COUNTS = {"v1_replay": 10_240, "v2_hardcase": 10_240}
V2_VALIDATION_SOURCE_COUNTS = {"v1_replay": 640, "v2_hardcase": 640}
V1_REPLAY_MANIFEST_SHA256 = "sha256:bdbe13b3ed3c2258d293fa8f4d8f6761b2be91363712cdb80000ce38f020a802"
V1_REPLAY_TRAIN_SHA256 = "sha256:8a9087e4124013a075953eddeaa56bc6535d9ffcef1adf03e5adc750aeeb56c6"
V1_REPLAY_VALIDATION_SHA256 = "sha256:78240307231b0fd72eaffe8a61400f65ee46edb1ad13f6b209d4e89016a2913f"

PARENT_FILES: tuple[dict[str, object], ...] = (
    {
        "path": "added_tokens.json",
        "bytes": 35,
        "sha256": "sha256:50b2f405ba56a26d4913fd772089992252d7f942123cc0a034d96424221ba946",
    },
    {
        "path": "chat_template.jinja",
        "bytes": 1_532,
        "sha256": "sha256:7de1c58e208eda46e9c7f86397df37ec49883aeece39fb961e0a6b24088dd3c4",
    },
    {
        "path": "config.json",
        "bytes": 1_509,
        "sha256": "sha256:83c1e2b6544bcc6a03e1105c109bea141edbba8d60727e4b47bb96a72e5e8609",
    },
    {
        "path": "generation_config.json",
        "bytes": 168,
        "sha256": "sha256:e452df866db1e3b43e75bcc5af31539e404e67c5bbc4e02a9a9c22357f1a70df",
    },
    {
        "path": "model.safetensors",
        "bytes": 536_223_056,
        "sha256": "sha256:06af1566e1e63139b3b1a98caee2f737c8d0d0ac32e6f58b5b68fee80284b9ad",
    },
    {
        "path": "reload-verification.json",
        "bytes": 406,
        "sha256": "sha256:50ef95fed9ccae953a97e5f60858028ff7300003eb9039fc27ef009a87514a47",
    },
    {"path": "scriber-protection-policy.json", "bytes": 4_361, "sha256": POLICY_ARTIFACT_SHA256},
    {
        "path": "special_tokens_map.json",
        "bytes": 662,
        "sha256": "sha256:2f7b0adf4fb469770bb1490e3e35df87b1dc578246c5e7e6fc76ecf33213a397",
    },
    {
        "path": "tokenizer.json",
        "bytes": 33_384_568,
        "sha256": "sha256:4667f2089529e8e7657cfb6d1c19910ae71ff5f28aa7ab2ff2763330affad795",
    },
    {
        "path": "tokenizer.model",
        "bytes": 4_689_074,
        "sha256": "sha256:1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c",
    },
    {
        "path": "tokenizer_config.json",
        "bytes": 1_155_404,
        "sha256": "sha256:c54055555aa322627af18187adffe5f6b9298fecb8b4a5837a5e1de1895dd693",
    },
    {
        "path": "training_args.bin",
        "bytes": 6_033,
        "sha256": "sha256:108082da2045ca3782fa9c0050c1a79f311700dcb7a86d079f5ef37d72aa5594",
    },
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVIEW_ROLES = {"adversarial": 2, "fact": 1, "style": 1}


class V2TrainingJobError(RuntimeError):
    """A V2 job input or immutable binding could not be proven."""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise V2TrainingJobError(f"{label} must be a regular file")
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2TrainingJobError(f"{label} changed while it was read")
    return payload


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2TrainingJobError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise V2TrainingJobError(f"{label} must contain an object")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise V2TrainingJobError(f"{label} must be a prefixed SHA-256")
    return value


def _count_jsonl(
    payload: bytes,
    label: str,
    *,
    split: str,
    seen_ids: set[str],
    parent_splits: dict[str, str],
) -> int:
    count = 0
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            raise V2TrainingJobError(f"{label} contains a blank row")
        row = _json_object(line, f"{label} row {line_number}")
        example_id = row.get("example_id")
        if not isinstance(example_id, str) or not example_id or example_id in seen_ids:
            raise V2TrainingJobError(f"{label} contains a missing or duplicate example_id")
        seen_ids.add(example_id)
        for key in ("split", "source_split", "evaluation_split"):
            split = row.get(key)
            if isinstance(split, str) and split.lower() in {"test", "challenge"}:
                raise V2TrainingJobError(f"{label} contains a test/challenge artifact")
        if row.get("is_test") is True or row.get("is_challenge") is True:
            raise V2TrainingJobError(f"{label} contains a test/challenge artifact")
        parent_id = row.get("parent_canonical_document_id")
        if isinstance(parent_id, str) and parent_id:
            previous_split = parent_splits.setdefault(parent_id, split)
            if previous_split != split:
                raise V2TrainingJobError("training mix contains parent split leakage")
        count += 1
    return count


def verify_training_mix(dataset_root: str | Path) -> dict[str, object]:
    """Bind the reviewed 20,480/1,280 mix and its four critic approvals."""

    candidate = Path(dataset_root).expanduser()
    if candidate.is_symlink():
        raise V2TrainingJobError("training mix root must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise V2TrainingJobError("training mix root must be a directory")
    manifest_payload = _regular_bytes(root / "manifest.json", "training mix manifest")
    manifest = _json_object(manifest_payload, "training mix manifest")
    if manifest_payload != _canonical_bytes(manifest):
        raise V2TrainingJobError("training mix manifest is not canonical JSON")
    schema_payload = _regular_bytes(V2_MIX_SCHEMA_PATH, "training mix manifest schema")
    schema = _json_object(schema_payload, "training mix manifest schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise V2TrainingJobError("training mix manifest schema is invalid") from error
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(manifest),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if schema_errors:
        raise V2TrainingJobError(f"training mix manifest failed schema validation: {schema_errors[0].message}")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "scriber_v2_training_mix"
        or manifest.get("training_ready") is not True
        or manifest.get("seed") != V2_MIX_SEED
        or manifest.get("selection_algorithm") != "v1_split_sha256_bottom_k_then_source_sha256_interleave_v1"
        or manifest.get("manifest_schema_sha256") != _sha256(schema_payload)
    ):
        raise V2TrainingJobError("training mix is not training-ready")
    if manifest.get("split_files", {"train": "train.jsonl", "validation": "validation.jsonl"}) != {
        "train": "train.jsonl",
        "validation": "validation.jsonl",
    }:
        raise V2TrainingJobError("training mix split filenames differ")
    expected_counts = {"train": V2_TRAIN_EXAMPLES, "validation": V2_VALIDATION_EXAMPLES}
    if manifest.get("split_counts") != expected_counts:
        raise V2TrainingJobError("training mix split counts differ")
    if manifest.get("source_counts") != {
        "train": V2_TRAIN_SOURCE_COUNTS,
        "validation": V2_VALIDATION_SOURCE_COUNTS,
    }:
        raise V2TrainingJobError("training mix source counts differ from the reviewed 50/50 mix")
    if manifest.get("quality_gates") != {
        "allowed_splits_only": True,
        "duplicate_example_id_count": 0,
        "duplicate_pair_count": 0,
        "normalized_nfc": True,
        "parent_split_leakage_count": 0,
        "schema_validated_pair_count": V2_TRAIN_EXAMPLES + V2_VALIDATION_EXAMPLES,
        "source_provenance_binding": True,
    }:
        raise V2TrainingJobError("training mix quality gates differ")
    if manifest.get("provenance_policy") != {
        "allowed_source_splits": ["train", "validation"],
        "test_or_challenge_opened": False,
        "row_provenance_preserved": True,
        "v1_rows_modified": False,
        "v2_rows_modified": False,
        "v2_critic_evidence_required": True,
    }:
        raise V2TrainingJobError("training mix provenance policy differs")
    safety = {
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "contains_test_or_challenge_split": False,
        "direct_test_or_challenge_reuse": False,
    }
    if any(manifest.get(key) is not expected for key, expected in safety.items()):
        raise V2TrainingJobError("training mix contains or permits a test/challenge artifact")
    sources = manifest.get("sources")
    hardcase = sources.get("v2_hardcase") if isinstance(sources, Mapping) else None
    replay = sources.get("v1_replay") if isinstance(sources, Mapping) else None
    if not isinstance(hardcase, Mapping):
        raise V2TrainingJobError("training mix has no reviewed hard-case critic evidence")
    if not isinstance(replay, Mapping):
        raise V2TrainingJobError("training mix has no frozen V1 replay binding")
    if (
        replay.get("kind") != "scriber_v1_frozen_replay"
        or replay.get("manifest_file") != "dataset_manifest.json"
        or replay.get("manifest_sha256") != V1_REPLAY_MANIFEST_SHA256
        or replay.get("source_split_files") != {"train": "splits/train.jsonl", "validation": "splits/validation.jsonl"}
        or replay.get("source_split_sha256")
        != {"train": V1_REPLAY_TRAIN_SHA256, "validation": V1_REPLAY_VALIDATION_SHA256}
        or replay.get("source_split_counts") != {"train": 120_000, "validation": 10_010}
        or replay.get("selected_counts") != {"train": 10_240, "validation": 640}
        or replay.get("parent_split_group") != "parent_canonical_document_id"
        or replay.get("sealed_splits") != ["test", "challenge"]
    ):
        raise V2TrainingJobError("training mix frozen V1 replay provenance differs")
    if (
        hardcase.get("kind") != "scriber_v2_reviewed_hardcase_dataset"
        or hardcase.get("training_ready") is not True
        or hardcase.get("manifest_file") != "manifest.json"
        or hardcase.get("source_split_files") != {"train": "train.jsonl", "validation": "validation.jsonl"}
        or hardcase.get("source_split_counts") != {"train": 10_240, "validation": 640}
        or hardcase.get("selected_counts") != {"train": 10_240, "validation": 640}
    ):
        raise V2TrainingJobError("training mix reviewed V2 provenance differs")
    critic_hashes = (
        "manifest_sha256",
        "artifact_tree_sha256",
        "review_candidate_sha256",
        "review_contract_sha256",
        "review_package_sha256",
        "review_evidence_sha256",
        "review_evidence_tree_sha256",
    )
    for field in critic_hashes:
        _hash(hardcase.get(field), f"hard-case critic {field}")
    for source_name, source in (("V1 replay", replay), ("V2 hard-case", hardcase)):
        for field in ("train", "validation"):
            hashes = source.get("selected_example_ids_sha256")
            if not isinstance(hashes, Mapping):
                raise V2TrainingJobError(f"{source_name} selected example bindings are missing")
            _hash(hashes.get(field), f"{source_name} selected {field} examples")
    reviewer_ids = hardcase.get("reviewer_ids")
    roles = hardcase.get("role_counts")
    if (
        not isinstance(reviewer_ids, list)
        or len(reviewer_ids) != 4
        or len(set(reviewer_ids)) != 4
        or any(not isinstance(item, str) or not item for item in reviewer_ids)
        or not isinstance(roles, Mapping)
        or dict(roles) != _REVIEW_ROLES
    ):
        raise V2TrainingJobError("training mix critic evidence is incomplete")
    split_hashes = manifest.get("split_sha256")
    if not isinstance(split_hashes, Mapping):
        raise V2TrainingJobError("training mix split hashes are missing")
    result_files: dict[str, dict[str, object]] = {}
    seen_ids: set[str] = set()
    parent_splits: dict[str, str] = {}
    for split, expected_count in expected_counts.items():
        payload = _regular_bytes(root / f"{split}.jsonl", f"training mix {split}")
        if _hash(split_hashes.get(split), f"training mix {split} hash") != _sha256(payload):
            raise V2TrainingJobError(f"training mix {split} SHA-256 differs")
        if (
            _count_jsonl(
                payload,
                f"training mix {split}",
                split=split,
                seen_ids=seen_ids,
                parent_splits=parent_splits,
            )
            != expected_count
        ):
            raise V2TrainingJobError(f"training mix {split} row count differs")
        result_files[split] = {
            "path": root / f"{split}.jsonl",
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    manifest_identity = dict(manifest)
    declared_tree = manifest_identity.pop("artifact_tree_sha256", None)
    identity_payload = json.dumps(
        manifest_identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if declared_tree != _sha256(identity_payload):
        raise V2TrainingJobError("training mix artifact tree SHA-256 differs")
    return {
        "root": root,
        "manifest": manifest,
        "manifest_path": root / "manifest.json",
        "manifest_sha256": _sha256(manifest_payload),
        "split_counts": expected_counts,
        "files": result_files,
        "critic_evidence": {
            "reviewer_ids": list(reviewer_ids),
            "role_counts": dict(sorted(dict(roles).items())),
            **{field: hardcase[field] for field in critic_hashes},
        },
    }


def _run_git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        shell=False,
    )
    if completed.returncode != 0 or completed.stderr:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise V2TrainingJobError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _git_identity(repository_root: str | Path) -> tuple[Path, str, str, bytes, dict[str, dict[str, str]]]:
    candidate = Path(repository_root).expanduser()
    if candidate.is_symlink():
        raise V2TrainingJobError("source repository must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise V2TrainingJobError("source repository must be a directory")
    discovered = Path(_run_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if discovered != root:
        raise V2TrainingJobError("source repository must be the Git root")
    head = _run_git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _run_git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None or re.fullmatch(r"[0-9a-f]{40}", tree) is None:
        raise V2TrainingJobError("source Git identity is invalid")
    commit_payload = _run_git(root, "cat-file", "commit", head)
    commit_object = b"commit " + str(len(commit_payload)).encode("ascii") + b"\0" + commit_payload
    if hashlib.sha1(commit_object, usedforsecurity=False).hexdigest() != head:
        raise V2TrainingJobError("source Git commit bytes do not bind HEAD")
    entries: dict[str, dict[str, str]] = {}
    raw_tree = _run_git(root, "ls-tree", "-r", "-z", "--full-tree", head)
    for raw in raw_tree.split(b"\0"):
        if not raw:
            continue
        identity, separator, path_bytes = raw.partition(b"\t")
        fields = identity.decode("ascii").split(" ")
        try:
            path = path_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise V2TrainingJobError("source Git path is not UTF-8") from error
        if (
            not separator
            or len(fields) != 3
            or fields[1] != "blob"
            or fields[0] not in {"100644", "100755"}
            or re.fullmatch(r"[0-9a-f]{40}", fields[2]) is None
            or not _safe_relative(path)
            or path in entries
        ):
            raise V2TrainingJobError("source Git tree contains an unsupported entry")
        entries[path] = {"git_mode": fields[0], "git_blob_sha1": fields[2]}
    required = {
        "ml/scriber_polishing/scripts/train_sft.py",
        "ml/scriber_polishing/scripts/run_hf_v2_training_job.py",
        "ml/scriber_polishing/src/scriber_polishing/hf_v2_training_job.py",
        "ml/scriber_polishing/configs/train_v2_hardcase_mixed.yaml",
        "ml/scriber_polishing/configs/training-policy-gemma-lexical-v1.json",
        "ml/scriber_polishing/contracts/v2_training_mix_manifest_schema.json",
        "ml/scriber_polishing/pyproject.toml",
    }
    if not required <= set(entries):
        raise V2TrainingJobError("source Git HEAD lacks the V2 runner or training entrypoint")
    return root, head, tree, commit_payload, entries


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and "\0" not in value
        and path.as_posix() == value
    )


def _materialize_git_head(source_root: Path, destination: Path) -> tuple[str, str, list[dict[str, object]]]:
    root, head, tree, commit_payload, git_entries = _git_identity(source_root)
    destination.mkdir(parents=True)
    records: list[dict[str, object]] = []
    names = sorted(git_entries)
    queries = b"".join((git_entries[name]["git_blob_sha1"] + "\n").encode("ascii") for name in names)
    completed = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input=queries,
        check=False,
        capture_output=True,
        shell=False,
    )
    if completed.returncode != 0 or completed.stderr:
        raise V2TrainingJobError("source Git blobs could not be read exactly")
    stream = io.BytesIO(completed.stdout)
    for name in names:
        binding = git_entries[name]
        header = stream.readline().decode("ascii", errors="strict").rstrip("\n")
        fields = header.split(" ")
        if len(fields) != 3 or fields[:2] != [binding["git_blob_sha1"], "blob"] or not fields[2].isdigit():
            raise V2TrainingJobError("source Git batch response is invalid")
        payload = stream.read(int(fields[2]))
        if len(payload) != int(fields[2]) or stream.read(1) != b"\n":
            raise V2TrainingJobError("source Git batch response is truncated")
        blob = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
        blob_sha1 = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
        if blob_sha1 != binding["git_blob_sha1"]:
            raise V2TrainingJobError(f"source Git blob differs from HEAD: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        records.append(
            {
                "path": f"source/{name}",
                "bytes": len(payload),
                "sha256": _sha256(payload),
                **binding,
            }
        )
    if stream.read(1):
        raise V2TrainingJobError("source Git batch response has trailing bytes")
    commit_path = destination.parent / "git" / "head.commit"
    commit_path.parent.mkdir()
    commit_path.write_bytes(commit_payload)
    records.append(
        {
            "path": "git/head.commit",
            "bytes": len(commit_payload),
            "sha256": _sha256(commit_payload),
        }
    )
    records.sort(key=lambda item: str(item["path"]))
    return head, tree, records


def _training_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_kind": "final",
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "method": "full_sft",
        "seed": 270_023,
        "epochs": 1,
        "train_examples": V2_TRAIN_EXAMPLES,
        "validation_examples": V2_VALIDATION_EXAMPLES,
        "max_sequence_length": 1_024,
        "micro_batch_size": 4,
        "gradient_accumulation_steps": 8,
        "learning_rate": 5e-6,
        "optimizer": "adamw_torch_fused",
        "eval_steps": 100,
        "save_steps": 200,
        "save_total_limit": 3,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "max_grad_norm": 1.0,
        "bf16": True,
        "gradient_checkpointing": True,
        "assistant_output_loss_only": True,
        "early_stopping": False,
        "protection_policy_path": "training-policy.json",
        "protection_policy_sha256": POLICY_ARTIFACT_SHA256.removeprefix("sha256:"),
        "parent_checkpoint_path": "parent-v1-checkpoint",
        "parent_checkpoint_tree_sha256": PARENT_CHECKPOINT_TREE_SHA256.removeprefix("sha256:"),
    }


def _load_exact_training_config(payload: bytes) -> dict[str, object]:
    try:
        value = yaml.safe_load(payload)
    except yaml.YAMLError as error:
        raise V2TrainingJobError("V2 mixed training config is invalid YAML") from error
    if not isinstance(value, dict) or value != _training_config():
        raise V2TrainingJobError("V2 mixed training config differs from the reviewed contract")
    return value


def v2_runner_payload() -> bytes:
    script = """#!/usr/bin/env bash
set -euo pipefail
unset PYTHONPATH PYTHONHOME
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export PYTHONNOUSERSITE=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends git
  rm -rf /var/lib/apt/lists/*
fi
python -m pip install --break-system-packages --no-cache-dir \\
  accelerate==1.14.0 jsonschema==4.26.0 numpy==2.5.1 pyyaml==6.0.3 \\
  safetensors==0.8.0 sentencepiece==0.2.2 tensorboard==2.21.0 \\
  transformers==4.57.6
exec python -I /input/packet/source/ml/scriber_polishing/scripts/run_hf_v2_training_job.py "$@"
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def v2_bootstrap_payload() -> str:
    return (
        "import hashlib,json,os,pathlib,sys;"
        "r=pathlib.Path('/input/packet');"
        "e=sys.argv[1:6];"
        "m=(r/'package-manifest.json').read_bytes();"
        "c=(r/'launch-contract.json').read_bytes();"
        "i=(r/'tree-inventory.json').read_bytes();"
        "h=lambda b:'sha256:'+hashlib.sha256(b).hexdigest();"
        "(h(m),h(c),h(i))==tuple(e[1:4]) or sys.exit('packet metadata hash mismatch');"
        "v=json.loads(i);f=v['files'];"
        "all((lambda p,x:p.is_file() and not p.is_symlink() and p.stat().st_size==x['bytes'] and h(p.read_bytes())==x['sha256'])(r/x['path'],x) for x in f) or sys.exit('packet file mismatch');"
        "h((json.dumps(f,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False)+'\\n').encode())==e[0] or sys.exit('packet tree mismatch');"
        "os.execv('/bin/bash',['bash',str(r/'run-v2-training.sh'),'--packet-root',str(r),'--parent-root','/input/parent','--output-root','"
        + V2_OUTPUT_ROOT
        + "','--expected-packet-tree-sha256',e[0],'--expected-manifest-sha256',e[1],'--expected-launch-contract-sha256',e[2],'--claim-owner',e[4]])"
    )


def _launch_contract(
    *,
    source_git_head: str,
    packet_tree_sha256: str,
    manifest_sha256: str,
    inventory_sha256: str,
) -> dict[str, object]:
    packet_volume = f"{V2_PACKET_REMOTE_URI}:/input/packet:ro"
    parent_volume = f"{PARENT_REMOTE_URI}:/input/parent:ro"
    output_volume = f"{V2_OUTPUT_MOUNT_URI}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--flavor",
        V2_JOB_FLAVOR,
        "--timeout",
        f"{V2_JOB_TIMEOUT_MINUTES}m",
        "--label",
        f"name={V2_JOB_NAME}",
        "--label",
        "project=scriber-polishing",
        "--label",
        "campaign=v2-hardcase-replay",
        "--label",
        "attempt=attempt12",
        "--label",
        "role=v2-training",
        "--label",
        f"output-prefix=attempt12/{V2_OUTPUT_RELATIVE}",
        "--label",
        f"launch-claim={V2_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        parent_volume,
        "-v",
        output_volume,
        V2_JOB_IMAGE,
        "python",
        "-I",
        "-c",
        v2_bootstrap_payload(),
        packet_tree_sha256,
        manifest_sha256,
        "__CONTRACT_SHA256_BOUND_BY_LAUNCHER__",
        inventory_sha256,
        V2_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_launch_contract",
        "attempt": "attempt12",
        "source_git_head": source_git_head,
        "image": V2_JOB_IMAGE,
        "flavor": V2_JOB_FLAVOR,
        "timeout": f"{V2_JOB_TIMEOUT_MINUTES}m",
        "job_name": V2_JOB_NAME,
        "packet_remote_uri": V2_PACKET_REMOTE_URI,
        "parent_remote_uri": PARENT_REMOTE_URI,
        "output_mount_uri": V2_OUTPUT_MOUNT_URI,
        "remote_result_uri": V2_OUTPUT_REMOTE_URI,
        "packet_volume": packet_volume,
        "parent_volume": parent_volume,
        "output_volume": output_volume,
        "packet_tree_sha256": packet_tree_sha256,
        "manifest_sha256": manifest_sha256,
        "inventory_sha256": inventory_sha256,
        "launch_claim_placeholder": V2_LAUNCH_CLAIM_PLACEHOLDER,
        "remote_packet_exact_inventory_required": True,
        "remote_output_absence_required": True,
        "active_job_snapshots_required": 3,
        "external_job_started": False,
        "auto_retry_allowed": False,
        "secret_names": [],
        "argv": argv,
    }


def package_v2_training_job(
    *,
    repository_root: str | Path,
    dataset_root: str | Path,
    policy_path: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    """Create one immutable packet from Git HEAD and reviewed training inputs."""

    dataset = verify_training_mix(dataset_root)
    policy = _regular_bytes(Path(policy_path).expanduser().resolve(), "V2 training policy")
    if _sha256(policy) != POLICY_ARTIFACT_SHA256:
        raise V2TrainingJobError("V2 training policy SHA-256 differs")
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise V2TrainingJobError("refusing to overwrite a V2 training packet")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        source_head, source_tree, records = _materialize_git_head(
            Path(repository_root).expanduser().resolve(), staging / "source"
        )
        source_mix_schema = _regular_bytes(
            staging / "source" / "ml" / "scriber_polishing" / "contracts" / "v2_training_mix_manifest_schema.json",
            "Git HEAD training mix schema",
        )
        if dataset["manifest"].get("manifest_schema_sha256") != _sha256(source_mix_schema):  # type: ignore[union-attr]
            raise V2TrainingJobError("Git HEAD training mix schema differs from the reviewed dataset")
        source_policy = _regular_bytes(
            staging / "source" / "ml" / "scriber_polishing" / "configs" / "training-policy-gemma-lexical-v1.json",
            "Git HEAD V2 training policy",
        )
        if source_policy != policy:
            raise V2TrainingJobError("Git HEAD V2 training policy differs from the packet policy")
        inputs = staging / "inputs"
        inputs.mkdir()
        copied = {
            "manifest.json": Path(dataset["manifest_path"]),
            "train.jsonl": Path(dataset["files"]["train"]["path"]),  # type: ignore[index]
            "validation.jsonl": Path(dataset["files"]["validation"]["path"]),  # type: ignore[index]
        }
        for name, source in copied.items():
            payload = _regular_bytes(source, f"V2 packet {name}")
            (inputs / name).write_bytes(payload)
            records.append({"path": f"inputs/{name}", "bytes": len(payload), "sha256": _sha256(payload)})
        (inputs / "training-policy.json").write_bytes(policy)
        records.append(
            {
                "path": "inputs/training-policy.json",
                "bytes": len(policy),
                "sha256": POLICY_ARTIFACT_SHA256,
            }
        )
        config_payload = _regular_bytes(
            staging / "source" / "ml" / "scriber_polishing" / "configs" / "train_v2_hardcase_mixed.yaml",
            "reviewed V2 mixed training config",
        )
        _load_exact_training_config(config_payload)
        (inputs / "train-v2.yaml").write_bytes(config_payload)
        records.append(
            {"path": "inputs/train-v2.yaml", "bytes": len(config_payload), "sha256": _sha256(config_payload)}
        )
        runner = v2_runner_payload()
        (staging / "run-v2-training.sh").write_bytes(runner)
        records.append({"path": "run-v2-training.sh", "bytes": len(runner), "sha256": _sha256(runner)})
        records.sort(key=lambda item: str(item["path"]))
        packet_tree = _sha256(_canonical_bytes(records))
        inventory = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_training_tree_inventory",
            "algorithm": "sha256-canonical-file-records-v1",
            "files": records,
            "file_count": len(records),
            "byte_count": sum(int(item["bytes"]) for item in records),
            "packet_tree_sha256": packet_tree,
        }
        inventory_payload = _canonical_bytes(inventory)
        inventory_sha = _sha256(inventory_payload)
        (staging / "tree-inventory.json").write_bytes(inventory_payload)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_training_job_packet",
            "attempt": "attempt12",
            "source": {
                "git_head": source_head,
                "git_tree": source_tree,
                "scope": "complete_tracked_HEAD_tree",
                "dirty_worktree_included": False,
            },
            "dataset": {
                "kind": "scriber_v2_training_mix",
                "training_ready": True,
                "manifest_sha256": dataset["manifest_sha256"],
                "split_counts": dataset["split_counts"],
                "critic_evidence": dataset["critic_evidence"],
                "test_or_challenge_artifacts": False,
            },
            "policy": {"path": "inputs/training-policy.json", "sha256": POLICY_ARTIFACT_SHA256},
            "parent": {
                "remote_uri": PARENT_REMOTE_URI,
                "variant_tree_sha256": PARENT_VARIANT_TREE_SHA256,
                "checkpoint_tree_sha256": PARENT_CHECKPOINT_TREE_SHA256,
                "copy_mode": "exact_12_file_allowlist",
                "files": list(PARENT_FILES),
                "ignored_remote_metadata": ["checkpoint-inventory.json", "variant-artifact-manifest.json"],
            },
            "training": {"epochs": 1, "learning_rate": 5e-6, "bf16": True, "save_final": True},
            "runtime": {"image": V2_JOB_IMAGE, "flavor": V2_JOB_FLAVOR, "timeout_minutes": V2_JOB_TIMEOUT_MINUTES},
            "output": {
                "remote_uri": V2_OUTPUT_REMOTE_URI,
                "container_path": V2_OUTPUT_ROOT,
                "unique": True,
                "overwrite_allowed": False,
                "resume_policy": "same_launch_owner_output_only",
            },
            "inventory": {
                "path": "tree-inventory.json",
                "sha256": inventory_sha,
                "packet_tree_sha256": packet_tree,
                "file_count": len(records),
                "byte_count": inventory["byte_count"],
            },
            "side_effects": {"publication_allowed": False, "auto_retry_allowed": False, "secrets_required": []},
        }
        manifest_payload = _canonical_bytes(manifest)
        manifest_sha = _sha256(manifest_payload)
        (staging / "package-manifest.json").write_bytes(manifest_payload)
        contract = _launch_contract(
            source_git_head=source_head,
            packet_tree_sha256=packet_tree,
            manifest_sha256=manifest_sha,
            inventory_sha256=inventory_sha,
        )
        contract_payload = _canonical_bytes(contract)
        contract_sha = _sha256(contract_payload)
        (staging / "launch-contract.json").write_bytes(contract_payload)
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "packet_dir": target,
        "source_git_head": source_head,
        "source_git_tree": source_tree,
        "packet_tree_sha256": packet_tree,
        "inventory_sha256": inventory_sha,
        "manifest_sha256": manifest_sha,
        "launch_contract_sha256": contract_sha,
    }


def verify_v2_training_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
) -> dict[str, object]:
    """Verify every packet byte against three independently supplied digests."""

    for value, label in (
        (expected_packet_tree_sha256, "expected packet tree"),
        (expected_manifest_sha256, "expected manifest"),
        (expected_launch_contract_sha256, "expected launch contract"),
    ):
        _hash(value, label)
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise V2TrainingJobError("V2 training packet must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise V2TrainingJobError("V2 training packet must be a directory")
    manifest_payload = _regular_bytes(root / "package-manifest.json", "V2 package manifest")
    inventory_payload = _regular_bytes(root / "tree-inventory.json", "V2 tree inventory")
    contract_payload = _regular_bytes(root / "launch-contract.json", "V2 launch contract")
    if _sha256(manifest_payload) != expected_manifest_sha256:
        raise V2TrainingJobError("V2 package manifest SHA-256 differs")
    if _sha256(contract_payload) != expected_launch_contract_sha256:
        raise V2TrainingJobError("V2 launch contract SHA-256 differs")
    manifest = _json_object(manifest_payload, "V2 package manifest")
    inventory = _json_object(inventory_payload, "V2 tree inventory")
    contract = _json_object(contract_payload, "V2 launch contract")
    if (
        manifest_payload != _canonical_bytes(manifest)
        or inventory_payload != _canonical_bytes(inventory)
        or contract_payload != _canonical_bytes(contract)
    ):
        raise V2TrainingJobError("V2 packet metadata is not canonical JSON")
    files = inventory.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(item, Mapping) for item in files):
        raise V2TrainingJobError("V2 packet tree inventory is invalid")
    paths: list[str] = []
    for item in files:
        path = str(item.get("path", ""))
        if not _safe_relative(path) or path in paths:
            raise V2TrainingJobError("V2 packet inventory path is unsafe or duplicated")
        payload = _regular_bytes(root / path, f"V2 packet file {path}")
        if item.get("bytes") != len(payload) or item.get("sha256") != _sha256(payload):
            raise V2TrainingJobError("V2 packet file SHA-256 differs from tree inventory")
        paths.append(path)
    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    }
    expected_files = {*paths, "package-manifest.json", "tree-inventory.json", "launch-contract.json"}
    if actual_files != expected_files or any(path.is_symlink() for path in root.rglob("*")):
        raise V2TrainingJobError("V2 packet contains an unbound file or symlink")
    packet_tree = _sha256(_canonical_bytes(files))
    if (
        packet_tree != expected_packet_tree_sha256
        or inventory.get("packet_tree_sha256") != packet_tree
        or inventory.get("file_count") != len(files)
        or inventory.get("byte_count") != sum(int(item["bytes"]) for item in files)
        or manifest.get("inventory", {}).get("sha256") != _sha256(inventory_payload)
    ):
        raise V2TrainingJobError("V2 packet tree binding differs")
    dataset = verify_training_mix(root / "inputs")
    source_mix_schema = _regular_bytes(
        root / "source" / "ml" / "scriber_polishing" / "contracts" / "v2_training_mix_manifest_schema.json",
        "packet Git HEAD training mix schema",
    )
    if dataset["manifest"].get("manifest_schema_sha256") != _sha256(source_mix_schema):  # type: ignore[union-attr]
        raise V2TrainingJobError("packet Git HEAD training mix schema differs from the reviewed dataset")
    policy = _regular_bytes(root / "inputs" / "training-policy.json", "V2 packet policy")
    source_policy = _regular_bytes(
        root / "source" / "ml" / "scriber_polishing" / "configs" / "training-policy-gemma-lexical-v1.json",
        "packet Git HEAD V2 training policy",
    )
    config = _load_exact_training_config(_regular_bytes(root / "inputs" / "train-v2.yaml", "V2 train config"))
    if policy != source_policy or _sha256(policy) != POLICY_ARTIFACT_SHA256 or config != _training_config():
        raise V2TrainingJobError("V2 policy or training config differs")
    source = manifest.get("source")
    parent = manifest.get("parent")
    training = manifest.get("training")
    if (
        not isinstance(source, Mapping)
        or not isinstance(parent, Mapping)
        or source.get("scope") != "complete_tracked_HEAD_tree"
        or parent.get("variant_tree_sha256") != PARENT_VARIANT_TREE_SHA256
        or parent.get("checkpoint_tree_sha256") != PARENT_CHECKPOINT_TREE_SHA256
        or parent.get("files") != list(PARENT_FILES)
        or training != {"epochs": 1, "learning_rate": 5e-6, "bf16": True, "save_final": True}
        or manifest.get("dataset", {}).get("manifest_sha256") != dataset["manifest_sha256"]
    ):
        raise V2TrainingJobError("V2 packet immutable training bindings differ")
    expected_contract = _launch_contract(
        source_git_head=str(source.get("git_head", "")),
        packet_tree_sha256=packet_tree,
        manifest_sha256=expected_manifest_sha256,
        inventory_sha256=_sha256(inventory_payload),
    )
    if contract != expected_contract:
        raise V2TrainingJobError("V2 launch contract differs from the exact job argv")
    return {
        "source_git_head": source["git_head"],
        "source_git_tree": source["git_tree"],
        "packet_tree_sha256": packet_tree,
        "manifest_sha256": expected_manifest_sha256,
        "launch_contract_sha256": expected_launch_contract_sha256,
        "training": {"epochs": 1, "learning_rate": 5e-6, "bf16": True},
        "parent_checkpoint_tree_sha256": PARENT_CHECKPOINT_TREE_SHA256,
        "remote_result_uri": V2_OUTPUT_REMOTE_URI,
        "file_count": len(files),
    }


def copy_allowlisted_parent(
    remote_parent: str | Path,
    local_parent: str | Path,
    *,
    expected_files: list[dict[str, object]] | tuple[dict[str, object], ...] = PARENT_FILES,
    expected_tree_sha256: str = PARENT_CHECKPOINT_TREE_SHA256,
    ignored_remote_metadata: tuple[str, ...] = ("checkpoint-inventory.json", "variant-artifact-manifest.json"),
) -> dict[str, object]:
    """Copy and hash only the allowlisted warm-start files from a read-only mount."""

    _hash(expected_tree_sha256, "expected parent checkpoint tree")
    source_candidate = Path(remote_parent).expanduser()
    if source_candidate.is_symlink():
        raise V2TrainingJobError("remote parent must not be a symlink")
    source = source_candidate.resolve()
    if not source.is_dir():
        raise V2TrainingJobError("remote parent mount is missing")
    destination = Path(local_parent).expanduser().resolve()
    if destination.exists():
        raise V2TrainingJobError("local parent destination already exists")
    normalized = [dict(item) for item in expected_files]
    normalized.sort(key=lambda item: str(item.get("path", "")))
    names: set[str] = set()
    for item in normalized:
        name = item.get("path")
        if (
            not isinstance(name, str)
            or not _safe_relative(name)
            or Path(name).name != name
            or name in names
            or isinstance(item.get("bytes"), bool)
            or not isinstance(item.get("bytes"), int)
            or int(item["bytes"]) < 0
        ):
            raise V2TrainingJobError("parent allowlist is invalid")
        _hash(item.get("sha256"), f"parent allowlist {name}")
        names.add(name)
    ignored = set(ignored_remote_metadata)
    if any(Path(name).name != name or not name for name in ignored) or names & ignored:
        raise V2TrainingJobError("parent metadata ignore list is invalid")
    observed_entries: set[str] = set()
    for path in source.iterdir():
        if path.is_symlink() or not path.is_file():
            raise V2TrainingJobError("remote parent contains an unexpected directory or symlink")
        observed_entries.add(path.name)
    unexpected = observed_entries - names - ignored
    missing = names - observed_entries
    if unexpected:
        raise V2TrainingJobError("remote parent contains an unexpected file: " + ", ".join(sorted(unexpected)))
    if missing:
        raise V2TrainingJobError("remote parent is missing an allowlisted file: " + ", ".join(sorted(missing)))
    destination.mkdir(parents=True, exist_ok=False)
    actual: list[dict[str, object]] = []
    try:
        for binding in normalized:
            name = str(binding["path"])
            source_path = source / name
            before = source_path.stat()
            digest = hashlib.sha256()
            byte_count = 0
            target = destination / name
            with source_path.open("rb") as input_stream, target.open("xb") as output_stream:
                while chunk := input_stream.read(4 * 1024 * 1024):
                    digest.update(chunk)
                    output_stream.write(chunk)
                    byte_count += len(chunk)
            after = source_path.stat()
            actual_binding = {
                "path": name,
                "bytes": byte_count,
                "sha256": "sha256:" + digest.hexdigest(),
            }
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or actual_binding != binding:
                raise V2TrainingJobError(f"parent allowlisted file changed or differs: {name}")
            actual.append(actual_binding)
        tree = _sha256(_canonical_bytes(actual))
        if tree != expected_tree_sha256:
            raise V2TrainingJobError("parent checkpoint tree SHA-256 differs")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "tree_sha256": tree,
        "files": actual,
        "file_count": len(actual),
        "byte_count": sum(int(item["bytes"]) for item in actual),
        "ignored_remote_metadata": sorted(observed_entries & ignored),
    }


def prepare_owned_output(
    output_root: str | Path,
    *,
    owner_token: str,
    packet_tree_sha256: str,
) -> Path | None:
    """Claim a unique output, or resume only a checkpoint owned by this launch."""

    if re.fullmatch(r"[0-9a-f]{64}", owner_token) is None:
        raise V2TrainingJobError("V2 output owner token is invalid")
    _hash(packet_tree_sha256, "V2 output packet tree")
    output = Path(output_root).expanduser().resolve()
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_output_owner",
        "owner_token": owner_token,
        "packet_tree_sha256": packet_tree_sha256,
        "remote_result_uri": V2_OUTPUT_REMOTE_URI,
        "resume_policy": "same_launch_owner_output_only",
        "auto_retry_allowed": False,
    }
    payload = _canonical_bytes(claim)
    owner_path = output / "launch-owner.json"
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        if output.is_symlink() or not output.is_dir():
            raise V2TrainingJobError("V2 output root is not a regular directory") from None
        if _regular_bytes(owner_path, "V2 output owner") != payload:
            raise V2TrainingJobError("V2 output belongs to a different launch owner") from None
    else:
        with owner_path.open("xb") as stream:
            stream.write(payload)
    if any(path.is_symlink() for path in output.rglob("*")):
        raise V2TrainingJobError("V2 owned output contains a symlink")
    if (output / "training-report.json").exists():
        raise V2TrainingJobError("V2 owned output already contains a terminal training report")
    training = output / "training"
    if not training.exists():
        return None
    if training.is_symlink() or not training.is_dir():
        raise V2TrainingJobError("V2 training output is not a regular directory")
    checkpoints: list[tuple[int, Path]] = []
    for path in training.iterdir():
        if path.name.startswith("checkpoint-"):
            match = re.fullmatch(r"checkpoint-([1-9][0-9]*)", path.name)
            if match is None or path.is_symlink() or not path.is_dir():
                raise V2TrainingJobError("V2 owned output contains an invalid checkpoint")
            checkpoints.append((int(match.group(1)), path.resolve()))
    return max(checkpoints, default=(0, None), key=lambda item: item[0])[1]


def build_train_sft_argv(
    *,
    project_root: str | Path,
    output_root: str | Path,
    resume_checkpoint: str | Path | None,
) -> list[str]:
    """Return the single reviewed train_sft invocation with mandatory final save."""

    project = Path(project_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    job_input = project / "job-input"
    resume = "none" if resume_checkpoint is None else str(Path(resume_checkpoint).expanduser().resolve())
    return [
        sys.executable,
        str(project / "scripts" / "train_sft.py"),
        "--config",
        str(job_input / "train-v2.yaml"),
        "--dataset-manifest",
        str(job_input / "manifest.json"),
        "--train",
        str(job_input / "train.jsonl"),
        "--validation",
        str(job_input / "validation.jsonl"),
        "--output-dir",
        str(output / "training"),
        "--report",
        str(output / "training-report.json"),
        "--resume",
        resume,
        "--save-final",
    ]


def verify_cuda_bf16(torch_module: object | None = None) -> dict[str, object]:
    """Require the single CUDA/BF16 device promised by the l4x1 contract."""

    if torch_module is None:
        try:
            import torch as torch_module  # type: ignore[no-redef]
        except ImportError as error:
            raise V2TrainingJobError("PyTorch is unavailable in the pinned job image") from error
    cuda = getattr(torch_module, "cuda", None)
    if (
        cuda is None
        or not callable(getattr(cuda, "is_available", None))
        or not cuda.is_available()
        or not callable(getattr(cuda, "is_bf16_supported", None))
        or not cuda.is_bf16_supported()
        or not callable(getattr(cuda, "device_count", None))
        or cuda.device_count() != 1
    ):
        raise V2TrainingJobError("the pinned l4x1 runtime lacks exactly one CUDA BF16 device")
    return {
        "cuda_available": True,
        "bf16_supported": True,
        "device_count": 1,
        "gpu": str(cuda.get_device_name(0)),
        "torch_version": str(getattr(torch_module, "__version__", "")),
        "cuda_runtime": str(getattr(getattr(torch_module, "version", None), "cuda", "")),
    }


def _write_immutable_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(_canonical_bytes(value))


def _write_or_verify_json(path: Path, value: object) -> None:
    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if _regular_bytes(path, path.name) != payload:
            raise V2TrainingJobError(f"refusing to replace different immutable evidence: {path.name}") from None


def _reconstruct_git_head(
    packet: Path, workspace_source: Path, manifest: Mapping[str, object], files: list[object]
) -> None:
    if workspace_source.exists():
        raise V2TrainingJobError("V2 workspace source already exists")
    shutil.copytree(packet / "source", workspace_source, copy_function=shutil.copy2)
    source_records = [
        item for item in files if isinstance(item, Mapping) and str(item.get("path", "")).startswith("source/")
    ]
    for binding in source_records:
        relative = str(binding["path"])[len("source/") :]
        path = workspace_source / relative
        mode = binding.get("git_mode")
        blob_sha1 = binding.get("git_blob_sha1")
        if mode not in {"100644", "100755"} or not isinstance(blob_sha1, str):
            raise V2TrainingJobError("V2 source inventory lacks exact Git mode/blob identity")
        path.chmod(0o755 if mode == "100755" else 0o644)
    _run_git(workspace_source, "init", "--initial-branch=main")
    _run_git(workspace_source, "config", "core.autocrlf", "false")
    _run_git(workspace_source, "config", "core.filemode", "true")
    for binding in source_records:
        relative = str(binding["path"])[len("source/") :]
        actual = _run_git(workspace_source, "hash-object", "-w", "--no-filters", relative).decode("ascii").strip()
        if actual != binding["git_blob_sha1"]:
            raise V2TrainingJobError(f"workspace source blob differs from Git HEAD: {relative}")
    _run_git(workspace_source, "add", "-f", "--all")
    actual_tree = _run_git(workspace_source, "write-tree").decode("ascii").strip()
    source_binding = manifest.get("source")
    if not isinstance(source_binding, Mapping) or actual_tree != source_binding.get("git_tree"):
        raise V2TrainingJobError("workspace source tree differs from Git HEAD")
    commit_path = packet / "git" / "head.commit"
    actual_head = (
        _run_git(workspace_source, "hash-object", "-t", "commit", "-w", str(commit_path)).decode("ascii").strip()
    )
    if actual_head != source_binding.get("git_head"):
        raise V2TrainingJobError("workspace source commit differs from Git HEAD")
    _run_git(workspace_source, "update-ref", "refs/heads/main", actual_head)
    status = _run_git(workspace_source, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise V2TrainingJobError("reconstructed V2 Git HEAD is dirty")


def run_v2_training_job(
    *,
    packet_root: str | Path,
    remote_parent_root: str | Path,
    output_root: str | Path,
    workspace_root: str | Path,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
    owner_token: str,
    torch_module: object | None = None,
) -> dict[str, object]:
    """Verify, materialize and execute one V2 SFT run without launching/retrying."""

    packet = Path(packet_root).expanduser().resolve()
    verification = verify_v2_training_packet(
        packet,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_launch_contract_sha256=expected_launch_contract_sha256,
    )
    manifest = _json_object(
        _regular_bytes(packet / "package-manifest.json", "V2 package manifest"), "V2 package manifest"
    )
    inventory = _json_object(_regular_bytes(packet / "tree-inventory.json", "V2 tree inventory"), "V2 tree inventory")
    workspace = Path(workspace_root).expanduser().resolve()
    if workspace.exists():
        raise V2TrainingJobError("V2 training workspace must be absent")
    workspace.mkdir(parents=True, exist_ok=False)
    source = workspace / "source"
    _reconstruct_git_head(packet, source, manifest, list(inventory["files"]))
    project = source / "ml" / "scriber_polishing"
    job_input = project / "job-input"
    if job_input.exists():
        raise V2TrainingJobError("Git HEAD unexpectedly contains V2 job-input")
    job_input.mkdir()
    for name in ("manifest.json", "train.jsonl", "validation.jsonl", "training-policy.json", "train-v2.yaml"):
        shutil.copy2(packet / "inputs" / name, job_input / name)
    local_parent = job_input / "parent-v1-checkpoint"
    parent_binding = copy_allowlisted_parent(remote_parent_root, local_parent)
    verify_training_mix(job_input)
    if _sha256(_regular_bytes(job_input / "training-policy.json", "runtime V2 policy")) != POLICY_ARTIFACT_SHA256:
        raise V2TrainingJobError("runtime V2 policy differs")
    _load_exact_training_config(_regular_bytes(job_input / "train-v2.yaml", "runtime V2 config"))
    runtime = verify_cuda_bf16(torch_module)
    output = Path(output_root).expanduser().resolve()
    resume = prepare_owned_output(output, owner_token=owner_token, packet_tree_sha256=expected_packet_tree_sha256)
    _write_or_verify_json(output / "packet-verification.json", verification)
    _write_or_verify_json(output / "parent-binding.json", parent_binding)
    _write_or_verify_json(output / "runtime-verification.json", runtime)
    argv = build_train_sft_argv(project_root=project, output_root=output, resume_checkpoint=resume)
    completed = subprocess.run(argv, cwd=project, check=False, shell=False)
    if completed.returncode != 0:
        raise V2TrainingJobError("train_sft.py failed; this runner never retries automatically")
    report_payload = _regular_bytes(output / "training-report.json", "V2 training report")
    report = _json_object(report_payload, "V2 training report")
    final_model = report.get("final_model")
    warm_start = report.get("warm_start_parent_checkpoint")
    if (
        report.get("training_completed") is not True
        or report.get("bf16") is not True
        or report.get("learning_rate") != 5e-6
        or not isinstance(final_model, Mapping)
        or not isinstance(warm_start, Mapping)
        or warm_start.get("tree_sha256") != PARENT_CHECKPOINT_TREE_SHA256
    ):
        raise V2TrainingJobError("V2 training report does not prove the reviewed BF16 warm-start run and final save")
    result = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_job_result",
        "packet_tree_sha256": expected_packet_tree_sha256,
        "source_git_head": verification["source_git_head"],
        "parent_checkpoint_tree_sha256": PARENT_CHECKPOINT_TREE_SHA256,
        "training_report_sha256": _sha256(report_payload),
        "run_fingerprint": report.get("run_fingerprint"),
        "training_completed": True,
        "save_final": True,
        "resume_checkpoint": None if resume is None else resume.name,
        "auto_retry_allowed": False,
    }
    _write_immutable_json(output / "v2-training-result.json", result)
    return result
