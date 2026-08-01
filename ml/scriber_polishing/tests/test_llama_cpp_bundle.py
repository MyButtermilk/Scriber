from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import scriber_polishing.llama_cpp_bundle as bundle_module
from scriber_polishing.hf_quantization_pipeline import (
    HfQuantizationError,
    build_cloud_llama_cpp_binding,
)
from scriber_polishing.llama_cpp_bundle import (
    LlamaCppBundleError,
    LocalProbeResult,
    load_verified_llama_cpp_bundle,
    materialize_llama_cpp_bundle_lock,
    verify_llama_cpp_runtime_closure,
)

_RUNTIME_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _elf_x86_64(label: bytes) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")
    return bytes(header) + label


def _oci_kwargs(commit: str) -> dict[str, object]:
    return {
        "oci_repository": "ghcr.io/ggml-org/llama.cpp",
        "oci_tag": "full-cuda12-b8000",
        "oci_index_digest": "sha256:" + "3" * 64,
        "oci_manifest_digest": "sha256:" + "4" * 64,
        "oci_config_digest": "sha256:" + "5" * 64,
        "oci_revision": commit,
        "oci_layers": [
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
        "runtime_image": _RUNTIME_IMAGE,
        "native_runtime_root": "runtime/app",
    }


class _ProbeRunner:
    def __init__(
        self,
        root: Path,
        commit: str,
        version: str,
        *,
        missing_native_dependency: bool = False,
        dirty_git_status: bool = False,
        quantize_reports_usage_for_version: bool = False,
        unresolved_cuda_driver: bool = False,
        resolved_cuda_driver: bool = False,
        ldd_stderr: bytes = b"",
    ) -> None:
        self.root = root
        self.commit = commit
        self.version = version
        self.missing_native_dependency = missing_native_dependency
        self.dirty_git_status = dirty_git_status
        self.quantize_reports_usage_for_version = quantize_reports_usage_for_version
        self.unresolved_cuda_driver = unresolved_cuda_driver
        self.resolved_cuda_driver = resolved_cuda_driver
        self.ldd_stderr = ldd_stderr

    def run(self, command, *, cwd, timeout_seconds):
        del timeout_seconds
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return LocalProbeResult(
                0,
                self.commit.encode("ascii") + b"\n",
                b"",
            )
        if command[:6] == [
            "git",
            "-c",
            "core.fileMode=false",
            "-c",
            "core.autocrlf=true",
            "status",
        ]:
            stdout = b" M conversion/__init__.py\n" if self.dirty_git_status else b""
            return LocalProbeResult(0, stdout, b"warning: ignored global excludes file\n")
        if command[:3] == ["git", "remote", "get-url"]:
            return LocalProbeResult(
                0,
                b"https://github.com/ggml-org/llama.cpp.git\n",
                b"",
            )
        if command[-1] == "--version":
            if (
                Path(command[0]).name == "llama-quantize"
                and self.quantize_reports_usage_for_version
            ):
                return LocalProbeResult(
                    1,
                    (
                        f"usage: {command[0]} [--help] [--allow-requantize] [--dry-run]\n"
                        "       model-f32.gguf [model-quant.gguf] type [nthreads]\n\n"
                        "  --allow-requantize\n"
                        "  --dry-run\n"
                        " allowed quantization types\n"
                        "  Q4_K_M  : quantization\n"
                        "  Q8_0    : quantization\n"
                    ).encode(),
                    b"",
                )
            return LocalProbeResult(
                0,
                f"llama.cpp {self.version}\n".encode(),
                b"build stderr\n",
            )
        if command[0] == "ldd":
            if self.missing_native_dependency:
                return LocalProbeResult(
                    0,
                    b"libggml.so.0 => not found\n",
                    b"",
                )
            library = (self.root / "runtime" / "app" / "libggml.so.0").resolve()
            cuda_driver = ""
            if self.unresolved_cuda_driver:
                cuda_driver = "libcuda.so.1 => not found\n"
            elif self.resolved_cuda_driver:
                cuda_driver = (
                    "libcuda.so.1 => /usr/local/nvidia/lib64/libcuda.so.1 "
                    "(0x00007f04)\n"
                )
            return LocalProbeResult(
                0,
                (
                    cuda_driver + f"libggml.so.0 => {library} (0x00007f01)\n"
                    "libc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x00007f02)\n"
                    "linux-vdso.so.1 (0x00007f03)\n"
                ).encode(),
                self.ldd_stderr,
            )
        raise AssertionError(command)


def _materialized_bundle(
    root: Path,
    *,
    version: str = "b7000-test",
    library_payload: bytes | None = None,
    add_library_symlink: bool = False,
    missing_native_dependency: bool = False,
    dirty_git_status: bool = False,
    quantize_reports_usage_for_version: bool = False,
    required_host_driver_sonames: tuple[str, ...] = (),
    unresolved_cuda_driver: bool = False,
    empty_runtime_files: tuple[str, ...] = (),
) -> Path:
    source = root / "source"
    conversion = source / "conversion"
    gguf_py = source / "gguf-py"
    runtime = root / "runtime" / "app"
    (source / ".git").mkdir(parents=True)
    conversion.mkdir()
    gguf_py.mkdir()
    runtime.mkdir(parents=True)
    (source / "convert_hf_to_gguf.py").write_text("print('converter')\n", encoding="utf-8")
    (conversion / "__init__.py").write_text("REGISTRY = {}\n", encoding="utf-8")
    (gguf_py / "gguf.py").write_text("GGUF_VERSION = 3\n", encoding="utf-8")
    for name in ("llama-quantize", "llama-cli", "llama-server"):
        (runtime / name).write_bytes(_elf_x86_64(name.encode()))
    (runtime / "libggml.so.0").write_bytes(library_payload or _elf_x86_64(b"libggml"))
    for relative in empty_runtime_files:
        marker = runtime / relative
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
    if add_library_symlink:
        os.symlink(
            "libggml.so.0",
            runtime / "libggml.so",
        )
    lock_path = root / "llama-cpp-bundle-lock.json"
    materialize_llama_cpp_bundle_lock(
        root,
        lock_path,
        expected_commit="1" * 40,
        llama_cpp_version=version,
        compiler_id="GNU",
        compiler_version="13.3",
        cuda_enabled=True,
        cuda_version="12.8",
        generator="Ninja",
        configuration="Release",
        flags=["-DGGML_CUDA=ON", "-DCMAKE_BUILD_TYPE=Release"],
        convert_hf_to_gguf_path="source/convert_hf_to_gguf.py",
        llama_quantize_path="runtime/app/llama-quantize",
        llama_cli_path="runtime/app/llama-cli",
        llama_server_path="runtime/app/llama-server",
        runner=_ProbeRunner(
            root,
            "1" * 40,
            version,
            missing_native_dependency=missing_native_dependency,
            dirty_git_status=dirty_git_status,
            quantize_reports_usage_for_version=quantize_reports_usage_for_version,
            unresolved_cuda_driver=unresolved_cuda_driver,
        ),
        required_host_driver_sonames=required_host_driver_sonames,
        **_oci_kwargs("1" * 40),
    )
    return lock_path


def test_materializer_accepts_only_the_pinned_quantize_usage_contract_for_version_probe(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(
        tmp_path,
        version="b10156",
        quantize_reports_usage_for_version=True,
    )

    value = json.loads(lock.read_text(encoding="utf-8"))
    assert value["build"]["llama_cpp_version"] == "b10156"
    assert value["build"]["llama_quantize_version_output_sha256"].startswith(
        "sha256:"
    )


@pytest.mark.parametrize(
    ("returncode", "stdout_suffix", "stderr", "expected_version"),
    (
        (2, b"", b"", "b10156"),
        (1, b"missing-q8-marker\n", b"", "b10156"),
        (1, b"", b"unexpected loader warning\n", "b10156"),
        (1, b"", b"", "b10157"),
    ),
)
def test_quantize_usage_version_fallback_rejects_any_contract_drift(
    tmp_path: Path,
    returncode: int,
    stdout_suffix: bytes,
    stderr: bytes,
    expected_version: str,
) -> None:
    valid_usage = (
        b"usage: /bundle/runtime/app/llama-quantize [--help] [--allow-requantize] [--dry-run]\n"
        b"       model-f32.gguf [model-quant.gguf] type [nthreads]\n"
        b"  --dry-run\n"
        b" allowed quantization types\n"
        b"  Q4_K_M  : quantization\n"
        b"  Q8_0    : quantization\n"
    )
    if stdout_suffix:
        valid_usage = valid_usage.replace(b"  Q8_0    : quantization\n", stdout_suffix)

    class Runner:
        def run(self, command, *, cwd, timeout_seconds):
            del command, cwd, timeout_seconds
            return LocalProbeResult(returncode, valid_usage, stderr)

    with pytest.raises(LlamaCppBundleError, match="version probe failed"):
        bundle_module._version_probe_hash(
            Runner(),
            tmp_path / "llama-quantize",
            cwd=tmp_path,
            expected_version=expected_version,
            label="llama-quantize",
        )


def test_native_runtime_inventory_accepts_only_exact_empty_pep561_marker(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(
        tmp_path,
        empty_runtime_files=("gguf-py/gguf/py.typed",),
    )

    value = json.loads(lock.read_text(encoding="utf-8"))
    marker = next(
        entry
        for entry in value["native_runtime"]["entries"]
        if entry["path"] == "runtime/app/gguf-py/gguf/py.typed"
    )

    assert marker == {
        "path": "runtime/app/gguf-py/gguf/py.typed",
        "kind": "file",
        "bytes": 0,
        "sha256": _hash(b""),
    }


@pytest.mark.parametrize(
    "relative",
    (
        "unexpected.txt",
        "other/py.typed",
    ),
)
def test_native_runtime_inventory_rejects_other_empty_files(
    tmp_path: Path,
    relative: str,
) -> None:
    with pytest.raises(LlamaCppBundleError, match="native runtime file is empty"):
        _materialized_bundle(
            tmp_path,
            empty_runtime_files=(relative,),
        )


def test_ldd_closure_allows_only_pinned_image_cuda_runtime_path() -> None:
    dependencies = bundle_module._ldd_dependencies(
        "libcudart.so.12 => "
        "/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib/libcudart.so.12\n",
        inventory_paths=set(),
    )

    assert dependencies == [
        {
            "soname": "libcudart.so.12",
            "path": "/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib/libcudart.so.12",
            "provider": "runtime_image",
        }
    ]


def test_ldd_closure_allows_only_pinned_image_cublas_runtime_path() -> None:
    dependencies = bundle_module._ldd_dependencies(
        "libcublas.so.12 => "
        "/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib/libcublas.so.12\n",
        inventory_paths=set(),
    )

    assert dependencies == [
        {
            "soname": "libcublas.so.12",
            "path": "/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib/libcublas.so.12",
            "provider": "runtime_image",
        }
    ]


def test_ldd_closure_allows_real_pinned_image_nccl_runtime_path() -> None:
    dependencies = bundle_module._ldd_dependencies(
        "libnccl.so.2 => "
        "/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so.2\n",
        inventory_paths=set(),
    )

    assert dependencies == [
        {
            "soname": "libnccl.so.2",
            "path": "/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so.2",
            "provider": "runtime_image",
        }
    ]

    with pytest.raises(LlamaCppBundleError, match="outside the locked closure"):
        bundle_module._ldd_dependencies(
            "libnccl.so.2 => /usr/local/lib/python3.12/dist-packages/nvidia/libnccl.so.2\n",
            inventory_paths=set(),
        )


def test_cpu_materialization_binds_only_exact_unresolved_cuda_driver() -> None:
    dependencies = bundle_module._ldd_dependencies(
        "libcuda.so.1 => not found\n",
        inventory_paths=set(),
        required_host_driver_sonames=("libcuda.so.1",),
        allow_unresolved_host_drivers=True,
    )

    assert dependencies == [
        {
            "soname": "libcuda.so.1",
            "path": "${NVIDIA_DRIVER}/libcuda.so.1",
            "provider": "nvidia_driver_required",
        }
    ]

    with pytest.raises(LlamaCppBundleError, match="only exact libcuda.so.1"):
        bundle_module._ldd_dependencies(
            "libcuda.so.2 => not found\n",
            inventory_paths=set(),
            required_host_driver_sonames=("libcuda.so.2",),
            allow_unresolved_host_drivers=True,
        )


@pytest.mark.parametrize(
    "line,required",
    (
        ("libcuda.so.1 => not found\n", ()),
        ("libcuda.so => not found\n", ("libcuda.so.1",)),
        ("libcuda.so.2 => not found\n", ("libcuda.so.1",)),
        ("libcudart.so.12 => not found\n", ("libcuda.so.1",)),
        ("libcuda.so.1 => not found trailing\n", ("libcuda.so.1",)),
    ),
)
def test_cpu_materialization_rejects_missing_dependency_drift(
    line: str,
    required: tuple[str, ...],
) -> None:
    with pytest.raises(LlamaCppBundleError, match="dependency is missing"):
        bundle_module._ldd_dependencies(
            line,
            inventory_paths=set(),
            required_host_driver_sonames=required,
            allow_unresolved_host_drivers=True,
        )


def test_live_cuda_driver_is_canonicalized_but_stub_is_rejected() -> None:
    dependencies = bundle_module._ldd_dependencies(
        "libcuda.so.1 => /usr/local/nvidia/lib64/libcuda.so.1\n",
        inventory_paths=set(),
        required_host_driver_sonames=("libcuda.so.1",),
    )

    assert dependencies == [
        {
            "soname": "libcuda.so.1",
            "path": "${NVIDIA_DRIVER}/libcuda.so.1",
            "provider": "nvidia_driver_required",
        }
    ]
    with pytest.raises(LlamaCppBundleError, match="real NVIDIA driver"):
        bundle_module._ldd_dependencies(
            "libcuda.so.1 => /usr/local/cuda/lib64/stubs/libcuda.so.1\n",
            inventory_paths=set(),
            required_host_driver_sonames=("libcuda.so.1",),
        )


def test_unresolved_cuda_driver_lock_requires_live_resolution_by_default(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(
        tmp_path,
        required_host_driver_sonames=("libcuda.so.1",),
        unresolved_cuda_driver=True,
    )
    value = json.loads(lock.read_text(encoding="utf-8"))
    closure = value["native_runtime"]["ldd_closure"]
    assert closure["required_host_driver_sonames"] == ["libcuda.so.1"]
    assert closure["algorithm"] == "ldd-contract-v2-host-driver"

    verified = load_verified_llama_cpp_bundle(tmp_path, lock)
    with pytest.raises(LlamaCppBundleError, match="dependency is missing"):
        verify_llama_cpp_runtime_closure(
            verified,
            runner=_ProbeRunner(
                tmp_path,
                "1" * 40,
                "b7000-test",
                unresolved_cuda_driver=True,
            ),
        )

    report = verify_llama_cpp_runtime_closure(
        verified,
        runner=_ProbeRunner(
            tmp_path,
            "1" * 40,
            "b7000-test",
            resolved_cuda_driver=True,
        ),
    )
    assert report["required_host_driver_sonames"] == ["libcuda.so.1"]
    assert report["host_driver_resolution"] == "live"

    with pytest.raises(LlamaCppBundleError, match="outside the locked closure"):
        bundle_module._ldd_dependencies(
            "untrusted.so => /usr/local/lib/python3.12/dist-packages/untrusted.so\n",
            inventory_paths=set(),
        )


def test_ldd_stderr_is_always_fail_closed(tmp_path: Path) -> None:
    lock = _materialized_bundle(tmp_path)
    verified = load_verified_llama_cpp_bundle(tmp_path, lock)

    with pytest.raises(LlamaCppBundleError, match="diagnostics on stderr"):
        verify_llama_cpp_runtime_closure(
            verified,
            runner=_ProbeRunner(
                tmp_path,
                "1" * 40,
                "b7000-test",
                ldd_stderr=b"libmystery.so => not found\n",
            ),
        )


def test_materializer_rejects_non_x86_64_native_shared_library(
    tmp_path: Path,
) -> None:
    foreign = bytearray(_elf_x86_64(b"foreign"))
    foreign[18:20] = (183).to_bytes(2, "little")

    with pytest.raises(
        LlamaCppBundleError,
        match="ELF64 x86-64",
    ):
        _materialized_bundle(
            tmp_path,
            library_payload=bytes(foreign),
        )


def test_native_runtime_inventory_binds_relative_shared_library_symlink(
    tmp_path: Path,
) -> None:
    try:
        lock = _materialized_bundle(
            tmp_path,
            add_library_symlink=True,
        )
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise

    value = json.loads(lock.read_text(encoding="utf-8"))
    symlinks = [entry for entry in value["native_runtime"]["entries"] if entry["kind"] == "symlink"]

    assert symlinks == [
        {
            "path": "runtime/app/libggml.so",
            "kind": "symlink",
            "target": "libggml.so.0",
            "sha256": _hash(b"symlink\0libggml.so.0"),
        }
    ]


def test_materializer_rejects_missing_ldd_dependency(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        LlamaCppBundleError,
        match="dependency is missing",
    ):
        _materialized_bundle(
            tmp_path,
            missing_native_dependency=True,
        )


def test_materializer_tolerates_git_warnings_but_rejects_porcelain_changes(
    tmp_path: Path,
) -> None:
    with pytest.raises(LlamaCppBundleError, match="checkout is not clean"):
        _materialized_bundle(tmp_path, dirty_git_status=True)


def test_runtime_closure_recheck_also_rebinds_complete_native_inventory(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(tmp_path)
    verified = load_verified_llama_cpp_bundle(tmp_path, lock)
    (tmp_path / "runtime" / "app" / "unexpected.txt").write_text(
        "substitution\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LlamaCppBundleError,
        match="native runtime inventory",
    ):
        verify_llama_cpp_runtime_closure(
            verified,
            runner=_ProbeRunner(
                tmp_path,
                "1" * 40,
                "b7000-test",
            ),
        )


def test_checked_in_llama_cpp_lock_template_is_deliberately_unmaterialized() -> None:
    template = Path(__file__).parents[1] / "configs" / "llama_cpp_bundle_lock.template.json"

    with pytest.raises(LlamaCppBundleError, match="UNMATERIALIZED"):
        load_verified_llama_cpp_bundle(template.parent, template)


def test_llama_cpp_bundle_lock_binds_source_subtrees_binaries_and_build_identity(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(tmp_path)

    verified = load_verified_llama_cpp_bundle(tmp_path, lock)

    assert verified.commit == "1" * 40
    assert verified.convert_hf_to_gguf == (tmp_path / "source" / "convert_hf_to_gguf.py").resolve()
    assert verified.llama_quantize == (tmp_path / "runtime" / "app" / "llama-quantize").resolve()
    assert verified.llama_cli == (tmp_path / "runtime" / "app" / "llama-cli").resolve()
    assert verified.llama_server == (tmp_path / "runtime" / "app" / "llama-server").resolve()
    assert verified.lock_sha256 == _hash(lock.read_bytes())
    assert verified.source_tree_sha256.startswith("sha256:")
    assert verified.build_identity_sha256.startswith("sha256:")

    (tmp_path / "source" / "conversion" / "__init__.py").write_text(
        "tampered = True\n",
        encoding="utf-8",
    )
    with pytest.raises(LlamaCppBundleError, match="source inventory"):
        load_verified_llama_cpp_bundle(tmp_path, lock)


def test_cloud_binding_rejects_a_nonreviewed_llama_cpp_revision(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(tmp_path)

    with pytest.raises(
        HfQuantizationError,
        match="exact reviewed b10156",
    ):
        build_cloud_llama_cpp_binding(tmp_path, lock)


def test_llama_cpp_bundle_lock_rejects_caller_substitution_of_a_binary(
    tmp_path: Path,
) -> None:
    lock = _materialized_bundle(tmp_path)
    (tmp_path / "runtime" / "app" / "llama-cli").write_bytes(b"substituted")

    with pytest.raises(
        LlamaCppBundleError,
        match="native runtime inventory",
    ):
        load_verified_llama_cpp_bundle(tmp_path, lock)


def test_materializer_derives_hashes_from_clean_checkout_and_real_probe_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    source = root / "source"
    (source / ".git").mkdir(parents=True)
    (source / "conversion").mkdir()
    (source / "gguf-py").mkdir()
    runtime = root / "runtime" / "app"
    runtime.mkdir(parents=True)
    (source / "convert_hf_to_gguf.py").write_text("print('convert')\n", encoding="utf-8")
    (source / "conversion" / "__init__.py").write_text("REGISTRY = {}\n", encoding="utf-8")
    (source / "gguf-py" / "gguf.py").write_text("VERSION = 3\n", encoding="utf-8")
    for name in ("llama-quantize", "llama-cli", "llama-server"):
        (runtime / name).write_bytes(_elf_x86_64(name.encode("ascii")))
    (runtime / "libggml.so.0").write_bytes(_elf_x86_64(b"libggml"))

    class Runner:
        def run(self, command, *, cwd, timeout_seconds):
            del timeout_seconds
            if command[:3] == ["git", "rev-parse", "--verify"]:
                return LocalProbeResult(0, b"2" * 40 + b"\n", b"")
            if command[:6] == [
                "git",
                "-c",
                "core.fileMode=false",
                "-c",
                "core.autocrlf=true",
                "status",
            ]:
                return LocalProbeResult(0, b"", b"warning: ignored global excludes file\n")
            if command[:3] == ["git", "remote", "get-url"]:
                return LocalProbeResult(
                    0,
                    b"https://github.com/ggml-org/llama.cpp.git\n",
                    b"",
                )
            if command[-1] == "--version":
                return LocalProbeResult(0, b"llama.cpp b8000-local\n", b"build stderr\n")
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

    output = root / "llama-cpp-bundle-lock.json"
    verified = materialize_llama_cpp_bundle_lock(
        root,
        output,
        expected_commit="2" * 40,
        llama_cpp_version="b8000-local",
        compiler_id="MSVC",
        compiler_version="19.44",
        cuda_enabled=True,
        cuda_version="12.8",
        generator="Ninja",
        configuration="Release",
        flags=["/Brepro", "GGML_CUDA=ON"],
        oci_repository="ghcr.io/ggml-org/llama.cpp",
        oci_tag="full-cuda12-b8000",
        oci_index_digest="sha256:" + "3" * 64,
        oci_manifest_digest="sha256:" + "4" * 64,
        oci_config_digest="sha256:" + "5" * 64,
        oci_revision="2" * 40,
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
        runner=Runner(),
    )

    lock = json.loads(output.read_text(encoding="utf-8"))
    actual_probe_hash = _hash(b"llama.cpp b8000-local\n\0build stderr\n")
    assert lock["schema_version"] == 3
    assert lock["distribution"] == {
        "kind": "oci_image_layers",
        "repository": "ghcr.io/ggml-org/llama.cpp",
        "tag": "full-cuda12-b8000",
        "index_digest": "sha256:" + "3" * 64,
        "manifest_digest": "sha256:" + "4" * 64,
        "config_digest": "sha256:" + "5" * 64,
        "revision": "2" * 40,
        "platform": {"os": "linux", "architecture": "amd64"},
        "layers": [
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
        "runtime_image": "pytorch/pytorch@sha256:" + "8" * 64,
    }
    assert lock["build"]["llama_server_version_output_sha256"] == actual_probe_hash
    assert lock["native_runtime"]["root"] == "runtime/app"
    assert lock["native_runtime"]["library_directories"] == ["runtime/app"]
    assert {row["kind"] for row in lock["native_runtime"]["entries"]} == {"file"}
    assert {
        dependency["provider"]
        for entry in lock["native_runtime"]["ldd_closure"]["entries"]
        for dependency in entry["dependencies"]
    } == {"bundle", "runtime_image", "kernel"}
    assert lock["tools"]["llama_server"]["sha256"] == _hash((runtime / "llama-server").read_bytes())
    assert verified.commit == "2" * 40
    assert verified.runtime_library_paths == (runtime.resolve(),)
    assert all(not row["path"].startswith(".git") for row in lock["source"]["files"])
    closure = verify_llama_cpp_runtime_closure(verified, runner=Runner())
    assert closure == {
        "status": "verified",
        "algorithm": "ldd-contract-v2-host-driver",
        "elf_object_count": 4,
        "ldd_closure_sha256": _hash(
            json.dumps(
                lock["native_runtime"]["ldd_closure"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ),
        "library_directories": [str(runtime.resolve())],
        "required_host_driver_sonames": [],
        "host_driver_resolution": "not_required",
    }


def test_materializer_refuses_dirty_or_wrong_revision_without_writing_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    (root / "source" / ".git").mkdir(parents=True)

    class WrongRevision:
        def run(self, command, **kwargs):
            del command, kwargs
            return LocalProbeResult(0, b"3" * 40 + b"\n", b"")

    output = root / "lock.json"
    with pytest.raises(LlamaCppBundleError, match="commit mismatch"):
        materialize_llama_cpp_bundle_lock(
            root,
            output,
            expected_commit="2" * 40,
            llama_cpp_version="b8000",
            compiler_id="MSVC",
            compiler_version="19.44",
            cuda_enabled=True,
            cuda_version="12.8",
            generator="Ninja",
            configuration="Release",
            flags=["/Brepro"],
            runner=WrongRevision(),
            **_oci_kwargs("2" * 40),
        )
    assert not output.exists()
