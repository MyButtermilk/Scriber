"""Deterministic local and batch inference with fail-closed SST handling."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .evaluation_io import (
    load_evaluation_references,
    validate_candidate_prediction,
    validate_evaluation_reference,
)
from .protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEGACY_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    PROTECTION_POLICY_FILENAME,
    ProtectedText,
    ProtectionPolicyError,
    _protect_with_validated_policy,
    legacy_policy_evidence,
    protect_spans,
    restore_spans,
    validate_protection_policy_evidence,
)
from .safe_inference import (
    protect_product_spans,
    run_product_inference,
    warmup_product_inference,
)
from .sst_parser import SSTParseError, parse_sst
from .target_renderer import render_html, render_markdown, render_plain_text
from .tokenize import POLISHING_INSTRUCTION
from .variant_artifacts import verify_saved_bitsandbytes_config

_PROJECT_ROOT = Path(__file__).parents[2]
_RESUME_JOURNAL_SCHEMA = _PROJECT_ROOT / "contracts" / "batch_inference_resume_journal_schema.json"
_PREDICTION_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "batch_inference_prediction_manifest_schema.json"
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CHECKPOINT_INVENTORY = "checkpoint-inventory.json"


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """One model completion plus bounded operational evidence."""

    prediction_sst: str
    valid_sst: bool
    restoration_error: str | None
    generation_seconds: float
    input_tokens: int
    output_tokens: int


def _token_ids(tokenizer: Any, text: str) -> list[Any]:
    messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]
    token_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()
    if token_ids and isinstance(token_ids[0], list):
        token_ids = token_ids[0]
    return list(token_ids)


def _decode_generated(
    tokenizer: Any,
    generated: Any,
    *,
    input_length: int,
) -> tuple[str, int]:
    if hasattr(generated, "tolist"):
        generated = generated.tolist()
    if generated and isinstance(generated[0], list):
        generated = generated[0]
    completion_tokens = list(generated[input_length:])
    return tokenizer.decode(completion_tokens, skip_special_tokens=True), len(completion_tokens)


def generate_prediction(
    transcript: str,
    *,
    tokenizer: Any,
    model: Any,
    max_new_tokens: int = 512,
    span_protector: Callable[[str], ProtectedText] = protect_spans,
) -> PredictionResult:
    """Generate raw SST evidence; invalid output remains visible to evaluation."""

    protected = span_protector(transcript)
    messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{protected.text}"}]
    tensor_inputs: Mapping[str, Any] | None = None
    try:
        encoded = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        if isinstance(encoded, Mapping):
            tensor_inputs = encoded
    except TypeError:
        tensor_inputs = None

    started = time.perf_counter()
    if tensor_inputs is None:
        input_ids = _token_ids(tokenizer, protected.text)
        generated = model.generate(
            input_ids,
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new_tokens,
        )
        completion, output_tokens = _decode_generated(
            tokenizer,
            generated,
            input_length=len(input_ids),
        )
        input_tokens = len(input_ids)
    else:
        device = getattr(model, "device", None)
        if device is not None and hasattr(tensor_inputs, "to"):
            tensor_inputs = tensor_inputs.to(device)
        elif device is not None:
            tensor_inputs = {
                key: value.to(device) if hasattr(value, "to") else value for key, value in tensor_inputs.items()
            }
        generated = model.generate(
            **dict(tensor_inputs),
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new_tokens,
            pad_token_id=getattr(tokenizer, "eos_token_id", None),
        )
        input_tensor = tensor_inputs["input_ids"]
        input_length = int(input_tensor.shape[-1])
        completion, output_tokens = _decode_generated(
            tokenizer,
            generated,
            input_length=input_length,
        )
        input_tokens = input_length
    elapsed = time.perf_counter() - started

    restoration_error: str | None = None
    try:
        prediction = restore_spans(protected, completion)
    except ValueError as error:
        prediction = completion
        restoration_error = str(error)
    if not prediction:
        prediction = " "
    valid_sst = restoration_error is None
    if valid_sst:
        try:
            parse_sst(prediction)
        except (SSTParseError, ValueError):
            valid_sst = False
    return PredictionResult(
        prediction_sst=prediction,
        valid_sst=valid_sst,
        restoration_error=restoration_error,
        generation_seconds=elapsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _token_matrix(value: Any, label: str) -> list[list[Any]]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list) or not value:
        raise ValueError(f"batched inference {label} must be a non-empty matrix")
    if not all(isinstance(row, list) and row for row in value):
        raise ValueError(f"batched inference {label} contains an invalid row")
    width = len(value[0])
    if any(len(row) != width for row in value):
        raise ValueError(f"batched inference {label} rows have different widths")
    return value


def _trim_generated_tokens(
    tokens: Sequence[Any],
    *,
    eos_token_ids: frozenset[Any],
) -> list[Any]:
    completion: list[Any] = []
    for token in tokens:
        completion.append(token)
        if token in eos_token_ids:
            break
    return completion


def generate_batch_predictions(
    transcripts: Sequence[str],
    *,
    tokenizer: Any,
    model: Any,
    max_new_tokens: int = 512,
    span_protector: Callable[[str], ProtectedText] = protect_spans,
) -> list[PredictionResult]:
    """Generate an ordered greedy batch with per-case single-run semantics."""

    if not transcripts:
        raise ValueError("batched inference requires at least one transcript")
    if max_new_tokens <= 0:
        raise ValueError("batched inference max_new_tokens must be positive")
    if any(not isinstance(transcript, str) for transcript in transcripts):
        raise TypeError("batched inference transcripts must be strings")
    protected = [span_protector(transcript) for transcript in transcripts]
    conversations = [[{"role": "user", "content": f"{POLISHING_INSTRUCTION}{item.text}"}] for item in protected]
    if not hasattr(tokenizer, "padding_side"):
        raise ValueError("batched inference tokenizer does not declare padding_side")
    original_padding_side = tokenizer.padding_side
    try:
        tokenizer.padding_side = "left"
        encoded = tokenizer.apply_chat_template(
            conversations,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            padding=True,
        )
    finally:
        tokenizer.padding_side = original_padding_side
    if not isinstance(encoded, Mapping):
        raise ValueError("batched inference tokenizer did not return a mapping")
    if "input_ids" not in encoded or "attention_mask" not in encoded:
        raise ValueError("batched inference tokenizer must return input_ids and attention_mask")
    input_rows = _token_matrix(encoded["input_ids"], "input_ids")
    mask_rows = _token_matrix(encoded["attention_mask"], "attention_mask")
    if len(input_rows) != len(protected) or len(mask_rows) != len(protected):
        raise ValueError("batched inference tokenizer returned the wrong batch size")
    pad_token_id = getattr(tokenizer, "pad_token_id", None)
    if pad_token_id is None:
        raise ValueError("batched inference tokenizer has no pad token")
    input_token_counts: list[int] = []
    for index, (row, mask, item) in enumerate(zip(input_rows, mask_rows, protected, strict=True)):
        if any(value not in {0, 1} for value in mask):
            raise ValueError(f"batched inference attention mask {index} is not binary")
        active = sum(int(value) for value in mask)
        if active <= 0 or mask != [0] * (len(mask) - active) + [1] * active:
            raise ValueError(f"batched inference attention mask {index} is not left padded")
        if any(token != pad_token_id for token in row[: len(row) - active]):
            raise ValueError(f"batched inference input row {index} has invalid left padding")
        expected = _token_ids(tokenizer, item.text)
        if row[-active:] != expected:
            raise ValueError(f"batched inference input row {index} differs from single tokenization")
        input_token_counts.append(active)

    tensor_inputs: Mapping[str, Any] = encoded
    device = getattr(model, "device", None)
    if device is not None and hasattr(tensor_inputs, "to"):
        tensor_inputs = tensor_inputs.to(device)
    elif device is not None:
        tensor_inputs = {
            key: value.to(device) if hasattr(value, "to") else value for key, value in tensor_inputs.items()
        }
    started = time.perf_counter()
    generated = model.generate(
        **dict(tensor_inputs),
        do_sample=False,
        num_beams=1,
        max_new_tokens=max_new_tokens,
        pad_token_id=getattr(tokenizer, "eos_token_id", None),
    )
    elapsed = time.perf_counter() - started
    generated_rows = _token_matrix(generated, "generated sequences")
    input_width = len(input_rows[0])
    if len(generated_rows) != len(protected):
        raise ValueError("batched inference model returned the wrong batch size")
    if any(len(row) < input_width for row in generated_rows):
        raise ValueError("batched inference model returned a truncated prompt")
    for index, (generated_row, input_row) in enumerate(zip(generated_rows, input_rows, strict=True)):
        if generated_row[:input_width] != input_row:
            raise ValueError(f"batched inference generated sequence {index} changed its prompt")
    eos_value = getattr(
        getattr(model, "generation_config", None),
        "eos_token_id",
        None,
    )
    if eos_value is None:
        eos_token_ids = frozenset()
    elif isinstance(eos_value, int):
        eos_token_ids = frozenset({eos_value})
    elif isinstance(eos_value, Sequence) and not isinstance(eos_value, str | bytes):
        eos_token_ids = frozenset(eos_value)
    else:
        raise ValueError("batched inference model has an invalid EOS-token setting")
    per_case_seconds = elapsed / len(protected)
    results: list[PredictionResult] = []
    for index, (item, row) in enumerate(zip(protected, generated_rows, strict=True)):
        completion_tokens = _trim_generated_tokens(
            row[input_width:],
            eos_token_ids=eos_token_ids,
        )
        completion = tokenizer.decode(
            completion_tokens,
            skip_special_tokens=True,
        )
        restoration_error: str | None = None
        try:
            prediction = restore_spans(item, completion)
        except ValueError as error:
            prediction = completion
            restoration_error = str(error)
        if not prediction:
            prediction = " "
        valid_sst = restoration_error is None
        if valid_sst:
            try:
                parse_sst(prediction)
            except (SSTParseError, ValueError):
                valid_sst = False
        results.append(
            PredictionResult(
                prediction_sst=prediction,
                valid_sst=valid_sst,
                restoration_error=restoration_error,
                generation_seconds=per_case_seconds,
                input_tokens=input_token_counts[index],
                output_tokens=len(completion_tokens),
            )
        )
    return results


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _prefixed_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return "sha256:" + value.removeprefix("sha256:")


def _stable_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError(f"{label} changed while it was read")
    return resolved, payload


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON value {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _artifact_tree_hash(files: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _inventory_local_model_tree(root: Path) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"local inference model must be a regular directory: {root}")
    inventory_path = root / _CHECKPOINT_INVENTORY
    declared_inventory: Mapping[str, Any] | None = None
    if inventory_path.exists():
        _, inventory_payload = _stable_bytes(
            inventory_path,
            "local model checkpoint inventory",
        )
        inventory = _strict_json_object(
            inventory_payload,
            "local model checkpoint inventory",
        )
        if inventory_payload != _canonical_json(inventory):
            raise ValueError("local model checkpoint inventory is not canonical JSON")
        declared_inventory = inventory
        raw_files = inventory.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError("local model checkpoint inventory has no files")
        declared_files: list[dict[str, object]] = []
        previous = ""
        for raw in raw_files:
            if not isinstance(raw, Mapping):
                raise ValueError("local model checkpoint inventory file is not an object")
            relative = raw.get("path")
            if (
                not isinstance(relative, str)
                or not relative
                or relative <= previous
                or relative == _CHECKPOINT_INVENTORY
            ):
                raise ValueError("local model checkpoint inventory paths must be safe, unique, and strictly sorted")
            path_value = Path(relative)
            if path_value.is_absolute() or relative.startswith(("/", "\\")) or ".." in path_value.parts:
                raise ValueError(f"local model checkpoint inventory has unsafe path: {relative}")
            previous = relative
            expected_bytes = raw.get("bytes")
            if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
                raise ValueError(f"local model checkpoint inventory has invalid size: {relative}")
            expected_hash = _normalized_sha256(
                raw.get("sha256"),
                f"local model checkpoint inventory hash for {relative}",
            )
            physical, payload = _stable_bytes(
                root / Path(*path_value.parts),
                f"local model file {relative}",
            )
            try:
                physical.relative_to(root)
            except ValueError as error:
                raise ValueError("local model inventory path escapes its root") from error
            if len(payload) != expected_bytes or _prefixed_sha256(payload) != expected_hash:
                raise ValueError(f"local model file differs from inventory: {relative}")
            declared_files.append(
                {
                    "path": relative,
                    "bytes": expected_bytes,
                    "sha256": expected_hash,
                }
            )
        actual_paths: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise ValueError(f"local model tree contains a symlink: {path}")
            if path.is_file():
                actual_paths.append(path.relative_to(root).as_posix())
        expected_paths = [
            *(str(item["path"]) for item in declared_files),
            _CHECKPOINT_INVENTORY,
        ]
        if sorted(actual_paths) != sorted(expected_paths):
            raise ValueError("local model tree contains files not bound by checkpoint inventory")
        tree_sha256 = _artifact_tree_hash(declared_files)
        if (
            inventory.get("file_count") != len(declared_files)
            or inventory.get("total_bytes") != sum(int(item["bytes"]) for item in declared_files)
            or _normalized_sha256(
                inventory.get("tree_sha256"),
                "local model checkpoint tree hash",
            )
            != tree_sha256
        ):
            raise ValueError("local model checkpoint inventory totals are inconsistent")
        files = declared_files
    else:
        files = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_dir():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"local model tree contains a non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            _, payload = _stable_bytes(path, f"local model file {relative}")
            if not payload:
                raise ValueError(f"local model tree contains an empty file: {relative}")
            files.append(
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": _prefixed_sha256(payload),
                }
            )
        if not files:
            raise ValueError("local model tree is empty")
        tree_sha256 = _artifact_tree_hash(files)
    config = next(
        (item for item in files if item["path"] == "config.json"),
        None,
    )
    if config is None:
        raise ValueError("local model tree has no bound config.json")
    if not any(str(item["path"]).endswith(".safetensors") for item in files):
        raise ValueError("local model tree has no bound safetensors weights")
    if declared_inventory is not None:
        source_id = declared_inventory.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("local model checkpoint inventory has no source identity")
    return {
        "tree_sha256": tree_sha256,
        "config_sha256": config["sha256"],
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
    }


def _resolved_hub_revision(
    *,
    requested_revision: str | None,
    tokenizer: Any,
    model: Any,
) -> str:
    candidates = [
        getattr(getattr(model, "config", None), "_commit_hash", None),
        getattr(tokenizer, "init_kwargs", {}).get("_commit_hash")
        if isinstance(getattr(tokenizer, "init_kwargs", None), Mapping)
        else None,
        requested_revision if requested_revision and _COMMIT_RE.fullmatch(requested_revision) else None,
    ]
    resolved = next(
        (value for value in candidates if isinstance(value, str) and _COMMIT_RE.fullmatch(value)),
        None,
    )
    if resolved is None:
        raise ValueError("resumable Hub inference requires an exact resolved 40-character revision")
    if (
        requested_revision is not None
        and _COMMIT_RE.fullmatch(requested_revision) is not None
        and requested_revision != resolved
    ):
        raise ValueError("loaded Hub model revision differs from requested exact revision")
    return resolved


def build_batch_resume_binding(
    *,
    references_path: str | Path,
    references: Sequence[Mapping[str, Any]],
    candidate: str,
    model_reference: str,
    revision: str | None,
    tokenizer: Any,
    model: Any,
    quantization: str,
    device: str,
    max_new_tokens: int,
    batch_size: int = 1,
    protection_policy: Mapping[str, object],
    protection_binding: Mapping[str, object] | None,
    predictions_output: str | Path,
    manifest_output: str | Path,
) -> dict[str, Any]:
    """Bind every input that may affect resumable batch prediction bytes."""

    if not candidate:
        raise ValueError("resume candidate must be non-empty")
    if max_new_tokens <= 0:
        raise ValueError("resume max_new_tokens must be positive")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("resume batch_size must be a positive integer")
    reference_file, reference_payload = _stable_bytes(
        references_path,
        "resume references",
    )
    validated_references = tuple(validate_evaluation_reference(reference) for reference in references)
    if not validated_references:
        raise ValueError("resume references must not be empty")
    case_ids = [reference["case_id"] for reference in validated_references]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("resume references contain duplicate case IDs")
    local = Path(model_reference)
    if local.is_dir():
        root = local.resolve()
        local_inventory = _inventory_local_model_tree(root)
        model_binding: dict[str, Any] = {
            "kind": "local_tree",
            "model_reference": model_reference,
            "resolved_path": str(root),
            "requested_revision": revision,
            "resolved_revision": None,
            **local_inventory,
        }
    else:
        resolved_revision = _resolved_hub_revision(
            requested_revision=revision,
            tokenizer=tokenizer,
            model=model,
        )
        model_binding = {
            "kind": "hub_revision",
            "model_reference": model_reference,
            "resolved_path": None,
            "requested_revision": revision,
            "resolved_revision": resolved_revision,
            "tree_sha256": None,
            "config_sha256": None,
            "file_count": None,
            "total_bytes": None,
        }
    model_binding["identity_sha256"] = _prefixed_sha256(_canonical_json(model_binding))
    policy = validate_protection_policy_evidence(protection_policy)
    source = str(protection_binding.get("source")) if protection_binding is not None else "implicit_legacy_default"
    artifact_hash = (
        _normalized_sha256(
            protection_binding.get("artifact_sha256"),
            "resume protection-policy artifact hash",
        )
        if protection_binding is not None
        else None
    )
    predictions_path = Path(predictions_output).resolve()
    manifest_path = Path(manifest_output).resolve()
    if predictions_path == manifest_path:
        raise ValueError("prediction and manifest outputs must be different paths")
    return {
        "reference_sha256": _prefixed_sha256(reference_payload),
        "reference_case_ids_sha256": _prefixed_sha256(_canonical_json(case_ids)),
        "reference_count": len(validated_references),
        "candidate": candidate,
        "model": model_binding,
        "quantization": quantization,
        "device": device,
        "max_new_tokens": max_new_tokens,
        "batch_size": batch_size,
        "protection_policy": {
            "policy_id": policy["policy_id"],
            "policy_sha256": _normalized_sha256(
                policy["policy_sha256"],
                "resume protection policy hash",
            ),
            "evidence_sha256": _prefixed_sha256(_canonical_json(policy)),
            "source": source,
            "artifact_sha256": artifact_hash,
        },
        "predictions_output_path": str(predictions_path),
        "manifest_output_path": str(manifest_path),
    }


def _load_policy_bytes(path: Path, tokenizer: Any) -> tuple[dict[str, object], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ProtectionPolicyError(f"protection policy artifact must be a regular file: {path}")
    payload = path.read_bytes()
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtectionPolicyError(f"protection policy artifact is invalid JSON: {error}") from error
    policy = validate_protection_policy_evidence(raw, tokenizer=tokenizer)
    if payload != _canonical_json(policy):
        raise ProtectionPolicyError("protection policy artifact is not canonical immutable JSON")
    return policy, payload


def load_inference_protection_policy(
    *,
    model_reference: str,
    tokenizer: Any,
    model: Any,
    revision: str | None,
    local_files_only: bool,
    artifact_path: str | Path | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Load a policy only from verified model evidence; otherwise keep legacy."""

    model_config = getattr(model, "config", None)
    expected_policy_id = getattr(
        model_config,
        "scriber_protection_policy_id",
        None,
    )
    expected_policy_sha256 = getattr(
        model_config,
        "scriber_protection_policy_sha256",
        None,
    )
    explicit_policy_ids = {
        GEMMA_UNUSED_POLICY_ID,
        LEXICAL_KEEP_POLICY_ID,
    }
    if expected_policy_id not in {
        None,
        LEGACY_POLICY_ID,
        *explicit_policy_ids,
    }:
        raise ProtectionPolicyError(f"model declares an unsupported protection policy: {expected_policy_id}")
    selected_path: Path | None = None
    source = "implicit_legacy_default"
    if artifact_path is not None:
        selected_path = Path(artifact_path).resolve()
        source = "explicit_artifact"
    else:
        local_model = Path(model_reference)
        if local_model.is_dir():
            candidate = local_model.resolve() / PROTECTION_POLICY_FILENAME
            if candidate.exists():
                selected_path = candidate
                source = "local_model_artifact"
        elif expected_policy_id in explicit_policy_ids:
            try:
                from huggingface_hub import hf_hub_download

                selected_path = Path(
                    hf_hub_download(
                        repo_id=model_reference,
                        filename=PROTECTION_POLICY_FILENAME,
                        revision=revision,
                        local_files_only=local_files_only,
                    )
                ).resolve()
                source = "hub_model_artifact"
            except Exception as error:
                raise ProtectionPolicyError(
                    "model declares an explicit policy but its exact policy artifact could not be loaded"
                ) from error
    if selected_path is None:
        if expected_policy_id in explicit_policy_ids:
            raise ProtectionPolicyError("model declares an explicit policy but has no verified policy artifact")
        return legacy_policy_evidence(), None
    policy, payload = _load_policy_bytes(selected_path, tokenizer)
    if expected_policy_id is None and policy["policy_id"] != LEGACY_POLICY_ID:
        raise ProtectionPolicyError("model config does not bind the supplied new protection policy")
    if expected_policy_id is not None and expected_policy_id != policy["policy_id"]:
        raise ProtectionPolicyError("model config and protection artifact policy IDs differ")
    if expected_policy_sha256 is not None and expected_policy_sha256 != policy["policy_sha256"]:
        raise ProtectionPolicyError("model config and protection artifact policy hashes differ")
    binding = {
        "source": source,
        "path": str(selected_path),
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
    }
    return policy, binding


