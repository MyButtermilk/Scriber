"""Deterministic synthetic, non-holdout laptop benchmark case materialization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .benchmark import BenchmarkError, load_benchmark_cases
from .tokenize import POLISHING_INSTRUCTION

_SEEDS = {
    "short_email": (
        "hallo frau meier bitte senden sie den termin morgen danke und viele grüße"
    ),
    "long_business_letter": (
        "sehr geehrte damen und herren wir bestätigen den auftrag und bitten um "
        "schriftliche rückmeldung"
    ),
    "tax_law_letter": (
        "das finanzamt prüft den vorsteuerabzug nach § 15 absatz 1 ustg für 2025"
    ),
    "multipart_letter": (
        "erstens geht es um die bestellung zweitens um die lieferung drittens um "
        "die rechnung"
    ),
    "numbered_list": (
        "punkt eins unterlagen sammeln punkt zwei belege prüfen punkt drei antwort senden"
    ),
    "number_heavy": (
        "die rechnung 2026 0042 beträgt 1.234,56 euro und ist am 31.08.2026 fällig"
    ),
    "unit_heavy": (
        "die lieferung umfasst 12,5 kilogramm 3,2 meter und 18 kilowattstunden"
    ),
    "legal_citations": (
        "gemäß § 280 absatz 1 bgb und artikel 6 absatz 1 buchstabe b dsgvo"
    ),
    "address_pronouns": (
        "bitte teilen sie uns mit ob ihre unterlagen vollständig sind vielen dank"
    ),
    "rich_text_formatting": (
        "überschrift projektstatus danach fett markierter hinweis und eine eingerückte liste"
    ),
    "identity_text": (
        "dieser text soll unverändert bleiben exakt unverändert ohne ergänzung oder auslassung"
    ),
    "hard_repetition": (
        "bitte bitte den termin nicht nicht verschieben sondern wie vereinbart bestätigen"
    ),
}

_FILLERS = {
    category: (
        f" ergänzung zum synthetischen fall {category} abschnitt {{index}} "
        "bleibt sachlich geordnet und enthält keine externen daten"
    )
    for category in _SEEDS
}


def _token_count(tokenizer: Any, text: str) -> int:
    tokens = tokenizer.apply_chat_template(
        [
            {
                "role": "user",
                "content": f"{POLISHING_INSTRUCTION}{text}",
            }
        ],
        tokenize=True,
        add_generation_prompt=True,
    )
    if hasattr(tokens, "tolist"):
        tokens = tokens.tolist()
    if tokens and isinstance(tokens[0], list):
        tokens = tokens[0]
    return len(tokens)


def _fit_case(
    tokenizer: Any,
    *,
    category: str,
    target_tokens: int,
    tolerance: int,
) -> tuple[str, int]:
    current = _SEEDS[category]
    current_count = _token_count(tokenizer, current)
    best = (current, current_count)
    for index in range(1, 10_001):
        candidate = current + _FILLERS[category].format(index=index)
        candidate_count = _token_count(tokenizer, candidate)
        if abs(candidate_count - target_tokens) < abs(best[1] - target_tokens):
            best = (candidate, candidate_count)
        if candidate_count >= target_tokens:
            break
        current = candidate
        current_count = candidate_count
    else:
        raise BenchmarkError(
            f"could not reach benchmark token target for {category}/{target_tokens}"
        )
    if abs(best[1] - target_tokens) > tolerance:
        raise BenchmarkError(
            f"synthetic benchmark case {category}/{target_tokens} has "
            f"{best[1]} tokens outside tolerance {tolerance}"
        )
    return best


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def materialize_synthetic_benchmark_cases(
    *,
    tokenizer: Any,
    config: Mapping[str, object],
    config_sha256: str,
    tokenizer_tree_sha256: str,
    output_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, object]:
    """Write the complete 60-case suite without reading evaluation holdouts."""

    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise BenchmarkError("quantization config is missing benchmark policy")
    if not config_sha256.startswith("sha256:") or len(config_sha256) != 71:
        raise BenchmarkError("benchmark config hash is invalid")
    if not tokenizer_tree_sha256.startswith("sha256:") or len(
        tokenizer_tree_sha256
    ) != 71:
        raise BenchmarkError("benchmark tokenizer tree hash is invalid")
    categories = [str(value) for value in benchmark["required_case_categories"]]
    if set(categories) != set(_SEEDS):
        raise BenchmarkError(
            "synthetic benchmark templates do not cover the configured categories"
        )
    buckets = [int(value) for value in benchmark["length_buckets"]]
    tolerance = int(benchmark["bucket_tolerance_tokens"])
    rows: list[dict[str, object]] = []
    actual_token_counts: dict[str, int] = {}
    for category in categories:
        for bucket in buckets:
            text, actual_tokens = _fit_case(
                tokenizer,
                category=category,
                target_tokens=bucket,
                tolerance=tolerance,
            )
            case_id = f"synthetic_{category}_{bucket}"
            rows.append(
                {
                    "schema_version": 1,
                    "case_id": case_id,
                    "category": category,
                    "token_bucket": bucket,
                    "text": text,
                }
            )
            actual_token_counts[case_id] = actual_tokens
    rows.sort(key=lambda row: str(row["case_id"]))
    records_payload = b"".join(_canonical_bytes(row) for row in rows)
    output = Path(output_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    if output.exists() or manifest_file.exists():
        raise BenchmarkError("refusing to overwrite synthetic benchmark evidence")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    output_tmp_fd, output_tmp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(output_tmp_fd)
    manifest_tmp_fd, manifest_tmp_name = tempfile.mkstemp(
        prefix=f".{manifest_file.name}.", suffix=".tmp", dir=manifest_file.parent
    )
    os.close(manifest_tmp_fd)
    output_tmp = Path(output_tmp_name)
    manifest_tmp = Path(manifest_tmp_name)
    try:
        output_tmp.write_bytes(records_payload)
        load_benchmark_cases(output_tmp, config)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_synthetic_laptop_benchmark_suite",
            "status": "complete",
            "generation_boundary": {
                "synthetic_only": True,
                "no_test_split_read": True,
                "no_challenge_split_read": True,
                "no_holdout_content": True,
                "no_generative_api": True,
                "no_human_curation": True,
            },
            "config_sha256": config_sha256,
            "tokenizer_tree_sha256": tokenizer_tree_sha256,
            "record_count": len(rows),
            "records_sha256": _sha256(records_payload),
            "category_count": len(categories),
            "token_buckets": buckets,
            "actual_input_token_counts": dict(sorted(actual_token_counts.items())),
        }
        manifest_tmp.write_bytes(_canonical_bytes(manifest))
        os.replace(output_tmp, output)
        os.replace(manifest_tmp, manifest_file)
    except Exception:
        output_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        manifest_file.unlink(missing_ok=True)
        raise
    return manifest
