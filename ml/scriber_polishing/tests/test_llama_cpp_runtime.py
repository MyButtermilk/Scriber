from __future__ import annotations

import hashlib
import io
import json
import threading
from pathlib import Path

import pytest

from scriber_polishing.llama_cpp_runtime import (
    LlamaCppHttpRuntime,
    LlamaCppRuntimeError,
)


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.status = status
        self._stream = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def readline(self, size: int = -1) -> bytes:
        return self._stream.readline(size)


class _SignaledBytesIO(io.BytesIO):
    def __init__(self, payload: bytes, drained: threading.Event) -> None:
        super().__init__(payload)
        self._drained = drained

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        if not chunk:
            self._drained.set()
        return chunk

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class _Process:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.environment = kwargs["env"]
        self.stdout = io.BytesIO(b"server stdout\n")
        self.stderr_drained = threading.Event()
        self.stderr = _SignaledBytesIO(
            (
                b"ggml_cuda_init: found 1 CUDA device:\n"
                b"Device 0: NVIDIA GeForce RTX 4070 Laptop GPU, "
                b"compute capability 8.9\n"
                b"llama_model_load: offloaded 19/19 layers to GPU\n"
                b"llama_model_loader: CUDA0 model buffer size = 123 MiB\n"
                + b"x" * 50_000
            ),
            self.stderr_drained,
        )
        self.pid = 1234
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_persistent_http_runtime_is_loopback_only_streamed_bounded_and_closeable(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model.gguf"
    server = tmp_path / "llama-server.exe"
    model.write_bytes(b"model")
    server.write_bytes(b"server")
    processes: list[_Process] = []

    def process_factory(command, **kwargs):
        process = _Process(command, **kwargs)
        processes.append(process)
        return process

    timings = {
        "prompt_n": 50,
        "predicted_n": 8,
        "prompt_ms": 0.0,
        "predicted_ms": 100.0,
        "predicted_per_second": 80.0,
    }
    completion_payload = (
        b"data: "
        + json.dumps(
            {
                "content": "[DOC]\n[P]Ergebnis.[/P]\n[/DOC]",
                "stop": True,
                "timings": timings,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )

    class Opener:
        def open(self, request, timeout):
            assert timeout > 0
            if request.full_url.endswith("/health"):
                assert processes[0].stderr_drained.wait(timeout=1)
                return _Response(b'{"status":"ok"}')
            assert request.full_url.endswith("/completion")
            body = json.loads(request.data)
            assert body == {
                "prompt": "prompt",
                "n_predict": 16,
                "temperature": 0,
                "seed": 0,
                "stream": True,
                "cache_prompt": False,
            }
            return _Response(completion_payload)

    runtime = LlamaCppHttpRuntime(
        model_path=model,
        llama_server_path=server,
        llama_server_sha256=_sha(server),
        context_size=4096,
        gpu_layers=999,
        startup_timeout_seconds=1,
        request_timeout_seconds=1,
        shutdown_timeout_seconds=1,
        stderr_max_bytes=16_384,
        port=8766,
        opener=Opener(),
        popen_factory=process_factory,
        sleep=lambda _seconds: None,
    )

    completion = runtime.complete("prompt", max_new_tokens=16)

    assert completion.text == "[DOC]\n[P]Ergebnis.[/P]\n[/DOC]"
    assert completion.prompt_tokens == 50
    assert completion.output_tokens == 8
    assert completion.tokens_per_second == 80.0
    assert processes[0].command[processes[0].command.index("--host") + 1] == "127.0.0.1"
    assert processes[0].command[processes[0].command.index("--parallel") + 1] == "1"
    assert processes[0].command[
        processes[0].command.index("--override-kv") + 1
    ] == "tokenizer.ggml.add_bos_token=bool:false"
    assert processes[0].command[
        processes[0].command.index("--n-gpu-layers") + 1
    ] == "all"
    assert processes[0].command[processes[0].command.index("--fit") + 1] == "on"
    assert processes[0].command[
        processes[0].command.index("--log-verbosity") + 1
    ] == "4"
    assert processes[0].environment["NO_PROXY"] == "*"
    assert len(runtime.stderr_tail.encode()) <= 16_384
    diagnostics = runtime.diagnostics()
    assert len(runtime.stderr_startup.encode()) <= 16_384
    assert "found 1 CUDA device" in diagnostics["stderr_startup"]
    assert "offloaded 19/19 layers to GPU" in diagnostics["stderr_startup"]
    runtime.close()
    assert processes[0].terminated is True
    assert runtime.health() is False


def test_persistent_http_runtime_rejects_timeout_and_substituted_executable(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model.gguf"
    server = tmp_path / "llama-server.exe"
    model.write_bytes(b"model")
    server.write_bytes(b"server")

    with pytest.raises(LlamaCppRuntimeError, match="hash mismatch"):
        LlamaCppHttpRuntime(
            model_path=model,
            llama_server_path=server,
            llama_server_sha256="sha256:" + "0" * 64,
            context_size=4096,
            gpu_layers=0,
            startup_timeout_seconds=1,
            request_timeout_seconds=1,
            shutdown_timeout_seconds=1,
            stderr_max_bytes=16_384,
        )

    class Opener:
        def open(self, request, timeout):
            del timeout
            if request.full_url.endswith("/health"):
                return _Response(b'{"status":"ok"}')
            raise TimeoutError("bounded timeout")

    process = _Process([], env={})
    runtime = LlamaCppHttpRuntime(
        model_path=model,
        llama_server_path=server,
        llama_server_sha256=_sha(server),
        context_size=4096,
        gpu_layers=0,
        startup_timeout_seconds=1,
        request_timeout_seconds=1,
        shutdown_timeout_seconds=1,
        stderr_max_bytes=16_384,
        port=8767,
        opener=Opener(),
        popen_factory=lambda *args, **kwargs: process,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(LlamaCppRuntimeError, match="protocol failed"):
        runtime.complete("prompt", max_new_tokens=8)
    runtime.close()