def _policy_span_protector(
    policy: Mapping[str, object],
    *,
    product: bool,
) -> Callable[[str], ProtectedText]:
    if policy["policy_id"] == LEGACY_POLICY_ID:
        return protect_product_spans if product else protect_spans
    return lambda text: _protect_with_validated_policy(text, policy)


def polish_transcript(
    transcript: str,
    *,
    tokenizer: Any,
    model: Any,
    max_new_tokens: int = 512,
    validator: Callable[[str], bool] | None = None,
    protection_policy: Mapping[str, object] | None = None,
) -> str:
    """Polish through the product safety layer; any unsafe result returns the source."""

    predictions: list[PredictionResult] = []
    policy = (
        validate_protection_policy_evidence(protection_policy)
        if protection_policy is not None
        else legacy_policy_evidence()
    )
    product_protector = _policy_span_protector(policy, product=True)

    def predict(value: str) -> PredictionResult:
        prediction = generate_prediction(
            value,
            tokenizer=tokenizer,
            model=model,
            max_new_tokens=max_new_tokens,
            span_protector=product_protector,
        )
        predictions.append(prediction)
        return prediction

    try:
        result = run_product_inference(
            transcript,
            predictor=predict,
        )
        if validator is not None and any(not validator(prediction.prediction_sst) for prediction in predictions):
            raise ValueError("polishing output did not satisfy the validator")
        return result.output
    except (TypeError, ValueError):
        return transcript


