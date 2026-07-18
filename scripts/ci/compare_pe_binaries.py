"""Compare Windows PE binaries with a deliberately narrow metadata oracle.

Only four kinds of bytes are normalized:

* the COFF ``TimeDateStamp``;
* the optional-header ``CheckSum``;
* each ``IMAGE_DEBUG_DIRECTORY.TimeDateStamp``; and
* the 16-byte GUID in an RSDS CodeView record.

Everything else, including the CodeView age and PDB path and every resource
byte, remains authoritative.  This makes the report useful for distinguishing
ordinary MSVC linker metadata churn from code, import, layout, or Tauri
resource changes without weakening the product's normal raw-SHA attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

import pefile


SCHEMA_VERSION = 1
MAX_PE_BYTES = 128 * 1024 * 1024
MAX_ATTESTATION_BYTES = 1024 * 1024
MAX_DIFF_RANGES = 4096
MAX_RESOURCE_DEPTH = 16
IMAGE_DEBUG_TYPE_CODEVIEW = 2
RSDS_SIGNATURE = b"RSDS"
CACHE_AND_PUBLICATION_FLAGS = (
    "SCRIBER_SAVE_ACTIONS_CACHES",
    "SCRIBER_SAVE_REF_LOCAL_TAURI_CACHE",
    "SCRIBER_SAVE_REF_LOCAL_DESKTOP_INCREMENTAL_CACHE",
    "SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS",
    "SCRIBER_PUBLISH_FINISHED_COMPONENT_CACHE_ARTIFACTS",
    "SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS",
)
ENVIRONMENT_KEYS = (
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_INCREMENTAL",
    "CARGO_TARGET_DIR",
    "tauriConfigPresent",
)


class ComparisonError(RuntimeError):
    """Raised when an input cannot be compared safely as a bounded PE file."""


@dataclass(frozen=True)
class NormalizationRange:
    kind: str
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "endExclusive": self.end,
            "length": self.length,
        }


@dataclass
class PeAnalysis:
    raw: bytes
    normalized: bytes
    report: dict[str, Any]
    normalization_ranges: tuple[NormalizationRange, ...]
    section_intervals: tuple[tuple[int, int, str], ...]
    resource_intervals: tuple[tuple[int, int, str], ...]
    debug_intervals: tuple[tuple[int, int, str], ...]
    header_end: int
    overlay_start: int | None


def _sha256(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_slice(data: bytes, start: int, length: int, label: str) -> bytes:
    if start < 0 or length < 0 or start > len(data) or length > len(data) - start:
        raise ComparisonError(f"{label} points outside the PE file.")
    return data[start : start + length]


def _decode_ascii(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("ascii", errors="replace")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComparisonError(f"{label} must be a JSON object.")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: Iterable[str], label: str
) -> None:
    expected_keys = set(expected)
    actual_keys = set(value)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise ComparisonError(
            f"{label} keys are incompatible; missing={missing}; unexpected={unexpected}."
        )


def _require_string(value: Any, label: str, pattern: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComparisonError(f"{label} must be a non-empty string.")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise ComparisonError(f"{label} has an invalid format.")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ComparisonError(f"{label} must be a JSON boolean.")
    return value


def _require_int(value: Any, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0):
        qualifier = "positive " if positive else ""
        raise ComparisonError(f"{label} must be a {qualifier}JSON integer.")
    return value


def _require_utc_timestamp(value: Any, label: str) -> tuple[datetime, int]:
    text = _require_string(value, label)
    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,7}))?Z", text
    )
    if match is None:
        raise ComparisonError(f"{label} must be a canonical UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ComparisonError(f"{label} is not a valid UTC timestamp.") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ComparisonError(f"{label} must use UTC.")
    fraction = match.group(1) or ""
    ticks_100ns = int(fraction.ljust(7, "0")) if fraction else 0
    return parsed.replace(microsecond=0), ticks_100ns


def _require_safe_relative_path(value: Any, label: str) -> str:
    text = _require_string(value, label)
    path = PurePosixPath(text)
    if (
        text.startswith("/")
        or "\\" in text
        or ":" in text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != text
    ):
        raise ComparisonError(f"{label} must be a canonical safe relative path.")
    return text


def _require_environment_setting(value: Any, label: str) -> dict[str, Any]:
    setting = _require_object(value, label)
    _require_exact_keys(setting, ("present", "value"), label)
    present = _require_bool(setting.get("present"), f"{label}.present")
    setting_value = setting.get("value")
    if present:
        if not isinstance(setting_value, str):
            raise ComparisonError(f"{label}.value must be a string when present.")
    elif setting_value is not None:
        raise ComparisonError(f"{label}.value must be null when absent.")
    return setting


def _require_cache_state(value: Any, label: str) -> str:
    return _require_string(value, label)


def _hash_files_fingerprint(manifest_fingerprint: str) -> str:
    """Reproduce Actions ``hashFiles`` for the raw 32-byte manifest digest."""

    return hashlib.sha256(bytes.fromhex(manifest_fingerprint)).hexdigest()


def _require_tool_identity(
    value: Any, label: str, *, family: str, executable_name: str
) -> dict[str, Any]:
    identity = _require_object(value, label)
    _require_exact_keys(
        identity,
        ("family", "versionDirectory", "relativePath", "sha256", "length", "fileVersion"),
        label,
    )
    _require_string(identity.get("family"), f"{label}.family")
    version_directory = _require_string(
        identity.get("versionDirectory"),
        f"{label}.versionDirectory",
        r"\d+(?:\.\d+){2,3}",
    )
    relative_path = _require_safe_relative_path(
        identity.get("relativePath"), f"{label}.relativePath"
    )
    _require_string(
        identity.get("sha256"), f"{label}.sha256", r"[0-9a-f]{64}"
    )
    _require_int(identity.get("length"), f"{label}.length", positive=True)
    _require_string(identity.get("fileVersion"), f"{label}.fileVersion")
    if identity["family"] != family:
        raise ComparisonError(f"{label}.family is incompatible.")
    expected_relative_path = (
        f"VC/Tools/MSVC/{version_directory}/bin/Hostx64/x64/{executable_name}"
        if family == "msvc-link"
        else f"bin/{version_directory}/x64/{executable_name}"
    )
    if relative_path != expected_relative_path:
        raise ComparisonError(f"{label}.relativePath is incompatible.")
    return identity


def _read_attestation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ComparisonError(f"PE attestation is not a file: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ATTESTATION_BYTES:
        raise ComparisonError(
            f"PE attestation size is outside the supported bound: {path.name}"
        )
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ComparisonError(f"PE attestation is not UTF-8: {path.name}") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ComparisonError(
                    f"PE attestation contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ComparisonError(f"PE attestation is invalid JSON: {path.name}") from exc
    payload = _require_object(payload, "PE attestation")
    _require_exact_keys(
        payload,
        (
            "schemaVersion",
            "generatedAtUtc",
            "diagnosticOnly",
            "mode",
            "runIdentity",
            "executable",
            "audioBuild",
            "overlapSetup",
            "runner",
            "environment",
            "cargoTarget",
            "cacheContext",
            "selfTest",
            "toolchain",
            "safeguards",
        ),
        "PE attestation",
    )

    if _require_int(payload.get("schemaVersion"), "attestation.schemaVersion") != 1:
        raise ComparisonError("PE attestation schemaVersion must be 1.")
    _require_utc_timestamp(payload.get("generatedAtUtc"), "attestation.generatedAtUtc")
    if _require_bool(payload.get("diagnosticOnly"), "attestation.diagnosticOnly") is not True:
        raise ComparisonError("PE attestation must be diagnostic-only.")
    mode = _require_string(payload.get("mode"), "attestation.mode")
    if mode not in {"sequential", "overlap"}:
        raise ComparisonError("PE attestation mode must be sequential or overlap.")

    run_identity = _require_object(payload.get("runIdentity"), "attestation.runIdentity")
    _require_exact_keys(
        run_identity, ("repository", "runId", "sourceCommit", "ref"), "attestation.runIdentity"
    )
    _require_string(run_identity.get("repository"), "attestation.runIdentity.repository")
    _require_int(run_identity.get("runId"), "attestation.runIdentity.runId", positive=True)
    _require_string(
        run_identity.get("sourceCommit"),
        "attestation.runIdentity.sourceCommit",
        r"[0-9a-f]{40}",
    )
    ref = _require_string(run_identity.get("ref"), "attestation.runIdentity.ref")
    if not ref.startswith("refs/heads/") or ref == "refs/heads/main":
        raise ComparisonError("PE attestation ref must be a non-main branch.")

    executable = _require_object(payload.get("executable"), "attestation.executable")
    _require_exact_keys(
        executable, ("fileName", "sha256", "length"), "attestation.executable"
    )
    file_name = _require_string(
        executable.get("fileName"), "attestation.executable.fileName"
    )
    if "/" in file_name or "\\" in file_name:
        raise ComparisonError("PE attestation executable fileName must be a basename.")
    _require_string(
        executable.get("sha256"),
        "attestation.executable.sha256",
        r"[0-9a-f]{64}",
    )
    _require_int(
        executable.get("length"), "attestation.executable.length", positive=True
    )

    audio_build = _require_object(payload.get("audioBuild"), "attestation.audioBuild")
    _require_exact_keys(
        audio_build,
        (
            "cacheHit",
            "cacheKey",
            "isolatedCargoTarget",
            "durationMs",
            "parallel",
            "sharedCargoTarget",
            "phaseSchema",
            "phaseParallelPropertyPresent",
            "phaseSharedCargoTargetPropertyPresent",
        ),
        "attestation.audioBuild",
    )
    _require_bool(audio_build.get("cacheHit"), "attestation.audioBuild.cacheHit")
    _require_string(
        audio_build.get("cacheKey"),
        "attestation.audioBuild.cacheKey",
        r"[0-9a-f]{64}",
    )
    _require_bool(
        audio_build.get("isolatedCargoTarget"),
        "attestation.audioBuild.isolatedCargoTarget",
    )
    if _require_int(
        audio_build.get("durationMs"), "attestation.audioBuild.durationMs"
    ) < 0:
        raise ComparisonError("attestation.audioBuild.durationMs must be non-negative.")
    _require_bool(audio_build.get("parallel"), "attestation.audioBuild.parallel")
    _require_bool(
        audio_build.get("sharedCargoTarget"),
        "attestation.audioBuild.sharedCargoTarget",
    )
    _require_string(audio_build.get("phaseSchema"), "attestation.audioBuild.phaseSchema")
    _require_bool(
        audio_build.get("phaseParallelPropertyPresent"),
        "attestation.audioBuild.phaseParallelPropertyPresent",
    )
    _require_bool(
        audio_build.get("phaseSharedCargoTargetPropertyPresent"),
        "attestation.audioBuild.phaseSharedCargoTargetPropertyPresent",
    )

    setup = _require_object(payload.get("overlapSetup"), "attestation.overlapSetup")
    _require_exact_keys(
        setup,
        (
            "sharedCargoTarget",
            "backendResourcePlaceholderCreated",
            "backendResourcePlaceholderInitialFileCount",
            "childTauriConfigPresent",
            "backendStagedOutsideResourcePath",
            "backendPromotedAfterAudio",
        ),
        "attestation.overlapSetup",
    )
    for field in (
        "sharedCargoTarget",
        "backendResourcePlaceholderCreated",
        "backendStagedOutsideResourcePath",
        "backendPromotedAfterAudio",
    ):
        _require_bool(setup.get(field), f"attestation.overlapSetup.{field}")
    initial_file_count = setup.get("backendResourcePlaceholderInitialFileCount")
    if initial_file_count is not None:
        _require_int(
            initial_file_count,
            "attestation.overlapSetup.backendResourcePlaceholderInitialFileCount",
        )
    child_tauri_config = setup.get("childTauriConfigPresent")
    if child_tauri_config is not None:
        _require_bool(
            child_tauri_config,
            "attestation.overlapSetup.childTauriConfigPresent",
        )

    runner = _require_object(payload.get("runner"), "attestation.runner")
    _require_exact_keys(
        runner, ("runnerOS", "runnerArch", "imageOS", "imageVersion"), "attestation.runner"
    )
    for field in ("runnerOS", "runnerArch", "imageOS", "imageVersion"):
        _require_string(runner.get(field), f"attestation.runner.{field}")

    environment = _require_object(payload.get("environment"), "attestation.environment")
    _require_exact_keys(environment, ENVIRONMENT_KEYS, "attestation.environment")
    for field in ENVIRONMENT_KEYS[:-1]:
        _require_environment_setting(
            environment.get(field), f"attestation.environment.{field}"
        )
    _require_bool(
        environment.get("tauriConfigPresent"),
        "attestation.environment.tauriConfigPresent",
    )

    cargo_target = _require_object(payload.get("cargoTarget"), "attestation.cargoTarget")
    _require_exact_keys(
        cargo_target,
        (
            "relativePath",
            "metadataApiVersion",
            "metadataCacheKey",
            "metadataCacheHit",
            "isolated",
        ),
        "attestation.cargoTarget",
    )
    _require_safe_relative_path(
        cargo_target.get("relativePath"), "attestation.cargoTarget.relativePath"
    )
    _require_string(
        cargo_target.get("metadataApiVersion"),
        "attestation.cargoTarget.metadataApiVersion",
    )
    _require_string(
        cargo_target.get("metadataCacheKey"),
        "attestation.cargoTarget.metadataCacheKey",
        r"[0-9a-f]{64}",
    )
    _require_bool(
        cargo_target.get("metadataCacheHit"),
        "attestation.cargoTarget.metadataCacheHit",
    )
    _require_bool(cargo_target.get("isolated"), "attestation.cargoTarget.isolated")

    cache_context = _require_object(payload.get("cacheContext"), "attestation.cacheContext")
    _require_exact_keys(
        cache_context,
        ("rustDependencies", "tauriAppBinary", "audioSidecar", "backendProducts"),
        "attestation.cacheContext",
    )
    rust_dependencies = _require_object(
        cache_context.get("rustDependencies"),
        "attestation.cacheContext.rustDependencies",
    )
    _require_exact_keys(
        rust_dependencies,
        (
            "manifestFingerprint",
            "hashFilesFingerprint",
            "expectedActionsKey",
            "matchedActionsKey",
            "actionsCacheExact",
            "effectiveRestoreSource",
            "releaseArtifactExact",
            "releaseArtifactRestored",
            "releaseArtifactImported",
        ),
        "attestation.cacheContext.rustDependencies",
    )
    for field in ("manifestFingerprint", "hashFilesFingerprint"):
        _require_string(
            rust_dependencies.get(field),
            f"attestation.cacheContext.rustDependencies.{field}",
            r"[0-9a-f]{64}",
        )
    for field in ("expectedActionsKey", "matchedActionsKey", "effectiveRestoreSource"):
        _require_string(
            rust_dependencies.get(field),
            f"attestation.cacheContext.rustDependencies.{field}",
        )
    _require_bool(
        rust_dependencies.get("actionsCacheExact"),
        "attestation.cacheContext.rustDependencies.actionsCacheExact",
    )
    for field in (
        "releaseArtifactExact",
        "releaseArtifactRestored",
        "releaseArtifactImported",
    ):
        state = _require_cache_state(
            rust_dependencies.get(field),
            f"attestation.cacheContext.rustDependencies.{field}",
        )
        if state not in {"true", "false", "empty"}:
            raise ComparisonError(
                f"attestation.cacheContext.rustDependencies.{field} is invalid."
            )

    tauri_app = _require_object(
        cache_context.get("tauriAppBinary"),
        "attestation.cacheContext.tauriAppBinary",
    )
    _require_exact_keys(
        tauri_app,
        (
            "manifestFingerprint",
            "hashFilesFingerprint",
            "expectedActionsKey",
            "actionsCacheExact",
            "importUsable",
            "importedSha256",
            "effectiveRestoreSource",
        ),
        "attestation.cacheContext.tauriAppBinary",
    )
    for field in (
        "manifestFingerprint",
        "hashFilesFingerprint",
        "importedSha256",
    ):
        _require_string(
            tauri_app.get(field),
            f"attestation.cacheContext.tauriAppBinary.{field}",
            r"[0-9a-f]{64}",
        )
    for field in ("expectedActionsKey", "effectiveRestoreSource"):
        _require_string(
            tauri_app.get(field),
            f"attestation.cacheContext.tauriAppBinary.{field}",
        )
    for field in ("actionsCacheExact", "importUsable"):
        _require_bool(
            tauri_app.get(field),
            f"attestation.cacheContext.tauriAppBinary.{field}",
        )

    audio_sidecar = _require_object(
        cache_context.get("audioSidecar"),
        "attestation.cacheContext.audioSidecar",
    )
    _require_exact_keys(
        audio_sidecar,
        (
            "actions",
            "releaseArtifact",
            "effectiveRestoreSource",
            "internalCacheHit",
            "internalCacheKey",
        ),
        "attestation.cacheContext.audioSidecar",
    )
    for field in ("actions", "releaseArtifact", "effectiveRestoreSource"):
        _require_string(
            audio_sidecar.get(field),
            f"attestation.cacheContext.audioSidecar.{field}",
        )
    _require_bool(
        audio_sidecar.get("internalCacheHit"),
        "attestation.cacheContext.audioSidecar.internalCacheHit",
    )
    _require_string(
        audio_sidecar.get("internalCacheKey"),
        "attestation.cacheContext.audioSidecar.internalCacheKey",
        r"[0-9a-f]{64}",
    )

    backend_products = _require_object(
        cache_context.get("backendProducts"),
        "attestation.cacheContext.backendProducts",
    )
    _require_exact_keys(
        backend_products,
        ("sidecar", "runtime", "sidecarPrebuilt", "coldProductsUsed"),
        "attestation.cacheContext.backendProducts",
    )
    for product_name in ("sidecar", "runtime"):
        product = _require_object(
            backend_products.get(product_name),
            f"attestation.cacheContext.backendProducts.{product_name}",
        )
        expected_keys = [
            "fingerprint",
            "actions",
            "releaseArtifact",
            "effectiveRestoreSource",
        ]
        if product_name == "runtime":
            expected_keys.append("validated")
        _require_exact_keys(
            product,
            expected_keys,
            f"attestation.cacheContext.backendProducts.{product_name}",
        )
        _require_string(
            product.get("fingerprint"),
            f"attestation.cacheContext.backendProducts.{product_name}.fingerprint",
            r"[0-9a-f]{64}",
        )
        for field in ("actions", "releaseArtifact", "effectiveRestoreSource"):
            _require_string(
                product.get(field),
                f"attestation.cacheContext.backendProducts.{product_name}.{field}",
            )
        if product_name == "runtime":
            validated = _require_string(
                product.get("validated"),
                "attestation.cacheContext.backendProducts.runtime.validated",
            )
            if validated not in {"true", "false", "empty"}:
                raise ComparisonError(
                    "attestation.cacheContext.backendProducts.runtime.validated is invalid."
                )
    _require_bool(
        backend_products.get("sidecarPrebuilt"),
        "attestation.cacheContext.backendProducts.sidecarPrebuilt",
    )
    _require_bool(
        backend_products.get("coldProductsUsed"),
        "attestation.cacheContext.backendProducts.coldProductsUsed",
    )

    self_test = _require_object(payload.get("selfTest"), "attestation.selfTest")
    if self_test.get("ok") is not True:
        raise ComparisonError("PE attestation selfTest.ok must be true.")
    toolchain = _require_object(payload.get("toolchain"), "attestation.toolchain")
    _require_exact_keys(
        toolchain, ("rustc", "cargo", "linker", "resourceCompiler"), "attestation.toolchain"
    )
    _require_string(toolchain.get("rustc"), "attestation.toolchain.rustc")
    _require_string(toolchain.get("cargo"), "attestation.toolchain.cargo")
    _require_tool_identity(
        toolchain.get("linker"),
        "attestation.toolchain.linker",
        family="msvc-link",
        executable_name="link.exe",
    )
    _require_tool_identity(
        toolchain.get("resourceCompiler"),
        "attestation.toolchain.resourceCompiler",
        family="windows-sdk-rc",
        executable_name="rc.exe",
    )

    safeguards = _require_object(payload.get("safeguards"), "attestation.safeguards")
    _require_exact_keys(
        safeguards,
        (
            "featureBranchOnly",
            "refreshReleaseCacheArtifacts",
            "cacheHitForbidden",
            "publicationForbidden",
            "tauriConfigPresent",
            "cacheAndPublicationEnvironment",
            "cacheAndPublicationFlagsAllFalse",
        ),
        "attestation.safeguards",
    )
    for field in (
        "featureBranchOnly",
        "refreshReleaseCacheArtifacts",
        "cacheHitForbidden",
        "publicationForbidden",
        "tauriConfigPresent",
        "cacheAndPublicationFlagsAllFalse",
    ):
        _require_bool(safeguards.get(field), f"attestation.safeguards.{field}")
    cache_flags = _require_object(
        safeguards.get("cacheAndPublicationEnvironment"),
        "attestation.safeguards.cacheAndPublicationEnvironment",
    )
    _require_exact_keys(
        cache_flags,
        CACHE_AND_PUBLICATION_FLAGS,
        "attestation.safeguards.cacheAndPublicationEnvironment",
    )
    for field in CACHE_AND_PUBLICATION_FLAGS:
        _require_string(
            cache_flags.get(field),
            f"attestation.safeguards.cacheAndPublicationEnvironment.{field}",
        )
    return payload


def _normalization_ranges(
    pe: pefile.PE, raw: bytes
) -> tuple[tuple[NormalizationRange, ...], list[dict[str, Any]], dict[str, Any]]:
    ranges = [
        NormalizationRange(
            "coff.timeDateStamp",
            pe.FILE_HEADER.get_field_absolute_offset("TimeDateStamp"),
            4,
        ),
        NormalizationRange(
            "optionalHeader.checkSum",
            pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum"),
            4,
        ),
    ]
    volatile_debug: list[dict[str, Any]] = []
    debug_invariants: list[dict[str, Any]] = []

    for index, entry in enumerate(getattr(pe, "DIRECTORY_ENTRY_DEBUG", [])):
        structure = entry.struct
        ranges.append(
            NormalizationRange(
                f"debug[{index}].timeDateStamp",
                structure.get_field_absolute_offset("TimeDateStamp"),
                4,
            )
        )
        payload = _bounded_slice(
            raw,
            int(structure.PointerToRawData),
            int(structure.SizeOfData),
            f"Debug entry {index}",
        )
        invariant: dict[str, Any] = {
            "index": index,
            "characteristics": int(structure.Characteristics),
            "majorVersion": int(structure.MajorVersion),
            "minorVersion": int(structure.MinorVersion),
            "type": int(structure.Type),
            "sizeOfData": int(structure.SizeOfData),
            "addressOfRawData": int(structure.AddressOfRawData),
            "pointerToRawData": int(structure.PointerToRawData),
        }
        volatile: dict[str, Any] = {
            "index": index,
            "timeDateStamp": int(structure.TimeDateStamp),
        }

        if int(structure.Type) == IMAGE_DEBUG_TYPE_CODEVIEW and payload.startswith(
            RSDS_SIGNATURE
        ):
            if len(payload) < 24:
                raise ComparisonError(f"Debug entry {index} has a truncated RSDS record.")
            guid_offset = int(structure.PointerToRawData) + 4
            ranges.append(
                NormalizationRange(f"debug[{index}].rsdsGuid", guid_offset, 16)
            )
            guid = payload[4:20]
            age = struct.unpack_from("<I", payload, 20)[0]
            path_field = payload[24:]
            nul_index = path_field.find(b"\0")
            if nul_index < 0:
                raise ComparisonError(
                    f"Debug entry {index} has an unterminated RSDS PDB path."
                )
            pdb_path = path_field[:nul_index]
            trailing = path_field[nul_index + 1 :]
            if any(trailing):
                raise ComparisonError(
                    f"Debug entry {index} has non-zero bytes after its RSDS PDB path."
                )
            normalized_payload = bytearray(payload)
            normalized_payload[4:20] = b"\0" * 16
            pdb_text = pdb_path.decode("utf-8", errors="replace")
            invariant["codeView"] = {
                "signature": "RSDS",
                "age": age,
                "pdbPathLength": len(pdb_path),
                "pdbPathSha256": _sha256(pdb_path),
                "pdbFileName": PureWindowsPath(pdb_text).name,
                "normalizedPayloadSha256": _sha256(normalized_payload),
            }
            volatile["rsdsGuid"] = guid.hex()
        else:
            invariant["payloadSha256"] = _sha256(payload)

        volatile_debug.append(volatile)
        debug_invariants.append(invariant)

    ordered = tuple(sorted(ranges, key=lambda item: (item.start, item.length, item.kind)))
    previous_end = -1
    for item in ordered:
        _bounded_slice(raw, item.start, item.length, item.kind)
        if item.start < previous_end:
            raise ComparisonError("PE normalization fields overlap unexpectedly.")
        previous_end = item.end

    volatile_metadata = {
        "coffTimeDateStamp": int(pe.FILE_HEADER.TimeDateStamp),
        "optionalHeaderCheckSum": int(pe.OPTIONAL_HEADER.CheckSum),
        "debug": volatile_debug,
    }
    return ordered, debug_invariants, volatile_metadata


def _apply_normalization(
    raw: bytes, ranges: Iterable[NormalizationRange]
) -> bytes:
    normalized = bytearray(raw)
    for item in ranges:
        normalized[item.start : item.end] = b"\0" * item.length
    return bytes(normalized)


def _identifier(entry: Any, *, depth: int) -> dict[str, Any]:
    if entry.name is not None:
        return {"kind": "name", "value": str(entry.name)}
    identifier = int(entry.id)
    result: dict[str, Any] = {"kind": "id", "value": identifier}
    if depth == 0:
        type_name = pefile.RESOURCE_TYPE.get(identifier)
        if isinstance(type_name, str):
            result["resourceType"] = type_name
    return result


def _resource_report(
    pe: pefile.PE, raw: bytes
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], tuple[tuple[int, int, str], ...]]:
    root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if root is None:
        return None, [], ()

    leaves: list[dict[str, Any]] = []
    intervals: list[tuple[int, int, str]] = []
    active_offsets: set[int] = set()

    def visit(directory: Any, path: list[dict[str, Any]], depth: int) -> dict[str, Any]:
        if depth > MAX_RESOURCE_DEPTH:
            raise ComparisonError("PE resource tree exceeds the supported depth.")
        structure = directory.struct
        structure_offset = int(structure.get_file_offset())
        if structure_offset in active_offsets:
            raise ComparisonError("PE resource tree contains a directory cycle.")
        active_offsets.add(structure_offset)
        try:
            node: dict[str, Any] = {
                "characteristics": int(structure.Characteristics),
                "timeDateStamp": int(structure.TimeDateStamp),
                "majorVersion": int(structure.MajorVersion),
                "minorVersion": int(structure.MinorVersion),
                "entries": [],
            }
            for entry in directory.entries:
                identity = _identifier(entry, depth=depth)
                child_path = [*path, identity]
                child: dict[str, Any] = {"identifier": identity}
                if hasattr(entry, "directory"):
                    child["directory"] = visit(entry.directory, child_path, depth + 1)
                elif hasattr(entry, "data"):
                    data_structure = entry.data.struct
                    rva = int(data_structure.OffsetToData)
                    size = int(data_structure.Size)
                    try:
                        file_offset = int(pe.get_offset_from_rva(rva))
                    except (pefile.PEFormatError, TypeError, ValueError) as exc:
                        raise ComparisonError(
                            "PE resource data points outside file-backed sections."
                        ) from exc
                    content = _bounded_slice(raw, file_offset, size, "PE resource data")
                    leaf = {
                        "path": child_path,
                        "rva": rva,
                        "fileOffset": file_offset,
                        "size": size,
                        "codePage": int(data_structure.CodePage),
                        "reserved": int(data_structure.Reserved),
                        "sha256": _sha256(content),
                    }
                    leaves.append(leaf)
                    path_text = "/".join(str(part["value"]) for part in child_path)
                    intervals.append(
                        (file_offset, file_offset + size, f"resource:{path_text}")
                    )
                    child["data"] = {key: value for key, value in leaf.items() if key != "path"}
                else:
                    raise ComparisonError("PE resource entry has no directory or data.")
                node["entries"].append(child)
            return node
        finally:
            active_offsets.remove(structure_offset)

    tree = visit(root, [], 0)
    leaves.sort(key=lambda item: json.dumps(item["path"], sort_keys=True))
    return tree, leaves, tuple(intervals)


def _import_symbol(item: Any, image_base: int) -> dict[str, Any]:
    name = _decode_ascii(item.name)
    return {
        "name": name,
        "ordinal": int(item.ordinal) if item.ordinal is not None else None,
        "hint": int(item.hint) if item.hint is not None else None,
        "iatRva": int(item.address) - image_base,
    }


def _imports(pe: pefile.PE, attribute: str) -> list[dict[str, Any]]:
    image_base = int(pe.OPTIONAL_HEADER.ImageBase)
    result = []
    for descriptor in getattr(pe, attribute, []):
        symbols = [_import_symbol(item, image_base) for item in descriptor.imports]
        symbols.sort(
            key=lambda item: (
                item["name"] is None,
                (item["name"] or "").casefold(),
                item["ordinal"] if item["ordinal"] is not None else -1,
                item["iatRva"],
            )
        )
        result.append({"dll": _decode_ascii(descriptor.dll), "symbols": symbols})
    result.sort(key=lambda item: (item["dll"] or "").casefold())
    return result


def _exports(pe: pefile.PE) -> list[dict[str, Any]]:
    directory = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
    if directory is None:
        return []
    exports = [
        {
            "name": _decode_ascii(symbol.name),
            "ordinal": int(symbol.ordinal),
            "rva": int(symbol.address),
            "forwarder": _decode_ascii(symbol.forwarder),
        }
        for symbol in directory.symbols
    ]
    exports.sort(key=lambda item: (item["ordinal"], item["name"] or ""))
    return exports


def _pe_invariants(
    pe: pefile.PE, debug_invariants: list[dict[str, Any]]
) -> dict[str, Any]:
    file_header_fields = (
        "Machine",
        "NumberOfSections",
        "PointerToSymbolTable",
        "NumberOfSymbols",
        "SizeOfOptionalHeader",
        "Characteristics",
    )
    optional_header_fields = (
        "Magic",
        "MajorLinkerVersion",
        "MinorLinkerVersion",
        "SizeOfCode",
        "SizeOfInitializedData",
        "SizeOfUninitializedData",
        "AddressOfEntryPoint",
        "BaseOfCode",
        "BaseOfData",
        "ImageBase",
        "SectionAlignment",
        "FileAlignment",
        "MajorOperatingSystemVersion",
        "MinorOperatingSystemVersion",
        "MajorImageVersion",
        "MinorImageVersion",
        "MajorSubsystemVersion",
        "MinorSubsystemVersion",
        "Reserved1",
        "Win32VersionValue",
        "SizeOfImage",
        "SizeOfHeaders",
        "Subsystem",
        "DllCharacteristics",
        "SizeOfStackReserve",
        "SizeOfStackCommit",
        "SizeOfHeapReserve",
        "SizeOfHeapCommit",
        "LoaderFlags",
        "NumberOfRvaAndSizes",
    )
    file_header = {
        name: int(getattr(pe.FILE_HEADER, name)) for name in file_header_fields
    }
    optional_header = {
        name: int(getattr(pe.OPTIONAL_HEADER, name))
        for name in optional_header_fields
        if hasattr(pe.OPTIONAL_HEADER, name)
    }
    directories = [
        {
            "index": index,
            "name": str(directory.name),
            "rva": int(directory.VirtualAddress),
            "size": int(directory.Size),
        }
        for index, directory in enumerate(pe.OPTIONAL_HEADER.DATA_DIRECTORY)
    ]
    return {
        "dosMagic": int(pe.DOS_HEADER.e_magic),
        "peSignature": int(pe.NT_HEADERS.Signature),
        "fileHeader": file_header,
        "optionalHeader": optional_header,
        "dataDirectories": directories,
        "debugEntries": debug_invariants,
    }


def _analyse(path: Path) -> PeAnalysis:
    if not path.is_file():
        raise ComparisonError(f"PE input is not a file: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_PE_BYTES:
        raise ComparisonError(f"PE input size is outside the supported bound: {path.name}")
    raw = path.read_bytes()
    try:
        pe = pefile.PE(data=raw, fast_load=False)
        pe.parse_data_directories()
    except pefile.PEFormatError as exc:
        raise ComparisonError(f"PE input is invalid: {path.name}") from exc

    ranges, debug_invariants, volatile_metadata = _normalization_ranges(pe, raw)
    normalized = _apply_normalization(raw, ranges)
    resource_tree, resource_leaves, resource_intervals = _resource_report(pe, raw)

    sections: list[dict[str, Any]] = []
    section_intervals: list[tuple[int, int, str]] = []
    for index, section in enumerate(pe.sections):
        name = section.Name.rstrip(b"\0").decode("ascii", errors="replace")
        raw_offset = int(section.PointerToRawData)
        raw_size = int(section.SizeOfRawData)
        raw_content = _bounded_slice(raw, raw_offset, raw_size, f"PE section {name}")
        normalized_content = _bounded_slice(
            normalized, raw_offset, raw_size, f"Normalized PE section {name}"
        )
        sections.append(
            {
                "index": index,
                "name": name,
                "virtualAddress": int(section.VirtualAddress),
                "virtualSize": int(section.Misc_VirtualSize),
                "rawOffset": raw_offset,
                "rawSize": raw_size,
                "characteristics": int(section.Characteristics),
                "rawSha256": _sha256(raw_content),
                "normalizedSha256": _sha256(normalized_content),
            }
        )
        if raw_size:
            section_intervals.append((raw_offset, raw_offset + raw_size, f"section:{name}"))

    non_empty_section_offsets = [
        int(section.PointerToRawData)
        for section in pe.sections
        if int(section.SizeOfRawData) > 0
    ]
    header_end = min(non_empty_section_offsets) if non_empty_section_offsets else len(raw)
    _bounded_slice(raw, 0, header_end, "PE headers")
    overlay_start = pe.get_overlay_data_start_offset()
    if overlay_start is not None:
        overlay_start = int(overlay_start)
        _bounded_slice(raw, overlay_start, len(raw) - overlay_start, "PE overlay")

    debug_intervals = tuple(
        (
            int(entry.struct.PointerToRawData),
            int(entry.struct.PointerToRawData) + int(entry.struct.SizeOfData),
            f"debugPayload:{index}",
        )
        for index, entry in enumerate(getattr(pe, "DIRECTORY_ENTRY_DEBUG", []))
        if int(entry.struct.SizeOfData) > 0
    )

    report = {
        "fileName": path.name,
        "length": len(raw),
        "rawSha256": _sha256(raw),
        "normalizedSha256": _sha256(normalized),
        "header": {
            "length": header_end,
            "rawSha256": _sha256(raw[:header_end]),
            "normalizedSha256": _sha256(normalized[:header_end]),
        },
        "overlay": None
        if overlay_start is None
        else {
            "offset": overlay_start,
            "length": len(raw) - overlay_start,
            "rawSha256": _sha256(raw[overlay_start:]),
            "normalizedSha256": _sha256(normalized[overlay_start:]),
        },
        "normalizationRanges": [item.as_dict() for item in ranges],
        "volatileMetadata": volatile_metadata,
        "peInvariants": _pe_invariants(pe, debug_invariants),
        "sections": sections,
        "imports": {
            "normal": _imports(pe, "DIRECTORY_ENTRY_IMPORT"),
            "delay": _imports(pe, "DIRECTORY_ENTRY_DELAY_IMPORT"),
        },
        "exports": _exports(pe),
        "resources": {"tree": resource_tree, "leaves": resource_leaves},
    }
    pe.close()
    return PeAnalysis(
        raw=raw,
        normalized=normalized,
        report=report,
        normalization_ranges=ranges,
        section_intervals=tuple(section_intervals),
        resource_intervals=resource_intervals,
        debug_intervals=debug_intervals,
        header_end=header_end,
        overlay_start=overlay_start,
    )


def _raw_diff_ranges(left: bytes, right: bytes) -> Iterable[tuple[int, int]]:
    shared_length = min(len(left), len(right))
    index = 0
    while index < shared_length:
        if left[index] == right[index]:
            index += 1
            continue
        start = index
        index += 1
        while index < shared_length and left[index] != right[index]:
            index += 1
        yield start, index
    if len(left) != len(right):
        yield shared_length, max(len(left), len(right))


def _overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _location_kinds(analysis: PeAnalysis, start: int, end: int) -> list[str]:
    kinds: list[str] = []
    if start < analysis.header_end:
        kinds.append("peHeaders")
    for range_start, range_end, kind in analysis.section_intervals:
        if _overlap(start, end, range_start, range_end):
            kinds.append(kind)
    for range_start, range_end, kind in analysis.debug_intervals:
        if _overlap(start, end, range_start, range_end):
            kinds.append(kind)
    for range_start, range_end, kind in analysis.resource_intervals:
        if _overlap(start, end, range_start, range_end):
            kinds.append(kind)
    if analysis.overlay_start is not None and end > analysis.overlay_start:
        kinds.append("overlay")
    if not kinds:
        kinds.append("unmapped")
    return kinds


def _common_allowed_range(
    left: PeAnalysis, right: PeAnalysis, start: int, end: int
) -> NormalizationRange | None:
    right_ranges = {
        (item.kind, item.start, item.length): item for item in right.normalization_ranges
    }
    for item in left.normalization_ranges:
        if (
            (item.kind, item.start, item.length) in right_ranges
            and item.start <= start
            and end <= item.end
        ):
            return item
    return None


def _diff_report(left: PeAnalysis, right: PeAnalysis) -> dict[str, Any]:
    stored: list[dict[str, Any]] = []
    range_count = 0
    differing_bytes = 0
    allowed_only = True
    for start, end in _raw_diff_ranges(left.raw, right.raw):
        range_count += 1
        differing_bytes += end - start
        allowed = _common_allowed_range(left, right, start, end)
        if allowed is None:
            allowed_only = False
        if len(stored) >= MAX_DIFF_RANGES:
            continue
        left_slice = left.raw[start : min(end, len(left.raw))]
        right_slice = right.raw[start : min(end, len(right.raw))]
        kinds = set(_location_kinds(left, start, end))
        kinds.update(_location_kinds(right, start, end))
        if allowed is not None:
            kinds.add(allowed.kind)
        stored.append(
            {
                "start": start,
                "endExclusive": end,
                "length": end - start,
                "allowed": allowed is not None,
                "kinds": sorted(kinds),
                "leftSha256": _sha256(left_slice),
                "rightSha256": _sha256(right_slice),
            }
        )
    return {
        "rangeCount": range_count,
        "storedRangeCount": len(stored),
        "rangesTruncated": range_count > len(stored),
        "differingByteCount": differing_bytes,
        "allowedDifferencesOnly": allowed_only,
        "ranges": stored,
    }


def _section_layout(report: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "index",
        "name",
        "virtualAddress",
        "virtualSize",
        "rawOffset",
        "rawSize",
        "characteristics",
    )
    return [{key: section[key] for key in keys} for section in report["sections"]]


def _sequential_setup_matches(attestation: dict[str, Any]) -> bool:
    audio = attestation["audioBuild"]
    setup = attestation["overlapSetup"]
    phase_schema_matches = (
        audio["phaseSchema"] == "explicit-sequential"
        and audio["phaseParallelPropertyPresent"] is True
        and audio["phaseSharedCargoTargetPropertyPresent"] is True
    ) or (
        audio["phaseSchema"] == "sequential-core-without-parallel-properties"
        and audio["phaseParallelPropertyPresent"] is False
        and audio["phaseSharedCargoTargetPropertyPresent"] is False
    )
    return (
        attestation["mode"] == "sequential"
        and phase_schema_matches
        and audio["parallel"] is False
        and audio["sharedCargoTarget"] is False
        and setup["sharedCargoTarget"] is False
        and setup["backendResourcePlaceholderCreated"] is False
        and setup["backendResourcePlaceholderInitialFileCount"] is None
        and setup["childTauriConfigPresent"] is None
        and setup["backendStagedOutsideResourcePath"] is False
        and setup["backendPromotedAfterAudio"] is False
    )


def _overlap_setup_matches(attestation: dict[str, Any]) -> bool:
    audio = attestation["audioBuild"]
    setup = attestation["overlapSetup"]
    return (
        attestation["mode"] == "overlap"
        and audio["phaseSchema"] == "explicit-overlap"
        and audio["phaseParallelPropertyPresent"] is True
        and audio["phaseSharedCargoTargetPropertyPresent"] is True
        and audio["parallel"] is True
        and audio["sharedCargoTarget"] is True
        and setup["sharedCargoTarget"] is True
        and setup["backendResourcePlaceholderCreated"] is True
        and setup["backendResourcePlaceholderInitialFileCount"] == 0
        and setup["childTauriConfigPresent"] is False
        and setup["backendStagedOutsideResourcePath"] is True
        and setup["backendPromotedAfterAudio"] is True
    )


def _runner_is_safe(attestation: dict[str, Any]) -> bool:
    runner = attestation["runner"]
    return runner["runnerOS"] == "Windows" and runner["runnerArch"] == "X64"


def _environment_is_safe(attestation: dict[str, Any]) -> bool:
    environment = attestation["environment"]
    incremental = environment["CARGO_INCREMENTAL"]
    return (
        environment["tauriConfigPresent"] is False
        and incremental["present"] is True
        and incremental["value"] == "1"
    )


def _cargo_target_is_safe(attestation: dict[str, Any]) -> bool:
    cargo_target = attestation["cargoTarget"]
    return (
        cargo_target["relativePath"] == "Frontend/src-tauri/target"
        and cargo_target["metadataApiVersion"] == "1"
        and cargo_target["metadataCacheKey"] == attestation["audioBuild"]["cacheKey"]
        and cargo_target["metadataCacheHit"] is False
        and cargo_target["isolated"] is False
    )


def _manifest_cache_key_matches(cache: dict[str, Any], prefix: str) -> bool:
    derived_fingerprint = _hash_files_fingerprint(cache["manifestFingerprint"])
    expected_key = f"{prefix}{derived_fingerprint}"
    return (
        cache["hashFilesFingerprint"] == derived_fingerprint
        and cache["expectedActionsKey"] == expected_key
    )


def _rust_dependencies_cache_is_safe(attestation: dict[str, Any]) -> bool:
    cache = attestation["cacheContext"]["rustDependencies"]
    return (
        _manifest_cache_key_matches(
            cache, "scriber-rust-dependencies-v1-Windows-"
        )
        and cache["matchedActionsKey"] == cache["expectedActionsKey"]
        and cache["actionsCacheExact"] is True
        and cache["effectiveRestoreSource"] == "actions-cache-exact"
    )


def _tauri_app_cache_is_safe(attestation: dict[str, Any]) -> bool:
    cache = attestation["cacheContext"]["tauriAppBinary"]
    return (
        _manifest_cache_key_matches(
            cache, "scriber-tauri-app-binary-v3-Windows-"
        )
        and cache["actionsCacheExact"] is True
        and cache["importUsable"] is True
        and cache["effectiveRestoreSource"] == "actions-cache-exact-validated"
    )


def _audio_sidecar_cache_is_safe(attestation: dict[str, Any]) -> bool:
    cache = attestation["cacheContext"]["audioSidecar"]
    return (
        cache["actions"] == "miss"
        and cache["releaseArtifact"] == "false"
        and cache["effectiveRestoreSource"] == "miss"
        and cache["internalCacheHit"] is False
        and cache["internalCacheKey"] == attestation["audioBuild"]["cacheKey"]
    )


def _backend_products_are_reusable(attestation: dict[str, Any]) -> bool:
    products = attestation["cacheContext"]["backendProducts"]
    return (
        products["coldProductsUsed"] is False
        and (
            products["sidecarPrebuilt"] is True
            or products["runtime"]["validated"] == "true"
        )
    )


def _safeguards_are_safe(attestation: dict[str, Any]) -> bool:
    safeguards = attestation["safeguards"]
    cache_environment = safeguards["cacheAndPublicationEnvironment"]
    return (
        safeguards["featureBranchOnly"] is True
        and safeguards["refreshReleaseCacheArtifacts"] is False
        and safeguards["cacheHitForbidden"] is True
        and safeguards["publicationForbidden"] is True
        and safeguards["tauriConfigPresent"] is False
        and all(cache_environment[field] == "false" for field in CACHE_AND_PUBLICATION_FLAGS)
        and safeguards["cacheAndPublicationFlagsAllFalse"] is True
    )


def _attestation_summary(attestation: dict[str, Any]) -> dict[str, Any]:
    self_test_bytes = _canonical_json_bytes(attestation["selfTest"])
    return {
        "generatedAtUtc": attestation["generatedAtUtc"],
        "mode": attestation["mode"],
        "runIdentity": attestation["runIdentity"],
        "executable": attestation["executable"],
        "audioBuild": attestation["audioBuild"],
        "overlapSetup": attestation["overlapSetup"],
        "runner": attestation["runner"],
        "environment": attestation["environment"],
        "cargoTarget": attestation["cargoTarget"],
        "cacheContext": attestation["cacheContext"],
        "toolchain": attestation["toolchain"],
        "selfTestCanonicalJsonSha256": _sha256(self_test_bytes),
        "safeguards": attestation["safeguards"],
    }


def _attestation_pair_report(
    left_attestation: dict[str, Any],
    right_attestation: dict[str, Any],
    left_pe: PeAnalysis,
    right_pe: PeAnalysis,
) -> dict[str, Any]:
    modes = [left_attestation["mode"], right_attestation["mode"]]
    sequential = next(
        (item for item in (left_attestation, right_attestation) if item["mode"] == "sequential"),
        None,
    )
    overlap = next(
        (item for item in (left_attestation, right_attestation) if item["mode"] == "overlap"),
        None,
    )
    left_self_test_bytes = _canonical_json_bytes(left_attestation["selfTest"])
    right_self_test_bytes = _canonical_json_bytes(right_attestation["selfTest"])
    checks = {
        "modesExactlySequentialAndOverlap": sorted(modes) == ["overlap", "sequential"],
        "sequentialGeneratedBeforeOverlap": sequential is not None
        and overlap is not None
        and _require_utc_timestamp(
            sequential["generatedAtUtc"], "sequential.generatedAtUtc"
        )
        < _require_utc_timestamp(overlap["generatedAtUtc"], "overlap.generatedAtUtc"),
        "repositoryEqual": left_attestation["runIdentity"]["repository"]
        == right_attestation["runIdentity"]["repository"],
        "sourceCommitEqual": left_attestation["runIdentity"]["sourceCommit"]
        == right_attestation["runIdentity"]["sourceCommit"],
        "refEqual": left_attestation["runIdentity"]["ref"]
        == right_attestation["runIdentity"]["ref"],
        "runIdsDistinct": left_attestation["runIdentity"]["runId"]
        != right_attestation["runIdentity"]["runId"],
        "audioCacheKeyEqual": left_attestation["audioBuild"]["cacheKey"]
        == right_attestation["audioBuild"]["cacheKey"],
        "runnerWindowsX64": _runner_is_safe(left_attestation)
        and _runner_is_safe(right_attestation),
        "runnerEqual": left_attestation["runner"] == right_attestation["runner"],
        "environmentSafe": _environment_is_safe(left_attestation)
        and _environment_is_safe(right_attestation),
        "environmentEqual": left_attestation["environment"]
        == right_attestation["environment"],
        "cargoTargetSafe": _cargo_target_is_safe(left_attestation)
        and _cargo_target_is_safe(right_attestation),
        "cargoTargetEqual": left_attestation["cargoTarget"]
        == right_attestation["cargoTarget"],
        "rustDependenciesCacheSafe": _rust_dependencies_cache_is_safe(
            left_attestation
        )
        and _rust_dependencies_cache_is_safe(right_attestation),
        "tauriAppBinaryCacheSafe": _tauri_app_cache_is_safe(left_attestation)
        and _tauri_app_cache_is_safe(right_attestation),
        "audioSidecarCacheSafe": _audio_sidecar_cache_is_safe(left_attestation)
        and _audio_sidecar_cache_is_safe(right_attestation),
        "backendProductsReusable": _backend_products_are_reusable(left_attestation)
        and _backend_products_are_reusable(right_attestation),
        "cacheContextEqual": left_attestation["cacheContext"]
        == right_attestation["cacheContext"],
        "toolchainEqual": left_attestation["toolchain"]
        == right_attestation["toolchain"],
        "safeguardsSafe": _safeguards_are_safe(left_attestation)
        and _safeguards_are_safe(right_attestation),
        "safeguardsEqual": left_attestation["safeguards"]
        == right_attestation["safeguards"],
        "bothCacheHitFalse": left_attestation["audioBuild"]["cacheHit"] is False
        and right_attestation["audioBuild"]["cacheHit"] is False,
        "bothIsolatedCargoTargetFalse": left_attestation["audioBuild"][
            "isolatedCargoTarget"
        ]
        is False
        and right_attestation["audioBuild"]["isolatedCargoTarget"] is False,
        "leftExecutableIdentityMatches": left_attestation["executable"]["sha256"]
        == left_pe.report["rawSha256"]
        and left_attestation["executable"]["length"] == len(left_pe.raw),
        "rightExecutableIdentityMatches": right_attestation["executable"]["sha256"]
        == right_pe.report["rawSha256"]
        and right_attestation["executable"]["length"] == len(right_pe.raw),
        "sequentialSetupExact": sequential is not None
        and _sequential_setup_matches(sequential),
        "overlapSetupExact": overlap is not None and _overlap_setup_matches(overlap),
        "selfTestStructureEqual": left_attestation["selfTest"]
        == right_attestation["selfTest"],
        "selfTestCanonicalJsonBytesEqual": left_self_test_bytes
        == right_self_test_bytes,
    }
    return {
        "equivalent": all(checks.values()),
        "checks": checks,
        "left": _attestation_summary(left_attestation),
        "right": _attestation_summary(right_attestation),
    }


def compare_pe_binaries(
    left_path: Path,
    right_path: Path,
    left_attestation_path: Path,
    right_attestation_path: Path,
) -> dict[str, Any]:
    """Return the complete mechanical PE comparison report."""

    left_attestation = _read_attestation(Path(left_attestation_path))
    right_attestation = _read_attestation(Path(right_attestation_path))
    left = _analyse(Path(left_path))
    right = _analyse(Path(right_path))
    diff = _diff_report(left, right)
    left_report = left.report
    right_report = right.report
    attestation = _attestation_pair_report(
        left_attestation, right_attestation, left, right
    )

    checks = {
        "sameLength": len(left.raw) == len(right.raw),
        "rawEqual": left_report["rawSha256"] == right_report["rawSha256"],
        "normalizedEqual": left_report["normalizedSha256"]
        == right_report["normalizedSha256"],
        "normalizationLayoutEqual": left_report["normalizationRanges"]
        == right_report["normalizationRanges"],
        "peInvariantsEqual": left_report["peInvariants"]
        == right_report["peInvariants"],
        "sectionLayoutEqual": _section_layout(left_report)
        == _section_layout(right_report),
        "sectionNormalizedHashesEqual": [
            section["normalizedSha256"] for section in left_report["sections"]
        ]
        == [section["normalizedSha256"] for section in right_report["sections"]],
        "importsEqual": left_report["imports"] == right_report["imports"],
        "exportsEqual": left_report["exports"] == right_report["exports"],
        "resourcesEqual": left_report["resources"] == right_report["resources"],
        "allowedDifferencesOnly": bool(diff["allowedDifferencesOnly"]),
        "attestationEquivalent": bool(attestation["equivalent"]),
    }
    equivalent = all(
        checks[name]
        for name in (
            "sameLength",
            "normalizedEqual",
            "normalizationLayoutEqual",
            "peInvariantsEqual",
            "sectionLayoutEqual",
            "sectionNormalizedHashesEqual",
            "importsEqual",
            "exportsEqual",
            "resourcesEqual",
            "allowedDifferencesOnly",
            "attestationEquivalent",
        )
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "equivalent": equivalent,
        "attestationEquivalent": bool(attestation["equivalent"]),
        "attestation": attestation,
        "left": left_report,
        "right": right_report,
        "comparison": {"checks": checks, "diff": diff},
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two PE binaries using Scriber's strict metadata oracle."
    )
    parser.add_argument("--left", required=True, type=Path)
    parser.add_argument("--right", required=True, type=Path)
    parser.add_argument("--left-attestation", required=True, type=Path)
    parser.add_argument("--right-attestation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        payload = compare_pe_binaries(
            args.left,
            args.right,
            args.left_attestation,
            args.right_attestation,
        )
        exit_code = 0 if payload["equivalent"] else 1
    except (ComparisonError, OSError, ValueError) as exc:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "equivalent": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        exit_code = 2

    try:
        _write_report(args.output, payload)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "equivalent": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
