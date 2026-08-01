"""Build the reviewed V2 hard-case plus frozen V1 replay training mix.

Only the frozen V1 train/validation splits and the critic-approved V2
train/validation splits are admissible inputs.  Test and challenge artifacts
are neither discovered nor opened.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import heapq
import json
import os
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .schemas import validate_training_pair
from .training_integrity import BoundTrainingDataset, TrainingIntegrityError, bind_training_dataset
from .v2_hardcase_dataset import HardCaseDatasetError, verify_v2_hardcase_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_V1_ROOT = PROJECT_ROOT / "artifacts" / "final_data_v1"
DEFAULT_V2_ROOT = PROJECT_ROOT / "artifacts" / "v2_hardcase_data_v1"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "v2_training_mix_v1"
_MANIFEST_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "v2_training_mix_manifest_schema.json"

_MIX_SEED = 270023
_V1_TRAIN_REPLAY_COUNT = 10_240
_V1_VALIDATION_REPLAY_COUNT = 640
_V2_TRAIN_COUNT = 10_240
_V2_VALIDATION_COUNT = 640
_V1_MANIFEST_SHA256 = "sha256:bdbe13b3ed3c2258d293fa8f4d8f6761b2be91363712cdb80000ce38f020a802"
_V1_TRAIN_SHA256 = "sha256:8a9087e4124013a075953eddeaa56bc6535d9ffcef1adf03e5adc750aeeb56c6"
_V1_VALIDATION_SHA256 = "sha256:78240307231b0fd72eaffe8a61400f65ee46edb1ad13f6b209d4e89016a2913f"
_V1_TRAIN_SOURCE_COUNT = 120_000
_V1_VALIDATION_SOURCE_COUNT = 10_010
_FORBIDDEN_PATH_PARTS = frozenset({"test", "challenge"})
_SHA256_PATTERN = "sha256:" + "0" * 64


class V2TrainingMixError(RuntimeError):
    """A source, selection, or output artifact failed closed verification."""


@dataclass(frozen=True, slots=True)
class V2TrainingMixBuild:
    output_dir: Path
    train_path: Path
    validation_path: Path
    manifest_path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _MixContract:
    seed: int
    v1_manifest_sha256: str
    v1_train_sha256: str
    v1_validation_sha256: str
    v1_train_source_count: int
    v1_validation_source_count: int
    v1_train_replay_count: int
    v1_validation_replay_count: int
    v2_train_count: int
    v2_validation_count: int


_PRODUCTION_CONTRACT = _MixContract(
    seed=_MIX_SEED,
    v1_manifest_sha256=_V1_MANIFEST_SHA256,
    v1_train_sha256=_V1_TRAIN_SHA256,
    v1_validation_sha256=_V1_VALIDATION_SHA256,
    v1_train_source_count=_V1_TRAIN_SOURCE_COUNT,
    v1_validation_source_count=_V1_VALIDATION_SOURCE_COUNT,
    v1_train_replay_count=_V1_TRAIN_REPLAY_COUNT,
    v1_validation_replay_count=_V1_VALIDATION_REPLAY_COUNT,
    v2_train_count=_V2_TRAIN_COUNT,
    v2_validation_count=_V2_VALIDATION_COUNT,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_bytes(value: object) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(_canonical_json(row) + "\n" for row in rows).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise V2TrainingMixError(f"cannot hash required artifact {path.name}: {error}") from error
    return "sha256:" + digest.hexdigest()


def _sha256_identifiers(values: Sequence[str]) -> str:
    return _sha256_bytes(_canonical_json(list(values)).encode("utf-8"))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(_SHA256_PATTERN)
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _required_text(row: Mapping[str, Any], field: str, label: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise V2TrainingMixError(f"{label} requires non-empty {field}")
    return value


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2TrainingMixError(f"{label} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise V2TrainingMixError(f"{label} must contain an object")
    return value, payload


def _assert_safe_root(path: str | Path, label: str) -> Path:
    resolved = Path(path).resolve()
    forbidden = [part for part in resolved.parts if part.casefold() in _FORBIDDEN_PATH_PARTS]
    if forbidden:
        raise V2TrainingMixError(f"{label} contains a prohibited path component")
    return resolved


def _source_paths(v1_root: str | Path, v2_root: str | Path) -> dict[str, Path]:
    v1 = _assert_safe_root(v1_root, "V1 source root")
    v2 = _assert_safe_root(v2_root, "V2 source root")
    return {
        "v1_root": v1,
        "v1_manifest": v1 / "dataset_manifest.json",
        "v1_train": v1 / "splits" / "train.jsonl",
        "v1_validation": v1 / "splits" / "validation.jsonl",
        "v2_root": v2,
        "v2_manifest": v2 / "manifest.json",
        "v2_train": v2 / "train.jsonl",
        "v2_validation": v2 / "validation.jsonl",
    }


def _assert_output_safe(output_root: str | Path, source_paths: Mapping[str, Path]) -> Path:
    output = _assert_safe_root(output_root, "mix output root")
    for key in ("v1_root", "v2_root"):
        source = source_paths[key]
        if output == source or source in output.parents or output in source.parents:
            raise V2TrainingMixError("mix output and source dataset roots must be disjoint")
    for prohibited in ("test.jsonl", "challenge.jsonl"):
        if (output / prohibited).exists():
            raise V2TrainingMixError("mix output contains a prohibited test or challenge artifact")
    return output


def _bind_dataset(
    manifest: Path,
    train: Path,
    validation: Path,
    *,
    label: str,
) -> BoundTrainingDataset:
    try:
        return bind_training_dataset(
            manifest,
            train=train,
            validation=validation,
            train_limit=1,
            validation_limit=1,
        )
    except TrainingIntegrityError as error:
        raise V2TrainingMixError(f"{label} failed the training dataset binder: {error}") from error


def _bind_v1_source(paths: Mapping[str, Path], contract: _MixContract) -> tuple[BoundTrainingDataset, dict[str, Any]]:
    manifest, manifest_bytes = _load_json_object(paths["v1_manifest"], "V1 dataset manifest")
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    if manifest_sha256 != contract.v1_manifest_sha256:
        raise V2TrainingMixError("V1 dataset manifest differs from the frozen replay contract")
    if manifest.get("manifest_version") != 1:
        raise V2TrainingMixError("V1 dataset manifest has an unsupported version")
    if manifest.get("split_group") != "parent_canonical_document_id":
        raise V2TrainingMixError("V1 dataset does not split by parent canonical document")
    sealed = manifest.get("sealed_splits")
    if not isinstance(sealed, list) or set(sealed) != {"test", "challenge"}:
        raise V2TrainingMixError("V1 dataset must keep test and challenge sealed")
    bound = _bind_dataset(
        paths["v1_manifest"],
        paths["v1_train"],
        paths["v1_validation"],
        label="V1 replay source",
    )
    expected = {
        "train": (contract.v1_train_sha256, contract.v1_train_source_count),
        "validation": (contract.v1_validation_sha256, contract.v1_validation_source_count),
    }
    for split, split_bound in (("train", bound.train), ("validation", bound.validation)):
        expected_sha256, expected_count = expected[split]
        if split_bound.sha256 != expected_sha256 or split_bound.total_records != expected_count:
            raise V2TrainingMixError(f"V1 {split} split differs from the frozen replay contract")
    return bound, manifest


def _verify_reviewed_v2_source(
    paths: Mapping[str, Path], contract: _MixContract
) -> tuple[BoundTrainingDataset, dict[str, Any]]:
    try:
        verified = verify_v2_hardcase_dataset(paths["v2_root"])
    except (HardCaseDatasetError, OSError, ValueError) as error:
        raise V2TrainingMixError(f"reviewed V2 hard-case source failed verification: {error}") from error
    manifest = dict(verified.manifest)
    if verified.manifest_path.resolve() != paths["v2_manifest"].resolve():
        raise V2TrainingMixError("reviewed V2 verifier returned an unexpected manifest path")
    if verified.train_path.resolve() != paths["v2_train"].resolve():
        raise V2TrainingMixError("reviewed V2 verifier returned an unexpected train path")
    if verified.validation_path.resolve() != paths["v2_validation"].resolve():
        raise V2TrainingMixError("reviewed V2 verifier returned an unexpected validation path")
    if manifest.get("kind") != "scriber_v2_reviewed_hardcase_dataset" or manifest.get("training_ready") is not True:
        raise V2TrainingMixError("V2 source is not a reviewed, training-ready hard-case dataset")
    if manifest.get("contains_test_or_challenge_split") is not False:
        raise V2TrainingMixError("V2 source permits a prohibited test or challenge split")
    if manifest.get("direct_test_or_challenge_reuse") is not False:
        raise V2TrainingMixError("V2 source permits direct test or challenge reuse")
    if manifest.get("split_counts") != {
        "train": contract.v2_train_count,
        "validation": contract.v2_validation_count,
    }:
        raise V2TrainingMixError("V2 source split counts differ from the mix contract")
    evidence = manifest.get("review_evidence")
    if not isinstance(evidence, Mapping):
        raise V2TrainingMixError("V2 source lacks critic review evidence")
    if evidence.get("role_counts") != {"adversarial": 2, "fact": 1, "style": 1}:
        raise V2TrainingMixError("V2 source lacks the required critic role coverage")
    reviewers = evidence.get("reviewer_ids")
    if not isinstance(reviewers, list) or len(reviewers) != 4 or len(set(reviewers)) != 4:
        raise V2TrainingMixError("V2 source requires four independent critic reviewers")
    for field in (
        "review_package_sha256",
        "review_candidate_sha256",
        "review_contract_sha256",
        "artifact_tree_sha256",
    ):
        if not _is_sha256(manifest.get(field)):
            raise V2TrainingMixError(f"V2 source requires a valid {field}")
    for field in ("sha256", "evidence_tree_sha256"):
        if not _is_sha256(evidence.get(field)):
            raise V2TrainingMixError(f"V2 source requires a valid review evidence {field}")
    bound = _bind_dataset(
        paths["v2_manifest"],
        paths["v2_train"],
        paths["v2_validation"],
        label="reviewed V2 hard-case source",
    )
    return bound, manifest


def _selection_rank(seed: int, split: str, example_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"scriber-v2-v1-replay-v1:{seed}:{split}:{example_id}".encode()).digest(),
        "big",
    )


def _stream_select_v1(
    path: Path,
    *,
    split: str,
    count: int,
    seed: int,
    seen_ids: set[str],
    parents: set[str],
) -> tuple[list[dict[str, Any]], int]:
    if count <= 0:
        raise V2TrainingMixError("V1 replay count must be positive")
    heap: list[tuple[int, str, dict[str, Any]]] = []
    seen_ranks: set[int] = set()
    total = 0
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line or raw_line in {b"\n", b"\r\n"}:
                    raise V2TrainingMixError(f"blank V1 {split} line at {line_number}")
                try:
                    row = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise V2TrainingMixError(f"invalid V1 {split} JSON at line {line_number}: {error}") from error
                if not isinstance(row, dict):
                    raise V2TrainingMixError(f"V1 {split} line {line_number} must be an object")
                example_id = _required_text(row, "example_id", f"V1 {split} line {line_number}")
                if example_id in seen_ids:
                    raise V2TrainingMixError(f"duplicate V1 example ID: {example_id}")
                seen_ids.add(example_id)
                if row.get("split") != split:
                    raise V2TrainingMixError(f"V1 {split} line {line_number} declares a different split")
                parent = _required_text(
                    row,
                    "parent_canonical_document_id",
                    f"V1 {split} line {line_number}",
                )
                parents.add(parent)
                _required_text(row, "source_text", f"V1 {split} line {line_number}")
                _required_text(row, "target_sst", f"V1 {split} line {line_number}")
                rank = _selection_rank(seed, split, example_id)
                if rank in seen_ranks:
                    raise V2TrainingMixError("V1 replay selection rank collision")
                seen_ranks.add(rank)
                entry = (-rank, example_id, row)
                if len(heap) < count:
                    heapq.heappush(heap, entry)
                elif entry[0] > heap[0][0]:
                    heapq.heapreplace(heap, entry)
                total += 1
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise V2TrainingMixError(f"cannot read V1 {split} source: {error}") from error
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2TrainingMixError(f"V1 {split} source changed while it was selected")
    if total < count:
        raise V2TrainingMixError(f"V1 {split} source has only {total} rows; {count} are required")
    return sorted((entry[2] for entry in heap), key=lambda row: str(row["example_id"])), total


def _read_v2_split(
    path: Path,
    *,
    split: str,
    expected_count: int,
    seen_ids: set[str],
    parents: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line or raw_line in {b"\n", b"\r\n"}:
                    raise V2TrainingMixError(f"blank V2 {split} line at {line_number}")
                try:
                    row = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise V2TrainingMixError(f"invalid V2 {split} JSON at line {line_number}: {error}") from error
                if not isinstance(row, dict):
                    raise V2TrainingMixError(f"V2 {split} line {line_number} must be an object")
                example_id = _required_text(row, "example_id", f"V2 {split} line {line_number}")
                if example_id in seen_ids:
                    raise V2TrainingMixError(f"duplicate V2 example ID: {example_id}")
                seen_ids.add(example_id)
                if row.get("split") != split:
                    raise V2TrainingMixError(f"V2 {split} line {line_number} declares a different split")
                parents.add(
                    _required_text(
                        row,
                        "parent_canonical_document_id",
                        f"V2 {split} line {line_number}",
                    )
                )
                rows.append(row)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise V2TrainingMixError(f"cannot read V2 {split} source: {error}") from error
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2TrainingMixError(f"V2 {split} source changed while it was read")
    if len(rows) != expected_count:
        raise V2TrainingMixError(f"V2 {split} source count differs from the mix contract")
    return sorted(rows, key=lambda row: str(row["example_id"]))


def _all_strings_nfc(value: object) -> bool:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value) == value
    if isinstance(value, Mapping):
        return all(_all_strings_nfc(key) and _all_strings_nfc(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return all(_all_strings_nfc(item) for item in value)
    return True


def _mix_rank(seed: int, split: str, source: str, example_id: str) -> bytes:
    return hashlib.sha256(f"scriber-v2-stable-interleave-v1:{seed}:{split}:{source}:{example_id}".encode()).digest()


def _stable_mix(
    *,
    split: str,
    seed: int,
    v1_rows: Sequence[dict[str, Any]],
    v2_rows: Sequence[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    tagged = [("v1_replay", row) for row in v1_rows] + [("v2_hardcase", row) for row in v2_rows]
    return sorted(
        tagged,
        key=lambda item: (
            _mix_rank(seed, split, item[0], str(item[1]["example_id"])),
            item[0],
            str(item[1]["example_id"]),
        ),
    )


def _validate_rows(
    train: Sequence[tuple[str, dict[str, Any]]],
    validation: Sequence[tuple[str, dict[str, Any]]],
    *,
    v2_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    all_rows = [*train, *validation]
    ids: set[str] = set()
    pair_signatures: set[str] = set()
    parent_splits: dict[str, set[str]] = {}
    v2_reviewers = set(v2_manifest["review_evidence"]["reviewer_ids"])
    reviewed_package = str(v2_manifest["review_candidate_sha256"]).removeprefix("sha256:")
    for expected_split, tagged_rows in (("train", train), ("validation", validation)):
        for source, row in tagged_rows:
            if row.get("split") != expected_split:
                raise V2TrainingMixError("mixed row declares the wrong split")
            try:
                validate_training_pair(row)
            except ValueError as error:
                raise V2TrainingMixError(f"mixed training pair failed schema: {error}") from error
            if not _all_strings_nfc(row):
                raise V2TrainingMixError("mixed training pair is not Unicode NFC")
            example_id = str(row["example_id"])
            if example_id in ids:
                raise V2TrainingMixError(f"mixed dataset contains duplicate example ID: {example_id}")
            ids.add(example_id)
            parent = str(row["parent_canonical_document_id"])
            parent_splits.setdefault(parent, set()).add(expected_split)
            signature = _sha256_bytes(f"{row['source_text']}\0{row['target_sst']}".encode())
            if signature in pair_signatures:
                raise V2TrainingMixError("mixed dataset contains a duplicate source/target pair")
            pair_signatures.add(signature)
            if source == "v2_hardcase":
                if row.get("reviewed_package_sha256") != reviewed_package:
                    raise V2TrainingMixError("V2 row differs from its reviewed package binding")
                critics = row.get("critic_agents")
                if not isinstance(critics, list) or set(critics) != v2_reviewers:
                    raise V2TrainingMixError("V2 row differs from its four-critic binding")
    if any(len(splits) != 1 for splits in parent_splits.values()):
        raise V2TrainingMixError("mixed dataset contains parent split leakage")
    return {
        "allowed_splits_only": True,
        "duplicate_example_id_count": 0,
        "duplicate_pair_count": 0,
        "normalized_nfc": True,
        "parent_split_leakage_count": 0,
        "schema_validated_pair_count": len(all_rows),
        "source_provenance_binding": True,
    }


def _source_assignment_sha(rows: Sequence[tuple[str, dict[str, Any]]]) -> str:
    identity = [{"example_id": row["example_id"], "source": source} for source, row in rows]
    return _sha256_bytes(_canonical_json(identity).encode("utf-8"))


def _compose_manifest(
    *,
    contract: _MixContract,
    v1_bound: BoundTrainingDataset,
    v1_manifest: Mapping[str, Any],
    v1_train: Sequence[dict[str, Any]],
    v1_validation: Sequence[dict[str, Any]],
    v2_bound: BoundTrainingDataset,
    v2_manifest: Mapping[str, Any],
    v2_train: Sequence[dict[str, Any]],
    v2_validation: Sequence[dict[str, Any]],
    mixed_train: Sequence[tuple[str, dict[str, Any]]],
    mixed_validation: Sequence[tuple[str, dict[str, Any]]],
    train_bytes: bytes,
    validation_bytes: bytes,
    quality_gates: Mapping[str, Any],
) -> dict[str, Any]:
    v2_evidence = v2_manifest["review_evidence"]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_v2_training_mix",
        "training_ready": True,
        "seed": contract.seed,
        "selection_algorithm": "v1_split_sha256_bottom_k_then_source_sha256_interleave_v1",
        "manifest_schema_sha256": _sha256_file(_MANIFEST_SCHEMA_PATH),
        "sources": {
            "v1_replay": {
                "kind": "scriber_v1_frozen_replay",
                "manifest_file": "dataset_manifest.json",
                "manifest_sha256": str(v1_bound.manifest["sha256"]),
                "source_split_files": {
                    "train": "splits/train.jsonl",
                    "validation": "splits/validation.jsonl",
                },
                "source_split_sha256": {
                    "train": v1_bound.train.sha256,
                    "validation": v1_bound.validation.sha256,
                },
                "source_split_counts": {
                    "train": v1_bound.train.total_records,
                    "validation": v1_bound.validation.total_records,
                },
                "selected_counts": {
                    "train": len(v1_train),
                    "validation": len(v1_validation),
                },
                "selected_example_ids_sha256": {
                    "train": _sha256_identifiers([str(row["example_id"]) for row in v1_train]),
                    "validation": _sha256_identifiers([str(row["example_id"]) for row in v1_validation]),
                },
                "parent_split_group": str(v1_manifest["split_group"]),
                "sealed_splits": list(v1_manifest["sealed_splits"]),
            },
            "v2_hardcase": {
                "kind": "scriber_v2_reviewed_hardcase_dataset",
                "training_ready": True,
                "manifest_file": "manifest.json",
                "manifest_sha256": str(v2_bound.manifest["sha256"]),
                "artifact_tree_sha256": v2_manifest["artifact_tree_sha256"],
                "source_split_files": {"train": "train.jsonl", "validation": "validation.jsonl"},
                "source_split_sha256": {
                    "train": v2_bound.train.sha256,
                    "validation": v2_bound.validation.sha256,
                },
                "source_split_counts": {
                    "train": v2_bound.train.total_records,
                    "validation": v2_bound.validation.total_records,
                },
                "selected_counts": {"train": len(v2_train), "validation": len(v2_validation)},
                "selected_example_ids_sha256": {
                    "train": _sha256_identifiers([str(row["example_id"]) for row in v2_train]),
                    "validation": _sha256_identifiers([str(row["example_id"]) for row in v2_validation]),
                },
                "review_package_sha256": v2_manifest["review_package_sha256"],
                "review_candidate_sha256": v2_manifest["review_candidate_sha256"],
                "review_contract_sha256": v2_manifest["review_contract_sha256"],
                "review_evidence_sha256": v2_evidence["sha256"],
                "review_evidence_tree_sha256": v2_evidence["evidence_tree_sha256"],
                "role_counts": dict(v2_evidence["role_counts"]),
                "reviewer_ids": list(v2_evidence["reviewer_ids"]),
            },
        },
        "split_files": {"train": "train.jsonl", "validation": "validation.jsonl"},
        "split_sha256": {
            "train": _sha256_bytes(train_bytes),
            "validation": _sha256_bytes(validation_bytes),
        },
        "split_counts": {"train": len(mixed_train), "validation": len(mixed_validation)},
        "source_counts": {
            "train": {"v1_replay": len(v1_train), "v2_hardcase": len(v2_train)},
            "validation": {"v1_replay": len(v1_validation), "v2_hardcase": len(v2_validation)},
        },
        "mixed_order": {
            "example_ids_sha256": {
                "train": _sha256_identifiers([str(row["example_id"]) for _source, row in mixed_train]),
                "validation": _sha256_identifiers([str(row["example_id"]) for _source, row in mixed_validation]),
            },
            "source_assignment_sha256": {
                "train": _source_assignment_sha(mixed_train),
                "validation": _source_assignment_sha(mixed_validation),
            },
        },
        "quality_gates": dict(quality_gates),
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
    }
    identity = copy.deepcopy(manifest)
    manifest["artifact_tree_sha256"] = _sha256_bytes(_canonical_json(identity).encode("utf-8"))
    return manifest


def _validate_mix_manifest(manifest: Mapping[str, Any], contract: _MixContract) -> None:
    try:
        schema = json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2TrainingMixError(f"mix manifest schema is unreadable: {error}") from error
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(manifest)), key=lambda error: list(error.path))
    if errors:
        raise V2TrainingMixError(f"mix manifest failed schema: {errors[0].message}")
    if manifest.get("seed") != contract.seed:
        raise V2TrainingMixError("mix manifest seed differs from the frozen contract")
    expected_sources = {
        "train": {"v1_replay": contract.v1_train_replay_count, "v2_hardcase": contract.v2_train_count},
        "validation": {
            "v1_replay": contract.v1_validation_replay_count,
            "v2_hardcase": contract.v2_validation_count,
        },
    }
    if manifest.get("source_counts") != expected_sources:
        raise V2TrainingMixError("mix manifest source counts differ from the frozen contract")
    if manifest.get("split_counts") != {
        "train": contract.v1_train_replay_count + contract.v2_train_count,
        "validation": contract.v1_validation_replay_count + contract.v2_validation_count,
    }:
        raise V2TrainingMixError("mix manifest split counts differ from the frozen contract")
    identity = copy.deepcopy(dict(manifest))
    declared_tree = identity.pop("artifact_tree_sha256", None)
    if _sha256_bytes(_canonical_json(identity).encode("utf-8")) != declared_tree:
        raise V2TrainingMixError("mix artifact tree SHA-256 differs from the manifest")


def _prepare_mix(
    v1_root: str | Path,
    v2_root: str | Path,
    *,
    contract: _MixContract,
) -> tuple[dict[str, Any], bytes, bytes]:
    paths = _source_paths(v1_root, v2_root)
    v1_bound, v1_manifest = _bind_v1_source(paths, contract)
    v2_bound, v2_manifest = _verify_reviewed_v2_source(paths, contract)

    v1_seen_ids: set[str] = set()
    v1_train_parents: set[str] = set()
    v1_validation_parents: set[str] = set()
    v1_train, v1_train_total = _stream_select_v1(
        paths["v1_train"],
        split="train",
        count=contract.v1_train_replay_count,
        seed=contract.seed,
        seen_ids=v1_seen_ids,
        parents=v1_train_parents,
    )
    v1_validation, v1_validation_total = _stream_select_v1(
        paths["v1_validation"],
        split="validation",
        count=contract.v1_validation_replay_count,
        seed=contract.seed,
        seen_ids=v1_seen_ids,
        parents=v1_validation_parents,
    )
    if v1_train_total != contract.v1_train_source_count or v1_validation_total != contract.v1_validation_source_count:
        raise V2TrainingMixError("V1 replay selection observed unexpected source counts")
    if v1_train_parents & v1_validation_parents:
        raise V2TrainingMixError("V1 replay source contains parent split leakage")

    v2_seen_ids: set[str] = set()
    v2_train_parents: set[str] = set()
    v2_validation_parents: set[str] = set()
    v2_train = _read_v2_split(
        paths["v2_train"],
        split="train",
        expected_count=contract.v2_train_count,
        seen_ids=v2_seen_ids,
        parents=v2_train_parents,
    )
    v2_validation = _read_v2_split(
        paths["v2_validation"],
        split="validation",
        expected_count=contract.v2_validation_count,
        seen_ids=v2_seen_ids,
        parents=v2_validation_parents,
    )
    if v2_train_parents & v2_validation_parents:
        raise V2TrainingMixError("V2 hard-case source contains parent split leakage")

    mixed_train = _stable_mix(
        split="train",
        seed=contract.seed,
        v1_rows=v1_train,
        v2_rows=v2_train,
    )
    mixed_validation = _stable_mix(
        split="validation",
        seed=contract.seed,
        v1_rows=v1_validation,
        v2_rows=v2_validation,
    )
    quality_gates = _validate_rows(mixed_train, mixed_validation, v2_manifest=v2_manifest)
    train_bytes = _jsonl_bytes([row for _source, row in mixed_train])
    validation_bytes = _jsonl_bytes([row for _source, row in mixed_validation])
    manifest = _compose_manifest(
        contract=contract,
        v1_bound=v1_bound,
        v1_manifest=v1_manifest,
        v1_train=v1_train,
        v1_validation=v1_validation,
        v2_bound=v2_bound,
        v2_manifest=v2_manifest,
        v2_train=v2_train,
        v2_validation=v2_validation,
        mixed_train=mixed_train,
        mixed_validation=mixed_validation,
        train_bytes=train_bytes,
        validation_bytes=validation_bytes,
        quality_gates=quality_gates,
    )
    _validate_mix_manifest(manifest, contract)
    return manifest, train_bytes, validation_bytes


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise V2TrainingMixError(f"refusing to overwrite a different mix artifact: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise V2TrainingMixError(f"stale temporary mix artifact exists: {temporary.name}")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _verify_written_output(
    output: Path,
    *,
    expected_manifest: Mapping[str, Any],
    expected_train: bytes,
    expected_validation: bytes,
    contract: _MixContract,
) -> V2TrainingMixBuild:
    manifest_path = output / "manifest.json"
    train_path = output / "train.jsonl"
    validation_path = output / "validation.jsonl"
    manifest, _payload = _load_json_object(manifest_path, "mix manifest")
    _validate_mix_manifest(manifest, contract)
    if manifest != expected_manifest:
        raise V2TrainingMixError("written mix manifest differs from the deterministic source selection")
    if train_path.read_bytes() != expected_train or validation_path.read_bytes() != expected_validation:
        raise V2TrainingMixError("written mix split differs from the deterministic source selection")
    _bind_dataset(manifest_path, train_path, validation_path, label="materialized V2 training mix")
    return V2TrainingMixBuild(
        output_dir=output,
        train_path=train_path,
        validation_path=validation_path,
        manifest_path=manifest_path,
        manifest=MappingProxyType(manifest),
    )


def _build_v2_training_mix(
    v1_root: str | Path,
    v2_root: str | Path,
    output_root: str | Path,
    *,
    contract: _MixContract,
) -> V2TrainingMixBuild:
    paths = _source_paths(v1_root, v2_root)
    output = _assert_output_safe(output_root, paths)
    manifest, train_bytes, validation_bytes = _prepare_mix(v1_root, v2_root, contract=contract)
    _write_immutable(output / "train.jsonl", train_bytes)
    _write_immutable(output / "validation.jsonl", validation_bytes)
    _write_immutable(output / "manifest.json", _canonical_json_bytes(manifest))
    return _verify_written_output(
        output,
        expected_manifest=manifest,
        expected_train=train_bytes,
        expected_validation=validation_bytes,
        contract=contract,
    )


def build_v2_training_mix(
    v1_root: str | Path = DEFAULT_V1_ROOT,
    v2_root: str | Path = DEFAULT_V2_ROOT,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> V2TrainingMixBuild:
    """Build the exact 20,480/1,280 production V2 training mix."""

    return _build_v2_training_mix(v1_root, v2_root, output_root, contract=_PRODUCTION_CONTRACT)


def _verify_v2_training_mix(
    output_root: str | Path,
    *,
    v1_root: str | Path,
    v2_root: str | Path,
    contract: _MixContract,
) -> V2TrainingMixBuild:
    paths = _source_paths(v1_root, v2_root)
    output = _assert_output_safe(output_root, paths)
    expected_manifest, expected_train, expected_validation = _prepare_mix(v1_root, v2_root, contract=contract)
    return _verify_written_output(
        output,
        expected_manifest=expected_manifest,
        expected_train=expected_train,
        expected_validation=expected_validation,
        contract=contract,
    )


def verify_v2_training_mix(
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    *,
    v1_root: str | Path = DEFAULT_V1_ROOT,
    v2_root: str | Path = DEFAULT_V2_ROOT,
) -> V2TrainingMixBuild:
    """Independently recompute and verify the production V2 mix."""

    return _verify_v2_training_mix(
        output_root,
        v1_root=v1_root,
        v2_root=v2_root,
        contract=_PRODUCTION_CONTRACT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-root", type=Path, default=DEFAULT_V1_ROOT)
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = (
        verify_v2_training_mix(args.output, v1_root=args.v1_root, v2_root=args.v2_root)
        if args.check
        else build_v2_training_mix(args.v1_root, args.v2_root, args.output)
    )
    print(
        json.dumps(
            {
                "artifact_tree_sha256": result.manifest["artifact_tree_sha256"],
                "split_counts": result.manifest["split_counts"],
                "status": "verified",
                "training_ready": result.manifest["training_ready"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
