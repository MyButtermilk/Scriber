"""Central, fail-closed identity and inventory contract for every model variant."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .policy_artifact import (
    PolicyArtifactError,
    verify_frozen_lexical_policy_artifact,
)

VARIANTS = ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
VARIANT_MANIFEST_FILENAME = "variant-artifact-manifest.json"
_SCHEMA = Path(__file__).parents[2] / "contracts" / "variant_artifact_manifest_schema.json"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKENIZER_FILENAMES = frozenset(
    {
        "added_tokens.json",
        "chat_template.jinja",
        "special_tokens_map.json",
        "spiece.model",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer_config.json",
    }
)


class VariantArtifactError(RuntimeError):
    """A variant artifact is incomplete, internally inconsistent, or tampered."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise VariantArtifactError(f"artifact entry is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _inventory(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
    included_names: frozenset[str] | None = None,
) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise VariantArtifactError(f"artifact root is not a regular directory: {root}")
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        if included_names is not None and relative not in included_names:
            continue
        if path.is_symlink() or not path.is_file():
            raise VariantArtifactError(f"artifact tree contains a non-regular file: {relative}")
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if not files:
        raise VariantArtifactError(f"artifact inventory is empty: {root}")
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256-path-size-content-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "files": files,
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VariantArtifactError(f"{label} is missing or not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantArtifactError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise VariantArtifactError(f"{label} must contain a JSON object")
    return value


def _model_identity(metadata_root: Path) -> dict[str, str]:
    config = _load_json_object(metadata_root / "config.json", "model config")
    architectures = config.get("architectures")
    if (
        not isinstance(architectures, list)
        or "Gemma3ForCausalLM" not in architectures
        or any(not isinstance(item, str) for item in architectures)
    ):
        raise VariantArtifactError("model config must declare Gemma3ForCausalLM")
    if config.get("model_type") != "gemma3_text":
        raise VariantArtifactError("model config must declare model_type=gemma3_text")
    return {
        "transformers_class": "Gemma3ForCausalLM",
        "model_type": "gemma3_text",
    }


def _tokenizer_identity(metadata_root: Path) -> dict[str, object]:
    config = _load_json_object(metadata_root / "tokenizer_config.json", "tokenizer config")
    chat_template = config.get("chat_template")
    if chat_template is None:
        template_path = metadata_root / "chat_template.jinja"
        if template_path.is_symlink() or not template_path.is_file():
            raise VariantArtifactError("tokenizer chat template must be non-empty")
        try:
            chat_template = template_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise VariantArtifactError(f"tokenizer chat template is invalid: {error}") from error
    if not isinstance(chat_template, str | dict) or not chat_template:
        raise VariantArtifactError("tokenizer chat template must be non-empty")
    existing = frozenset(
        name
        for name in _TOKENIZER_FILENAMES
        if (metadata_root / name).is_file() and not (metadata_root / name).is_symlink()
    )
    if "tokenizer_config.json" not in existing or len(existing) < 2:
        raise VariantArtifactError("tokenizer artifact inventory must contain config and tokenizer data")
    inventory = _inventory(metadata_root, included_names=existing)
    return {
        "chat_template_sha256": _sha256(_canonical_json(chat_template)),
        "tree_sha256": inventory["tree_sha256"],
        "file_count": inventory["file_count"],
        "files": inventory["files"],
    }


def _validate_quantization(variant: str, config: Mapping[str, object]) -> None:
    if variant == "bf16":
        valid = config == {"quant_method": "none", "weight_dtype": "BF16"}
    elif variant == "int8":
        valid = config.get("quant_method") == "bitsandbytes" and config.get("load_in_8bit") is True
    elif variant == "int4_nf4":
        valid = (
            config.get("quant_method") == "bitsandbytes"
            and config.get("load_in_4bit") is True
            and str(config.get("bnb_4bit_quant_type", "")).lower() == "nf4"
            and str(config.get("bnb_4bit_compute_dtype", "")).lower() in {"bfloat16", "torch.bfloat16"}
            and config.get("bnb_4bit_use_double_quant") is True
        )
    else:
        valid = config == {"quant_method": "llama_cpp", "quant_type": variant}
    if not valid:
        raise VariantArtifactError(f"quantization config does not prove variant {variant}")


def verify_saved_bitsandbytes_config(
    artifact_root: str | Path,
    variant: str,
) -> dict[str, object]:
    """Read and validate the quantization config persisted inside one artifact."""

    root = Path(artifact_root).resolve()
    model_config = _load_json_object(root / "config.json", "quantized model config")
    raw = model_config.get("quantization_config")
    if not isinstance(raw, Mapping) or raw.get("quant_method") != "bitsandbytes":
        raise VariantArtifactError("saved model config does not declare bitsandbytes quantization")
    if variant == "int8":
        if raw.get("load_in_8bit") is not True:
            raise VariantArtifactError("saved model config does not prove load_in_8bit=true")
        return {
            "quant_method": "bitsandbytes",
            "load_in_8bit": True,
        }
    if (
        variant != "int4_nf4"
        or raw.get("load_in_4bit") is not True
        or str(raw.get("bnb_4bit_quant_type", "")).lower() != "nf4"
        or str(raw.get("bnb_4bit_compute_dtype", "")).lower() not in {"bfloat16", "torch.bfloat16"}
        or raw.get("bnb_4bit_use_double_quant") is not True
    ):
        raise VariantArtifactError("saved model config does not prove the required NF4 double-quant configuration")
    return {
        "quant_method": "bitsandbytes",
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_compute_dtype": "bfloat16",
        "bnb_4bit_use_double_quant": True,
    }


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))


