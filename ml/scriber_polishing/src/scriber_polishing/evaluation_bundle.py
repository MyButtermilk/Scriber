"""Fail-closed immutable combination of validated evaluation JSONL packages."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .evaluation_io import (
    validate_candidate_prediction,
    validate_evaluation_reference,
)
from .regular_test_selector import validate_regular_test_selection_manifest

_PROJECT_ROOT = Path(__file__).parents[2]
_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "evaluation_bundle_manifest_schema.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_CASE_ID_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


class EvaluationBundleError(ValueError):
    """One evaluation input or immutable output violated the bundle contract."""


@dataclass(frozen=True, slots=True)
class EvaluationPackagePart:
    records_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class _LoadedPart:
    records_path: Path
    manifest_path: Path
    records_payload: bytes
    manifest_payload: bytes
    records: tuple[dict[str, Any], ...]
    records_sha256: str
    manifest_sha256: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise EvaluationBundleError(f"value is not canonical JSON: {error}") from error
    return (text + "\n").encode("utf-8")


def _canonical_jsonl(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(record) for record in records)


def _case_ids_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return _sha256(_canonical_json([record["case_id"] for record in records]))


def _strict_json(text: str, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise EvaluationBundleError(f"{label} contains non-finite JSON value {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationBundleError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise EvaluationBundleError(f"{label} is invalid JSON: {error.msg}") from error


def _stable_bytes(path: Path, label: str) -> tuple[Path, bytes]:
    if path.is_symlink():
        raise EvaluationBundleError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise EvaluationBundleError(f"{label} must be a regular file: {resolved}")
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
        raise EvaluationBundleError(f"{label} changed while it was read")
    return resolved, payload


def _load_jsonl(
    path: Path,
    *,
    label: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> tuple[Path, bytes, tuple[dict[str, Any], ...]]:
    resolved, payload = _stable_bytes(path, label)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvaluationBundleError(f"{label} is not UTF-8") from error
    if not text or not text.endswith("\n"):
        raise EvaluationBundleError(f"{label} must end with exactly one canonical LF")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise EvaluationBundleError(f"{label} contains blank line {line_number}")
        value = _strict_json(line, f"{label} line {line_number}")
        if not isinstance(value, Mapping):
            raise EvaluationBundleError(f"{label} line {line_number} must be an object")
        try:
            rows.append(validator(value))
        except ValueError as error:
            raise EvaluationBundleError(f"{label} line {line_number} failed schema validation: {error}") from error
    if not rows:
        raise EvaluationBundleError(f"{label} is empty")
    canonical = _canonical_jsonl(rows)
    if payload != canonical:
        raise EvaluationBundleError(f"{label} uses noncanonical JSONL framing or serialization")
    return resolved, payload, tuple(rows)


def _load_manifest(path: Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    resolved, payload = _stable_bytes(path, label)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvaluationBundleError(f"{label} is not UTF-8") from error
    value = _strict_json(text, label)
    if not isinstance(value, dict):
        raise EvaluationBundleError(f"{label} must contain an object")
    return resolved, payload, value


def _normalized_sha256(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EvaluationBundleError(f"{label} must declare SHA-256 text")
    normalized = value.removeprefix("sha256:")
    if not _SHA256.fullmatch(normalized):
        raise EvaluationBundleError(f"{label} must declare a lowercase SHA-256")
    return normalized


def _manifest_hash(manifest: Mapping[str, Any], *, prediction: bool) -> str:
    fields = ("prediction_sha256", "records_sha256") if prediction else ("records_sha256",)
    present = [_normalized_sha256(manifest[field], f"manifest {field}") for field in fields if field in manifest]
    if not present:
        expected = "prediction_sha256 or records_sha256" if prediction else "records_sha256"
        raise EvaluationBundleError(f"manifest requires {expected}")
    if len(set(present)) != 1:
        raise EvaluationBundleError("manifest declares conflicting record hashes")
    return present[0]


def _manifest_count(manifest: Mapping[str, Any], *, prediction: bool) -> int:
    fields = ("record_count", "prediction_count", "case_count") if prediction else ("record_count",)
    present = [manifest[field] for field in fields if field in manifest]
    if not present:
        raise EvaluationBundleError(f"manifest requires {fields[0]}")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in present):
        raise EvaluationBundleError("manifest record count must be a positive integer")
    if len(set(present)) != 1:
        raise EvaluationBundleError("manifest declares conflicting record counts")
    return int(present[0])


def _validate_manifest_kind(
    manifest: Mapping[str, Any],
    *,
    prediction: bool,
) -> Mapping[str, Any]:
    kind = manifest.get("kind")
    if kind is None:
        return manifest
    expected = "scriber_evaluation_prediction_bundle" if prediction else "scriber_evaluation_reference_bundle"
    if kind == expected:
        validate_evaluation_bundle_manifest(manifest)
        return manifest
    if not prediction and kind == "scriber_regular_test_selection":
        try:
            validated = validate_regular_test_selection_manifest(manifest)
        except ValueError as error:
            raise EvaluationBundleError(str(error)) from error
        return validated["output"]
    raise EvaluationBundleError(f"input manifest kind {kind!r} does not match {expected!r}")


def _verify_part(
    part: EvaluationPackagePart,
    *,
    position: int,
    prediction: bool,
    candidate: str | None = None,
) -> _LoadedPart:
    label = "prediction" if prediction else "reference"
    validator = validate_candidate_prediction if prediction else validate_evaluation_reference
    records_path, records_payload, records = _load_jsonl(
        Path(part.records_path),
        label=f"{label} input {position}",
        validator=validator,
    )
    manifest_path, manifest_payload, manifest = _load_manifest(
        Path(part.manifest_path),
        f"{label} manifest {position}",
    )
    record_binding = _validate_manifest_kind(manifest, prediction=prediction)
    actual_sha256 = _sha256(records_payload)
    if _manifest_hash(record_binding, prediction=prediction) != actual_sha256:
        raise EvaluationBundleError(f"{label} input {position} hash differs from manifest")
    if _manifest_count(record_binding, prediction=prediction) != len(records):
        raise EvaluationBundleError(f"{label} input {position} row count differs from manifest")
    declared_case_ids_sha256 = record_binding.get("case_ids_sha256")
    if declared_case_ids_sha256 is not None and _normalized_sha256(
        declared_case_ids_sha256,
        "manifest case_ids_sha256",
    ) != _case_ids_sha256(records):
        raise EvaluationBundleError(f"{label} input {position} case-ID hash differs from manifest")
    output_filename = record_binding.get("output_filename", record_binding.get("filename"))
    if output_filename is not None and output_filename != records_path.name:
        raise EvaluationBundleError(f"{label} input {position} filename differs from manifest")
    if prediction:
        declared_candidate = manifest.get("candidate")
        if not isinstance(declared_candidate, str) or not _CANDIDATE.fullmatch(declared_candidate):
            raise EvaluationBundleError(f"prediction manifest {position} requires a valid candidate")
        if declared_candidate != candidate:
            raise EvaluationBundleError(
                f"prediction manifest {position} candidate {declared_candidate!r} differs from requested {candidate!r}"
            )
    else:
        actual_split_counts = Counter(str(record["split"]) for record in records)
        declared_split_counts = record_binding.get("split_counts")
        if declared_split_counts is not None and declared_split_counts != dict(sorted(actual_split_counts.items())):
            raise EvaluationBundleError(f"reference input {position} split counts differ from manifest")
    return _LoadedPart(
        records_path=records_path,
        manifest_path=manifest_path,
        records_payload=records_payload,
        manifest_payload=manifest_payload,
        records=records,
        records_sha256=actual_sha256,
        manifest_sha256=_sha256(manifest_payload),
    )


def _input_binding(part: _LoadedPart, position: int) -> dict[str, Any]:
    return {
        "position": position,
        "records_filename": part.records_path.name,
        "records_sha256": part.records_sha256,
        "record_count": len(part.records),
        "case_ids_sha256": _case_ids_sha256(part.records),
        "manifest_filename": part.manifest_path.name,
        "manifest_sha256": part.manifest_sha256,
    }


def _scoped_case_id(namespace: str, case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return f"{namespace}_{digest[:40]}"


def scope_evaluation_package(
    part: EvaluationPackagePart,
    *,
    namespace: str,
    prediction: bool,
    destination: str | Path,
    manifest_destination: str | Path,
    candidate: str | None = None,
    split_override: str | None = None,
    add_slice_tags: Sequence[str] = (),
) -> dict[str, Any]:
    """Give one validated package deterministic, collision-safe case IDs."""

    if _CASE_ID_NAMESPACE.fullmatch(namespace) is None:
        raise EvaluationBundleError("case-ID namespace must be lowercase ASCII with underscores")
    if prediction:
        if candidate is None or _CANDIDATE.fullmatch(candidate) is None:
            raise EvaluationBundleError("scoped prediction package requires a valid candidate")
        if split_override is not None or add_slice_tags:
            raise EvaluationBundleError("prediction scoping cannot change reference metadata")
    elif candidate is not None:
        raise EvaluationBundleError("scoped reference package must not declare a candidate")
    if split_override is not None and split_override not in {
        "test",
        "challenge",
        "ai_gold_test",
        "ai_gold_challenge",
        "regression",
    }:
        raise EvaluationBundleError("scoped reference split override is invalid")
    if (
        isinstance(add_slice_tags, str | bytes)
        or any(not isinstance(tag, str) or not tag for tag in add_slice_tags)
        or len(set(add_slice_tags)) != len(add_slice_tags)
    ):
        raise EvaluationBundleError("scoped reference slice tags must be unique non-empty strings")

    loaded = _verify_part(
        part,
        position=0,
        prediction=prediction,
        candidate=candidate,
    )
    validator = validate_candidate_prediction if prediction else validate_evaluation_reference
    scoped_records: list[dict[str, Any]] = []
    source_to_scoped: dict[str, str] = {}
    for record in loaded.records:
        source_case_id = str(record["case_id"])
        scoped_case_id = _scoped_case_id(namespace, source_case_id)
        if scoped_case_id in source_to_scoped.values():
            raise EvaluationBundleError("case-ID namespace produced a collision")
        source_to_scoped[source_case_id] = scoped_case_id
        transformed = {**record, "case_id": scoped_case_id}
        if not prediction:
            if split_override is not None:
                transformed["split"] = split_override
            transformed["slice_tags"] = sorted(set(transformed["slice_tags"]).union(add_slice_tags))
        scoped_records.append(validator(transformed))
    payload = _canonical_jsonl(scoped_records)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "output_filename": Path(destination).name,
        "record_count": len(scoped_records),
        "records_sha256": _sha256(payload),
        "case_ids_sha256": _case_ids_sha256(scoped_records),
        "case_id_namespace": namespace,
        "case_id_transform": "namespace_sha256_prefix40_v1",
        "reference_metadata_transform": {
            "split_override": split_override,
            "added_slice_tags": list(add_slice_tags),
        },
        "source": _input_binding(loaded, 0),
    }
    if prediction:
        manifest.update(
            {
                "candidate": candidate,
                "prediction_sha256": _sha256(payload),
            }
        )
    else:
        manifest["split_counts"] = dict(sorted(Counter(str(record["split"]) for record in scoped_records).items()))
    manifest_payload = _canonical_json(manifest)
    destination_path = _preflight_target(
        Path(destination),
        payload,
        "scoped evaluation package",
    )
    manifest_path = _preflight_target(
        Path(manifest_destination),
        manifest_payload,
        "scoped evaluation package manifest",
    )
    if _sha256(loaded.records_path.read_bytes()) != loaded.records_sha256:
        raise EvaluationBundleError("evaluation input changed before scoped publication")
    if _sha256(loaded.manifest_path.read_bytes()) != loaded.manifest_sha256:
        raise EvaluationBundleError("evaluation manifest changed before scoped publication")
    _write_preflighted(destination_path, payload)
    _write_preflighted(manifest_path, manifest_payload)
    return manifest


def _flatten_unique(parts: Sequence[_LoadedPart], label: str) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in parts:
        for record in part.records:
            case_id = record["case_id"]
            if case_id in seen:
                raise EvaluationBundleError(f"duplicate {label} case_id: {case_id}")
            seen.add(case_id)
            rows.append(record)
    return tuple(rows)


@cache
def _manifest_validator() -> Draft202012Validator:
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validate_evaluation_bundle_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a closed output manifest and all locally derivable invariants."""

    try:
        materialized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise EvaluationBundleError(f"bundle manifest is not finite JSON: {error}") from error
    errors = sorted(
        _manifest_validator().iter_errors(materialized),
        key=lambda error: list(error.path),
    )
    if errors:
        raise EvaluationBundleError(f"bundle manifest schema failed: {errors[0].message}")
    inputs = materialized["inputs"]
    if [item["position"] for item in inputs] != list(range(len(inputs))):
        raise EvaluationBundleError("bundle manifest input positions are not contiguous")
    if sum(item["record_count"] for item in inputs) != materialized["record_count"]:
        raise EvaluationBundleError("bundle manifest input counts differ from output count")
    if materialized["kind"] == "scriber_evaluation_reference_bundle":
        if sum(materialized["split_counts"].values()) != materialized["record_count"]:
            raise EvaluationBundleError("bundle manifest split counts differ from output count")
    else:
        if materialized["prediction_sha256"] != materialized["records_sha256"]:
            raise EvaluationBundleError("prediction bundle hashes are inconsistent")
        if materialized["reference"]["record_count"] != materialized["record_count"]:
            raise EvaluationBundleError("prediction bundle reference count differs from output count")
    return materialized


