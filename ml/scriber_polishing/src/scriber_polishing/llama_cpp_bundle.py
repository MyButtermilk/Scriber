"""Verification of one complete, commit-bound local llama.cpp toolchain bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

_SCHEMA = Path(__file__).parents[2] / "contracts" / "llama_cpp_bundle_lock_schema.json"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OCI_TAG = re.compile(r"^full-cuda12-b[0-9]+$")
_RUNTIME_IMAGE = re.compile(r"^pytorch/pytorch@sha256:[0-9a-f]{64}$")
_OCI_LAYER_PURPOSES = frozenset({"native_libraries", "tools_and_conversion"})


class LlamaCppBundleError(RuntimeError):
    """The selected llama.cpp source/tool bundle does not match its reviewed lock."""


@dataclass(frozen=True, slots=True)
class VerifiedLlamaCppBundle:
    root: Path
    lock_path: Path
    lock_sha256: str
    commit: str
    source_tree_sha256: str
    build_identity_sha256: str
    build: dict[str, Any]
    distribution: dict[str, Any]
    native_runtime: dict[str, Any]
    runtime_library_paths: tuple[Path, ...]
    convert_hf_to_gguf: Path
    llama_quantize: Path
    llama_cli: Path
    llama_server: Path


@dataclass(frozen=True, slots=True)
class LocalProbeResult:
    """Bounded output from a local source-control or tool version probe."""

    returncode: int
    stdout: bytes
    stderr: bytes


class LocalProbeRunner(Protocol):
    """No-network subprocess seam used by lock materialization."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> LocalProbeResult: ...


