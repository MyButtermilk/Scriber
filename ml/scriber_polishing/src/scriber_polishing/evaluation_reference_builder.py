"""Hash-bound conversion of sealed dataset rows into closed evaluation references."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .evaluation_io import validate_evaluation_reference

_OUTPUT_TO_INPUT_SPLIT = {
    "test": "test",
    "challenge": "challenge",
    "ai_gold_test": "test",
    "ai_gold_challenge": "challenge",
    "regression": "regression",
}
_ADDRESS_MODES = {
    "formal_sie",
    "personal_du_capitalized",
    "personal_du_lowercase",
    "neutral_third_person",
    "quoted_speech",
    "mixed_correspondence",
    "ambiguous",
}


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"evaluation source record requires non-empty {field}")
    return value


def _slug(value: str, *, prefix: str | None = None) -> str:
    normalized = unicodedata.normalize("NFKD", value.replace("ß", "ss"))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")
    if prefix:
        slug = f"{prefix}:{slug}"
    if not slug or not re.fullmatch(r"[a-z][a-z0-9_:-]{1,79}", slug):
        raise ValueError(f"value cannot be represented as an evaluation slice tag: {value!r}")
    return slug


def _case_id(record: Mapping[str, Any]) -> str:
    raw = record.get("example_id", record.get("canonical_document_id"))
    if not isinstance(raw, str) or not raw:
        raise ValueError("evaluation source record requires example_id or canonical_document_id")
    value = _slug(raw)
    if len(value) > 200:
        suffix = hashlib.sha256(raw.encode()).hexdigest()[:16]
        value = f"{value[:183].rstrip('_')}_{suffix}"
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,199}", value):
        raise ValueError(f"evaluation case id is invalid after normalization: {raw!r}")
    return value


def _slice_tags(record: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for risk_tag in record.get("risk_tags", ()):
        if not isinstance(risk_tag, str) or not risk_tag:
            raise ValueError("risk_tags must contain non-empty strings")
        result.append(_slug(risk_tag))
    for field in ("domain", "document_type"):
        value = record.get(field)
        if isinstance(value, str) and value:
            result.append(_slug(value, prefix=field))
    for operation in record.get("operations", ()):
        if not isinstance(operation, str) or not operation:
            raise ValueError("operations must contain non-empty strings")
        result.append(_slug(operation, prefix="operation"))
    profile = record.get("corruption_profile")
    if isinstance(profile, str) and profile:
        result.append(_slug(profile, prefix="profile"))
    return list(dict.fromkeys(result))


def build_evaluation_references(
    records: Iterable[Mapping[str, Any]],
    *,
    output_split: str,
) -> list[dict[str, Any]]:
    """Convert immutable canonical/pair rows without exposing any extra fields."""

    expected_input_split = _OUTPUT_TO_INPUT_SPLIT.get(output_split)
    if expected_input_split is None:
        raise ValueError(f"unsupported evaluation output split: {output_split}")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        input_split = record.get("split")
        if input_split != expected_input_split:
            raise ValueError(
                f"evaluation input split must be {expected_input_split!r} for {output_split!r}, "
                f"got {input_split!r}"
            )
        case_id = _case_id(record)
        if case_id in seen:
            raise ValueError(f"duplicate evaluation case id after normalization: {case_id}")
        seen.add(case_id)
        source_text = _required_text(record, "source_text")
        target_plain_text = _required_text(record, "target_plain_text")
        reference = {
            "schema_version": 1,
            "case_id": case_id,
            "split": output_split,
            "source_text": source_text,
            "target_sst": _required_text(record, "target_sst"),
            "target_plain_text": target_plain_text,
            "target_ast": record.get("target_ast"),
            "semantic_plan": record.get("semantic_plan"),
            "protected_spans": list(record.get("protected_spans", ())),
            "slice_tags": _slice_tags(record),
            "is_identity": source_text == target_plain_text,
        }
        address_mode = record.get("address_mode")
        if address_mode is not None:
            if not isinstance(address_mode, str) or address_mode not in _ADDRESS_MODES:
                raise ValueError("evaluation source has an invalid address_mode")
            reference["address_mode"] = address_mode
        result.append(validate_evaluation_reference(reference))
    if not result:
        raise ValueError("evaluation source contains no records")
    return sorted(result, key=lambda item: item["case_id"])


def _payload(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different evaluation artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def write_evaluation_references(
    records: Sequence[Mapping[str, Any]],
    destination: str | Path,
) -> dict[str, Any]:
    """Write one immutable, schema-validated reference package."""

    validated = [validate_evaluation_reference(record) for record in records]
    if not validated:
        raise ValueError("cannot write an empty evaluation reference package")
    payload = _payload(validated)
    path = Path(destination)
    _write_immutable(path, payload)
    split_counts = Counter(str(record["split"]) for record in validated)
    return {
        "schema_version": 1,
        "record_count": len(validated),
        "split_counts": dict(sorted(split_counts.items())),
        "records_sha256": hashlib.sha256(payload).hexdigest(),
    }
