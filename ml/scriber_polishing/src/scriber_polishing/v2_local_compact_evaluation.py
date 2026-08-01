"""Fail-closed Windows execution of the hash-bound V2 compact evaluation packet."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from .curriculum import canonical_json_bytes
from .hf_v2_final_evaluation import (
    V1_MODEL_TREE_SHA256,
    V2_COMPACT_RECORDS_PER_SUITE,
    V2FinalEvaluationError,
    verify_v2_final_evaluation_packet,
    verify_v2_final_evaluation_result,
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEREDOC = re.compile(r"(?ms)^[ \t]*python\b[^\n]* <<'PY'\r?\n(.*?)\r?\nPY$")
_COMPLETED_MARKER = b"scriber_v2_compact_final_evaluation_complete_v1\n"
_EXPECTED_RUNTIME_VERSIONS = {
    "accelerate": "1.14.0",
    "jsonschema": "4.26.0",
    "numpy": "2.5.1",
    "PyYAML": "6.0.3",
    "safetensors": "0.8.0",
    "sentencepiece": "0.2.2",
    "transformers": "4.57.6",
}
_EXPECTED_TORCH_VERSION = "2.11.0+cu128"
_INFERENCE_MODEL_FILES = frozenset(
    {
        "added_tokens.json",
        "chat_template.jinja",
        "config.json",
        "generation_config.json",
        "model.safetensors",
        "scriber-protection-policy.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "tokenizer.model",
    }
)
_SUITES = {
    "regular_test": "regular_test_1000.references.jsonl",
    "critical_regression": "critical_regression_as_challenge.references.jsonl",
    "ai_gold": "ai_gold_automatic_test_challenge.references.jsonl",
}


class V2LocalCompactEvaluationError(RuntimeError):
    """Raised when the local compact evaluation cannot remain fail-closed."""


@dataclass(frozen=True, slots=True)
class LocalEvaluationReservation:
    """Exclusive sibling work and final result paths."""

    output_root: Path
    work_root: Path


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise V2LocalCompactEvaluationError(f"{label} is missing or not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise V2LocalCompactEvaluationError(f"cannot read {label}: {error}") from error


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular_bytes(path, label))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2LocalCompactEvaluationError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise V2LocalCompactEvaluationError(f"{label} must contain one JSON object")
    return value


def extract_runner_python_stage(runner: str, sentinel: str) -> str:
    """Extract exactly one compiled Python stage from an immutable packet runner."""

    matches = [source for source in _HEREDOC.findall(runner) if sentinel in source]
    if len(matches) != 1:
        raise V2LocalCompactEvaluationError(
            f"packet runner must contain exactly one Python stage for {sentinel!r}"
        )
    source = matches[0]
    try:
        compile(source, f"<packet-stage:{sentinel}>", "exec")
    except SyntaxError as error:
        raise V2LocalCompactEvaluationError(f"packet runner stage does not compile: {sentinel}") from error
    return source


def canonicalize_packet_json_file(path: str | Path) -> dict[str, int | str]:
    """Rewrite one packet-produced JSON object as canonical UTF-8 bytes with one LF."""

    candidate = Path(path).expanduser().resolve()
    value = _json_object(candidate, "packet-produced JSON artifact")
    payload = canonical_json_bytes(value)
    try:
        candidate.write_bytes(payload)
    except OSError as error:
        raise V2LocalCompactEvaluationError(
            f"cannot canonicalize packet-produced JSON artifact {candidate}: {error}"
        ) from error
    return {"bytes": len(payload), "sha256": _sha256(payload)}


def reserve_local_evaluation_paths(output_root: str | Path) -> LocalEvaluationReservation:
    """Atomically reserve a new sibling work directory without touching an existing result."""

    raw_output = Path(output_root).expanduser()
    if not raw_output.name or raw_output == Path(raw_output.anchor):
        raise V2LocalCompactEvaluationError("output must name a dedicated result directory")
    output = raw_output.resolve()
    if output.exists() or output.is_symlink():
        raise V2LocalCompactEvaluationError(f"output already exists: {output}")
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir():
        raise V2LocalCompactEvaluationError(f"output parent must be an existing regular directory: {parent}")
    work = output.with_name(output.name + ".work")
    if work.exists() or work.is_symlink():
        raise V2LocalCompactEvaluationError(f"work directory already exists: {work}")
    try:
        work.mkdir()
    except FileExistsError as error:
        raise V2LocalCompactEvaluationError(f"work directory already exists: {work}") from error
    except OSError as error:
        raise V2LocalCompactEvaluationError(f"cannot reserve work directory {work}: {error}") from error
    return LocalEvaluationReservation(output_root=output, work_root=work.resolve())


def _checkpoint_tree(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    seen: set[str] = set()
    for item in files:
        path = item.get("path")
        byte_count = item.get("bytes")
        sha256 = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path in seen
            or "/" in path
            or "\\" in path
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or not isinstance(sha256, str)
            or _HASH.fullmatch(sha256) is None
        ):
            raise V2LocalCompactEvaluationError("base-model checkpoint inventory contains an invalid record")
        seen.add(path)
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(byte_count).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _verify_local_model(
    *, packet_root: Path, model_root: Path, base_model_metadata_root: Path
) -> dict[str, Any]:
    if model_root.is_symlink() or not model_root.is_dir():
        raise V2LocalCompactEvaluationError(f"local V2 model directory is invalid: {model_root}")
    entries = list(model_root.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise V2LocalCompactEvaluationError("local V2 model may contain only regular top-level files")
    observed_names = {entry.name for entry in entries}
    if observed_names != _INFERENCE_MODEL_FILES:
        missing = sorted(_INFERENCE_MODEL_FILES - observed_names)
        extra = sorted(observed_names - _INFERENCE_MODEL_FILES)
        raise V2LocalCompactEvaluationError(
            f"local V2 inference model file set differs (missing={missing}, extra={extra})"
        )

    binding = _json_object(
        packet_root / "inputs/v2-postflight-model-binding.json",
        "packet V2 postflight model binding",
    )
    model_binding = binding.get("model")
    if not isinstance(model_binding, dict):
        raise V2LocalCompactEvaluationError("packet V2 model binding is missing")
    weight_files = model_binding.get("weight_files")
    if not isinstance(weight_files, list) or len(weight_files) != 1 or not isinstance(weight_files[0], dict):
        raise V2LocalCompactEvaluationError("packet V2 weight binding is invalid")
    weight = weight_files[0]
    if weight.get("path") != "model.safetensors":
        raise V2LocalCompactEvaluationError("packet V2 weight path differs")

    base_inventory = _json_object(
        base_model_metadata_root / "checkpoint-inventory.json",
        "base-model checkpoint inventory",
    )
    base_files = base_inventory.get("files")
    if not isinstance(base_files, list) or any(not isinstance(item, dict) for item in base_files):
        raise V2LocalCompactEvaluationError("base-model checkpoint inventory files are invalid")
    if (
        base_inventory.get("tree_sha256") != V1_MODEL_TREE_SHA256
        or _checkpoint_tree(base_files) != V1_MODEL_TREE_SHA256
    ):
        raise V2LocalCompactEvaluationError("base-model checkpoint inventory tree differs")
    base_by_path = {str(item["path"]): item for item in base_files}

    verified: dict[str, dict[str, int | str]] = {}
    for name in sorted(_INFERENCE_MODEL_FILES):
        payload = _regular_bytes(model_root / name, f"local V2 model file {name}")
        actual = {"bytes": len(payload), "sha256": _sha256(payload)}
        if name == "model.safetensors":
            expected = {"bytes": weight.get("bytes"), "sha256": weight.get("sha256")}
        else:
            base_record = base_by_path.get(name)
            if base_record is None:
                raise V2LocalCompactEvaluationError(f"base-model metadata does not bind {name}")
            expected = {"bytes": base_record.get("bytes"), "sha256": base_record.get("sha256")}
        if actual != expected:
            raise V2LocalCompactEvaluationError(f"local V2 model file binding differs: {name}")
        verified[name] = actual
    if verified["scriber-protection-policy.json"]["sha256"] != model_binding.get(
        "protection_policy_sha256"
    ):
        raise V2LocalCompactEvaluationError("local V2 protection policy differs from postflight binding")
    return {
        "weight_bytes": verified["model.safetensors"]["bytes"],
        "weight_sha256": verified["model.safetensors"]["sha256"],
        "policy_sha256": verified["scriber-protection-policy.json"]["sha256"],
        "inference_file_count": len(verified),
    }


def _verify_runtime() -> dict[str, Any]:
    versions = {name: metadata.version(name) for name in _EXPECTED_RUNTIME_VERSIONS}
    if versions != _EXPECTED_RUNTIME_VERSIONS:
        raise V2LocalCompactEvaluationError(
            f"local evaluation dependency versions differ: expected={_EXPECTED_RUNTIME_VERSIONS}, actual={versions}"
        )
    try:
        import torch
    except ImportError as error:
        raise V2LocalCompactEvaluationError("PyTorch is not installed in the local evaluation runtime") from error
    if torch.__version__ != _EXPECTED_TORCH_VERSION:
        raise V2LocalCompactEvaluationError(
            f"local PyTorch version differs: expected={_EXPECTED_TORCH_VERSION}, actual={torch.__version__}"
        )
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise V2LocalCompactEvaluationError("local CUDA BF16 runtime is unavailable")
    if torch.cuda.device_count() < 1:
        raise V2LocalCompactEvaluationError("local CUDA runtime reports no device")
    return {
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "device_count": torch.cuda.device_count(),
        "device_name": torch.cuda.get_device_name(0),
        "bf16_supported": True,
        "dependencies": versions,
    }


def _v1_suite_lane(v1_evaluation_root: Path, suite: str) -> Path:
    candidates = (
        v1_evaluation_root / "final" / suite,
        v1_evaluation_root / suite,
    )
    present = [candidate for candidate in candidates if candidate.is_dir() and not candidate.is_symlink()]
    if len(present) != 1:
        raise V2LocalCompactEvaluationError(
            f"completed V1 suite lane must exist in exactly one supported layout: {suite}"
        )
    return present[0]


def _verify_v1_evaluation(packet_root: Path, v1_evaluation_root: Path) -> dict[str, Any]:
    baseline = _json_object(
        packet_root / "baseline/v1-final-evaluation-result.json",
        "packet V1 final-evaluation binding",
    )
    reports = baseline.get("reports")
    if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
        raise V2LocalCompactEvaluationError("packet V1 report bindings are invalid")
    report_by_path = {str(item.get("path")): item for item in reports}
    references = _json_object(packet_root / "package-manifest.json", "packet manifest").get("references")
    if not isinstance(references, dict):
        raise V2LocalCompactEvaluationError("packet compact reference bindings are missing")
    prediction_bytes = 0
    for suite in _SUITES:
        lane = _v1_suite_lane(v1_evaluation_root, suite)
        report_relative = f"final/{suite}/evaluation.json"
        bound_report = report_by_path.get(report_relative)
        if bound_report is None:
            raise V2LocalCompactEvaluationError(f"packet V1 report binding is missing: {suite}")
        report_payload = _regular_bytes(lane / "evaluation.json", f"completed V1 report {suite}")
        if {
            "bytes": len(report_payload),
            "sha256": _sha256(report_payload),
        } != {
            "bytes": bound_report.get("bytes"),
            "sha256": bound_report.get("sha256"),
        }:
            raise V2LocalCompactEvaluationError(f"completed V1 report differs: {suite}")
        prediction_path = lane / "predictions.jsonl"
        prediction_payload = _regular_bytes(prediction_path, f"completed V1 predictions {suite}")
        prediction_manifest = _json_object(
            lane / "predictions.manifest.json", f"completed V1 prediction manifest {suite}"
        )
        reference_binding = references.get(suite)
        expected_count = (
            reference_binding.get("source_record_count") if isinstance(reference_binding, dict) else None
        )
        if (
            prediction_manifest.get("candidate") != "final"
            or prediction_manifest.get("record_count") != expected_count
            or prediction_manifest.get("prediction_sha256") != hashlib.sha256(prediction_payload).hexdigest()
        ):
            raise V2LocalCompactEvaluationError(f"completed V1 prediction binding differs: {suite}")
        prediction_bytes += len(prediction_payload)
    return {"suite_count": len(_SUITES), "prediction_bytes": prediction_bytes}


def _run_command(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    label: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> int:
    print(f"local_v2_evaluation={label}", flush=True)
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
    )
    if completed.returncode not in allowed_returncodes:
        raise V2LocalCompactEvaluationError(
            f"{label} failed with exit code {completed.returncode}"
        )
    return completed.returncode


def _write_stage(work_root: Path, name: str, source: str) -> Path:
    stage_root = work_root / "packet-stages"
    stage_root.mkdir(exist_ok=True)
    path = stage_root / f"{name}.py"
    path.write_text(source + "\n", encoding="utf-8", newline="\n")
    return path


def _recovery_immutable_binding(work_root: Path) -> dict[str, Any]:
    roots = (
        work_root / "materialized",
        work_root / "final" / "v1_reused",
        work_root / "final" / "v2",
    )
    records: list[dict[str, int | str]] = []
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise V2LocalCompactEvaluationError(f"local recovery input directory is invalid: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            payload = _regular_bytes(path, "local recovery immutable input")
            records.append(
                {
                    "path": path.relative_to(work_root).as_posix(),
                    "bytes": len(payload),
                    "sha256": _sha256(payload),
                }
            )
    if not records:
        raise V2LocalCompactEvaluationError("local recovery has no immutable inputs")
    return {
        "file_count": len(records),
        "tree_sha256": _sha256(canonical_json_bytes(records)),
    }


def _evaluate_predictions(
    *,
    evaluator_root: Path,
    environment: dict[str, str],
    references: Path,
    predictions: Path,
    candidate: str,
    lane: Path,
) -> None:
    lane.mkdir(parents=True, exist_ok=False)
    code = _run_command(
        [
            sys.executable,
            str(evaluator_root / "scripts/evaluate_predictions.py"),
            "--references",
            str(references),
            "--predictions",
            str(predictions),
            "--candidate",
            candidate,
            "--gate-candidate",
            "final",
            "--config",
            str(evaluator_root / "configs/evaluation.yaml"),
            "--output",
            str(lane / "evaluation.json"),
        ],
        cwd=evaluator_root,
        environment=environment,
        label=f"evaluate-{candidate}-{lane.name}",
        allowed_returncodes=frozenset({0, 2}),
    )
    (lane / "evaluation-exit.txt").write_text(str(code), encoding="ascii", newline="")


def run_local_v2_compact_evaluation(
    *,
    packet_root: str | Path,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
    model_root: str | Path,
    base_model_metadata_root: str | Path,
    v1_evaluation_root: str | Path,
    output_root: str | Path,
    batch_size: int = 8,
) -> dict[str, Any]:
    """Run the exact three-suite packet locally and publish only a verified complete result."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise V2LocalCompactEvaluationError("batch_size must be a positive integer")
    packet = Path(packet_root).expanduser().resolve()
    model = Path(model_root).expanduser().resolve()
    base_model = Path(base_model_metadata_root).expanduser().resolve()
    v1_root = Path(v1_evaluation_root).expanduser().resolve()
    try:
        packet_verification = verify_v2_final_evaluation_packet(
            packet,
            expected_packet_tree_sha256=expected_packet_tree_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_launch_contract_sha256=expected_launch_contract_sha256,
        )
    except V2FinalEvaluationError as error:
        raise V2LocalCompactEvaluationError(f"packet verification failed: {error}") from error
    model_verification = _verify_local_model(
        packet_root=packet,
        model_root=model,
        base_model_metadata_root=base_model,
    )
    v1_verification = _verify_v1_evaluation(packet, v1_root)
    runtime = _verify_runtime()
    reservation = reserve_local_evaluation_paths(output_root)
    work = reservation.work_root / "materialized"
    staging_result = reservation.work_root / "final"
    work.mkdir()
    staging_result.mkdir()

    runner = _regular_bytes(packet / "run-v2-final-evaluation.sh", "packet runner").decode("utf-8")
    selection_stage = _write_stage(
        reservation.work_root,
        "materialize-selection",
        extract_runner_python_stage(runner, "prediction_manifest.get"),
    )
    combined_stage = _write_stage(
        reservation.work_root,
        "materialize-combined",
        extract_runner_python_stage(runner, "combined case coverage differs"),
    )
    final_stage = _write_stage(
        reservation.work_root,
        "materialize-final-result",
        extract_runner_python_stage(runner, "Terra source count differs"),
    )
    evaluator = packet / "evaluator"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONPATH": str(evaluator / "src"),
        }
    )

    for suite, reference_name in _SUITES.items():
        compact_references = work / f"{suite}.references.jsonl"
        compact_v1_predictions = work / f"{suite}.v1-predictions.jsonl"
        _run_command(
            [
                sys.executable,
                str(selection_stage),
                suite,
                str(packet / "references" / reference_name),
                str(_v1_suite_lane(v1_root, suite) / "predictions.jsonl"),
                str(compact_references),
                str(compact_v1_predictions),
                str(staging_result / f"{suite}.selection.json"),
            ],
            cwd=evaluator,
            environment=environment,
            label=f"select-{suite}-{V2_COMPACT_RECORDS_PER_SUITE}",
        )
        canonicalize_packet_json_file(staging_result / f"{suite}.selection.json")
        v2_lane = staging_result / "v2" / suite
        v2_lane.mkdir(parents=True, exist_ok=False)
        _run_command(
            [
                sys.executable,
                "-m",
                "scriber_polishing.inference",
                "--model",
                str(model),
                "--references",
                str(compact_references),
                "--predictions-output",
                str(v2_lane / "predictions.jsonl"),
                "--manifest-output",
                str(v2_lane / "predictions.manifest.json"),
                "--candidate",
                "v2",
                "--batch-size",
                str(batch_size),
                "--max-new-tokens",
                "512",
                "--device",
                "cuda",
                "--quantization",
                "none",
                "--protection-policy-artifact",
                str(model / "scriber-protection-policy.json"),
                "--local-files-only",
            ],
            cwd=evaluator,
            environment=environment,
            label=f"infer-v2-{suite}-{V2_COMPACT_RECORDS_PER_SUITE}",
        )
        _evaluate_predictions(
            evaluator_root=evaluator,
            environment=environment,
            references=compact_references,
            predictions=v2_lane / "predictions.jsonl",
            candidate="v2",
            lane=v2_lane / "evaluation-lane",
        )
        evaluation_lane = v2_lane / "evaluation-lane"
        (evaluation_lane / "evaluation.json").replace(v2_lane / "evaluation.json")
        (evaluation_lane / "evaluation-exit.txt").replace(v2_lane / "evaluation-exit.txt")
        evaluation_lane.rmdir()
        _evaluate_predictions(
            evaluator_root=evaluator,
            environment=environment,
            references=compact_references,
            predictions=compact_v1_predictions,
            candidate="v1_reused",
            lane=staging_result / "v1_reused" / suite,
        )

    _run_command(
        [sys.executable, str(combined_stage), str(work), str(staging_result)],
        cwd=evaluator,
        environment=environment,
        label="materialize-combined-300",
    )
    _evaluate_predictions(
        evaluator_root=evaluator,
        environment=environment,
        references=work / "combined.references.jsonl",
        predictions=work / "combined.v1-predictions.jsonl",
        candidate="v1_reused",
        lane=staging_result / "v1_reused" / "combined",
    )
    _evaluate_predictions(
        evaluator_root=evaluator,
        environment=environment,
        references=work / "combined.references.jsonl",
        predictions=work / "combined.v2-predictions.jsonl",
        candidate="v2",
        lane=staging_result / "v2" / "combined",
    )
    _run_command(
        [
            sys.executable,
            str(final_stage),
            str(packet / "package-manifest.json"),
            str(staging_result),
            str(work),
        ],
        cwd=evaluator,
        environment=environment,
        label="materialize-result-and-terra-300",
    )
    canonicalize_packet_json_file(staging_result / "v2-final-evaluation-result.json")
    (staging_result / "COMPLETED").write_bytes(_COMPLETED_MARKER)
    try:
        result_verification = verify_v2_final_evaluation_result(
            packet_manifest_path=packet / "package-manifest.json",
            result_root=staging_result,
            result_path=staging_result / "v2-final-evaluation-result.json",
        )
    except V2FinalEvaluationError as error:
        raise V2LocalCompactEvaluationError(f"local result verification failed: {error}") from error

    for owned in (reservation.work_root / "packet-stages", work):
        resolved = owned.resolve()
        if resolved.parent != reservation.work_root:
            raise V2LocalCompactEvaluationError("refusing cleanup outside the reserved local work directory")
        shutil.rmtree(resolved)
    if reservation.output_root.exists() or reservation.output_root.is_symlink():
        raise V2LocalCompactEvaluationError("output appeared before atomic publication")
    os.replace(staging_result, reservation.output_root)
    reservation.work_root.rmdir()
    return {
        "schema_version": 1,
        "kind": "scriber_v2_local_compact_evaluation_execution",
        "status": "passed",
        "execution": "local_windows_cuda_bf16",
        "output_root": str(reservation.output_root),
        "batch_size": batch_size,
        "packet": packet_verification,
        "model": model_verification,
        "v1_evaluation": v1_verification,
        "runtime": runtime,
        "result": result_verification,
        "external_job_started": False,
        "external_mutation_performed": False,
    }


