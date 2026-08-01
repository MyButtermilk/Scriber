"""Fail-closed private Hugging Face publication with revision-pinned reloads."""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .hub_regression import (
    HubRegressionError,
    load_hub_regression_suite,
    run_hub_regression_suite,
    validate_hub_regression_policy,
)
from .policy_artifact import (
    PolicyArtifactError,
    frozen_lexical_policy_binding,
    validate_frozen_lexical_policy_requirement,
    verify_frozen_lexical_policy_artifact,
)
from .variant_release import VariantReleaseError, validate_variant_release_report

MANIFEST_FILENAME = "scriber-publication-manifest.json"
REPORT_SCHEMA_FILENAME = "hub_verification_schema.json"
TRAINING_BINDING_FILENAME = "reports/completed_final_training_binding.json"
SELECTED_CHECKPOINT_BINDING_FILENAME = "reports/selected_checkpoint_binding.json"
VARIANT_RELEASE_REPORT_FILENAME = "reports/variant_release_matrix_report.json"
CHECKSUMS_FILENAME = "checksums.json"
_ARTIFACT_KINDS = ("model", "dataset")
_EXPECTED_REPOSITORIES = {
    "model": "Buttermilk03/scriber-gemma3-270m-polishing-de-v1",
    "dataset": "Buttermilk03/scriber-transcript-polishing-de-v1",
}


def _auth_check_repository_access(
    auth_check: Callable[..., Any],
    *,
    repo_id: str,
    repo_type: str,
    token: str,
) -> None:
    """Use the strongest access probe supported by the installed Hub client.

    Older ``huggingface_hub`` releases accepted ``write=True`` while current
    releases removed that keyword.  Repository creation immediately before
    this probe already proves write access; this call additionally verifies
    authenticated access without weakening compatibility with either API.
    """

    parameters = inspect.signature(auth_check).parameters.values()
    supports_write = any(
        parameter.name == "write"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    kwargs: dict[str, Any] = {"repo_type": repo_type, "token": token}
    if supports_write:
        kwargs["write"] = True
    auth_check(repo_id, **kwargs)
_EXPECTED_PRIVACY = {
    "synthetic_ai_only": True,
    "human_curation": False,
    "contains_private_scriber_data": False,
    "contains_secrets": False,
}
_EXPECTED_MODEL_LEGAL = {
    "kind": "gemma_derived_model",
    "notice_file": "NOTICE",
    "license_file": "GEMMA_LICENSE.md",
    "modifications_file": "MODIFICATIONS.md",
    "base_model": "google/gemma-3-270m-it",
    "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
}
_EXPECTED_DATASET_LEGAL = {
    "kind": "synthetic_dataset",
    "license_id": "Apache-2.0",
    "license_file": "LICENSE",
    "sources_file": "DATASET_SOURCES.md",
    "licenses_file": "DATA_LICENSES.json",
}
_MANDATORY_PATTERNS = {
    "model": frozenset(
        {
            "README.md",
            "NOTICE",
            "GEMMA_LICENSE.md",
            "MODIFICATIONS.md",
            "config.json",
            "scriber-protection-policy.json",
            "generation_config.json",
            "*.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "sst_v1_schema.json",
            "runtime/inference.py",
            "runtime/sst_parser.py",
            "runtime/renderers.py",
            "MODEL_CARD.md",
            "training_config.yaml",
            "package_versions.json",
            "reports/data_quality_report.json",
            "reports/ai_data_factory_report.json",
            "reports/ai_gold_report.json",
            "reports/evaluation_report.json",
            "reports/terra_judge_report.json",
            "reports/sol_judge_report.json",
            "reports/benchmark_report.json",
            VARIANT_RELEASE_REPORT_FILENAME,
            TRAINING_BINDING_FILENAME,
            SELECTED_CHECKPOINT_BINDING_FILENAME,
            CHECKSUMS_FILENAME,
            "examples/inference.json",
            "examples/regression_sample.jsonl",
        }
    ),
    "dataset": frozenset(
        {
            "README.md",
            "data/train.jsonl",
            "data/validation.jsonl",
            "data/test.jsonl",
            "data/challenge.jsonl",
            "ai_gold/train.jsonl",
            "ai_gold/validation.jsonl",
            "ai_gold/test.jsonl",
            "ai_gold/challenge.jsonl",
            "DATASET_CARD.md",
            "LICENSE",
            "DATASET_SOURCES.md",
            "DATA_LICENSES.json",
            "reports/data_quality_report.json",
            "reports/ai_data_factory_report.json",
            "reports/ai_gold_report.json",
            CHECKSUMS_FILENAME,
        }
    ),
}
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SECRET_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials",
        "credentials.json",
        "token",
        "token.txt",
    }
)
_SECRET_CONTENT = (
    re.compile(rb"hf_[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:password|secret|access[_-]?token)\s*[:=]\s*[^\s\"']{8,}"),
)
_PRIVATE_CONTENT = (
    re.compile(rb"(?i)(?:[A-Z]:\\Users\\[^\s\\]+\\|/home/[^/\s]+/)"),
    re.compile(rb"(?i)AppData\\(?:Local|Roaming)\\"),
)
_IGNORED_SNAPSHOT_PREFIXES = (".cache/huggingface/",)
_IGNORED_SNAPSHOT_FILES = frozenset({".gitattributes", MANIFEST_FILENAME})
_ALLOWED_REMOTE_EXTRA_FILES = frozenset({".gitattributes"})
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_hub_reload_worker.py"
_RUNTIME_SOURCE_BINDINGS = {
    "runtime/inference.py": "inference.py",
    "runtime/sst_parser.py": "sst_parser.py",
    "runtime/renderers.py": "target_renderer.py",
    "sst_v1_schema.json": "../../contracts/sst_v1_schema.json",
}


class PublicationError(RuntimeError):
    """Publication or verification failed before a trustworthy report existed."""


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hash(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['path']}\0{item['size_bytes']}\0{item['sha256']}\n".encode())
    return digest.hexdigest()


def _sha256_json(value: object, *, pretty: bool = False) -> str:
    return hashlib.sha256(_canonical_json(value, pretty=pretty)).hexdigest()


def _validate_report_schema(report: Mapping[str, Any]) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "contracts" / REPORT_SCHEMA_FILENAME
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError(f"Hub verification schema is unavailable: {REPORT_SCHEMA_FILENAME}") from error
    errors = sorted(Draft202012Validator(schema).iter_errors(report), key=lambda error: list(error.path))
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "<root>"
        raise PublicationError(f"Hub verification report is invalid at {location}: {errors[0].message}")


def _validate_local_preparation_schema(report: Mapping[str, Any]) -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "hub_preparation_schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("Hub preparation schema is unavailable") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: list(error.path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "<root>"
        raise PublicationError(
            f"Hub preparation report is invalid at {location}: "
            f"{errors[0].message}"
        )