def load_transformers_runtime(
    *,
    model: str,
    revision: str | None = None,
    device: str = "cuda",
    quantization: str = "none",
    local_files_only: bool = False,
) -> tuple[Any, Any]:
    """Load a local or Hub checkpoint without silently falling back to CPU."""

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    common: dict[str, Any] = {
        "revision": revision,
        "local_files_only": local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(
        model,
        **common,
        # Gemma's saved tokenizer contains a Split pre-tokenizer, not the
        # mutable sequence assumed by Transformers' Mistral compatibility fix.
        fix_mistral_regex=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model_kwargs: dict[str, Any] = {
        **common,
        "attn_implementation": "sdpa",
        "low_cpu_mem_usage": True,
    }
    if quantization in {"int8", "int4_nf4"}:
        artifact = Path(model).resolve()
        if revision is not None or not local_files_only or not artifact.is_dir():
            raise ValueError(
                "quantized inference requires a local saved artifact, "
                "local_files_only=true, and no Hub revision override"
            )
        verify_saved_bitsandbytes_config(artifact, quantization)
        model_kwargs["device_map"] = "auto"
    elif quantization == "none":
        model_kwargs["dtype"] = torch.bfloat16 if device == "cuda" else torch.float32
    else:
        raise ValueError(f"unsupported quantization mode: {quantization}")
    loaded_model = AutoModelForCausalLM.from_pretrained(model, **model_kwargs)
    if quantization == "none":
        loaded_model = loaded_model.to(device)
    else:
        loaded_flag = "is_loaded_in_8bit" if quantization == "int8" else "is_loaded_in_4bit"
        if not bool(getattr(loaded_model, loaded_flag, False)):
            raise RuntimeError(f"saved artifact reload did not activate expected {quantization} quantization")
    loaded_model.eval()
    loaded_model.generation_config.top_p = None
    loaded_model.generation_config.top_k = None
    return tokenizer, loaded_model


def _jsonl_payload(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in records
    ).encode("utf-8")


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_durable_replace(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked inference path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
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
        _fsync_parent(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"inference artifact must not be a symlink: {path}")
    if path.exists():
        _, existing = _stable_bytes(path, "existing inference artifact")
        if existing != payload:
            raise ValueError(f"refusing to overwrite different inference artifact: {path}")
        return
    _atomic_durable_replace(path, payload)


@cache
def _resume_journal_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_RESUME_JOURNAL_SCHEMA.read_text(encoding="utf-8")))


@cache
def _prediction_manifest_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_PREDICTION_MANIFEST_SCHEMA.read_text(encoding="utf-8")))


