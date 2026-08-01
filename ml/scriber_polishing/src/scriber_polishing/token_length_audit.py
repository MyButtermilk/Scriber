"""Reproducible token-length evidence for frozen Gemma SFT datasets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .tokenize import IGNORE_INDEX, tokenize_example

AUDIT_THRESHOLDS = (512, 768, 1024)
TOKENIZER_ARTIFACT_FILENAMES = (
    "added_tokens.json",
    "chat_template.jinja",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
)
_SPLITS = ("train", "validation", "test", "challenge")
_PERCENTILES = (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99))
_ALGORITHM = "gemma_completion_only_token_length_audit_v1"
_PERCENTILE_METHOD = "nearest_rank_ceiling_v1"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return "sha256:" + digest.hexdigest(), byte_count


def _required_int_attribute(tokenizer: Any, name: str) -> int:
    value = getattr(tokenizer, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"exact tokenizer requires a non-negative integer {name}")
    return value


def build_tokenizer_evidence(
    tokenizer: Any,
    *,
    tokenizer_files: Mapping[str, Path],
    transformers_version: str,
) -> dict[str, Any]:
    """Bind the exact runtime tokenizer and every reviewed tokenizer artifact."""

    if getattr(tokenizer, "name_or_path", None) != BASE_MODEL_ID:
        raise ValueError(f"tokenizer must be loaded from {BASE_MODEL_ID}")
    if getattr(tokenizer, "is_fast", None) is not True:
        raise ValueError("exact Gemma audit requires the fast tokenizer")
    if getattr(tokenizer, "padding_side", None) != "right":
        raise ValueError("exact Gemma audit requires right tokenizer padding")
    if not isinstance(transformers_version, str) or not transformers_version:
        raise ValueError("transformers_version must be non-empty")
    chat_template = getattr(tokenizer, "chat_template", None)
    if not isinstance(chat_template, str) or not chat_template:
        raise ValueError("exact Gemma tokenizer requires a non-empty chat template")
    if set(tokenizer_files) != set(TOKENIZER_ARTIFACT_FILENAMES):
        raise ValueError(
            "tokenizer files must exactly match the reviewed Gemma artifact set: "
            + ", ".join(TOKENIZER_ARTIFACT_FILENAMES)
        )

    file_records: list[dict[str, Any]] = []
    for filename in TOKENIZER_ARTIFACT_FILENAMES:
        path = Path(tokenizer_files[filename])
        if path.name != filename or not path.is_file():
            raise ValueError(f"missing exact tokenizer artifact: {filename}")
        sha256, byte_count = _sha256_path(path)
        file_records.append(
            {
                "path": filename,
                "bytes": byte_count,
                "sha256": sha256,
            }
        )
    files_sha256 = _sha256_bytes(_canonical_json(file_records).encode("utf-8"))
    identity = {
        "class": type(tokenizer).__name__,
        "name_or_path": BASE_MODEL_ID,
        "requested_revision": BASE_MODEL_REVISION,
        "resolved_revision": BASE_MODEL_REVISION,
        "transformers_version": transformers_version,
        "is_fast": True,
        "vocab_size": _required_int_attribute(tokenizer, "vocab_size"),
        "padding_side": "right",
        "special_token_ids": {
            "bos": _required_int_attribute(tokenizer, "bos_token_id"),
            "eos": _required_int_attribute(tokenizer, "eos_token_id"),
            "pad": _required_int_attribute(tokenizer, "pad_token_id"),
            "unk": _required_int_attribute(tokenizer, "unk_token_id"),
        },
        "chat_template_sha256": _sha256_bytes(chat_template.encode("utf-8")),
        "files": file_records,
        "files_sha256": files_sha256,
    }
    identity["runtime_fingerprint_sha256"] = _sha256_bytes(
        _canonical_json(identity).encode("utf-8")
    )
    return identity


def load_exact_tokenizer_and_evidence(
    *,
    cache_dir: Path | None = None,
    local_files_only: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Load and byte-bind the exact tokenizer used by real Gemma SFT."""

    import transformers
    from huggingface_hub import hf_hub_download
    from transformers import AutoTokenizer

    load_options = {
        "revision": BASE_MODEL_REVISION,
        "trust_remote_code": False,
        "fix_mistral_regex": False,
        "local_files_only": local_files_only,
    }
    if cache_dir is not None:
        load_options["cache_dir"] = str(cache_dir)
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_ID, **load_options)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    tokenizer_files: dict[str, Path] = {}
    for filename in TOKENIZER_ARTIFACT_FILENAMES:
        download_options: dict[str, Any] = {
            "repo_id": BASE_MODEL_ID,
            "filename": filename,
            "repo_type": "model",
            "revision": BASE_MODEL_REVISION,
            "local_files_only": local_files_only,
        }
        if cache_dir is not None:
            download_options["cache_dir"] = str(cache_dir)
        tokenizer_files[filename] = Path(hf_hub_download(**download_options))
    return tokenizer, build_tokenizer_evidence(
        tokenizer,
        tokenizer_files=tokenizer_files,
        transformers_version=transformers.__version__,
    )