class SubprocessLocalProbeRunner:
    """Run one local command without a shell and retain bounded raw output."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> LocalProbeResult:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LlamaCppBundleError(f"local llama.cpp probe failed: {error}") from error
        return LocalProbeResult(
            returncode=completed.returncode,
            stdout=completed.stdout[-128 * 1024 :],
            stderr=completed.stderr[-128 * 1024 :],
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise LlamaCppBundleError(f"bundle entry is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _inventory(root: Path, *, prefix: str | None = None) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise LlamaCppBundleError(f"bundle source root is not a regular directory: {root}")
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if prefix is not None and not relative.startswith(prefix + "/"):
            continue
        if path.is_symlink() or not path.is_file():
            raise LlamaCppBundleError(f"bundle source contains a non-regular file: {relative}")
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if not files:
        raise LlamaCppBundleError(f"bundle source inventory is empty{f' for {prefix}' if prefix else ''}")
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


def _safe_bundle_relative(value: str, label: str) -> str:
    if (
        not value
        or "\\" in value
        or Path(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise LlamaCppBundleError(f"{label} must be a safe POSIX relative path")
    return value


def _is_elf64_x86_64(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        header = stream.read(20)
    return (
        len(header) >= 20
        and header[:4] == b"\x7fELF"
        and header[4] == 2
        and header[5] == 1
        and header[6] == 1
        and int.from_bytes(header[18:20], "little") == 62
    )


def _is_elf(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) == b"\x7fELF"


def _require_elf64_x86_64(path: Path, label: str) -> None:
    if not _is_elf64_x86_64(path):
        raise LlamaCppBundleError(f"{label} must be a little-endian ELF64 x86-64 object")


_ALLOWED_EMPTY_NATIVE_RUNTIME_FILES = frozenset(
    {
        # PEP 561 marker shipped by llama.cpp's gguf-py package. Its required
        # representation is an empty regular file; every other runtime file
        # remains subject to the fail-closed non-empty check below.
        "runtime/app/gguf-py/gguf/py.typed",
    }
)


def _native_runtime_inventory(
    bundle_root: Path,
    runtime_relative: str,
) -> dict[str, object]:
    relative_root = _safe_bundle_relative(runtime_relative, "native runtime root")
    runtime_candidate = bundle_root / relative_root
    cursor = bundle_root
    for part in Path(relative_root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise LlamaCppBundleError("native runtime root must not traverse a symlink")
    runtime_root = runtime_candidate.resolve()
    if not runtime_root.is_dir():
        raise LlamaCppBundleError("native runtime root must be a regular directory")
    if Path(os.path.commonpath([bundle_root, runtime_root])) != bundle_root:
        raise LlamaCppBundleError("native runtime root escapes the bundle")
    entries: list[dict[str, object]] = []
    library_directories: set[str] = set()
    has_shared_library = False
    for path in sorted(
        runtime_root.rglob("*"),
        key=lambda item: item.relative_to(bundle_root).as_posix(),
    ):
        relative = path.relative_to(bundle_root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or "\\" in target:
                raise LlamaCppBundleError(f"native runtime symlink target must be relative: {relative}")
            try:
                resolved = path.resolve(strict=True)
                common = Path(os.path.commonpath([runtime_root, resolved]))
            except (OSError, ValueError) as error:
                raise LlamaCppBundleError(f"native runtime symlink is broken or unsafe: {relative}") from error
            if common != runtime_root or not resolved.is_file():
                raise LlamaCppBundleError(f"native runtime symlink escapes its root: {relative}")
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": target,
                    "sha256": _sha256(b"symlink\0" + target.encode("utf-8")),
                }
            )
            if re.search(r"\.so(?:\.|$)", path.name):
                _require_elf64_x86_64(
                    resolved,
                    f"native shared-library target {relative}",
                )
                has_shared_library = True
                library_directories.add(path.parent.relative_to(bundle_root).as_posix())
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise LlamaCppBundleError(f"native runtime contains a non-file entry: {relative}")
        size = path.stat().st_size
        if size < 1 and relative not in _ALLOWED_EMPTY_NATIVE_RUNTIME_FILES:
            raise LlamaCppBundleError(f"native runtime file is empty: {relative}")
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "bytes": size,
                "sha256": _sha256_file(path),
            }
        )
        is_shared_library = re.search(r"\.so(?:\.|$)", path.name) is not None
        if is_shared_library or _is_elf(path):
            _require_elf64_x86_64(
                path,
                f"native ELF object {relative}",
            )
        if is_shared_library:
            has_shared_library = True
            library_directories.add(path.parent.relative_to(bundle_root).as_posix())
    if not entries or not has_shared_library or not library_directories:
        raise LlamaCppBundleError("native runtime must contain at least one shared library")
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(_canonical_json(entry))
        digest.update(b"\n")
    return {
        "root": relative_root,
        "algorithm": "sha256-path-kind-size-content-or-target-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "entry_count": len(entries),
        "total_file_bytes": sum(int(entry["bytes"]) for entry in entries if entry["kind"] == "file"),
        "entries": entries,
        "library_directories": sorted(library_directories),
    }


_LDD_ADDRESS = re.compile(r"\s+\(0x[0-9a-fA-F]+\)\s*$")
_LDD_ALLOWED_IMAGE_PREFIXES = (
    "/lib/",
    "/lib64/",
    "/usr/lib/",
    "/usr/lib64/",
    "/usr/local/cuda/",
    "/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib/",
    "/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib/",
    "/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/",
)
CUDA_HOST_DRIVER_SONAMES = ("libcuda.so.1",)
_SUPPORTED_REQUIRED_HOST_DRIVER_SONAMES = frozenset(CUDA_HOST_DRIVER_SONAMES)
_NVIDIA_DRIVER_PREFIXES = (
    "/usr/local/nvidia/",
    "/usr/lib/x86_64-linux-gnu/",
    "/lib/x86_64-linux-gnu/",
    "/usr/lib64/",
    "/lib64/",
)
_MISSING_LDD_DEPENDENCY = re.compile(r"^([^\s=>]+) => not found$")


def _validated_required_host_driver_sonames(
    value: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple | list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
        or set(value) - _SUPPORTED_REQUIRED_HOST_DRIVER_SONAMES
    ):
        raise LlamaCppBundleError(
            "required host-driver SONAMEs must contain only exact libcuda.so.1"
        )
    return tuple(sorted(value))


def _normalize_ldd_output(payload: bytes, bundle_root: Path) -> str:
    text = payload.decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    root_variants = {
        str(bundle_root),
        bundle_root.as_posix(),
        str(bundle_root).replace("\\", "/"),
    }
    for root in sorted(root_variants, key=len, reverse=True):
        text = text.replace(root, "${BUNDLE_ROOT}")
    text = text.replace("\\", "/")
    lines = [_LDD_ADDRESS.sub("", line.strip()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def _ldd_dependencies(
    normalized_stdout: str,
    *,
    inventory_paths: set[str],
    required_host_driver_sonames: tuple[str, ...] | list[str] = (),
    allow_unresolved_host_drivers: bool = False,
) -> list[dict[str, str]]:
    dependencies, _ = _parse_ldd_dependencies(
        normalized_stdout,
        inventory_paths=inventory_paths,
        required_host_driver_sonames=required_host_driver_sonames,
        allow_unresolved_host_drivers=allow_unresolved_host_drivers,
    )
    return dependencies


def _parse_ldd_dependencies(
    normalized_stdout: str,
    *,
    inventory_paths: set[str],
    required_host_driver_sonames: tuple[str, ...] | list[str],
    allow_unresolved_host_drivers: bool,
) -> tuple[list[dict[str, str]], set[str]]:
    required = set(
        _validated_required_host_driver_sonames(required_host_driver_sonames)
    )
    dependencies: list[dict[str, str]] = []
    unresolved_host_drivers: set[str] = set()
    for line in normalized_stdout.splitlines():
        if line == "statically linked":
            continue
        missing = _MISSING_LDD_DEPENDENCY.fullmatch(line)
        if missing is not None:
            soname = missing.group(1)
            if not allow_unresolved_host_drivers or soname not in required:
                raise LlamaCppBundleError(f"native runtime dependency is missing: {line}")
            dependencies.append(
                {
                    "soname": soname,
                    "path": f"${{NVIDIA_DRIVER}}/{soname}",
                    "provider": "nvidia_driver_required",
                }
            )
            unresolved_host_drivers.add(soname)
            continue
        if "not found" in line:
            raise LlamaCppBundleError(f"native runtime dependency is missing: {line}")
        if "=>" in line:
            soname, path = (part.strip() for part in line.split("=>", 1))
        else:
            path = line.strip()
            soname = Path(path).name
        if (
            not soname
            or not path
            or any(character.isspace() for character in soname)
            or any(character.isspace() for character in path)
        ):
            raise LlamaCppBundleError(f"ldd dependency line is malformed: {line}")
        if soname in required:
            if (
                Path(path).name != soname
                or "/stubs/" in path
                or not path.startswith(_NVIDIA_DRIVER_PREFIXES)
            ):
                raise LlamaCppBundleError(
                    f"required {soname} did not resolve from a real NVIDIA driver: {path}"
                )
            dependencies.append(
                {
                    "soname": soname,
                    "path": f"${{NVIDIA_DRIVER}}/{soname}",
                    "provider": "nvidia_driver_required",
                }
            )
            continue
        if path.startswith("linux-vdso.so."):
            provider = "kernel"
        elif path.startswith("${BUNDLE_ROOT}/"):
            relative = path.removeprefix("${BUNDLE_ROOT}/")
            if relative not in inventory_paths:
                raise LlamaCppBundleError(f"ldd resolved an untracked bundle dependency: {relative}")
            provider = "bundle"
        elif path.startswith("/usr/local/nvidia/"):
            provider = "nvidia_driver"
        elif path.startswith(_LDD_ALLOWED_IMAGE_PREFIXES):
            provider = "runtime_image"
        else:
            raise LlamaCppBundleError(f"ldd dependency is outside the locked closure: {path}")
        dependencies.append(
            {
                "soname": soname,
                "path": path,
                "provider": provider,
            }
        )
    return dependencies, unresolved_host_drivers


def _probe_ldd_closure(
    bundle_root: Path,
    native_runtime: dict[str, object],
    runner: LocalProbeRunner,
    *,
    required_host_driver_sonames: tuple[str, ...] | list[str],
    allow_unresolved_host_drivers: bool,
) -> tuple[dict[str, object], set[str]]:
    required = _validated_required_host_driver_sonames(
        required_host_driver_sonames
    )
    inventory_paths = {
        str(entry["path"])
        for entry in native_runtime["entries"]
        if isinstance(entry, dict)
    }
    elf_paths = [
        path
        for path in sorted(inventory_paths)
        if (bundle_root / path).is_file()
        and not (bundle_root / path).is_symlink()
        and _is_elf64_x86_64(bundle_root / path)
    ]
    if not elf_paths:
        raise LlamaCppBundleError("native runtime contains no ELF64 x86-64 objects")
    entries: list[dict[str, object]] = []
    observed_required: set[str] = set()
    unresolved_host_drivers: set[str] = set()
    for relative in elf_paths:
        result = _checked_probe(
            runner,
            ["ldd", str((bundle_root / relative).resolve())],
            cwd=bundle_root,
            label=f"{relative} ldd closure",
        )
        stdout = _normalize_ldd_output(result.stdout, bundle_root)
        stderr = _normalize_ldd_output(result.stderr, bundle_root)
        if stderr:
            raise LlamaCppBundleError(
                f"{relative} ldd closure emitted diagnostics on stderr: {stderr[:1000]}"
            )
        dependencies, unresolved = _parse_ldd_dependencies(
            stdout,
            inventory_paths=inventory_paths,
            required_host_driver_sonames=required,
            allow_unresolved_host_drivers=allow_unresolved_host_drivers,
        )
        observed_required.update(
            dependency["soname"]
            for dependency in dependencies
            if dependency["provider"] == "nvidia_driver_required"
        )
        unresolved_host_drivers.update(unresolved)
        entries.append(
            {
                "path": relative,
                "output_sha256": _sha256(
                    _canonical_json(
                        {
                            "dependencies": dependencies,
                            "stderr": "",
                        }
                    )
                ),
                "dependencies": dependencies,
            }
        )
    if observed_required != set(required):
        missing = sorted(set(required) - observed_required)
        unexpected = sorted(observed_required - set(required))
        raise LlamaCppBundleError(
            "native runtime host-driver dependency set differs from the declared contract: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return (
        {
            "algorithm": "ldd-contract-v2-host-driver",
            "required_host_driver_sonames": list(required),
            "entries": entries,
        },
        unresolved_host_drivers,
    )


def _materialize_ldd_closure(
    bundle_root: Path,
    native_runtime: dict[str, object],
    runner: LocalProbeRunner,
    *,
    required_host_driver_sonames: tuple[str, ...] | list[str] = (),
    allow_unresolved_host_drivers: bool = False,
) -> dict[str, object]:
    closure, _ = _probe_ldd_closure(
        bundle_root,
        native_runtime,
        runner,
        required_host_driver_sonames=required_host_driver_sonames,
        allow_unresolved_host_drivers=allow_unresolved_host_drivers,
    )
    return closure


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))


def _load_lock(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise LlamaCppBundleError(f"llama.cpp bundle lock is not a regular file: {path}")
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LlamaCppBundleError(f"llama.cpp bundle lock is invalid: {error}") from error
    if not isinstance(value, dict):
        raise LlamaCppBundleError("llama.cpp bundle lock must contain a JSON object")
    errors = sorted(
        _validator().iter_errors(value),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise LlamaCppBundleError(f"llama.cpp bundle lock schema failed at {location}: {errors[0].message}")
    return value, payload


def _resolve_locked_file(root: Path, record: dict[str, Any], label: str) -> Path:
    relative = str(record["path"])
    candidate = root / relative
    cursor = root
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise LlamaCppBundleError(f"{label} must not traverse a symlink")
    path = candidate.resolve()
    try:
        common = Path(os.path.commonpath([root, path]))
    except ValueError as error:
        raise LlamaCppBundleError(f"{label} escapes the bundle root") from error
    if common != root:
        raise LlamaCppBundleError(f"{label} escapes the bundle root")
    if not path.is_file():
        raise LlamaCppBundleError(f"{label} is not a regular locked file: {path}")
    actual_size = path.stat().st_size
    if actual_size != record["bytes"]:
        raise LlamaCppBundleError(f"{label} byte size mismatch: expected {record['bytes']}, actual {actual_size}")
    actual_hash = _sha256_file(path)
    if actual_hash != record["sha256"]:
        raise LlamaCppBundleError(f"{label} hash mismatch: expected {record['sha256']}, actual {actual_hash}")
    return path


def _checked_probe(
    runner: LocalProbeRunner,
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_seconds: int = 30,
) -> LocalProbeResult:
    result = runner.run(command, cwd=cwd, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        detail = detail.strip().replace("\n", " ")[:1000]
        raise LlamaCppBundleError(f"{label} failed with exit code {result.returncode}: {detail or 'no diagnostics'}")
    return result


def _tool_record(root: Path, relative: str, label: str) -> dict[str, object]:
    if not relative or "\\" in relative or Path(relative).is_absolute():
        raise LlamaCppBundleError(f"{label} path must be a non-empty POSIX relative path")
    path = _resolve_locked_file(
        root,
        {
            "path": relative,
            "bytes": (root / relative).stat().st_size if (root / relative).is_file() else -1,
            "sha256": _sha256_file(root / relative) if (root / relative).is_file() else "sha256:" + "0" * 64,
        },
        label,
    )
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _version_probe_hash(
    runner: LocalProbeRunner,
    executable: Path,
    *,
    cwd: Path,
    expected_version: str,
    label: str,
) -> str:
    result = runner.run(
        [str(executable), "--version"],
        cwd=cwd,
        timeout_seconds=30,
    )
    try:
        stdout = result.stdout.decode("utf-8", errors="strict").replace("\r\n", "\n")
        stderr = result.stderr.decode("utf-8", errors="strict").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise LlamaCppBundleError(f"{label} version probe output is not UTF-8") from error
    payload = stdout.replace("\r", "\n").encode("utf-8")
    payload += b"\0"
    payload += stderr.replace("\r", "\n").encode("utf-8")
    rendered = payload.decode("utf-8", errors="replace")
    if result.returncode == 0:
        if expected_version not in rendered:
            raise LlamaCppBundleError(f"{label} version output does not contain expected {expected_version!r}")
        return _sha256(payload)

    # The exact reviewed b10156 llama-quantize program has no version option:
    # its upstream parser calls usage() before parsing a single argument and
    # deliberately exits 1.  Accept only that one pinned, deterministic usage
    # contract.  Source commit, OCI provenance, and executable bytes are bound
    # separately in the same build identity, so this remains fail-closed while
    # still proving the locked executable can load and execute.
    quantize_usage_markers = (
        "usage: ",
        "[--help] [--allow-requantize]",
        "model-f32.gguf [model-quant.gguf] type [nthreads]",
        "--dry-run",
        "allowed quantization types",
        "Q4_K_M",
        "Q8_0",
    )
    accepts_pinned_quantize_usage = (
        label == "llama-quantize"
        and executable.name == "llama-quantize"
        and expected_version == "b10156"
        and result.returncode == 1
        and not stderr.strip()
        and 0 < len(result.stdout) <= 64 * 1024
        and b"\0" not in result.stdout
        and stdout.startswith("usage: ")
        and all(marker in stdout for marker in quantize_usage_markers)
    )
    if not accepts_pinned_quantize_usage:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        detail = detail.strip().replace("\n", " ")[:1000]
        raise LlamaCppBundleError(
            f"{label} version probe failed with exit code {result.returncode}: "
            f"{detail or 'no diagnostics'}"
        )
    return _sha256(payload)


def materialize_llama_cpp_bundle_lock(
    bundle_root: str | Path,
    output_path: str | Path,
    *,
    expected_commit: str,
    llama_cpp_version: str,
    compiler_id: str,
    compiler_version: str,
    cuda_enabled: bool,
    cuda_version: str | None,
    generator: str,
    configuration: str,
    flags: list[str],
    oci_repository: str,
    oci_tag: str,
    oci_index_digest: str,
    oci_manifest_digest: str,
    oci_config_digest: str,
    oci_revision: str,
    oci_layers: list[dict[str, object]],
    runtime_image: str,
    native_runtime_root: str,
    convert_hf_to_gguf_path: str = "source/convert_hf_to_gguf.py",
    llama_quantize_path: str = "runtime/app/llama-quantize",
    llama_cli_path: str = "runtime/app/llama-cli",
    llama_server_path: str = "runtime/app/llama-server",
    required_host_driver_sonames: tuple[str, ...] = (),
    runner: LocalProbeRunner | None = None,
) -> VerifiedLlamaCppBundle:
    """Create a lock from a clean local checkout and actual local tool bytes.

    Source, tool, native-runtime, and version-output hashes are derived locally.
    The caller supplies the previously verified OCI identities whose selected
    layers produced this bundle; those immutable identities become part of the
    combined build hash.
    """

    if len(expected_commit) != 40 or any(character not in "0123456789abcdef" for character in expected_commit):
        raise LlamaCppBundleError("expected_commit must be 40 lowercase hexadecimal characters")
    if not llama_cpp_version.strip():
        raise LlamaCppBundleError("llama_cpp_version must be non-empty")
    if not compiler_id.strip() or not compiler_version.strip():
        raise LlamaCppBundleError("compiler identity must be complete")
    if not generator.strip() or not configuration.strip():
        raise LlamaCppBundleError("build generator and configuration must be complete")
    if not flags or any(not flag.strip() for flag in flags) or len(set(flags)) != len(flags):
        raise LlamaCppBundleError("build flags must be non-empty and unique")
    if cuda_enabled is not True or not cuda_version:
        raise LlamaCppBundleError("OCI full-cuda12 bundles require an exact CUDA-enabled build")
    if oci_repository != "ghcr.io/ggml-org/llama.cpp":
        raise LlamaCppBundleError("OCI repository must be the official llama.cpp package")
    if _OCI_TAG.fullmatch(oci_tag) is None:
        raise LlamaCppBundleError("OCI tag must be an exact full-cuda12 build tag")
    for label, digest in (
        ("OCI index", oci_index_digest),
        ("OCI manifest", oci_manifest_digest),
        ("OCI config", oci_config_digest),
    ):
        if _SHA256.fullmatch(digest) is None:
            raise LlamaCppBundleError(f"{label} digest must be an exact SHA-256")
    if oci_revision != expected_commit:
        raise LlamaCppBundleError("OCI revision must equal the exact source commit")
    if _RUNTIME_IMAGE.fullmatch(runtime_image) is None:
        raise LlamaCppBundleError("runtime image must be pinned by an immutable PyTorch digest")
    if (
        not isinstance(oci_layers, list)
        or len(oci_layers) != len(_OCI_LAYER_PURPOSES)
        or {layer.get("purpose") for layer in oci_layers if isinstance(layer, dict)} != _OCI_LAYER_PURPOSES
    ):
        raise LlamaCppBundleError("OCI layers must bind native libraries and tools/conversion")
    normalized_layers: list[dict[str, object]] = []
    seen_layer_digests: set[str] = set()
    for layer in oci_layers:
        if not isinstance(layer, dict) or set(layer) != {
            "digest",
            "compressed_bytes",
            "media_type",
            "purpose",
        }:
            raise LlamaCppBundleError("OCI layer records must be closed")
        digest = layer["digest"]
        size = layer["compressed_bytes"]
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or digest in seen_layer_digests
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or layer["media_type"] != "application/vnd.oci.image.layer.v1.tar+gzip"
        ):
            raise LlamaCppBundleError("OCI layer identity is invalid")
        seen_layer_digests.add(digest)
        normalized_layers.append(dict(layer))

    root_candidate = Path(bundle_root).expanduser()
    if root_candidate.is_symlink():
        raise LlamaCppBundleError("llama.cpp bundle root must not be a symlink")
    root = root_candidate.resolve()
    output = Path(output_path).resolve()
    source = root / "source"
    if root.is_symlink() or not root.is_dir():
        raise LlamaCppBundleError(f"llama.cpp bundle root is not a regular directory: {root}")
    if source.is_symlink() or not source.is_dir() or not (source / ".git").is_dir():
        raise LlamaCppBundleError("materialization requires bundle_root/source to be a local git checkout")
    if output.exists():
        raise LlamaCppBundleError(f"refusing to overwrite llama.cpp bundle lock: {output}")

    runtime = runner or SubprocessLocalProbeRunner()
    head = (
        _checked_probe(
            runtime,
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=source,
            label="llama.cpp HEAD probe",
        )
        .stdout.decode("ascii", errors="strict")
        .strip()
    )
    if head != expected_commit:
        raise LlamaCppBundleError(f"llama.cpp checkout commit mismatch: expected {expected_commit}, actual {head}")
    status = _checked_probe(
        runtime,
        [
            "git",
            "-c",
            "core.fileMode=false",
            "-c",
            "core.autocrlf=true",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=source,
        label="llama.cpp clean-tree probe",
    )
    # Porcelain status reports working-tree changes on stdout. Git may emit
    # harmless environment warnings on stderr (for example for an inaccessible
    # global excludes file inside a container); `_checked_probe` already makes
    # every non-zero status fail closed.
    if status.stdout.strip():
        raise LlamaCppBundleError("llama.cpp checkout is not clean")
    origin = (
        _checked_probe(
            runtime,
            ["git", "remote", "get-url", "origin"],
            cwd=source,
            label="llama.cpp origin probe",
        )
        .stdout.decode("utf-8", errors="strict")
        .strip()
    )
    normalized_origin = origin.removesuffix(".git").rstrip("/")
    if normalized_origin != "https://github.com/ggml-org/llama.cpp":
        raise LlamaCppBundleError(f"unexpected llama.cpp origin: {origin!r}")

    tool_paths = {
        "convert_hf_to_gguf": convert_hf_to_gguf_path,
        "llama_quantize": llama_quantize_path,
        "llama_cli": llama_cli_path,
        "llama_server": llama_server_path,
    }
    tools = {name: _tool_record(root, relative, name) for name, relative in tool_paths.items()}
    native_runtime = _native_runtime_inventory(root, native_runtime_root)
    native_tool_paths = {name: root / relative for name, relative in tool_paths.items() if name != "convert_hf_to_gguf"}
    runtime_root = (root / native_runtime_root).resolve()
    for name, path in native_tool_paths.items():
        try:
            common = Path(os.path.commonpath([runtime_root, path.resolve()]))
        except ValueError as error:
            raise LlamaCppBundleError(f"{name} is outside the native runtime root") from error
        if common != runtime_root:
            raise LlamaCppBundleError(f"{name} is outside the native runtime root")
        _require_elf64_x86_64(path, name)
    required_host_drivers = _validated_required_host_driver_sonames(
        required_host_driver_sonames
    )
    ldd_closure = _materialize_ldd_closure(
        root,
        native_runtime,
        runtime,
        required_host_driver_sonames=required_host_drivers,
        allow_unresolved_host_drivers=bool(required_host_drivers),
    )
    native_runtime["ldd_closure"] = ldd_closure
    build: dict[str, object] = {
        "llama_cpp_version": llama_cpp_version,
        "llama_quantize_version_output_sha256": _version_probe_hash(
            runtime,
            root / llama_quantize_path,
            cwd=root,
            expected_version=llama_cpp_version,
            label="llama-quantize",
        ),
        "llama_cli_version_output_sha256": _version_probe_hash(
            runtime,
            root / llama_cli_path,
            cwd=root,
            expected_version=llama_cpp_version,
            label="llama-cli",
        ),
        "llama_server_version_output_sha256": _version_probe_hash(
            runtime,
            root / llama_server_path,
            cwd=root,
            expected_version=llama_cpp_version,
            label="llama-server",
        ),
        "compiler": {"id": compiler_id, "version": compiler_version},
        "cuda": {"enabled": cuda_enabled, "version": cuda_version},
        "generator": generator,
        "configuration": configuration,
        "flags": flags,
    }
    source_inventory = {
        "root": "source",
        **_inventory(source),
    }
    required_subtrees = {
        "conversion": {
            "root": "source/conversion",
            **_inventory(source, prefix="conversion"),
        },
        "gguf_py": {
            "root": "source/gguf-py",
            **_inventory(source, prefix="gguf-py"),
        },
    }
    final_head = (
        _checked_probe(
            runtime,
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=source,
            label="llama.cpp final HEAD probe",
        )
        .stdout.decode("ascii", errors="strict")
        .strip()
    )
    final_status = _checked_probe(
        runtime,
        [
            "git",
            "-c",
            "core.fileMode=false",
            "-c",
            "core.autocrlf=true",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=source,
        label="llama.cpp final clean-tree probe",
    )
    if (
        final_head != expected_commit
        or final_status.stdout.strip()
        or source_inventory != {"root": "source", **_inventory(source)}
        or required_subtrees["conversion"]
        != {
            "root": "source/conversion",
            **_inventory(source, prefix="conversion"),
        }
        or required_subtrees["gguf_py"]
        != {
            "root": "source/gguf-py",
            **_inventory(source, prefix="gguf-py"),
        }
    ):
        raise LlamaCppBundleError("llama.cpp source changed during lock materialization")
    distribution: dict[str, object] = {
        "kind": "oci_image_layers",
        "repository": oci_repository,
        "tag": oci_tag,
        "index_digest": oci_index_digest,
        "manifest_digest": oci_manifest_digest,
        "config_digest": oci_config_digest,
        "revision": oci_revision,
        "platform": {"os": "linux", "architecture": "amd64"},
        "layers": normalized_layers,
        "runtime_image": runtime_image,
    }
    lock: dict[str, object] = {
        "schema_version": 3,
        "status": "materialized",
        "upstream_repository": "https://github.com/ggml-org/llama.cpp",
        "commit": expected_commit,
        "distribution": distribution,
        "source": source_inventory,
        "required_subtrees": required_subtrees,
        "tools": tools,
        "build": build,
        "build_identity_sha256": _sha256(
            _canonical_json(
                {
                    "build": build,
                    "distribution": distribution,
                    "native_runtime": native_runtime,
                    "source": source_inventory,
                    "required_subtrees": required_subtrees,
                    "tools": tools,
                }
            )
        ),
        "native_runtime": native_runtime,
    }
    errors = sorted(
        _validator().iter_errors(lock),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise LlamaCppBundleError(f"materialized llama.cpp lock schema failed at {location}: {errors[0].message}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    payload = json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        os.replace(temporary, output)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise LlamaCppBundleError(f"could not publish llama.cpp bundle lock: {error}") from error
    return load_verified_llama_cpp_bundle(root, output)


def load_verified_llama_cpp_bundle(
    bundle_root: str | Path,
    lock_path: str | Path,
) -> VerifiedLlamaCppBundle:
    """Verify every locked source and executable byte before returning any tool path."""

    root_candidate = Path(bundle_root).expanduser()
    if root_candidate.is_symlink():
        raise LlamaCppBundleError("llama.cpp bundle root must not be a symlink")
    root = root_candidate.resolve()
    lock_candidate = Path(lock_path).expanduser()
    if lock_candidate.is_symlink():
        raise LlamaCppBundleError("llama.cpp bundle lock must not be a symlink")
    lock = lock_candidate.resolve()
    value, lock_payload = _load_lock(lock)
    if value.get("status") != "materialized":
        raise LlamaCppBundleError("llama.cpp bundle lock is UNMATERIALIZED; no GGUF toolchain is authorized")
    if root.is_symlink() or not root.is_dir():
        raise LlamaCppBundleError(f"llama.cpp bundle root is not a regular directory: {root}")
    source = root / "source"
    expected_source = dict(value["source"])
    expected_source.pop("root")
    if _inventory(source) != expected_source:
        raise LlamaCppBundleError("llama.cpp source inventory or full-tree hash mismatch")
    subtree_specs = {
        "conversion": "conversion",
        "gguf_py": "gguf-py",
    }
    for label, prefix in subtree_specs.items():
        expected = dict(value["required_subtrees"][label])
        expected.pop("root")
        if _inventory(source, prefix=prefix) != expected:
            raise LlamaCppBundleError(f"llama.cpp {label} source inventory mismatch")
    build = dict(value["build"])
    distribution = dict(value["distribution"])
    native_runtime = dict(value["native_runtime"])
    layers = distribution.get("layers")
    if (
        not isinstance(layers, list)
        or {layer.get("purpose") for layer in layers if isinstance(layer, dict)} != _OCI_LAYER_PURPOSES
        or len({layer.get("digest") for layer in layers if isinstance(layer, dict)}) != len(_OCI_LAYER_PURPOSES)
    ):
        raise LlamaCppBundleError("llama.cpp OCI layers have duplicate roles or digests")
    if distribution.get("revision") != value.get("commit"):
        raise LlamaCppBundleError("llama.cpp OCI revision differs from the source commit")
    expected_native = dict(native_runtime)
    expected_ldd = expected_native.pop("ldd_closure", None)
    runtime_root = str(expected_native.pop("root", ""))
    actual_native = _native_runtime_inventory(root, runtime_root)
    actual_native.pop("root")
    if actual_native != expected_native:
        raise LlamaCppBundleError("llama.cpp native runtime inventory mismatch")
    if not isinstance(expected_ldd, dict):
        raise LlamaCppBundleError("llama.cpp ldd closure is missing")
    native_runtime["ldd_closure"] = expected_ldd
    expected_build_hash = _sha256(
        _canonical_json(
            {
                "build": build,
                "distribution": distribution,
                "native_runtime": native_runtime,
                "source": value["source"],
                "required_subtrees": value["required_subtrees"],
                "tools": value["tools"],
            }
        )
    )
    if expected_build_hash != value["build_identity_sha256"]:
        raise LlamaCppBundleError("llama.cpp build identity hash does not match")
    tools = value["tools"]
    convert = _resolve_locked_file(root, tools["convert_hf_to_gguf"], "convert_hf_to_gguf")
    quantize = _resolve_locked_file(root, tools["llama_quantize"], "llama_quantize")
    cli = _resolve_locked_file(root, tools["llama_cli"], "llama_cli")
    server = _resolve_locked_file(root, tools["llama_server"], "llama_server")
    _require_elf64_x86_64(quantize, "llama_quantize")
    _require_elf64_x86_64(cli, "llama_cli")
    _require_elf64_x86_64(server, "llama_server")
    source_file = next(
        (record for record in expected_source["files"] if record["path"] == "convert_hf_to_gguf.py"),
        None,
    )
    if source_file is None or source_file["sha256"] != tools["convert_hf_to_gguf"]["sha256"]:
        raise LlamaCppBundleError("convert_hf_to_gguf tool is not the converter from the locked source tree")
    return VerifiedLlamaCppBundle(
        root=root,
        lock_path=lock,
        lock_sha256=_sha256(lock_payload),
        commit=str(value["commit"]),
        source_tree_sha256=str(expected_source["tree_sha256"]),
        build_identity_sha256=str(value["build_identity_sha256"]),
        build=build,
        distribution=distribution,
        native_runtime=native_runtime,
        runtime_library_paths=tuple(
            (root / relative).resolve() for relative in native_runtime.get("library_directories", [])
        ),
        convert_hf_to_gguf=convert,
        llama_quantize=quantize,
        llama_cli=cli,
        llama_server=server,
    )


def verify_llama_cpp_runtime_closure(
    bundle: VerifiedLlamaCppBundle,
    *,
    runner: LocalProbeRunner | None = None,
    allow_unresolved_host_drivers: bool = False,
) -> dict[str, object]:
    """Re-probe every locked ELF object and require the exact ldd closure."""

    if bundle.native_runtime.get("ldd_closure") is None or not bundle.runtime_library_paths:
        raise LlamaCppBundleError("llama.cpp bundle has no locked native runtime closure")
    actual_inventory = _native_runtime_inventory(
        bundle.root,
        str(bundle.native_runtime.get("root", "")),
    )
    expected_inventory = dict(bundle.native_runtime)
    expected_inventory.pop("ldd_closure", None)
    if actual_inventory != expected_inventory:
        raise LlamaCppBundleError("llama.cpp native runtime inventory differs during ldd preflight")
    expected_closure = bundle.native_runtime["ldd_closure"]
    if not isinstance(expected_closure, dict):
        raise LlamaCppBundleError("llama.cpp runtime ldd closure is invalid")
    required_host_drivers = expected_closure.get(
        "required_host_driver_sonames"
    )
    if not isinstance(required_host_drivers, list):
        raise LlamaCppBundleError(
            "llama.cpp runtime ldd closure has no host-driver contract"
        )
    actual_closure, unresolved_host_drivers = _probe_ldd_closure(
        bundle.root,
        actual_inventory,
        runner or SubprocessLocalProbeRunner(),
        required_host_driver_sonames=required_host_drivers,
        allow_unresolved_host_drivers=allow_unresolved_host_drivers,
    )
    if actual_closure != expected_closure:
        raise LlamaCppBundleError("llama.cpp runtime ldd closure differs from the lock")
    return {
        "status": "verified",
        "algorithm": "ldd-contract-v2-host-driver",
        "elf_object_count": len(actual_closure["entries"]),
        "ldd_closure_sha256": _sha256(_canonical_json(actual_closure)),
        "library_directories": [str(path) for path in bundle.runtime_library_paths],
        "required_host_driver_sonames": list(required_host_drivers),
        "host_driver_resolution": (
            "symbolic"
            if unresolved_host_drivers
            else "live"
            if required_host_drivers
            else "not_required"
        ),
    }
