"""Assemble immutable private-Hub upload trees from verified local evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .hub_regression import load_hub_regression_suite
from .variant_release import validate_variant_release_report

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MODEL_RUNTIME_FILES = {
    "runtime/inference.py": _PROJECT_ROOT / "src" / "scriber_polishing" / "inference.py",
    "runtime/sst_parser.py": _PROJECT_ROOT / "src" / "scriber_polishing" / "sst_parser.py",
    "runtime/renderers.py": _PROJECT_ROOT / "src" / "scriber_polishing" / "target_renderer.py",
    "sst_v1_schema.json": _PROJECT_ROOT / "contracts" / "sst_v1_schema.json",
}
_MODEL_FILES = (
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
    "variant-artifact-manifest.json",
)
_DATASET_SPLITS = {
    "train": "splits/train.jsonl",
    "validation": "splits/validation.jsonl",
    "test": "splits/test.jsonl",
    "challenge": "splits/challenge.jsonl",
}
_AI_GOLD_SPLITS = {
    "train": "sealed/training/train/ai_gold_train.sealed.jsonl",
    "validation": "sealed/evaluation/validation/ai_gold_validation.sealed.jsonl",
    "test": "sealed/holdout/test/ai_gold_test.sealed.jsonl",
    "challenge": "sealed/holdout/challenge/ai_gold_challenge.sealed.jsonl",
}
_PACKAGES = (
    "accelerate",
    "bitsandbytes",
    "datasets",
    "huggingface-hub",
    "jsonschema",
    "safetensors",
    "torch",
    "transformers",
    "trl",
)


class HubArtifactAssemblyError(RuntimeError):
    """A publication tree cannot be assembled from the supplied evidence."""


def _canonical_json(value: object, *, pretty: bool = True) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _regular_file(path: str | Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise HubArtifactAssemblyError(f"{label} must be a regular file: {path}")
    return resolved


def _directory(path: str | Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if resolved.is_symlink() or not resolved.is_dir():
        raise HubArtifactAssemblyError(f"{label} must be a real directory: {path}")
    return resolved


def _json(path: str | Path, label: str) -> dict[str, Any]:
    source = _regular_file(path, label)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HubArtifactAssemblyError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise HubArtifactAssemblyError(f"{label} must be a JSON object")
    return value


def _safe_destination(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise HubArtifactAssemblyError(f"unsafe publication path: {relative}")
    destination = root.joinpath(*pure.parts)
    if root not in destination.parents:
        raise HubArtifactAssemblyError(f"publication path escapes its root: {relative}")
    return destination


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        if (
            destination.is_file()
            and not destination.is_symlink()
            and destination.stat().st_size == source.stat().st_size
            and _sha256(destination) == _sha256(source)
        ):
            return
        raise HubArtifactAssemblyError(f"refusing to replace publication file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)


def _write_immutable(destination: Path, payload: bytes) -> None:
    if destination.exists():
        if destination.is_file() and not destination.is_symlink() and destination.read_bytes() == payload:
            return
        raise HubArtifactAssemblyError(f"refusing to replace publication file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)


def _copy_tree(source: Path, destination: Path) -> None:
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise HubArtifactAssemblyError(f"publication source tree is empty: {source}")
    for path in files:
        if path.is_symlink():
            raise HubArtifactAssemblyError(f"publication source tree contains a symlink: {path}")
        _link_or_copy(path, destination / path.relative_to(source))


def _count_jsonl(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        for raw in stream:
            if raw in {b"\n", b"\r\n"}:
                raise HubArtifactAssemblyError(f"JSONL contains a blank line: {path}")
            count += 1
    if count < 1:
        raise HubArtifactAssemblyError(f"JSONL is empty: {path}")
    return count


def _evaluation_summary(evaluation_root: Path) -> dict[str, Any]:
    suites: dict[str, Any] = {}
    for suite in ("regular_test", "critical_regression", "ai_gold"):
        path = _regular_file(evaluation_root / suite / "evaluation.json", f"{suite} evaluation")
        report = _json(path, f"{suite} evaluation")
        gate = report.get("gate")
        metrics = report.get("metrics")
        if not isinstance(gate, Mapping) or not isinstance(metrics, Mapping):
            raise HubArtifactAssemblyError(f"{suite} evaluation lacks gate or metrics")
        suites[suite] = {
            "report_sha256": _sha256(path),
            "candidate": report.get("candidate"),
            "gate_status": gate.get("status"),
            "gate_passed": gate.get("passed"),
            "failed_checks": gate.get("failed_checks"),
            "total": metrics.get("total"),
            "sst_parse_rate": metrics.get("sst_parse_rate"),
            "output_only_compliance_rate": metrics.get("output_only_compliance_rate"),
            "protected_span_exact_rate": metrics.get("protected_span_exact_rate"),
            "safe_renderer_rate": metrics.get("safe_renderer_rate"),
            "normalized_exact_match": metrics.get("normalized_exact_match"),
            "critical_errors": metrics.get("critical_errors"),
        }
    return {
        "schema_version": 1,
        "kind": "scriber_private_candidate_evaluation_summary",
        "status": "PRIVATE_CANDIDATE",
        "suites": suites,
    }


def _package_versions() -> dict[str, Any]:
    versions: dict[str, str] = {}
    for package in _PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    return {
        "schema_version": 1,
        "python": os.sys.version.split()[0],
        "packages": versions,
    }


def _metric(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}"
    if value is None:
        return "n/a"
    return str(value)


def _model_card(
    *,
    evaluation: Mapping[str, Any],
    matrix: Mapping[str, Any],
    training: Mapping[str, Any],
    selected_variant: str,
    quantization_selection: Mapping[str, Any] | None,
) -> str:
    rows = []
    for name, suite in evaluation["suites"].items():
        rows.append(
            "| "
            + " | ".join(
                (
                    name,
                    _metric(suite["total"]),
                    _metric(suite["gate_status"]),
                    _metric(suite["sst_parse_rate"]),
                    _metric(suite["protected_span_exact_rate"]),
                    _metric(suite["output_only_compliance_rate"]),
                )
            )
            + " |"
        )
    quantization_lines: tuple[str, ...] = ()
    if quantization_selection is not None:
        decision = quantization_selection["decision"]
        quantization_lines = (
            f"- Preferred internal desktop candidate: `{decision['preferred_internal_desktop_variant']}`",
            f"- Optional low-resource candidate: `{decision['optional_low_resource_variant']}`",
            "- Quantized quality delta proven: `no`; both GGUF variants remain private candidates.",
        )
    return "\n".join(
        (
            "---",
            "language:",
            "- de",
            "- en",
            "license: gemma",
            "base_model: google/gemma-3-270m-it",
            "pipeline_tag: text-generation",
            "tags:",
            "- text-polishing",
            "- speech-to-text",
            "- synthetic-data",
            "---",
            "",
            "# Scriber Gemma 3 270M transcript polishing (private candidate)",
            "",
            "Status: **PRIVATE_CANDIDATE**. This artifact is experimental and is not a production release.",
            "At least one fixed quality gate failed; the attached reports remain authoritative.",
            "",
            "The model is a Scriber-specific full fine-tune of `google/gemma-3-270m-it` for conservative",
            "local transcript cleanup into Scriber Structured Text v1. It must be used with protected-span",
            "handling and fail-closed validation. It is not an STT model and does not replace the existing",
            "speech-recognition providers.",
            "",
            "## Bound identity",
            "",
            "- Base revision: `ac82b4e820549b854eebf28ce6dedaf9fdfa17b3`",
            f"- Training fingerprint: `{training.get('run_fingerprint') or training.get('training_run_fingerprint') or training.get('training_fingerprint')}`",
            f"- Completed step: `{training.get('global_step')}`",
            f"- Published quality-reference variant: `{selected_variant}`",
            f"- Variant matrix status: `{matrix.get('status')}`",
            *quantization_lines,
            "",
            "## Automatic evaluation",
            "",
            "| Suite | Cases | Gate | SST parse | Protected spans | Output only |",
            "|---|---:|---|---:|---:|---:|",
            *rows,
            "",
            "See `reports/` for complete automatic, Terra, Sol, benchmark, training, and quantization evidence.",
            "Hard deterministic errors cannot be overruled by an AI judge.",
            "",
            "## Usage and safety",
            "",
            "Use deterministic greedy generation with the bundled runtime contract. Reject invalid SST, unknown",
            "tags, changed protected spans, numbers, amounts, legal references, or invented structure. Fall back",
            "to conservative plain text and ultimately to the original transcript when validation fails.",
            "",
            "Gemma and this derivative are subject to the bundled Gemma Terms of Use and Prohibited Use Policy.",
            "This model does not provide legal, tax, medical, or financial advice.",
            "",
        )
    )


def _dataset_card(
    *,
    release_counts: Mapping[str, int],
    ai_gold_counts: Mapping[str, int],
    dataset_manifest: Mapping[str, Any],
    ai_gold_manifest: Mapping[str, Any],
) -> str:
    return "\n".join(
        (
            "---",
            "license: apache-2.0",
            "language:",
            "- de",
            "- en",
            "task_categories:",
            "- text-generation",
            "tags:",
            "- synthetic",
            "- speech-to-text",
            "- text-polishing",
            "---",
            "",
            "# Scriber AI-only transcript-polishing dataset",
            "",
            "Private synthetic dataset for Scriber Structured Text v1. No private Scriber data, human curation,",
            "Human-Gold data, or token-based generative model API was used.",
            "",
            "## Split counts",
            "",
            *(f"- `{name}`: {count:,}" for name, count in release_counts.items()),
            *(f"- `ai_gold/{name}`: {count:,}" for name, count in ai_gold_counts.items()),
            "",
            f"- Canonical documents: {dataset_manifest.get('canonical_count')}",
            f"- AI-Gold total: {ai_gold_manifest.get('total_count')}",
            f"- Split before variants: {dataset_manifest.get('split_before_variants')}",
            f"- Split leakage: {dataset_manifest.get('split_leakage')}",
            "",
            "Test, challenge, AI-Gold test, and AI-Gold challenge are frozen holdouts and were not used for training.",
            "The semantic plan is the fact truth and the document AST is the structure truth.",
            "See `DATASET_SOURCES.md`, `DATA_LICENSES.json`, and `reports/` for provenance and quality evidence.",
            "",
        )
    )


def _training_config(training_report: Mapping[str, Any]) -> bytes:
    configuration = training_report.get("config")
    if not isinstance(configuration, Mapping):
        configuration = {
            key: training_report.get(key)
            for key in (
                "base_model",
                "base_revision",
                "training_method",
                "global_step",
                "max_steps",
            )
            if training_report.get(key) is not None
        }
    return yaml.safe_dump(dict(configuration), allow_unicode=True, sort_keys=True).encode("utf-8")


def _first_regression_example(suite: Path) -> dict[str, Any]:
    with suite.open(encoding="utf-8") as stream:
        first = json.loads(next(stream))
    if not isinstance(first, dict):
        raise HubArtifactAssemblyError("Hub regression suite has an invalid first record")
    return {
        "source_text": first["source_text"],
        "expected_sst_sha256": first["expected"]["sst_sha256"],
        "case_id": first["case_id"],
        "note": "The exact output is hash-bound in examples/regression_sample.jsonl.",
    }


def _prepare_root(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise HubArtifactAssemblyError(f"publication root is not a directory: {path}")
        if any(path.iterdir()):
            raise HubArtifactAssemblyError(f"publication root is not empty: {path}")
    else:
        path.mkdir(parents=True)


def assemble_private_hub_artifacts(
    *,
    model_source: str | Path,
    dataset_source: str | Path,
    ai_gold_source: str | Path,
    evaluation_root: str | Path,
    completed_training_report: str | Path,
    variant_matrix_report: str | Path,
    benchmark_report: str | Path,
    terra_judge_report: str | Path,
    sol_judge_report: str | Path,
    regression_suite: str | Path,
    publication_config: str | Path,
    gemma_license: str | Path,
    apache_license: str | Path,
    output_root: str | Path,
    selected_variant: str,
    quantized_artifacts: Mapping[str, str | Path] | None = None,
    quantization_selection_report: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble model and dataset upload roots without contacting Hugging Face."""

    if selected_variant not in {"bf16", "Q8_0", "Q4_K_M"}:
        raise HubArtifactAssemblyError("selected variant is not a supported publication candidate")
    model = _directory(model_source, "completed model")
    dataset = _directory(dataset_source, "final dataset")
    ai_gold = _directory(ai_gold_source, "AI-Gold dataset")
    evaluation = _directory(evaluation_root, "final evaluation")
    training_path = _regular_file(completed_training_report, "completed training report")
    matrix_path = _regular_file(variant_matrix_report, "variant matrix report")
    benchmark_path = _regular_file(benchmark_report, "benchmark report")
    terra_path = _regular_file(terra_judge_report, "Terra judge report")
    sol_path = _regular_file(sol_judge_report, "Sol judge report")
    regression_path = _regular_file(regression_suite, "Hub regression suite")
    config_path = _regular_file(publication_config, "publication config")
    gemma_license_path = _regular_file(gemma_license, "Gemma license")
    apache_license_path = _regular_file(apache_license, "Apache-2.0 license")
    training = _json(training_path, "completed training report")
    matrix = validate_variant_release_report(_json(matrix_path, "variant matrix report"))
    if selected_variant not in matrix["variants"]:
        raise HubArtifactAssemblyError("selected variant has no complete matrix evidence")
    training_fingerprint = (
        training.get("run_fingerprint")
        or training.get("training_run_fingerprint")
        or training.get("training_fingerprint")
    )
    if matrix["training_fingerprint"] != training_fingerprint:
        raise HubArtifactAssemblyError("variant matrix differs from the completed training run")
    quantization_selection: dict[str, Any] | None = None
    quantization_selection_path: Path | None = None
    if quantization_selection_report is not None:
        quantization_selection_path = _regular_file(
            quantization_selection_report,
            "quantization selection report",
        )
        quantization_selection = _json(
            quantization_selection_path,
            "quantization selection report",
        )
        decision = quantization_selection.get("decision")
        quality = quantization_selection.get("quality_evidence")
        variants = quantization_selection.get("variants")
        q8 = variants.get("Q8_0") if isinstance(variants, Mapping) else None
        q4 = variants.get("Q4_K_M") if isinstance(variants, Mapping) else None
        if (
            quantization_selection.get("kind")
            != "scriber_quantization_selection_report"
            or quantization_selection.get("status") != "PRIVATE_CANDIDATE"
            or quantization_selection.get("training_fingerprint")
            != training_fingerprint
            or not isinstance(decision, Mapping)
            or decision.get("quality_reference_variant") != selected_variant
            or decision.get("preferred_internal_desktop_variant") != "Q8_0"
            or decision.get("optional_low_resource_variant") != "Q4_K_M"
            or decision.get("release_qualified") is not False
            or not isinstance(quality, Mapping)
            or quality.get("quantized_quality_delta_proven") is not False
            or not isinstance(variants, Mapping)
            or not isinstance(q8, Mapping)
            or q8.get("fresh_reload_passed") is not True
            or not isinstance(q4, Mapping)
            or q4.get("fresh_reload_passed") is not True
        ):
            raise HubArtifactAssemblyError(
                "quantization selection is not the expected private Q8/Q4 decision"
            )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise HubArtifactAssemblyError("publication config is invalid")
    regression_policy = config["artifacts"]["model"]["reload"]["regression"]
    _, regression_evidence = load_hub_regression_suite(
        regression_path,
        regression_policy,
    )
    output = Path(output_root).resolve()
    model_root = output / "model"
    dataset_root = output / "dataset"
    _prepare_root(model_root)
    _prepare_root(dataset_root)

    for relative in _MODEL_FILES:
        _link_or_copy(_regular_file(model / relative, f"model file {relative}"), model_root / relative)
    for relative, source in _MODEL_RUNTIME_FILES.items():
        _link_or_copy(_regular_file(source, f"runtime source {relative}"), _safe_destination(model_root, relative))
    quantized = dict(quantized_artifacts or {})
    for variant in sorted(quantized):
        if variant not in {"Q8_0", "Q4_K_M"}:
            raise HubArtifactAssemblyError(f"unsupported quantized artifact: {variant}")
        _copy_tree(
            _directory(quantized[variant], f"{variant} artifact"),
            model_root / "quantized" / variant,
        )

    evaluation_summary = _evaluation_summary(evaluation)
    _link_or_copy(gemma_license_path, model_root / "GEMMA_LICENSE.md")
    _write_immutable(
        model_root / "NOTICE",
        (
            b"Gemma is provided under and subject to the Gemma Terms of Use found at "
            b"ai.google.dev/gemma/terms\n\n"
            b"Google LLC is the provider of Gemma. Scriber modified Gemma by full fine-tuning "
            b"it for conservative transcript polishing. Google does not endorse Scriber.\n"
        ),
    )
    _write_immutable(
        model_root / "MODIFICATIONS.md",
        (
            b"# Scriber modifications\n\n"
            b"Base model: `google/gemma-3-270m-it`\n\n"
            b"Base revision: `ac82b4e820549b854eebf28ce6dedaf9fdfa17b3`\n\n"
            b"Scriber performed a full supervised fine-tune on synthetic AI-only transcript-polishing "
            b"data. The modified files include `model.safetensors` and `config.json`. The generation "
            b"configuration, frozen protected-span policy, SST schema, runtime validation sources, "
            b"evaluation evidence, and optional GGUF quantizations are distributed with the derivative.\n"
        ),
    )
    card = _model_card(
        evaluation=evaluation_summary,
        matrix=matrix,
        training=training,
        selected_variant=selected_variant,
        quantization_selection=quantization_selection,
    ).encode("utf-8")
    _write_immutable(model_root / "README.md", card)
    _write_immutable(model_root / "MODEL_CARD.md", card)
    _write_immutable(model_root / "training_config.yaml", _training_config(training))
    _write_immutable(model_root / "package_versions.json", _canonical_json(_package_versions()))
    _link_or_copy(
        _regular_file(dataset / "splits" / "coverage_report.json", "data quality report"),
        model_root / "reports" / "data_quality_report.json",
    )
    _link_or_copy(
        _regular_file(dataset / "dataset_manifest.json", "dataset manifest"),
        model_root / "reports" / "ai_data_factory_report.json",
    )
    _link_or_copy(
        _regular_file(ai_gold / "ai_gold_manifest.json", "AI-Gold manifest"),
        model_root / "reports" / "ai_gold_report.json",
    )
    _write_immutable(
        model_root / "reports" / "evaluation_report.json",
        _canonical_json(evaluation_summary),
    )
    _link_or_copy(terra_path, model_root / "reports" / "terra_judge_report.json")
    _link_or_copy(sol_path, model_root / "reports" / "sol_judge_report.json")
    _link_or_copy(benchmark_path, model_root / "reports" / "benchmark_report.json")
    _link_or_copy(
        matrix_path,
        model_root / "reports" / "variant_release_matrix_report.json",
    )
    if quantization_selection_path is not None:
        _link_or_copy(
            quantization_selection_path,
            model_root / "reports" / "quantization_selection_report.json",
        )
    _link_or_copy(regression_path, model_root / "examples" / "regression_sample.jsonl")
    _write_immutable(
        model_root / "examples" / "inference.json",
        _canonical_json(_first_regression_example(regression_path)),
    )

    release_counts: dict[str, int] = {}
    ai_gold_counts: dict[str, int] = {}
    for split, relative in _DATASET_SPLITS.items():
        source = _regular_file(dataset / relative, f"dataset {split} split")
        release_counts[split] = _count_jsonl(source)
        _link_or_copy(source, dataset_root / "data" / f"{split}.jsonl")
    for split, relative in _AI_GOLD_SPLITS.items():
        source = _regular_file(ai_gold / relative, f"AI-Gold {split} split")
        ai_gold_counts[split] = _count_jsonl(source)
        _link_or_copy(source, dataset_root / "ai_gold" / f"{split}.jsonl")
    dataset_manifest = _json(dataset / "dataset_manifest.json", "dataset manifest")
    ai_gold_manifest = _json(ai_gold / "ai_gold_manifest.json", "AI-Gold manifest")
    dataset_card = _dataset_card(
        release_counts=release_counts,
        ai_gold_counts=ai_gold_counts,
        dataset_manifest=dataset_manifest,
        ai_gold_manifest=ai_gold_manifest,
    ).encode("utf-8")
    _write_immutable(dataset_root / "README.md", dataset_card)
    _write_immutable(dataset_root / "DATASET_CARD.md", dataset_card)
    _link_or_copy(apache_license_path, dataset_root / "LICENSE")
    _write_immutable(
        dataset_root / "DATASET_SOURCES.md",
        (
            b"# Dataset sources\n\n"
            b"This dataset is synthetic. AI agents authored semantic plans, document targets, and critic "
            b"decisions; deterministic local generators created the mass variants and STT corruptions. "
            b"No private Scriber data, no human curation, no Human-Gold set, and no generative model API "
            b"were used. Test and challenge partitions remained isolated from training.\n"
        ),
    )
    _write_immutable(
        dataset_root / "DATA_LICENSES.json",
        _canonical_json(
            {
                "schema_version": 1,
                "dataset_license": "Apache-2.0",
                "sources": [
                    {
                        "id": "ai_agent_semantic_plans_and_targets",
                        "origin": "Codex agents through the included ChatGPT Pro entitlement",
                        "license": "Apache-2.0",
                        "synthetic_only": True,
                        "human_curation": False,
                        "private_data": False,
                        "license_reviewed": True,
                    },
                    {
                        "id": "local_deterministic_generators",
                        "origin": "Scriber repository generators and STT corruption engine",
                        "license": "Apache-2.0",
                        "synthetic_only": True,
                        "human_curation": False,
                        "private_data": False,
                        "license_reviewed": True,
                    },
                ],
            }
        ),
    )
    for root in (model_root, dataset_root):
        _link_or_copy(
            _regular_file(dataset / "splits" / "coverage_report.json", "data quality report"),
            root / "reports" / "data_quality_report.json",
        )
        _link_or_copy(
            _regular_file(dataset / "dataset_manifest.json", "data factory report"),
            root / "reports" / "ai_data_factory_report.json",
        )
        _link_or_copy(
            _regular_file(ai_gold / "ai_gold_manifest.json", "AI-Gold report"),
            root / "reports" / "ai_gold_report.json",
        )

    result = {
        "schema_version": 1,
        "status": "assembled_private_candidate",
        "selected_variant": selected_variant,
        "model_root": str(model_root),
        "dataset_root": str(dataset_root),
        "release_split_counts": release_counts,
        "ai_gold_split_counts": ai_gold_counts,
        "regression": regression_evidence,
        "training_fingerprint": training_fingerprint,
        "variant_matrix_status": matrix["status"],
    }
    _write_immutable(output / "assembly-report.json", _canonical_json(result))
    return result
