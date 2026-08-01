"""Bind one completed final model to evaluation and fresh laptop evidence.

This is the final-training counterpart to ``prepare_selected_checkpoint_publication``.
It never loads model weights, uses a GPU, or contacts the Hub.  Instead it
requires the immutable final-training report, the exact local model tree, the
tree-bound final-evaluation result, and a completed BF16 laptop benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.checkpoint_selection import (  # noqa: E402
    CheckpointSelectionError,
    canonical_json_sha256,
    publication_integration_hooks,
)
from scriber_polishing.variant_artifacts import (  # noqa: E402
    VariantArtifactError,
    verify_variant_artifact_manifest,
)

_BASE_MODEL = "google/gemma-3-270m-it"
_BASE_REVISION = "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"
_HASH_PREFIX = "sha256:"


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise CheckpointSelectionError(f"{label} must be a regular file: {path}")
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointSelectionError(f"{label} is not valid UTF-8 JSON: {path}") from error
    if not isinstance(value, Mapping):
        raise CheckpointSelectionError(f"{label} must be a JSON object: {path}")
    return dict(value), payload


def _validate_schema(value: Mapping[str, Any], filename: str, label: str) -> None:
    schema = json.loads((PROJECT_ROOT / "contracts" / filename).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise CheckpointSelectionError(f"{label} failed schema at {location}: {errors[0].message}")


def _validate_final_evaluation_result(value: Mapping[str, Any]) -> None:
    _validate_schema(
        value,
        "full_model_final_evaluation_result_schema.json",
        "final-evaluation result",
    )
    expected_paths = {
        f"{candidate}/{suite}/evaluation.json"
        for candidate in ("base_model", "final")
        for suite in ("ai_gold", "critical_regression", "regular_test")
    }
    reports = value["reports"]
    observed_paths = {row["path"] for row in reports}
    if observed_paths != expected_paths:
        raise CheckpointSelectionError(
            "final-evaluation result does not bind exactly the six required reports"
        )


def _sha256(payload: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return _HASH_PREFIX + digest.hexdigest()


def _canonical_model_sha256(files: list[dict[str, object]]) -> str:
    payload = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _HASH_PREFIX + hashlib.sha256(payload).hexdigest()


def _write_immutable(path: Path, value: Mapping[str, Any], label: str) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise CheckpointSelectionError(f"refusing to replace immutable {label}: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _verified_final_inventory(
    training: Mapping[str, Any], model_root: Path
) -> tuple[dict[str, Any], list[dict[str, object]], int]:
    final_model = training.get("final_model")
    inventory = final_model.get("inventory") if isinstance(final_model, Mapping) else None
    if not isinstance(inventory, Mapping):
        raise CheckpointSelectionError("completed training report has no final-model inventory")
    inventory_path = model_root / "checkpoint-inventory.json"
    local_inventory, _payload = _load_json(inventory_path, "local checkpoint inventory")
    if local_inventory != dict(inventory):
        raise CheckpointSelectionError("local checkpoint inventory differs from the completed training report")
    for item in inventory.get("files", []):
        if not isinstance(item, Mapping):
            raise CheckpointSelectionError("final-model inventory contains a non-object file")
        relative = Path(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise CheckpointSelectionError("final-model inventory contains an unsafe path")
        source = model_root / relative
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_size != item.get("bytes")
            or _sha256_file(source) != item.get("sha256")
        ):
            raise CheckpointSelectionError(
                f"local final-model file differs from training evidence: {relative.as_posix()}"
            )
    config = model_root / "config.json"
    weights = sorted(model_root.glob("*.safetensors"), key=lambda path: path.name)
    if not config.is_file() or not weights:
        raise CheckpointSelectionError("local final model lacks config or safetensors weights")
    model_files = [
        {
            "path": weight.relative_to(model_root).as_posix(),
            "bytes": weight.stat().st_size,
            "sha256": _sha256_file(weight),
        }
        for weight in weights
        if weight.is_file() and not weight.is_symlink()
    ]
    if not model_files:
        raise CheckpointSelectionError("local final model has no regular safetensors weights")
    selected_bytes = config.stat().st_size + sum(int(item["bytes"]) for item in model_files)
    return dict(inventory), model_files, selected_bytes


def build_publication(
    *,
    training: Mapping[str, Any],
    training_payload: bytes,
    model_root: Path,
    evaluation_result: Mapping[str, Any],
    evaluation_payload: bytes,
    model_binding: Mapping[str, Any],
    model_binding_payload: bytes,
    benchmark: Mapping[str, Any],
    benchmark_payload: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the publication handoff and its immutable source-evidence object."""

    _validate_schema(
        training,
        "completed_final_training_report_schema.json",
        "completed final-training report",
    )
    _validate_schema(benchmark, "benchmark_report_schema.json", "BF16 laptop benchmark")
    _validate_final_evaluation_result(evaluation_result)
    if (
        training.get("run_kind") != "final"
        or training.get("training_completed") is not True
        or training.get("bf16") is not True
        or training.get("base_model") != _BASE_MODEL
        or training.get("base_revision") != _BASE_REVISION
    ):
        raise CheckpointSelectionError("training report is not the completed pinned BF16 final run")
    run_fingerprint = training.get("run_fingerprint")
    checkpoint_state = training.get("checkpoint_state")
    if not isinstance(checkpoint_state, Mapping):
        raise CheckpointSelectionError("training report has no checkpoint state")
    global_step = checkpoint_state.get("global_step")
    if not isinstance(global_step, int) or isinstance(global_step, bool) or global_step < 1:
        raise CheckpointSelectionError("training report has an invalid final global step")
    checkpoint_name = f"checkpoint-{global_step}"
    surviving = checkpoint_state.get("surviving_checkpoints")
    if not isinstance(surviving, list):
        raise CheckpointSelectionError("training report has no surviving checkpoints")
    matches = [
        dict(item) for item in surviving if isinstance(item, Mapping) and item.get("checkpoint") == checkpoint_name
    ]
    if len(matches) != 1 or matches[0].get("run_fingerprint") != run_fingerprint:
        raise CheckpointSelectionError(
            "final global step is not represented by exactly one same-run checkpoint identity"
        )
    inventory, model_files, selected_bytes = _verified_final_inventory(training, model_root)
    tree_sha256 = inventory.get("tree_sha256")
    weight_hashes = {item["sha256"] for item in model_files}
    expected_binding = {
        "kind": "scriber_full_model_evaluation_binding",
        "model_tree_sha256": tree_sha256,
        "model_weight_sha256": next(iter(weight_hashes)) if len(weight_hashes) == 1 else None,
        "source_run_fingerprint": run_fingerprint,
        "training_steps_executed": 0,
    }
    if dict(model_binding) != expected_binding:
        raise CheckpointSelectionError("final-evaluation model binding differs from the final model")
    if (
        evaluation_result.get("kind") != "scriber_full_model_final_evaluation_result"
        or evaluation_result.get("model_tree_sha256") != tree_sha256
        or evaluation_result.get("training_steps_executed") != 0
    ):
        raise CheckpointSelectionError("final-evaluation result is not bound to the final model")
    runtime = benchmark.get("runtime_execution")
    protocol = benchmark.get("runtime_protocol")
    warm = protocol.get("warm_runtime") if isinstance(protocol, Mapping) else None
    cold = protocol.get("cold_loads") if isinstance(protocol, Mapping) else None
    samples = benchmark.get("samples")
    try:
        variant_manifest = verify_variant_artifact_manifest(model_root)
    except VariantArtifactError as error:
        raise CheckpointSelectionError(
            f"BF16 variant artifact is invalid: {error}"
        ) from error
    variant_artifact = variant_manifest.get("artifact")
    variant_source = variant_manifest.get("source")
    variant_manifest_path = model_root / "variant-artifact-manifest.json"
    if (
        benchmark.get("variant") != "bf16"
        or benchmark.get("training_fingerprint") != run_fingerprint
        or benchmark.get("artifact_manifest_sha256")
        != _sha256_file(variant_manifest_path)
        or not isinstance(variant_artifact, Mapping)
        or benchmark.get("artifact_tree_sha256")
        != variant_artifact.get("tree_sha256")
        or benchmark.get("artifact_size_bytes")
        != variant_artifact.get("total_bytes")
        or variant_manifest.get("schema_version") != 2
        or variant_manifest.get("variant") != "bf16"
        or variant_manifest.get("backend") != "transformers"
        or variant_manifest.get("training_fingerprint") != run_fingerprint
        or not isinstance(variant_source, Mapping)
        or variant_source.get("id") != f"training-run:{run_fingerprint}"
        or variant_source.get("tree_sha256")
        != variant_artifact.get("tree_sha256")
        or not isinstance(runtime, Mapping)
        or runtime.get("status") != "verified"
        or runtime.get("backend") != "transformers_cuda"
        or runtime.get("loaded_variant") != "bf16"
        or runtime.get("full_model_on_gpu") is not True
        or not isinstance(warm, Mapping)
        or warm.get("persistent_model_process") is not True
        or warm.get("instance_identity_stable") is not True
        or not isinstance(cold, Mapping)
        or int(cold.get("adapter_instances", 0)) < 3
        or not isinstance(samples, list)
    ):
        raise CheckpointSelectionError("BF16 benchmark is not fresh, complete CUDA evidence")
    generated_tokens = sum(int(sample.get("output_tokens", 0)) for sample in samples if isinstance(sample, Mapping))
    if generated_tokens < 1:
        raise CheckpointSelectionError("BF16 benchmark contains no generated completion tokens")
    config_sha256 = _sha256_file(model_root / "config.json")
    model_sha256 = _canonical_model_sha256(model_files)
    evidence = {
        "schema_version": 1,
        "kind": "scriber_completed_final_model_publication_evidence",
        "completed_training_report_sha256": _sha256(training_payload),
        "final_evaluation_result_sha256": _sha256(evaluation_payload),
        "final_evaluation_model_binding_sha256": _sha256(model_binding_payload),
        "bf16_laptop_benchmark_sha256": _sha256(benchmark_payload),
        "run_fingerprint": run_fingerprint,
        "checkpoint": checkpoint_name,
        "checkpoint_identity_sha256": matches[0].get("identity_sha256"),
        "model_tree_sha256": tree_sha256,
        "config_sha256": config_sha256,
        "model_sha256": model_sha256,
    }
    publication = {
        "schema_version": 2,
        "status": "ready_for_publication",
        "selection_handoff_sha256": canonical_json_sha256(evidence),
        "selected_candidate_id": checkpoint_name,
        "checkpoint": {
            "candidate_id": checkpoint_name,
            "checkpoint": checkpoint_name,
            "global_step": global_step,
            "run_fingerprint": run_fingerprint,
            "identity_sha256": matches[0].get("identity_sha256"),
            "tree_sha256": tree_sha256,
            "config_sha256": config_sha256,
            "model_sha256": model_sha256,
            "file_count": len(model_files) + 1,
            "total_bytes": selected_bytes,
            "candidate_evidence_sha256": _sha256(training_payload),
            "deterministic_automatic_report_sha256": _sha256(evaluation_payload),
            "eval_loss": training.get("eval_metrics", {}).get("eval_loss"),
        },
        "base_tokenizer": {"id": _BASE_MODEL, "revision": _BASE_REVISION},
        "fresh_reload": {
            "status": "passed",
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "tokenizer_source": "pinned_base_tokenizer",
            "model_tree_sha256": tree_sha256,
            "model_config_sha256": config_sha256,
            "model_sha256": model_sha256,
            "generated_tokens": generated_tokens,
        },
        "fresh_inventory": {
            "status": "passed",
            "tree_sha256": tree_sha256,
            "config_sha256": config_sha256,
            "model_sha256": model_sha256,
            "file_count": len(model_files) + 1,
            "total_bytes": selected_bytes,
        },
        "integration_hooks": list(publication_integration_hooks()),
    }
    _validate_schema(
        publication,
        "selected_checkpoint_publication_schema.json",
        "completed final-model publication handoff",
    )
    return publication, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completed-training-report", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--final-evaluation-result", type=Path, required=True)
    parser.add_argument("--model-binding", type=Path, required=True)
    parser.add_argument("--benchmark-report", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        training, training_payload = _load_json(args.completed_training_report, "completed final-training report")
        evaluation, evaluation_payload = _load_json(args.final_evaluation_result, "final-evaluation result")
        binding, binding_payload = _load_json(args.model_binding, "evaluation model binding")
        benchmark, benchmark_payload = _load_json(args.benchmark_report, "BF16 benchmark")
        publication, evidence = build_publication(
            training=training,
            training_payload=training_payload,
            model_root=args.model.resolve(),
            evaluation_result=evaluation,
            evaluation_payload=evaluation_payload,
            model_binding=binding,
            model_binding_payload=binding_payload,
            benchmark=benchmark,
            benchmark_payload=benchmark_payload,
        )
        _write_immutable(args.evidence_output, evidence, "publication evidence")
        _write_immutable(args.output, publication, "publication handoff")
    except (OSError, CheckpointSelectionError, ValueError) as error:
        print(f"completed_final_model_publication=failed reason={error}", file=sys.stderr)
        return 2
    print(
        "completed_final_model_publication=prepared "
        f"candidate={publication['selected_candidate_id']} "
        f"tree={publication['checkpoint']['tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
