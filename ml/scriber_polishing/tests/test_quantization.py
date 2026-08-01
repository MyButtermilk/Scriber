from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import scriber_polishing.quantization as quantization_module
from scriber_polishing.llama_cpp_bundle import (
    LocalProbeResult,
    materialize_llama_cpp_bundle_lock,
)
from scriber_polishing.quantization import (
    CommandResult,
    QuantizationError,
    RenderedPrompt,
    build_gguf_variants,
    inventory_bf16_checkpoint,
    load_quantization_config,
    materialize_bf16_variant_manifest,
    materialize_bitsandbytes_variant,
)
from scriber_polishing.variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    verify_variant_artifact_manifest,
)

_TRAINING_FINGERPRINT = "sha256:" + "a" * 64
_POLICY_ID = "gemma_lexical_v1"
_POLICY_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
_TOKENIZER_IDENTITY_SHA256 = "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"


def _policy_binding() -> dict[str, str]:
    return {
        "filename": "scriber-protection-policy.json",
        "artifact_sha256": _POLICY_ARTIFACT_SHA256,
        "policy_id": _POLICY_ID,
        "policy_sha256": _POLICY_SHA256,
        "tokenizer_identity_sha256": _TOKENIZER_IDENTITY_SHA256,
    }


def _write_safetensors(path: Path, tensors: dict[str, str]) -> None:
    header = {
        name: {"dtype": dtype, "shape": [1], "data_offsets": [index, index + 2]}
        for index, (name, dtype) in enumerate(tensors.items(), start=0)
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    path.write_bytes(len(header_bytes).to_bytes(8, "little") + header_bytes + b"\0" * 16)


def _checkpoint(root: Path, *, dtype: str = "BF16") -> Path:
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Gemma3ForCausalLM"],
                "model_type": "gemma3_text",
                "torch_dtype": "bfloat16",
                "scriber_protection_policy_id": _POLICY_ID,
                "scriber_protection_policy_sha256": _POLICY_SHA256,
            }
        ),
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "{{ messages | tojson }}"}),
        encoding="utf-8",
    )
    (root / "reload-verification.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "run_fingerprint": _TRAINING_FINGERPRINT,
            }
        ),
        encoding="utf-8",
    )
    shutil.copyfile(
        Path(__file__).parents[1] / "artifacts" / "protection" / "gemma_lexical_v1.json",
        root / "scriber-protection-policy.json",
    )
    _write_safetensors(root / "model.safetensors", {"model.embed_tokens.weight": dtype})
    return root


