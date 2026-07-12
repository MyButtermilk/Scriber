"""Validate the bundled static diarization worker without loading user models.

The same stdlib-only smoke runs against the staged backend resource tree and
the installed NSIS tree.  It intentionally does not inspect SCRIBER_DATA_DIR:
the two ONNX models are an optional post-install component and must be absent
from the signed base package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
from pathlib import Path
from typing import Any


WORKER_NAME = "scriber-diarization-sidecar"
WORKER_FILE = f"{WORKER_NAME}.exe"
MANIFEST_FILE = f"{WORKER_NAME}.manifest.json"
WORKER_VERSION = "0.1.0"
SHERPA_VERSION = "1.13.3"
SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024
MAX_WORKER_BYTES = 64 * 1024 * 1024

SEGMENTATION_MODEL_FILE = "pyannote-segmentation-3.0.int8.onnx"
EMBEDDING_MODEL_FILE = (
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
OPTIONAL_COMPONENT_FILE = "component.json"

_FORBIDDEN_DYNAMIC_IMPORT_MARKERS = (
    "sherpa",
    "onnx",
    "torch",
    "c10",
    "tensorflow",
    "openvino",
    "cudart",
    "cublas",
    "libstdc++",
    "libgcc",
    "vcruntime",
    "msvcp",
    "ucrtbase",
    "api-ms-win-crt",
    "concrt",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int:
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            relative = rva - virtual_address
            if relative >= raw_size:
                break
            return raw_offset + relative
    raise ValueError("PE import table points outside file-backed sections.")


def _pe_imports(path: Path) -> list[str]:
    """Return imported DLL names using only the PE headers.

    Installed machines do not necessarily have ``dumpbin`` or LLVM tools, so
    the release smoke parses the bounded import table directly.
    """

    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("Diarization worker is not a PE executable.")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("Diarization worker has an invalid PE header.")

    coff_offset = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff_offset + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    optional_offset = coff_offset + 20
    if optional_offset + optional_size > len(data):
        raise ValueError("Diarization worker has a truncated optional header.")
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    if magic == 0x20B:  # PE32+
        data_directory_offset = optional_offset + 112
    elif magic == 0x10B:  # PE32
        data_directory_offset = optional_offset + 96
    else:
        raise ValueError("Diarization worker uses an unsupported PE format.")
    import_directory_offset = data_directory_offset + 8
    if import_directory_offset + 8 > optional_offset + optional_size:
        raise ValueError("Diarization worker has no bounded PE import directory.")
    import_rva, import_size = struct.unpack_from("<II", data, import_directory_offset)
    if import_rva == 0 or import_size == 0:
        return []

    section_offset = optional_offset + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        entry = section_offset + index * 40
        if entry + 40 > len(data):
            raise ValueError("Diarization worker has a truncated PE section table.")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, entry + 8
        )
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    descriptor_offset = _rva_to_offset(import_rva, sections)
    descriptor_limit = min(len(data), descriptor_offset + import_size)
    imports: list[str] = []
    descriptor_count = 0
    while descriptor_offset + 20 <= descriptor_limit:
        descriptor = struct.unpack_from("<IIIII", data, descriptor_offset)
        if descriptor == (0, 0, 0, 0, 0):
            break
        name_rva = descriptor[3]
        name_offset = _rva_to_offset(name_rva, sections)
        name_end = data.find(b"\0", name_offset, min(len(data), name_offset + 512))
        if name_end < 0:
            raise ValueError("Diarization worker has an unterminated PE import name.")
        try:
            name = data[name_offset:name_end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Diarization worker has a non-ASCII PE import name.") from exc
        imports.append(name)
        descriptor_offset += 20
        descriptor_count += 1
        if descriptor_count > 4096:
            raise ValueError("Diarization worker has too many PE imports.")
    else:
        raise ValueError("Diarization worker PE import table is not terminated.")
    return sorted(set(imports), key=str.casefold)


def _run_control(executable: Path, argument: str) -> dict[str, Any]:
    process = subprocess.run(
        [str(executable), argument],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if (
        process.returncode != 0
        or len(process.stdout) > MAX_CONTROL_BYTES
        or len(process.stderr) > MAX_CONTROL_BYTES
    ):
        raise RuntimeError(f"Diarization worker {argument} probe failed.")
    try:
        payload = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Diarization worker {argument} returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Diarization worker {argument} returned an invalid payload.")
    return payload


def _assert_identity(payload: dict[str, Any], *, label: str) -> None:
    worker = payload.get("worker")
    engine = payload.get("engine")
    if (
        payload.get("schemaVersion") != SCHEMA_VERSION
        or payload.get("ok") is not True
        or not isinstance(worker, dict)
        or worker.get("name") != WORKER_NAME
        or worker.get("version") != WORKER_VERSION
        or not isinstance(engine, dict)
        or engine.get("name") != "sherpa-onnx"
        or engine.get("version") != SHERPA_VERSION
        or engine.get("linkMode") != "static"
    ):
        raise RuntimeError(f"Diarization worker {label} identity is incompatible.")


def _resolve_resource_dir(root: Path) -> Path:
    candidates = (
        root,
        root / "tools" / "diarization",
        root / "backend" / "tools" / "diarization",
        root / "resources" / "backend" / "tools" / "diarization",
    )
    matches = [
        candidate
        for candidate in candidates
        if (candidate / WORKER_FILE).is_file()
        or (candidate / MANIFEST_FILE).is_file()
    ]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one bundled diarization worker resource.")
    return matches[0]


def validate_resource(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Diarization smoke root must be an existing directory.")
    resource_dir = _resolve_resource_dir(root)
    executable = resource_dir / WORKER_FILE
    manifest_path = resource_dir / MANIFEST_FILE
    if not executable.is_file() or not manifest_path.is_file():
        raise RuntimeError("Bundled diarization worker or manifest is missing.")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Bundled diarization worker manifest is invalid.") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("Bundled diarization worker manifest is invalid.")
    worker = manifest.get("worker")
    executable_size = executable.stat().st_size
    if (
        manifest.get("schemaVersion") != SCHEMA_VERSION
        or manifest.get("distribution") != "bundled-signed-scriber-resource"
        or not isinstance(worker, dict)
        or worker.get("name") != WORKER_NAME
        or worker.get("fileName") != WORKER_FILE
        or worker.get("version") != WORKER_VERSION
        or worker.get("protocolSchemaVersion") != SCHEMA_VERSION
        or worker.get("sherpaOnnxVersion") != SHERPA_VERSION
        or worker.get("linkMode") != "static"
        or executable_size <= 0
        or executable_size > MAX_WORKER_BYTES
        or worker.get("byteSize") != executable_size
        or worker.get("sha256") != _sha256(executable)
    ):
        raise RuntimeError("Bundled diarization worker attestation does not match.")

    version = _run_control(executable, "--version")
    self_test = _run_control(executable, "--self-test")
    _assert_identity(version, label="version")
    _assert_identity(self_test, label="self-test")
    platform = self_test.get("platform")
    if (
        self_test.get("loadsUserAudio") is not False
        or self_test.get("loadsModels") is not False
        or not isinstance(platform, dict)
        or platform.get("windows") is not True
        or platform.get("memoryLimit") != "jobObject"
    ):
        raise RuntimeError("Diarization worker self-test policy is incompatible.")

    imports = _pe_imports(executable)
    forbidden_imports = [
        name
        for name in imports
        if any(marker in name.casefold() for marker in _FORBIDDEN_DYNAMIC_IMPORT_MARKERS)
    ]
    if forbidden_imports:
        raise RuntimeError("Diarization worker is not statically self-contained.")

    forbidden_files = []
    for name in (SEGMENTATION_MODEL_FILE, EMBEDDING_MODEL_FILE):
        forbidden_files.extend(path for path in root.rglob(name) if path.is_file())
    forbidden_files.extend(path for path in resource_dir.rglob("*.onnx") if path.is_file())
    forbidden_files.extend(
        path for path in resource_dir.rglob(OPTIONAL_COMPONENT_FILE) if path.is_file()
    )
    if forbidden_files:
        raise RuntimeError("Optional diarization models are present in the base package.")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "resourceDir": resource_dir.relative_to(root).as_posix(),
        "worker": {
            "version": WORKER_VERSION,
            "sherpaOnnxVersion": SHERPA_VERSION,
            "linkMode": "static",
            "sha256": worker["sha256"],
            "byteSize": worker["byteSize"],
            "peImports": imports,
        },
        "selfTest": {
            "loadsUserAudio": False,
            "loadsModels": False,
            "memoryLimit": "jobObject",
        },
        "optionalModelsAbsent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = validate_resource(args.root)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