def recover_local_v2_compact_evaluation_postprocessing(
    *,
    packet_root: str | Path,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
    output_root: str | Path,
) -> dict[str, Any]:
    """Recover only Windows metadata from a completed local 300-prediction work tree."""

    packet = Path(packet_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    work_root = output.with_name(output.name + ".work")
    staging_result = work_root / "final"
    materialized = work_root / "materialized"
    if output.exists() or output.is_symlink():
        raise V2LocalCompactEvaluationError(f"output already exists: {output}")
    if work_root.is_symlink() or not work_root.is_dir():
        raise V2LocalCompactEvaluationError(f"completed local work directory is missing: {work_root}")
    if staging_result.is_symlink() or not staging_result.is_dir():
        raise V2LocalCompactEvaluationError("completed local staging result is missing")
    try:
        packet_verification = verify_v2_final_evaluation_packet(
            packet,
            expected_packet_tree_sha256=expected_packet_tree_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_launch_contract_sha256=expected_launch_contract_sha256,
        )
    except V2FinalEvaluationError as error:
        raise V2LocalCompactEvaluationError(f"packet verification failed: {error}") from error
    immutable_before = _recovery_immutable_binding(work_root)
    for suite in _SUITES:
        canonicalize_packet_json_file(staging_result / f"{suite}.selection.json")

    runner = _regular_bytes(packet / "run-v2-final-evaluation.sh", "packet runner").decode("utf-8")
    final_stage = _write_stage(
        work_root,
        "recover-final-result",
        extract_runner_python_stage(runner, "Terra source count differs"),
    )
    evaluator = packet / "evaluator"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONPATH": str(evaluator / "src"),
        }
    )
    _run_command(
        [
            sys.executable,
            str(final_stage),
            str(packet / "package-manifest.json"),
            str(staging_result),
            str(materialized),
        ],
        cwd=evaluator,
        environment=environment,
        label="recover-result-and-terra-metadata-only",
    )
    canonicalize_packet_json_file(staging_result / "v2-final-evaluation-result.json")
    (staging_result / "COMPLETED").write_bytes(_COMPLETED_MARKER)
    try:
        result_verification = verify_v2_final_evaluation_result(
            packet_manifest_path=packet / "package-manifest.json",
            result_root=staging_result,
            result_path=staging_result / "v2-final-evaluation-result.json",
        )
    except V2FinalEvaluationError as error:
        raise V2LocalCompactEvaluationError(f"local result verification failed: {error}") from error
    immutable_after = _recovery_immutable_binding(work_root)
    if immutable_after != immutable_before:
        raise V2LocalCompactEvaluationError("local recovery changed immutable predictions, references, or reports")

    for owned in (work_root / "packet-stages", materialized):
        resolved = owned.resolve()
        if resolved.parent != work_root:
            raise V2LocalCompactEvaluationError("refusing cleanup outside the reserved local work directory")
        shutil.rmtree(resolved)
    if output.exists() or output.is_symlink():
        raise V2LocalCompactEvaluationError("output appeared before atomic recovery publication")
    os.replace(staging_result, output)
    work_root.rmdir()
    return {
        "schema_version": 1,
        "kind": "scriber_v2_local_compact_evaluation_postprocessing_recovery",
        "status": "passed",
        "execution": "local_windows_metadata_recovery_only",
        "output_root": str(output),
        "packet": packet_verification,
        "immutable_inputs": immutable_after,
        "result": result_verification,
        "inference_restarted": False,
        "external_job_started": False,
        "external_mutation_performed": False,
    }
