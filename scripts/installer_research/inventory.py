from __future__ import annotations

import base64
import binascii
import configparser
import copy
import csv
import fnmatch
import hashlib
import json
import os
import re
import stat
import struct
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Mapping, Sequence


INVENTORY_CONTRACT = "InstallerResearchInventoryV1"
INVENTORY_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
DIST_INFO_RE = re.compile(r"^(?P<name>.+)-(?P<version>[0-9][^-]*)\.dist-info$", re.IGNORECASE)
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$")
READ_CHUNK_BYTES = 1024 * 1024
CONSOLE_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WINDOWS_RECORD_LAUNCHER_RE = re.compile(
    r"^\.\./\.\./Scripts/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.exe$",
    re.IGNORECASE,
)
RECORD_SHA256_RE = re.compile(r"^sha256=[A-Za-z0-9_-]{43}$")


class InventoryError(ValueError):
    """Raised when an artifact cannot produce trustworthy size evidence."""


def validate_run_id(value: str, *, field: str = "run_id") -> str:
    if not isinstance(value, str):
        raise InventoryError(f"{field} must be a canonical non-nil RFC 4122 UUID.")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise InventoryError(
            f"{field} must be a canonical non-nil RFC 4122 UUID."
        ) from exc
    if parsed.int == 0 or str(parsed) != value:
        raise InventoryError(f"{field} must be a canonical non-nil RFC 4122 UUID.")
    return str(parsed)


def validate_source_commit(value: str, *, field: str = "source_commit") -> str:
    if not isinstance(value, str) or not SOURCE_COMMIT_RE.fullmatch(value):
        raise InventoryError(f"{field} must be a full lowercase Git object id.")
    return value


def validate_replica_id(value: str, *, field: str = "replica_id") -> str:
    if not isinstance(value, str) or not REPLICA_ID_RE.fullmatch(value):
        raise InventoryError(
            f"{field} must match {REPLICA_ID_RE.pattern} and contain no path data."
        )
    return value


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_root_identity_sha256(path: Path) -> str:
    """Hash a resolved build-root identity without persisting its local path."""
    normalized = os.path.normcase(str(path.expanduser().resolve())).replace("\\", "/")
    normalized = normalized.rstrip("/")
    return _sha256_bytes(normalized.encode("utf-8"))


def _read_stable_bytes(path: Path) -> bytes:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise InventoryError(f"File changed while it was inventoried: {path.name}")
    return data


def _hash_stable_file(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or length != after.st_size
    ):
        raise InventoryError(f"File changed while it was inventoried: {path.name}")
    return length, digest.hexdigest()


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _ensure_plain_path(path: Path, *, description: str) -> None:
    info = path.lstat()
    if path.is_symlink() or _is_reparse_point(info):
        raise InventoryError(f"{description} must not be a symlink or reparse point: {path}")