def _validate_tokenizer_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = json.loads(_canonical_json(value))
    if evidence.get("name_or_path") != BASE_MODEL_ID:
        raise ValueError("tokenizer evidence model identity does not match the fixed base")
    for key in ("requested_revision", "resolved_revision"):
        if evidence.get(key) != BASE_MODEL_REVISION:
            raise ValueError(f"tokenizer evidence {key} does not match the fixed revision")
    files = evidence.get("files")
    if not isinstance(files, list) or [item.get("path") for item in files if isinstance(item, dict)] != list(
        TOKENIZER_ARTIFACT_FILENAMES
    ):
        raise ValueError("tokenizer evidence has an incomplete or unordered artifact inventory")
    expected_files_hash = _sha256_bytes(_canonical_json(files).encode("utf-8"))
    if evidence.get("files_sha256") != expected_files_hash:
        raise ValueError("tokenizer evidence files hash does not match its inventory")
    runtime_fingerprint = evidence.pop("runtime_fingerprint_sha256", None)
    expected_runtime_fingerprint = _sha256_bytes(_canonical_json(evidence).encode("utf-8"))
    if runtime_fingerprint != expected_runtime_fingerprint:
        raise ValueError("tokenizer evidence runtime fingerprint does not match its contents")
    evidence["runtime_fingerprint_sha256"] = runtime_fingerprint
    return evidence


def _assert_runtime_tokenizer_matches_evidence(
    tokenizer: Any,
    evidence: Mapping[str, Any],
) -> None:
    observed = {
        "class": type(tokenizer).__name__,
        "name_or_path": getattr(tokenizer, "name_or_path", None),
        "is_fast": getattr(tokenizer, "is_fast", None),
        "vocab_size": getattr(tokenizer, "vocab_size", None),
        "padding_side": getattr(tokenizer, "padding_side", None),
        "special_token_ids": {
            "bos": getattr(tokenizer, "bos_token_id", None),
            "eos": getattr(tokenizer, "eos_token_id", None),
            "pad": getattr(tokenizer, "pad_token_id", None),
            "unk": getattr(tokenizer, "unk_token_id", None),
        },
        "chat_template_sha256": _sha256_bytes(
            str(getattr(tokenizer, "chat_template", "")).encode("utf-8")
        ),
    }
    expected = {key: evidence.get(key) for key in observed}
    if observed != expected:
        raise ValueError("runtime tokenizer does not match the bound tokenizer evidence")


def _nearest_rank(sorted_values: Sequence[int], percentile: float) -> int:
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def _length_summary(values: Sequence[int]) -> dict[str, int | float]:
    if not values:
        raise ValueError("cannot summarize an empty token-length collection")
    ordered = sorted(values)
    result: dict[str, int | float] = {
        "count": len(ordered),
        "min": ordered[0],
    }
    result.update({name: _nearest_rank(ordered, percentile) for name, percentile in _PERCENTILES})
    result["max"] = ordered[-1]
    result["mean"] = round(sum(ordered) / len(ordered), 6)
    return result