def _preflight_target(path: Path, payload: bytes, label: str) -> Path:
    if path.is_symlink():
        raise EvaluationBundleError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve()
    if resolved.exists():
        if not resolved.is_file() or resolved.read_bytes() != payload:
            raise EvaluationBundleError(f"refusing to overwrite different {label}: {resolved}")
        return resolved
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    if temporary.exists():
        raise EvaluationBundleError(f"stale temporary {label}: {temporary}")
    return resolved


def _write_preflighted(path: Path, payload: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _write_bundle(
    records_payload: bytes,
    manifest: Mapping[str, Any],
    *,
    destination: Path,
    manifest_destination: Path,
    sources: Sequence[_LoadedPart],
) -> dict[str, Any]:
    validated = validate_evaluation_bundle_manifest(manifest)
    manifest_payload = _canonical_json(validated)
    destination_resolved = destination.resolve()
    manifest_resolved = manifest_destination.resolve()
    source_paths = {path for source in sources for path in (source.records_path, source.manifest_path)}
    if destination_resolved == manifest_resolved:
        raise EvaluationBundleError("bundle data and manifest destinations must differ")
    if destination_resolved in source_paths or manifest_resolved in source_paths:
        raise EvaluationBundleError("bundle destinations must differ from every input")
    output_path = _preflight_target(
        destination,
        records_payload,
        "evaluation bundle",
    )
    manifest_path = _preflight_target(
        manifest_destination,
        manifest_payload,
        "evaluation bundle manifest",
    )
    for source in sources:
        if _sha256(source.records_path.read_bytes()) != source.records_sha256:
            raise EvaluationBundleError("evaluation input changed before bundle publication")
        if _sha256(source.manifest_path.read_bytes()) != source.manifest_sha256:
            raise EvaluationBundleError("evaluation manifest changed before bundle publication")
    _write_preflighted(output_path, records_payload)
    _write_preflighted(manifest_path, manifest_payload)
    return validated


def combine_reference_packages(
    parts: Sequence[EvaluationPackagePart],
    *,
    destination: str | Path,
    manifest_destination: str | Path,
) -> dict[str, Any]:
    """Concatenate validated reference packages in declared order."""

    if not parts:
        raise EvaluationBundleError("at least one reference package is required")
    loaded = tuple(_verify_part(part, position=index, prediction=False) for index, part in enumerate(parts))
    records = _flatten_unique(loaded, "reference")
    payload = b"".join(part.records_payload for part in loaded)
    split_counts = Counter(str(record["split"]) for record in records)
    manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_reference_bundle",
        "output_filename": Path(destination).name,
        "record_count": len(records),
        "records_sha256": _sha256(payload),
        "case_ids_sha256": _case_ids_sha256(records),
        "split_counts": dict(sorted(split_counts.items())),
        "inputs": [_input_binding(part, position) for position, part in enumerate(loaded)],
    }
    return _write_bundle(
        payload,
        manifest,
        destination=Path(destination),
        manifest_destination=Path(manifest_destination),
        sources=loaded,
    )