def validate_batch_prediction_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a current manifest; legacy manifests without batch_size stay readable elsewhere."""

    try:
        materialized = json.loads(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"prediction manifest is not finite JSON: {error}") from error
    errors = sorted(
        _prediction_manifest_validator().iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise ValueError(f"prediction manifest schema failed at {path}: {errors[0].message}")
    count = materialized["record_count"]
    if (
        materialized["valid_sst_count"] > count
        or materialized["restoration_error_count"] > count
        or materialized["valid_sst_count"] + materialized["restoration_error_count"] > count
    ):
        raise ValueError("prediction manifest case counts are inconsistent")
    return materialized


def _entry_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": entry["index"],
        "case_id": entry["case_id"],
        "prediction": entry["prediction"],
        "evidence": entry["evidence"],
    }


def validate_batch_resume_journal(
    journal: Mapping[str, Any],
    *,
    references: Sequence[Mapping[str, Any]],
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate schema, exact bindings, and an uninterrupted case-ID prefix."""

    try:
        materialized = json.loads(
            json.dumps(
                journal,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"resume journal is not finite JSON: {error}") from error
    errors = sorted(
        _resume_journal_validator().iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise ValueError(f"resume journal schema failed at {path}: {errors[0].message}")
    expected_binding_copy = json.loads(
        json.dumps(expected_binding, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    if materialized["binding"] != expected_binding_copy:
        raise ValueError("resume journal binding differs from the current batch request")
    expected_binding_hash = _prefixed_sha256(_canonical_json(expected_binding_copy))
    if materialized["binding_sha256"] != expected_binding_hash:
        raise ValueError("resume journal binding hash is inconsistent")
    model_binding = dict(materialized["binding"]["model"])
    model_identity_sha256 = model_binding.pop("identity_sha256")
    if model_identity_sha256 != _prefixed_sha256(_canonical_json(model_binding)):
        raise ValueError("resume journal model identity hash is inconsistent")
    validated_references = tuple(validate_evaluation_reference(reference) for reference in references)
    case_ids = [reference["case_id"] for reference in validated_references]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("current resume references contain duplicate case IDs")
    if materialized["binding"]["reference_count"] != len(case_ids) or materialized["binding"][
        "reference_case_ids_sha256"
    ] != _prefixed_sha256(_canonical_json(case_ids)):
        raise ValueError("resume journal reference identity is inconsistent")
    entries = materialized["entries"]
    if len(entries) > len(case_ids):
        raise ValueError("resume journal has more entries than frozen references")
    seen: set[str] = set()
    for expected_index, entry in enumerate(entries):
        expected_case_id = case_ids[expected_index]
        if (
            entry["index"] != expected_index
            or entry["case_id"] != expected_case_id
            or entry["prediction"]["case_id"] != expected_case_id
            or entry["evidence"]["case_id"] != expected_case_id
        ):
            raise ValueError("resume journal entries must be the exact ordered reference prefix")
        if entry["case_id"] in seen:
            raise ValueError("resume journal contains a duplicate case ID")
        seen.add(entry["case_id"])
        validate_candidate_prediction(entry["prediction"])
        evidence = entry["evidence"]
        generation_seconds = evidence["generation_seconds"]
        if (
            isinstance(generation_seconds, bool)
            or not isinstance(generation_seconds, int | float)
            or not math.isfinite(generation_seconds)
            or generation_seconds < 0
        ):
            raise ValueError("resume journal generation_seconds must be finite")
        for field in ("input_tokens", "output_tokens"):
            value = evidence[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"resume journal {field} must be non-negative")
        if entry["entry_sha256"] != _prefixed_sha256(_canonical_json(_entry_payload(entry))):
            raise ValueError(f"resume journal entry hash is inconsistent at {expected_case_id}")
    return materialized


def _batch_manifest(
    predictions: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    candidate: str,
    batch_size: int,
) -> tuple[bytes, dict[str, Any]]:
    if not predictions:
        raise ValueError("cannot generate an empty prediction package")
    payload = _jsonl_payload(predictions)
    manifest = {
        "schema_version": 1,
        "candidate": candidate,
        "record_count": len(predictions),
        "valid_sst_count": sum(bool(item["valid_sst"]) for item in evidence),
        "restoration_error_count": sum(bool(item["restoration_error"]) for item in evidence),
        "prediction_sha256": hashlib.sha256(payload).hexdigest(),
        "total_generation_seconds": sum(float(item["generation_seconds"]) for item in evidence),
        "total_input_tokens": sum(int(item["input_tokens"]) for item in evidence),
        "total_output_tokens": sum(int(item["output_tokens"]) for item in evidence),
        "batch_size": batch_size,
    }
    return payload, validate_batch_prediction_manifest(manifest)


def _predict_reference_batch(
    sources: Sequence[str],
    *,
    predictor: Callable[[str], PredictionResult],
    batch_predictor: Callable[[Sequence[str]], Sequence[PredictionResult]] | None,
    batch_size: int,
) -> tuple[PredictionResult, ...]:
    if batch_size == 1:
        results = tuple(predictor(source) for source in sources)
    else:
        if batch_predictor is None:
            raise ValueError("batch_size greater than one requires a batch predictor")
        results = tuple(batch_predictor(sources))
    if len(results) != len(sources):
        raise ValueError("batch predictor returned the wrong number of results")
    if any(not isinstance(result, PredictionResult) for result in results):
        raise TypeError("batch predictor returned an invalid result")
    return results


def _journal_entry(
    *,
    index: int,
    case_id: str,
    result: PredictionResult,
) -> dict[str, Any]:
    prediction = {
        "schema_version": 1,
        "case_id": case_id,
        "prediction_sst": result.prediction_sst,
    }
    validate_candidate_prediction(prediction)
    evidence = {
        "case_id": case_id,
        "valid_sst": result.valid_sst,
        "restoration_error": result.restoration_error is not None,
        "generation_seconds": result.generation_seconds,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    entry: dict[str, Any] = {
        "index": index,
        "case_id": case_id,
        "prediction": prediction,
        "evidence": evidence,
    }
    entry["entry_sha256"] = _prefixed_sha256(_canonical_json(entry))
    return entry


def _write_resume_journal(path: Path, journal: Mapping[str, Any]) -> None:
    _atomic_durable_replace(path, _canonical_json(journal))


def _load_resume_journal(
    path: Path,
    *,
    references: Sequence[Mapping[str, Any]],
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    _, payload = _stable_bytes(path, "batch inference resume journal")
    journal = _strict_json_object(payload, "batch inference resume journal")
    if payload != _canonical_json(journal):
        raise ValueError("batch inference resume journal is not canonical JSON")
    return validate_batch_resume_journal(
        journal,
        references=references,
        expected_binding=expected_binding,
    )


def write_resumable_batch_predictions(
    references: Sequence[Mapping[str, Any]],
    predictions_destination: str | Path,
    manifest_destination: str | Path,
    *,
    predictor: Callable[[str], PredictionResult],
    batch_predictor: Callable[[Sequence[str]], Sequence[PredictionResult]] | None = None,
    batch_size: int = 1,
    candidate: str,
    resume_journal: str | Path,
    resume_binding: Mapping[str, Any],
    manifest_fields: Mapping[str, Any],
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Resume one exact prefix and atomically finalize ordinary batch artifacts."""

    if not candidate:
        raise ValueError("candidate must be non-empty")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    validated_references = tuple(validate_evaluation_reference(reference) for reference in references)
    if not validated_references:
        raise ValueError("cannot generate an empty prediction package")
    raw_paths = (
        Path(predictions_destination),
        Path(manifest_destination),
        Path(resume_journal),
    )
    if any(path.is_symlink() for path in raw_paths):
        raise ValueError("resume journal and final artifact paths must not be symlinks")
    predictions_path, manifest_path, journal_path = (path.resolve() for path in raw_paths)
    if len({predictions_path, manifest_path, journal_path}) != 3:
        raise ValueError("resume journal and final artifact paths must be distinct")
    if (
        resume_binding.get("predictions_output_path") != str(predictions_path)
        or resume_binding.get("manifest_output_path") != str(manifest_path)
        or resume_binding.get("candidate") != candidate
        or resume_binding.get("batch_size") != batch_size
    ):
        raise ValueError("resume binding does not match final artifact destinations")
    model_binding = resume_binding.get("model")
    if isinstance(model_binding, Mapping) and model_binding.get("kind") == "local_tree":
        model_root = Path(str(model_binding.get("resolved_path"))).resolve()
        for path in (predictions_path, manifest_path, journal_path):
            try:
                path.relative_to(model_root)
            except ValueError:
                continue
            raise ValueError("resumable inference artifacts must be outside the bound model tree")
    if journal_path.exists():
        journal = _load_resume_journal(
            journal_path,
            references=validated_references,
            expected_binding=resume_binding,
        )
    else:
        if predictions_path.exists() or manifest_path.exists():
            raise ValueError("resume journal is absent but a final artifact path already exists")
        journal = {
            "schema_version": 1,
            "kind": "scriber_batch_inference_resume_journal",
            "status": "in_progress",
            "binding_sha256": _prefixed_sha256(_canonical_json(resume_binding)),
            "binding": dict(resume_binding),
            "entries": [],
        }
        journal = validate_batch_resume_journal(
            journal,
            references=validated_references,
            expected_binding=resume_binding,
        )
        _write_resume_journal(journal_path, journal)
    completed = len(journal["entries"])
    if completed < len(validated_references) and (predictions_path.exists() or manifest_path.exists()):
        raise ValueError("incomplete resume journal must not coexist with a final artifact")
    total = len(validated_references)
    for start in range(completed, total, batch_size):
        batch_references = validated_references[start : start + batch_size]
        results = _predict_reference_batch(
            [reference["source_text"] for reference in batch_references],
            predictor=predictor,
            batch_predictor=batch_predictor,
            batch_size=batch_size,
        )
        batch_entries: list[dict[str, Any]] = []
        for offset, (reference, result) in enumerate(zip(batch_references, results, strict=True)):
            index = start + offset
            batch_entries.append(
                _journal_entry(
                    index=index,
                    case_id=reference["case_id"],
                    result=result,
                )
            )
        candidate_journal = {
            **journal,
            "entries": [*journal["entries"], *batch_entries],
        }
        candidate_journal = validate_batch_resume_journal(
            candidate_journal,
            references=validated_references,
            expected_binding=resume_binding,
        )
        _write_resume_journal(journal_path, candidate_journal)
        journal = candidate_journal
        for entry in batch_entries:
            if progress is not None:
                progress(int(entry["index"]) + 1, total)
    predictions = [entry["prediction"] for entry in journal["entries"]]
    evidence = [entry["evidence"] for entry in journal["entries"]]
    prediction_payload, manifest = _batch_manifest(
        predictions,
        evidence,
        candidate=candidate,
        batch_size=batch_size,
    )
    conflicting = {key for key, value in manifest_fields.items() if key in manifest and manifest[key] != value}
    if conflicting:
        raise ValueError("manifest fields conflict with batch evidence: " + ", ".join(sorted(conflicting)))
    manifest.update(dict(manifest_fields))
    _write_immutable(predictions_path, prediction_payload)
    _write_manifest(manifest_path, manifest)
    journal_path.unlink()
    _fsync_parent(journal_path)
    return manifest


def write_batch_predictions(
    references: Sequence[Mapping[str, Any]],
    destination: str | Path,
    *,
    predictor: Callable[[str], PredictionResult],
    batch_predictor: Callable[[Sequence[str]], Sequence[PredictionResult]] | None = None,
    batch_size: int = 1,
    candidate: str,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Generate exactly one immutable prediction for every frozen reference."""

    if not candidate:
        raise ValueError("candidate must be non-empty")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    predictions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    total = len(references)
    for start in range(0, total, batch_size):
        batch_references = tuple(
            validate_evaluation_reference(reference) for reference in references[start : start + batch_size]
        )
        results = _predict_reference_batch(
            [reference["source_text"] for reference in batch_references],
            predictor=predictor,
            batch_predictor=batch_predictor,
            batch_size=batch_size,
        )
        for offset, (reference, result) in enumerate(zip(batch_references, results, strict=True)):
            predictions.append(
                {
                    "schema_version": 1,
                    "case_id": reference["case_id"],
                    "prediction_sst": result.prediction_sst,
                }
            )
            evidence.append(
                {
                    "case_id": reference["case_id"],
                    "valid_sst": result.valid_sst,
                    "restoration_error": result.restoration_error is not None,
                    "generation_seconds": result.generation_seconds,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
            )
            if progress is not None:
                progress(start + offset + 1, total)
    if not predictions:
        raise ValueError("cannot generate an empty prediction package")
    payload, manifest = _batch_manifest(
        predictions,
        evidence,
        candidate=candidate,
        batch_size=batch_size,
    )
    _write_immutable(Path(destination), payload)
    return manifest


def _write_manifest(path: Path, value: Mapping[str, Any]) -> None:
    validated = validate_batch_prediction_manifest(value)
    payload = (json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_immutable(path, payload)


def _render_prediction(result: PredictionResult, output_format: str, original: str) -> str:
    if not result.valid_sst:
        return original
    document = parse_sst(result.prediction_sst)
    if output_format == "sst":
        return result.prediction_sst
    if output_format == "plain":
        return render_plain_text(document)
    if output_format == "markdown":
        return render_markdown(document)
    if output_format == "html":
        return render_html(document)
    raise ValueError(f"unsupported output format: {output_format}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--references", type=Path)
    parser.add_argument("--predictions-output", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--candidate")
    parser.add_argument("--resume-journal", type=Path)
    parser.add_argument("--output-format", choices=("plain", "sst", "markdown", "html"), default="plain")
    parser.add_argument("--diagnostics-output", type=Path)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--max-chunk-characters", type=int, default=3000)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--quantization",
        choices=("none", "int8", "int4_nf4"),
        default="none",
    )
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--protection-policy-artifact", type=Path)
    args = parser.parse_args(argv)
    if args.max_new_tokens <= 0:
        parser.error("--max-new-tokens must be positive")
    if args.max_chunk_characters <= 0:
        parser.error("--max-chunk-characters must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    is_batch = args.references is not None
    if is_batch and (args.predictions_output is None or args.manifest_output is None or not args.candidate):
        parser.error("batch mode requires --predictions-output, --manifest-output, and --candidate")
    if not is_batch and (args.predictions_output or args.manifest_output or args.candidate or args.resume_journal):
        parser.error("prediction artifact options require --references")
    if is_batch and args.diagnostics_output is not None:
        parser.error("--diagnostics-output is available only with --text")
    if not is_batch and args.batch_size != 1:
        parser.error("--batch-size greater than one requires --references")

    tokenizer, model = load_transformers_runtime(
        model=args.model,
        revision=args.revision,
        device=args.device,
        quantization=args.quantization,
        local_files_only=args.local_files_only,
    )
    protection_policy, protection_binding = load_inference_protection_policy(
        model_reference=args.model,
        tokenizer=tokenizer,
        model=model,
        revision=args.revision,
        local_files_only=args.local_files_only,
        artifact_path=args.protection_policy_artifact,
    )
    raw_span_protector = _policy_span_protector(
        protection_policy,
        product=False,
    )
    product_span_protector = _policy_span_protector(
        protection_policy,
        product=True,
    )

    def raw_predict(value: str) -> PredictionResult:
        return generate_prediction(
            value,
            tokenizer=tokenizer,
            model=model,
            max_new_tokens=args.max_new_tokens,
            span_protector=raw_span_protector,
        )

    def raw_batch_predict(values: Sequence[str]) -> list[PredictionResult]:
        return generate_batch_predictions(
            values,
            tokenizer=tokenizer,
            model=model,
            max_new_tokens=args.max_new_tokens,
            span_protector=raw_span_protector,
        )

    if not is_batch:

        def product_predict(value: str) -> PredictionResult:
            return generate_prediction(
                value,
                tokenizer=tokenizer,
                model=model,
                max_new_tokens=args.max_new_tokens,
                span_protector=product_span_protector,
            )

        warmup = warmup_product_inference(product_predict) if args.warmup else None
        result = run_product_inference(
            args.text,
            predictor=product_predict,
            output_format=args.output_format,
            max_chunk_characters=args.max_chunk_characters,
        )
        diagnostics = result.diagnostics.to_dict()
        diagnostics.update(
            {
                "requested_format": result.requested_format,
                "actual_format": result.actual_format,
                "warmup": (warmup.to_dict() if warmup is not None else {"requested": False}),
            }
        )
        diagnostics_payload = (
            json.dumps(
                diagnostics,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if args.diagnostics_output is None:
            print(diagnostics_payload, file=sys.stderr, end="")
        else:
            args.diagnostics_output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.diagnostics_output.with_suffix(args.diagnostics_output.suffix + ".tmp")
            temporary.write_text(diagnostics_payload, encoding="utf-8")
            temporary.replace(args.diagnostics_output)
        print(result.output)
        return 0
    if args.warmup:
        warmup_product_inference(raw_predict)
    references = load_evaluation_references(args.references)

    def report_progress(completed: int, total: int) -> None:
        if completed == 1 or completed % 25 == 0 or completed == total:
            print(
                f"predictions_progress candidate={args.candidate} completed={completed} total={total}",
                flush=True,
            )

    manifest_fields: dict[str, Any] = {
        "model": args.model,
        "revision": args.revision,
        "reference_sha256": hashlib.sha256(args.references.read_bytes()).hexdigest(),
        "quantization": args.quantization,
        "device": args.device,
    }
    if protection_binding is not None:
        manifest_fields["protection_policy"] = protection_binding
    if args.resume_journal is None:
        manifest = write_batch_predictions(
            references,
            args.predictions_output,
            predictor=raw_predict,
            batch_predictor=raw_batch_predict,
            batch_size=args.batch_size,
            candidate=args.candidate,
            progress=report_progress,
        )
        manifest.update(manifest_fields)
        _write_manifest(args.manifest_output, manifest)
    else:
        resume_binding = build_batch_resume_binding(
            references_path=args.references,
            references=references,
            candidate=args.candidate,
            model_reference=args.model,
            revision=args.revision,
            tokenizer=tokenizer,
            model=model,
            quantization=args.quantization,
            device=args.device,
            max_new_tokens=args.max_new_tokens,
            batch_size=args.batch_size,
            protection_policy=protection_policy,
            protection_binding=protection_binding,
            predictions_output=args.predictions_output,
            manifest_output=args.manifest_output,
        )
        manifest = write_resumable_batch_predictions(
            references,
            args.predictions_output,
            args.manifest_output,
            predictor=raw_predict,
            batch_predictor=raw_batch_predict,
            batch_size=args.batch_size,
            candidate=args.candidate,
            resume_journal=args.resume_journal,
            resume_binding=resume_binding,
            manifest_fields=manifest_fields,
            progress=report_progress,
        )
    print(
        f"predictions=complete candidate={args.candidate} records={manifest['record_count']} "
        f"valid_sst={manifest['valid_sst_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