def _absolute_path_entry(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _ensure_plain_descendant_chain(root: Path, path: Path, *, description: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    current = root
    for part in relative.parts:
        current = current / part
        try:
            _ensure_plain_path(current, description=description)
        except OSError as exc:
            raise InventoryError(f"{description} does not exist: {current}") from exc


def _resolve_plain_path(
    path: Path,
    *,
    description: str,
    kind: str,
) -> tuple[Path, Path]:
    """Validate the caller-visible entry before resolving and then its target."""
    entry = _absolute_path_entry(path)
    try:
        entry_info = entry.lstat()
    except OSError as exc:
        raise InventoryError(f"{description} does not exist: {entry}") from exc
    _ensure_plain_path(entry, description=description)
    if kind == "file" and not stat.S_ISREG(entry_info.st_mode):
        raise InventoryError(f"{description} is not a regular file: {entry}")
    if kind == "directory" and not stat.S_ISDIR(entry_info.st_mode):
        raise InventoryError(f"{description} is not a directory: {entry}")
    if kind not in {"file", "directory"}:
        raise AssertionError(f"Unsupported plain-path kind: {kind}")
    try:
        resolved = entry.resolve(strict=True)
    except OSError as exc:
        raise InventoryError(f"{description} cannot be resolved: {entry}") from exc
    _ensure_plain_path(resolved, description=f"Resolved {description.lower()}")
    resolved_info = resolved.lstat()
    if kind == "file" and not stat.S_ISREG(resolved_info.st_mode):
        raise InventoryError(f"Resolved {description.lower()} is not a regular file.")
    if kind == "directory" and not stat.S_ISDIR(resolved_info.st_mode):
        raise InventoryError(f"Resolved {description.lower()} is not a directory.")
    return entry, resolved


def _iter_plain_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise InventoryError(f"Staged root is not a directory: {root}")
    _ensure_plain_path(root, description="Staged root")

    result: list[tuple[str, Path]] = []
    seen_casefolded: dict[str, str] = {}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directory_names):
            directory = current_path / name
            _ensure_plain_path(directory, description="Payload directory")
        directory_names.sort(key=lambda item: (item.casefold(), item))
        file_names.sort(key=lambda item: (item.casefold(), item))
        for name in file_names:
            path = current_path / name
            _ensure_plain_path(path, description="Payload file")
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise InventoryError(f"Payload entry is not a regular file: {path}")
            relative = path.relative_to(root).as_posix()
            if not relative or relative.startswith("../") or "\\" in relative:
                raise InventoryError(f"Invalid payload-relative path: {relative!r}")
            folded = relative.casefold()
            prior = seen_casefolded.get(folded)
            if prior is not None and prior != relative:
                raise InventoryError(
                    f"Case-insensitive payload path collision: {prior!r} and {relative!r}"
                )
            seen_casefolded[folded] = relative
            result.append((relative, path))
    return sorted(result, key=lambda item: item[0].encode("utf-8"))


def _update_length_prefixed(digest: Any, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _tree_hash(
    entries: Sequence[Mapping[str, Any]],
    *,
    length_key: str,
    sha_key: str,
) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item["path"].encode("utf-8")):
        _update_length_prefixed(digest, entry["path"].encode("utf-8"))
        digest.update(struct.pack(">Q", int(entry[length_key])))
        sha = str(entry[sha_key])
        if not SHA256_RE.fullmatch(sha):
            raise InventoryError(f"Invalid SHA-256 in tree entry: {entry['path']}")
        digest.update(bytes.fromhex(sha))
    return digest.hexdigest()


def _file_list_hash(paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.encode("utf-8")):
        _update_length_prefixed(digest, path.encode("utf-8"))
    return digest.hexdigest()


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError(f"{label} must be a JSON object.")
    return value


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InventoryError(f"{label} must be a non-empty string.")
    return value


def _require_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise InventoryError(f"{label} must be a boolean.")
    return value


def _require_nonnegative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InventoryError(f"{label} must be a non-negative integer.")
    return value


def _normalize_sidecar_build_metadata(raw: bytes) -> bytes:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError("sidecar-build-metadata.json is not valid UTF-8 JSON.") from exc
    metadata = _require_mapping(payload, label="sidecar build metadata")
    if metadata.get("apiVersion") != "1":
        raise InventoryError("Unsupported sidecar build metadata apiVersion.")

    generated_at = _require_string(metadata.get("generatedAt"), label="generatedAt")
    if not ISO_UTC_RE.fullmatch(generated_at):
        raise InventoryError("sidecar build metadata generatedAt is not an ISO timestamp.")
    _require_string(metadata.get("sidecarDir"), label="sidecarDir")
    _require_string(metadata.get("sidecarExe"), label="sidecarExe")
    copied_to = metadata.get("copiedToTauriRelease")
    if copied_to is not None and not isinstance(copied_to, str):
        raise InventoryError("copiedToTauriRelease must be a string or null.")
    _require_bool(metadata.get("targetCurrent"), label="targetCurrent")

    sidecar = _require_mapping(metadata.get("sidecar"), label="sidecar")
    sidecar_sha = _require_string(sidecar.get("sha256"), label="sidecar.sha256")
    if not SHA256_RE.fullmatch(sidecar_sha):
        raise InventoryError("sidecar.sha256 is not a lowercase SHA-256.")
    _require_nonnegative_int(sidecar.get("length"), label="sidecar.length")

    cache = _require_mapping(metadata.get("cache"), label="cache")
    cache_enabled = _require_bool(cache.get("enabled"), label="cache.enabled")
    cache_hit = _require_bool(cache.get("hit"), label="cache.hit")
    cache_key = cache.get("key")
    if cache_enabled:
        cache_key = _require_string(cache_key, label="cache.key")
        if not SHA256_RE.fullmatch(cache_key):
            raise InventoryError("cache.key is not a lowercase SHA-256.")
    elif cache_key != "":
        raise InventoryError("cache.key must be empty when cache.enabled is false.")
    if cache_hit and not cache_enabled:
        raise InventoryError("cache.hit cannot be true when cache.enabled is false.")
    runtime = _require_mapping(metadata.get("runtimeLayer"), label="runtimeLayer")
    _require_bool(runtime.get("cacheHit"), label="runtimeLayer.cacheHit")

    phases = metadata.get("phases")
    if not isinstance(phases, list):
        raise InventoryError("sidecar build metadata phases must be an array.")
    for index, phase_value in enumerate(phases):
        phase = _require_mapping(phase_value, label=f"phases[{index}]")
        _require_string(phase.get("label"), label=f"phases[{index}].label")
        _require_bool(phase.get("ok"), label=f"phases[{index}].ok")
        duration = phase.get("durationMs")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            raise InventoryError(f"phases[{index}].durationMs must be non-negative.")
    total_duration = metadata.get("totalDurationMs")
    if (
        isinstance(total_duration, bool)
        or not isinstance(total_duration, (int, float))
        or total_duration < 0
    ):
        raise InventoryError("totalDurationMs must be non-negative.")

    copied_media = metadata.get("mediaToolsCopied")
    if not isinstance(copied_media, list) or not all(
        isinstance(item, str) for item in copied_media
    ):
        raise InventoryError("mediaToolsCopied must be an array of strings.")
    prepared_media = metadata.get("preparedMediaTools")
    if prepared_media is not None and not isinstance(
        prepared_media, (dict, list, str, int, float, bool)
    ):
        raise InventoryError("preparedMediaTools has an unsupported JSON type.")

    normalized = copy.deepcopy(metadata)
    normalized["generatedAt"] = "1970-01-01T00:00:00Z"
    normalized["sidecarDir"] = "<normalized-sidecar-dir>"
    normalized["sidecarExe"] = "<normalized-sidecar-exe>"
    normalized["copiedToTauriRelease"] = (
        "<normalized-tauri-release-dir>" if copied_to is not None else None
    )
    normalized["targetCurrent"] = False
    normalized["cache"]["hit"] = False
    normalized["runtimeLayer"]["cacheHit"] = False
    normalized["preparedMediaTools"] = None
    normalized["mediaToolsCopied"] = []
    normalized["totalDurationMs"] = 0

    for phase in normalized["phases"]:
        phase["durationMs"] = 0
        if "overlappedWallDurationMs" in phase:
            value = phase["overlappedWallDurationMs"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise InventoryError("overlappedWallDurationMs must be non-negative.")
            phase["overlappedWallDurationMs"] = 0
        if "cacheHitBeforePrestage" in phase:
            _require_bool(
                phase["cacheHitBeforePrestage"],
                label="phase.cacheHitBeforePrestage",
            )
            phase["cacheHitBeforePrestage"] = False

    audio_copy = normalized.get("rustAudioSidecarCopied")
    if audio_copy is not None:
        audio_copy = _require_mapping(audio_copy, label="rustAudioSidecarCopied")
        for key in ("targetDir", "targetExe", "metadataPath"):
            if key in audio_copy:
                _require_string(audio_copy[key], label=f"rustAudioSidecarCopied.{key}")
                audio_copy[key] = f"<normalized-{key}>"
        if "cacheHit" in audio_copy:
            _require_bool(audio_copy["cacheHit"], label="rustAudioSidecarCopied.cacheHit")
            audio_copy["cacheHit"] = False

    diarization_copy = normalized.get("rustDiarizationSidecarCopied")
    if diarization_copy is not None:
        diarization_copy = _require_mapping(
            diarization_copy, label="rustDiarizationSidecarCopied"
        )
        for key in (
            "targetDir",
            "targetExe",
            "targetManifest",
            "cacheRoot",
            "archiveCacheRoot",
            "cargoTargetDir",
            "buildMetadataPath",
        ):
            if key in diarization_copy:
                _require_string(
                    diarization_copy[key],
                    label=f"rustDiarizationSidecarCopied.{key}",
                )
                diarization_copy[key] = f"<normalized-{key}>"
        if "cacheHit" in diarization_copy:
            _require_bool(
                diarization_copy["cacheHit"],
                label="rustDiarizationSidecarCopied.cacheHit",
            )
            diarization_copy["cacheHit"] = False
        archive = diarization_copy.get("archive")
        if isinstance(archive, dict) and "cacheHitForCurrentBuild" in archive:
            hit = archive["cacheHitForCurrentBuild"]
            if hit is not None and not isinstance(hit, bool):
                raise InventoryError(
                    "rustDiarizationSidecarCopied.archive.cacheHitForCurrentBuild "
                    "must be a boolean or null."
                )
            archive["cacheHitForCurrentBuild"] = None
        sync = diarization_copy.get("sync")
        if isinstance(sync, dict):
            for key in ("copied", "skipped", "removed"):
                if key in sync:
                    _require_nonnegative_int(
                        sync[key], label=f"rustDiarizationSidecarCopied.sync.{key}"
                    )
                    sync[key] = 0

    return (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _normalize_tauri_bundle_type(
    raw: bytes,
    marker: Mapping[str, Any],
) -> bytes:
    prefix = str(marker["prefix"]).encode("ascii")
    allowed_values = tuple(
        str(value).encode("ascii") for value in marker["allowedValues"]
    )
    normalized_value = str(marker["normalizedValue"]).encode("ascii")
    marker_count = raw.count(prefix)
    if marker_count == 0:
        if raw.startswith(b"MZ"):
            raise InventoryError("Tauri desktop executable has no bundle-type marker.")
        return raw
    if marker_count != 1:
        raise InventoryError(
            "Tauri desktop executable has multiple bundle-type markers."
        )
    marker_start = raw.index(prefix) + len(prefix)
    marker_end = marker_start + len(normalized_value)
    value = raw[marker_start:marker_end]
    if value not in allowed_values:
        raise InventoryError("Tauri desktop executable has an unsupported bundle type.")
    return raw[:marker_start] + normalized_value + raw[marker_end:]


def load_component_map(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise InventoryError(f"Component map does not exist: {path}")
    _ensure_plain_path(path, description="Component map")
    raw = _read_stable_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError("Component map is not valid UTF-8 JSON.") from exc
    component_map = _require_mapping(value, label="component map")
    if component_map.get("schemaVersion") != 1:
        raise InventoryError("Unsupported component-map schemaVersion.")
    _require_string(component_map.get("mapId"), label="component map mapId")
    _require_string(
        component_map.get("pyinstallerVersion"),
        label="component map pyinstallerVersion",
    )
    if component_map.get("classificationPolicy") != "all-explicit-matches-or-fallback":
        raise InventoryError("Unsupported component-map classification policy.")

    components = component_map.get("components")
    if not isinstance(components, list) or not components:
        raise InventoryError("Component map must contain a non-empty components array.")
    component_ids: list[str] = []
    for index, component_value in enumerate(components):
        component = _require_mapping(component_value, label=f"components[{index}]")
        component_id = _require_string(component.get("id"), label=f"components[{index}].id")
        if component_id in component_ids:
            raise InventoryError(f"Duplicate component id: {component_id}")
        component_ids.append(component_id)
        for key in ("exactPaths", "fileNames", "pathPrefixes", "pathGlobs"):
            patterns = component.get(key, [])
            if not isinstance(patterns, list) or not all(
                isinstance(item, str) and item for item in patterns
            ):
                raise InventoryError(f"{component_id}.{key} must be an array of strings.")
        if "backendExecutable" in component:
            _require_bool(
                component["backendExecutable"],
                label=f"{component_id}.backendExecutable",
            )

    fallback = _require_string(
        component_map.get("fallbackComponent"), label="fallbackComponent"
    )
    if fallback not in component_ids:
        raise InventoryError("fallbackComponent does not name a declared component.")

    pyz_rules = component_map.get("pyzPrefixComponents")
    if not isinstance(pyz_rules, list):
        raise InventoryError("pyzPrefixComponents must be an array.")
    seen_prefixes: set[str] = set()
    for index, rule_value in enumerate(pyz_rules):
        rule = _require_mapping(rule_value, label=f"pyzPrefixComponents[{index}]")
        prefix = _require_string(rule.get("prefix"), label=f"pyzPrefixComponents[{index}].prefix")
        component = _require_string(
            rule.get("component"), label=f"pyzPrefixComponents[{index}].component"
        )
        if prefix in seen_prefixes:
            raise InventoryError(f"Duplicate PYZ prefix: {prefix}")
        if component not in component_ids:
            raise InventoryError(f"Unknown PYZ component: {component}")
        seen_prefixes.add(prefix)

    allowed = component_map.get("allowedDistributions")
    if not isinstance(allowed, list) or not all(
        isinstance(item, str) and item == item.casefold() for item in allowed
    ):
        raise InventoryError("allowedDistributions must contain lowercase names.")
    if len(set(allowed)) != len(allowed):
        raise InventoryError("allowedDistributions contains duplicates.")

    normalization = _require_mapping(
        component_map.get("semanticNormalization"), label="semanticNormalization"
    )
    if (
        normalization.get("contract")
        != "scriber-installer-semantic-normalization-v2"
    ):
        raise InventoryError("Unsupported semantic-normalization contract.")
    names = normalization.get("sidecarMetadataFileNames")
    if names != ["sidecar-build-metadata.json"]:
        raise InventoryError("Unexpected sidecar metadata normalization scope.")
    tauri_marker = _require_mapping(
        normalization.get("tauriBundleTypeMarker"),
        label="tauriBundleTypeMarker",
    )
    if tauri_marker != {
        "desktopExecutablePath": "scriber-desktop.exe",
        "prefix": "__TAURI_BUNDLE_TYPE_VAR_",
        "allowedValues": ["UNK", "NSS"],
        "normalizedValue": "UNK",
    }:
        raise InventoryError("Unexpected Tauri bundle-type normalization scope.")

    return component_map, _sha256_bytes(raw)


def _component_matches(
    component: Mapping[str, Any],
    *,
    relative_path: str,
    backend_relative_path: str,
) -> bool:
    path = relative_path.casefold()
    name = PurePosixPath(path).name
    if component.get("backendExecutable") and path == backend_relative_path.casefold():
        return True
    if path in {str(item).casefold() for item in component.get("exactPaths", [])}:
        return True
    if name in {str(item).casefold() for item in component.get("fileNames", [])}:
        return True
    if any(path.startswith(str(prefix).casefold()) for prefix in component.get("pathPrefixes", [])):
        return True
    return any(
        fnmatch.fnmatchcase(path, str(pattern).casefold())
        for pattern in component.get("pathGlobs", [])
    )


def _classify_file(
    component_map: Mapping[str, Any],
    *,
    relative_path: str,
    backend_relative_path: str,
) -> str:
    fallback = str(component_map["fallbackComponent"])
    matches: list[str] = []
    for component in component_map["components"]:
        component_id = str(component["id"])
        if component_id == fallback:
            continue
        if _component_matches(
            component,
            relative_path=relative_path,
            backend_relative_path=backend_relative_path,
        ):
            matches.append(component_id)
    if len(matches) > 1:
        raise InventoryError(
            "multiplyAssignedObjects: "
            f"{relative_path!r} matches {', '.join(sorted(matches))}"
        )
    return matches[0] if matches else fallback


def _semantic_bytes_for_file(
    *,
    path: Path,
    relative_path: str,
    raw: bytes | None,
    component_map: Mapping[str, Any],
    bundled_relative_paths: frozenset[str],
) -> bytes | None:
    normalized_path = relative_path.casefold()
    if PurePosixPath(normalized_path).name == "sidecar-build-metadata.json":
        return _normalize_sidecar_build_metadata(
            raw if raw is not None else _read_stable_bytes(path)
        )
    relative = PurePosixPath(relative_path)
    if (
        relative.name.casefold() == "record"
        and DIST_INFO_RE.fullmatch(relative.parent.name) is not None
    ):
        record_bytes = raw if raw is not None else _read_stable_bytes(path)
        normalized_record = _normalize_unbundled_console_launcher_records(
            path=path,
            relative_path=relative_path,
            raw=record_bytes,
            bundled_relative_paths=bundled_relative_paths,
        )
        if normalized_record is not None:
            return normalized_record
    normalization = component_map["semanticNormalization"]
    tauri_marker = normalization["tauriBundleTypeMarker"]
    if normalized_path == str(tauri_marker["desktopExecutablePath"]).casefold():
        return _normalize_tauri_bundle_type(
            raw if raw is not None else _read_stable_bytes(path),
            tauri_marker,
        )
    return None


def _console_script_names(entry_points_path: Path) -> frozenset[str]:
    try:
        _ensure_plain_path(entry_points_path, description="entry_points.txt")
        text = _read_stable_bytes(entry_points_path).decode("utf-8")
        parser = configparser.ConfigParser(
            interpolation=None,
            strict=True,
            delimiters=("=",),
        )
        parser.optionxform = str
        parser.read_string(text)
    except (InventoryError, UnicodeDecodeError, configparser.Error, OSError):
        return frozenset()
    if not parser.has_section("console_scripts"):
        return frozenset()

    names: set[str] = set()
    for raw_name, raw_target in parser.items("console_scripts", raw=True):
        name = raw_name.strip()
        folded = name.casefold()
        if (
            not CONSOLE_SCRIPT_NAME_RE.fullmatch(name)
            or not raw_target.strip()
            or folded in names
        ):
            return frozenset()
        names.add(folded)
    return frozenset(names)


def _payload_relative_target(record_relative_path: str, recorded_path: str) -> str | None:
    parts = list(PurePosixPath(record_relative_path).parent.parts)
    for part in PurePosixPath(recorded_path).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return PurePosixPath(*parts).as_posix() if parts else None


def _is_canonical_record_sha256(value: str) -> bool:
    if RECORD_SHA256_RE.fullmatch(value) is None:
        return False
    encoded = value.removeprefix("sha256=")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=")
    except (binascii.Error, ValueError):
        return False
    return (
        len(decoded) == hashlib.sha256().digest_size
        and base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") == encoded
    )


def _normalize_unbundled_console_launcher_records(
    *,
    path: Path,
    relative_path: str,
    raw: bytes,
    bundled_relative_paths: frozenset[str],
) -> bytes | None:
    relative = PurePosixPath(relative_path)
    if (
        relative.name.casefold() != "record"
        or DIST_INFO_RE.fullmatch(relative.parent.name) is None
    ):
        return None

    entry_points_relative = (relative.parent / "entry_points.txt").as_posix()
    if entry_points_relative.casefold() not in bundled_relative_paths:
        return None
    console_scripts = _console_script_names(path.with_name("entry_points.txt"))
    if not console_scripts:
        return None

    retained: list[bytes] = []
    ignored_targets: set[str] = set()
    ignored_count = 0
    for raw_line in raw.splitlines(keepends=True):
        try:
            decoded_line = raw_line.decode("utf-8")
            rows = list(csv.reader([decoded_line], strict=True))
        except (UnicodeDecodeError, csv.Error):
            return None
        if len(rows) != 1 or len(rows[0]) != 3:
            return None
        recorded_path, recorded_digest, recorded_length = rows[0]
        launcher = WINDOWS_RECORD_LAUNCHER_RE.fullmatch(recorded_path)
        if launcher is None or launcher.group("name").casefold() not in console_scripts:
            retained.append(raw_line)
            continue
        if (
            not _is_canonical_record_sha256(recorded_digest)
            or not recorded_length.isascii()
            or not recorded_length.isdecimal()
        ):
            retained.append(raw_line)
            continue
        target = _payload_relative_target(relative_path, recorded_path)
        if target is None or target.casefold() in bundled_relative_paths:
            retained.append(raw_line)
            continue
        folded_target = target.casefold()
        if folded_target in ignored_targets:
            return None
        ignored_targets.add(folded_target)
        ignored_count += 1

    if ignored_count == 0:
        return None
    return b"".join(retained)


def _distribution_name(relative_path: str) -> str | None:
    for part in PurePosixPath(relative_path).parts:
        match = DIST_INFO_RE.fullmatch(part)
        if match:
            return match.group("name").replace("_", "-").casefold()
    return None


def _build_tree_inventory(
    root: Path,
    *,
    component_map: Mapping[str, Any],
    backend_relative_path: str,
    backend_attribution: Mapping[str, Any],
) -> dict[str, Any]:
    component_ids = [str(item["id"]) for item in component_map["components"]]
    component_contributors: dict[str, list[dict[str, Any]]] = {
        component_id: [] for component_id in component_ids
    }
    files: list[dict[str, Any]] = []
    duplicate_candidates: dict[tuple[int, str], list[str]] = defaultdict(list)
    distributions: set[str] = set()

    plain_files = tuple(_iter_plain_files(root))
    bundled_relative_paths = frozenset(
        relative_path.casefold() for relative_path, _path in plain_files
    )
    for relative_path, path in plain_files:
        raw: bytes | None = None
        relative = PurePosixPath(relative_path)
        if (
            relative.name.casefold() == "sidecar-build-metadata.json"
            or (
                relative.name.casefold() == "record"
                and DIST_INFO_RE.fullmatch(relative.parent.name) is not None
            )
        ):
            raw = _read_stable_bytes(path)
            length = len(raw)
            sha256 = _sha256_bytes(raw)
        else:
            length, sha256 = _hash_stable_file(path)
        semantic_bytes = _semantic_bytes_for_file(
            path=path,
            relative_path=relative_path,
            raw=raw,
            component_map=component_map,
            bundled_relative_paths=bundled_relative_paths,
        )
        semantic_length = length if semantic_bytes is None else len(semantic_bytes)
        semantic_sha256 = sha256 if semantic_bytes is None else _sha256_bytes(semantic_bytes)
        if relative_path.casefold() == backend_relative_path.casefold():
            if (
                length != backend_attribution.get("length")
                or sha256 != backend_attribution.get("sha256")
            ):
                raise InventoryError(
                    "Backend executable identity changed between archive and tree inspection."
                )
            allocations = backend_attribution.get("componentAllocations")
            if not isinstance(allocations, dict):
                raise InventoryError("Backend executable has no component allocation.")
            component_id: str | None = None
            for allocated_component, allocated_bytes in sorted(allocations.items()):
                if allocated_component not in component_contributors:
                    raise InventoryError(
                        f"Backend allocation names unknown component: {allocated_component}"
                    )
                if (
                    isinstance(allocated_bytes, bool)
                    or not isinstance(allocated_bytes, int)
                    or allocated_bytes < 0
                ):
                    raise InventoryError("Backend component allocation is invalid.")
                component_contributors[allocated_component].append(
                    {
                        "id": f"{relative_path}::component:{allocated_component}",
                        "path": relative_path,
                        "rawBytes": allocated_bytes,
                        "kind": "partitioned-backend-executable",
                    }
                )
            entry_allocations: dict[str, int] | None = dict(allocations)
        else:
            component_id = _classify_file(
                component_map,
                relative_path=relative_path,
                backend_relative_path=backend_relative_path,
            )
            entry_allocations = None
            component_contributors[component_id].append(
                {
                    "id": relative_path,
                    "path": relative_path,
                    "rawBytes": length,
                    "kind": "file",
                }
            )
        entry = {
            "path": relative_path,
            "length": length,
            "sha256": sha256,
            "semanticLength": semantic_length,
            "semanticSha256": semantic_sha256,
            "component": component_id,
            "componentAllocations": entry_allocations,
        }
        files.append(entry)
        duplicate_candidates[(length, sha256)].append(relative_path)
        distribution = _distribution_name(relative_path)
        if distribution is not None:
            distributions.add(distribution)

    total_bytes = sum(int(item["length"]) for item in files)
    components: dict[str, dict[str, Any]] = {}
    for component_id in component_ids:
        contributors = component_contributors[component_id]
        components[component_id] = {
            "rawBytes": sum(int(item["rawBytes"]) for item in contributors),
            "fileCount": len({str(item["path"]) for item in contributors}),
            "allocationCount": len(contributors),
            "fileListSha256": _file_list_hash(
                str(item["path"]) for item in contributors
            ),
            "allocationListSha256": _file_list_hash(
                f"{item['id']}:{item['rawBytes']}" for item in contributors
            ),
        }
    component_total = sum(int(item["rawBytes"]) for item in components.values())
    if component_total != total_bytes:
        raise InventoryError("Physical component partition does not sum to payload bytes.")

    duplicates = [
        {"length": length, "sha256": sha256, "paths": sorted(paths)}
        for (length, sha256), paths in duplicate_candidates.items()
        if len(paths) > 1 and length > 0
    ]
    duplicates.sort(key=lambda item: (-item["length"], item["sha256"]))
    allowed_distributions = set(component_map["allowedDistributions"])
    unexpected_distributions = sorted(distributions - allowed_distributions)

    return {
        "totalBytes": total_bytes,
        "fileCount": len(files),
        "exactTreeSha256": _tree_hash(files, length_key="length", sha_key="sha256"),
        "semanticTreeSha256": _tree_hash(
            files,
            length_key="semanticLength",
            sha_key="semanticSha256",
        ),
        "fileListSha256": _file_list_hash(str(item["path"]) for item in files),
        "components": components,
        "componentBytesSum": component_total,
        "distributions": sorted(distributions),
        "unexpectedDistributions": unexpected_distributions,
        "duplicateGroups": duplicates,
        "files": files,
    }


def _hash_stream_region(handle: BinaryIO, *, offset: int, length: int) -> str:
    if offset < 0 or length < 0:
        raise InventoryError("Archive region has a negative offset or length.")
    handle.seek(offset)
    remaining = length
    digest = hashlib.sha256()
    while remaining:
        chunk = handle.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            raise InventoryError("Archive region ended before its declared length.")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _validate_nonoverlapping_regions(
    regions: Sequence[tuple[int, int, str]],
    *,
    container_length: int,
    label: str,
) -> None:
    prior_end = 0
    prior_name = ""
    for offset, length, name in sorted(regions, key=lambda item: (item[0], item[1], item[2])):
        if offset < 0 or length < 0 or offset + length > container_length:
            raise InventoryError(f"{label} entry is outside its container: {name}")
        if length == 0:
            continue
        if offset < prior_end:
            raise InventoryError(
                f"{label} entries overlap: {prior_name!r} and {name!r}"
            )
        prior_end = offset + length
        prior_name = name


def _pyz_component(
    module_name: str,
    *,
    rules: Sequence[Mapping[str, Any]],
    fallback: str,
) -> str:
    matches: list[tuple[str, str]] = []
    for rule in rules:
        prefix = str(rule["prefix"])
        if module_name == prefix or module_name.startswith(prefix + "."):
            matches.append((prefix, str(rule["component"])))
    if not matches:
        return fallback
    if len(matches) > 1:
        rendered = ", ".join(
            f"{prefix}->{component}" for prefix, component in sorted(matches)
        )
        raise InventoryError(
            f"multiplyAssignedPyzModules: {module_name!r} matches {rendered}"
        )
    return matches[0][1]


def inspect_pyinstaller_executable(
    backend_exe: Path,
    *,
    component_map: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        import PyInstaller
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise InventoryError("PyInstaller is required to inspect the backend executable.") from exc

    expected_version = str(component_map["pyinstallerVersion"])
    if PyInstaller.__version__ != expected_version:
        raise InventoryError(
            f"PyInstaller reader drift: expected {expected_version}, got {PyInstaller.__version__}."
        )
    exe_length, exe_sha256 = _hash_stable_file(backend_exe)
    try:
        archive = CArchiveReader(str(backend_exe))
    except Exception as exc:
        raise InventoryError("Backend executable is not a readable PyInstaller CArchive.") from exc

    archive_start = int(archive._start_offset)
    archive_end = int(archive._end_offset)
    if not (0 <= archive_start < archive_end <= exe_length):
        raise InventoryError("PyInstaller CArchive boundaries are outside the executable.")
    archive_length = archive_end - archive_start

    carchive_entries: list[dict[str, Any]] = []
    carchive_regions: list[tuple[int, int, str]] = []
    for name, value in archive.toc.items():
        if not isinstance(value, tuple) or len(value) != 5:
            raise InventoryError("Unexpected PyInstaller 6.20 CArchive TOC shape.")
        entry_offset, data_length, uncompressed_length, compression_flag, typecode = value
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value[:4]):
            raise InventoryError(f"Invalid CArchive numeric fields for entry: {name}")
        if not isinstance(typecode, str) or len(typecode) != 1:
            raise InventoryError(f"Invalid CArchive typecode for entry: {name}")
        if entry_offset < 0 or data_length < 0 or uncompressed_length < 0:
            raise InventoryError(f"Negative CArchive field for entry: {name}")
        carchive_regions.append((entry_offset, data_length, name))
        carchive_entries.append(
            {
                "name": name,
                "offset": entry_offset,
                "compressedBytes": data_length,
                "uncompressedBytes": uncompressed_length,
                "compressionFlag": compression_flag,
                "typecode": typecode,
            }
        )
    _validate_nonoverlapping_regions(
        carchive_regions,
        container_length=archive_length,
        label="CArchive",
    )
    pyz_entries = [item for item in carchive_entries if item["typecode"] == "z"]
    if len(pyz_entries) != 1 or pyz_entries[0]["name"] != "PYZ.pyz":
        raise InventoryError("Expected exactly one CArchive PYZ.pyz entry.")
    pyz_carchive_entry = pyz_entries[0]

    try:
        pyz_reader = archive.open_embedded_archive("PYZ.pyz")
    except Exception as exc:
        raise InventoryError("Embedded PYZ.pyz could not be opened.") from exc
    expected_pyz_start = archive_start + int(pyz_carchive_entry["offset"])
    if int(pyz_reader._start_offset) != expected_pyz_start:
        raise InventoryError("Embedded PYZ start offset does not match the CArchive TOC.")
    pyz_length = int(pyz_carchive_entry["compressedBytes"])

    pyz_regions: list[tuple[int, int, str]] = []
    module_entries: list[dict[str, Any]] = []
    root_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "compressedBytes": 0,
            "moduleCount": 0,
            "components": defaultdict(lambda: {"compressedBytes": 0, "moduleCount": 0}),
        }
    )
    with backend_exe.open("rb") as handle:
        for module_name, value in pyz_reader.toc.items():
            if not isinstance(value, tuple) or len(value) != 3:
                raise InventoryError("Unexpected PyInstaller 6.20 PYZ TOC shape.")
            typecode, offset, length = value
            if any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in (typecode, offset, length)
            ):
                raise InventoryError(f"Invalid PYZ fields for module: {module_name}")
            if offset < 0 or length < 0:
                raise InventoryError(f"Negative PYZ field for module: {module_name}")
            pyz_regions.append((offset, length, module_name))
            compressed_sha256 = _hash_stream_region(
                handle,
                offset=expected_pyz_start + offset,
                length=length,
            )
            try:
                decompressed = pyz_reader.extract(module_name, raw=True)
            except Exception as exc:
                raise InventoryError(
                    f"PYZ module could not be decompressed: {module_name}"
                ) from exc
            if decompressed is None:
                decompressed_bytes = b""
            elif isinstance(decompressed, bytes):
                decompressed_bytes = decompressed
            else:
                raise InventoryError(
                    f"PYZ raw extraction returned an unexpected type: {module_name}"
                )
            root = module_name.split(".", 1)[0]
            component = _pyz_component(
                module_name,
                rules=component_map["pyzPrefixComponents"],
                fallback="python-runtime-other",
            )
            root_total = root_totals[root]
            root_total["compressedBytes"] += length
            root_total["moduleCount"] += 1
            root_total["components"][component]["compressedBytes"] += length
            root_total["components"][component]["moduleCount"] += 1
            module_entries.append(
                {
                    "name": module_name,
                    "root": root,
                    "component": component,
                    "typecode": typecode,
                    "offset": offset,
                    "compressedBytes": length,
                    "compressedSha256": compressed_sha256,
                    "decompressedBytes": len(decompressed_bytes),
                    "decompressedSha256": _sha256_bytes(decompressed_bytes),
                }
            )
    _validate_nonoverlapping_regions(
        pyz_regions,
        container_length=pyz_length,
        label="PYZ",
    )
    module_entries.sort(key=lambda item: item["name"].encode("utf-8"))
    compressed_module_bytes = sum(int(item["compressedBytes"]) for item in module_entries)
    pyz_overhead_bytes = pyz_length - compressed_module_bytes
    if pyz_overhead_bytes < 0:
        raise InventoryError("PYZ module bytes exceed the containing CArchive entry.")

    pyz_digest = hashlib.sha256()
    for item in module_entries:
        _update_length_prefixed(pyz_digest, item["name"].encode("utf-8"))
        pyz_digest.update(struct.pack(">BQQ", item["typecode"], item["compressedBytes"], item["offset"]))
        pyz_digest.update(bytes.fromhex(item["compressedSha256"]))
        pyz_digest.update(struct.pack(">Q", item["decompressedBytes"]))
        pyz_digest.update(bytes.fromhex(item["decompressedSha256"]))

    roots = {
        root: {
            "compressedBytes": values["compressedBytes"],
            "moduleCount": values["moduleCount"],
            "components": {
                component: dict(component_values)
                for component, component_values in sorted(values["components"].items())
            },
        }
        for root, values in sorted(
            root_totals.items(), key=lambda item: (item[0].casefold(), item[0])
        )
    }
    if sum(int(item["compressedBytes"]) for item in roots.values()) != compressed_module_bytes:
        raise InventoryError("PYZ root partition does not sum to compressed module bytes.")

    carchive_entry_bytes = sum(int(item["compressedBytes"]) for item in carchive_entries)
    carchive_overhead_bytes = archive_length - carchive_entry_bytes
    if carchive_overhead_bytes < 0:
        raise InventoryError("CArchive entries exceed the containing archive.")
    non_pyz_entry_bytes = carchive_entry_bytes - pyz_length
    trailing_bytes = exe_length - archive_end
    regions = [
        {"id": "pe-bootloader-prefix", "rawBytes": archive_start},
        {"id": "carchive-non-pyz-entries", "rawBytes": non_pyz_entry_bytes},
        {"id": "pyz-compressed-modules", "rawBytes": compressed_module_bytes},
        {"id": "pyz-overhead", "rawBytes": pyz_overhead_bytes},
        {"id": "carchive-overhead", "rawBytes": carchive_overhead_bytes},
        {"id": "post-carchive-trailing-data", "rawBytes": trailing_bytes},
    ]
    for region in regions:
        region["countedInStagedPayload"] = False
    partition_bytes = sum(int(item["rawBytes"]) for item in regions)
    if partition_bytes != exe_length:
        raise InventoryError("Virtual backend executable partition is not byte-exact.")

    pyz_component_bytes: dict[str, int] = defaultdict(int)
    for item in module_entries:
        pyz_component_bytes[str(item["component"])] += int(item["compressedBytes"])
    component_allocations = dict(sorted(pyz_component_bytes.items()))
    component_allocations["backend-executable"] = (
        component_allocations.get("backend-executable", 0)
        + exe_length
        - compressed_module_bytes
    )
    component_allocations = dict(sorted(component_allocations.items()))
    if sum(component_allocations.values()) != exe_length:
        raise InventoryError("Backend component allocation is not byte-exact.")

    return {
        "path": None,
        "length": exe_length,
        "sha256": exe_sha256,
        "pyinstallerVersion": PyInstaller.__version__,
        "physicalAllocationMode": "disjoint-virtual-leaves",
        "componentAllocations": component_allocations,
        "virtualPartitionBytes": partition_bytes,
        "regions": regions,
        "carchive": {
            "startOffset": archive_start,
            "endOffset": archive_end,
            "length": archive_length,
            "entryCount": len(carchive_entries),
            "entryBytes": carchive_entry_bytes,
            "overheadBytes": carchive_overhead_bytes,
            "entries": sorted(carchive_entries, key=lambda item: item["name"]),
        },
        "pyzDiagnostics": {
            "countedInStagedPayload": False,
            "archiveLength": pyz_length,
            "entryCount": len(module_entries),
            "compressedModuleBytes": compressed_module_bytes,
            "overheadBytes": pyz_overhead_bytes,
            "inventorySha256": pyz_digest.hexdigest(),
            "roots": roots,
            "entries": module_entries,
        },
    }


def _resolve_product_version(staged_root: Path, explicit_version: str | None) -> str:
    discovered: set[str] = set()
    for relative in (
        "backend/app/app-layer-manifest.json",
        "app/app-layer-manifest.json",
    ):
        path = staged_root / PurePosixPath(relative)
        if not path.is_file():
            continue
        _ensure_plain_path(path, description="Application-layer manifest")
        try:
            payload = json.loads(_read_stable_bytes(path).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InventoryError(f"Invalid application-layer manifest: {relative}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("applicationVersion"), str):
            discovered.add(payload["applicationVersion"])
    if len(discovered) > 1:
        raise InventoryError("Staged payload contains conflicting application versions.")
    if explicit_version is None:
        if not discovered:
            raise InventoryError(
                "Product version was not supplied and no app-layer manifest provides it."
            )
        version = next(iter(discovered))
    else:
        version = explicit_version
        if discovered and discovered != {version}:
            raise InventoryError("Explicit product version disagrees with staged metadata.")
    if not VERSION_RE.fullmatch(version):
        raise InventoryError(f"Invalid product version: {version!r}")
    return version


def select_installer(
    *,
    product_version: str,
    installer: Path | None,
    artifact_dir: Path | None,
) -> Path:
    if (installer is None) == (artifact_dir is None):
        raise InventoryError("Specify exactly one of installer or artifact_dir.")
    expected_name = f"Scriber_{product_version}_x64-setup.exe"
    if installer is not None:
        installer_entry, selected = _resolve_plain_path(
            installer,
            description="Installer",
            kind="file",
        )
        if installer_entry.name != expected_name or selected.name != expected_name:
            raise InventoryError(
                f"Installer must be named exactly {expected_name}, got {installer_entry.name}."
            )
        return selected

    assert artifact_dir is not None
    _directory_entry, directory = _resolve_plain_path(
        artifact_dir,
        description="Artifact directory",
        kind="directory",
    )
    matches: list[Path] = []
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            _ensure_plain_path(
                current_path / name,
                description="Artifact subdirectory",
            )
        for name in file_names:
            if name != expected_name:
                continue
            match_entry, match_resolved = _resolve_plain_path(
                current_path / name,
                description="Installer artifact",
                kind="file",
            )
            if match_entry.name != expected_name or match_resolved.name != expected_name:
                raise InventoryError(
                    f"Installer artifact must be named exactly {expected_name}."
                )
            matches.append(match_resolved)
    matches.sort(key=lambda path: (str(path).casefold(), str(path)))
    if not matches:
        raise InventoryError(f"Artifact directory has no exact {expected_name}.")
    if len(matches) != 1:
        raise InventoryError(
            f"Artifact directory contains multiple exact {expected_name} files."
        )
    return matches[0]


def _installed_parity(
    staged: Mapping[str, Any], installed: Mapping[str, Any]
) -> dict[str, Any]:
    staged_files = {item["path"].casefold(): item for item in staged["files"]}
    installed_files = {item["path"].casefold(): item for item in installed["files"]}
    missing = sorted(
        staged_files[key]["path"] for key in staged_files.keys() - installed_files.keys()
    )
    installed_only = sorted(
        installed_files[key]["path"] for key in installed_files.keys() - staged_files.keys()
    )
    changed = sorted(
        staged_files[key]["path"]
        for key in staged_files.keys() & installed_files.keys()
        if (
            staged_files[key]["semanticLength"]
            != installed_files[key]["semanticLength"]
            or staged_files[key]["semanticSha256"]
            != installed_files[key]["semanticSha256"]
        )
    )
    allowed_installed_only = [
        path for path in installed_only if PurePosixPath(path.casefold()).name == "uninstall.exe"
    ]
    unexpected_installed_only = sorted(set(installed_only) - set(allowed_installed_only))
    return {
        "ok": not missing and not changed and not unexpected_installed_only,
        "missingFromInstalled": missing,
        "changedInInstalled": changed,
        "installedOnly": installed_only,
        "allowedInstalledOnly": allowed_installed_only,
        "unexpectedInstalledOnly": unexpected_installed_only,
    }


def build_inventory(
    *,
    run_id: str,
    source_commit: str,
    replica_id: str,
    build_root_sha256: str,
    staged_root: Path,
    backend_exe: Path,
    component_map_path: Path,
    installer: Path | None = None,
    artifact_dir: Path | None = None,
    installed_root: Path | None = None,
    product_version: str | None = None,
    compression: str,
    toolchain_hash: str,
    evaluator_hash: str,
) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    source_commit = validate_source_commit(source_commit)
    replica_id = validate_replica_id(replica_id)
    _staged_entry, staged_root = _resolve_plain_path(
        staged_root,
        description="Staged root",
        kind="directory",
    )
    if not isinstance(build_root_sha256, str) or not SHA256_RE.fullmatch(
        build_root_sha256
    ):
        raise InventoryError("build_root_sha256 must be a lowercase SHA-256.")
    expected_build_root_sha256 = build_root_identity_sha256(staged_root)
    if build_root_sha256 != expected_build_root_sha256:
        raise InventoryError(
            "build_root_sha256 does not match the resolved staged-root identity."
        )
    backend_entry = _absolute_path_entry(backend_exe)
    _ensure_plain_descendant_chain(
        _staged_entry,
        backend_entry,
        description="Backend path entry",
    )
    _backend_entry, backend_exe = _resolve_plain_path(
        backend_exe,
        description="Backend executable",
        kind="file",
    )
    if compression not in {"bzip2", "zlib", "lzma"}:
        raise InventoryError(f"Unsupported NSIS compression: {compression}")
    if not SHA256_RE.fullmatch(toolchain_hash):
        raise InventoryError("toolchain_hash must be a lowercase SHA-256.")
    if not SHA256_RE.fullmatch(evaluator_hash):
        raise InventoryError("evaluator_hash must be a lowercase SHA-256.")
    _component_map_entry, resolved_component_map = _resolve_plain_path(
        component_map_path,
        description="Component map",
        kind="file",
    )
    component_map, component_map_sha256 = load_component_map(resolved_component_map)

    try:
        backend_relative = backend_exe.relative_to(staged_root).as_posix()
    except ValueError as exc:
        raise InventoryError("Backend executable is outside the staged root.") from exc
    version = _resolve_product_version(staged_root, product_version)
    selected_installer = select_installer(
        product_version=version,
        installer=installer,
        artifact_dir=artifact_dir,
    )
    installer_length, installer_sha256 = _hash_stable_file(selected_installer)
    backend = inspect_pyinstaller_executable(
        backend_exe,
        component_map=component_map,
    )
    backend["path"] = backend_relative
    staged = _build_tree_inventory(
        staged_root,
        component_map=component_map,
        backend_relative_path=backend_relative,
        backend_attribution=backend,
    )
    backend_file = next(
        (item for item in staged["files"] if item["path"] == backend_relative), None
    )
    if backend_file is None:
        raise InventoryError("Backend executable is missing from the staged inventory.")
    if (
        backend_file["length"] != backend["length"]
        or backend_file["sha256"] != backend["sha256"]
        or backend_file["component"] is not None
        or backend_file["componentAllocations"] != backend["componentAllocations"]
    ):
        raise InventoryError("Backend executable physical and virtual identities disagree.")

    installed: dict[str, Any] | None = None
    parity: dict[str, Any] | None = None
    if installed_root is not None:
        _installed_entry, resolved_installed_root = _resolve_plain_path(
            installed_root,
            description="Installed root",
            kind="directory",
        )
        installed = _build_tree_inventory(
            resolved_installed_root,
            component_map=component_map,
            backend_relative_path=backend_relative,
            backend_attribution=backend,
        )
        parity = _installed_parity(staged, installed)

    integrity_failures: list[str] = []
    if staged["unexpectedDistributions"]:
        integrity_failures.append("unexpected_distributions")
    if staged["componentBytesSum"] != staged["totalBytes"]:
        integrity_failures.append("component_partition_mismatch")
    if backend["virtualPartitionBytes"] != backend["length"]:
        integrity_failures.append("pyinstaller_partition_mismatch")
    if installed is not None and installed["unexpectedDistributions"]:
        integrity_failures.append("installed_unexpected_distributions")
    if parity is not None and not parity["ok"]:
        integrity_failures.append("staged_installed_parity_failed")

    return {
        "inventoryContract": INVENTORY_CONTRACT,
        "schemaVersion": INVENTORY_SCHEMA_VERSION,
        "generatedAtUtc": _utc_now(),
        "ok": not integrity_failures,
        "reasonCodes": integrity_failures,
        "runId": run_id,
        "sourceCommit": source_commit,
        "buildProvenance": {
            "replicaId": replica_id,
            "buildRootSha256": build_root_sha256,
        },
        "productVersion": version,
        "compression": compression,
        "evaluatorHash": evaluator_hash,
        "toolchainHash": toolchain_hash,
        "componentMap": {
            "mapId": component_map["mapId"],
            "sha256": component_map_sha256,
        },
        "installer": {
            "name": selected_installer.name,
            "length": installer_length,
            "sha256": installer_sha256,
        },
        "payload": {
            "staged": staged,
            "installed": installed,
            "stagedInstalledParity": parity,
        },
        "backendExecutable": backend,
    }


def write_json_atomic(payload: Mapping[str, Any], output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