def _statistics(
    token_lengths: Sequence[int],
    prompt_lengths: Sequence[int],
    completion_lengths: Sequence[int],
) -> dict[str, Any]:
    if not (len(token_lengths) == len(prompt_lengths) == len(completion_lengths)):
        raise ValueError("token statistics inputs must have equal lengths")
    counts_above: dict[str, int] = {}
    truncation_estimates: dict[str, dict[str, int | float]] = {}
    for threshold in AUDIT_THRESHOLDS:
        over_limit = sum(length > threshold for length in token_lengths)
        tokens_removed = sum(max(0, length - threshold) for length in token_lengths)
        completion_removed = [
            max(0, completion - max(0, threshold - prompt))
            for prompt, completion in zip(prompt_lengths, completion_lengths, strict=True)
        ]
        counts_above[str(threshold)] = over_limit
        truncation_estimates[str(threshold)] = {
            "examples_over_limit": over_limit,
            "fraction_over_limit": round(over_limit / len(token_lengths), 8),
            "tokens_removed_by_right_truncation": tokens_removed,
            "completion_tokens_removed": sum(completion_removed),
            "examples_with_completion_partially_truncated": sum(
                0 < removed < completion
                for removed, completion in zip(completion_removed, completion_lengths, strict=True)
            ),
            "examples_with_completion_fully_truncated": sum(
                removed == completion
                for removed, completion in zip(completion_removed, completion_lengths, strict=True)
            ),
            "examples_rejected_by_training_filter": over_limit,
        }
    return {
        "token_length": _length_summary(token_lengths),
        "prompt_token_length": _length_summary(prompt_lengths),
        "completion_token_length": _length_summary(completion_lengths),
        "counts_above": counts_above,
        "truncation_estimates": truncation_estimates,
    }


def _required_text(record: Mapping[str, Any], field: str, *, location: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} requires non-empty {field}")
    return value


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_size,
        first.st_mtime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    )


def _read_split(
    split: str,
    path: Path,
    *,
    tokenizer: Any,
    seen_example_ids: set[str],
) -> tuple[dict[str, Any], tuple[list[int], list[int], list[int]]]:
    if not path.is_file():
        raise ValueError(f"frozen {split} split is not a regular file: {path}")
    digest = hashlib.sha256()
    byte_count = 0
    token_lengths: list[int] = []
    prompt_lengths: list[int] = []
    completion_lengths: list[int] = []
    with path.open("rb") as stream:
        first_snapshot = os.fstat(stream.fileno())
        for line_number, raw_line in enumerate(stream, start=1):
            digest.update(raw_line)
            byte_count += len(raw_line)
            location = f"{path}:{line_number}"
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as error:
                raise ValueError(f"{location} is not valid UTF-8") from error
            if not line.strip():
                raise ValueError(f"{location} is a blank JSONL row")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{location} is malformed JSON") from error
            if not isinstance(record, dict):
                raise ValueError(f"{location} training record must be an object")
            example_id = _required_text(record, "example_id", location=location)
            if example_id in seen_example_ids:
                raise ValueError(f"duplicate example_id across frozen splits: {example_id}")
            seen_example_ids.add(example_id)
            declared_split = _required_text(record, "split", location=location)
            if declared_split != split:
                raise ValueError(
                    f"{location} declares split {declared_split!r}, expected {split!r}"
                )
            source_text = _required_text(record, "source_text", location=location)
            target_sst = _required_text(record, "target_sst", location=location)
            try:
                encoded = tokenize_example(
                    tokenizer,
                    transcript=source_text,
                    completion=target_sst,
                )
            except (TypeError, ValueError) as error:
                raise ValueError(f"{location} tokenization failed: {error}") from error
            input_ids = encoded["input_ids"]
            labels = encoded["labels"]
            if len(input_ids) != len(labels) or not input_ids:
                raise ValueError(f"{location} produced invalid completion-only tokenization")
            completion_length = sum(label != IGNORE_INDEX for label in labels)
            prompt_length = len(labels) - completion_length
            if (
                completion_length <= 0
                or any(label != IGNORE_INDEX for label in labels[:prompt_length])
                or any(label == IGNORE_INDEX for label in labels[prompt_length:])
            ):
                raise ValueError(f"{location} produced a non-contiguous completion-only mask")
            token_lengths.append(len(input_ids))
            prompt_lengths.append(prompt_length)
            completion_lengths.append(completion_length)
        second_snapshot = os.fstat(stream.fileno())
    if not _same_file_snapshot(first_snapshot, second_snapshot):
        raise ValueError(f"frozen {split} split changed while it was audited: {path}")
    if not token_lengths:
        raise ValueError(f"frozen {split} split is empty: {path}")
    return (
        {
            "path": path.as_posix(),
            "bytes": byte_count,
            "count": len(token_lengths),
            "sha256": "sha256:" + digest.hexdigest(),
        },
        (token_lengths, prompt_lengths, completion_lengths),
    )


