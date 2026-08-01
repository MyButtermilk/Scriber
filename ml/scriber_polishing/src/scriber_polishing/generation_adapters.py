"""Deterministic local generation adapters shared by evaluation and benchmarks."""

from __future__ import annotations

import gc
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .inference import PredictionResult
from .llama_cpp_runtime import (
    LlamaCppHttpRuntime,
    PersistentLlamaRuntime,
)
from .protected_spans import protect_spans, restore_spans
from .sst_parser import SSTParseError, parse_sst
from .tokenize import POLISHING_INSTRUCTION
from .variant_artifacts import verify_saved_bitsandbytes_config


@dataclass(frozen=True, slots=True)
class GenerationMeasurement:
    """One deterministic generation with explicitly defined latency components."""

    text: str
    input_tokens: int
    output_tokens: int
    tokenization_seconds: float
    model_load_seconds: float
    prefill_seconds: float
    time_to_first_token_seconds: float
    decode_seconds: float
    end_to_end_seconds: float
    tokens_per_second: float

    def __post_init__(self) -> None:
        if self.input_tokens <= 0 or self.output_tokens <= 0:
            raise ValueError("generation token counts must be positive")
        values = {
            "tokenization": self.tokenization_seconds,
            "model_load": self.model_load_seconds,
            "prefill": self.prefill_seconds,
            "time_to_first_token": self.time_to_first_token_seconds,
            "decode": self.decode_seconds,
            "end_to_end": self.end_to_end_seconds,
            "tokens_per_second": self.tokens_per_second,
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("generation timing and throughput values must be non-negative")
        if self.time_to_first_token_seconds < self.prefill_seconds:
            raise ValueError("time_to_first_token cannot be shorter than prefill")
        if self.end_to_end_seconds < self.time_to_first_token_seconds:
            raise ValueError("end_to_end cannot be shorter than time_to_first_token")
        if self.tokens_per_second <= 0:
            raise ValueError("tokens_per_second must be positive")


class DeterministicGenerationAdapter(Protocol):
    """Stable seam for complete local model variants."""

    backend: str
    artifact_size_bytes: int
    model_size_bytes: int
    runtime_mode: str
    runtime_protocol: str
    runtime_executable_sha256: str
    runtime_instance_id: str

    def count_input_tokens(self, text: str) -> int: ...

    def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement: ...

    def health(self) -> bool: ...

    def warmup(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LlamaCliExecution:
    """Captured streamed llama-cli execution."""

    returncode: int
    stdout: str
    stderr: str
    first_output_seconds: float | None
    end_to_end_seconds: float


class LlamaCliRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> LlamaCliExecution: ...


class StreamingLlamaCliRunner:
    """Measure the first completion byte while draining both process streams."""

    def run(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> LlamaCliExecution:
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **environment},
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise RuntimeError(f"llama-cli could not start: {error}") from error
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        first_output: list[float] = []
        stdout_size = 0
        stderr_size = 0
        reader_errors: list[Exception] = []

        def read_stdout() -> None:
            nonlocal stdout_size
            try:
                assert process.stdout is not None
                while chunk := process.stdout.read(1):
                    if not first_output:
                        first_output.append(time.perf_counter() - started)
                    if stdout_size < 16 * 1024 * 1024:
                        stdout_parts.append(chunk)
                        stdout_size += len(chunk)
            except Exception as error:
                reader_errors.append(error)

        def read_stderr() -> None:
            nonlocal stderr_size
            try:
                assert process.stderr is not None
                while chunk := process.stderr.read(4096):
                    if stderr_size < 16 * 1024 * 1024:
                        stderr_parts.append(chunk)
                        stderr_size += len(chunk)
            except Exception as error:
                reader_errors.append(error)

        threads = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise RuntimeError(f"llama-cli timed out after {timeout_seconds} seconds") from error
        finally:
            for thread in threads:
                thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("llama-cli stream reader did not stop")
        if reader_errors:
            raise RuntimeError(f"llama-cli stream reader failed: {reader_errors[0]}")
        ended = time.perf_counter()
        return LlamaCliExecution(
            returncode=returncode,
            stdout=b"".join(stdout_parts).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_parts).decode("utf-8", errors="replace"),
            first_output_seconds=first_output[0] if first_output else None,
            end_to_end_seconds=ended - started,
        )


def _directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def _model_weight_size(path: Path) -> int:
    weight_files = [
        item
        for pattern in ("*.safetensors", "*.bin")
        for item in path.glob(pattern)
        if item.is_file() and not item.is_symlink()
    ]
    if not weight_files:
        raise ValueError(f"local model directory has no regular weight files: {path}")
    return sum(item.stat().st_size for item in weight_files)


class TransformersGenerationAdapter:
    """Greedy, local-only Transformers adapter with synchronized GPU timings."""

    backend = "transformers"
    runtime_mode = "persistent_model_process"
    runtime_protocol = "in_process_transformers_v1"

    def __init__(
        self,
        tokenizer: Any,
        model: Any,
        torch: Any,
        artifact_size_bytes: int,
        model_size_bytes: int,
        *,
        variant: str,
    ) -> None:
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch
        self.artifact_size_bytes = artifact_size_bytes
        self.model_size_bytes = model_size_bytes
        self._variant = variant
        self.runtime_executable_sha256 = (
            "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        )
        identity = f"{os.getpid()}\0{id(model)}\0{self.runtime_executable_sha256}".encode()
        self.runtime_instance_id = "sha256:" + hashlib.sha256(identity).hexdigest()
        self._closed = False

    @classmethod
    def load(
        cls,
        model_path: str | Path,
        *,
        variant: str,
        device: str = "cuda",
    ) -> TransformersGenerationAdapter:
        """Load BF16/INT8/NF4 from a local directory; Hub fallback is impossible."""

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        path = Path(model_path).resolve()
        if not path.is_dir():
            raise ValueError(f"local model directory does not exist: {path}")
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but is unavailable")
        tokenizer = AutoTokenizer.from_pretrained(
            path,
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
        model_kwargs: dict[str, Any] = {
            "local_files_only": True,
            "trust_remote_code": False,
            "low_cpu_mem_usage": True,
            "attn_implementation": "sdpa",
        }
        if variant == "bf16":
            model_kwargs["dtype"] = torch.bfloat16 if device == "cuda" else torch.float32
        elif variant in {"int8", "int4_nf4"}:
            verify_saved_bitsandbytes_config(path, variant)
            model_kwargs["device_map"] = "auto"
        else:
            raise ValueError(f"unsupported Transformers variant: {variant}")
        model = AutoModelForCausalLM.from_pretrained(path, **model_kwargs)
        if variant == "bf16":
            model = model.to(device)
        else:
            loaded_flag = (
                "is_loaded_in_8bit" if variant == "int8" else "is_loaded_in_4bit"
            )
            if not bool(getattr(model, loaded_flag, False)):
                raise RuntimeError(
                    f"saved artifact reload did not activate expected {variant} quantization"
                )
        model.eval()
        model.generation_config.do_sample = False
        model.generation_config.num_beams = 1
        model.generation_config.top_p = None
        model.generation_config.top_k = None
        return cls(
            tokenizer,
            model,
            torch,
            _directory_size(path),
            _model_weight_size(path),
            variant=variant,
        )

    @property
    def runtime_execution_evidence(self) -> dict[str, object]:
        """Prove that the complete Transformers model is resident on CUDA."""

        raw_device_map = getattr(self._model, "hf_device_map", None)
        if isinstance(raw_device_map, dict) and raw_device_map:
            devices = sorted({str(value) for value in raw_device_map.values()})
        else:
            model_device = getattr(self._model, "device", None)
            if model_device is None:
                try:
                    model_device = next(self._model.parameters()).device
                except (AttributeError, StopIteration) as error:
                    raise RuntimeError(
                        "Transformers runtime cannot prove model device residency"
                    ) from error
            devices = [str(model_device)]
        normalized_devices = [
            "cuda:0" if value in {"0", "cuda"} else value for value in devices
        ]
        if any(not value.startswith("cuda") for value in normalized_devices):
            raise RuntimeError(
                "Transformers runtime placed model weights outside CUDA: "
                + ", ".join(normalized_devices)
            )
        device_index = int(self._torch.cuda.current_device())
        major, minor = self._torch.cuda.get_device_capability(device_index)
        return {
            "backend": "transformers_cuda",
            "device": "cuda",
            "loaded_variant": self._variant,
            "model_devices": normalized_devices,
            "full_model_on_gpu": True,
            "gpu_name": str(self._torch.cuda.get_device_name(device_index)),
            "compute_capability": f"{major}.{minor}",
            "cuda_device_index": device_index,
            "cuda_memory_allocated_bytes": int(
                self._torch.cuda.memory_allocated(device_index)
            ),
            "cuda_memory_reserved_bytes": int(
                self._torch.cuda.memory_reserved(device_index)
            ),
        }

    def _encode(self, text: str) -> Any:
        messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]
        encoded = self._tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        device = getattr(self._model, "device", None)
        if device is not None and hasattr(encoded, "to"):
            return encoded.to(device)
        if device is not None:
            return {
                key: value.to(device) if hasattr(value, "to") else value
                for key, value in encoded.items()
            }
        return encoded

    def count_input_tokens(self, text: str) -> int:
        encoded = self._encode(text)
        return int(encoded["input_ids"].shape[-1])

    def _synchronize(self) -> None:
        if self._torch.cuda.is_available():
            self._torch.cuda.synchronize()

    def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        if self._closed:
            raise RuntimeError("Transformers adapter is closed")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        started = time.perf_counter()
        encoded = self._encode(text)
        tokenized = time.perf_counter()
        input_ids = encoded["input_ids"]
        attention_mask = encoded.get("attention_mask")
        if attention_mask is None:
            attention_mask = self._torch.ones_like(input_ids)
        self._synchronize()
        prefill_started = time.perf_counter()
        with self._torch.inference_mode():
            outputs = self._model(
                **dict(encoded),
                use_cache=True,
                return_dict=True,
            )
            next_token = self._torch.argmax(outputs.logits[:, -1, :], dim=-1, keepdim=True)
        self._synchronize()
        first_token_at = time.perf_counter()
        generated = [next_token]
        full_ids = self._torch.cat([input_ids, next_token], dim=-1)
        full_attention = self._torch.cat(
            [attention_mask, self._torch.ones_like(next_token)],
            dim=-1,
        )
        past_key_values = outputs.past_key_values
        cache_position = self._torch.tensor(
            [full_ids.shape[1] - 1],
            device=full_ids.device,
            dtype=self._torch.long,
        )
        eos = getattr(self._model.generation_config, "eos_token_id", None)
        if eos is None:
            eos = self._tokenizer.eos_token_id
        eos_ids = set(eos if isinstance(eos, list | tuple) else [eos])
        while len(generated) < max_new_tokens and int(next_token[0, 0].item()) not in eos_ids:
            prepared = self._model.prepare_inputs_for_generation(
                full_ids,
                past_key_values=past_key_values,
                attention_mask=full_attention,
                cache_position=cache_position,
                use_cache=True,
            )
            with self._torch.inference_mode():
                outputs = self._model(**prepared, return_dict=True)
                next_token = self._torch.argmax(
                    outputs.logits[:, -1, :],
                    dim=-1,
                    keepdim=True,
                )
            generated.append(next_token)
            full_ids = self._torch.cat([full_ids, next_token], dim=-1)
            full_attention = self._torch.cat(
                [full_attention, self._torch.ones_like(next_token)],
                dim=-1,
            )
            past_key_values = outputs.past_key_values
            cache_position = self._torch.tensor(
                [full_ids.shape[1] - 1],
                device=full_ids.device,
                dtype=self._torch.long,
            )
        self._synchronize()
        ended = time.perf_counter()
        completion_ids = self._torch.cat(generated, dim=-1)[0]
        completion = self._tokenizer.decode(completion_ids, skip_special_tokens=True)
        output_tokens = len(generated)
        generation_seconds = ended - prefill_started
        return GenerationMeasurement(
            text=completion,
            input_tokens=int(input_ids.shape[-1]),
            output_tokens=output_tokens,
            tokenization_seconds=tokenized - started,
            model_load_seconds=0.0,
            prefill_seconds=first_token_at - prefill_started,
            time_to_first_token_seconds=first_token_at - started,
            decode_seconds=ended - first_token_at,
            end_to_end_seconds=ended - started,
            tokens_per_second=output_tokens / max(generation_seconds, 1e-9),
        )

    def health(self) -> bool:
        return not self._closed and hasattr(self, "_model")

    def warmup(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        return self.generate(text, max_new_tokens=max_new_tokens)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        del self._model
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


_LLAMA_TIMING_PATTERNS = {
    "load": re.compile(
        r"^\s*(?:llama_perf_context_print:\s*)?load time\s*=\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*ms",
        re.IGNORECASE | re.MULTILINE,
    ),
    "prefill": re.compile(
        r"^\s*(?:llama_perf_context_print:\s*)?prompt eval time\s*=\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*ms\s*/\s*([0-9]+)\s*tokens?",
        re.IGNORECASE | re.MULTILINE,
    ),
    "decode": re.compile(
        r"^\s*(?:llama_perf_context_print:\s*)?eval time\s*=\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*ms\s*/\s*([0-9]+)\s*(?:runs?|tokens?)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "throughput": re.compile(
        r"^\s*(?:llama_perf_context_print:\s*)?eval time[^\n]*"
        r"\(([0-9]+(?:\.[0-9]+)?)\s*tokens per second\)",
        re.IGNORECASE | re.MULTILINE,
    ),
}


class LlamaCppCliGenerationAdapter:
    """Hash-pinned, deterministic GGUF adapter using native llama-cli timings."""

    backend = "llama_cpp_cli"
    runtime_mode = "per_request_model_process"
    runtime_protocol = "llama_cli_process_per_request_v1"

    def __init__(
        self,
        *,
        model_path: str | Path,
        llama_cli_path: str | Path,
        llama_cli_sha256: str,
        tokenizer: Any,
        runner: LlamaCliRunner | None = None,
    ) -> None:
        self._model_path = Path(model_path).resolve()
        self._llama_cli_path = Path(llama_cli_path).resolve()
        if (
            not self._model_path.is_file()
            or self._model_path.is_symlink()
            or self._model_path.stat().st_size == 0
        ):
            raise ValueError(f"GGUF model is not a non-empty regular file: {self._model_path}")
        if not self._llama_cli_path.is_file() or self._llama_cli_path.is_symlink():
            raise ValueError(f"llama-cli is not a regular file: {self._llama_cli_path}")
        actual_hash = "sha256:" + hashlib.sha256(self._llama_cli_path.read_bytes()).hexdigest()
        if actual_hash != llama_cli_sha256:
            raise ValueError(
                f"llama-cli hash mismatch: expected {llama_cli_sha256}, actual {actual_hash}"
            )
        self._tokenizer = tokenizer
        self._runner = runner or StreamingLlamaCliRunner()
        self.artifact_size_bytes = self._model_path.stat().st_size
        self.model_size_bytes = self.artifact_size_bytes
        self.runtime_executable_sha256 = actual_hash
        identity = f"{actual_hash}\0{self._model_path}\0{id(self)}".encode()
        self.runtime_instance_id = "sha256:" + hashlib.sha256(identity).hexdigest()

    @classmethod
    def load(
        cls,
        model_path: str | Path,
        *,
        llama_cli_path: str | Path,
        llama_cli_sha256: str,
        tokenizer_path: str | Path,
    ) -> LlamaCppCliGenerationAdapter:
        from transformers import AutoTokenizer

        tokenizer_root = Path(tokenizer_path).resolve()
        if not tokenizer_root.is_dir():
            raise ValueError(f"local tokenizer directory does not exist: {tokenizer_root}")
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_root,
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        return cls(
            model_path=model_path,
            llama_cli_path=llama_cli_path,
            llama_cli_sha256=llama_cli_sha256,
            tokenizer=tokenizer,
        )

    def _messages(self, text: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]

    def count_input_tokens(self, text: str) -> int:
        tokens = self._tokenizer.apply_chat_template(
            self._messages(text),
            tokenize=True,
            add_generation_prompt=True,
        )
        if hasattr(tokens, "tolist"):
            tokens = tokens.tolist()
        if tokens and isinstance(tokens[0], list):
            tokens = tokens[0]
        return len(tokens)

    @staticmethod
    def _required_match(name: str, stderr: str) -> re.Match[str]:
        match = _LLAMA_TIMING_PATTERNS[name].search(stderr)
        if match is None:
            raise RuntimeError(f"llama-cli output is missing required {name} timing")
        return match

    def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        tokenization_started = time.perf_counter()
        prompt = self._tokenizer.apply_chat_template(
            self._messages(text),
            tokenize=False,
            add_generation_prompt=True,
        )
        input_tokens = self.count_input_tokens(text)
        tokenization_seconds = time.perf_counter() - tokenization_started
        environment = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "NO_PROXY": "*",
        }
        execution = self._runner.run(
            [
                str(self._llama_cli_path),
                "--model",
                str(self._model_path),
                "--prompt",
                prompt,
                "--n-predict",
                str(max_new_tokens),
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
            ],
            environment=environment,
            timeout_seconds=600,
        )
        if execution.returncode != 0:
            detail = (execution.stderr or execution.stdout).strip().replace("\n", " ")[:1000]
            raise RuntimeError(
                f"llama-cli failed with exit code {execution.returncode}: "
                f"{detail or 'no diagnostics'}"
            )
        if execution.first_output_seconds is None:
            raise RuntimeError("llama-cli emitted no completion bytes; TTFT is unknown")
        completion = execution.stdout.strip()
        if not completion:
            raise RuntimeError("llama-cli emitted an empty completion")
        load_match = self._required_match("load", execution.stderr)
        prefill_match = self._required_match("prefill", execution.stderr)
        decode_match = self._required_match("decode", execution.stderr)
        prompt_tokens = int(prefill_match.group(2))
        if prompt_tokens != input_tokens:
            raise RuntimeError(
                f"llama-cli prompt token count {prompt_tokens} differs from tokenizer {input_tokens}"
            )
        output_tokens = int(decode_match.group(2))
        throughput_match = _LLAMA_TIMING_PATTERNS["throughput"].search(execution.stderr)
        decode_seconds = float(decode_match.group(1)) / 1000
        throughput = (
            float(throughput_match.group(1))
            if throughput_match is not None
            else output_tokens / max(decode_seconds, 1e-9)
        )
        prefill_seconds = float(prefill_match.group(1)) / 1000
        return GenerationMeasurement(
            text=completion,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokenization_seconds=tokenization_seconds,
            model_load_seconds=float(load_match.group(1)) / 1000,
            prefill_seconds=prefill_seconds,
            time_to_first_token_seconds=execution.first_output_seconds,
            decode_seconds=decode_seconds,
            end_to_end_seconds=execution.end_to_end_seconds,
            tokens_per_second=throughput,
        )

    def close(self) -> None:
        return None

    def health(self) -> bool:
        return True

    def warmup(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        return self.generate(text, max_new_tokens=max_new_tokens)


class LlamaCppServerGenerationAdapter:
    """Deterministic GGUF adapter backed by one persistent llama-server process."""

    backend = "llama_cpp_server"
    runtime_mode = "persistent_model_process"

    def __init__(
        self,
        *,
        model_path: str | Path,
        tokenizer: Any,
        runtime: PersistentLlamaRuntime,
    ) -> None:
        model = Path(model_path).resolve()
        if model.is_symlink() or not model.is_file() or model.stat().st_size < 1:
            raise ValueError(f"GGUF model is not a non-empty regular file: {model}")
        if not runtime.health():
            raise RuntimeError("persistent llama.cpp runtime is not healthy")
        self._model_path = model
        self._tokenizer = tokenizer
        self._runtime = runtime
        self.artifact_size_bytes = model.stat().st_size
        self.model_size_bytes = self.artifact_size_bytes
        self.runtime_protocol = runtime.protocol
        self.runtime_executable_sha256 = runtime.executable_sha256
        self.runtime_instance_id = runtime.instance_id
        self._closed = False

    @classmethod
    def load(
        cls,
        model_path: str | Path,
        *,
        llama_server_path: str | Path,
        llama_server_sha256: str,
        tokenizer_path: str | Path,
        context_size: int,
        gpu_layers: int,
        startup_timeout_seconds: float,
        request_timeout_seconds: float,
        shutdown_timeout_seconds: float,
        stderr_max_bytes: int,
    ) -> LlamaCppServerGenerationAdapter:
        """Start one hash-pinned localhost server and load only a local tokenizer."""

        from transformers import AutoTokenizer

        tokenizer_root = Path(tokenizer_path).resolve()
        if not tokenizer_root.is_dir():
            raise ValueError(f"local tokenizer directory does not exist: {tokenizer_root}")
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_root,
            local_files_only=True,
            trust_remote_code=False,
            fix_mistral_regex=False,
        )
        runtime = LlamaCppHttpRuntime(
            model_path=model_path,
            llama_server_path=llama_server_path,
            llama_server_sha256=llama_server_sha256,
            context_size=context_size,
            gpu_layers=gpu_layers,
            startup_timeout_seconds=startup_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            stderr_max_bytes=stderr_max_bytes,
        )
        try:
            return cls(
                model_path=model_path,
                tokenizer=tokenizer,
                runtime=runtime,
            )
        except BaseException:
            runtime.close()
            raise

    def _messages(self, text: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]

    def count_input_tokens(self, text: str) -> int:
        tokens = self._tokenizer.apply_chat_template(
            self._messages(text),
            tokenize=True,
            add_generation_prompt=True,
        )
        if hasattr(tokens, "tolist"):
            tokens = tokens.tolist()
        if tokens and isinstance(tokens[0], list):
            tokens = tokens[0]
        return len(tokens)

    @property
    def runtime_execution_evidence(self) -> dict[str, object]:
        """Expose bounded native load diagnostics for full-offload verification."""

        diagnostics = getattr(self._runtime, "diagnostics", None)
        if not callable(diagnostics):
            raise RuntimeError(
                "persistent llama.cpp runtime does not expose load diagnostics"
            )
        value = diagnostics()
        if not isinstance(value, dict):
            raise RuntimeError("persistent llama.cpp diagnostics are invalid")
        return value

    def generate(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        if self._closed:
            raise RuntimeError("persistent llama.cpp adapter is closed")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        started = time.perf_counter()
        prompt = self._tokenizer.apply_chat_template(
            self._messages(text),
            tokenize=False,
            add_generation_prompt=True,
        )
        input_tokens = self.count_input_tokens(text)
        tokenized = time.perf_counter()
        completion = self._runtime.complete(prompt, max_new_tokens=max_new_tokens)
        if completion.prompt_tokens != input_tokens:
            raise RuntimeError(
                "llama-server prompt token count "
                f"{completion.prompt_tokens} differs from tokenizer {input_tokens}"
            )
        if not completion.text:
            raise RuntimeError("llama-server emitted an empty completion")
        tokenization_seconds = tokenized - started
        time_to_first_token = tokenization_seconds + completion.first_token_seconds
        end_to_end = tokenization_seconds + completion.end_to_end_seconds
        return GenerationMeasurement(
            text=completion.text,
            input_tokens=input_tokens,
            output_tokens=completion.output_tokens,
            tokenization_seconds=tokenization_seconds,
            model_load_seconds=0.0,
            prefill_seconds=completion.prefill_seconds,
            time_to_first_token_seconds=time_to_first_token,
            decode_seconds=completion.decode_seconds,
            end_to_end_seconds=end_to_end,
            tokens_per_second=completion.tokens_per_second,
        )

    def health(self) -> bool:
        return not self._closed and self._runtime.health()

    def warmup(self, text: str, *, max_new_tokens: int) -> GenerationMeasurement:
        return self.generate(text, max_new_tokens=max_new_tokens)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runtime.close()


def prediction_from_adapter(
    transcript: str,
    *,
    adapter: DeterministicGenerationAdapter,
    max_new_tokens: int = 512,
) -> PredictionResult:
    """Expose any deterministic adapter through the frozen evaluation interface."""

    protected = protect_spans(transcript)
    measurement = adapter.generate(protected.text, max_new_tokens=max_new_tokens)
    restoration_error: str | None = None
    try:
        prediction = restore_spans(protected, measurement.text)
    except ValueError as error:
        prediction = measurement.text or " "
        restoration_error = str(error)
    valid_sst = restoration_error is None
    if valid_sst:
        try:
            parse_sst(prediction)
        except (SSTParseError, ValueError):
            valid_sst = False
    return PredictionResult(
        prediction_sst=prediction,
        valid_sst=valid_sst,
        restoration_error=restoration_error,
        generation_seconds=measurement.end_to_end_seconds,
        input_tokens=measurement.input_tokens,
        output_tokens=measurement.output_tokens,
    )
