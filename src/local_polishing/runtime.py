"""Verified, loopback-only llama-server adapter for GGUF product inference."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from aiohttp import ClientSession, ClientTimeout

from src.runtime.paths import app_root

RuntimeName = Literal["vulkan", "cpu"]
CompletionFinishReason = Literal["eos", "stop", "limit", "unknown"]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VULKAN_DEVICE_LINE = re.compile(
    r"  Vulkan(?P<index>[0-9]+): (?P<name>[^\r\n]{1,256}) "
    r"\((?P<total>[1-9][0-9]*) MiB, (?P<free>[0-9]+) MiB free\)"
)
_RUNTIME_ENVIRONMENT_KEYS = frozenset(
    {
        "GGML_VK_VISIBLE_DEVICES",
        "GGML_VK_DISABLE_BFLOAT16",
        "LLAMA_ARG_DEVICE",
        "LLAMA_ARG_N_GPU_LAYERS",
    }
)


class LlamaRuntimeError(RuntimeError):
    """The native runtime or its bounded HTTP protocol failed."""


class VulkanBfloat16ExtensionError(LlamaRuntimeError):
    """The selected Vulkan device rejected b10158's BF16 extension request."""


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Bounded generation text plus the exact termination evidence."""

    content: str
    finish_reason: CompletionFinishReason
    tokens_predicted: int
    prompt_truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise LlamaRuntimeError("invalid completion content")
        if self.finish_reason not in {"eos", "stop", "limit", "unknown"}:
            raise LlamaRuntimeError("unsupported completion finish reason")
        if isinstance(self.tokens_predicted, bool) or self.tokens_predicted < 0:
            raise LlamaRuntimeError("invalid predicted-token count")
        if not isinstance(self.prompt_truncated, bool):
            raise LlamaRuntimeError("invalid prompt truncation flag")

    @property
    def stop_reached(self) -> bool:
        return self.finish_reason in {"eos", "stop"}


class GenerationRuntime(Protocol):
    @property
    def backend_name(self) -> str: ...

    async def properties(self) -> dict[str, Any]: ...
    async def apply_template(self, messages: list[dict[str, str]]) -> str: ...
    async def complete(self, prompt: str, *, max_new_tokens: int) -> CompletionResult: ...
    async def close(self) -> None: ...


class GenerationRuntimeFactory(Protocol):
    name: str

    async def create(self, *, model_path: Path, model_sha256: str) -> GenerationRuntime: ...


@dataclass(frozen=True, slots=True)
class VulkanDevice:
    physical_index: int
    description: str
    total_mib: int
    free_mib: int


@dataclass(frozen=True, slots=True)
class VulkanDeviceProbeResult:
    stdout: str
    stderr: str
    returncode: int


VulkanDeviceProbe = Callable[["RuntimeBinary", Mapping[str, str]], Awaitable[VulkanDeviceProbeResult | None]]
VulkanDeviceSelector = Callable[["RuntimeBinary"], Awaitable[int | None]]


def _validated_runtime_environment_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    if overrides is None:
        return {}
    normalized = dict(overrides)
    if not set(normalized) <= _RUNTIME_ENVIRONMENT_KEYS:
        raise LlamaRuntimeError("unsupported llama.cpp child environment override")
    visible = normalized.get("GGML_VK_VISIBLE_DEVICES")
    if visible is not None and (not re.fullmatch(r"0|[1-9][0-9]*", visible) or int(visible) > 63):
        raise LlamaRuntimeError("invalid Vulkan visibility index")
    disable_bfloat16 = normalized.get("GGML_VK_DISABLE_BFLOAT16")
    if disable_bfloat16 is not None and disable_bfloat16 != "1":
        raise LlamaRuntimeError("invalid Vulkan BF16 compatibility value")
    return normalized


def _windows_directory() -> Path:
    if os.name != "nt":
        raise LlamaRuntimeError("Windows directory requested on a non-Windows host")
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise LlamaRuntimeError("unable to resolve the trusted Windows directory")
    return Path(buffer.value).resolve(strict=True)


def _child_environment(
    overrides: Mapping[str, str] | None = None,
    *,
    runtime_directory: Path | None = None,
    temporary_directory: Path | None = None,
) -> dict[str, str]:
    runtime_root = (runtime_directory or Path(sys.executable).resolve().parent).resolve(strict=True)
    temporary_root = (temporary_directory or Path(tempfile.gettempdir())).resolve(strict=True)
    environment = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    if os.name == "nt":
        windows = _windows_directory()
        system32 = windows / "System32"
        environment.update(
            {
                "SystemRoot": str(windows),
                "WINDIR": str(windows),
                "PATH": os.pathsep.join((str(runtime_root), str(system32))),
                "TEMP": str(temporary_root),
                "TMP": str(temporary_root),
            }
        )
    else:
        environment.update(
            {
                "HOME": str(temporary_root),
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(temporary_root),
            }
        )
    environment.update(_validated_runtime_environment_overrides(overrides))
    return environment


def parse_vulkan_device_list(value: str) -> tuple[VulkanDevice, ...]:
    """Parse only b10158's exact `--list-devices` stdout contract."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > 16 * 1024:
        raise LlamaRuntimeError("Vulkan device list exceeded its product bound")
    lines = value.splitlines()
    if len(lines) < 2 or lines[0] != "Available devices:" or any(not line for line in lines[1:]):
        raise LlamaRuntimeError("invalid Vulkan device list header")
    if lines[1:] == ["  (none)"]:
        return ()
    devices: list[VulkanDevice] = []
    for line in lines[1:]:
        match = _VULKAN_DEVICE_LINE.fullmatch(line)
        if match is None:
            raise LlamaRuntimeError("invalid Vulkan device row")
        description = match.group("name")
        if any(ord(character) < 32 or ord(character) == 127 for character in description):
            raise LlamaRuntimeError("invalid Vulkan device description")
        total_mib = int(match.group("total"))
        free_mib = int(match.group("free"))
        if free_mib > total_mib:
            raise LlamaRuntimeError("invalid Vulkan device memory evidence")
        devices.append(
            VulkanDevice(
                physical_index=int(match.group("index")),
                description=description,
                total_mib=total_mib,
                free_mib=free_mib,
            )
        )
    if [device.physical_index for device in devices] != list(range(len(devices))):
        raise LlamaRuntimeError("Vulkan device indices are not contiguous")
    return tuple(devices)


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


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        normalized = self.sha256.removeprefix("sha256:")
        relative = PurePosixPath(self.name)
        if relative.is_absolute() or len(relative.parts) != 1 or "\\" in self.name or self.name in {"", ".", ".."}:
            raise LlamaRuntimeError("runtime file name must be one safe basename")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 1:
            raise LlamaRuntimeError("runtime file size must be positive")
        if not _SHA256.fullmatch(normalized):
            raise LlamaRuntimeError("runtime file hash must be exact SHA-256")
        object.__setattr__(self, "sha256", normalized)