def test_bf16_checkpoint_inventory_hashes_every_file_and_rejects_mixed_weights(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")

    inventory = inventory_bf16_checkpoint(checkpoint, source_id="final-bf16")

    assert inventory["format"] == "transformers_safetensors"
    assert inventory["variant"] == "bf16"
    assert inventory["source_id"] == "final-bf16"
    assert inventory["weight_dtype_counts"] == {"BF16": 1}
    assert inventory["file_count"] == 6
    assert inventory["tree_sha256"].startswith("sha256:")
    assert {item["path"] for item in inventory["files"]} == {
        "config.json",
        "model.safetensors",
        "reload-verification.json",
        "scriber-protection-policy.json",
        "tokenizer.json",
        "tokenizer_config.json",
    }
    assert all(item["sha256"].startswith("sha256:") for item in inventory["files"])

    mixed = _checkpoint(tmp_path / "mixed", dtype="F32")
    with pytest.raises(QuantizationError, match="not an all-BF16 checkpoint"):
        inventory_bf16_checkpoint(mixed, source_id="mixed")


def test_checked_in_quantization_config_is_closed_and_requires_every_benchmark_bucket() -> None:
    project_root = Path(__file__).parents[1]

    config = load_quantization_config(project_root / "configs" / "quantization.yaml")

    assert config["schema_version"] == 6
    assert config["protection_policy"] == {
        "filename": "scriber-protection-policy.json",
        "artifact_sha256": _POLICY_ARTIFACT_SHA256,
        "policy_id": _POLICY_ID,
        "policy_sha256": _POLICY_SHA256,
    }
    assert config["enabled_variants"] == ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"]
    assert config["transformers_variants"] == ["int8", "int4_nf4"]
    assert config["gguf_variants"] == ["Q8_0", "Q4_K_M"]
    assert config["benchmark"]["length_buckets"] == [50, 150, 300, 500, 1000]
    assert config["benchmark"]["percentiles"] == [50, 95, 99]
    assert config["reevaluate_every_variant"] is True


def test_bf16_uses_the_same_variant_manifest_and_fresh_reload_contract(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")

    manifest = materialize_bf16_variant_manifest(
        checkpoint,
        source_id="final-bf16",
        training_fingerprint=_TRAINING_FINGERPRINT,
        reload_probe=lambda _root, _variant: {
            "status": "passed",
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "loaded_quantization": "bf16",
            "deterministic": True,
            "valid_sst": True,
            "generated_tokens": 8,
            "protection_policy": _policy_binding(),
        },
        package_versions={"torch": "2.11.0", "transformers": "4.57.6"},
    )

    assert manifest["variant"] == "bf16"
    assert manifest["quantization"]["config"] == {
        "quant_method": "none",
        "weight_dtype": "BF16",
    }
    assert manifest["protection_policy"] == _policy_binding()
    assert verify_variant_artifact_manifest(checkpoint) == manifest


class FakeBitsAndBytesBackend:
    versions = {
        "transformers": "4.57.6",
        "bitsandbytes": "0.49.2",
        "torch": "2.11.0",
    }

    def __init__(self) -> None:
        self.materialized: list[tuple[str, Path, Path]] = []
        self.reloaded: list[tuple[str, Path]] = []

    def materialize(self, source: Path, destination: Path, variant: str) -> None:
        self.materialized.append((variant, source, destination))
        if variant == "int8":
            quantization = {
                "quant_method": "bitsandbytes",
                "load_in_8bit": True,
            }
        else:
            quantization = {
                "quant_method": "bitsandbytes",
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": "bfloat16",
                "bnb_4bit_use_double_quant": True,
            }
        (destination / "config.json").write_text(
            json.dumps(
                {
                    "architectures": ["Gemma3ForCausalLM"],
                    "model_type": "gemma3_text",
                    "scriber_protection_policy_id": _POLICY_ID,
                    "scriber_protection_policy_sha256": _POLICY_SHA256,
                    "quantization_config": quantization,
                }
            ),
            encoding="utf-8",
        )
        (destination / "model.safetensors").write_bytes(variant.encode("ascii"))
        (destination / "tokenizer.json").write_text("{}", encoding="utf-8")
        (destination / "tokenizer_config.json").write_text(
            json.dumps({"chat_template": "{{ messages | tojson }}"}),
            encoding="utf-8",
        )

    def reload_and_self_test(self, destination: Path, variant: str) -> dict[str, object]:
        self.reloaded.append((variant, destination))
        return {
            "status": "passed",
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "loaded_quantization": variant,
            "deterministic": True,
            "valid_sst": True,
            "generated_tokens": 1,
            "protection_policy": _policy_binding(),
        }


@pytest.mark.parametrize("variant", ["int8", "int4_nf4"])
def test_bitsandbytes_materialization_is_local_reload_verified_and_manifested(
    tmp_path: Path,
    variant: str,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")
    destination = tmp_path / variant
    backend = FakeBitsAndBytesBackend()

    manifest = materialize_bitsandbytes_variant(
        checkpoint,
        destination,
        variant=variant,
        source_id="final-bf16",
        training_fingerprint=_TRAINING_FINGERPRINT,
        backend=backend,
    )

    assert len(backend.materialized) == 1
    assert backend.materialized[0][0:2] == (variant, checkpoint.resolve())
    assert len(backend.reloaded) == 1
    assert backend.reloaded[0][0] == variant
    assert manifest["variant"] == variant
    assert manifest["backend"] == "transformers_bitsandbytes"
    assert manifest["source"]["tree_sha256"].startswith("sha256:")
    assert manifest["artifact"]["tree_sha256"].startswith("sha256:")
    assert manifest["reload"]["deterministic"] is True
    assert manifest["protection_policy"] == _policy_binding()
    assert (destination / "scriber-protection-policy.json").read_bytes() == (
        checkpoint / "scriber-protection-policy.json"
    ).read_bytes()
    manifest_path = destination / VARIANT_MANIFEST_FILENAME
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert verify_variant_artifact_manifest(destination) == manifest
    assert "sha256:" + hashlib.sha256((destination / "model.safetensors").read_bytes()).hexdigest() == next(
        item["sha256"] for item in manifest["artifact"]["files"] if item["path"] == "model.safetensors"
    )


def test_bitsandbytes_materialization_fails_closed_on_unsupported_backend_and_existing_output(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")

    class UnsupportedBackend(FakeBitsAndBytesBackend):
        def materialize(self, source: Path, destination: Path, variant: str) -> None:
            raise RuntimeError("Gemma3 4-bit serialization is unsupported")

    with pytest.raises(QuantizationError, match="Gemma3 4-bit serialization is unsupported"):
        materialize_bitsandbytes_variant(
            checkpoint,
            tmp_path / "int4",
            variant="int4_nf4",
            source_id="final-bf16",
            training_fingerprint=_TRAINING_FINGERPRINT,
            backend=UnsupportedBackend(),
        )
    assert not (tmp_path / "int4").exists()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "foreign.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(QuantizationError, match="destination already exists"):
        materialize_bitsandbytes_variant(
            checkpoint,
            occupied,
            variant="int8",
            source_id="final-bf16",
            training_fingerprint=_TRAINING_FINGERPRINT,
            backend=FakeBitsAndBytesBackend(),
        )
    assert (occupied / "foreign.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_quantization_rejects_a_source_without_the_frozen_policy_before_backend_use(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")
    (checkpoint / "scriber-protection-policy.json").unlink()
    backend = FakeBitsAndBytesBackend()

    with pytest.raises(QuantizationError, match="protection policy"):
        materialize_bitsandbytes_variant(
            checkpoint,
            tmp_path / "int8",
            variant="int8",
            source_id="final-bf16",
            training_fingerprint=_TRAINING_FINGERPRINT,
            backend=backend,
        )

    assert backend.materialized == []


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.calls.append((tuple(command), environment))
        executable = Path(command[0]).name.lower()
        if "--print-supported-models" in command:
            return CommandResult(
                0,
                "",
                "Supported models:\nTEXT models:\n  - Gemma3ForCausalLM\n  - OtherModel\n",
            )
        if "--help" in command:
            return CommandResult(0, "Q8_0 Q4_K_M", "")
        if "--version" in command:
            return CommandResult(0, "llama.cpp b7000-test", "")
        if executable.startswith("python"):
            output = Path(command[command.index("--outfile") + 1])
            output.write_bytes(b"bf16 gguf")
            return CommandResult(0, "", "converted")
        if "quantize" in executable:
            Path(command[2]).write_bytes(command[3].encode("ascii"))
            return CommandResult(0, "", "quantized")
        if "llama-cli" in executable:
            prompt = command[command.index("--prompt") + 1]
            return CommandResult(
                0,
                (
                    "llama.cpp test UI\n> " + prompt + "\n\n[DOC]\n[P]Test.[/P]\n[/DOC]\n\n"
                    "[ Prompt: 10.0 t/s | Generation: 20.0 t/s ]\nExiting..."
                ),
                ("prompt eval time = 1.00 ms / 12 tokens\neval time = 2.00 ms / 8 runs\n"),
            )
        raise AssertionError(f"unexpected command: {command}")


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _inventory(root: Path, relative_paths: list[str]) -> dict[str, object]:
    files = []
    for relative in sorted(relative_paths):
        payload = (root / relative).read_bytes()
        files.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": _hash(payload),
            }
        )
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode())
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode())
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode())
        digest.update(b"\n")
    return {
        "algorithm": "sha256-path-size-content-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "files": files,
    }


