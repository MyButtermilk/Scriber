"""Hash-locked materialization of the native Windows CUDA llama.cpp benchmark runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from jsonschema import Draft202012Validator

_PROJECT_ROOT = Path(__file__).parents[2]
_DISTRIBUTION_SCHEMA = (
    _PROJECT_ROOT / "contracts" / "windows_llama_cpp_distribution_lock_schema.json"
)
_MANIFEST_FILENAME = "windows-llama-cpp-runtime-manifest.json"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^b([0-9]+)$")


class WindowsLlamaCppRuntimeError(RuntimeError):
    """The Windows llama.cpp distribution or materialized runtime is untrusted."""


@dataclass(frozen=True, slots=True)
class VersionProbeResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class VersionProbeRunner(Protocol):
    def run(
        self,
        executable: Path,
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> VersionProbeResult: ...


class SubprocessVersionProbeRunner:
    """Probe one absolute Windows executable without a shell or network."""

    def run(
        self,
        executable: Path,
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> VersionProbeResult:
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                shell=False,
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WindowsLlamaCppRuntimeError(
                f"native Windows llama-server version probe failed: {error}"
            ) from error
        return VersionProbeResult(
            returncode=completed.returncode,
            stdout=completed.stdout[-128 * 1024 :],
            stderr=completed.stderr[-128 * 1024 :],
        )


@dataclass(frozen=True, slots=True)
class VerifiedWindowsLlamaCppRuntime:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    distribution_lock_sha256: str
    runtime_tree_sha256: str
    release_tag: str
    commit: str
    conversion_tag: str
    conversion_commit: str
    llama_cli: Path
    llama_cli_sha256: str
    llama_quantize: Path
    llama_quantize_sha256: str
    llama_server: Path
    llama_server_sha256: str
    runtime_path_entries: tuple[Path, ...]


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise WindowsLlamaCppRuntimeError(
            f"runtime input is not a regular file: {path}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


@cache
def _distribution_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(_DISTRIBUTION_SCHEMA.read_text(encoding="utf-8"))
    )


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise WindowsLlamaCppRuntimeError(
            f"{label} is missing or not a regular file: {path}"
        )
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WindowsLlamaCppRuntimeError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise WindowsLlamaCppRuntimeError(f"{label} must contain an object")
    return value, payload


def load_windows_llama_cpp_distribution_lock(
    path: str | Path,
) -> tuple[dict[str, Any], str]:
    """Load and validate the two-archive upstream distribution lock."""

    value, payload = _load_json_object(
        Path(path).expanduser().resolve(),
        "Windows llama.cpp distribution lock",
    )
    errors = sorted(
        _distribution_validator().iter_errors(value),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise WindowsLlamaCppRuntimeError(
            "Windows llama.cpp distribution lock schema failed at "
            f"{location}: {errors[0].message}"
        )
    assets = value["assets"]
    if (
        {asset["role"] for asset in assets} != {"runtime", "cuda_runtime"}
        or len({asset["filename"].lower() for asset in assets}) != 2
        or len({asset["sha256"] for asset in assets}) != 2
    ):
        raise WindowsLlamaCppRuntimeError(
            "Windows llama.cpp distribution assets have duplicate roles or identities"
        )
    tag = str(value["release"]["tag"])
    if any(
        not str(asset["url"]).endswith(f"/{asset['filename']}")
        or f"/download/{tag}/" not in str(asset["url"])
        for asset in assets
    ):
        raise WindowsLlamaCppRuntimeError(
            "Windows llama.cpp asset URL differs from its release binding"
        )
    runtime_build = int(_TAG.fullmatch(tag).group(1))  # type: ignore[union-attr]
    conversion_tag = str(value["conversion_compatibility"]["tag"])
    conversion_match = _TAG.fullmatch(conversion_tag)
    if conversion_match is None or runtime_build <= int(conversion_match.group(1)):
        raise WindowsLlamaCppRuntimeError(
            "Windows runtime must be a newer same-upstream build than the GGUF converter"
        )
    return value, _sha256_bytes(payload)


def _verify_archive(path: Path, record: Mapping[str, object], label: str) -> None:
    if path.name != record["filename"]:
        raise WindowsLlamaCppRuntimeError(
            f"{label} filename differs from the distribution lock"
        )
    actual_size = path.stat().st_size if path.is_file() and not path.is_symlink() else -1
    if actual_size != record["bytes"] or _sha256_file(path) != record["sha256"]:
        raise WindowsLlamaCppRuntimeError(
            f"{label} bytes or SHA-256 differ from the distribution lock"
        )


def _safe_zip_relative(name: str) -> PurePosixPath:
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise WindowsLlamaCppRuntimeError(
            f"archive contains an unsafe path: {name!r}"
        )
    relative = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise WindowsLlamaCppRuntimeError(
            f"archive contains an unsafe path: {name!r}"
        )
    return relative


def _extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    maximum_total_bytes: int,
    extracted_total: int,
    seen_casefold: dict[str, Path],
) -> int:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise WindowsLlamaCppRuntimeError(
            f"Windows runtime archive is invalid: {archive_path.name}: {error}"
        ) from error
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise WindowsLlamaCppRuntimeError(
                    f"archive entry is encrypted: {info.filename}"
                )
            if info.create_system == 3:
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode & 0o170000 == 0o120000:
                    raise WindowsLlamaCppRuntimeError(
                        f"archive contains a symlink: {info.filename}"
                    )
            relative = _safe_zip_relative(info.filename)
            extracted_total += int(info.file_size)
            if extracted_total > maximum_total_bytes:
                raise WindowsLlamaCppRuntimeError(
                    "Windows runtime archives exceed the bounded extracted byte limit"
                )
            key = relative.as_posix().casefold()
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + f".extract-{os.getpid()}.tmp")
            digest = hashlib.sha256()
            written = 0
            try:
                with archive.open(info, "r") as source, temporary.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)
                        digest.update(block)
                        written += len(block)
                if written != info.file_size:
                    raise WindowsLlamaCppRuntimeError(
                        f"archive entry size changed during extraction: {info.filename}"
                    )
                previous = seen_casefold.get(key)
                if previous is None:
                    os.replace(temporary, target)
                    seen_casefold[key] = target
                elif (
                    previous.stat().st_size != written
                    or _sha256_file(previous)
                    != "sha256:" + digest.hexdigest()
                ):
                    raise WindowsLlamaCppRuntimeError(
                        f"runtime archives contain a conflicting path: {info.filename}"
                    )
            finally:
                temporary.unlink(missing_ok=True)
    return extracted_total


def _require_pe_x86_64(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size < 0x88:
        raise WindowsLlamaCppRuntimeError(
            f"{label} is not a non-empty Windows executable"
        )
    with path.open("rb") as stream:
        dos = stream.read(0x40)
        if len(dos) != 0x40 or dos[:2] != b"MZ":
            raise WindowsLlamaCppRuntimeError(f"{label} has no valid DOS header")
        pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
        if pe_offset < 0x40 or pe_offset > min(path.stat().st_size - 6, 16 * 1024 * 1024):
            raise WindowsLlamaCppRuntimeError(f"{label} has an unsafe PE offset")
        stream.seek(pe_offset)
        header = stream.read(6)
    if header[:4] != b"PE\0\0" or int.from_bytes(header[4:6], "little") != 0x8664:
        raise WindowsLlamaCppRuntimeError(f"{label} is not a PE x86-64 object")


def _inventory(root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == _MANIFEST_FILENAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise WindowsLlamaCppRuntimeError(
                f"runtime inventory contains a non-regular entry: {relative}"
            )
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if not files:
        raise WindowsLlamaCppRuntimeError("Windows runtime inventory is empty")
    digest = hashlib.sha256()
    for record in files:
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256-path-size-content-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": sum(int(record["bytes"]) for record in files),
        "files": files,
    }


def _required_file_map(
    root: Path,
    required_names: list[str],
) -> dict[str, Path]:
    by_name: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            by_name.setdefault(path.name.casefold(), []).append(path)
    resolved: dict[str, Path] = {}
    for name in required_names:
        matches = by_name.get(name.casefold(), [])
        if len(matches) != 1:
            raise WindowsLlamaCppRuntimeError(
                f"Windows runtime must contain exactly one required {name}"
            )
        resolved[name] = matches[0]
        _require_pe_x86_64(matches[0], name)
    parents = {path.parent.resolve() for path in resolved.values()}
    if len(parents) != 1:
        raise WindowsLlamaCppRuntimeError(
            "llama-server and every required CUDA DLL must share one runtime directory"
        )
    return resolved


def _probe_environment(runtime_directory: Path) -> dict[str, str]:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    return {
        **os.environ,
        "PATH": os.pathsep.join(
            (
                str(runtime_directory),
                str(system_root / "System32"),
                str(system_root),
            )
        ),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NO_PROXY": "*",
    }


def _version_probe(
    runner: VersionProbeRunner,
    server: Path,
    *,
    release_tag: str,
    commit: str,
) -> dict[str, object]:
    result = runner.run(
        server,
        cwd=server.parent,
        environment=_probe_environment(server.parent),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        )
        raise WindowsLlamaCppRuntimeError(
            "native Windows llama-server version probe failed with exit code "
            f"{result.returncode}: {detail.strip()[:1000] or 'no diagnostics'}"
        )
    stdout = result.stdout.decode("utf-8", errors="strict").replace("\r\n", "\n")
    stderr = result.stderr.decode("utf-8", errors="strict").replace("\r\n", "\n")
    combined = f"{stdout}\n{stderr}"
    tag_match = _TAG.fullmatch(release_tag)
    assert tag_match is not None
    if (
        re.search(rf"\b{re.escape(tag_match.group(1))}\b", combined) is None
        or commit[:8] not in combined
    ):
        raise WindowsLlamaCppRuntimeError(
            "native Windows llama-server version output differs from its release"
        )
    payload = stdout.encode("utf-8") + b"\0" + stderr.encode("utf-8")
    return {
        "returncode": 0,
        "stdout_stderr_sha256": _sha256_bytes(payload),
        "release_build_observed": True,
        "commit_prefix_observed": commit[:8],
    }


def _validate_materialized_manifest(value: Mapping[str, object]) -> dict[str, Any]:
    manifest = dict(value)
    expected = {
        "schema_version",
        "kind",
        "status",
        "distribution_lock_sha256",
        "distribution",
        "runtime",
        "version_probe",
        "network_access",
    }
    if set(manifest) != expected:
        raise WindowsLlamaCppRuntimeError(
            "materialized Windows runtime manifest is not closed"
        )
    distribution = manifest.get("distribution")
    runtime = manifest.get("runtime")
    probe = manifest.get("version_probe")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind")
        != "scriber_materialized_windows_llama_cpp_cuda_runtime"
        or manifest.get("status") != "verified"
        or manifest.get("network_access") != "disabled_offline"
        or not isinstance(distribution, Mapping)
        or not isinstance(runtime, Mapping)
        or not isinstance(probe, Mapping)
        or _HASH.fullmatch(str(manifest.get("distribution_lock_sha256"))) is None
        or runtime.get("platform") != "windows_x86_64_cuda"
        or runtime.get("required_files_present") is not True
        or runtime.get("pe_x86_64_verified") is not True
        or probe.get("returncode") != 0
        or probe.get("release_build_observed") is not True
    ):
        raise WindowsLlamaCppRuntimeError(
            "materialized Windows runtime manifest identity is invalid"
        )
    return manifest


def materialize_windows_llama_cpp_runtime(
    *,
    distribution_lock_path: str | Path,
    runtime_archive_path: str | Path,
    cuda_archive_path: str | Path,
    output_dir: str | Path,
    runner: VersionProbeRunner | None = None,
) -> VerifiedWindowsLlamaCppRuntime:
    """Verify two downloaded assets, extract safely, probe, and publish atomically."""

    distribution, lock_sha256 = load_windows_llama_cpp_distribution_lock(
        distribution_lock_path
    )
    assets = {
        str(record["role"]): record for record in distribution["assets"]
    }
    runtime_archive = Path(runtime_archive_path).expanduser().resolve()
    cuda_archive = Path(cuda_archive_path).expanduser().resolve()
    _verify_archive(runtime_archive, assets["runtime"], "llama.cpp runtime archive")
    _verify_archive(
        cuda_archive,
        assets["cuda_runtime"],
        "llama.cpp CUDA runtime archive",
    )
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise WindowsLlamaCppRuntimeError(
            f"refusing to overwrite Windows llama.cpp runtime: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    ).resolve()
    try:
        seen: dict[str, Path] = {}
        extracted = 0
        for archive in (runtime_archive, cuda_archive):
            extracted = _extract_archive(
                archive,
                temporary,
                maximum_total_bytes=int(distribution["maximum_extracted_bytes"]),
                extracted_total=extracted,
                seen_casefold=seen,
            )
        required_names = [str(name) for name in distribution["required_files"]]
        required = _required_file_map(temporary, required_names)
        cli = required.get("llama-cli.exe")
        quantize = required.get("llama-quantize.exe")
        if cli is None or quantize is None:
            raise WindowsLlamaCppRuntimeError(
                "Windows runtime lock must require llama-cli.exe and llama-quantize.exe"
            )
        server = required["llama-server.exe"]
        release = distribution["release"]
        probe = _version_probe(
            runner or SubprocessVersionProbeRunner(),
            server,
            release_tag=str(release["tag"]),
            commit=str(release["commit"]),
        )
        inventory = _inventory(temporary)
        manifest = _validate_materialized_manifest(
            {
                "schema_version": 1,
                "kind": "scriber_materialized_windows_llama_cpp_cuda_runtime",
                "status": "verified",
                "distribution_lock_sha256": lock_sha256,
                "distribution": distribution,
                "runtime": {
                    "platform": "windows_x86_64_cuda",
                    "inventory": inventory,
                    "required_files_present": True,
                    "pe_x86_64_verified": True,
                    "runtime_directory": server.parent.relative_to(
                        temporary
                    ).as_posix()
                    or ".",
                    "llama_cli_path": cli.relative_to(temporary).as_posix(),
                    "llama_cli_sha256": _sha256_file(cli),
                    "llama_quantize_path": quantize.relative_to(temporary).as_posix(),
                    "llama_quantize_sha256": _sha256_file(quantize),
                    "llama_server_path": server.relative_to(temporary).as_posix(),
                    "llama_server_sha256": _sha256_file(server),
                },
                "version_probe": probe,
                "network_access": "disabled_offline",
            }
        )
        manifest_path = temporary / _MANIFEST_FILENAME
        manifest_path.write_bytes(_canonical_bytes(manifest))
        load_verified_windows_llama_cpp_runtime(
            temporary,
            manifest_path,
            runner=runner,
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return load_verified_windows_llama_cpp_runtime(
        target,
        target / _MANIFEST_FILENAME,
        runner=runner,
    )


def load_verified_windows_llama_cpp_runtime(
    runtime_root: str | Path,
    manifest_path: str | Path,
    *,
    runner: VersionProbeRunner | None = None,
) -> VerifiedWindowsLlamaCppRuntime:
    """Recompute every local byte and re-probe the pinned Windows runtime."""

    root_candidate = Path(runtime_root).expanduser()
    manifest_candidate = Path(manifest_path).expanduser()
    if root_candidate.is_symlink() or manifest_candidate.is_symlink():
        raise WindowsLlamaCppRuntimeError(
            "Windows runtime root and manifest must not be symlinks"
        )
    root = root_candidate.resolve()
    path = manifest_candidate.resolve()
    if not root.is_dir():
        raise WindowsLlamaCppRuntimeError(
            f"Windows runtime root is missing: {root}"
        )
    manifest, payload = _load_json_object(path, "materialized Windows runtime manifest")
    manifest = _validate_materialized_manifest(manifest)
    if payload != _canonical_bytes(manifest):
        raise WindowsLlamaCppRuntimeError(
            "materialized Windows runtime manifest is not canonical JSON"
        )
    distribution = manifest["distribution"]
    schema_errors = sorted(
        _distribution_validator().iter_errors(distribution),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if schema_errors:
        raise WindowsLlamaCppRuntimeError(
            "embedded Windows distribution lock is invalid"
        )
    runtime = manifest["runtime"]
    if _inventory(root) != runtime["inventory"]:
        raise WindowsLlamaCppRuntimeError(
            "Windows llama.cpp runtime inventory differs from its manifest"
        )
    required = _required_file_map(
        root,
        [str(name) for name in distribution["required_files"]],
    )
    cli = required.get("llama-cli.exe")
    quantize = required.get("llama-quantize.exe")
    if cli is None or quantize is None:
        raise WindowsLlamaCppRuntimeError(
            "Windows runtime lock must require llama-cli.exe and llama-quantize.exe"
        )
    server = required["llama-server.exe"]
    if (
        cli.relative_to(root).as_posix() != runtime.get("llama_cli_path")
        or _sha256_file(cli) != runtime.get("llama_cli_sha256")
        or quantize.relative_to(root).as_posix()
        != runtime.get("llama_quantize_path")
        or _sha256_file(quantize) != runtime.get("llama_quantize_sha256")
        or
        server.relative_to(root).as_posix() != runtime["llama_server_path"]
        or _sha256_file(server) != runtime["llama_server_sha256"]
    ):
        raise WindowsLlamaCppRuntimeError(
            "Windows llama-server path or hash differs from its manifest"
        )
    release = distribution["release"]
    actual_probe = _version_probe(
        runner or SubprocessVersionProbeRunner(),
        server,
        release_tag=str(release["tag"]),
        commit=str(release["commit"]),
    )
    if actual_probe != manifest["version_probe"]:
        raise WindowsLlamaCppRuntimeError(
            "Windows llama-server version probe differs from its manifest"
        )
    runtime_directory = (root / str(runtime["runtime_directory"])).resolve()
    if runtime_directory != server.parent.resolve():
        raise WindowsLlamaCppRuntimeError(
            "Windows runtime directory differs from the server and DLL location"
        )
    return VerifiedWindowsLlamaCppRuntime(
        root=root,
        manifest_path=path,
        manifest_sha256=_sha256_bytes(payload),
        distribution_lock_sha256=str(manifest["distribution_lock_sha256"]),
        runtime_tree_sha256=str(runtime["inventory"]["tree_sha256"]),
        release_tag=str(release["tag"]),
        commit=str(release["commit"]),
        conversion_tag=str(distribution["conversion_compatibility"]["tag"]),
        conversion_commit=str(
            distribution["conversion_compatibility"]["commit"]
        ),
        llama_cli=cli,
        llama_cli_sha256=str(runtime["llama_cli_sha256"]),
        llama_quantize=quantize,
        llama_quantize_sha256=str(runtime["llama_quantize_sha256"]),
        llama_server=server,
        llama_server_sha256=str(runtime["llama_server_sha256"]),
        runtime_path_entries=(runtime_directory,),
    )