@dataclass(frozen=True, slots=True)
class RuntimeBinary:
    name: RuntimeName
    executable: Path
    sha256: str
    device: str
    gpu_layers: str
    runtime_files: tuple[RuntimeFile, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in {"vulkan", "cpu"}:
            raise LlamaRuntimeError("unsupported llama.cpp runtime name")
        normalized = self.sha256.removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise LlamaRuntimeError("runtime hash must be exact SHA-256")
        if not self.device or not self.gpu_layers:
            raise LlamaRuntimeError("runtime device and GPU layer policy are required")
        if not isinstance(self.runtime_files, tuple) or len({item.name for item in self.runtime_files}) != len(
            self.runtime_files
        ):
            raise LlamaRuntimeError("runtime files must be one exact unique tuple")
        if self.runtime_files and self.executable.name not in {item.name for item in self.runtime_files}:
            raise LlamaRuntimeError("runtime files must include the server executable")
        object.__setattr__(self, "sha256", normalized)
        object.__setattr__(self, "executable", self.executable.resolve())


@dataclass(frozen=True, slots=True)
class _RuntimeSnapshot:
    workspace: Path
    runtime_root: Path
    temporary_root: Path
    binary: RuntimeBinary


def _runtime_file_specs(binary: RuntimeBinary) -> tuple[RuntimeFile, ...]:
    if binary.runtime_files:
        return binary.runtime_files
    executable = binary.executable
    try:
        size_bytes = executable.stat().st_size
    except OSError as exc:
        raise LlamaRuntimeError("runtime executable is unavailable") from exc
    return (RuntimeFile(executable.name, size_bytes, binary.sha256),)


def _verify_runtime_source(binary: RuntimeBinary) -> tuple[RuntimeFile, ...]:
    specs = _runtime_file_specs(binary)
    root = binary.executable.parent
    actual: set[str] = set()
    for item in specs:
        path = root / item.name
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise LlamaRuntimeError("runtime file is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(metadata)
            or metadata.st_nlink != 1
            or metadata.st_size != item.size_bytes
            or _file_sha256(path) != item.sha256
        ):
            raise LlamaRuntimeError("runtime file hash mismatch")
        actual.add(item.name)
    if binary.executable.name not in actual:
        raise LlamaRuntimeError("runtime executable is not manifest-bound")
    return specs


def _create_runtime_snapshot(binary: RuntimeBinary) -> _RuntimeSnapshot:
    specs = _verify_runtime_source(binary)
    workspace = Path(tempfile.mkdtemp(prefix="scriber-llama-runtime-")).resolve(strict=True)
    runtime_root = workspace / "runtime"
    temporary_root = workspace / "temp"
    runtime_root.mkdir(mode=0o700)
    temporary_root.mkdir(mode=0o700)
    try:
        source_root = binary.executable.parent
        for item in specs:
            source = source_root / item.name
            destination = runtime_root / item.name
            with source.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if destination.stat().st_size != item.size_bytes or _file_sha256(destination) != item.sha256:
                raise LlamaRuntimeError("private runtime snapshot hash mismatch")
        _verify_runtime_source(binary)
        snapshot_binary = RuntimeBinary(
            binary.name,
            runtime_root / binary.executable.name,
            binary.sha256,
            binary.device,
            binary.gpu_layers,
            specs,
        )
        snapshot = _RuntimeSnapshot(workspace, runtime_root, temporary_root, snapshot_binary)
        _verify_runtime_snapshot(snapshot)
        for item in specs:
            os.chmod(runtime_root / item.name, stat.S_IREAD)
        os.chmod(runtime_root, stat.S_IREAD | stat.S_IEXEC)
        return snapshot
    except BaseException:
        _remove_runtime_snapshot(workspace)
        raise


def _verify_runtime_snapshot(snapshot: _RuntimeSnapshot) -> None:
    specs = snapshot.binary.runtime_files
    try:
        entries = tuple(snapshot.runtime_root.iterdir())
    except OSError as exc:
        raise LlamaRuntimeError("private runtime snapshot is unavailable") from exc
    if {entry.name for entry in entries} != {item.name for item in specs}:
        raise LlamaRuntimeError("private runtime snapshot file set changed")
    for item in specs:
        path = snapshot.runtime_root / item.name
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise LlamaRuntimeError("private runtime snapshot file is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(metadata)
            or metadata.st_nlink != 1
            or metadata.st_size != item.size_bytes
            or _file_sha256(path) != item.sha256
        ):
            raise LlamaRuntimeError("private runtime snapshot hash mismatch")


def _remove_runtime_snapshot(workspace: Path | None) -> None:
    if workspace is None or workspace.is_symlink() or not workspace.exists():
        return
    for current_root, directories, filenames in os.walk(workspace, topdown=False, followlinks=False):
        current = Path(current_root)
        for name in filenames:
            with suppress(OSError):
                os.chmod(current / name, stat.S_IREAD | stat.S_IWRITE)
        for name in directories:
            with suppress(OSError):
                os.chmod(current / name, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    with suppress(OSError):
        os.chmod(workspace, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    shutil.rmtree(workspace, ignore_errors=True)


async def _stop_probe_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(Exception):
        await process.wait()


async def _run_vulkan_device_probe(
    binary: RuntimeBinary,
    environment_overrides: Mapping[str, str],
) -> VulkanDeviceProbeResult | None:
    try:
        snapshot = await asyncio.to_thread(_create_runtime_snapshot, binary)
    except LlamaRuntimeError, OSError:
        return None
    try:
        await asyncio.to_thread(_verify_runtime_snapshot, snapshot)
        process = await asyncio.create_subprocess_exec(
            str(snapshot.binary.executable),
            "--list-devices",
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_environment(
                environment_overrides,
                runtime_directory=snapshot.runtime_root,
                temporary_directory=snapshot.temporary_root,
            ),
            cwd=str(snapshot.runtime_root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError, LlamaRuntimeError:
        await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
    except TimeoutError:
        await _stop_probe_process(process)
        await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
        return None
    except BaseException:
        await _stop_probe_process(process)
        await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
        raise
    await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
    if len(stdout) > 16 * 1024 or len(stderr) > 16 * 1024:
        return None
    try:
        decoded_stdout = stdout.decode("utf-8")
        decoded_stderr = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return VulkanDeviceProbeResult(
        stdout=decoded_stdout,
        stderr=decoded_stderr,
        returncode=process.returncode if process.returncode is not None else -1,
    )


async def select_preferred_vulkan_device(
    binary: RuntimeBinary,
    *,
    probe: VulkanDeviceProbe | None = None,
) -> int | None:
    """Return one verified physical NVIDIA index, otherwise fail closed."""

    try:
        if await asyncio.to_thread(_file_sha256, binary.executable) != binary.sha256:
            return None
    except LlamaRuntimeError, OSError:
        return None
    probe_runner = probe or _run_vulkan_device_probe
    initial = await probe_runner(binary, {})
    if initial is None or initial.returncode != 0 or initial.stderr:
        return None
    try:
        devices = parse_vulkan_device_list(initial.stdout)
    except LlamaRuntimeError:
        return None
    candidates = [device for device in devices if device.description.casefold().startswith("nvidia ")]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    isolated = await probe_runner(binary, {"GGML_VK_VISIBLE_DEVICES": str(candidate.physical_index)})
    if isolated is None or isolated.returncode != 0 or isolated.stderr:
        return None
    try:
        isolated_devices = parse_vulkan_device_list(isolated.stdout)
    except LlamaRuntimeError:
        return None
    if (
        len(isolated_devices) != 1
        or isolated_devices[0].physical_index != 0
        or isolated_devices[0].description != candidate.description
    ):
        return None
    return candidate.physical_index


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


def _reserve_loopback_port() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.set_inheritable(False)
        listener.bind(("127.0.0.1", 0))
        return listener
    except BaseException:
        listener.close()
        raise


def _windows_process_owns_loopback_listener(pid: int, port: int) -> bool:
    class TcpRowOwnerPid(ctypes.Structure):
        _fields_ = [
            ("state", ctypes.c_uint32),
            ("local_address", ctypes.c_uint32),
            ("local_port", ctypes.c_uint32),
            ("remote_address", ctypes.c_uint32),
            ("remote_port", ctypes.c_uint32),
            ("owner_pid", ctypes.c_uint32),
        ]

    get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    get_table.restype = ctypes.c_uint32
    size = ctypes.c_uint32(0)
    result = get_table(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if result not in {0, 122} or size.value < ctypes.sizeof(ctypes.c_uint32):
        return False
    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
        return False
    count = ctypes.c_uint32.from_buffer_copy(buffer.raw[:4]).value
    row_size = ctypes.sizeof(TcpRowOwnerPid)
    expected_address = int.from_bytes(socket.inet_aton("127.0.0.1"), sys.byteorder)
    for index in range(count):
        offset = 4 + (index * row_size)
        if offset + row_size > size.value:
            return False
        row = TcpRowOwnerPid.from_buffer_copy(buffer.raw[offset : offset + row_size])
        if (
            row.state == 2
            and row.owner_pid == pid
            and socket.ntohs(row.local_port & 0xFFFF) == port
            and row.local_address == expected_address
        ):
            return True
    return False


def _linux_process_owns_loopback_listener(pid: int, port: int) -> bool:
    expected = f"0100007F:{port:04X}"
    inodes: set[str] = set()
    try:
        rows = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    except OSError, UnicodeDecodeError:
        return False
    for row in rows:
        fields = row.split()
        if len(fields) >= 10 and fields[1].upper() == expected and fields[3] == "0A":
            inodes.add(fields[9])
    if not inodes:
        return False
    descriptor_root = Path(f"/proc/{pid}/fd")
    try:
        descriptors = tuple(descriptor_root.iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.startswith("socket:[") and target.removeprefix("socket:[").removesuffix("]") in inodes:
            return True
    return False


def _process_owns_loopback_listener(pid: int, port: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not 1 <= port <= 65_535:
        return False
    if sys.platform == "win32":
        return _windows_process_owns_loopback_listener(pid, port)
    if sys.platform == "linux":
        return _linux_process_owns_loopback_listener(pid, port)
    return False


def _loopback_listener_present(port: int) -> bool:
    """Probe listener presence without sending the private API credential."""

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.05)
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


async def _capture_bounded_stderr(reader: asyncio.StreamReader, limit: int = 64 * 1024) -> bytes:
    captured = bytearray()
    while block := await reader.read(4096):
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(block[:remaining])
    return bytes(captured)


def _is_bfloat16_extension_failure(stderr: bytes) -> bool:
    try:
        value = stderr.decode("utf-8", errors="replace")
    except Exception:
        return False
    # b10158 does not print the requested extension name before this pinned
    # Vulkan-Hpp failure, including with the product's --log-disable setting.
    # The retry remains safe and narrow: one fresh process removes only BF16;
    # any other missing extension will fail again and then fall back to CPU.
    return "vk::PhysicalDevice::createDevice: ErrorExtensionNotPresent" in value


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
        environment_overrides: Mapping[str, str] | None = None,
        backend_name: str | None = None,
    ) -> None:
        self.backend_name = backend_name or binary.name
        self._binary = binary
        self._model_path = model_path.resolve()
        self._model_sha256 = model_sha256.removeprefix("sha256:")
        self._launch_spec = launch_spec
        self._startup_timeout = startup_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._environment_overrides = _validated_runtime_environment_overrides(environment_overrides)
        self._port_reservation = _reserve_loopback_port()
        self._port = int(self._port_reservation.getsockname()[1])
        self._api_key = secrets.token_urlsafe(32)
        self._process: asyncio.subprocess.Process | None = None
        self._session: ClientSession | None = None
        self._stderr_task: asyncio.Task[bytes] | None = None
        self._runtime_snapshot: _RuntimeSnapshot | None = None
        self._listener_verified = False

    def _release_port_reservation(self) -> None:
        reservation = self._port_reservation
        self._port_reservation = None
        if reservation is not None:
            reservation.close()

    async def _finish_stderr_capture(self) -> bytes:
        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return b""
        try:
            return await task
        except Exception:
            return b""

    async def start(self) -> None:
        snapshot: _RuntimeSnapshot | None = None
        try:
            snapshot = await asyncio.to_thread(_create_runtime_snapshot, self._binary)
            if await asyncio.to_thread(_file_sha256, self._model_path) != self._model_sha256:
                raise LlamaRuntimeError("GGUF model hash mismatch")
            await asyncio.to_thread(_verify_runtime_snapshot, snapshot)
        except BaseException:
            self._release_port_reservation()
            if snapshot is not None:
                await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
            raise
        assert snapshot is not None
        self._runtime_snapshot = snapshot
        command = self._launch_spec.command(
            binary=snapshot.binary,
            model_path=self._model_path,
            port=self._port,
            api_key=self._api_key,
        )
        await asyncio.to_thread(_verify_runtime_snapshot, snapshot)
        if await asyncio.to_thread(_file_sha256, self._model_path) != self._model_sha256:
            await self.close()
            raise LlamaRuntimeError("GGUF model changed immediately before spawn")
        self._release_port_reservation()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_child_environment(
                    self._environment_overrides,
                    runtime_directory=snapshot.runtime_root,
                    temporary_directory=snapshot.temporary_root,
                ),
                cwd=str(snapshot.runtime_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            await self.close()
            raise
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(_capture_bounded_stderr(self._process.stderr))
        self._session = ClientSession(timeout=ClientTimeout(total=self._request_timeout), trust_env=False)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                stderr = await self._finish_stderr_capture()
                await self.close()
                if self._binary.name == "vulkan" and _is_bfloat16_extension_failure(stderr):
                    raise VulkanBfloat16ExtensionError("Vulkan BF16 extension is unavailable")
                raise LlamaRuntimeError("llama-server exited during startup")
            pid = getattr(self._process, "pid", None)
            if not await asyncio.to_thread(_process_owns_loopback_listener, pid, self._port):
                if await asyncio.to_thread(_loopback_listener_present, self._port):
                    await self.close()
                    raise LlamaRuntimeError("llama-server listener ownership verification failed")
                await asyncio.sleep(0.05)
                continue
            try:
                payload = await self._request("GET", "/health", limit=64 * 1024)
            except LlamaRuntimeError:
                payload = {}
            if payload.get("status") == "ok":
                if not await asyncio.to_thread(_process_owns_loopback_listener, pid, self._port):
                    await self.close()
                    raise LlamaRuntimeError("llama-server listener ownership verification failed")
                self._listener_verified = True
                return
            await asyncio.sleep(0.05)
        await self.close()
        raise LlamaRuntimeError("llama-server startup timed out")

    async def _request(
        self, method: str, path: str, *, body: object | None = None, limit: int = 1024 * 1024
    ) -> dict[str, Any]:
        if self._session is None or self._process is None or self._process.returncode is not None:
            raise LlamaRuntimeError("llama-server is not running")
        if not await asyncio.to_thread(
            _process_owns_loopback_listener,
            getattr(self._process, "pid", None),
            self._port,
        ):
            raise LlamaRuntimeError("llama-server listener ownership was lost")
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

    async def complete(self, prompt: str, *, max_new_tokens: int) -> CompletionResult:
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
        tokens_predicted = value.get("tokens_predicted")
        stop = value.get("stop")
        # llama.cpp b10158's non-OAI /completion contract exposes the
        # final reason as stop_type: eos | word | limit | none.
        stop_type = value.get("stop_type")
        truncated = value.get("truncated")
        if not isinstance(content, str):
            raise LlamaRuntimeError("llama-server returned invalid completion content")
        if isinstance(tokens_predicted, bool) or not isinstance(tokens_predicted, int) or tokens_predicted < 0:
            raise LlamaRuntimeError("llama-server returned invalid completion token evidence")
        if stop is not True or not isinstance(truncated, bool):
            raise LlamaRuntimeError("llama-server returned incomplete termination evidence")
        if stop_type == "limit":
            finish_reason: CompletionFinishReason = "limit"
        elif stop_type == "eos":
            finish_reason = "eos"
        elif stop_type == "word":
            finish_reason = "stop"
        elif stop_type == "none":
            finish_reason = "unknown"
        else:
            raise LlamaRuntimeError("llama-server returned an invalid stop type")
        return CompletionResult(
            content=content,
            finish_reason=finish_reason,
            tokens_predicted=tokens_predicted,
            prompt_truncated=truncated,
        )

    async def close(self) -> None:
        self._release_port_reservation()
        self._listener_verified = False
        if self._session is not None:
            await self._session.close()
            self._session = None
        process = self._process
        self._process = None
        if process is None:
            await self._finish_stderr_capture()
            snapshot = self._runtime_snapshot
            self._runtime_snapshot = None
            if snapshot is not None:
                await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)
            return
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await asyncio.wait_for(process.wait(), timeout=5.0)
        await self._finish_stderr_capture()
        snapshot = self._runtime_snapshot
        self._runtime_snapshot = None
        if snapshot is not None:
            await asyncio.to_thread(_remove_runtime_snapshot, snapshot.workspace)


@dataclass(frozen=True, slots=True)
class LlamaServerRuntimeFactory:
    binary: RuntimeBinary
    launch_spec: LlamaServerLaunchSpec = LlamaServerLaunchSpec()
    name: str = "llama_cpp"
    vulkan_device_selector: VulkanDeviceSelector = select_preferred_vulkan_device
    allow_bfloat16_compatibility_retry: bool = False
    disable_bfloat16_on_first_launch: bool = False

    async def _start_one(
        self,
        *,
        model_path: Path,
        model_sha256: str,
        environment_overrides: Mapping[str, str],
        backend_name: str,
    ) -> GenerationRuntime:
        runtime = LlamaServerRuntime(
            binary=self.binary,
            model_path=model_path,
            model_sha256=model_sha256,
            launch_spec=self.launch_spec,
            environment_overrides=environment_overrides,
            backend_name=backend_name,
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

    async def create(self, *, model_path: Path, model_sha256: str) -> GenerationRuntime:
        if self.binary.name != "vulkan":
            return await self._start_one(
                model_path=model_path,
                model_sha256=model_sha256,
                environment_overrides={},
                backend_name=self.binary.name,
            )
        physical_index = await self.vulkan_device_selector(self.binary)
        if physical_index is None:
            raise LlamaRuntimeError("no unambiguous preferred Vulkan device is available")
        environment_overrides = {"GGML_VK_VISIBLE_DEVICES": str(physical_index)}
        if self.disable_bfloat16_on_first_launch:
            environment_overrides["GGML_VK_DISABLE_BFLOAT16"] = "1"
        try:
            return await self._start_one(
                model_path=model_path,
                model_sha256=model_sha256,
                environment_overrides=environment_overrides,
                backend_name=("vulkan_compat" if self.disable_bfloat16_on_first_launch else "vulkan"),
            )
        except VulkanBfloat16ExtensionError:
            if self.disable_bfloat16_on_first_launch or not self.allow_bfloat16_compatibility_retry:
                raise
        return await self._start_one(
            model_path=model_path,
            model_sha256=model_sha256,
            environment_overrides={
                **environment_overrides,
                "GGML_VK_DISABLE_BFLOAT16": "1",
            },
            backend_name="vulkan_compat",
        )


def packaged_runtime_factories(root: Path | None = None) -> tuple[LlamaServerRuntimeFactory, ...]:
    runtime_source = root or (app_root() / "tools" / "local-polishing")
    try:
        root_metadata = os.lstat(runtime_source)
        root_is_junction = getattr(runtime_source, "is_junction", lambda: False)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or _is_reparse_point(root_metadata)
            or root_is_junction()
        ):
            return ()
        runtime_root = runtime_source.resolve(strict=True)
    except OSError:
        return ()
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
            source_path = runtime_root / name
            metadata = os.lstat(source_path)
            path = source_path.resolve(strict=True)
            if (
                runtime_root not in path.parents
                or not stat.S_ISREG(metadata.st_mode)
                or _is_reparse_point(metadata)
                or metadata.st_nlink != 1
            ):
                raise LlamaRuntimeError("packaged runtime escaped its root")
            if metadata.st_size != byte_size or _file_sha256(path) != file_sha:
                raise LlamaRuntimeError("packaged runtime file differs from its manifest")
        platform = value["platform"]
        if platform.get("primaryBackend") != "vulkan" or platform.get("cpuFallback") is not True:
            raise LlamaRuntimeError("packaged runtime does not provide the required backends")
        executable = runtime_root / "llama-server.exe"
        server_sha = declared["llama-server.exe"][1]
        runtime_files = tuple(
            RuntimeFile(name, byte_size, file_sha) for name, (byte_size, file_sha) in sorted(declared.items())
        )
        binaries = {
            "vulkan": RuntimeBinary("vulkan", executable, server_sha, "Vulkan0", "all", runtime_files),
            "cpu": RuntimeBinary("cpu", executable, server_sha, "none", "0", runtime_files),
        }
    except LlamaRuntimeError, OSError, TypeError, ValueError:
        return ()
    return (
        LlamaServerRuntimeFactory(
            binaries["vulkan"],
            name="vulkan",
            allow_bfloat16_compatibility_retry=True,
            disable_bfloat16_on_first_launch=True,
        ),
        LlamaServerRuntimeFactory(binaries["cpu"], name="cpu"),
    )
