from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scriber_polishing.benchmark import (
    BenchmarkError,
    _verify_runtime_execution,
    build_runtime_identity,
    load_benchmark_cases,
    run_laptop_benchmark,
    write_benchmark_report,
)
from scriber_polishing.benchmark_cases import materialize_synthetic_benchmark_cases
from scriber_polishing.generation_adapters import (
    GenerationMeasurement,
    LlamaCliExecution,
    LlamaCppCliGenerationAdapter,
    LlamaCppServerGenerationAdapter,
    TransformersGenerationAdapter,
    prediction_from_adapter,
)
from scriber_polishing.llama_cpp_runtime import LlamaServerCompletion
from scriber_polishing.quantization import load_quantization_config

_CATEGORIES = (
    "short_email",
    "long_business_letter",
    "tax_law_letter",
    "multipart_letter",
    "numbered_list",
    "number_heavy",
    "unit_heavy",
    "legal_citations",
    "address_pronouns",
    "rich_text_formatting",
    "identity_text",
    "hard_repetition",
)


def test_llama_cpp_b10158_structured_startup_proves_unchanged_all_layer_cuda_load() -> None:
    startup = "\n".join(
        (
            "0.00 I cmn common_param: device_info:",
            "0.00 I cmn common_param:   - CUDA0   : NVIDIA GeForce RTX 4070 Laptop GPU (8187 MiB, 7054 MiB free)",
            "0.00 I cmn common_param: system_info: CUDA : ARCHS = 500,610,700,750,800,860,890,900 | USE_GRAPHS = 1",
            "0.01 I common_memory_breakdown_print: |   - CUDA0 (RTX 4070 Laptop GPU) |  8187 = 7054 + ( 320 =   271 +      27 +      21) + 813 |",
            "0.01 I common_params_fit_impl: will leave 6733 >= 1024 MiB of free device memory, no changes needed",
            "0.01 I common_fit_params: successfully fit params to free device memory",
            "0.02 I srv llama_server: model loaded",
        )
    )

    evidence = _verify_runtime_execution(
        {
            "backend": "llama_cpp_cuda",
            "device": "cuda",
            "requested_gpu_layers": 999,
            "gpu_layers_mode": "all",
            "fit_mode": "on",
            "runtime_instance_id": "sha256:" + "1" * 64,
            "runtime_executable_sha256": "sha256:" + "2" * 64,
            "stderr_startup": startup,
        },
        variant="Q8_0",
        expected_hardware={
            "gpu_name": "NVIDIA GeForce RTX 4070 Laptop GPU",
            "gpu_compute_capability": "8.9",
            "gpu_device_index": 0,
        },
    )

    assert evidence["status"] == "verified"
    assert evidence["full_model_on_gpu"] is True
    assert evidence["gpu_layers_mode"] == "all"
    assert evidence["fit_parameters_unchanged"] is True
    assert evidence["cuda_model_memory_mib"] == 271


def _case_rows() -> list[dict[str, object]]:
    buckets = (50, 150, 300, 500, 1000)
    return [
        {
            "schema_version": 1,
            "case_id": f"benchmark_{category}_{bucket}",
            "category": category,
            "token_bucket": bucket,
            "text": f"{bucket} tokens",
        }
        for category in _CATEGORIES
        for bucket in buckets
    ]