def _implementation_evidence(
    implementation_paths: Mapping[str, Path] | None,
) -> dict[str, Any]:
    paths = {"module": Path(__file__)}
    if implementation_paths is not None:
        paths.update({name: Path(path) for name, path in implementation_paths.items()})
    evidence: dict[str, Any] = {}
    for name in sorted(paths):
        path = paths[name]
        if not path.is_file():
            raise ValueError(f"implementation evidence file does not exist: {path}")
        sha256, byte_count = _sha256_path(path)
        evidence[name] = {
            "path": path.name,
            "bytes": byte_count,
            "sha256": sha256,
        }
    return evidence


def _write_canonical_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists() and not path.is_file():
        raise ValueError(f"audit output must be a file path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def audit_token_lengths(
    split_paths: Mapping[str, Path],
    *,
    tokenizer: Any,
    tokenizer_evidence: Mapping[str, Any],
    output_path: Path,
    command: Sequence[str],
    implementation_paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Audit frozen JSONL splits and atomically publish canonical evidence."""

    if "train" not in split_paths:
        raise ValueError("token-length audit requires the frozen train split")
    unknown_splits = sorted(set(split_paths) - set(_SPLITS))
    if unknown_splits:
        raise ValueError(f"unknown frozen dataset split: {unknown_splits[0]}")
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command evidence must be a non-empty string sequence")
    resolved_output = Path(output_path).resolve()
    normalized_paths = {split: Path(path) for split, path in split_paths.items()}
    resolved_inputs = [path.resolve() for path in normalized_paths.values()]
    if len(set(resolved_inputs)) != len(resolved_inputs):
        raise ValueError("frozen dataset splits must use distinct input files")
    if resolved_output in resolved_inputs:
        raise ValueError("audit output must not overwrite a frozen dataset split")

    tokenizer_binding = _validate_tokenizer_evidence(tokenizer_evidence)
    _assert_runtime_tokenizer_matches_evidence(tokenizer, tokenizer_binding)
    input_evidence: dict[str, Any] = {}
    split_statistics: dict[str, Any] = {}
    all_token_lengths: list[int] = []
    all_prompt_lengths: list[int] = []
    all_completion_lengths: list[int] = []
    seen_example_ids: set[str] = set()
    for split in _SPLITS:
        if split not in normalized_paths:
            continue
        input_record, lengths = _read_split(
            split,
            normalized_paths[split],
            tokenizer=tokenizer,
            seen_example_ids=seen_example_ids,
        )
        token_lengths, prompt_lengths, completion_lengths = lengths
        input_evidence[split] = input_record
        split_statistics[split] = _statistics(
            token_lengths,
            prompt_lengths,
            completion_lengths,
        )
        all_token_lengths.extend(token_lengths)
        all_prompt_lengths.extend(prompt_lengths)
        all_completion_lengths.extend(completion_lengths)

    effective_config = {
        "algorithm": _ALGORITHM,
        "thresholds": list(AUDIT_THRESHOLDS),
        "percentile_method": _PERCENTILE_METHOD,
        "split_order": [split for split in _SPLITS if split in normalized_paths],
        "representation": {
            "builder": "scriber_polishing.tokenize.tokenize_example",
            "chat_template_api": "tokenizer.apply_chat_template",
            "completion_only_ignore_index": IGNORE_INDEX,
            "source_field": "source_text",
            "completion_field": "target_sst",
            "right_truncation": True,
            "training_filter": "reject_if_token_length_exceeds_limit",
        },
    }
    command_record = {
        "argv": list(command),
        "sha256": _sha256_bytes(_canonical_json(list(command)).encode("utf-8")),
    }
    dataset_binding = [
        {
            "split": split,
            "count": input_evidence[split]["count"],
            "sha256": input_evidence[split]["sha256"],
        }
        for split in _SPLITS
        if split in input_evidence
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_token_length_audit",
        "status": "complete",
        "model": {
            "id": BASE_MODEL_ID,
            "revision": BASE_MODEL_REVISION,
        },
        "tokenizer": tokenizer_binding,
        "inputs": input_evidence,
        "dataset_sha256": _sha256_bytes(_canonical_json(dataset_binding).encode("utf-8")),
        "statistics": {
            "all": _statistics(
                all_token_lengths,
                all_prompt_lengths,
                all_completion_lengths,
            ),
            "splits": split_statistics,
        },
        "completion_only_mask_verified_records": len(all_token_lengths),
        "evidence": {
            "command": command_record,
            "effective_config": effective_config,
            "effective_config_sha256": _sha256_bytes(
                _canonical_json(effective_config).encode("utf-8")
            ),
            "implementation": _implementation_evidence(implementation_paths),
        },
    }
    _write_canonical_json_atomic(Path(output_path), report)
    return report
