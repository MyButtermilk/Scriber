"""Offline, fail-closed materialization of local polishing-model variants."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol

import yaml
from jsonschema import Draft202012Validator

from .hf_llama_cpp_toolchain import (
    HfLlamaCppToolchainError,
    verify_llama_cpp_source_checkout,
)
from .llama_cpp_bundle import (
    LlamaCppBundleError,
    VerifiedLlamaCppBundle,
    load_verified_llama_cpp_bundle,
)
from .policy_artifact import (
    PolicyArtifactError,
    frozen_lexical_policy_requirement,
    validate_frozen_lexical_policy_requirement,
    verify_frozen_lexical_policy_artifact,
)
from .protected_spans import PROTECTION_POLICY_FILENAME
from .sst_parser import SSTParseError, parse_sst
from .tokenize import POLISHING_INSTRUCTION
from .variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    VariantArtifactError,
    build_variant_artifact_manifest,
    verify_saved_bitsandbytes_config,
    verify_variant_artifact_manifest,
)
from .windows_llama_cpp_runtime import (
    WindowsLlamaCppRuntimeError,
    load_verified_windows_llama_cpp_runtime,
)

_PROJECT_ROOT = Path(__file__).parents[2]
_CONFIG_SCHEMA = _PROJECT_ROOT / "contracts" / "quantization_config_schema.json"
_INVENTORY_SCHEMA = _PROJECT_ROOT / "contracts" / "checkpoint_inventory_schema.json"
_GGUF_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "gguf_manifest_schema.json"
_SHA256_PATTERN = "sha256:"
_TRANSFORMERS_VARIANTS = frozenset({"int8", "int4_nf4"})
_TRAINING_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class QuantizationError(RuntimeError):
    """A local checkpoint cannot be safely inventoried or materialized."""


class BitsAndBytesBackend(Protocol):
    """System boundary for the CUDA/Transformers quantization runtime."""

    @property
    def versions(self) -> Mapping[str, str]: ...

    def materialize(self, source: Path, destination: Path, variant: str) -> None: ...

    def reload_and_self_test(self, destination: Path, variant: str) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded process result returned by the external-tool boundary."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One exact HF-tokenizer chat prompt reused by every llama.cpp self-test."""

    prompt: str
    chat_template_sha256: str
    tokenizer_source: Path | None = None

    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("rendered GGUF self-test prompt must be non-empty")
        if not _TRAINING_FINGERPRINT_RE.fullmatch(self.chat_template_sha256):
            raise ValueError("chat template hash must use sha256:<64 lowercase hex>")
        if self.tokenizer_source is not None and not self.tokenizer_source.is_dir():
            raise ValueError("tokenizer source must be an existing directory")


class CommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> CommandResult: ...


