"""Persistent, bounded localhost HTTP runtime for one hash-pinned llama.cpp server."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

_HASH_PREFIX = "sha256:"


class LlamaCppRuntimeError(RuntimeError):
    """The persistent llama.cpp process or its local protocol violated its contract."""


@dataclass(frozen=True, slots=True)
class LlamaServerCompletion:
    """One complete streamed response with native llama.cpp timing evidence."""

    text: str
    prompt_tokens: int
    output_tokens: int
    prefill_seconds: float
    first_token_seconds: float
    decode_seconds: float
    end_to_end_seconds: float
    tokens_per_second: float


class PersistentLlamaRuntime(Protocol):
    """Injectable persistent-runtime boundary used by the generation adapter."""

    protocol: str
    executable_sha256: str
    instance_id: str

    def health(self) -> bool: ...

    def complete(self, prompt: str, *, max_new_tokens: int) -> LlamaServerCompletion: ...

    def close(self) -> None: ...


class _BoundedByteTail:
    """Thread-safe tail buffer whose memory cannot exceed the configured byte cap."""

    def __init__(self, maximum_bytes: int) -> None:
        if maximum_bytes < 1024:
            raise ValueError("process stream limit must be at least 1024 bytes")
        self._maximum_bytes = maximum_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) > self._maximum_bytes:
            chunk = chunk[-self._maximum_bytes :]
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self._maximum_bytes and self._chunks:
                excess = self._size - self._maximum_bytes
                first = self._chunks[0]
                if len(first) <= excess:
                    self._chunks.popleft()
                    self._size -= len(first)
                else:
                    self._chunks[0] = first[excess:]
                    self._size -= excess

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", errors="replace")


class _BoundedByteHead:
    """Thread-safe prefix buffer that preserves immutable process startup evidence."""

    def __init__(self, maximum_bytes: int) -> None:
        if maximum_bytes < 1024:
            raise ValueError("process stream limit must be at least 1024 bytes")
        self._maximum_bytes = maximum_bytes
        self._payload = bytearray()
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            remaining = self._maximum_bytes - len(self._payload)
            if remaining > 0:
                self._payload.extend(chunk[:remaining])

    def text(self) -> str:
        with self._lock:
            return bytes(self._payload).decode("utf-8", errors="replace")


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise LlamaCppRuntimeError(f"runtime entry is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return _HASH_PREFIX + digest.hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _drain_stream(
    stream: BinaryIO,
    tail: _BoundedByteTail,
    errors: list[BaseException],
    head: _BoundedByteHead | None = None,
) -> None:
    try:
        # BufferedReader.read() may wait for the full requested size while a
        # long-lived Windows child keeps the pipe open. read1() returns the
        # bytes already available, so model-load diagnostics are observable as
        # soon as the server becomes healthy.
        read_available = getattr(stream, "read1", stream.read)
        while chunk := read_available(4096):
            if head is not None:
                head.append(chunk)
            tail.append(chunk)
    except BaseException as error:
        errors.append(error)


def _number(mapping: dict[str, Any], name: str) -> float:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise LlamaCppRuntimeError(f"llama-server timing {name!r} is missing or invalid")
    return float(value)


def _positive_integer(mapping: dict[str, Any], name: str) -> int:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LlamaCppRuntimeError(f"llama-server count {name!r} is missing or invalid")
    return value


class LlamaCppHttpRuntime:
    """Own one persistent llama-server process and stream deterministic completions."""

    protocol = "llama_server_http_sse_v1"

    def __init__(
        self,
        *,
        model_path: str | Path,
        llama_server_path: str | Path,
        llama_server_sha256: str,
        context_size: int,
        gpu_layers: int,
        startup_timeout_seconds: float,
        request_timeout_seconds: float,
        shutdown_timeout_seconds: float,
        stderr_max_bytes: int,
        port: int | None = None,
        opener: Any | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model_path = Path(model_path).resolve()
        self._server_path = Path(llama_server_path).resolve()
        if (
            self._model_path.is_symlink()
            or not self._model_path.is_file()
            or self._model_path.stat().st_size < 1
        ):
            raise LlamaCppRuntimeError(
                f"GGUF model is not a non-empty regular file: {self._model_path}"
            )
        actual_server_hash = _sha256_file(self._server_path)
        if actual_server_hash != llama_server_sha256:
            raise LlamaCppRuntimeError(
                "llama-server hash mismatch: "
                f"expected {llama_server_sha256}, actual {actual_server_hash}"
            )
        if context_size < 1024:
            raise LlamaCppRuntimeError("llama-server context_size must be at least 1024")
        if gpu_layers < 0:
            raise LlamaCppRuntimeError("llama-server gpu_layers cannot be negative")
        if min(
            startup_timeout_seconds,
            request_timeout_seconds,
            shutdown_timeout_seconds,
        ) <= 0:
            raise LlamaCppRuntimeError("llama-server timeouts must be positive")

        self.executable_sha256 = actual_server_hash
        self._requested_gpu_layers = gpu_layers
        self._gpu_layers_mode = "all" if gpu_layers == 999 else str(gpu_layers)
        self._fit_mode = "on"
        self._startup_timeout_seconds = startup_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._clock = clock
        self._sleep = sleep
        self._port = port if port is not None else _free_loopback_port()
        if self._port < 1 or self._port > 65535:
            raise LlamaCppRuntimeError("llama-server port must be between 1 and 65535")
        self._base_url = f"http://127.0.0.1:{self._port}"
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._stdout_tail = _BoundedByteTail(stderr_max_bytes)
        self._stderr_tail = _BoundedByteTail(stderr_max_bytes)
        self._stderr_startup = _BoundedByteHead(stderr_max_bytes)
        self._reader_errors: list[BaseException] = []
        self._closed = False
        # The pinned Gemma chat template renders its own explicit <bos>. Keep
        # llama.cpp from prepending a second token so native prompt timing and
        # the local Hugging Face tokenizer bind the same prompt token count.
        command = [
            str(self._server_path),
            "--model",
            str(self._model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self._port),
            "--ctx-size",
            str(context_size),
            "--parallel",
            "1",
            "--n-gpu-layers",
            self._gpu_layers_mode,
            "--fit",
            self._fit_mode,
            "--log-verbosity",
            "4",
            "--override-kv",
            "tokenizer.ggml.add_bos_token=bool:false",
            "--no-webui",
        ]
        environment = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "NO_PROXY": "*",
        }
        try:
            self._process = popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise LlamaCppRuntimeError(f"llama-server could not start: {error}") from error
        if self._process.stdout is None or self._process.stderr is None:
            self._terminate_process()
            raise LlamaCppRuntimeError("llama-server process streams are unavailable")
        self._threads = [
            threading.Thread(
                target=_drain_stream,
                args=(self._process.stdout, self._stdout_tail, self._reader_errors),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(
                    self._process.stderr,
                    self._stderr_tail,
                    self._reader_errors,
                    self._stderr_startup,
                ),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        identity_payload = (
            f"{self.executable_sha256}\0{_sha256_file(self._model_path)}\0"
            f"{self._process.pid}\0{self._clock()}"
        ).encode()
        self.instance_id = _HASH_PREFIX + hashlib.sha256(identity_payload).hexdigest()
        try:
            self._wait_until_healthy()
        except BaseException:
            self.close()
            raise

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail.text()

    @property
    def stderr_startup(self) -> str:
        return self._stderr_startup.text()

    def diagnostics(self) -> dict[str, object]:
        """Return bounded native startup evidence without model input or output text."""

        return {
            "backend": "llama_cpp_cuda",
            "device": "cuda" if self._requested_gpu_layers > 0 else "cpu",
            "requested_gpu_layers": self._requested_gpu_layers,
            "gpu_layers_mode": self._gpu_layers_mode,
            "fit_mode": self._fit_mode,
            "runtime_instance_id": self.instance_id,
            "runtime_executable_sha256": self.executable_sha256,
            "stderr_startup": self.stderr_startup,
        }

    def _process_failure(self, label: str) -> LlamaCppRuntimeError:
        returncode = self._process.poll()
        detail = self.stderr_tail.strip().replace("\n", " ")[-1000:]
        return LlamaCppRuntimeError(
            f"{label}; process_exit={returncode}; stderr={detail or 'no diagnostics'}"
        )

    def _terminate_process(self) -> None:
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self._shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._shutdown_timeout_seconds)

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        return self._opener.open(request, timeout=timeout)

    def health(self) -> bool:
        if self._closed or self._process.poll() is not None:
            return False
        request = urllib.request.Request(
            f"{self._base_url}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._open(request, min(2.0, self._request_timeout_seconds)) as response:
                if int(response.status) != 200:
                    return False
                payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                return False
            value = json.loads(payload)
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            return False
        return isinstance(value, dict) and value.get("status") == "ok"

    def _wait_until_healthy(self) -> None:
        deadline = self._clock() + self._startup_timeout_seconds
        while self._clock() < deadline:
            if self._process.poll() is not None:
                raise self._process_failure("llama-server exited before becoming healthy")
            if self._reader_errors:
                raise self._process_failure("llama-server stream reader failed")
            if self.health():
                return
            self._sleep(0.05)
        raise self._process_failure(
            f"llama-server health timeout after {self._startup_timeout_seconds} seconds"
        )

    def complete(self, prompt: str, *, max_new_tokens: int) -> LlamaServerCompletion:
        if not prompt:
            raise LlamaCppRuntimeError("llama-server prompt must be non-empty")
        if max_new_tokens < 1:
            raise LlamaCppRuntimeError("max_new_tokens must be positive")
        if not self.health():
            raise self._process_failure("llama-server is not healthy before generation")
        body = json.dumps(
            {
                "prompt": prompt,
                "n_predict": max_new_tokens,
                "temperature": 0,
                "seed": 0,
                "stream": True,
                "cache_prompt": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/completion",
            data=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = self._clock()
        first_token_at: float | None = None
        parts: list[str] = []
        content_bytes = 0
        final: dict[str, Any] | None = None
        try:
            with self._open(request, self._request_timeout_seconds) as response:
                if int(response.status) != 200:
                    raise LlamaCppRuntimeError(
                        f"llama-server completion returned HTTP {response.status}"
                    )
                while True:
                    if self._clock() - started > self._request_timeout_seconds:
                        raise LlamaCppRuntimeError("llama-server completion exceeded timeout")
                    line = response.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 1024 * 1024:
                        raise LlamaCppRuntimeError("llama-server emitted an oversized SSE line")
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == b"[DONE]":
                        continue
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise LlamaCppRuntimeError(
                            "llama-server SSE payload must contain an object"
                        )
                    content = value.get("content", "")
                    if not isinstance(content, str):
                        raise LlamaCppRuntimeError(
                            "llama-server SSE content must be text"
                        )
                    if content:
                        if first_token_at is None:
                            first_token_at = self._clock()
                        content_bytes += len(content.encode("utf-8"))
                        if content_bytes > 16 * 1024 * 1024:
                            raise LlamaCppRuntimeError(
                                "llama-server completion exceeded 16 MiB"
                            )
                        parts.append(content)
                    if value.get("stop") is True:
                        final = value
                        break
        except LlamaCppRuntimeError:
            raise
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ) as error:
            raise LlamaCppRuntimeError(
                f"llama-server completion protocol failed: {error}"
            ) from error
        ended = self._clock()
        if final is None or first_token_at is None:
            raise LlamaCppRuntimeError(
                "llama-server stream ended without a final event and completion token"
            )
        timings = final.get("timings")
        if not isinstance(timings, dict):
            raise LlamaCppRuntimeError("llama-server final event is missing timings")
        prompt_tokens = _positive_integer(timings, "prompt_n")
        output_tokens = _positive_integer(timings, "predicted_n")
        prefill_seconds = _number(timings, "prompt_ms") / 1000
        decode_seconds = _number(timings, "predicted_ms") / 1000
        throughput = _number(timings, "predicted_per_second")
        if throughput <= 0:
            raise LlamaCppRuntimeError("llama-server decode throughput must be positive")
        first_token_seconds = first_token_at - started
        end_to_end_seconds = ended - started
        if first_token_seconds < prefill_seconds:
            raise LlamaCppRuntimeError(
                "llama-server wall-clock TTFT is shorter than native prefill timing"
            )
        if end_to_end_seconds < first_token_seconds:
            raise LlamaCppRuntimeError("llama-server end-to-end timing precedes TTFT")
        return LlamaServerCompletion(
            text="".join(parts).strip(),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            prefill_seconds=prefill_seconds,
            first_token_seconds=first_token_seconds,
            decode_seconds=decode_seconds,
            end_to_end_seconds=end_to_end_seconds,
            tokens_per_second=throughput,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._terminate_process()
        for stream_name in ("stdout", "stderr"):
            stream = getattr(self._process, stream_name, None)
            if stream is not None:
                stream.close()
        for thread in getattr(self, "_threads", []):
            thread.join(timeout=self._shutdown_timeout_seconds)
        if any(thread.is_alive() for thread in getattr(self, "_threads", [])):
            raise LlamaCppRuntimeError("llama-server stream reader did not stop")
        if self._reader_errors:
            raise LlamaCppRuntimeError(
                f"llama-server stream reader failed: {self._reader_errors[0]}"
            )
