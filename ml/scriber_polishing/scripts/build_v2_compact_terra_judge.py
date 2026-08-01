"""Build the exact 100-case public Terra packet and separate private V2/V1 map."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.compact_terra_judge import (  # noqa: E402
    build_compact_terra_bundle,
    compact_terra_prompt_bytes,
    public_cases_jsonl_bytes,
    source_cases_jsonl_bytes,
)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _load_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank evaluation case line at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"evaluation case line {line_number} must be an object")
        rows.append(value)
    return tuple(rows)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-cases", type=Path, required=True)
    parser.add_argument("--evaluation-result", type=Path, required=True)
    parser.add_argument("--public-cases", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--prompt-output", type=Path, required=True)
    args = parser.parse_args()
    destinations = {args.public_cases.resolve(), args.private_mapping.resolve(), args.prompt_output.resolve()}
    inputs = {args.evaluation_cases.resolve(), args.evaluation_result.resolve()}
    if len(destinations) != 3 or len(inputs) != 2 or destinations.intersection(inputs):
        raise ValueError("input, public, private, and prompt paths must be distinct")

    result_payload = args.evaluation_result.read_bytes()
    result = json.loads(result_payload)
    if not isinstance(result, dict):
        raise ValueError("V2 evaluation result must be an object")
    if result_payload != _canonical_json_bytes(result):
        raise ValueError("V2 evaluation result must use canonical JSON")
    scope = result.get("evaluation_scope")
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_v2_final_evaluation_result"
        or result.get("status") != "complete"
        or not isinstance(scope, dict)
        or scope.get("evidence_class") != "compact_smoke_test"
        or scope.get("publication_grade") is not False
        or scope.get("total_records") != 300
    ):
        raise ValueError("V2 evaluation result is not the completed 300-case compact smoke test")
    source_binding = result.get("terra_judge_source")
    if not isinstance(source_binding, dict):
        raise ValueError("V2 evaluation result lacks the bound Terra judge source")
    relative_source = Path(str(source_binding.get("path", "")))
    if relative_source.is_absolute() or ".." in relative_source.parts or "\\" in str(source_binding.get("path", "")):
        raise ValueError("V2 evaluation Terra source path must be safe and relative")
    expected_source = (args.evaluation_result.parent / relative_source).resolve()
    if args.evaluation_cases.resolve() != expected_source:
        raise ValueError("evaluation cases path differs from the result-bound Terra source path")
    source_payload = args.evaluation_cases.read_bytes()
    source_cases = _load_jsonl(args.evaluation_cases)
    if source_payload != source_cases_jsonl_bytes(source_cases):
        raise ValueError("evaluation Terra source must use canonical sorted JSONL with LF endings")
    source_sha256 = "sha256:" + hashlib.sha256(source_payload).hexdigest()
    if source_binding.get("bytes") != len(source_payload) or source_binding.get("sha256") != source_sha256:
        raise ValueError("evaluation Terra source bytes or SHA-256 differ from the completed result")

    result_sha256 = "sha256:" + hashlib.sha256(result_payload).hexdigest()
    bundle = build_compact_terra_bundle(
        source_cases,
        evaluation_result_sha256=result_sha256,
        evaluation_source=source_binding,
    )
    _write_atomic(args.public_cases, public_cases_jsonl_bytes(bundle.public_cases))
    _write_atomic(args.private_mapping, _canonical_json_bytes(bundle.private_mapping))
    _write_atomic(args.prompt_output, compact_terra_prompt_bytes())
    summary = {
        "schema_version": 1,
        "kind": "scriber_v2_compact_terra_build_summary",
        "selected_case_count": bundle.private_mapping["selected_case_count"],
        "judge_model_family": bundle.private_mapping["judge_model_family"],
        "prompt_sha256": bundle.private_mapping["prompt_sha256"],
        "public_cases_sha256": bundle.private_mapping["public_cases_sha256"],
        "evaluation_result_sha256": bundle.private_mapping["evaluation_result_sha256"],
        "evaluation_source_sha256": bundle.private_mapping["evaluation_source"]["sha256"],
        "evidence_class": bundle.private_mapping["evidence_class"],
        "publication_grade": bundle.private_mapping["publication_grade"],
    }
    sys.stdout.buffer.write(_canonical_json_bytes(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