def _elf_x86_64(label: bytes) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")
    return bytes(header) + label


def _llama_bundle(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    (source / ".git").mkdir(parents=True)
    (source / "conversion").mkdir(parents=True)
    (source / "gguf-py").mkdir()
    runtime = root / "runtime" / "app"
    runtime.mkdir(parents=True)
    (source / "convert_hf_to_gguf.py").write_text(
        "# support is discovered through --print-supported-models\n",
        encoding="utf-8",
    )
    (source / "conversion" / "__init__.py").write_text("REGISTRY = {}\n", encoding="utf-8")
    (source / "gguf-py" / "gguf.py").write_text("VERSION = 3\n", encoding="utf-8")
    for name in ("llama-quantize", "llama-cli", "llama-server"):
        (runtime / name).write_bytes(_elf_x86_64(name.encode("ascii")))
    (runtime / "libggml.so.0").write_bytes(_elf_x86_64(b"libggml"))

    class MaterializeProbe:
        def run(self, command, *, cwd, timeout_seconds):
            del timeout_seconds
            if command[:3] == ["git", "rev-parse", "--verify"]:
                return LocalProbeResult(0, b"1" * 40 + b"\n", b"")
            if command == [
                "git",
                "-c",
                "core.fileMode=false",
                "-c",
                "core.autocrlf=true",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]:
                return LocalProbeResult(0, b"", b"")
            if command[:3] == ["git", "remote", "get-url"]:
                return LocalProbeResult(
                    0,
                    b"https://github.com/ggml-org/llama.cpp.git\n",
                    b"",
                )
            if command[-1] == "--version":
                return LocalProbeResult(0, b"llama.cpp b7000-test", b"")
            if command[0] == "ldd":
                library = (cwd / "runtime" / "app" / "libggml.so.0").resolve()
                return LocalProbeResult(
                    0,
                    (
                        f"libggml.so.0 => {library} (0x00007f01)\n"
                        "libc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x00007f02)\n"
                        "linux-vdso.so.1 (0x00007f03)\n"
                    ).encode(),
                    b"",
                )
            raise AssertionError(command)

    lock_path = root / "llama-cpp-bundle-lock.json"
    materialize_llama_cpp_bundle_lock(
        root,
        lock_path,
        expected_commit="1" * 40,
        llama_cpp_version="b7000-test",
        compiler_id="GNU",
        compiler_version="13.3",
        cuda_enabled=True,
        cuda_version="12.8",
        generator="Ninja",
        configuration="Release",
        flags=["-DGGML_CUDA=ON", "-DCMAKE_BUILD_TYPE=Release"],
        oci_repository="ghcr.io/ggml-org/llama.cpp",
        oci_tag="full-cuda12-b7000",
        oci_index_digest="sha256:" + "3" * 64,
        oci_manifest_digest="sha256:" + "4" * 64,
        oci_config_digest="sha256:" + "5" * 64,
        oci_revision="1" * 40,
        oci_layers=[
            {
                "digest": "sha256:" + "6" * 64,
                "compressed_bytes": 162_560_439,
                "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                "purpose": "native_libraries",
            },
            {
                "digest": "sha256:" + "7" * 64,
                "compressed_bytes": 173_450_458,
                "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                "purpose": "tools_and_conversion",
            },
        ],
        runtime_image="pytorch/pytorch@sha256:" + "8" * 64,
        native_runtime_root="runtime/app",
        convert_hf_to_gguf_path="source/convert_hf_to_gguf.py",
        llama_quantize_path="runtime/app/llama-quantize",
        llama_cli_path="runtime/app/llama-cli",
        llama_server_path="runtime/app/llama-server",
        runner=MaterializeProbe(),
    )
    return root, lock_path


def test_gguf_pipeline_uses_bundle_capability_probe_real_conversion_and_deterministic_reload(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")
    bundle_root, bundle_lock = _llama_bundle(tmp_path / "llama-bundle")
    runner = FakeCommandRunner()

    manifest = build_gguf_variants(
        checkpoint,
        tmp_path / "gguf",
        source_id="final-bf16",
        training_fingerprint=_TRAINING_FINGERPRINT,
        bundle_root=bundle_root,
        bundle_lock=bundle_lock,
        python_executable=Path("python.exe"),
        runner=runner,
        prompt_renderer=lambda _source: RenderedPrompt(
            prompt="<start_of_turn>user\nTest<end_of_turn>\n<start_of_turn>model\n",
            chat_template_sha256="sha256:" + "c" * 64,
        ),
    )

    assert manifest["variants"] == ["bf16", "Q8_0", "Q4_K_M"]
    assert manifest["schema_version"] == 4
    assert manifest["protection_policy"] == _policy_binding()
    assert manifest["tools"]["llama_server"]["filename"] == "llama-server"
    assert manifest["tools"]["llama_server"]["version_output_sha256"] == _hash(b"llama.cpp b7000-test\0")
    assert manifest["gemma3_text_support"]["supported_models_probe"] is True
    assert manifest["gemma3_text_support"]["actual_bf16_conversion"] is True
    assert set(manifest["variant_manifests"]) == {"bf16", "Q8_0", "Q4_K_M"}
    for variant in ("bf16", "Q8_0", "Q4_K_M"):
        variant_root = tmp_path / "gguf" / variant
        variant_manifest = verify_variant_artifact_manifest(variant_root)
        assert variant_manifest["protection_policy"] == _policy_binding()
        assert variant_manifest["reload"]["deterministic"] is True
        assert variant_manifest["reload"]["valid_sst"] is True
        assert variant_manifest["reload"]["no_conversation"] is True
    calls = [call for call, _environment in runner.calls]
    capability_call = next(call for call in calls if "--print-supported-models" in call)
    assert Path(capability_call[1]).name == "convert_hf_to_gguf.py"
    self_test_calls = [call for call in calls if "llama-cli" in Path(call[0]).name.lower() and "--model" in call]
    assert self_test_calls
    assert all("--no-conversation" in call for call in self_test_calls)
    assert all("--single-turn" in call for call in self_test_calls)
    assert all("--gpu-layers" in call and "all" in call for call in self_test_calls)
    assert all("<start_of_turn>user" in call[call.index("--prompt") + 1] for call in self_test_calls)
    assert {path.relative_to(tmp_path / "gguf").as_posix() for path in (tmp_path / "gguf").rglob("*.gguf")} == {
        "bf16/model-BF16.gguf",
        "Q4_K_M/model-Q4_K_M.gguf",
        "Q8_0/model-Q8_0.gguf",
    }
    base_probe = next(
        index
        for index, call in enumerate(calls)
        if "llama-cli" in Path(call[0]).name.lower() and "model-BF16.gguf" in " ".join(call)
    )
    quantize_calls = [
        index for index, call in enumerate(calls) if "quantize" in Path(call[0]).name.lower() and "--help" not in call
    ]
    assert quantize_calls and base_probe < min(quantize_calls)
    assert all(
        environment["HF_HUB_OFFLINE"] == "1"
        and environment["TRANSFORMERS_OFFLINE"] == "1"
        and environment["NO_PROXY"] == "*"
        for _call, environment in runner.calls
    )


def test_gguf_pipeline_refuses_unmaterialized_or_unsupported_bundle_before_conversion(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")
    bundle_root, bundle_lock = _llama_bundle(tmp_path / "llama-bundle")

    class UnsupportedRunner(FakeCommandRunner):
        def run(self, command, **kwargs):
            if "--print-supported-models" in command:
                self.calls.append((tuple(command), kwargs["environment"]))
                return CommandResult(0, '["LlamaForCausalLM"]', "")
            return super().run(command, **kwargs)

    runner = UnsupportedRunner()
    with pytest.raises(QuantizationError, match="Gemma3ForCausalLM"):
        build_gguf_variants(
            checkpoint,
            tmp_path / "gguf",
            source_id="final-bf16",
            training_fingerprint=_TRAINING_FINGERPRINT,
            bundle_root=bundle_root,
            bundle_lock=bundle_lock,
            python_executable=Path("python.exe"),
            runner=runner,
            prompt_renderer=lambda _source: RenderedPrompt(
                prompt="prompt",
                chat_template_sha256="sha256:" + "c" * 64,
            ),
        )
    assert len(runner.calls) == 1
    assert not (tmp_path / "gguf").exists()


def test_gguf_pipeline_accepts_hash_bound_windows_runtime_and_exact_converter_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = _checkpoint(tmp_path / "checkpoint")
    runtime = tmp_path / "windows-runtime"
    runtime.mkdir()
    for name in ("llama-cli.exe", "llama-quantize.exe", "llama-server.exe"):
        (runtime / name).write_bytes(name.encode("ascii"))
    source = tmp_path / "llama-source"
    source.mkdir()
    (source / "convert_hf_to_gguf.py").write_text("# converter\n", encoding="utf-8")
    source_tree = "sha256:" + "d" * 64
    monkeypatch.setattr(
        quantization_module,
        "load_verified_windows_llama_cpp_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            root=runtime,
            distribution_lock_sha256="sha256:" + "e" * 64,
            runtime_tree_sha256="sha256:" + "f" * 64,
            release_tag="b10158",
            commit="f87067841bac583bc089a225382248d857791ca8",
            conversion_commit="91f8c9c5fb038c086e13e9cd823c29b33b07ba54",
            llama_cli=runtime / "llama-cli.exe",
            llama_quantize=runtime / "llama-quantize.exe",
            llama_server=runtime / "llama-server.exe",
        ),
    )
    monkeypatch.setattr(
        quantization_module,
        "verify_llama_cpp_source_checkout",
        lambda *_args, **_kwargs: {"source_inventory": {"tree_sha256": source_tree}},
    )

    class WindowsRunner(FakeCommandRunner):
        def run(self, command, **kwargs):
            if "--help" in command:
                self.calls.append((tuple(command), kwargs["environment"]))
                return CommandResult(1, "Q8_0 Q4_K_M", "usage")
            if "--version" in command:
                self.calls.append((tuple(command), kwargs["environment"]))
                return CommandResult(0, "version: 10158 (f8706784)", "")
            return super().run(command, **kwargs)

    manifest = build_gguf_variants(
        checkpoint,
        tmp_path / "gguf-windows",
        source_id="final-bf16",
        training_fingerprint=_TRAINING_FINGERPRINT,
        windows_runtime_root=runtime,
        windows_runtime_manifest=runtime / "windows-llama-cpp-runtime-manifest.json",
        llama_cpp_source=source,
        python_executable=Path("python.exe"),
        runner=WindowsRunner(),
        prompt_renderer=lambda _source: RenderedPrompt(
            prompt="prompt",
            chat_template_sha256="sha256:" + "c" * 64,
        ),
    )

    assert manifest["toolchain"]["llama_cpp_commit"] == ("91f8c9c5fb038c086e13e9cd823c29b33b07ba54")
    for variant in ("Q8_0", "Q4_K_M"):
        verified = verify_variant_artifact_manifest(tmp_path / "gguf-windows" / variant)
        assert verified["toolchain"]["source_tree_sha256"] == source_tree
