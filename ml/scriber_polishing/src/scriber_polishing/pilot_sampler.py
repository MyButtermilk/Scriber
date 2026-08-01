"""Streaming, deterministic pilot subsets from already frozen dataset splits."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SPLITS = ("train", "validation", "test", "challenge")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rank(seed: int, split: str, example_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"pilot-v1:{seed}:{split}:{example_id}".encode()).digest(),
        "big",
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"pilot source requires non-empty {field}")
    return value


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different pilot artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _distribution(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    domains: Counter[str] = Counter()
    profiles: Counter[str] = Counter()
    risk_tags: Counter[str] = Counter()
    for row in rows:
        domain = row.get("domain")
        if isinstance(domain, str) and domain:
            domains[domain] += 1
        profile = row.get("corruption_profile")
        if isinstance(profile, str) and profile:
            profiles[profile] += 1
        for tag in row.get("risk_tags", ()):
            if isinstance(tag, str) and tag:
                risk_tags[tag] += 1
    return {
        "domain": dict(sorted(domains.items())),
        "corruption_profile": dict(sorted(profiles.items())),
        "risk_tags": dict(sorted(risk_tags.items())),
    }


def build_pilot_subsets(
    inputs: Mapping[str, Path],
    *,
    output_root: Path,
    counts: Mapping[str, int],
    seed: int,
) -> dict[str, Any]:
    """Select per-split SHA-256 bottom-k records without loading the corpus."""

    if set(inputs) != set(_SPLITS) or set(counts) != set(_SPLITS):
        raise ValueError("pilot inputs and counts must contain every frozen split")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("pilot seed must be a non-negative integer")
    if any(isinstance(count, bool) or not isinstance(count, int) or count <= 0 for count in counts.values()):
        raise ValueError("pilot counts must be positive integers")

    selected: dict[str, list[dict[str, Any]]] = {}
    input_counts: dict[str, int] = {}
    input_hashes: dict[str, str] = {}
    parent_splits: dict[str, set[str]] = {}
    seen_ids: set[str] = set()
    for split in _SPLITS:
        path = Path(inputs[split])
        input_hashes[split] = _sha256_path(path)
        heap: list[tuple[int, str, dict[str, Any]]] = []
        input_count = 0
        with path.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    raise ValueError(f"blank pilot JSONL line in {path} at {line_number}")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"pilot JSONL row must be an object in {path} at {line_number}")
                example_id = _required_text(value, "example_id")
                if example_id in seen_ids:
                    raise ValueError(f"duplicate pilot source example_id: {example_id}")
                seen_ids.add(example_id)
                declared_split = _required_text(value, "split")
                if declared_split != split:
                    raise ValueError(
                        f"pilot input {path} declares split {declared_split!r}, expected {split!r}"
                    )
                parent = _required_text(value, "parent_canonical_document_id")
                parent_splits.setdefault(parent, set()).add(split)
                _required_text(value, "source_text")
                _required_text(value, "target_sst")
                rank = _rank(seed, split, example_id)
                entry = (-rank, example_id, value)
                if len(heap) < counts[split]:
                    heapq.heappush(heap, entry)
                elif entry[0] > heap[0][0]:
                    heapq.heapreplace(heap, entry)
                input_count += 1
        if input_count < counts[split]:
            raise ValueError(
                f"insufficient pilot rows for {split}: required {counts[split]}, found {input_count}"
            )
        input_counts[split] = input_count
        selected[split] = sorted((entry[2] for entry in heap), key=lambda row: row["example_id"])

    leakage = sorted(parent for parent, split_set in parent_splits.items() if len(split_set) != 1)
    if leakage:
        raise ValueError(f"pilot parent split leakage detected: {leakage[0]}")

    split_hashes: dict[str, str] = {}
    distributions: dict[str, dict[str, dict[str, int]]] = {}
    for split in _SPLITS:
        payload = (
            "".join(f"{_canonical_json(row)}\n" for row in selected[split])
        ).encode("utf-8")
        _write_immutable(output_root / f"{split}.jsonl", payload)
        split_hashes[split] = hashlib.sha256(payload).hexdigest()
        distributions[split] = _distribution(selected[split])
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "selection_algorithm": "split_seeded_sha256_bottom_k_v1",
        "seed": seed,
        "input_counts": input_counts,
        "input_sha256": input_hashes,
        "split_counts": dict(counts),
        "split_sha256": split_hashes,
        "distributions": distributions,
        "split_leakage": False,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
    }
    _write_immutable(
        output_root / "manifest.json",
        (_canonical_json(manifest) + "\n").encode("utf-8"),
    )
    return manifest
