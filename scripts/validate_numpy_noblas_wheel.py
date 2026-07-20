"""Validate Scriber's locked CPython 3.13 NumPy no-BLAS wheel.

The validator is intentionally standard-library-only so it can run before the
backend environment exists.  It treats the wheel, its lock, its static runtime
version, licenses, RECORD entries, and PE import tables as one artifact.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_PATH = (
    REPO_ROOT / "packaging" / "wheels" / "numpy-noblas-wheel-lock-v1.json"
)
CONTRACT = "ScriberNumPyNoBlasWheelLockV1"
SCHEMA_VERSION = 1
MAX_PYD_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ValidationError(RuntimeError):
    """Raised when the locked wheel contract is not satisfied."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key in NumPy wheel lock: {key}")
        result[key] = value
    return result


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read NumPy wheel lock: {path}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("NumPy wheel lock must be a JSON object")
    if set(payload) != {
        "contract",
        "schemaVersion",
        "artifact",
        "source",
        "build",
        "validation",
    }:
        raise ValidationError("NumPy wheel lock has unexpected top-level fields")
    if payload.get("contract") != CONTRACT or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValidationError("NumPy wheel lock contract is incompatible")
    for field in ("artifact", "source", "build", "validation"):
        if not isinstance(payload.get(field), dict):
            raise ValidationError(f"NumPy wheel lock {field} must be an object")
    return payload


def _require_string(mapping: Mapping[str, Any], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}.{key} must be a non-empty string")
    return value


def _require_int(mapping: Mapping[str, Any], key: str, *, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label}.{key} must be a non-negative integer")
    return value