def combine_prediction_packages(
    parts: Sequence[EvaluationPackagePart],
    *,
    candidate: str,
    references: str | Path,
    reference_manifest: str | Path,
    destination: str | Path,
    manifest_destination: str | Path,
) -> dict[str, Any]:
    """Concatenate one candidate's predictions and require exact reference coverage."""

    if not _CANDIDATE.fullmatch(candidate):
        raise EvaluationBundleError("candidate must use the evaluation candidate ID format")
    if not parts:
        raise EvaluationBundleError("at least one prediction package is required")
    reference_part = _verify_part(
        EvaluationPackagePart(Path(references), Path(reference_manifest)),
        position=0,
        prediction=False,
    )
    loaded = tuple(
        _verify_part(
            part,
            position=index,
            prediction=True,
            candidate=candidate,
        )
        for index, part in enumerate(parts)
    )
    predictions = _flatten_unique(loaded, "prediction")
    reference_ids = {record["case_id"] for record in reference_part.records}
    prediction_ids = {record["case_id"] for record in predictions}
    missing = sorted(reference_ids - prediction_ids)
    extra = sorted(prediction_ids - reference_ids)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing[0]}")
        if extra:
            details.append(f"extra={extra[0]}")
        raise EvaluationBundleError(f"prediction join coverage is incomplete or changed ({', '.join(details)})")
    payload = b"".join(part.records_payload for part in loaded)
    manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_prediction_bundle",
        "candidate": candidate,
        "output_filename": Path(destination).name,
        "record_count": len(predictions),
        "records_sha256": _sha256(payload),
        "prediction_sha256": _sha256(payload),
        "case_ids_sha256": _case_ids_sha256(predictions),
        "inputs": [_input_binding(part, position) for position, part in enumerate(loaded)],
        "reference": {
            "records_filename": reference_part.records_path.name,
            "records_sha256": reference_part.records_sha256,
            "record_count": len(reference_part.records),
            "case_ids_sha256": _case_ids_sha256(reference_part.records),
            "manifest_filename": reference_part.manifest_path.name,
            "manifest_sha256": reference_part.manifest_sha256,
        },
        "exact_case_coverage": True,
    }
    return _write_bundle(
        payload,
        manifest,
        destination=Path(destination),
        manifest_destination=Path(manifest_destination),
        sources=(*loaded, reference_part),
    )