def _write_cases(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class FakeAdapter:
    backend = "transformers"
    runtime_mode = "persistent_model_process"
    runtime_protocol = "in_process_transformers_v1"
    runtime_executable_sha256 = "sha256:" + "e" * 64
    artifact_size_bytes = 123_456
    model_size_bytes = 120_000

    def __init__(self) -> None:
        self.closed = False
        self.runtime_instance_id = "sha256:" + hashlib.sha256(
            str(id(self)).encode()
        ).hexdigest()
        self.warmup_count = 0

    def count_input_tokens(self, text: str) -> int:
        return int(text.split()[0])

    def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        bucket = self.count_input_tokens(text)
        assert max_new_tokens == {
            50: 128,
            150: 256,
            300: 512,
            500: 768,
            1000: 1536,
        }[bucket]
        return GenerationMeasurement(
            text="[DOC]\n[P]Ergebnis.[/P]\n[/DOC]",
            input_tokens=bucket,
            output_tokens=10,
            tokenization_seconds=0.01,
            model_load_seconds=0.0,
            prefill_seconds=bucket / 10_000,
            time_to_first_token_seconds=bucket / 8_000,
            decode_seconds=0.1,
            end_to_end_seconds=bucket / 8_000 + 0.1,
            tokens_per_second=100.0,
        )

    def close(self) -> None:
        self.closed = True

    def health(self) -> bool:
        return not self.closed

    def warmup(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        self.warmup_count += 1
        return self.generate(text, max_new_tokens=max_new_tokens)

    @property
    def runtime_execution_evidence(self) -> dict[str, object]:
        return {
            "backend": "transformers_cuda",
            "device": "cuda",
            "loaded_variant": "int4_nf4",
            "model_devices": ["cuda:0"],
            "full_model_on_gpu": True,
            "gpu_name": "NVIDIA GeForce RTX 4070 Laptop GPU",
            "compute_capability": "8.9",
            "cuda_device_index": 0,
            "cuda_memory_allocated_bytes": 1_000_000,
            "cuda_memory_reserved_bytes": 2_000_000,
        }


class FakeTelemetry:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> dict[str, object]:
        assert self.started
        return {
            "sample_count": 3,
            "peak_ram_bytes": 2_000_000,
            "peak_vram_bytes": 3_000_000,
            "cpu_percent_samples": [10.0, 20.0, 30.0],
            "gpu_percent_samples": [50.0, 60.0, 70.0],
            "peak_process_count": 4,
            "telemetry_scope": {
                "cpu_memory": "root_process_tree",
                "gpu": "physical_device",
            },
            "hardware": {
                "os": "Windows-11-test",
                "python": "3.12.0",
                "cpu_name": "Fake CPU",
                "logical_cpu_count": 16,
                "physical_cpu_count": 8,
                "total_ram_bytes": 32_000_000_000,
                "gpu_name": "NVIDIA GeForce RTX 4070 Laptop GPU",
                "gpu_uuid": "GPU-fake",
                "gpu_driver_version": "591.86",
                "gpu_total_memory_bytes": 8_188 * 1024 * 1024,
                "gpu_compute_capability": "8.9",
                "torch_bf16_supported": True,
            },
            "baseline_vram_bytes": 1_000_000,
            "peak_vram_delta_bytes": 2_000_000,
        }


def test_laptop_benchmark_covers_required_cases_buckets_metrics_and_percentiles(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    config_path = project_root / "configs" / "quantization.yaml"
    config = load_quantization_config(config_path)
    cases_path = tmp_path / "cases.jsonl"
    _write_cases(cases_path, _case_rows())
    cases = load_benchmark_cases(cases_path, config)
    adapters: list[FakeAdapter] = []
    quantization_config = {
        "quant_method": "bitsandbytes",
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_compute_dtype": "bfloat16",
        "bnb_4bit_use_double_quant": True,
    }
    quantization_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            quantization_config,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    def adapter_factory() -> FakeAdapter:
        adapter = FakeAdapter()
        adapters.append(adapter)
        return adapter

    runtime_identity = build_runtime_identity(
        kind="transformers_local_cuda",
        platform_name="windows_x86_64",
        details={
            "python_executable_sha256": "sha256:" + "f" * 64,
            "packages": {
                "bitsandbytes": "0.49.0",
                "torch": "2.11.0",
                "transformers": "4.57.0",
            },
            "requested_device": "cuda:0",
        },
    )
    report = run_laptop_benchmark(
        adapter_factory=adapter_factory,
        variant="int4_nf4",
        cases=cases,
        config=config,
        config_sha256="sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest(),
        cases_sha256="sha256:" + hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        artifact_manifest_sha256="sha256:" + "a" * 64,
        artifact_tree_sha256="sha256:" + "b" * 64,
        training_fingerprint="sha256:" + "c" * 64,
        artifact_identity={
            "variant": "int4_nf4",
            "backend": "transformers_bitsandbytes",
            "quantization_config": quantization_config,
            "quantization_config_sha256": quantization_hash,
            "toolchain_sha256": "sha256:" + "d" * 64,
        },
        runtime_identity=runtime_identity,
        artifact_size_bytes=123_456,
        telemetry_factory=FakeTelemetry,
    )

    assert report["status"] == "complete"
    assert report["variant"] == "int4_nf4"
    assert report["backend"] == "transformers"
    assert report["artifact_size_bytes"] == 123_456
    assert report["model_size_bytes"] == 120_000
    assert report["runtime_identity"] == runtime_identity
    assert report["runtime_execution"]["status"] == "verified"
    assert report["runtime_execution"]["full_model_on_gpu"] is True
    assert report["warmup_seconds"] >= 0
    assert report["sample_count"] == 303
    assert report["cold_sample_count"] == 3
    assert report["warm_sample_count"] == 300
    assert sum(sample["cold_start"] for sample in report["samples"]) == 3
    assert report["runtime_protocol"] == {
        "cold_loads": {
            "adapter_instances": 3,
            "repetitions": 3,
            "case_id": "benchmark_address_pronouns_50",
        },
        "warm_runtime": {
            "adapter_instances": 1,
            "persistent_model_process": True,
            "repetitions_per_case": 5,
            "warmup_repetitions": 1,
            "health_before_warmup": True,
            "health_after_measurement": True,
            "instance_identity_stable": True,
            "runtime_protocol": "in_process_transformers_v1",
            "runtime_executable_sha256": "sha256:" + "e" * 64,
        },
    }
    assert set(report["by_token_bucket"]) == {"50", "150", "300", "500", "1000"}
    assert set(report["by_case_category"]) == set(_CATEGORIES)
    assert {
        bucket: summary["sample_count"]
        for bucket, summary in report["by_token_bucket"].items()
    } == {
        "50": 60,
        "150": 60,
        "300": 60,
        "500": 60,
        "1000": 60,
    }
    assert report["latency"]["end_to_end_seconds"] == {
        "p50": 0.1375,
        "p95": 0.225,
        "p99": 0.225,
    }
    assert report["throughput_tokens_per_second"] == {
        "p50": 100.0,
        "p95": 100.0,
        "p99": 100.0,
    }
    assert report["resources"]["peak_ram_bytes"] == 2_000_000
    assert report["resources"]["peak_vram_bytes"] == 3_000_000
    assert report["resources"]["baseline_vram_bytes"] == 1_000_000
    assert report["resources"]["peak_vram_delta_bytes"] == 2_000_000
    assert report["resources"]["peak_process_count"] == 4
    assert report["resources"]["cpu_percent"]["p95"] == 30.0
    assert report["resources"]["gpu_percent"]["p95"] == 70.0
    assert report["resources"]["hardware"]["gpu_uuid"] == "GPU-fake"
    assert report["resources"]["hardware_matches_expected"] is True
    assert len(adapters) == 4
    assert all(adapter.closed for adapter in adapters)
    assert [adapter.warmup_count for adapter in adapters] == [0, 0, 0, 1]

    output = tmp_path / "benchmark.json"
    write_benchmark_report(output, report)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    with pytest.raises(BenchmarkError, match="overwrite"):
        write_benchmark_report(output, {**report, "variant": "int8"})


def test_benchmark_cases_fail_closed_when_a_required_bucket_or_category_is_missing(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    config = load_quantization_config(project_root / "configs" / "quantization.yaml")
    path = tmp_path / "cases.jsonl"
    rows = _case_rows()
    rows = [row for row in rows if row["category"] != "hard_repetition"]
    _write_cases(path, rows)

    with pytest.raises(BenchmarkError, match="required benchmark category"):
        load_benchmark_cases(path, config)

    rows = [
        row
        for row in _case_rows()
        if not (
            row["category"] == "short_email"
            and row["token_bucket"] == 1000
        )
    ]
    _write_cases(path, rows)
    with pytest.raises(BenchmarkError, match="category/bucket cell"):
        load_benchmark_cases(path, config)


def test_runtime_identity_is_closed_and_hash_bound() -> None:
    identity = build_runtime_identity(
        kind="llama_cpp_windows_cuda",
        platform_name="windows_x86_64",
        details={
            "runtime_manifest_sha256": "sha256:" + "1" * 64,
            "runtime_tree_sha256": "sha256:" + "2" * 64,
            "llama_cpp_commit": "3" * 40,
            "release_tag": "b10158",
        },
    )

    assert identity["identity_sha256"].startswith("sha256:")
    with pytest.raises(BenchmarkError, match="runtime identity hash"):
        run_laptop_benchmark(
            adapter_factory=FakeAdapter,
            variant="int4_nf4",
            cases=_case_rows(),
            config=load_quantization_config(
                Path(__file__).parents[1] / "configs" / "quantization.yaml"
            ),
            config_sha256="sha256:" + "1" * 64,
            cases_sha256="sha256:" + "2" * 64,
            artifact_manifest_sha256="sha256:" + "3" * 64,
            artifact_tree_sha256="sha256:" + "4" * 64,
            training_fingerprint="sha256:" + "5" * 64,
            artifact_identity={
                "variant": "int4_nf4",
                "backend": "transformers_bitsandbytes",
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_4bit": True,
                    "bnb_4bit_quant_type": "nf4",
                    "bnb_4bit_compute_dtype": "bfloat16",
                    "bnb_4bit_use_double_quant": True,
                },
                "quantization_config_sha256": (
                    "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            {
                                "quant_method": "bitsandbytes",
                                "load_in_4bit": True,
                                "bnb_4bit_quant_type": "nf4",
                                "bnb_4bit_compute_dtype": "bfloat16",
                                "bnb_4bit_use_double_quant": True,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                ),
                "toolchain_sha256": "sha256:" + "6" * 64,
            },
            runtime_identity={**identity, "identity_sha256": "sha256:" + "0" * 64},
            artifact_size_bytes=123_456,
            telemetry_factory=FakeTelemetry,
        )


def test_synthetic_benchmark_suite_is_complete_tokenizer_native_and_non_holdout(
    tmp_path: Path,
) -> None:
    class WordTokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs == {
                "tokenize": True,
                "add_generation_prompt": True,
            }
            return messages[0]["content"].split()

    config_path = Path(__file__).parents[1] / "configs" / "quantization.yaml"
    config = load_quantization_config(config_path)
    output = tmp_path / "synthetic-benchmark.jsonl"
    manifest_path = tmp_path / "synthetic-benchmark.manifest.json"
    manifest = materialize_synthetic_benchmark_cases(
        tokenizer=WordTokenizer(),
        config=config,
        config_sha256=(
            "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
        ),
        tokenizer_tree_sha256="sha256:" + "7" * 64,
        output_path=output,
        manifest_path=manifest_path,
    )

    rows = load_benchmark_cases(output, config)
    assert len(rows) == 60
    assert manifest["record_count"] == 60
    assert manifest["generation_boundary"] == {
        "synthetic_only": True,
        "no_test_split_read": True,
        "no_challenge_split_read": True,
        "no_holdout_content": True,
        "no_generative_api": True,
        "no_human_curation": True,
    }
    assert set(manifest["actual_input_token_counts"]) == {
        row["case_id"] for row in rows
    }
    with pytest.raises(BenchmarkError, match="overwrite"):
        materialize_synthetic_benchmark_cases(
            tokenizer=WordTokenizer(),
            config=config,
            config_sha256=(
                "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            ),
            tokenizer_tree_sha256="sha256:" + "7" * 64,
            output_path=output,
            manifest_path=manifest_path,
        )


def test_evaluation_adapter_restores_protected_values_and_keeps_timing_evidence() -> None:
    class PredictionAdapter:
        backend = "fake"
        artifact_size_bytes = 1

        def count_input_tokens(self, text: str) -> int:
            return len(text)

        def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
            assert "⟦SCRIBER_PROTECTED_0000⟧" in text
            assert max_new_tokens == 32
            return GenerationMeasurement(
                text="[DOC]\n[P]Bitte ⟦SCRIBER_PROTECTED_0000⟧ prüfen.[/P]\n[/DOC]",
                input_tokens=12,
                output_tokens=8,
                tokenization_seconds=0.01,
                model_load_seconds=0.0,
                prefill_seconds=0.02,
                time_to_first_token_seconds=0.03,
                decode_seconds=0.04,
                end_to_end_seconds=0.07,
                tokens_per_second=200.0,
            )

        def close(self) -> None:
            pass

    result = prediction_from_adapter(
        "Bitte 1.234,56 € pruefen",
        adapter=PredictionAdapter(),
        max_new_tokens=32,
    )

    assert result.prediction_sst == "[DOC]\n[P]Bitte 1.234,56 € prüfen.[/P]\n[/DOC]"
    assert result.valid_sst is True
    assert result.generation_seconds == 0.07
    assert result.input_tokens == 12
    assert result.output_tokens == 8


def test_generation_measurements_reject_impossible_timings() -> None:
    with pytest.raises(ValueError, match="time_to_first_token"):
        GenerationMeasurement(
            text="x",
            input_tokens=1,
            output_tokens=1,
            tokenization_seconds=0.1,
            model_load_seconds=0.0,
            prefill_seconds=0.2,
            time_to_first_token_seconds=0.1,
            decode_seconds=0.1,
            end_to_end_seconds=0.2,
            tokens_per_second=10.0,
        )


def test_transformers_quantized_adapter_reloads_saved_config_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "int8"
    artifact.mkdir()
    (artifact / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Gemma3ForCausalLM"],
                "model_type": "gemma3_text",
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_8bit": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (artifact / "model.safetensors").write_bytes(b"quantized")
    observed_model_kwargs: dict[str, object] = {}

    class Tokenizer:
        pad_token_id = 1
        eos_token = "<eos>"

    class Model:
        is_loaded_in_8bit = True
        generation_config = SimpleNamespace()

        def eval(self) -> None:
            return None

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            del args, kwargs
            return Tokenizer()

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            del args
            observed_model_kwargs.update(kwargs)
            return Model()

    class ForbiddenBitsAndBytesConfig:
        def __init__(self, **kwargs):
            raise AssertionError(f"quantization override was supplied: {kwargs}")

    torch_module = ModuleType("torch")
    torch_module.bfloat16 = object()
    torch_module.float32 = object()
    torch_module.cuda = SimpleNamespace(is_available=lambda: True)
    transformers_module = ModuleType("transformers")
    transformers_module.AutoModelForCausalLM = AutoModelForCausalLM
    transformers_module.AutoTokenizer = AutoTokenizer
    transformers_module.BitsAndBytesConfig = ForbiddenBitsAndBytesConfig
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "transformers", transformers_module)

    adapter = TransformersGenerationAdapter.load(
        artifact,
        variant="int8",
        device="cuda",
    )

    assert "quantization_config" not in observed_model_kwargs
    assert observed_model_kwargs["device_map"] == "auto"
    assert adapter.runtime_mode == "persistent_model_process"


def test_llama_cpp_adapter_parses_native_timings_and_uses_deterministic_offline_flags(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model-Q4_K_M.gguf"
    model.write_bytes(b"quantized model")
    llama_cli = tmp_path / "llama-cli.exe"
    llama_cli.write_bytes(b"pinned runtime")

    class Tokenizer:
        eos_token_id = 1

        def apply_chat_template(self, messages, **kwargs):
            assert messages[0]["role"] == "user"
            if kwargs["tokenize"]:
                return list(range(50))
            return "<start_of_turn>user\nprompt<end_of_turn>\n<start_of_turn>model\n"

    class Runner:
        def __init__(self) -> None:
            self.command: list[str] | None = None
            self.environment: dict[str, str] | None = None

        def run(self, command, *, environment, timeout_seconds):
            assert timeout_seconds == 600
            self.command = command
            self.environment = environment
            return LlamaCliExecution(
                returncode=0,
                stdout="[DOC]\n[P]Ergebnis.[/P]\n[/DOC]",
                stderr=(
                    "load time = 25.00 ms\n"
                    "prompt eval time = 40.00 ms / 50 tokens (1250.00 tokens per second)\n"
                    "eval time = 100.00 ms / 10 runs (100.00 tokens per second)\n"
                ),
                first_output_seconds=0.08,
                end_to_end_seconds=0.15,
            )

    runner = Runner()
    adapter = LlamaCppCliGenerationAdapter(
        model_path=model,
        llama_cli_path=llama_cli,
        llama_cli_sha256="sha256:" + hashlib.sha256(llama_cli.read_bytes()).hexdigest(),
        tokenizer=Tokenizer(),
        runner=runner,
    )

    measurement = adapter.generate("Test", max_new_tokens=10)

    assert measurement.input_tokens == 50
    assert measurement.output_tokens == 10
    assert measurement.model_load_seconds == 0.025
    assert measurement.prefill_seconds == 0.04
    assert measurement.time_to_first_token_seconds == 0.08
    assert measurement.tokens_per_second == 100.0
    assert "--temp" in runner.command and runner.command[runner.command.index("--temp") + 1] == "0"
    assert "--no-conversation" in runner.command
    assert "--single-turn" in runner.command
    assert runner.command[runner.command.index("--gpu-layers") + 1] == "all"
    assert runner.environment["HF_HUB_OFFLINE"] == "1"
    assert runner.environment["NO_PROXY"] == "*"


def test_persistent_llama_server_adapter_reuses_one_healthy_runtime_and_native_timings(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model-Q4_K_M.gguf"
    model.write_bytes(b"quantized model")

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert messages[0]["role"] == "user"
            if kwargs["tokenize"]:
                return list(range(50))
            return "<start_of_turn>user\nprompt<end_of_turn>\n<start_of_turn>model\n"

    class Runtime:
        protocol = "llama_server_http_sse_v1"
        executable_sha256 = "sha256:" + "f" * 64
        instance_id = "sha256:" + "1" * 64

        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def health(self) -> bool:
            return not self.closed

        def complete(self, prompt: str, *, max_new_tokens: int) -> LlamaServerCompletion:
            assert "<start_of_turn>model" in prompt
            assert max_new_tokens == 16
            self.calls += 1
            return LlamaServerCompletion(
                text="truncated raw completion",
                prompt_tokens=50,
                output_tokens=10,
                prefill_seconds=0.04,
                first_token_seconds=0.08,
                decode_seconds=0.1,
                end_to_end_seconds=0.18,
                tokens_per_second=100.0,
            )

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    adapter = LlamaCppServerGenerationAdapter(
        model_path=model,
        tokenizer=Tokenizer(),
        runtime=runtime,
    )

    warmup = adapter.warmup("Test", max_new_tokens=16)
    measured = adapter.generate("Test", max_new_tokens=16)

    assert runtime.calls == 2
    assert adapter.runtime_mode == "persistent_model_process"
    assert adapter.runtime_instance_id == "sha256:" + "1" * 64
    assert measured.text == "truncated raw completion"
    assert measured.input_tokens == 50
    assert measured.prefill_seconds == 0.04
    assert measured.time_to_first_token_seconds >= warmup.prefill_seconds
    assert measured.tokens_per_second == 100.0
    adapter.close()
    assert runtime.closed is True
    assert adapter.health() is False
