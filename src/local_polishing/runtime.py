"""Verified, loopback-only llama-server adapter for GGUF product inference."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from aiohttp import ClientSession, ClientTimeout

from src.runtime.paths import app_root

RuntimeName = Literal["vulkan", "cpu"]
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LlamaRuntimeError(RuntimeError):
    """The native runtime or its bounded HTTP protocol failed."""


class GenerationRuntime(Protocol):
    backend_name: str

    async def properties(self) -> dict[str, Any]: ...
    async def apply_template(self, messages: list[dict[str, str]]) -> str: ...
    async def complete(self, prompt: str, *, max_new_tokens: int) -> str: ...
    async def close(self) -> None: ...


class GenerationRuntimeFactory(Protocol):
    name: str

    async def create(self, *, model_path: Path, model_sha256: str) -> GenerationRuntime: ...


async def _close_partial_runtime(runtime: GenerationRuntime) -> None:
    """Retain teardown ownership even if shutdown cancels the creator twice."""

    close_task = asyncio.create_task(runtime.close())
    pending_cancel: asyncio.CancelledError | None = None
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as error:
            pending_cancel = error
        except Exception:
            break
    with suppress(Exception):
        close_task.result()
    if pending_cancel is not None:
        raise pending_cancel


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise LlamaRuntimeError("runtime entry is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeBinary:
    name: RuntimeName
    executable: Path
    sha256: str
    device: str
    gpu_layers: str

    def __post_init__(self) -> None:
        if self.name not in {"vulkan", "cpu"}:
            raise LlamaRuntimeError("unsupported llama.cpp runtime name")
        normalized = self.sha256.removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise LlamaRuntimeError("runtime hash must be exact SHA-256")
        if not self.device or not self.gpu_layers:
            raise LlamaRuntimeError("runtime device and GPU layer policy are required")
        object.__setattr__(self, "sha256", normalized)
        object.__setattr__(self, "executable", self.executable.resolve())


@dataclass(frozen=True, slots=True)
class LlamaServerLaunchSpec:
    context_size: int = 4096
    idle_sleep_seconds: int = 600

    def command(self, *, binary: RuntimeBinary, model_path: Path, port: int, api_key: str) -> list[str]:
        if not 1 <= port <= 65535 or not api_key:
            raise LlamaRuntimeError("runtime port and API key are required")
        return [
            str(binary.executable),
            "--model",
            str(model_path.resolve()),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            str(self.context_size),
            "--parallel",
            "1",
            "--device",
            binary.device,
            "--n-gpu-layers",
            binary.gpu_layers,
            "--api-key",
            api_key,
            "--offline",
            "--no-webui",
            "--log-disable",
            "--jinja",
            "--sleep-idle-seconds",
            str(self.idle_sleep_seconds),
        ]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class LlamaServerRuntime:
    def __init__(
        self,
        *,
        binary: RuntimeBinary,
        model_path: Path,
        model_sha256: str,
        launch_spec: LlamaServerLaunchSpec,
        startup_timeout_seconds: float = 120.0,
        request_timeout_seconds: float = 180.0,
    ) -> None:
        self.backend_name = binary.name
        self._binary = binary
        self._model_path = model_path.resolve()
        self._model_sha256 = model_sha256.removeprefix("sha256:")
        self._launch_spec = launch_spec
        self._startup_timeout = startup_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._port = _free_loopback_port()
        self._api_key = secrets.token_urlsafe(32)
        self._process: asyncio.subprocess.Process | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if await asyncio.to_thread(_file_sha256, self._binary.executable) != self._binary.sha256:
            raise LlamaRuntimeError("llama-server executable hash mismatch")
        if await asyncio.to_thread(_file_sha256, self._model_path) != self._model_sha256:
            raise LlamaRuntimeError("GGUF model hash mismatch")
        command = self._launch_spec.command(
            binary=self._binary,
            model_path=self._model_path,
            port=self._port,
            api_key=self._api_key,
        )
        environment = dict(os.environ)
        for name in (
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._session = ClientSession(timeout=ClientTimeout(total=self._request_timeout), trust_env=False)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                await self.close()
                raise LlamaRuntimeError("llama-server exited during startup")
            try:
                payload = await self._request("GET", "/health", limit=64 * 1024)
                if payload.get("status") == "ok":
                    return
            except LlamaRuntimeError:
                pass
            await asyncio.sleep(0.05)
        await self.close()
        raise LlamaRuntimeError("llama-server startup timed out")

    async def _request(
        self, method: str, path: str, *, body: object | None = None, limit: int = 1024 * 1024
    ) -> dict[str, Any]:
        if self._session is None or self._process is None or self._process.returncode is not None:
            raise LlamaRuntimeError("llama-server is not running")
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            async with self._session.request(
                method,
                f"http://127.0.0.1:{self._port}{path}",
                json=body,
                headers=headers,
            ) as response:
                if response.status != 200:
                    raise LlamaRuntimeError(f"llama-server {path} returned HTTP {response.status}")
                payload = await response.read()
        except LlamaRuntimeError:
            raise
        except Exception as error:
            raise LlamaRuntimeError(f"llama-server {path} request failed") from error
        if len(payload) > limit:
            raise LlamaRuntimeError(f"llama-server {path} response exceeded its limit")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LlamaRuntimeError(f"llama-server {path} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise LlamaRuntimeError(f"llama-server {path} returned a non-object")
        return value

    async def properties(self) -> dict[str, Any]:
        return await self._request("GET", "/props")

    async def apply_template(self, messages: list[dict[str, str]]) -> str:
        value = await self._request(
            "POST",
            "/apply-template",
            body={"messages": messages, "add_generation_prompt": True},
        )
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > 1024 * 1024:
            raise LlamaRuntimeError("llama-server returned an invalid rendered prompt")
        return prompt

    async def complete(self, prompt: str, *, max_new_tokens: int) -> str:
        if not prompt or not 1 <= max_new_tokens <= 4096:
            raise LlamaRuntimeError("invalid deterministic completion request")
        value = await self._request(
            "POST",
            "/completion",
            body={
                "prompt": prompt,
                "n_predict": max_new_tokens,
                "temperature": 0,
                "seed": 0,
                "stream": False,
                "cache_prompt": False,
            },
            limit=16 * 1024 * 1024,
        )
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LlamaRuntimeError("llama-server returned an empty completion")
        return content.strip()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5.0)


@dataclass(frozen=True, slots=True)
class LlamaServerRuntimeFactory:
    binary: RuntimeBinary
    launch_spec: LlamaServerLaunchSpec = LlamaServerLaunchSpec()
    name: str = "llama_cpp"

    async def create(self, *, model_path: Path, model_sha256: str) -> GenerationRuntime:
        runtime = LlamaServerRuntime(
            binary=self.binary,
            model_path=model_path,
            model_sha256=model_sha256,
            launch_spec=self.launch_spec,
        )
        try:
            await runtime.start()
        except BaseException:
            # A cancelled background prewarm may already have launched the
            # private server. The manager cannot own it until create returns,
            # so the factory must close this partial runtime itself.
            with suppress(Exception):
                await _close_partial_runtime(runtime)
            raise
        return runtime


def packaged_runtime_factories(root: Path | None = None) -> tuple[LlamaServerRuntimeFactory, ...]:
    runtime_root = (root or (app_root() / "tools" / "local-polishing")).resolve()
    manifest_path = runtime_root / "runtime-manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return ()
    if (
        not isinstance(value, dict)
        or value.get("contract") != "ScriberLocalPolishingRuntimeManifestV1"
        or value.get("schemaVersion") != 1
        or value.get("runtime") != "llama.cpp"
        or not isinstance(value.get("platform"), dict)
        or not isinstance(value.get("files"), list)
    ):
        return ()
    try:
        declared: dict[str, tuple[int, str]] = {}
        for item in value["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise LlamaRuntimeError("invalid packaged runtime path")
            relative_value = item["path"]
            relative = PurePosixPath(relative_value)
            if (
                relative.is_absolute()
                or len(relative.parts) != 1
                or "\\" in relative_value
                or ".." in relative.parts
                or relative_value in declared
            ):
                raise LlamaRuntimeError("unsafe packaged runtime path")
            byte_size = item.get("bytes")
            file_sha = str(item.get("sha256", "")).removeprefix("sha256:")
            if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
                raise LlamaRuntimeError("invalid packaged runtime size")
            if not _SHA256.fullmatch(file_sha):
                raise LlamaRuntimeError("invalid packaged runtime hash")
            declared[relative_value] = (byte_size, file_sha)
        actual = {
            path.name for path in runtime_root.iterdir() if path.is_file() and path.name != "runtime-manifest.json"
        }
        if actual != set(declared) or "llama-server.exe" not in declared:
            raise LlamaRuntimeError("packaged runtime file set differs from its manifest")
        for name, (byte_size, file_sha) in declared.items():
            path = (runtime_root / name).resolve()
            if runtime_root not in path.parents or path.is_symlink() or not path.is_file():
                raise LlamaRuntimeError("packaged runtime escaped its root")
            if path.stat().st_size != byte_size or _file_sha256(path) != file_sha:
                raise LlamaRuntimeError("packaged runtime file differs from its manifest")
        platform = value["platform"]
        if platform.get("primaryBackend") != "vulkan" or platform.get("cpuFallback") is not True:
            raise LlamaRuntimeError("packaged runtime does not provide the required backends")
        executable = runtime_root / "llama-server.exe"
        server_sha = declared["llama-server.exe"][1]
        binaries = {
            "vulkan": RuntimeBinary("vulkan", executable, server_sha, "Vulkan0", "all"),
            "cpu": RuntimeBinary("cpu", executable, server_sha, "none", "0"),
        }
    except LlamaRuntimeError, OSError, TypeError, ValueError:
        return ()
    return tuple(LlamaServerRuntimeFactory(binaries[name], name=name) for name in ("vulkan", "cpu"))