def _validate_schema(value: Mapping[str, object]) -> None:
    errors = sorted(
        _validator().iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise VariantArtifactError(f"variant artifact manifest schema failed at {path}: {errors[0].message}")


def build_variant_artifact_manifest(
    artifact_root: str | Path,
    *,
    variant: str,
    backend: str,
    source_id: str,
    source_tree_sha256: str,
    training_fingerprint: str,
    quantization: Mapping[str, object],
    toolchain: Mapping[str, object],
    reload_evidence: Mapping[str, object],
    metadata_root: str | Path | None = None,
) -> dict[str, object]:
    """Build, validate, atomically write, and re-verify one immutable manifest."""

    root = Path(artifact_root).resolve()
    metadata = Path(metadata_root).resolve() if metadata_root is not None else root
    if variant not in VARIANTS:
        raise VariantArtifactError(f"unsupported model variant: {variant}")
    if not source_id.strip():
        raise VariantArtifactError("source_id must be non-empty")
    if not _HASH_RE.fullmatch(source_tree_sha256):
        raise VariantArtifactError("source tree hash must use sha256:<64 lowercase hex>")
    if not _HASH_RE.fullmatch(training_fingerprint):
        raise VariantArtifactError("training fingerprint must use sha256:<64 lowercase hex>")
    quantization_config = dict(quantization)
    _validate_quantization(variant, quantization_config)
    architecture = _model_identity(metadata)
    tokenizer_identity = _tokenizer_identity(metadata)
    reload_report = dict(reload_evidence)
    if reload_report.get("process_boundary") != "fresh_subprocess":
        raise VariantArtifactError("reload evidence must use process_boundary=fresh_subprocess")
    if reload_report.get("configuration_source") != "saved_artifact":
        raise VariantArtifactError("reload evidence must use configuration_source=saved_artifact")
    if reload_report.get("loaded_quantization") != variant:
        raise VariantArtifactError("reload evidence reports a different variant")
    try:
        protection_policy = verify_frozen_lexical_policy_artifact(metadata)
    except PolicyArtifactError as error:
        raise VariantArtifactError(str(error)) from error
    if reload_report.get("protection_policy") != protection_policy:
        raise VariantArtifactError("reload evidence does not bind the frozen lexical protection policy")
    manifest_path = root / VARIANT_MANIFEST_FILENAME
    if manifest_path.exists():
        raise VariantArtifactError(f"variant manifest already exists: {manifest_path}")
    manifest: dict[str, object] = {
        "schema_version": 2,
        "variant": variant,
        "backend": backend,
        "architecture": architecture,
        "base_model": {
            "id": BASE_MODEL_ID,
            "revision": BASE_MODEL_REVISION,
        },
        "training_fingerprint": training_fingerprint,
        "source": {
            "id": source_id,
            "tree_sha256": source_tree_sha256,
        },
        "artifact": _inventory(
            root,
            excluded=frozenset({VARIANT_MANIFEST_FILENAME}),
        ),
        "tokenizer": tokenizer_identity,
        "quantization": {
            "config": quantization_config,
            "config_sha256": _sha256(_canonical_json(quantization_config)),
        },
        "protection_policy": protection_policy,
        "toolchain": dict(toolchain),
        "reload": reload_report,
        "network_access": "disabled_offline",
    }
    _validate_schema(manifest)
    payload = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    temporary = manifest_path.with_suffix(".json.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        os.replace(temporary, manifest_path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise VariantArtifactError(f"could not publish variant manifest: {error}") from error
    return verify_variant_artifact_manifest(root)


def verify_variant_artifact_manifest(
    artifact_root: str | Path,
    manifest_path: str | Path | None = None,
) -> dict[str, object]:
    """Recompute all file, architecture, tokenizer, and configuration bindings."""

    root = Path(artifact_root).resolve()
    path = Path(manifest_path).resolve() if manifest_path is not None else root / VARIANT_MANIFEST_FILENAME
    manifest = _load_json_object(path, "variant artifact manifest")
    _validate_schema(manifest)
    expected_inventory = _inventory(
        root,
        excluded=frozenset({path.relative_to(root).as_posix()}),
    )
    if manifest["artifact"] != expected_inventory:
        raise VariantArtifactError("artifact inventory or tree hash does not match current bytes")
    if manifest["architecture"] != _model_identity(root):
        raise VariantArtifactError("artifact architecture identity does not match config.json")
    if manifest["tokenizer"] != _tokenizer_identity(root):
        raise VariantArtifactError("artifact tokenizer or chat template identity does not match")
    try:
        protection_policy = verify_frozen_lexical_policy_artifact(root)
    except PolicyArtifactError as error:
        raise VariantArtifactError(str(error)) from error
    if manifest["protection_policy"] != protection_policy:
        raise VariantArtifactError("manifest protection policy differs from the saved artifact")
    quantization = manifest["quantization"]
    if not isinstance(quantization, Mapping):
        raise VariantArtifactError("quantization manifest section is invalid")
    config = quantization.get("config")
    if not isinstance(config, Mapping):
        raise VariantArtifactError("quantization config is invalid")
    _validate_quantization(str(manifest["variant"]), config)
    if quantization.get("config_sha256") != _sha256(_canonical_json(dict(config))):
        raise VariantArtifactError("quantization config hash does not match")
    reload_evidence = manifest["reload"]
    if not isinstance(reload_evidence, Mapping):
        raise VariantArtifactError("reload evidence is invalid")
    if reload_evidence.get("loaded_quantization") != manifest["variant"]:
        raise VariantArtifactError("reload evidence reports a different variant")
    if reload_evidence.get("protection_policy") != protection_policy:
        raise VariantArtifactError("reload evidence protection policy differs from the saved artifact")
    return manifest