def _require_string_list(mapping: Mapping[str, Any], key: str, *, label: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{label}.{key} must be a non-empty string array")
    if len(value) != len(set(value)):
        raise ValidationError(f"{label}.{key} contains duplicates")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_artifact_lock(lock: Mapping[str, Any]) -> dict[str, Any]:
    artifact = lock["artifact"]
    validation = lock["validation"]
    assert isinstance(artifact, dict)
    assert isinstance(validation, dict)

    expected_fields = {
        "relativePath",
        "fileName",
        "distribution",
        "version",
        "pythonTag",
        "abiTag",
        "platformTag",
        "wheelTag",
        "length",
        "sha256",
        "entryCount",
        "uncompressedBytes",
        "compressedPayloadBytes",
        "pydCount",
        "pydBytes",
    }
    if set(artifact) != expected_fields:
        raise ValidationError("artifact lock fields are not exact")

    file_name = _require_string(artifact, "fileName", label="artifact")
    relative_path = _require_string(artifact, "relativePath", label="artifact")
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts or pure_path.name != file_name:
        raise ValidationError("artifact.relativePath is unsafe or inconsistent")
    if pure_path.parts[:2] != ("packaging", "wheels"):
        raise ValidationError("artifact.relativePath must stay under packaging/wheels")

    sha256 = _require_string(artifact, "sha256", label="artifact")
    if not SHA256_RE.fullmatch(sha256):
        raise ValidationError("artifact.sha256 is invalid")
    for key in (
        "length",
        "entryCount",
        "uncompressedBytes",
        "compressedPayloadBytes",
        "pydCount",
        "pydBytes",
    ):
        _require_int(artifact, key, label="artifact")

    distribution = _require_string(artifact, "distribution", label="artifact")
    version = _require_string(artifact, "version", label="artifact")
    python_tag = _require_string(artifact, "pythonTag", label="artifact")
    abi_tag = _require_string(artifact, "abiTag", label="artifact")
    platform_tag = _require_string(artifact, "platformTag", label="artifact")
    wheel_tag = _require_string(artifact, "wheelTag", label="artifact")
    expected_file_name = (
        f"{distribution}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    )
    if file_name != expected_file_name or wheel_tag != f"{python_tag}-{abi_tag}-{platform_tag}":
        raise ValidationError("artifact filename and wheel tags are inconsistent")

    if validation.get("metadataName") != distribution:
        raise ValidationError("validation metadata name differs from artifact distribution")
    if validation.get("metadataVersion") != version:
        raise ValidationError("validation metadata version differs from artifact version")
    if validation.get("rootIsPurelib") is not False:
        raise ValidationError("NumPy wheel must be locked as a platform wheel")
    dependencies = validation.get("buildDependencies")
    if dependencies != {"blas": "none", "lapack": "none"}:
        raise ValidationError("NumPy wheel build dependency policy is invalid")
    _require_string_list(validation, "forbiddenArchiveSuffixes", label="validation")
    _require_string_list(validation, "forbiddenArchivePathMarkers", label="validation")
    _require_string_list(validation, "forbiddenPeImportMarkers", label="validation")
    _require_string_list(validation, "allowedPeImports", label="validation")
    _require_string_list(validation, "licenseFiles", label="validation")
    _require_string(validation, "licenseExpression", label="validation")
    return dict(artifact)


def _validate_member_names(names: Sequence[str]) -> None:
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ValidationError("wheel contains duplicate or case-colliding members")
    for name in names:
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or any(part in ("", ".") or ":" in part for part in path.parts)
        ):
            raise ValidationError(f"wheel contains an unsafe member name: {name!r}")


def extract_validated_wheel(wheel_path: Path, destination: Path) -> int:
    """Extract a previously validated wheel into one empty local directory."""

    wheel_path = wheel_path.resolve(strict=True)
    destination = destination.resolve(strict=True)
    if not destination.is_dir() or destination.is_symlink():
        raise ValidationError("NumPy overlay destination must be a physical directory")
    if any(destination.iterdir()):
        raise ValidationError("NumPy overlay destination must be empty")

    extracted = 0
    with zipfile.ZipFile(wheel_path) as archive:
        infos = archive.infolist()
        _validate_member_names([info.filename for info in infos])
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise ValidationError(f"wheel symlink is forbidden: {info.filename!r}")
            member = PurePosixPath(info.filename)
            target = (destination / Path(*member.parts)).resolve()
            if os.path.commonpath((str(destination), str(target))) != str(destination):
                raise ValidationError(f"wheel member escapes overlay root: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            extracted += 1
    return extracted


def _validate_no_forbidden_archive_entries(
    names: Iterable[str], *, suffixes: Sequence[str], path_markers: Sequence[str]
) -> None:
    folded_suffixes = tuple(value.casefold() for value in suffixes)
    folded_markers = tuple(value.casefold() for value in path_markers)
    for name in names:
        folded = name.casefold()
        if folded.endswith(folded_suffixes):
            raise ValidationError(f"wheel contains a forbidden binary payload: {name}")
        if any(marker in folded for marker in folded_markers):
            raise ValidationError(f"wheel contains a forbidden library path: {name}")


def _single_member(names: Sequence[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValidationError(f"wheel must contain exactly one {suffix} member")
    return matches[0]


def _parse_message(data: bytes, *, label: str):
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(data)
    except Exception as exc:  # email parsers may raise several malformed-input errors
        raise ValidationError(f"wheel {label} is malformed") from exc
    if message.defects:
        raise ValidationError(f"wheel {label} contains parser defects")
    return message


def _static_runtime_version(source: bytes) -> dict[str, str]:
    try:
        tree = ast.parse(source.decode("utf-8"), filename="numpy/version.py")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValidationError("numpy/version.py is not valid UTF-8 Python") from exc
    values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        if value_node is None:
            continue
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            value = value_node.value
        elif isinstance(value_node, ast.Name) and value_node.id in values:
            value = values[value_node.id]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
    required = {key: values.get(key) for key in ("version", "__version__", "full_version")}
    if not all(isinstance(value, str) and value for value in required.values()):
        raise ValidationError("numpy/version.py does not expose a static complete version")
    return {key: str(value) for key, value in required.items()}


def _validate_static_runtime_version(
    source: bytes, *, expected_version: str
) -> dict[str, str]:
    runtime_versions = _static_runtime_version(source)
    if set(runtime_versions.values()) != {expected_version}:
        raise ValidationError("wheel static runtime version differs from METADATA")
    return runtime_versions


def _validate_init_version_binding(source: bytes) -> None:
    try:
        tree = ast.parse(source.decode("utf-8"), filename="numpy/__init__.py")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValidationError("numpy/__init__.py is not valid UTF-8 Python") from exc
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "version"
            and any(alias.name == "__version__" for alias in node.names)
        ):
            return
    raise ValidationError("numpy.__version__ is not statically bound to numpy.version")


def _dict_entry(node: ast.AST, key: str, *, label: str) -> ast.AST:
    if not isinstance(node, ast.Dict):
        raise ValidationError(f"{label} is not a static dictionary")
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if isinstance(key_node, ast.Constant) and key_node.value == key:
            return value_node
    raise ValidationError(f"{label} has no {key!r} entry")


def _validate_static_build_dependencies(source: bytes) -> None:
    try:
        tree = ast.parse(source.decode("utf-8"), filename="numpy/__config__.py")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValidationError("numpy/__config__.py is not valid UTF-8 Python") from exc
    config_node: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CONFIG" for target in node.targets
        ):
            config_node = node.value
            break
    if isinstance(config_node, ast.Call) and config_node.args:
        config_node = config_node.args[0]
    if config_node is None:
        raise ValidationError("numpy/__config__.py has no static CONFIG mapping")
    dependencies = _dict_entry(config_node, "Build Dependencies", label="CONFIG")
    for dependency in ("blas", "lapack"):
        entry = _dict_entry(dependencies, dependency, label="CONFIG Build Dependencies")
        name_node = _dict_entry(entry, "name", label=f"CONFIG {dependency}")
        if not isinstance(name_node, ast.Constant) or name_node.value != "none":
            raise ValidationError(f"numpy/__config__.py does not lock {dependency} to none")


def _rva_to_offset(rva: int, sections: Sequence[tuple[int, int, int, int]]) -> int:
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            relative = rva - virtual_address
            if relative < raw_size:
                return raw_offset + relative
            break
    raise ValidationError("PE import table points outside file-backed sections")


def _read_ascii_name(data: bytes, offset: int, *, label: str) -> str:
    if not 0 <= offset < len(data):
        raise ValidationError(f"{label} PE import name is outside the file")
    end = data.find(b"\0", offset, min(len(data), offset + 512))
    if end < 0:
        raise ValidationError(f"{label} PE import name is unterminated")
    try:
        return data[offset:end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} PE import name is not ASCII") from exc


def _pe_imports(data: bytes, *, label: str) -> list[str]:
    if len(data) > MAX_PYD_BYTES:
        raise ValidationError(f"{label} exceeds the bounded PE size")
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValidationError(f"{label} is not a PE binary")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValidationError(f"{label} has an invalid PE header")

    coff_offset = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff_offset + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    if section_count == 0 or section_count > 96:
        raise ValidationError(f"{label} has an invalid PE section count")
    optional_offset = coff_offset + 20
    if optional_offset + optional_size > len(data):
        raise ValidationError(f"{label} has a truncated PE optional header")
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    if magic == 0x20B:
        directory_count_offset = optional_offset + 108
        directory_offset = optional_offset + 112
        image_base = struct.unpack_from("<Q", data, optional_offset + 24)[0]
    elif magic == 0x10B:
        directory_count_offset = optional_offset + 92
        directory_offset = optional_offset + 96
        image_base = struct.unpack_from("<I", data, optional_offset + 28)[0]
    else:
        raise ValidationError(f"{label} uses an unsupported PE format")
    if directory_count_offset + 4 > optional_offset + optional_size:
        raise ValidationError(f"{label} has no bounded PE data-directory count")
    directory_count = struct.unpack_from("<I", data, directory_count_offset)[0]

    section_offset = optional_offset + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        entry = section_offset + index * 40
        if entry + 40 > len(data):
            raise ValidationError(f"{label} has a truncated PE section table")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, entry + 8
        )
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    imports: set[str] = set()

    def directory(index: int) -> tuple[int, int]:
        if directory_count <= index or directory_offset + (index + 1) * 8 > optional_offset + optional_size:
            return (0, 0)
        return struct.unpack_from("<II", data, directory_offset + index * 8)

    import_rva, import_size = directory(1)
    if import_rva and import_size:
        descriptor = _rva_to_offset(import_rva, sections)
        limit = min(len(data), descriptor + import_size)
        count = 0
        while descriptor + 20 <= limit:
            fields = struct.unpack_from("<IIIII", data, descriptor)
            if fields == (0, 0, 0, 0, 0):
                break
            name_offset = _rva_to_offset(fields[3], sections)
            imports.add(_read_ascii_name(data, name_offset, label=label))
            descriptor += 20
            count += 1
            if count > 4096:
                raise ValidationError(f"{label} has too many PE imports")
        else:
            raise ValidationError(f"{label} PE import table is not terminated")

    delay_rva, delay_size = directory(13)
    if delay_rva and delay_size:
        descriptor = _rva_to_offset(delay_rva, sections)
        limit = min(len(data), descriptor + delay_size)
        count = 0
        while descriptor + 32 <= limit:
            fields = struct.unpack_from("<IIIIIIII", data, descriptor)
            if fields == (0, 0, 0, 0, 0, 0, 0, 0):
                break
            attributes, name_pointer = fields[0], fields[1]
            name_rva = name_pointer if attributes & 1 else name_pointer - image_base
            name_offset = _rva_to_offset(name_rva, sections)
            imports.add(_read_ascii_name(data, name_offset, label=label))
            descriptor += 32
            count += 1
            if count > 4096:
                raise ValidationError(f"{label} has too many delay-loaded PE imports")
        else:
            raise ValidationError(f"{label} PE delay-import table is not terminated")

    return sorted(imports, key=str.casefold)


def _validate_pe_import_policy(
    imports: Iterable[str], *, forbidden_markers: Sequence[str], allowed_imports: Sequence[str]
) -> None:
    allowed = {item.casefold() for item in allowed_imports}
    markers = tuple(item.casefold() for item in forbidden_markers)
    for imported in imports:
        folded = imported.casefold()
        if any(marker in folded for marker in markers):
            raise ValidationError(f"forbidden numerical runtime PE import: {imported}")
        if folded not in allowed:
            raise ValidationError(f"unexpected NumPy PE import: {imported}")


def _validate_record(archive: zipfile.ZipFile, names: Sequence[str], record_name: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ValidationError("wheel RECORD is malformed") from exc
    if any(len(row) != 3 for row in rows):
        raise ValidationError("wheel RECORD rows must contain exactly three fields")
    records: dict[str, tuple[str, str]] = {}
    for name, digest, size in rows:
        if name in records:
            raise ValidationError(f"wheel RECORD repeats {name}")
        records[name] = (digest, size)
    if set(records) != set(names):
        raise ValidationError("wheel RECORD members differ from archive members")
    for name in names:
        digest, size = records[name]
        if name == record_name:
            if digest or size:
                raise ValidationError("wheel RECORD must not hash itself")
            continue
        data = archive.read(name)
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        if digest != f"sha256={encoded}" or size != str(len(data)):
            raise ValidationError(f"wheel RECORD does not attest {name}")


def validate(lock_path: Path = DEFAULT_LOCK_PATH, wheel_path: Path | None = None) -> dict[str, Any]:
    lock = _load_lock(lock_path.resolve())
    artifact = _validate_artifact_lock(lock)
    validation = lock["validation"]
    assert isinstance(validation, dict)

    if wheel_path is None:
        wheel_path = REPO_ROOT / str(artifact["relativePath"])
    wheel_path = wheel_path.resolve()
    if not wheel_path.is_file():
        raise ValidationError(f"locked NumPy wheel is missing: {wheel_path}")
    if wheel_path.name != artifact["fileName"]:
        raise ValidationError("NumPy wheel filename differs from the lock")
    if wheel_path.stat().st_size != artifact["length"]:
        raise ValidationError("NumPy wheel length differs from the lock")
    if _sha256_file(wheel_path) != artifact["sha256"]:
        raise ValidationError("NumPy wheel SHA-256 differs from the lock")

    try:
        archive = zipfile.ZipFile(wheel_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValidationError("locked NumPy wheel is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        _validate_member_names(names)
        if len(infos) != artifact["entryCount"]:
            raise ValidationError("NumPy wheel entry count differs from the lock")
        if sum(entry.file_size for entry in infos) != artifact["uncompressedBytes"]:
            raise ValidationError("NumPy wheel uncompressed size differs from the lock")
        if sum(entry.compress_size for entry in infos) != artifact["compressedPayloadBytes"]:
            raise ValidationError("NumPy wheel compressed payload differs from the lock")

        _validate_no_forbidden_archive_entries(
            names,
            suffixes=_require_string_list(
                validation, "forbiddenArchiveSuffixes", label="validation"
            ),
            path_markers=_require_string_list(
                validation, "forbiddenArchivePathMarkers", label="validation"
            ),
        )

        metadata_name = _single_member(names, ".dist-info/METADATA")
        wheel_metadata_name = _single_member(names, ".dist-info/WHEEL")
        record_name = _single_member(names, ".dist-info/RECORD")
        expected_dist_info = f"numpy-{artifact['version']}.dist-info/"
        if not all(
            name.startswith(expected_dist_info)
            for name in (metadata_name, wheel_metadata_name, record_name)
        ):
            raise ValidationError("wheel dist-info directory differs from the locked version")

        metadata = _parse_message(archive.read(metadata_name), label="METADATA")
        if metadata.get("Name") != validation["metadataName"]:
            raise ValidationError("wheel METADATA name differs from the lock")
        if metadata.get("Version") != validation["metadataVersion"]:
            raise ValidationError("wheel METADATA version differs from the lock")
        if metadata.get("License-Expression") != validation["licenseExpression"]:
            raise ValidationError("wheel METADATA license expression differs from the lock")

        wheel_metadata = _parse_message(archive.read(wheel_metadata_name), label="WHEEL")
        if wheel_metadata.get_all("Tag", []) != [artifact["wheelTag"]]:
            raise ValidationError("wheel compatibility tag differs from the lock")
        root_is_pure = str(wheel_metadata.get("Root-Is-Purelib", "")).casefold() == "true"
        if root_is_pure is not validation["rootIsPurelib"]:
            raise ValidationError("wheel Root-Is-Purelib differs from the lock")

        _validate_static_runtime_version(
            archive.read("numpy/version.py"), expected_version=artifact["version"]
        )
        _validate_init_version_binding(archive.read("numpy/__init__.py"))
        _validate_static_build_dependencies(archive.read("numpy/__config__.py"))

        expected_licenses = set(
            _require_string_list(validation, "licenseFiles", label="validation")
        )
        actual_licenses = {name for name in names if ".dist-info/licenses/" in name}
        if actual_licenses != expected_licenses:
            raise ValidationError("wheel license members differ from the lock")

        pyd_infos = [entry for entry in infos if entry.filename.casefold().endswith(".pyd")]
        if len(pyd_infos) != artifact["pydCount"]:
            raise ValidationError("wheel PYD count differs from the lock")
        if sum(entry.file_size for entry in pyd_infos) != artifact["pydBytes"]:
            raise ValidationError("wheel PYD bytes differ from the lock")
        forbidden_markers = _require_string_list(
            validation, "forbiddenPeImportMarkers", label="validation"
        )
        allowed_imports = _require_string_list(
            validation, "allowedPeImports", label="validation"
        )
        aggregate_imports: set[str] = set()
        for entry in pyd_infos:
            imports = _pe_imports(archive.read(entry), label=entry.filename)
            _validate_pe_import_policy(
                imports,
                forbidden_markers=forbidden_markers,
                allowed_imports=allowed_imports,
            )
            aggregate_imports.update(imports)
        if {item.casefold() for item in aggregate_imports} != {
            item.casefold() for item in allowed_imports
        }:
            raise ValidationError("aggregate NumPy PE imports differ from the lock")

        _validate_record(archive, names, record_name)

    return {
        "ok": True,
        "contract": CONTRACT,
        "wheel": artifact["fileName"],
        "version": artifact["version"],
        "length": artifact["length"],
        "sha256": artifact["sha256"],
        "pydCount": artifact["pydCount"],
        "peImports": sorted(aggregate_imports, key=str.casefold),
        "blas": "none",
        "lapack": "none",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = validate(args.lock, args.wheel)
        if args.extract_to is not None:
            wheel_path = args.wheel
            if wheel_path is None:
                lock = _load_lock(args.lock.resolve())
                wheel_path = REPO_ROOT / str(lock["artifact"]["relativePath"])
            summary["extractedFiles"] = extract_validated_wheel(
                wheel_path,
                args.extract_to,
            )
    except (OSError, ValidationError, zipfile.BadZipFile) as exc:
        print(f"NumPy no-BLAS wheel validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
