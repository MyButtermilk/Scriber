from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scriber_polishing.windows_llama_cpp_runtime import (
    VersionProbeResult,
    WindowsLlamaCppRuntimeError,
    load_verified_windows_llama_cpp_runtime,
    materialize_windows_llama_cpp_runtime,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _pe_x64(label: bytes) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\0\0"
    payload[0x84:0x86] = (0x8664).to_bytes(2, "little")
    payload[0x100 : 0x100 + len(label)] = label
    return bytes(payload)


def _zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def _lock(path: Path, runtime_zip: Path, cuda_zip: Path) -> Path:
    value = {
        "schema_version": 1,
        "kind": "scriber_windows_llama_cpp_cuda_distribution",
        "status": "locked",
        "upstream_repository": "https://github.com/ggml-org/llama.cpp",
        "release": {
            "tag": "b10158",
            "commit": "f87067841bac583bc089a225382248d857791ca8",
            "published_at_utc": "2026-07-28T08:54:30Z",
        },
        "conversion_compatibility": {
            "tag": "b10156",
            "commit": "91f8c9c5fb038c086e13e9cd823c29b33b07ba54",
            "policy": "newer_same_upstream_runtime_with_fresh_gguf_reload",
        },
        "platform": {
            "os": "windows",
            "architecture": "amd64",
            "backend": "cuda",
            "cuda_major_minor": "12.4",
        },
        "assets": [
            {
                "role": "runtime",
                "filename": runtime_zip.name,
                "bytes": runtime_zip.stat().st_size,
                "sha256": _sha256(runtime_zip.read_bytes()),
                "url": (
                    "https://github.com/ggml-org/llama.cpp/releases/download/"
                    f"b10158/{runtime_zip.name}"
                ),
            },
            {
                "role": "cuda_runtime",
                "filename": cuda_zip.name,
                "bytes": cuda_zip.stat().st_size,
                "sha256": _sha256(cuda_zip.read_bytes()),
                "url": (
                    "https://github.com/ggml-org/llama.cpp/releases/download/"
                    f"b10158/{cuda_zip.name}"
                ),
            },
        ],
        "required_files": [
            "cublas64_12.dll",
            "cublasLt64_12.dll",
            "cudart64_12.dll",
            "ggml-base.dll",
            "ggml-cpu.dll",
            "ggml-cuda.dll",
            "ggml.dll",
            "llama-cli.exe",
            "llama-quantize.exe",
            "llama-server.exe",
            "llama.dll",
        ],
        "maximum_extracted_bytes": 10_000_000,
    }
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


class Probe:
    def run(
        self,
        executable: Path,
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> VersionProbeResult:
        assert executable.name == "llama-server.exe"
        assert cwd == executable.parent
        assert environment["HF_HUB_OFFLINE"] == "1"
        assert timeout_seconds == 30
        return VersionProbeResult(
            returncode=0,
            stdout=b"version: 10158 (f8706784)\n",
            stderr=b"",
        )


def _archives(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "llama-b10158-bin-win-cuda-12.4-x64.zip"
    cuda = tmp_path / "cudart-llama-bin-win-cuda-12.4-x64.zip"
    _zip(
        runtime,
        {
            name: _pe_x64(name.encode())
            for name in (
                "llama-server.exe",
                "llama-cli.exe",
                "llama-quantize.exe",
                "ggml-base.dll",
                "ggml-cpu.dll",
                "ggml-cuda.dll",
                "ggml.dll",
                "llama.dll",
            )
        },
    )
    _zip(
        cuda,
        {
            name: _pe_x64(name.encode())
            for name in (
                "cublas64_12.dll",
                "cublasLt64_12.dll",
                "cudart64_12.dll",
            )
        },
    )
    return runtime, cuda


def test_materializes_and_reverifies_one_hash_bound_native_windows_cuda_runtime(
    tmp_path: Path,
) -> None:
    runtime_zip, cuda_zip = _archives(tmp_path)
    lock = _lock(tmp_path / "distribution-lock.json", runtime_zip, cuda_zip)
    output = tmp_path / "runtime"

    verified = materialize_windows_llama_cpp_runtime(
        distribution_lock_path=lock,
        runtime_archive_path=runtime_zip,
        cuda_archive_path=cuda_zip,
        output_dir=output,
        runner=Probe(),
    )

    assert verified.release_tag == "b10158"
    assert verified.commit == "f87067841bac583bc089a225382248d857791ca8"
    assert verified.llama_server == output / "llama-server.exe"
    assert verified.llama_cli == output / "llama-cli.exe"
    assert verified.llama_quantize == output / "llama-quantize.exe"
    assert verified.runtime_tree_sha256.startswith("sha256:")
    assert verified.manifest_sha256.startswith("sha256:")
    assert set(verified.runtime_path_entries) == {output}
    reloaded = load_verified_windows_llama_cpp_runtime(
        output,
        output / "windows-llama-cpp-runtime-manifest.json",
        runner=Probe(),
    )
    assert reloaded == verified

    (output / "ggml-cuda.dll").write_bytes(b"tampered")
    with pytest.raises(WindowsLlamaCppRuntimeError, match="inventory"):
        load_verified_windows_llama_cpp_runtime(
            output,
            output / "windows-llama-cpp-runtime-manifest.json",
            runner=Probe(),
        )


def test_rejects_traversal_before_publishing_runtime(tmp_path: Path) -> None:
    runtime_zip, cuda_zip = _archives(tmp_path)
    with zipfile.ZipFile(runtime_zip, "a") as archive:
        archive.writestr("../escape.dll", _pe_x64(b"escape"))
    lock = _lock(tmp_path / "distribution-lock.json", runtime_zip, cuda_zip)
    output = tmp_path / "runtime"

    with pytest.raises(WindowsLlamaCppRuntimeError, match="unsafe"):
        materialize_windows_llama_cpp_runtime(
            distribution_lock_path=lock,
            runtime_archive_path=runtime_zip,
            cuda_archive_path=cuda_zip,
            output_dir=output,
            runner=Probe(),
        )

    assert not output.exists()