def _completed_training_binding(path: str | Path) -> dict[str, Any]:
    report_path = Path(path).resolve()
    if Path(path).is_symlink() or not report_path.is_file():
        raise PublicationError("completed final-training report must be a regular local file")
    try:
        payload = report_path.read_bytes()
        report = json.loads(payload)
        schema = json.loads(
            (
                Path(__file__).resolve().parents[2] / "contracts" / "completed_final_training_report_schema.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("completed final-training report is unreadable") from error
    errors = sorted(Draft202012Validator(schema).iter_errors(report), key=lambda error: list(error.path))
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "<root>"
        raise PublicationError(f"completed final-training report is invalid at {location}")
    if (
        not isinstance(report, Mapping)
        or report.get("run_kind") != "final"
        or report.get("training_completed") is not True
        or report.get("bf16") is not True
        or report.get("base_model") != _EXPECTED_MODEL_LEGAL["base_model"]
        or report.get("base_revision") != _EXPECTED_MODEL_LEGAL["base_revision"]
    ):
        raise PublicationError(
            "completed training evidence is not the finished pinned BF16 final run"
        )
    run_fingerprint = report.get("run_fingerprint")
    final_model = report.get("final_model")
    inventory = final_model.get("inventory") if isinstance(final_model, Mapping) else None
    tree_sha256 = inventory.get("tree_sha256") if isinstance(inventory, Mapping) else None
    policy_report = report.get("protection_policy")
    policy_files = inventory.get("files") if isinstance(inventory, Mapping) else None
    reload_verification = final_model.get("reload_verification") if isinstance(final_model, Mapping) else None
    expected_policy = frozen_lexical_policy_binding()
    policy_file_matches = (
        isinstance(policy_files, list)
        and sum(
            1
            for item in policy_files
            if isinstance(item, Mapping)
            and item.get("path") == expected_policy["filename"]
            and item.get("sha256") == expected_policy["artifact_sha256"]
        )
        == 1
    )
    if (
        not isinstance(run_fingerprint, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", run_fingerprint)
        or not isinstance(final_model, Mapping)
        or final_model.get("run_fingerprint") != run_fingerprint
        or not isinstance(tree_sha256, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", tree_sha256)
        or not isinstance(policy_report, Mapping)
        or policy_report.get("policy_id") != expected_policy["policy_id"]
        or policy_report.get("policy_sha256") != expected_policy["policy_sha256"]
        or policy_report.get("sha256") != expected_policy["artifact_sha256"]
        or not policy_file_matches
        or not isinstance(reload_verification, Mapping)
        or reload_verification.get("protection_policy_sha256") != expected_policy["artifact_sha256"]
    ):
        raise PublicationError(
            "completed training report lacks matching final-model inventory evidence "
            "or frozen protection-policy evidence"
        )
    return {
        "schema_version": 1,
        "completed_training_report_sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "training_run_fingerprint": run_fingerprint,
        "bf16_final_model_tree_sha256": tree_sha256,
        "protection_policy": expected_policy,
    }


def _checkpoint_canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _selected_checkpoint_binding(
    path: str | Path,
    *,
    training_binding: Mapping[str, Any],
    model_root: Path,
) -> dict[str, Any]:
    publication_path = Path(path).resolve()
    if Path(path).is_symlink() or not publication_path.is_file():
        raise PublicationError(
            "selected checkpoint publication must be a regular local file"
        )
    try:
        payload = publication_path.read_bytes()
        publication = json.loads(payload)
        schema = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "contracts"
                / "selected_checkpoint_publication_schema.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(
            "selected checkpoint publication is unreadable"
        ) from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(publication),
        key=lambda error: list(error.path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "<root>"
        raise PublicationError(
            f"selected checkpoint publication is invalid at {location}"
        )
    checkpoint = publication["checkpoint"]
    fresh_reload = publication["fresh_reload"]
    fresh_inventory = publication["fresh_inventory"]
    base_tokenizer = publication["base_tokenizer"]
    if (
        publication["selected_candidate_id"] != checkpoint["candidate_id"]
        or checkpoint["candidate_id"] != checkpoint["checkpoint"]
        or checkpoint["run_fingerprint"]
        != training_binding["training_run_fingerprint"]
        or fresh_reload["process_boundary"] != "fresh_subprocess"
        or fresh_reload["configuration_source"] != "saved_artifact"
        or fresh_reload["tokenizer_source"] != "pinned_base_tokenizer"
        or fresh_reload["model_tree_sha256"] != checkpoint["tree_sha256"]
        or fresh_reload["model_config_sha256"] != checkpoint["config_sha256"]
        or fresh_reload["model_sha256"] != checkpoint["model_sha256"]
        or fresh_inventory["tree_sha256"] != checkpoint["tree_sha256"]
        or fresh_inventory["config_sha256"] != checkpoint["config_sha256"]
        or fresh_inventory["model_sha256"] != checkpoint["model_sha256"]
        or fresh_inventory["file_count"] != checkpoint["file_count"]
        or fresh_inventory["total_bytes"] != checkpoint["total_bytes"]
        or base_tokenizer
        != {
            "id": _EXPECTED_MODEL_LEGAL["base_model"],
            "revision": _EXPECTED_MODEL_LEGAL["base_revision"],
        }
    ):
        raise PublicationError(
            "selected checkpoint publication differs from its training or fresh inventory"
        )
    config_path = _required_file(model_root, "config.json", "published model config")
    config_sha256 = f"sha256:{_sha256_path(config_path)}"
    model_files = [
        {
            "path": weight.relative_to(model_root).as_posix(),
            "bytes": weight.stat().st_size,
            "sha256": f"sha256:{_sha256_path(weight)}",
        }
        for weight in sorted(
            model_root.glob("*.safetensors"),
            key=lambda item: item.name,
        )
        if weight.is_file() and not weight.is_symlink()
    ]
    if (
        not model_files
        or config_sha256 != checkpoint["config_sha256"]
        or _checkpoint_canonical_sha256(model_files) != checkpoint["model_sha256"]
        or checkpoint["file_count"] != len(model_files) + 1
        or checkpoint["total_bytes"]
        != config_path.stat().st_size
        + sum(int(item["bytes"]) for item in model_files)
    ):
        raise PublicationError(
            "published model bytes differ from the selected checkpoint"
        )
    return {
        "schema_version": 1,
        "selected_checkpoint_publication_sha256": (
            "sha256:" + hashlib.sha256(payload).hexdigest()
        ),
        "selected_candidate_id": publication["selected_candidate_id"],
        "selected_checkpoint_identity_sha256": checkpoint["identity_sha256"],
        "selected_checkpoint_tree_sha256": checkpoint["tree_sha256"],
        "selected_model_sha256": checkpoint["model_sha256"],
        "selected_config_sha256": checkpoint["config_sha256"],
        "base_tokenizer": copy.deepcopy(publication["base_tokenizer"]),
    }


def _write_immutable(path: Path, payload: bytes, *, label: str) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise PublicationError(f"refusing to replace an immutable {label}: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _safe_relative_path(value: object, label: str) -> str:
    text = _required_text(value, label).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a safe relative path")
    return path.as_posix()


def validate_publication_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the fixed private Buttermilk03 publication policy."""

    if not isinstance(config, Mapping):
        raise ValueError("publication config must be an object")
    value = copy.deepcopy(dict(config))
    if value.get("schema_version") != 4:
        raise ValueError("publication config schema_version must be 4")
    if value.get("namespace") != "Buttermilk03":
        raise ValueError("publication namespace is fixed to Buttermilk03")
    if value.get("visibility") != "private":
        raise ValueError("publication visibility must remain private")
    if value.get("token_env") != "HF_TOKEN":
        raise ValueError("publication token environment name must be HF_TOKEN")
    if value.get("manifest_filename") != MANIFEST_FILENAME:
        raise ValueError(f"publication manifest_filename must be {MANIFEST_FILENAME}")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(_ARTIFACT_KINDS):
        raise ValueError("publication artifacts must contain exactly model and dataset")
    for kind in _ARTIFACT_KINDS:
        definition = artifacts.get(kind)
        if not isinstance(definition, Mapping):
            raise ValueError(f"publication {kind} definition must be an object")
        repo_id = _required_text(definition.get("repo_id"), f"{kind}.repo_id")
        if repo_id != _EXPECTED_REPOSITORIES[kind]:
            raise ValueError(f"publication {kind} repository is not allowlisted")
        if definition.get("repo_type") != kind:
            raise ValueError(f"publication {kind} repo_type must be {kind}")
        patterns = definition.get("required_patterns")
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(pattern, str) and pattern for pattern in patterns)
            or len(patterns) != len(set(patterns))
        ):
            raise ValueError(f"publication {kind} required_patterns must be unique strings")
        missing_patterns = sorted(_MANDATORY_PATTERNS[kind].difference(patterns))
        if missing_patterns:
            raise ValueError(
                f"publication {kind} required_patterns omits mandatory file: "
                f"{missing_patterns[0]}"
            )
        if definition.get("privacy") != _EXPECTED_PRIVACY:
            raise ValueError(f"publication {kind} privacy declaration is not safe")
        expected_legal = (
            _EXPECTED_MODEL_LEGAL if kind == "model" else _EXPECTED_DATASET_LEGAL
        )
        if definition.get("legal") != expected_legal:
            raise ValueError(f"publication {kind} legal policy is not the fixed policy")
        if kind == "model":
            try:
                validate_frozen_lexical_policy_requirement(definition.get("protection_policy"))
            except PolicyArtifactError as error:
                raise ValueError("publication model does not bind the frozen lexical protection policy") from error
            if "scriber-protection-policy.json" not in patterns:
                raise ValueError("publication model must require the frozen protection-policy file")
        elif "protection_policy" in definition:
            raise ValueError("dataset publication must not declare a model protection policy")
        reload_definition = definition.get("reload")
        if not isinstance(reload_definition, Mapping):
            raise ValueError(f"publication {kind} reload definition must be an object")
        expected_loader = "transformers_local" if kind == "model" else "jsonl_splits"
        if reload_definition.get("kind") != expected_loader:
            raise ValueError(f"publication {kind} reload kind must be {expected_loader}")
        if kind == "model":
            regression = reload_definition.get("regression")
            if not isinstance(regression, Mapping):
                raise ValueError("publication model reload has no regression policy")
            try:
                reload_definition["regression"] = validate_hub_regression_policy(
                    regression
                )
            except HubRegressionError as error:
                raise ValueError(
                    "publication model reload regression policy is invalid"
                ) from error
            if (
                reload_definition.get("variant_release_report")
                != VARIANT_RELEASE_REPORT_FILENAME
            ):
                raise ValueError(
                    "publication model reload must bind the complete variant release report"
                )
            if set(reload_definition) != {
                "kind",
                "regression",
                "variant_release_report",
            }:
                raise ValueError("publication model reload policy is not closed")
        else:
            split_files = reload_definition.get("split_files")
            if not isinstance(split_files, Mapping) or set(split_files) != {
                "train",
                "validation",
                "test",
                "challenge",
            }:
                raise ValueError("dataset reload split_files must contain all four splits")
            reload_definition["split_files"] = {
                split: _safe_relative_path(path, f"dataset.reload.split_files.{split}")
                for split, path in split_files.items()
            }
            ai_gold_split_files = reload_definition.get("ai_gold_split_files")
            if ai_gold_split_files is not None:
                if not isinstance(ai_gold_split_files, Mapping) or set(ai_gold_split_files) != {
                    "train",
                    "validation",
                    "test",
                    "challenge",
                }:
                    raise ValueError("dataset reload ai_gold_split_files must contain all four splits when configured")
                reload_definition["ai_gold_split_files"] = {
                    split: _safe_relative_path(path, f"dataset.reload.ai_gold_split_files.{split}")
                    for split, path in ai_gold_split_files.items()
                }
            if set(reload_definition) != {
                "kind",
                "split_files",
                "ai_gold_split_files",
            }:
                raise ValueError("publication dataset reload policy is not closed")
    return value


def load_publication_config(path: str | Path) -> dict[str, Any]:
    """Load and validate a checked-in YAML publication policy."""

    config_path = Path(path)
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"publication config is unreadable: {config_path}") from error
    if not isinstance(value, Mapping):
        raise ValueError("publication config must decode to an object")
    return validate_publication_config(value)


def _is_secret_filename(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(part.casefold() in _SECRET_FILE_NAMES for part in parts)


def _assert_secret_free(path: Path, relative: str) -> None:
    if _is_secret_filename(relative):
        raise PublicationError(f"artifact contains a secret-bearing filename: {relative}")
    overlap = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            content = overlap + chunk
            if any(pattern.search(content) for pattern in _SECRET_CONTENT):
                raise PublicationError(
                    f"artifact contains secret-like content: {relative}"
                )
            if any(pattern.search(content) for pattern in _PRIVATE_CONTENT):
                raise PublicationError(
                    f"artifact contains private local-path content: {relative}"
                )
            overlap = content[-512:]


def _inventory(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
    excluded_prefixes: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise PublicationError("publication artifact must be a real local directory")
    files: list[dict[str, Any]] = []
    candidates = sorted(
        root.rglob("*"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in candidates:
        if path.is_symlink():
            raise PublicationError("publication artifacts may not contain symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded or any(
            relative.startswith(prefix) for prefix in excluded_prefixes
        ):
            continue
        _assert_secret_free(path, relative)
        size = path.stat().st_size
        files.append({"path": relative, "size_bytes": size, "sha256": _sha256_path(path)})
    if not files:
        raise PublicationError("publication artifact directory is empty")
    return files


def _assert_required_patterns(files: list[dict[str, Any]], patterns: list[str], *, label: str) -> None:
    paths = [str(item["path"]) for item in files]
    for pattern in patterns:
        if not any(fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern) for path in paths):
            raise PublicationError(f"{label} artifact is missing required pattern: {pattern}")


def _required_file(root: Path, relative: str, label: str) -> Path:
    path = root / PurePosixPath(_safe_relative_path(relative, label))
    if path.is_symlink() or not path.is_file():
        raise PublicationError(f"{label} is missing")
    return path


def _verify_runtime_sources(root: Path) -> dict[str, Any]:
    """Bind downloaded runtime copies to the code executing the reload checks."""

    package_root = Path(__file__).resolve().parent
    files: list[dict[str, str]] = []
    for relative, source_relative in _RUNTIME_SOURCE_BINDINGS.items():
        bundled = _required_file(
            root,
            relative,
            f"bundled runtime source {relative}",
        )
        source = (package_root / source_relative).resolve()
        if not source.is_file() or source.is_symlink():
            raise PublicationError(
                f"executed runtime source is unavailable for {relative}"
            )
        bundled_bytes = bundled.read_bytes()
        if bundled_bytes != source.read_bytes():
            raise PublicationError(
                f"bundled runtime source differs from executed code: {relative}"
            )
        if bundled.suffix == ".py":
            try:
                compile(
                    bundled_bytes,
                    f"<downloaded:{relative}>",
                    "exec",
                    dont_inherit=True,
                )
            except (SyntaxError, ValueError) as error:
                raise PublicationError(
                    f"bundled runtime source does not compile: {relative}"
                ) from error
        files.append(
            {
                "path": relative,
                "sha256": _sha256_path(bundled),
            }
        )
    return {
        "status": "verified",
        "source_equivalence": "artifact_bytes_equal_executed_package",
        "files": files,
    }


def _required_legal_text(
    root: Path,
    relative: str,
    *,
    label: str,
    markers: tuple[str, ...],
) -> tuple[Path, str]:
    path = _required_file(root, relative, label)
    if path.stat().st_size > 1024 * 1024:
        raise PublicationError(f"{label} is unexpectedly large")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PublicationError(f"{label} is not UTF-8 text") from error
    folded = text.casefold()
    missing = [marker for marker in markers if marker.casefold() not in folded]
    if missing:
        raise PublicationError(f"{label} lacks required legal marker: {missing[0]}")
    return path, text


def _verify_legal_files(
    root: Path,
    definition: Mapping[str, Any],
    *,
    artifact_kind: str,
) -> dict[str, Any]:
    legal = definition.get("legal")
    expected = (
        _EXPECTED_MODEL_LEGAL
        if artifact_kind == "model"
        else _EXPECTED_DATASET_LEGAL
    )
    if legal != expected:
        raise PublicationError(f"{artifact_kind} legal policy differs from the fixed policy")
    if artifact_kind == "model":
        notice, _ = _required_legal_text(
            root,
            str(legal["notice_file"]),
            label="Gemma NOTICE",
            markers=(
                "Gemma is provided under and subject to the Gemma Terms of "
                "Use found at ai.google.dev/gemma/terms",
                "Google",
                "Scriber",
                "modified",
            ),
        )
        license_path, _ = _required_legal_text(
            root,
            str(legal["license_file"]),
            label="Gemma license notice",
            markers=(
                "Gemma Terms of Use",
                "https://ai.google.dev/gemma/terms",
                "Section 1: DEFINITIONS",
                "Section 2: ELIGIBILITY AND USAGE",
                "Section 3: DISTRIBUTION AND RESTRICTIONS",
                "Section 3.2 Use Restrictions",
                "Gemma Prohibited Use Policy",
                "Section 4: ADDITIONAL PROVISIONS",
                "Appendix",
                "Gemma 3",
            ),
        )
        if license_path.stat().st_size < 4096:
            raise PublicationError(
                "Gemma license notice is not a complete Agreement copy"
            )
        modifications, _ = _required_legal_text(
            root,
            str(legal["modifications_file"]),
            label="Gemma modifications disclosure",
            markers=(
                str(legal["base_model"]),
                str(legal["base_revision"]),
                "fine-tun",
                "Scriber",
                "config.json",
                ".safetensors",
            ),
        )
        paths = (notice, license_path, modifications)
    else:
        license_path, _ = _required_legal_text(
            root,
            str(legal["license_file"]),
            label="dataset license",
            markers=("Apache License", "Version 2.0"),
        )
        sources_path, _ = _required_legal_text(
            root,
            str(legal["sources_file"]),
            label="dataset sources disclosure",
            markers=(
                "synthetic",
                "no private Scriber data",
                "no human curation",
                "no generative model API",
            ),
        )
        licenses_path = _required_file(
            root,
            str(legal["licenses_file"]),
            "dataset license inventory",
        )
        try:
            licenses = json.loads(licenses_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PublicationError("dataset license inventory is invalid JSON") from error
        if (
            not isinstance(licenses, Mapping)
            or set(licenses) != {"schema_version", "dataset_license", "sources"}
            or licenses.get("schema_version") != 1
            or licenses.get("dataset_license") != legal["license_id"]
            or not isinstance(licenses.get("sources"), list)
            or not licenses["sources"]
        ):
            raise PublicationError("dataset license inventory has an unsafe shape")
        required_source_fields = {
            "id",
            "origin",
            "license",
            "synthetic_only",
            "human_curation",
            "private_data",
            "license_reviewed",
        }
        for source in licenses["sources"]:
            if (
                not isinstance(source, Mapping)
                or set(source) != required_source_fields
                or not isinstance(source.get("id"), str)
                or not source["id"]
                or not isinstance(source.get("origin"), str)
                or not source["origin"]
                or not isinstance(source.get("license"), str)
                or not source["license"]
                or source.get("synthetic_only") is not True
                or source.get("human_curation") is not False
                or source.get("private_data") is not False
                or source.get("license_reviewed") is not True
            ):
                raise PublicationError(
                    "dataset license inventory contains an unreviewed source"
                )
        paths = (license_path, sources_path, licenses_path)
    return {
        "status": "verified",
        "kind": legal["kind"],
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256_path(path),
            }
            for path in paths
        ],
    }


def _materialize_checksums(root: Path) -> dict[str, Any]:
    files = _inventory(
        root,
        excluded=frozenset(
            {
                CHECKSUMS_FILENAME,
                MANIFEST_FILENAME,
                *_ALLOWED_REMOTE_EXTRA_FILES,
            }
        ),
        excluded_prefixes=_IGNORED_SNAPSHOT_PREFIXES,
    )
    value = {
        "schema_version": 1,
        "algorithm": "sha256-path-size-content-v1",
        "payload_tree_sha256": _tree_hash(files),
        "files": files,
    }
    _write_immutable(
        root / CHECKSUMS_FILENAME,
        _canonical_json(value, pretty=True),
        label="checksums manifest",
    )
    return value


def _verify_checksums(root: Path) -> dict[str, Any]:
    path = _required_file(root, CHECKSUMS_FILENAME, "checksums manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError("checksums manifest is invalid JSON") from error
    files = _inventory(
        root,
        excluded=frozenset(
            {
                CHECKSUMS_FILENAME,
                MANIFEST_FILENAME,
                *_ALLOWED_REMOTE_EXTRA_FILES,
            }
        ),
        excluded_prefixes=_IGNORED_SNAPSHOT_PREFIXES,
    )
    expected = {
        "schema_version": 1,
        "algorithm": "sha256-path-size-content-v1",
        "payload_tree_sha256": _tree_hash(files),
        "files": files,
    }
    if value != expected or path.read_bytes() != _canonical_json(expected, pretty=True):
        raise PublicationError("checksums manifest differs from the artifact bytes")
    return {
        "status": "verified",
        "manifest_sha256": _sha256_path(path),
        "payload_tree_sha256": expected["payload_tree_sha256"],
        "file_count": len(files),
    }


def _variant_release_binding(
    root: Path,
    definition: Mapping[str, Any],
    *,
    training_fingerprint: str | None = None,
) -> dict[str, Any]:
    reload_definition = definition.get("reload")
    relative = (
        reload_definition.get("variant_release_report")
        if isinstance(reload_definition, Mapping)
        else None
    )
    if relative != VARIANT_RELEASE_REPORT_FILENAME:
        raise PublicationError("model publication has no fixed variant release report")
    path = _required_file(root, str(relative), "variant release report")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError
        validated = validate_variant_release_report(value)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        VariantReleaseError,
    ) as error:
        raise PublicationError("variant release report is invalid") from error
    if (
        training_fingerprint is not None
        and validated.get("training_fingerprint") != training_fingerprint
    ):
        raise PublicationError(
            "variant release report differs from the completed training run"
        )
    if validated.get("protection_policy") != frozen_lexical_policy_binding():
        raise PublicationError("variant release report has another protection policy")
    return {
        "status": "verified",
        "matrix_status": validated["status"],
        "passed": validated["passed"],
        "report_sha256": f"sha256:{_sha256_path(path)}",
        "training_fingerprint": validated["training_fingerprint"],
        "tested_variants": list(validated["tested_variants"]),
        "variants": list(validated["variants"]),
        "ineligible_variants": copy.deepcopy(
            dict(validated["ineligible_variants"])
        ),
        "qualification_failures": copy.deepcopy(
            dict(validated["qualification_failures"])
        ),
        "reasons": list(validated["reasons"]),
        "fresh_reload_report_sha256": {
            variant: validated["variant_summaries"][variant][
                "reload_report_sha256"
            ]
            for variant in validated["variants"]
        },
    }


def _jsonl_split_counts(
    root: Path,
    split_files: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for split in sorted(split_files):
        relative = _safe_relative_path(split_files[split], f"{label}.{split}")
        path = root / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise PublicationError(f"{label} split is missing: {split}")
        count = 0
        with path.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.rstrip("\r\n"):
                    raise PublicationError(f"{label} split {split} has a blank line at {line_number}")
                count += 1
        if count <= 0:
            raise PublicationError(f"{label} split is empty: {split}")
        counts[split] = count
    return counts


def _jsonl_split_inventory(
    root: Path,
    split_files: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    counts = _jsonl_split_counts(root, split_files, label=label)
    inventory: dict[str, dict[str, Any]] = {}
    for split in sorted(split_files):
        relative = _safe_relative_path(split_files[split], f"{label}.{split}")
        path = _required_file(root, relative, f"{label} split {split}")
        inventory[split] = {
            "path": relative,
            "record_count": counts[split],
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
        }
    return inventory


def prepare_publication_manifest(
    artifact_dir: str | Path,
    definition: Mapping[str, Any],
    *,
    manifest_filename: str = MANIFEST_FILENAME,
) -> dict[str, Any]:
    """Inventory one upload folder and create an immutable content manifest."""

    root = Path(artifact_dir).resolve()
    if manifest_filename != MANIFEST_FILENAME:
        raise ValueError(f"manifest filename must be {MANIFEST_FILENAME}")
    repo_type = definition.get("repo_type")
    if repo_type not in _ARTIFACT_KINDS:
        raise ValueError("artifact repo_type must be model or dataset")
    if definition.get("repo_id") != _EXPECTED_REPOSITORIES[repo_type]:
        raise ValueError("artifact repository is not allowlisted")
    if definition.get("privacy") != _EXPECTED_PRIVACY:
        raise ValueError("artifact privacy declaration is not safe")
    patterns = definition.get("required_patterns")
    if not isinstance(patterns, list):
        raise ValueError("artifact required_patterns must be a list")
    legal_files = _verify_legal_files(root, definition, artifact_kind=repo_type)
    model_regression_suite: dict[str, Any] | None = None
    variant_release: dict[str, Any] | None = None
    dataset_split_inventory: dict[str, Any] | None = None
    if repo_type == "model":
        reload_definition = definition.get("reload")
        if not isinstance(reload_definition, Mapping):
            raise PublicationError("model publication has no reload definition")
        regression = reload_definition.get("regression")
        if not isinstance(regression, Mapping):
            raise PublicationError("model publication has no regression policy")
        regression_path = root / PurePosixPath(str(regression["file"]))
        try:
            _, model_regression_suite = load_hub_regression_suite(
                regression_path,
                regression,
            )
        except HubRegressionError as error:
            raise PublicationError(str(error)) from error
        variant_release = _variant_release_binding(root, definition)
        runtime_reload = _verify_runtime_sources(root)
    else:
        reload_definition = definition.get("reload")
        if not isinstance(reload_definition, Mapping):
            raise PublicationError("dataset publication has no reload definition")
        split_files = reload_definition.get("split_files")
        if not isinstance(split_files, Mapping):
            raise PublicationError("dataset publication has no split file map")
        dataset_split_inventory = {
            "release": _jsonl_split_inventory(
                root,
                split_files,
                label="dataset release",
            )
        }
        ai_gold_split_files = reload_definition.get("ai_gold_split_files")
        if isinstance(ai_gold_split_files, Mapping):
            dataset_split_inventory["ai_gold"] = _jsonl_split_inventory(
                root,
                ai_gold_split_files,
                label="dataset AI-Gold",
            )
    checksums = _materialize_checksums(root)
    files = _inventory(root, excluded=frozenset({manifest_filename}))
    _assert_required_patterns(files, patterns, label=repo_type)
    manifest_path = root / manifest_filename
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PublicationError(f"refusing to replace an immutable manifest: {manifest_path.name}") from error
        existing_payload = existing_manifest.get("payload") if isinstance(existing_manifest, Mapping) else None
        if not isinstance(existing_payload, Mapping) or existing_payload.get("files") != files:
            raise PublicationError(f"refusing to replace an immutable manifest: {manifest_path.name}")
    payload = {
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "tree_sha256": _tree_hash(files),
        "files": files,
    }
    manifest = {
        "schema_version": 4,
        "artifact_kind": repo_type,
        "repo_id": definition["repo_id"],
        "visibility": "private",
        "privacy": copy.deepcopy(_EXPECTED_PRIVACY),
        "payload": payload,
        "legal_files": legal_files,
        "checksums": {
            "status": "verified",
            "manifest_sha256": _sha256_path(root / CHECKSUMS_FILENAME),
            "payload_tree_sha256": checksums["payload_tree_sha256"],
            "file_count": len(checksums["files"]),
        },
    }
    if repo_type == "model":
        try:
            protection_policy = verify_frozen_lexical_policy_artifact(
                root,
                expected=definition.get("protection_policy"),
            )
        except PolicyArtifactError as error:
            raise PublicationError(str(error)) from error
        manifest["protection_policy"] = protection_policy
        manifest["model_regression_suite"] = model_regression_suite
        manifest["variant_release"] = variant_release
        manifest["runtime_reload"] = runtime_reload
    if repo_type == "dataset":
        reload_definition = definition.get("reload")
        if not isinstance(reload_definition, Mapping):
            raise PublicationError("dataset publication has no reload definition")
        split_files = reload_definition.get("split_files")
        if not isinstance(split_files, Mapping):
            raise PublicationError("dataset publication has no split file map")
        split_counts = {
            "release": _jsonl_split_counts(root, split_files, label="dataset release"),
        }
        ai_gold_split_files = reload_definition.get("ai_gold_split_files")
        if isinstance(ai_gold_split_files, Mapping):
            split_counts["ai_gold"] = _jsonl_split_counts(
                root,
                ai_gold_split_files,
                label="dataset AI-Gold",
            )
        manifest["dataset_split_counts"] = split_counts
        manifest["dataset_split_inventory"] = dataset_split_inventory
    _write_immutable(
        manifest_path,
        _canonical_json(manifest, pretty=True),
        label="manifest",
    )
    return manifest


def prepare_publication_bundle(
    config: Mapping[str, Any],
    *,
    artifact_dirs: Mapping[str, str | Path],
    completed_training_report_path: str | Path,
    selected_checkpoint_publication_path: str | Path,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Prepare and bind both local publication folders without contacting the Hub."""

    validated = validate_publication_config(config)
    if set(artifact_dirs) != set(_ARTIFACT_KINDS):
        raise ValueError("artifact_dirs must contain exactly model and dataset")
    for kind in _ARTIFACT_KINDS:
        artifact = Path(artifact_dirs[kind])
        if artifact.is_symlink() or not artifact.is_dir():
            raise PublicationError(f"{kind} publication artifact must be a real local directory")
    training_binding = _completed_training_binding(completed_training_report_path)
    model_root = Path(artifact_dirs["model"]).resolve()
    selected_checkpoint = _selected_checkpoint_binding(
        selected_checkpoint_publication_path,
        training_binding=training_binding,
        model_root=model_root,
    )
    try:
        model_policy = verify_frozen_lexical_policy_artifact(
            model_root,
            expected=validated["artifacts"]["model"]["protection_policy"],
        )
    except PolicyArtifactError as error:
        raise PublicationError(str(error)) from error
    if model_policy != training_binding["protection_policy"]:
        raise PublicationError("model protection policy differs from completed training evidence")
    _variant_release_binding(
        model_root,
        validated["artifacts"]["model"],
        training_fingerprint=training_binding["training_run_fingerprint"],
    )
    _write_immutable(
        model_root / TRAINING_BINDING_FILENAME,
        _canonical_json(training_binding, pretty=True),
        label="completed training binding",
    )
    _write_immutable(
        model_root / SELECTED_CHECKPOINT_BINDING_FILENAME,
        _canonical_json(selected_checkpoint, pretty=True),
        label="selected checkpoint binding",
    )
    manifests = {
        kind: prepare_publication_manifest(
            artifact_dirs[kind],
            validated["artifacts"][kind],
            manifest_filename=validated["manifest_filename"],
        )
        for kind in _ARTIFACT_KINDS
    }
    if (
        manifests["model"]["variant_release"]["training_fingerprint"]
        != training_binding["training_run_fingerprint"]
    ):
        raise PublicationError(
            "model variant release evidence differs from completed training"
        )
    artifacts = {
        kind: {
            "repo_id": manifests[kind]["repo_id"],
            "manifest_sha256": _sha256_json(manifests[kind], pretty=True),
            "payload_tree_sha256": manifests[kind]["payload"]["tree_sha256"],
            "file_count": manifests[kind]["payload"]["file_count"],
            "total_size_bytes": manifests[kind]["payload"]["total_size_bytes"],
        }
        for kind in _ARTIFACT_KINDS
    }
    binding = {
        "status": "prepared_private",
        "namespace": validated["namespace"],
        "visibility": "private",
        **training_binding,
        **selected_checkpoint,
        "artifacts": artifacts,
        "schema_version": 2,
    }
    binding["bundle_sha256"] = _sha256_json(binding)
    _validate_local_preparation_schema(binding)
    if report_path is not None:
        _write_immutable(
            Path(report_path),
            _canonical_json(binding, pretty=True),
            label="local publication preparation report",
        )
    return binding


def _upload_inventory(
    artifact_dir: str | Path,
    *,
    manifest_filename: str,
) -> list[dict[str, Any]]:
    files = _inventory(Path(artifact_dir).resolve())
    if manifest_filename not in {str(item["path"]) for item in files}:
        raise PublicationError("prepared publication manifest is missing from upload inventory")
    return files


def _expected_file_map(files: list[dict[str, Any]]) -> dict[str, tuple[int, str]]:
    expected: dict[str, tuple[int, str]] = {}
    for item in files:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or isinstance(item.get("size_bytes"), bool)
            or not isinstance(item.get("size_bytes"), int)
            or not isinstance(item.get("sha256"), str)
            or not _HEX_SHA256.fullmatch(item["sha256"])
        ):
            raise PublicationError("publication inventory contains an invalid file entry")
        expected[item["path"]] = (item["size_bytes"], item["sha256"])
    if len(expected) != len(files):
        raise PublicationError("publication inventory contains duplicate file paths")
    return expected


def _assert_snapshot_matches(snapshot: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = _expected_file_map(files)
    verified: list[dict[str, Any]] = []
    for relative, (size, expected_hash) in expected.items():
        path = snapshot / PurePosixPath(relative)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or _sha256_path(path) != expected_hash
        ):
            raise PublicationError(f"snapshot payload differs from upload manifest: {relative}")
        verified.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": expected_hash,
                "snapshot_sha256_verified": True,
            }
        )
    observed_extra: list[str] = []
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(snapshot).as_posix()
        if (
            relative in expected
            or relative in _IGNORED_SNAPSHOT_FILES
            or relative.startswith(_IGNORED_SNAPSHOT_PREFIXES)
        ):
            continue
        observed_extra.append(relative)
    if observed_extra:
        raise PublicationError(f"snapshot payload contains an unmanifested file: {observed_extra[0]}")
    return verified


@contextmanager
def _offline_environment() -> Any:
    updates = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NO_PROXY": "*",
    }
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def offline_reload_model(
    snapshot: str | Path,
    definition: Mapping[str, Any],
    *,
    model_loader: Callable[..., Any] | None = None,
    tokenizer_loader: Callable[..., Any] | None = None,
    regression_runner: Callable[
        [Path, Mapping[str, Any], Any, Any], Mapping[str, Any]
    ]
    | None = None,
) -> dict[str, Any]:
    """Reload the exact downloaded model without any network fallback."""

    root = Path(snapshot).resolve()
    reload_definition = definition.get("reload")
    if not isinstance(reload_definition, Mapping) or reload_definition.get("kind") != "transformers_local":
        raise PublicationError("model reload definition is not transformers_local")
    if not root.is_dir():
        raise PublicationError("downloaded model snapshot is missing")
    try:
        protection_policy = verify_frozen_lexical_policy_artifact(
            root,
            expected=definition.get("protection_policy"),
        )
    except PolicyArtifactError as error:
        raise PublicationError(str(error)) from error
    if model_loader is None or tokenizer_loader is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_loader = model_loader or AutoModelForCausalLM.from_pretrained
        tokenizer_loader = tokenizer_loader or AutoTokenizer.from_pretrained
    local_path = str(root)
    tokenizer = tokenizer_loader(
        local_path,
        local_files_only=True,
        fix_mistral_regex=False,
    )
    vocab_size = getattr(tokenizer, "vocab_size", None)
    if isinstance(vocab_size, bool) or not isinstance(vocab_size, int) or vocab_size <= 0:
        try:
            vocab_size = len(tokenizer)
        except (TypeError, AttributeError) as error:
            raise PublicationError("reloaded tokenizer has no valid vocabulary") from error
    if isinstance(vocab_size, bool) or not isinstance(vocab_size, int) or vocab_size <= 0:
        raise PublicationError("reloaded tokenizer has no valid vocabulary")
    try:
        runtime_policy = verify_frozen_lexical_policy_artifact(
            root,
            tokenizer=tokenizer,
            expected=definition.get("protection_policy"),
        )
    except PolicyArtifactError as error:
        raise PublicationError(str(error)) from error
    if runtime_policy != protection_policy:
        raise PublicationError("runtime tokenizer protection policy differs from the downloaded artifact")
    model = model_loader(
        local_path,
        local_files_only=True,
        dtype="auto",
    )
    model.eval()
    model_type = getattr(getattr(model, "config", None), "model_type", None)
    if not isinstance(model_type, str) or not model_type:
        raise PublicationError("reloaded model has no model_type")
    model_config = getattr(model, "config", None)
    if (
        getattr(model_config, "scriber_protection_policy_id", None) != protection_policy["policy_id"]
        or getattr(model_config, "scriber_protection_policy_sha256", None) != protection_policy["policy_sha256"]
    ):
        raise PublicationError("reloaded model config does not bind the frozen protection policy")
    parameter_count = model.num_parameters()
    if isinstance(parameter_count, bool) or not isinstance(parameter_count, int) or parameter_count <= 0:
        raise PublicationError("reloaded model has an invalid parameter count")
    legal_files = _verify_legal_files(root, definition, artifact_kind="model")
    checksums = _verify_checksums(root)
    variant_release = _variant_release_binding(root, definition)
    runtime_reload = _verify_runtime_sources(root)
    regression_policy = reload_definition.get("regression")
    if not isinstance(regression_policy, Mapping):
        raise PublicationError("model reload has no regression policy")
    if regression_runner is None:
        from .inference import generate_prediction

        def predictor(case: dict[str, Any]) -> Mapping[str, object]:
            prediction = generate_prediction(
                str(case["source_text"]),
                tokenizer=tokenizer,
                model=model,
                max_new_tokens=int(regression_policy["max_new_tokens"]),
            )
            return {
                "prediction_sst": prediction.prediction_sst,
                "valid_sst": prediction.valid_sst,
                "restoration_error": prediction.restoration_error,
                "generation_seconds": prediction.generation_seconds,
            }

        try:
            regression = run_hub_regression_suite(
                root / PurePosixPath(str(regression_policy["file"])),
                regression_policy,
                predictor=predictor,
            )
        except HubRegressionError as error:
            raise PublicationError(str(error)) from error
    else:
        regression = dict(regression_runner(root, definition, tokenizer, model))
        if regression.get("status") != "passed":
            raise PublicationError("injected model regression runner did not pass")
    return {
        "status": "passed",
        "loader": "transformers_local",
        "tokenizer_class": type(tokenizer).__name__,
        "tokenizer_vocab_size": vocab_size,
        "model_type": model_type,
        "parameter_count": parameter_count,
        "protection_policy": protection_policy,
        "legal_files": legal_files,
        "checksums": checksums,
        "variant_release": variant_release,
        "runtime_reload": runtime_reload,
        "regression": regression,
    }


def offline_reload_dataset(
    snapshot: str | Path,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse every downloaded JSONL split and enforce AI-only provenance."""

    root = Path(snapshot).resolve()
    reload_definition = definition.get("reload")
    if not isinstance(reload_definition, Mapping) or reload_definition.get("kind") != "jsonl_splits":
        raise PublicationError("dataset reload definition is not jsonl_splits")
    split_files = reload_definition.get("split_files")
    if not isinstance(split_files, Mapping) or set(split_files) != {
        "train",
        "validation",
        "test",
        "challenge",
    }:
        raise PublicationError("dataset reload requires all four splits")
    ai_gold_split_files = reload_definition.get("ai_gold_split_files")
    if ai_gold_split_files is not None and (
        not isinstance(ai_gold_split_files, Mapping)
        or set(ai_gold_split_files) != {"train", "validation", "test", "challenge"}
    ):
        raise PublicationError("dataset AI-Gold reload requires all four splits")
    seen_ids: set[str] = set()

    def load_group(files: Mapping[str, Any], *, group: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for split in sorted(files):
            relative = _safe_relative_path(files[split], f"dataset {group} split {split}")
            path = root / PurePosixPath(relative)
            if not path.is_file() or path.is_symlink():
                raise PublicationError(f"dataset {group} split is missing: {split}")
            count = 0
            with path.open(encoding="utf-8") as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    line = raw_line.rstrip("\r\n")
                    if not line:
                        raise PublicationError(f"dataset {group} split {split} has a blank line at {line_number}")
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise PublicationError(
                            f"dataset {group} split {split} has invalid JSON at {line_number}"
                        ) from error
                    if not isinstance(record, Mapping) or record.get("split") != split:
                        raise PublicationError(f"dataset {group} split declaration mismatch in {split}")
                    if (
                        "private_data" in record
                        and record.get("private_data") is not False
                    ):
                        raise PublicationError(
                            "dataset record has an unsafe private-data declaration"
                        )
                    example_id = record.get("example_id")
                    scoped_id = f"{group}\0{example_id}"
                    if not isinstance(example_id, str) or not example_id or scoped_id in seen_ids:
                        raise PublicationError("dataset example identifiers are missing or duplicated")
                    seen_ids.add(scoped_id)
                    provenance = record.get("provenance")
                    if (
                        not isinstance(provenance, Mapping)
                        or provenance.get("synthetic_only") is not True
                        or provenance.get("human_curation") is not False
                        or provenance.get("generative_api") is not False
                    ):
                        raise PublicationError("dataset record is not proven synthetic AI-only data")
                    generator_fields = (
                        "canonical_generator_id",
                        "canonical_generator_version",
                        "plan_generator_agent",
                        "target_writer_agent",
                    )
                    if any(
                        not isinstance(provenance.get(field), str) or not provenance[field]
                        for field in generator_fields
                    ):
                        raise PublicationError("dataset record lacks complete generator provenance")
                    decisions = record.get("critic_decisions")
                    if (
                        not isinstance(decisions, list)
                        or not decisions
                        or not all(isinstance(item, Mapping) and item.get("acceptable") is True for item in decisions)
                    ):
                        raise PublicationError("dataset record has no complete acceptable critic decisions")
                    scores = record.get("validation_scores")
                    if not isinstance(scores, Mapping) or scores.get("deterministic") != 1.0:
                        raise PublicationError("dataset record lacks deterministic validation evidence")
                    required_record_fields = (
                        "semantic_plan",
                        "target_ast",
                        "target_sst",
                        "target_plain_text",
                    )
                    if any(record.get(field) in (None, "", {}) for field in required_record_fields):
                        raise PublicationError("dataset record lacks a required semantic or target representation")
                    semantic_plan = record.get("semantic_plan")
                    entities = (
                        semantic_plan.get("entities")
                        if isinstance(semantic_plan, Mapping)
                        else None
                    )
                    if isinstance(entities, list) and any(
                        not isinstance(entity, Mapping)
                        or entity.get("fictional") is not True
                        for entity in entities
                    ):
                        raise PublicationError(
                            "dataset semantic plan contains a non-fictional entity"
                        )
                    count += 1
            if count <= 0:
                raise PublicationError(f"dataset {group} split is empty: {split}")
            counts[split] = count
        return counts

    counts = load_group(split_files, group="release")
    result: dict[str, Any] = {
        "status": "passed",
        "loader": "jsonl_splits",
        "split_counts": counts,
        "split_inventory": _jsonl_split_inventory(
            root,
            split_files,
            label="dataset release",
        ),
        "legal_files": _verify_legal_files(
            root,
            definition,
            artifact_kind="dataset",
        ),
        "checksums": _verify_checksums(root),
    }
    if isinstance(ai_gold_split_files, Mapping):
        result["ai_gold_split_counts"] = load_group(ai_gold_split_files, group="ai_gold")
        result["ai_gold_split_inventory"] = _jsonl_split_inventory(
            root,
            ai_gold_split_files,
            label="dataset AI-Gold",
        )
    return result


def _preflight_write_identity(
    api: Any,
    *,
    namespace: str,
    token: str,
) -> dict[str, Any]:
    try:
        identity = api.whoami(token=token)
    except Exception as error:
        raise PublicationError("Hub token identity preflight failed") from error
    if not isinstance(identity, Mapping):
        raise PublicationError("Hub token identity preflight returned no structured identity")
    name = identity.get("name")
    if not isinstance(name, str) or not name:
        raise PublicationError("Hub token identity preflight returned no account name")
    access = identity.get("auth")
    token_info = access.get("accessToken") if isinstance(access, Mapping) else None
    token_role = token_info.get("role") if isinstance(token_info, Mapping) else None
    if token_role != "write":
        raise PublicationError("HF_TOKEN does not prove global write permission")

    namespace_authorized = name.casefold() == namespace.casefold()
    if not namespace_authorized:
        organizations = identity.get("orgs")
        if isinstance(organizations, list):
            for organization in organizations:
                if not isinstance(organization, Mapping):
                    continue
                organization_name = organization.get("name")
                organization_role = organization.get("roleInOrg", organization.get("role"))
                if (
                    isinstance(organization_name, str)
                    and organization_name.casefold() == namespace.casefold()
                    and organization_role in {"admin", "write"}
                ):
                    namespace_authorized = True
                    break
    if not namespace_authorized:
        raise PublicationError("Hub identity is not authorized for the configured namespace")
    return {
        "status": "passed",
        "token_role": "write",
        "namespace_authorized": True,
        "identity_sha256": hashlib.sha256(name.casefold().encode("utf-8")).hexdigest(),
    }


def _repo_info(
    api: Any,
    kind: str,
    repo_id: str,
    token: str,
    *,
    revision: str | None = None,
) -> Any:
    loader = api.model_info if kind == "model" else api.dataset_info
    kwargs: dict[str, Any] = {"token": token, "files_metadata": True}
    if revision is not None:
        kwargs["revision"] = revision
    info = loader(repo_id, **kwargs)
    if getattr(info, "id", repo_id) != repo_id or getattr(info, "private", None) is not True:
        raise PublicationError(f"{kind} repository is not confirmed private")
    return info


def _remote_inventory(
    info: Any,
    expected_files: list[dict[str, Any]],
    *,
    source_root: Path,
) -> list[dict[str, Any]]:
    siblings = getattr(info, "siblings", None)
    if not isinstance(siblings, list):
        raise PublicationError("Hub repository info has no file inventory")
    remote = {str(item.rfilename): item for item in siblings if isinstance(getattr(item, "rfilename", None), str)}
    if not remote:
        raise PublicationError("Hub repository file inventory is empty")
    expected = _expected_file_map(expected_files)
    missing = sorted(set(expected).difference(remote))
    if missing:
        raise PublicationError(f"Hub repository is missing uploaded file: {missing[0]}")
    extras = sorted(set(remote).difference(expected).difference(_ALLOWED_REMOTE_EXTRA_FILES))
    if extras:
        raise PublicationError(f"Hub repository contains an unmanifested file: {extras[0]}")

    verified: list[dict[str, Any]] = []
    for relative in sorted(expected):
        expected_size, expected_sha256 = expected[relative]
        source = source_root / PurePosixPath(relative)
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size != expected_size
            or _sha256_path(source) != expected_sha256
        ):
            raise PublicationError(f"local upload source changed before Hub verification: {relative}")
        sibling = remote[relative]
        remote_size = getattr(sibling, "size", None)
        if isinstance(remote_size, bool) or not isinstance(remote_size, int) or remote_size != expected_size:
            raise PublicationError(f"Hub file size does not match local artifact: {relative}")
        lfs = getattr(sibling, "lfs", None)
        lfs_sha256 = lfs.get("sha256") if isinstance(lfs, Mapping) else getattr(lfs, "sha256", None)
        if lfs_sha256 is not None:
            if lfs_sha256 != expected_sha256:
                raise PublicationError(f"Hub LFS SHA-256 does not match local artifact: {relative}")
            hash_evidence = "hub_lfs_sha256"
            hub_hash = lfs_sha256
        else:
            blob_id = getattr(sibling, "blob_id", None)
            if not isinstance(blob_id, str) or not blob_id:
                raise PublicationError(f"Hub file metadata has no blob identity: {relative}")
            digest = hashlib.sha1(usedforsecurity=False)
            digest.update(f"blob {expected_size}\0".encode("ascii"))
            with source.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            expected_blob_id = digest.hexdigest()
            if blob_id != expected_blob_id:
                raise PublicationError(f"Hub Git blob differs from local artifact: {relative}")
            hash_evidence = "git_blob_sha1"
            hub_hash = blob_id
        verified.append(
            {
                "path": relative,
                "size_bytes": expected_size,
                "expected_sha256": expected_sha256,
                "hub_hash_kind": hash_evidence,
                "hub_hash": hub_hash,
                "remote_size_verified": True,
                "remote_identity_verified": True,
            }
        )
    return verified


def _sanitized_worker_environment(cache_root: Path) -> dict[str, str]:
    credential_markers = ("TOKEN", "API_KEY", "PASSWORD", "SECRET")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in credential_markers)
    }
    environment.update(
        {
            "HF_HOME": str(cache_root / "hf-home"),
            "HF_HUB_CACHE": str(cache_root / "hub"),
            "TRANSFORMERS_CACHE": str(cache_root / "transformers"),
            "HF_DATASETS_CACHE": str(cache_root / "datasets"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "NO_PROXY": "*",
            "SCRIBER_HUB_RELOAD_DEPENDENCY_ROOTS_JSON": json.dumps(
                sorted(
                    {
                        str(Path(entry).resolve())
                        for entry in sys.path
                        if entry
                        and Path(entry).is_dir()
                        and Path(entry).name in {"site-packages", "dist-packages"}
                    }
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    )
    return environment


def isolated_reload(
    kind: str,
    snapshot: str | Path,
    definition: Mapping[str, Any],
    *,
    cache_root: str | Path,
    worker_path: str | Path = _WORKER_PATH,
    timeout_seconds: int = 1800,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Reload one exact local snapshot in a fresh isolated interpreter and cache."""

    if kind not in _ARTIFACT_KINDS:
        raise ValueError("reload kind must be model or dataset")
    root = Path(snapshot).resolve()
    cache = Path(cache_root).resolve()
    worker = Path(worker_path).resolve()
    if not root.is_dir():
        raise PublicationError(f"{kind} reload snapshot is missing")
    if cache.exists():
        raise PublicationError(f"{kind} reload cache must be fresh")
    if not worker.is_file():
        raise PublicationError("isolated Hub reload worker is missing")
    cache.mkdir(parents=True)
    request = {
        "schema_version": 1,
        "kind": kind,
        "snapshot": str(root),
        "definition": copy.deepcopy(dict(definition)),
    }
    try:
        completed = run_fn(
            [sys.executable, "-I", str(worker)],
            input=json.dumps(request, ensure_ascii=False, sort_keys=True),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=_sanitized_worker_environment(cache),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PublicationError(f"{kind} isolated reload worker could not complete") from error
    if completed.returncode != 0:
        raise PublicationError(f"{kind} isolated reload worker failed with exit code {completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PublicationError(f"{kind} isolated reload worker returned invalid output") from error
    if not isinstance(result, Mapping) or result.get("status") != "passed":
        raise PublicationError(f"{kind} isolated reload worker did not pass")
    return {
        **copy.deepcopy(dict(result)),
        "execution_isolation": "fresh_python_isolated",
        "fresh_cache": True,
        "network_disabled": True,
        "credentials_removed": True,
    }


def _render_verification_markdown(report: Mapping[str, Any], json_sha256: str) -> bytes:
    model = report["artifacts"]["model"]
    dataset = report["artifacts"]["dataset"]
    lines = [
        "# Scriber Hub verification",
        "",
        f"- Status: `{report['status']}`",
        f"- Visibility: `{report['visibility']}`",
        f"- Local bundle SHA-256: `{report['local_bundle_sha256']}`",
        f"- Publication binding SHA-256: `{report['publication_binding_sha256']}`",
        f"- Completed training report: `{report['completed_training_report_sha256']}`",
        f"- Training run: `{report['training_run_fingerprint']}`",
        f"- BF16 final model tree: `{report['bf16_final_model_tree_sha256']}`",
        f"- Selected checkpoint: `{report['selected_candidate_id']}`",
        f"- Selected model SHA-256: `{report['selected_model_sha256']}`",
        f"- Base tokenizer revision: `{report['base_tokenizer']['revision']}`",
        f"- Protection policy: `{report['protection_policy']['policy_id']}` "
        f"(`{report['protection_policy']['artifact_sha256']}`)",
        f"- JSON report SHA-256: `{json_sha256}`",
        "",
        "## Model",
        "",
        f"- Repository: `{model['repo_id']}`",
        f"- Exact revision: `{model['revision']}`",
        f"- Payload tree SHA-256: `{model['payload_tree_sha256']}`",
        f"- Isolated reload: `{model['reload']['status']}`",
        "",
        "## Dataset",
        "",
        f"- Repository: `{dataset['repo_id']}`",
        f"- Exact revision: `{dataset['revision']}`",
        f"- Payload tree SHA-256: `{dataset['payload_tree_sha256']}`",
        f"- Isolated reload: `{dataset['reload']['status']}`",
        "",
        "Both repositories were confirmed private. Every uploaded file was size-bound by Hub metadata. "
        "LFS files were SHA-256-bound directly and ordinary Git files were matched to their exact Git blob IDs. "
        "The unchanged local upload sources were SHA-256-verified and reloaded in fresh offline processes; no "
        "post-upload roundtrip download was performed when verification mode was `local_source_remote_identity`.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def publish_private_bundle(
    config: Mapping[str, Any],
    *,
    artifact_dirs: Mapping[str, str | Path],
    completed_training_report_path: str | Path,
    selected_checkpoint_publication_path: str | Path,
    download_root: str | Path,
    report_path: str | Path,
    report_markdown_path: str | Path | None = None,
    token: str,
    api: Any | None = None,
    snapshot_download_fn: Callable[..., str] | None = None,
    reloaders: Mapping[str, Callable[[Path, Mapping[str, Any]], dict[str, Any]]] | None = None,
    verification_mode: str = "fresh_exact_revision_download",
) -> dict[str, Any]:
    """Upload, pin, verify remote identity, and offline-reload both repositories.

    ``local_source_remote_identity`` deliberately avoids downloading bytes that
    were just uploaded.  It binds Hub LFS objects by SHA-256, ordinary Git
    objects by their exact blob IDs, and reloads the unchanged local upload
    source in a fresh offline process.
    """

    report = Path(report_path).resolve()
    markdown_report = (
        Path(report_markdown_path).resolve() if report_markdown_path is not None else report.with_suffix(".md")
    )
    if report == markdown_report:
        raise ValueError("JSON and Markdown Hub verification reports must be separate files")
    if report.exists() or markdown_report.exists():
        raise PublicationError("refusing to replace an immutable report")
    if verification_mode not in {
        "fresh_exact_revision_download",
        "local_source_remote_identity",
    }:
        raise ValueError("unsupported Hub verification mode")
    fresh_root = Path(download_root).resolve()
    if fresh_root.exists():
        raise PublicationError("fresh verification root already exists")
    validated = validate_publication_config(config)
    if not isinstance(token, str) or not token:
        raise PublicationError("a non-empty Hub token is required")
    if set(artifact_dirs) != set(_ARTIFACT_KINDS):
        raise ValueError("artifact_dirs must contain exactly model and dataset")
    for kind in _ARTIFACT_KINDS:
        artifact = Path(artifact_dirs[kind])
        if artifact.is_symlink() or not artifact.is_dir():
            raise PublicationError(f"{kind} publication artifact must be a real local directory")
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi(token=token)
    if verification_mode == "fresh_exact_revision_download" and snapshot_download_fn is None:
        from huggingface_hub import snapshot_download

        snapshot_download_fn = snapshot_download
    training_binding = _completed_training_binding(completed_training_report_path)
    model_root = Path(artifact_dirs["model"]).resolve()
    selected_checkpoint = _selected_checkpoint_binding(
        selected_checkpoint_publication_path,
        training_binding=training_binding,
        model_root=model_root,
    )
    try:
        model_policy = verify_frozen_lexical_policy_artifact(
            Path(artifact_dirs["model"]).resolve(),
            expected=validated["artifacts"]["model"]["protection_policy"],
        )
    except PolicyArtifactError as error:
        raise PublicationError(str(error)) from error
    if model_policy != training_binding["protection_policy"]:
        raise PublicationError("model protection policy differs from completed training evidence")
    _variant_release_binding(
        model_root,
        validated["artifacts"]["model"],
        training_fingerprint=training_binding["training_run_fingerprint"],
    )
    preflight = _preflight_write_identity(
        api,
        namespace=validated["namespace"],
        token=token,
    )
    _write_immutable(
        model_root / TRAINING_BINDING_FILENAME,
        _canonical_json(training_binding, pretty=True),
        label="completed training binding",
    )
    _write_immutable(
        model_root / SELECTED_CHECKPOINT_BINDING_FILENAME,
        _canonical_json(selected_checkpoint, pretty=True),
        label="selected checkpoint binding",
    )
    manifests = {
        kind: prepare_publication_manifest(
            artifact_dirs[kind],
            validated["artifacts"][kind],
            manifest_filename=validated["manifest_filename"],
        )
        for kind in _ARTIFACT_KINDS
    }
    if (
        manifests["model"]["variant_release"]["training_fingerprint"]
        != training_binding["training_run_fingerprint"]
    ):
        raise PublicationError(
            "model variant release evidence differs from completed training"
        )
    upload_inventories = {
        kind: _upload_inventory(
            artifact_dirs[kind],
            manifest_filename=validated["manifest_filename"],
        )
        for kind in _ARTIFACT_KINDS
    }
    local_bundle = {
        "namespace": validated["namespace"],
        "visibility": "private",
        **training_binding,
        **selected_checkpoint,
        "artifacts": {
            kind: {
                "repo_id": manifests[kind]["repo_id"],
                "manifest_sha256": _sha256_json(manifests[kind], pretty=True),
                "payload_tree_sha256": manifests[kind]["payload"]["tree_sha256"],
                "upload_tree_sha256": _tree_hash(upload_inventories[kind]),
            }
            for kind in _ARTIFACT_KINDS
        },
        "schema_version": 2,
    }
    local_bundle_sha256 = _sha256_json(local_bundle)
    reload_functions = {
        "model": offline_reload_model,
        "dataset": offline_reload_dataset,
        **dict(reloaders or {}),
    }

    # Prove token-level write authorization first, then prove access and privacy
    # for both targets before the first destructive folder synchronization.
    for kind in _ARTIFACT_KINDS:
        definition = validated["artifacts"][kind]
        try:
            api.create_repo(
                definition["repo_id"],
                repo_type=definition["repo_type"],
                private=True,
                exist_ok=True,
                token=token,
            )
            _repo_info(api, kind, definition["repo_id"], token)
        except PublicationError:
            raise
        except Exception as error:
            raise PublicationError(f"{kind} repository preflight failed") from error
        auth_check = getattr(api, "auth_check", None)
        if not callable(auth_check):
            raise PublicationError("Hub client cannot perform repository access preflight")
        try:
            _auth_check_repository_access(
                auth_check,
                repo_id=definition["repo_id"],
                repo_type=definition["repo_type"],
                token=token,
            )
        except Exception as error:
            raise PublicationError(f"{kind} repository access preflight failed") from error
    preflight["repository_access_confirmed"] = True

    artifact_reports: dict[str, Any] = {}
    revisions: dict[str, str] = {}
    remote_inventories: dict[str, list[dict[str, Any]]] = {}
    for kind in _ARTIFACT_KINDS:
        definition = validated["artifacts"][kind]
        root = Path(artifact_dirs[kind]).resolve()
        if _inventory(root) != upload_inventories[kind]:
            raise PublicationError(f"{kind} artifact changed after local publication preflight")
        allowed = [str(item["path"]) for item in upload_inventories[kind]]
        try:
            commit = api.upload_folder(
                folder_path=str(root),
                repo_id=definition["repo_id"],
                repo_type=definition["repo_type"],
                revision="main",
                create_pr=False,
                delete_patterns="*",
                allow_patterns=allowed,
                commit_message="Publish hash-bound private Scriber candidate",
                token=token,
            )
        except Exception as error:
            raise PublicationError(f"{kind} private upload failed") from error
        revision = getattr(commit, "oid", None)
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise PublicationError(f"{kind} upload did not return an exact commit revision")
        try:
            info = _repo_info(
                api,
                kind,
                definition["repo_id"],
                token,
                revision=revision,
            )
        except PublicationError:
            raise
        except Exception as error:
            raise PublicationError(f"{kind} uploaded revision lookup failed") from error
        if getattr(info, "sha", None) != revision:
            raise PublicationError(f"{kind} repository did not resolve the uploaded commit exactly")
        remote_inventories[kind] = _remote_inventory(
            info,
            upload_inventories[kind],
            source_root=root,
        )
        revisions[kind] = revision

    fresh_root.mkdir(parents=True)
    download_cache_root = fresh_root / "_download_cache"
    for kind in _ARTIFACT_KINDS:
        definition = validated["artifacts"][kind]
        if verification_mode == "fresh_exact_revision_download":
            if snapshot_download_fn is None:  # pragma: no cover - guarded above
                raise PublicationError("snapshot download function is unavailable")
            destination = fresh_root / kind
            try:
                snapshot_result = snapshot_download_fn(
                    definition["repo_id"],
                    repo_type=definition["repo_type"],
                    revision=revisions[kind],
                    local_dir=str(destination),
                    cache_dir=str(download_cache_root / kind),
                    token=token,
                    local_files_only=False,
                    force_download=True,
                )
            except Exception as error:
                raise PublicationError(f"{kind} exact-revision download failed") from error
            verified_source = Path(snapshot_result).resolve()
            if verified_source != destination.resolve():
                raise PublicationError(f"{kind} snapshot was downloaded outside the fresh root")
        else:
            verified_source = Path(artifact_dirs[kind]).resolve()
        snapshot_inventory = _assert_snapshot_matches(
            verified_source,
            upload_inventories[kind],
        )
        if reloaders is not None and kind in reloaders:
            with _offline_environment():
                reload_result = reload_functions[kind](verified_source, definition)
            reload_result = {
                **copy.deepcopy(dict(reload_result)),
                "execution_isolation": "in_process_test_override",
                "fresh_cache": True,
                "network_disabled": True,
                "credentials_removed": True,
            }
        else:
            reload_result = isolated_reload(
                kind,
                verified_source,
                definition,
                cache_root=fresh_root / "_reload_cache" / kind,
            )
        if not isinstance(reload_result, Mapping) or reload_result.get("status") != "passed":
            raise PublicationError(f"{kind} offline reload did not pass")
        if reload_result.get("legal_files") != manifests[kind].get("legal_files"):
            raise PublicationError(
                f"downloaded {kind} reload did not verify the legal files"
            )
        if reload_result.get("checksums") != manifests[kind].get("checksums"):
            raise PublicationError(
                f"downloaded {kind} reload did not verify checksums.json"
            )
        if kind == "model":
            if (
                reload_result.get("protection_policy")
                != manifests["model"].get("protection_policy")
            ):
                raise PublicationError(
                    "downloaded model reload did not prove the frozen protection policy"
                )
            if (
                reload_result.get("variant_release")
                != manifests["model"].get("variant_release")
            ):
                raise PublicationError(
                    "downloaded model reload did not verify quantized fresh-reload evidence"
                )
            if (
                reload_result.get("runtime_reload")
                != manifests["model"].get("runtime_reload")
            ):
                raise PublicationError(
                    "downloaded parser and renderer sources differ from executed code"
                )
            regression = reload_result.get("regression")
            expected_regression = manifests["model"].get(
                "model_regression_suite"
            )
            if (
                not isinstance(regression, Mapping)
                or not isinstance(expected_regression, Mapping)
                or regression.get("status") != "passed"
                or any(
                    regression.get(field) != expected_regression.get(field)
                    for field in (
                        "file",
                        "cases_sha256",
                        "case_count",
                        "minimum_case_count",
                        "category_counts",
                        "source_partition_counts",
                        "required_categories",
                        "ai_gold_smoke_count",
                        "ai_gold_smoke_minimum",
                        "private_data",
                        "holdout_data",
                    )
                )
                or regression.get("exact_output_hash_matches")
                != expected_regression.get("case_count")
                or regression.get("sst_round_trip_checks")
                != expected_regression.get("case_count")
            ):
                raise PublicationError(
                    "downloaded model did not pass the exact >=100-case regression suite"
                )
        if kind == "dataset":
            expected_counts = manifests[kind].get("dataset_split_counts")
            expected_inventory = manifests[kind].get("dataset_split_inventory")
            if not isinstance(expected_counts, Mapping):
                raise PublicationError("dataset publication manifest has no split counts")
            if not isinstance(expected_inventory, Mapping):
                raise PublicationError("dataset publication manifest has no split inventory")
            if reload_result.get("split_counts") != expected_counts.get("release"):
                raise PublicationError("downloaded dataset release split counts do not match the upload")
            if reload_result.get("split_inventory") != expected_inventory.get(
                "release"
            ):
                raise PublicationError(
                    "downloaded dataset release split hashes do not match the upload"
                )
            if "ai_gold" in expected_counts and (
                reload_result.get("ai_gold_split_counts") != expected_counts.get("ai_gold")
            ):
                raise PublicationError("downloaded dataset AI-Gold split counts do not match the upload")
            if "ai_gold" in expected_inventory and (
                reload_result.get("ai_gold_split_inventory")
                != expected_inventory.get("ai_gold")
            ):
                raise PublicationError(
                    "downloaded dataset AI-Gold split hashes do not match the upload"
                )
        snapshot_by_path = {item["path"]: item for item in snapshot_inventory}
        verified_remote_files = []
        for item in remote_inventories[kind]:
            verified_remote_files.append(
                {
                    **item,
                    "source_sha256_verified": snapshot_by_path[item["path"]]["snapshot_sha256_verified"],
                }
            )
        artifact_report = {
            "repo_id": definition["repo_id"],
            "repo_type": definition["repo_type"],
            "revision": revisions[kind],
            "manifest_sha256": _sha256_json(manifests[kind], pretty=True),
            "payload_tree_sha256": manifests[kind]["payload"]["tree_sha256"],
            "upload_tree_sha256": _tree_hash(upload_inventories[kind]),
            "uploaded_file_count": len(upload_inventories[kind]),
            "remote_files": verified_remote_files,
            "verification_mode": verification_mode,
            "exact_revision_downloaded": verification_mode == "fresh_exact_revision_download",
            "source_snapshot_verified": True,
            "remote_identity_verified": True,
            "reload": copy.deepcopy(dict(reload_result)),
        }
        if kind == "dataset":
            artifact_report["expected_split_counts"] = copy.deepcopy(manifests[kind]["dataset_split_counts"])
            artifact_report["expected_split_inventory"] = copy.deepcopy(
                manifests[kind]["dataset_split_inventory"]
            )
        artifact_reports[kind] = artifact_report

    publication_binding = {
        "local_bundle_sha256": local_bundle_sha256,
        **training_binding,
        **selected_checkpoint,
        "artifacts": {
            kind: {
                "repo_id": artifact_reports[kind]["repo_id"],
                "revision": artifact_reports[kind]["revision"],
                "manifest_sha256": artifact_reports[kind]["manifest_sha256"],
                "upload_tree_sha256": artifact_reports[kind]["upload_tree_sha256"],
            }
            for kind in _ARTIFACT_KINDS
        },
    }
    result = {
        "schema_version": 6,
        "status": "verified_private",
        "namespace": validated["namespace"],
        "visibility": "private",
        "preflight": preflight,
        "completed_training_report_sha256": training_binding["completed_training_report_sha256"],
        "training_run_fingerprint": training_binding["training_run_fingerprint"],
        "bf16_final_model_tree_sha256": training_binding["bf16_final_model_tree_sha256"],
        "protection_policy": training_binding["protection_policy"],
        "selected_checkpoint_publication_sha256": selected_checkpoint[
            "selected_checkpoint_publication_sha256"
        ],
        "selected_candidate_id": selected_checkpoint["selected_candidate_id"],
        "selected_checkpoint_identity_sha256": selected_checkpoint[
            "selected_checkpoint_identity_sha256"
        ],
        "selected_checkpoint_tree_sha256": selected_checkpoint[
            "selected_checkpoint_tree_sha256"
        ],
        "selected_model_sha256": selected_checkpoint["selected_model_sha256"],
        "selected_config_sha256": selected_checkpoint["selected_config_sha256"],
        "base_tokenizer": copy.deepcopy(selected_checkpoint["base_tokenizer"]),
        "local_bundle_sha256": local_bundle_sha256,
        "publication_binding_sha256": _sha256_json(publication_binding),
        "artifacts": artifact_reports,
    }
    _validate_report_schema(result)
    json_payload = _canonical_json(result, pretty=True)
    markdown_payload = _render_verification_markdown(
        result,
        hashlib.sha256(json_payload).hexdigest(),
    )
    _write_immutable(report, json_payload, label="JSON report")
    _write_immutable(markdown_report, markdown_payload, label="Markdown report")
    return result