class LocalCommandRunner:
    """No-shell runner with an explicit offline environment and timeout."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env={**os.environ, **environment},
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise QuantizationError(f"external tool could not complete: {error}") from error
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout[-16_000:],
            stderr=completed.stderr[-16_000:],
        )


def _sha256_bytes(payload: bytes) -> str:
    return _SHA256_PATTERN + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash one regular file without following an unreviewed symlink."""

    if path.is_symlink() or not path.is_file():
        raise QuantizationError(f"artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return _SHA256_PATTERN + digest.hexdigest()


def _files(root: Path, *, omit: frozenset[str] = frozenset()) -> list[dict[str, object]]:
    if not root.is_dir():
        raise QuantizationError(f"artifact directory does not exist: {root}")
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in omit:
            continue
        if path.is_symlink() or not path.is_file():
            raise QuantizationError(f"artifact tree contains a non-regular file: {relative}")
        records.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not records:
        raise QuantizationError(f"artifact directory is empty: {root}")
    return records


def _tree_hash(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return _SHA256_PATTERN + digest.hexdigest()


def _safetensors_dtypes(path: Path) -> Counter[str]:
    try:
        with path.open("rb") as stream:
            header_size_bytes = stream.read(8)
            if len(header_size_bytes) != 8:
                raise ValueError("missing eight-byte header length")
            header_size = int.from_bytes(header_size_bytes, "little")
            if header_size <= 1 or header_size > 100 * 1024 * 1024:
                raise ValueError("unsafe header length")
            header = json.loads(stream.read(header_size))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise QuantizationError(f"invalid safetensors header in {path.name}: {error}") from error
    if not isinstance(header, dict):
        raise QuantizationError(f"invalid safetensors header object in {path.name}")
    dtypes: Counter[str] = Counter()
    for tensor_name, metadata in header.items():
        if tensor_name == "__metadata__":
            continue
        if not isinstance(metadata, dict) or not isinstance(metadata.get("dtype"), str):
            raise QuantizationError(f"missing tensor dtype for {tensor_name!r} in {path.name}")
        dtypes[metadata["dtype"]] += 1
    if not dtypes:
        raise QuantizationError(f"no tensors found in {path.name}")
    return dtypes


def inventory_bf16_checkpoint(
    checkpoint: str | Path,
    *,
    source_id: str,
) -> dict[str, object]:
    """Inventory and cryptographically bind an all-BF16 safetensors checkpoint."""

    root = Path(checkpoint).resolve()
    if not source_id.strip():
        raise QuantizationError("source_id must be non-empty")
    if not root.is_dir():
        raise QuantizationError(f"checkpoint directory does not exist: {root}")
    config_path = root / "config.json"
    if not config_path.is_file():
        raise QuantizationError("checkpoint is missing config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QuantizationError(f"checkpoint config.json is invalid: {error}") from error
    if not isinstance(config, dict):
        raise QuantizationError("checkpoint config.json must contain an object")
    declared_dtype = config.get("dtype", config.get("torch_dtype"))
    if declared_dtype not in {"bfloat16", "bf16"}:
        raise QuantizationError(f"checkpoint config does not declare BF16 weights: {declared_dtype!r}")
    weight_paths = sorted(root.glob("*.safetensors"))
    if not weight_paths:
        raise QuantizationError("checkpoint has no root-level safetensors weight files")
    dtypes: Counter[str] = Counter()
    for weight_path in weight_paths:
        dtypes.update(_safetensors_dtypes(weight_path))
    if set(dtypes) != {"BF16"}:
        rendered = ", ".join(f"{name}={count}" for name, count in sorted(dtypes.items()))
        raise QuantizationError(f"not an all-BF16 checkpoint: {rendered}")
    files = _files(root)
    inventory: dict[str, object] = {
        "schema_version": 1,
        "variant": "bf16",
        "format": "transformers_safetensors",
        "source_id": source_id,
        "model_type": str(config.get("model_type", "unknown")),
        "declared_dtype": str(declared_dtype),
        "weight_dtype_counts": dict(sorted(dtypes.items())),
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": _tree_hash(files),
        "files": files,
    }
    _validate(_INVENTORY_SCHEMA, inventory, "BF16 checkpoint inventory")
    return inventory


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate(schema_path: Path, value: Mapping[str, object], label: str) -> None:
    errors = sorted(
        _validator(schema_path).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise QuantizationError(f"{label} schema failed at {path}: {errors[0].message}")


def load_quantization_config(path: str | Path) -> dict[str, Any]:
    """Load the closed quantization/benchmark policy."""

    config_path = Path(path)
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise QuantizationError(f"cannot read quantization config {config_path}: {error}") from error
    if not isinstance(value, dict):
        raise QuantizationError("quantization config must contain an object")
    _validate(_CONFIG_SCHEMA, value, "quantization config")
    try:
        validate_frozen_lexical_policy_requirement(value["protection_policy"])
    except (KeyError, PolicyArtifactError) as error:
        raise QuantizationError("quantization config does not bind the frozen lexical protection policy") from error
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QuantizationError(f"{label} is missing or not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QuantizationError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise QuantizationError(f"{label} must contain a JSON object")
    return value


def _verify_training_fingerprint(checkpoint: Path, training_fingerprint: str) -> None:
    if not _TRAINING_FINGERPRINT_RE.fullmatch(training_fingerprint):
        raise QuantizationError("training_fingerprint must use sha256:<64 lowercase hex>")
    evidence = _read_json_object(
        checkpoint / "reload-verification.json",
        "source checkpoint reload verification",
    )
    if evidence.get("status") != "passed":
        raise QuantizationError("source checkpoint reload verification did not pass")
    if evidence.get("run_fingerprint") != training_fingerprint:
        raise QuantizationError("source checkpoint training fingerprint differs from the requested fingerprint")


def _verify_frozen_policy(checkpoint: Path) -> dict[str, str]:
    try:
        return verify_frozen_lexical_policy_artifact(
            checkpoint,
            expected=frozen_lexical_policy_requirement(),
        )
    except PolicyArtifactError as error:
        raise QuantizationError(str(error)) from error


def _saved_bitsandbytes_config(destination: Path, variant: str) -> dict[str, object]:
    try:
        return verify_saved_bitsandbytes_config(destination, variant)
    except VariantArtifactError as error:
        raise QuantizationError(str(error)) from error


class LocalTransformersBitsAndBytesBackend:
    """Official Transformers/bitsandbytes local-only serialization boundary."""

    @property
    def versions(self) -> Mapping[str, str]:
        return {
            "transformers": _package_version("transformers"),
            "bitsandbytes": _package_version("bitsandbytes"),
            "torch": _package_version("torch"),
        }

    @staticmethod
    def _configuration(variant: str, torch: Any, bits_and_bytes_config: Any) -> Any:
        if variant == "int8":
            return bits_and_bytes_config(load_in_8bit=True)
        if variant == "int4_nf4":
            return bits_and_bytes_config(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        raise QuantizationError(f"unsupported bitsandbytes variant: {variant}")

    def materialize(self, source: Path, destination: Path, variant: str) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        except ImportError as error:
            raise QuantizationError(
                "Transformers, torch, and bitsandbytes training dependencies are required"
            ) from error
        if not torch.cuda.is_available():
            raise QuantizationError("CUDA is required to materialize bitsandbytes variants")
        quantization_config = self._configuration(variant, torch, BitsAndBytesConfig)
        common = {
            "local_files_only": True,
            "trust_remote_code": False,
        }
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                source,
                **common,
                fix_mistral_regex=False,
            )
            model = AutoModelForCausalLM.from_pretrained(
                source,
                **common,
                quantization_config=quantization_config,
                device_map="auto",
                low_cpu_mem_usage=True,
                attn_implementation="sdpa",
            )
            model.eval()
            model.save_pretrained(destination, safe_serialization=True)
            tokenizer.save_pretrained(destination)
        except Exception as error:
            raise QuantizationError(
                f"{variant} materialization is unsupported by the installed "
                f"Transformers/bitsandbytes/model combination: {error}"
            ) from error
        finally:
            if "model" in locals():
                del model
            gc.collect()
            torch.cuda.empty_cache()

    def reload_and_self_test(self, destination: Path, variant: str) -> Mapping[str, object]:
        if variant in _TRANSFORMERS_VARIANTS:
            _saved_bitsandbytes_config(destination, variant)
        elif variant != "bf16":
            raise QuantizationError(f"unsupported fresh-reload variant: {variant}")
        report_fd, report_name = tempfile.mkstemp(
            prefix=f".{destination.name}.{variant}.fresh-reload-",
            suffix=".json",
            dir=destination.parent,
        )
        os.close(report_fd)
        report_file = Path(report_name)
        report_file.unlink(missing_ok=True)
        source_root = str(Path(__file__).parents[1])
        python_path = os.environ.get("PYTHONPATH")
        environment = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "NO_PROXY": "*",
            "PYTHONPATH": (source_root if not python_path else os.pathsep.join((source_root, python_path))),
        }
        command = [
            sys.executable,
            "-m",
            "scriber_polishing.quantized_reload",
            "--artifact",
            str(destination),
            "--variant",
            variant,
            "--output",
            str(report_file),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                shell=False,
                timeout=900,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")[:8000]
                raise QuantizationError(
                    f"{variant} fresh-subprocess reload failed with exit code "
                    f"{completed.returncode}: {detail or 'no diagnostics'}"
                )
            report = _read_json_object(report_file, "fresh-subprocess reload report")
            if report.get("process_boundary") != "fresh_subprocess":
                raise QuantizationError("fresh reload report did not prove a new process boundary")
            if report.get("configuration_source") != "saved_artifact":
                raise QuantizationError("fresh reload report did not prove saved-artifact configuration")
            return report
        except subprocess.TimeoutExpired as error:
            raise QuantizationError(f"{variant} fresh-subprocess reload timed out") from error
        finally:
            report_file.unlink(missing_ok=True)


def materialize_bf16_variant_manifest(
    checkpoint: str | Path,
    *,
    source_id: str,
    training_fingerprint: str,
    reload_probe: Callable[[Path, str], Mapping[str, object]] | None = None,
    package_versions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Apply the central variant contract to the already atomically published BF16 model."""

    source = Path(checkpoint).resolve()
    source_inventory = inventory_bf16_checkpoint(source, source_id=source_id)
    _verify_training_fingerprint(source, training_fingerprint)
    _verify_frozen_policy(source)
    probe = reload_probe or LocalTransformersBitsAndBytesBackend().reload_and_self_test
    reload_evidence = dict(probe(source, "bf16"))
    if (
        reload_evidence.get("loaded_quantization") != "bf16"
        or reload_evidence.get("deterministic") is not True
        or reload_evidence.get("valid_sst") is not True
    ):
        raise QuantizationError("BF16 fresh reload did not prove deterministic valid SST")
    packages = (
        dict(package_versions)
        if package_versions is not None
        else {
            "torch": _package_version("torch"),
            "transformers": _package_version("transformers"),
        }
    )
    try:
        return build_variant_artifact_manifest(
            source,
            variant="bf16",
            backend="transformers",
            source_id=source_id,
            source_tree_sha256=str(source_inventory["tree_sha256"]),
            training_fingerprint=training_fingerprint,
            quantization={
                "quant_method": "none",
                "weight_dtype": "BF16",
            },
            toolchain={
                "kind": "transformers_training",
                "packages": dict(sorted(packages.items())),
            },
            reload_evidence=reload_evidence,
        )
    except VariantArtifactError as error:
        raise QuantizationError(f"BF16 artifact verification failed: {error}") from error


def materialize_bitsandbytes_variant(
    checkpoint: str | Path,
    destination: str | Path,
    *,
    variant: str,
    source_id: str,
    training_fingerprint: str,
    backend: BitsAndBytesBackend | None = None,
) -> dict[str, object]:
    """Materialize, locally reload, hash, and atomically publish INT8 or NF4 INT4."""

    if variant not in _TRANSFORMERS_VARIANTS:
        raise QuantizationError(f"unsupported bitsandbytes variant: {variant}")
    source = Path(checkpoint).resolve()
    output = Path(destination).resolve()
    source_inventory = inventory_bf16_checkpoint(source, source_id=source_id)
    _verify_training_fingerprint(source, training_fingerprint)
    source_policy = _verify_frozen_policy(source)
    if output.exists():
        raise QuantizationError(f"destination already exists; refusing overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = backend or LocalTransformersBitsAndBytesBackend()
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.quantizing-", dir=output.parent)).resolve()
    try:
        runtime.materialize(source, temporary, variant)
        shutil.copy2(
            source / PROTECTION_POLICY_FILENAME,
            temporary / PROTECTION_POLICY_FILENAME,
        )
        if _verify_frozen_policy(temporary) != source_policy:
            raise QuantizationError("materialized variant changed the frozen lexical protection policy")
        quantization_config = _saved_bitsandbytes_config(temporary, variant)
        reload_evidence = dict(runtime.reload_and_self_test(temporary, variant))
        if reload_evidence.get("loaded_quantization") != variant:
            raise QuantizationError("reload self-test reported a different quantization variant")
        if reload_evidence.get("deterministic") is not True:
            raise QuantizationError("reload self-test did not prove deterministic generation")
        if reload_evidence.get("valid_sst") is not True:
            raise QuantizationError("reload self-test did not produce valid non-empty SST")
        manifest = build_variant_artifact_manifest(
            temporary,
            variant=variant,
            backend="transformers_bitsandbytes",
            source_id=source_id,
            source_tree_sha256=str(source_inventory["tree_sha256"]),
            training_fingerprint=training_fingerprint,
            quantization=quantization_config,
            toolchain={
                "kind": "transformers_bitsandbytes",
                "packages": dict(sorted(runtime.versions.items())),
            },
            reload_evidence=reload_evidence,
        )
        os.replace(temporary, output)
        verify_variant_artifact_manifest(output)
        return manifest
    except QuantizationError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except VariantArtifactError as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise QuantizationError(f"{variant} artifact verification failed: {error}") from error
    except Exception as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise QuantizationError(f"{variant} materialization failed: {error}") from error


def _run_checked(
    runner: CommandRunner,
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    label: str,
    timeout_seconds: int = 1800,
) -> CommandResult:
    result = runner.run(
        command,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:1000]
        raise QuantizationError(f"{label} failed with exit code {result.returncode}: {detail or 'no diagnostics'}")
    unsupported = f"{result.stdout}\n{result.stderr}".lower()
    if "unsupported model" in unsupported or "unknown model architecture" in unsupported:
        raise QuantizationError(f"{label} reported unsupported Gemma 3 text architecture")
    return result


def _supported_models(result: CommandResult) -> frozenset[str]:
    """Parse exact converter capability records, never converter source substrings."""

    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    models: set[str] = set()
    if combined:
        try:
            decoded = json.loads(combined)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            models.update(item for item in decoded if isinstance(item, str))
        elif isinstance(decoded, dict):
            for key in ("models", "text_models", "supported_models"):
                values = decoded.get(key)
                if isinstance(values, list):
                    models.update(item for item in values if isinstance(item, str))
        if not models:
            bullet = re.compile(r"^\s*-\s+([A-Za-z0-9_.]+)\s*$")
            for line in combined.splitlines():
                match = bullet.fullmatch(line)
                if match is not None:
                    models.add(match.group(1))
    return frozenset(models)


def render_gguf_self_test_prompt(source: Path) -> RenderedPrompt:
    """Render the exact HF tokenizer template once for all GGUF process probes."""

    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            source,
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        chat_template = getattr(tokenizer, "chat_template", None)
        if not isinstance(chat_template, str | dict) or not chat_template:
            raise QuantizationError("source tokenizer has no non-empty chat template")
        prompt = tokenizer.apply_chat_template(
            [
                {
                    "role": "user",
                    "content": f"{POLISHING_INSTRUCTION}Hallo, das ist ein Test.",
                }
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
    except QuantizationError:
        raise
    except Exception as error:
        raise QuantizationError(f"could not render GGUF self-test prompt: {error}") from error
    if not isinstance(prompt, str) or not prompt:
        raise QuantizationError("source tokenizer returned an empty GGUF self-test prompt")
    return RenderedPrompt(
        prompt=prompt,
        chat_template_sha256=_sha256_bytes(
            json.dumps(
                chat_template,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ),
        tokenizer_source=source.resolve(),
    )


def _llama_self_test_command(
    llama_cli: Path,
    model: Path,
    prompt: str,
) -> list[str]:
    return [
        str(llama_cli),
        "--model",
        str(model),
        "--prompt",
        prompt,
        "--n-predict",
        "128",
        "--temp",
        "0",
        "--seed",
        "0",
        "--no-display-prompt",
        "--simple-io",
        "--no-conversation",
        "--single-turn",
        "--gpu-layers",
        "all",
    ]


def _llama_self_test(
    runner: CommandRunner,
    *,
    llama_cli: Path,
    model: Path,
    prompt: RenderedPrompt,
    cwd: Path,
    environment: dict[str, str],
    label: str,
) -> dict[str, object]:
    outputs: list[str] = []
    output_token_counts: list[int] = []
    token_pattern = re.compile(
        r"^\s*(?:llama_perf_context_print:\s*)?eval time\s*=\s*"
        r"[0-9]+(?:\.[0-9]+)?\s*ms\s*/\s*([1-9][0-9]*)\s*(?:runs?|tokens?)",
        re.IGNORECASE | re.MULTILINE,
    )
    response_summary_pattern = re.compile(
        r"^\s*\[\s*Prompt:\s*[0-9]+(?:\.[0-9]+)?\s*t/s\s*\|\s*"
        r"Generation:\s*[0-9]+(?:\.[0-9]+)?\s*t/s\s*\]\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    for attempt in (1, 2):
        result = _run_checked(
            runner,
            _llama_self_test_command(llama_cli, model, prompt.prompt),
            cwd=cwd,
            environment=environment,
            label=f"{label} deterministic run {attempt}",
            timeout_seconds=300,
        )
        raw_output = result.stdout.strip()
        output = raw_output
        try:
            parse_sst(output)
        except (SSTParseError, ValueError):
            prompt_offset = raw_output.rfind(prompt.prompt)
            if prompt_offset < 0:
                raise QuantizationError(
                    f"{label} output contained neither raw SST nor the exact echoed prompt"
                ) from None
            response = raw_output[prompt_offset + len(prompt.prompt) :]
            summary = response_summary_pattern.search(response)
            if summary is None:
                raise QuantizationError(f"{label} output lacked its bounded response summary") from None
            output = response[: summary.start()].strip()
        if not output:
            raise QuantizationError(f"{label} produced an empty completion")
        try:
            parse_sst(output)
        except (SSTParseError, ValueError) as error:
            raise QuantizationError(f"{label} did not produce valid SST: {error}") from error
        token_match = token_pattern.search(result.stderr)
        if token_match is not None:
            generated_tokens = int(token_match.group(1))
        elif prompt.tokenizer_source is not None:
            try:
                from transformers import AutoTokenizer

                tokenizer = AutoTokenizer.from_pretrained(
                    prompt.tokenizer_source,
                    local_files_only=True,
                    trust_remote_code=False,
                    fix_mistral_regex=False,
                )
                generated_tokens = len(tokenizer.encode(output, add_special_tokens=False))
            except Exception as error:
                raise QuantizationError(
                    f"{label} could not count generated tokens with the bound tokenizer: {error}"
                ) from error
            if generated_tokens < 1:
                raise QuantizationError(f"{label} produced zero tokenizer tokens")
        else:
            raise QuantizationError(f"{label} did not report a non-zero generated-token count")
        outputs.append(output)
        output_token_counts.append(generated_tokens)
    if outputs[0] != outputs[1]:
        raise QuantizationError(f"{label} was not deterministic across fresh process reloads")
    if output_token_counts[0] != output_token_counts[1]:
        raise QuantizationError(f"{label} reported inconsistent generated-token counts")
    return {
        "status": "passed",
        "process_boundary": "fresh_subprocess",
        "configuration_source": "saved_artifact",
        "deterministic": True,
        "valid_sst": True,
        "generated_tokens": output_token_counts[0],
        "invocations": 2,
        "no_conversation": True,
        "prompt_sha256": _sha256_bytes(prompt.prompt.encode("utf-8")),
        "chat_template_sha256": prompt.chat_template_sha256,
        "output_sha256": _sha256_bytes(outputs[0].encode("utf-8")),
    }


_GGUF_METADATA_FILES = frozenset(
    {
        "added_tokens.json",
        "chat_template.jinja",
        "config.json",
        "generation_config.json",
        "special_tokens_map.json",
        "spiece.model",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer_config.json",
        PROTECTION_POLICY_FILENAME,
    }
)


def _copy_gguf_metadata(source: Path, destination: Path) -> None:
    for filename in sorted(_GGUF_METADATA_FILES):
        source_file = source / filename
        if not source_file.exists():
            continue
        if source_file.is_symlink() or not source_file.is_file():
            raise QuantizationError(f"GGUF metadata is not a regular file: {source_file}")
        shutil.copy2(source_file, destination / filename)
    for required in ("config.json", "tokenizer_config.json"):
        if not (destination / required).is_file():
            raise QuantizationError(f"GGUF metadata copy is missing {required}")


def build_gguf_variants(
    checkpoint: str | Path,
    destination: str | Path,
    *,
    source_id: str,
    training_fingerprint: str,
    bundle_root: str | Path | None = None,
    bundle_lock: str | Path | None = None,
    windows_runtime_root: str | Path | None = None,
    windows_runtime_manifest: str | Path | None = None,
    llama_cpp_source: str | Path | None = None,
    python_executable: Path,
    runner: CommandRunner | None = None,
    prompt_renderer: Callable[[Path], RenderedPrompt] = render_gguf_self_test_prompt,
) -> dict[str, object]:
    """Build GGUF variants only through one complete, commit-bound llama.cpp bundle."""

    source = Path(checkpoint).resolve()
    output = Path(destination).resolve()
    source_inventory = inventory_bf16_checkpoint(source, source_id=source_id)
    _verify_training_fingerprint(source, training_fingerprint)
    source_policy = _verify_frozen_policy(source)
    if output.exists():
        raise QuantizationError(f"destination already exists; refusing overwrite: {output}")
    linux_requested = bundle_root is not None or bundle_lock is not None
    windows_requested = any(
        value is not None
        for value in (
            windows_runtime_root,
            windows_runtime_manifest,
            llama_cpp_source,
        )
    )
    if linux_requested == windows_requested:
        raise QuantizationError("select exactly one complete Linux bundle or Windows CUDA runtime")
    expected_cli_version_hash: str | None = None
    expected_server_version_hash: str | None = None
    expected_windows_version: tuple[str, str] | None = None
    if linux_requested:
        if bundle_root is None or bundle_lock is None:
            raise QuantizationError("Linux llama.cpp bundle inputs are incomplete")
        try:
            bundle: VerifiedLlamaCppBundle = load_verified_llama_cpp_bundle(
                bundle_root,
                bundle_lock,
            )
        except LlamaCppBundleError as error:
            raise QuantizationError(f"llama.cpp bundle verification failed: {error}") from error
        convert_path = bundle.convert_hf_to_gguf
        quantize_path = bundle.llama_quantize
        llama_cli_path = bundle.llama_cli
        llama_server_path = bundle.llama_server
        expected_cli_version_hash = str(bundle.build["llama_cli_version_output_sha256"])
        expected_server_version_hash = str(bundle.build["llama_server_version_output_sha256"])
        toolchain_binding = {
            "kind": "llama_cpp_bundle",
            "bundle_lock_sha256": bundle.lock_sha256,
            "llama_cpp_commit": bundle.commit,
            "source_tree_sha256": bundle.source_tree_sha256,
            "build_identity_sha256": bundle.build_identity_sha256,
        }
    else:
        if windows_runtime_root is None or windows_runtime_manifest is None or llama_cpp_source is None:
            raise QuantizationError("Windows llama.cpp runtime inputs are incomplete")
        try:
            windows_runtime = load_verified_windows_llama_cpp_runtime(
                windows_runtime_root,
                windows_runtime_manifest,
            )
            source_binding = verify_llama_cpp_source_checkout(llama_cpp_source)
        except (WindowsLlamaCppRuntimeError, HfLlamaCppToolchainError) as error:
            raise QuantizationError(f"Windows llama.cpp toolchain verification failed: {error}") from error
        source_root = Path(llama_cpp_source).expanduser().resolve()
        convert_path = source_root / "convert_hf_to_gguf.py"
        if convert_path.is_symlink() or not convert_path.is_file():
            raise QuantizationError("Windows GGUF converter source is missing")
        quantize_path = windows_runtime.llama_quantize
        llama_cli_path = windows_runtime.llama_cli
        llama_server_path = windows_runtime.llama_server
        expected_windows_version = (
            windows_runtime.release_tag.removeprefix("b"),
            windows_runtime.commit[:8],
        )
        source_tree_sha256 = str(
            source_binding["source_inventory"]["tree_sha256"]  # type: ignore[index]
        )
        build_identity_payload = (
            json.dumps(
                {
                    "conversion_commit": windows_runtime.conversion_commit,
                    "distribution_lock_sha256": windows_runtime.distribution_lock_sha256,
                    "runtime_commit": windows_runtime.commit,
                    "runtime_tree_sha256": windows_runtime.runtime_tree_sha256,
                    "source_tree_sha256": source_tree_sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        toolchain_binding = {
            "kind": "llama_cpp_bundle",
            "bundle_lock_sha256": windows_runtime.distribution_lock_sha256,
            "llama_cpp_commit": windows_runtime.conversion_commit,
            "source_tree_sha256": source_tree_sha256,
            "build_identity_sha256": _sha256_bytes(build_identity_payload),
        }
    rendered_prompt = prompt_renderer(source)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.gguf-", dir=output.parent)).resolve()
    process_runner = runner or LocalCommandRunner()
    offline_environment = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NO_PROXY": "*",
    }
    try:
        supported_probe = _run_checked(
            process_runner,
            [str(python_executable), str(convert_path), "--print-supported-models"],
            cwd=temporary,
            environment=offline_environment,
            label="GGUF converter supported-models probe",
            timeout_seconds=60,
        )
        supported = _supported_models(supported_probe)
        if "Gemma3ForCausalLM" not in supported:
            raise QuantizationError("GGUF converter capability probe did not list exact Gemma3ForCausalLM support")
        quantize_help = process_runner.run(
            [str(quantize_path), "--help"],
            cwd=temporary,
            environment=offline_environment,
            timeout_seconds=60,
        )
        if quantize_help.returncode not in {0, 1}:
            raise QuantizationError(
                f"llama-quantize help probe failed with unexpected exit code {quantize_help.returncode}"
            )
        help_text = f"{quantize_help.stdout}\n{quantize_help.stderr}"
        for quantization in ("Q8_0", "Q4_K_M"):
            if quantization not in help_text:
                raise QuantizationError(f"llama-quantize help does not advertise required {quantization}")
        cli_version = _run_checked(
            process_runner,
            [str(llama_cli_path), "--version"],
            cwd=temporary,
            environment=offline_environment,
            label="llama-cli version probe",
            timeout_seconds=60,
        )
        version_text = (cli_version.stdout or cli_version.stderr).strip().splitlines()
        if not version_text:
            raise QuantizationError("llama-cli version probe returned no version")
        bounded_version = version_text[0][:200]
        cli_version_payload = cli_version.stdout.encode("utf-8") + b"\0" + cli_version.stderr.encode("utf-8")
        actual_version_hash = _sha256_bytes(cli_version_payload)
        if expected_cli_version_hash is not None and actual_version_hash != expected_cli_version_hash:
            raise QuantizationError("llama-cli version output differs from the locked build identity")
        if expected_windows_version is not None and not all(
            token in f"{cli_version.stdout}\n{cli_version.stderr}" for token in expected_windows_version
        ):
            raise QuantizationError("llama-cli version differs from the locked Windows runtime")
        server_version = _run_checked(
            process_runner,
            [str(llama_server_path), "--version"],
            cwd=temporary,
            environment=offline_environment,
            label="llama-server version probe",
            timeout_seconds=60,
        )
        server_version_text = (server_version.stdout or server_version.stderr).strip().splitlines()
        if not server_version_text:
            raise QuantizationError("llama-server version probe returned no version")
        bounded_server_version = server_version_text[0][:200]
        server_version_payload = server_version.stdout.encode("utf-8") + b"\0" + server_version.stderr.encode("utf-8")
        actual_server_version_hash = _sha256_bytes(server_version_payload)
        if expected_server_version_hash is not None and actual_server_version_hash != expected_server_version_hash:
            raise QuantizationError("llama-server version output differs from the locked build identity")
        if expected_windows_version is not None and not all(
            token in f"{server_version.stdout}\n{server_version.stderr}" for token in expected_windows_version
        ):
            raise QuantizationError("llama-server version differs from the locked Windows runtime")

        bf16_root = temporary / "bf16"
        bf16_root.mkdir()
        bf16_gguf = bf16_root / "model-BF16.gguf"
        _run_checked(
            process_runner,
            [
                str(python_executable),
                str(convert_path),
                str(source),
                "--outfile",
                str(bf16_gguf),
                "--outtype",
                "bf16",
            ],
            cwd=temporary,
            environment=offline_environment,
            label="Gemma 3 BF16 GGUF conversion",
        )
        if not bf16_gguf.is_file() or bf16_gguf.stat().st_size == 0:
            raise QuantizationError("GGUF converter did not produce a non-empty BF16 model")
        bf16_reload = _llama_self_test(
            process_runner,
            llama_cli=llama_cli_path,
            model=bf16_gguf,
            prompt=rendered_prompt,
            cwd=temporary,
            environment=offline_environment,
            label="Gemma 3 BF16 llama-cli runtime self-test",
        )
        bf16_reload["protection_policy"] = source_policy
        bf16_reload["loaded_quantization"] = "bf16"

        _copy_gguf_metadata(source, bf16_root)
        bf16_policy = _verify_frozen_policy(bf16_root)
        if bf16_policy != source_policy:
            raise QuantizationError("BF16 changed the frozen lexical protection policy")
        bf16_reload["protection_policy"] = bf16_policy
        bf16_manifest = build_variant_artifact_manifest(
            bf16_root,
            variant="bf16",
            backend="llama_cpp",
            source_id=source_id,
            source_tree_sha256=str(source_inventory["tree_sha256"]),
            training_fingerprint=training_fingerprint,
            quantization={
                "quant_method": "none",
                "weight_dtype": "BF16",
            },
            toolchain=toolchain_binding,
            reload_evidence=bf16_reload,
        )
        bf16_manifest_path = bf16_root / VARIANT_MANIFEST_FILENAME
        variant_manifests: dict[str, dict[str, object]] = {
            "bf16": {
                "path": bf16_manifest_path.relative_to(temporary).as_posix(),
                "sha256": sha256_file(bf16_manifest_path),
                "artifact_tree_sha256": bf16_manifest["artifact"]["tree_sha256"],
                "reload_self_test": bf16_reload,
            }
        }
        for quantization in ("Q8_0", "Q4_K_M"):
            variant_root = temporary / quantization
            variant_root.mkdir()
            quantized = variant_root / f"model-{quantization}.gguf"
            _run_checked(
                process_runner,
                [str(quantize_path), str(bf16_gguf), str(quantized), quantization],
                cwd=temporary,
                environment=offline_environment,
                label=f"llama.cpp {quantization} quantization",
            )
            if not quantized.is_file() or quantized.stat().st_size == 0:
                raise QuantizationError(f"llama-quantize did not produce non-empty {quantization} output")
            reload_evidence = _llama_self_test(
                process_runner,
                llama_cli=llama_cli_path,
                model=quantized,
                prompt=rendered_prompt,
                cwd=temporary,
                environment=offline_environment,
                label=f"{quantization} llama-cli reload self-test",
            )
            reload_evidence["loaded_quantization"] = quantization
            _copy_gguf_metadata(source, variant_root)
            variant_policy = _verify_frozen_policy(variant_root)
            if variant_policy != source_policy:
                raise QuantizationError(f"{quantization} changed the frozen lexical protection policy")
            reload_evidence["protection_policy"] = variant_policy
            variant_manifest = build_variant_artifact_manifest(
                variant_root,
                variant=quantization,
                backend="llama_cpp",
                source_id=source_id,
                source_tree_sha256=str(source_inventory["tree_sha256"]),
                training_fingerprint=training_fingerprint,
                quantization={
                    "quant_method": "llama_cpp",
                    "quant_type": quantization,
                },
                toolchain=toolchain_binding,
                reload_evidence=reload_evidence,
            )
            manifest_path = variant_root / VARIANT_MANIFEST_FILENAME
            variant_manifests[quantization] = {
                "path": manifest_path.relative_to(temporary).as_posix(),
                "sha256": sha256_file(manifest_path),
                "artifact_tree_sha256": variant_manifest["artifact"]["tree_sha256"],
                "reload_self_test": reload_evidence,
            }
        files = _files(temporary)
        manifest: dict[str, object] = {
            "schema_version": 4,
            "backend": "llama_cpp",
            "source_id": source_id,
            "source_tree_sha256": source_inventory["tree_sha256"],
            "training_fingerprint": training_fingerprint,
            "protection_policy": source_policy,
            "variants": ["bf16", "Q8_0", "Q4_K_M"],
            "artifact_tree_sha256": _tree_hash(files),
            "file_count": len(files),
            "total_bytes": sum(int(item["bytes"]) for item in files),
            "files": files,
            "tools": {
                "convert_hf_to_gguf": {
                    "filename": convert_path.name,
                    "sha256": sha256_file(convert_path),
                },
                "llama_quantize": {
                    "filename": quantize_path.name,
                    "sha256": sha256_file(quantize_path),
                },
                "llama_cli": {
                    "filename": llama_cli_path.name,
                    "sha256": sha256_file(llama_cli_path),
                    "version": bounded_version,
                    "version_output_sha256": actual_version_hash,
                },
                "llama_server": {
                    "filename": llama_server_path.name,
                    "sha256": sha256_file(llama_server_path),
                    "version": bounded_server_version,
                    "version_output_sha256": actual_server_version_hash,
                },
            },
            "toolchain": {key: value for key, value in toolchain_binding.items() if key != "kind"},
            "gemma3_text_support": {
                "supported_models_probe": True,
                "supported_model": "Gemma3ForCausalLM",
                "supported_models_output_sha256": _sha256_bytes(
                    "\n".join(part for part in (supported_probe.stdout, supported_probe.stderr) if part)
                    .strip()
                    .encode("utf-8")
                ),
                "actual_bf16_conversion": True,
                "bf16_runtime_self_test": True,
            },
            "bf16_reload_self_test": bf16_reload,
            "variant_manifests": dict(sorted(variant_manifests.items())),
            "network_access": "disabled_offline",
        }
        _validate(_GGUF_MANIFEST_SCHEMA, manifest, "GGUF manifest")
        _write_json(temporary / "gguf-bundle-manifest.json", manifest)
        os.replace(temporary, output)
        for quantization in ("bf16", "Q8_0", "Q4_K_M"):
            verify_variant_artifact_manifest(output / quantization)
        return manifest
    except QuantizationError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except VariantArtifactError as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise QuantizationError(f"GGUF artifact verification failed: {error}") from error
    except Exception as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise QuantizationError(f"GGUF materialization failed: {error}") from error
