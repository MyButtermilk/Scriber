from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path

import pytest

from scripts.ci import compare_pe_binaries as pe_compare


PE_OFFSET = 0x80
COFF_TIMESTAMP_OFFSET = PE_OFFSET + 8
OPTIONAL_OFFSET = PE_OFFSET + 24
CHECKSUM_OFFSET = OPTIONAL_OFFSET + 64
TEXT_RAW_OFFSET = 0x200
RDATA_RAW_OFFSET = 0x400
DEBUG_TIMESTAMP_OFFSET = RDATA_RAW_OFFSET + 4
RSDS_OFFSET = RDATA_RAW_OFFSET + 0x20
RSDS_GUID_OFFSET = RSDS_OFFSET + 4
NON_VOLATILE_RDATA_OFFSET = RDATA_RAW_OFFSET + 0x80
RESOURCE_RAW_OFFSET = 0x600
RESOURCE_CONTENT_OFFSET = RESOURCE_RAW_OFFSET + 0x60
REPOSITORY = "MyButtermilk/Scriber"
SOURCE_COMMIT = "a" * 40
AUDIO_CACHE_KEY = "b" * 64
RUST_MANIFEST_FINGERPRINT = "1" * 64
RUST_HASH_FILES_FINGERPRINT = hashlib.sha256(
    bytes.fromhex(RUST_MANIFEST_FINGERPRINT)
).hexdigest()
RUST_EXPECTED_ACTIONS_KEY = (
    "scriber-rust-dependencies-v1-Windows-" + RUST_HASH_FILES_FINGERPRINT
)
TAURI_MANIFEST_FINGERPRINT = "2" * 64
TAURI_HASH_FILES_FINGERPRINT = hashlib.sha256(
    bytes.fromhex(TAURI_MANIFEST_FINGERPRINT)
).hexdigest()
TAURI_EXPECTED_ACTIONS_KEY = (
    "scriber-tauri-app-binary-v3-Windows-" + TAURI_HASH_FILES_FINGERPRINT
)
TOOLCHAIN = {
    "rustc": "rustc 1.97.0 (fixture)\nhost: x86_64-pc-windows-msvc",
    "cargo": "cargo 1.97.0 (fixture)",
    "linker": {
        "family": "msvc-link",
        "versionDirectory": "14.44.35207",
        "relativePath": "VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/link.exe",
        "sha256": "6" * 64,
        "length": 2_048_000,
        "fileVersion": "14.44.35211.0",
    },
    "resourceCompiler": {
        "family": "windows-sdk-rc",
        "versionDirectory": "10.0.26100.0",
        "relativePath": "bin/10.0.26100.0/x64/rc.exe",
        "sha256": "7" * 64,
        "length": 128_000,
        "fileVersion": "10.0.26100.1",
    },
}
SELF_TEST = {
    "ok": True,
    "sidecar": "scriber-audio-sidecar",
    "protocolVersion": "1",
}


def _write_section_header(
    data: bytearray,
    offset: int,
    *,
    name: bytes,
    virtual_size: int,
    virtual_address: int,
    raw_size: int,
    raw_offset: int,
    characteristics: int,
) -> None:
    struct.pack_into(
        "<8sIIIIIIHHI",
        data,
        offset,
        name.ljust(8, b"\0"),
        virtual_size,
        virtual_address,
        raw_size,
        raw_offset,
        0,
        0,
        0,
        0,
        characteristics,
    )


def _pe_fixture() -> bytearray:
    data = bytearray(0x800)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, PE_OFFSET)
    data[PE_OFFSET : PE_OFFSET + 4] = b"PE\0\0"

    # IMAGE_FILE_HEADER: x64, three sections, PE32+ optional header.
    struct.pack_into(
        "<HHIIIHH",
        data,
        PE_OFFSET + 4,
        0x8664,
        3,
        0x65010203,
        0,
        0,
        0xF0,
        0x22,
    )

    # IMAGE_OPTIONAL_HEADER64. Fields not needed by the fixture remain zero.
    struct.pack_into("<HBB", data, OPTIONAL_OFFSET, 0x20B, 14, 51)
    struct.pack_into("<III", data, OPTIONAL_OFFSET + 4, 0x200, 0x400, 0)
    struct.pack_into("<II", data, OPTIONAL_OFFSET + 16, 0x1000, 0x1000)
    struct.pack_into("<Q", data, OPTIONAL_OFFSET + 24, 0x140000000)
    struct.pack_into("<II", data, OPTIONAL_OFFSET + 32, 0x1000, 0x200)
    struct.pack_into("<HH", data, OPTIONAL_OFFSET + 40, 6, 0)
    struct.pack_into("<HH", data, OPTIONAL_OFFSET + 44, 0, 0)
    struct.pack_into("<HH", data, OPTIONAL_OFFSET + 48, 6, 0)
    struct.pack_into("<I", data, OPTIONAL_OFFSET + 52, 0)
    struct.pack_into("<II", data, OPTIONAL_OFFSET + 56, 0x4000, 0x200)
    struct.pack_into("<I", data, CHECKSUM_OFFSET, 0xA1B2C3D4)
    struct.pack_into("<HH", data, OPTIONAL_OFFSET + 68, 3, 0x8160)
    struct.pack_into("<QQQQ", data, OPTIONAL_OFFSET + 72, 0x100000, 0x1000, 0x100000, 0x1000)
    struct.pack_into("<II", data, OPTIONAL_OFFSET + 104, 0, 16)

    data_directory_offset = OPTIONAL_OFFSET + 112
    struct.pack_into("<II", data, data_directory_offset + 2 * 8, 0x3000, 0x80)
    struct.pack_into("<II", data, data_directory_offset + 6 * 8, 0x2000, 28)

    section_table = OPTIONAL_OFFSET + 0xF0
    _write_section_header(
        data,
        section_table,
        name=b".text",
        virtual_size=0x40,
        virtual_address=0x1000,
        raw_size=0x200,
        raw_offset=TEXT_RAW_OFFSET,
        characteristics=0x60000020,
    )
    _write_section_header(
        data,
        section_table + 40,
        name=b".rdata",
        virtual_size=0xA0,
        virtual_address=0x2000,
        raw_size=0x200,
        raw_offset=RDATA_RAW_OFFSET,
        characteristics=0x40000040,
    )
    _write_section_header(
        data,
        section_table + 80,
        name=b".rsrc",
        virtual_size=0x80,
        virtual_address=0x3000,
        raw_size=0x200,
        raw_offset=RESOURCE_RAW_OFFSET,
        characteristics=0x40000040,
    )

    data[TEXT_RAW_OFFSET : TEXT_RAW_OFFSET + 16] = b"\x48\x31\xc0\xc3" + b"TEXT-CODE!!!"

    rsds = (
        b"RSDS"
        + bytes.fromhex("00112233445566778899aabbccddeeff")
        + struct.pack("<I", 1)
        + b"fixture.pdb\0"
    )
    struct.pack_into(
        "<IIHHIIII",
        data,
        RDATA_RAW_OFFSET,
        0,
        0x65010203,
        0,
        0,
        pe_compare.IMAGE_DEBUG_TYPE_CODEVIEW,
        len(rsds),
        0x2020,
        RSDS_OFFSET,
    )
    data[RSDS_OFFSET : RSDS_OFFSET + len(rsds)] = rsds
    non_volatile_rdata = b"NONVOLATILE-RDATA-BYTES"
    data[
        NON_VOLATILE_RDATA_OFFSET : NON_VOLATILE_RDATA_OFFSET
        + len(non_volatile_rdata)
    ] = non_volatile_rdata

    # Three-level resource tree: RT_RCDATA / id 1 / language 1033.
    struct.pack_into("<IIHHHH", data, RESOURCE_RAW_OFFSET, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", data, RESOURCE_RAW_OFFSET + 0x10, 10, 0x80000018)
    struct.pack_into("<IIHHHH", data, RESOURCE_RAW_OFFSET + 0x18, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", data, RESOURCE_RAW_OFFSET + 0x28, 1, 0x80000030)
    struct.pack_into("<IIHHHH", data, RESOURCE_RAW_OFFSET + 0x30, 0, 0, 0, 0, 0, 1)
    struct.pack_into("<II", data, RESOURCE_RAW_OFFSET + 0x40, 1033, 0x48)
    resource = b"RESOURCE-FIXTURE"
    struct.pack_into(
        "<IIII",
        data,
        RESOURCE_RAW_OFFSET + 0x48,
        0x3060,
        len(resource),
        0,
        0,
    )
    data[RESOURCE_CONTENT_OFFSET : RESOURCE_CONTENT_OFFSET + len(resource)] = resource
    return data


def _write_fixture(tmp_path: Path, name: str, data: bytearray | None = None) -> Path:
    path = tmp_path / name
    path.write_bytes(bytes(_pe_fixture() if data is None else data))
    return path


def _attestation_payload(mode: str, binary: Path) -> dict[str, object]:
    binary_bytes = binary.read_bytes()
    overlap = mode == "overlap"
    return {
        "schemaVersion": 1,
        "generatedAtUtc": (
            "2026-07-18T00:01:00.0000000Z"
            if overlap
            else "2026-07-18T00:00:00.0000000Z"
        ),
        "diagnosticOnly": True,
        "mode": mode,
        "runIdentity": {
            "repository": REPOSITORY,
            "runId": 101 if mode == "sequential" else 102,
            "sourceCommit": SOURCE_COMMIT,
            "ref": "refs/heads/codex/installer-autoresearch",
        },
        "executable": {
            "fileName": binary.name,
            "sha256": hashlib.sha256(binary_bytes).hexdigest(),
            "length": len(binary_bytes),
        },
        "audioBuild": {
            "cacheHit": False,
            "cacheKey": AUDIO_CACHE_KEY,
            "isolatedCargoTarget": False,
            "durationMs": 1000,
            "parallel": overlap,
            "sharedCargoTarget": overlap,
            "phaseSchema": (
                "explicit-overlap"
                if overlap
                else "sequential-core-without-parallel-properties"
            ),
            "phaseParallelPropertyPresent": overlap,
            "phaseSharedCargoTargetPropertyPresent": overlap,
        },
        "overlapSetup": {
            "sharedCargoTarget": overlap,
            "backendResourcePlaceholderCreated": overlap,
            "backendResourcePlaceholderInitialFileCount": 0 if overlap else None,
            "childTauriConfigPresent": False if overlap else None,
            "backendStagedOutsideResourcePath": overlap,
            "backendPromotedAfterAudio": overlap,
        },
        "runner": {
            "runnerOS": "Windows",
            "runnerArch": "X64",
            "imageOS": "win25",
            "imageVersion": "20260713.1.0",
        },
        "environment": {
            "RUSTFLAGS": {"present": False, "value": None},
            "CARGO_ENCODED_RUSTFLAGS": {"present": False, "value": None},
            "CARGO_INCREMENTAL": {"present": True, "value": "1"},
            "CARGO_TARGET_DIR": {"present": False, "value": None},
            "tauriConfigPresent": False,
        },
        "cargoTarget": {
            "relativePath": "Frontend/src-tauri/target",
            "metadataApiVersion": "1",
            "metadataCacheKey": AUDIO_CACHE_KEY,
            "metadataCacheHit": False,
            "isolated": False,
        },
        "cacheContext": {
            "rustDependencies": {
                "manifestFingerprint": RUST_MANIFEST_FINGERPRINT,
                "hashFilesFingerprint": RUST_HASH_FILES_FINGERPRINT,
                "expectedActionsKey": RUST_EXPECTED_ACTIONS_KEY,
                "matchedActionsKey": RUST_EXPECTED_ACTIONS_KEY,
                "actionsCacheExact": True,
                "effectiveRestoreSource": "actions-cache-exact",
                "releaseArtifactExact": "false",
                "releaseArtifactRestored": "false",
                "releaseArtifactImported": "false",
            },
            "tauriAppBinary": {
                "manifestFingerprint": TAURI_MANIFEST_FINGERPRINT,
                "hashFilesFingerprint": TAURI_HASH_FILES_FINGERPRINT,
                "expectedActionsKey": TAURI_EXPECTED_ACTIONS_KEY,
                "actionsCacheExact": True,
                "importUsable": True,
                "importedSha256": "3" * 64,
                "effectiveRestoreSource": "actions-cache-exact-validated",
            },
            "audioSidecar": {
                "actions": "miss",
                "releaseArtifact": "false",
                "effectiveRestoreSource": "miss",
                "internalCacheHit": False,
                "internalCacheKey": AUDIO_CACHE_KEY,
            },
            "backendProducts": {
                "sidecar": {
                    "fingerprint": "4" * 64,
                    "actions": "exact",
                    "releaseArtifact": "false",
                    "effectiveRestoreSource": "actions-cache-exact",
                },
                "runtime": {
                    "fingerprint": "5" * 64,
                    "actions": "miss",
                    "releaseArtifact": "false",
                    "effectiveRestoreSource": "miss",
                    "validated": "false",
                },
                "sidecarPrebuilt": True,
                "coldProductsUsed": False,
            },
        },
        "selfTest": dict(SELF_TEST),
        "toolchain": copy.deepcopy(TOOLCHAIN),
        "safeguards": {
            "featureBranchOnly": True,
            "refreshReleaseCacheArtifacts": False,
            "cacheHitForbidden": True,
            "publicationForbidden": True,
            "tauriConfigPresent": False,
            "cacheAndPublicationEnvironment": {
                "SCRIBER_SAVE_ACTIONS_CACHES": "false",
                "SCRIBER_SAVE_REF_LOCAL_TAURI_CACHE": "false",
                "SCRIBER_SAVE_REF_LOCAL_DESKTOP_INCREMENTAL_CACHE": "false",
                "SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS": "false",
                "SCRIBER_PUBLISH_FINISHED_COMPONENT_CACHE_ARTIFACTS": "false",
                "SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS": "false",
            },
            "cacheAndPublicationFlagsAllFalse": True,
        },
    }


def _write_attestation(
    tmp_path: Path, name: str, payload: dict[str, object]
) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _attested_pair(
    tmp_path: Path, left: Path, right: Path
) -> tuple[Path, Path]:
    return (
        _write_attestation(
            tmp_path,
            "sequential-attestation.json",
            _attestation_payload("sequential", left),
        ),
        _write_attestation(
            tmp_path,
            "overlap-attestation.json",
            _attestation_payload("overlap", right),
        ),
    )


def test_same_file_is_equivalent_and_reports_pe_structure(tmp_path: Path) -> None:
    binary = _write_fixture(tmp_path, "same.exe")
    left_attestation, right_attestation = _attested_pair(tmp_path, binary, binary)

    report = pe_compare.compare_pe_binaries(
        binary, binary, left_attestation, right_attestation
    )

    assert report["equivalent"] is True
    assert report["attestationEquivalent"] is True
    assert all(report["attestation"]["checks"].values())
    assert set(report["attestation"]["left"]) == {
        "generatedAtUtc",
        "mode",
        "runIdentity",
        "executable",
        "audioBuild",
        "overlapSetup",
        "runner",
        "environment",
        "cargoTarget",
        "cacheContext",
        "toolchain",
        "selfTestCanonicalJsonSha256",
        "safeguards",
    }
    assert report["comparison"]["checks"]["rawEqual"] is True
    assert report["comparison"]["diff"]["rangeCount"] == 0
    assert [section["name"] for section in report["left"]["sections"]] == [
        ".text",
        ".rdata",
        ".rsrc",
    ]
    assert report["left"]["imports"] == {"normal": [], "delay": []}
    assert report["left"]["exports"] == []
    assert len(report["left"]["resources"]["leaves"]) == 1
    assert report["left"]["resources"]["leaves"][0]["path"][0][
        "resourceType"
    ] == "RT_RCDATA"


def test_only_allowed_linker_metadata_differences_are_equivalent(tmp_path: Path) -> None:
    left_data = _pe_fixture()
    right_data = bytearray(left_data)
    struct.pack_into("<I", right_data, COFF_TIMESTAMP_OFFSET, 0x76020304)
    struct.pack_into("<I", right_data, CHECKSUM_OFFSET, 0x01020304)
    struct.pack_into("<I", right_data, DEBUG_TIMESTAMP_OFFSET, 0x76020304)
    right_data[RSDS_GUID_OFFSET : RSDS_GUID_OFFSET + 16] = bytes(range(16))
    left = _write_fixture(tmp_path, "metadata-left.exe", left_data)
    right = _write_fixture(tmp_path, "metadata-right.exe", right_data)
    left_attestation, right_attestation = _attested_pair(tmp_path, left, right)

    report = pe_compare.compare_pe_binaries(
        left, right, left_attestation, right_attestation
    )

    assert report["equivalent"] is True
    assert report["comparison"]["checks"]["rawEqual"] is False
    assert report["comparison"]["checks"]["normalizedEqual"] is True
    assert report["comparison"]["diff"]["allowedDifferencesOnly"] is True
    changed_kinds = {
        kind
        for item in report["comparison"]["diff"]["ranges"]
        for kind in item["kinds"]
        if kind.startswith("coff.")
        or kind.startswith("optionalHeader.")
        or kind.startswith("debug[")
    }
    assert changed_kinds == {
        "coff.timeDateStamp",
        "optionalHeader.checkSum",
        "debug[0].timeDateStamp",
        "debug[0].rsdsGuid",
    }
    assert report["left"]["peInvariants"] == report["right"]["peInvariants"]
    assert report["left"]["resources"] == report["right"]["resources"]


def test_text_difference_is_rejected(tmp_path: Path) -> None:
    left_data = _pe_fixture()
    right_data = bytearray(left_data)
    right_data[TEXT_RAW_OFFSET + 1] ^= 0x7F
    left = _write_fixture(tmp_path, "text-left.exe", left_data)
    right = _write_fixture(tmp_path, "text-right.exe", right_data)
    left_attestation, right_attestation = _attested_pair(tmp_path, left, right)

    report = pe_compare.compare_pe_binaries(
        left, right, left_attestation, right_attestation
    )

    assert report["equivalent"] is False
    assert report["attestationEquivalent"] is True
    assert report["comparison"]["checks"]["normalizedEqual"] is False
    assert report["comparison"]["diff"]["allowedDifferencesOnly"] is False
    assert "section:.text" in report["comparison"]["diff"]["ranges"][0]["kinds"]


def test_resource_difference_is_rejected(tmp_path: Path) -> None:
    left_data = _pe_fixture()
    right_data = bytearray(left_data)
    right_data[RESOURCE_CONTENT_OFFSET + 2] ^= 0x01
    left = _write_fixture(tmp_path, "resource-left.exe", left_data)
    right = _write_fixture(tmp_path, "resource-right.exe", right_data)
    left_attestation, right_attestation = _attested_pair(tmp_path, left, right)

    report = pe_compare.compare_pe_binaries(
        left, right, left_attestation, right_attestation
    )

    assert report["equivalent"] is False
    assert report["comparison"]["checks"]["resourcesEqual"] is False
    assert report["comparison"]["checks"]["normalizedEqual"] is False
    kinds = report["comparison"]["diff"]["ranges"][0]["kinds"]
    assert "section:.rsrc" in kinds
    assert "resource:10/1/1033" in kinds


def test_non_normalized_rdata_difference_is_rejected(tmp_path: Path) -> None:
    left_data = _pe_fixture()
    right_data = bytearray(left_data)
    right_data[NON_VOLATILE_RDATA_OFFSET + 3] ^= 0x20
    left = _write_fixture(tmp_path, "rdata-left.exe", left_data)
    right = _write_fixture(tmp_path, "rdata-right.exe", right_data)
    left_attestation, right_attestation = _attested_pair(tmp_path, left, right)

    report = pe_compare.compare_pe_binaries(
        left, right, left_attestation, right_attestation
    )

    assert report["equivalent"] is False
    assert report["comparison"]["checks"]["normalizedEqual"] is False
    assert report["comparison"]["checks"]["resourcesEqual"] is True
    assert "section:.rdata" in report["comparison"]["diff"]["ranges"][0]["kinds"]


def test_cli_exit_code_follows_equivalence_and_writes_json(tmp_path: Path) -> None:
    left = _write_fixture(tmp_path, "cli-left.exe")
    left_attestation, right_attestation = _attested_pair(tmp_path, left, left)
    equivalent_output = tmp_path / "equivalent.json"
    assert (
        pe_compare.main(
            [
                "--left",
                str(left),
                "--right",
                str(left),
                "--left-attestation",
                str(left_attestation),
                "--right-attestation",
                str(right_attestation),
                "--output",
                str(equivalent_output),
            ]
        )
        == 0
    )
    assert json.loads(equivalent_output.read_text(encoding="utf-8"))["equivalent"] is True

    different_data = _pe_fixture()
    different_data[TEXT_RAW_OFFSET] ^= 0x01
    different = _write_fixture(tmp_path, "cli-different.exe", different_data)
    left_attestation, right_attestation = _attested_pair(tmp_path, left, different)
    different_output = tmp_path / "different.json"
    assert (
        pe_compare.main(
            [
                "--left",
                str(left),
                "--right",
                str(different),
                "--left-attestation",
                str(left_attestation),
                "--right-attestation",
                str(right_attestation),
                "--output",
                str(different_output),
            ]
        )
        == 1
    )
    assert json.loads(different_output.read_text(encoding="utf-8"))[
        "equivalent"
    ] is False


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _mutate_pair_gate(
    case: str,
    left: dict[str, object],
    right: dict[str, object],
) -> None:
    left_run = _mapping(left["runIdentity"])
    right_run = _mapping(right["runIdentity"])
    left_audio = _mapping(left["audioBuild"])
    right_audio = _mapping(right["audioBuild"])
    left_setup = _mapping(left["overlapSetup"])
    right_setup = _mapping(right["overlapSetup"])

    if case == "mode":
        right["mode"] = "sequential"
    elif case == "repository":
        right_run["repository"] = "MyButtermilk/Other"
    elif case == "sourceCommit":
        right_run["sourceCommit"] = "c" * 40
    elif case == "ref":
        right_run["ref"] = "refs/heads/codex/other-audio-diagnostic"
    elif case == "runId":
        right_run["runId"] = left_run["runId"]
    elif case == "cacheKey":
        right_audio["cacheKey"] = "d" * 64
    elif case == "toolchain":
        _mapping(right["toolchain"])["cargo"] = "cargo 1.98.0 (fixture)"
    elif case == "cacheHit":
        right_audio["cacheHit"] = True
    elif case == "isolatedCargoTarget":
        right_audio["isolatedCargoTarget"] = True
    elif case == "sequentialSetup":
        left_setup["backendPromotedAfterAudio"] = True
    elif case == "overlapSetup":
        right_setup["backendResourcePlaceholderCreated"] = False
    elif case == "selfTest":
        right["selfTest"] = {**SELF_TEST, "protocolVersion": "2"}
    elif case == "executableSha":
        right_executable = _mapping(right["executable"])
        right_executable["sha256"] = "e" * 64
    elif case == "executableLength":
        right_executable = _mapping(right["executable"])
        right_executable["length"] = int(right_executable["length"]) + 1
    elif case == "runnerImageVersion":
        _mapping(right["runner"])["imageVersion"] = "20260714.1.0"
    elif case == "linkerSha":
        right_toolchain = _mapping(right["toolchain"])
        _mapping(right_toolchain["linker"])["sha256"] = "8" * 64
    elif case == "rustMatchedKey":
        right_cache = _mapping(right["cacheContext"])
        rust_cache = _mapping(right_cache["rustDependencies"])
        rust_cache["matchedActionsKey"] = (
            "scriber-rust-dependencies-v1-Windows-" + "8" * 64
        )
    elif case == "rustHashFilesFingerprint":
        right_cache = _mapping(right["cacheContext"])
        _mapping(right_cache["rustDependencies"])["hashFilesFingerprint"] = "8" * 64
    elif case == "tauriExpectedKey":
        right_cache = _mapping(right["cacheContext"])
        _mapping(right_cache["tauriAppBinary"])["expectedActionsKey"] = (
            "scriber-tauri-app-binary-v3-Windows-" + "9" * 64
        )
    elif case == "audioRestore":
        right_cache = _mapping(right["cacheContext"])
        _mapping(right_cache["audioSidecar"])["releaseArtifact"] = "true"
    elif case == "audioEffective":
        right_cache = _mapping(right["cacheContext"])
        _mapping(right_cache["audioSidecar"])[
            "effectiveRestoreSource"
        ] = "actions-cache-exact"
    elif case == "tauriConfig":
        _mapping(right["environment"])["tauriConfigPresent"] = True
    elif case == "cargoIncremental":
        right_environment = _mapping(right["environment"])
        _mapping(right_environment["CARGO_INCREMENTAL"])["value"] = "0"
    elif case == "cacheFlag":
        right_safeguards = _mapping(right["safeguards"])
        cache_environment = _mapping(
            right_safeguards["cacheAndPublicationEnvironment"]
        )
        cache_environment["SCRIBER_SAVE_ACTIONS_CACHES"] = "true"
    elif case == "cargoTarget":
        _mapping(right["cargoTarget"])[
            "relativePath"
        ] = "Frontend/src-tauri/target-other"
    elif case == "phaseSchema":
        right_audio["phaseSchema"] = "explicit-sequential"
    elif case == "runOrder":
        left["generatedAtUtc"] = "2026-07-18T00:01:00.0000002Z"
        right["generatedAtUtc"] = "2026-07-18T00:01:00.0000001Z"
    else:  # pragma: no cover - the parameter list is the closed set.
        raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "failed_check"),
    [
        ("mode", "modesExactlySequentialAndOverlap"),
        ("repository", "repositoryEqual"),
        ("sourceCommit", "sourceCommitEqual"),
        ("ref", "refEqual"),
        ("runId", "runIdsDistinct"),
        ("cacheKey", "audioCacheKeyEqual"),
        ("toolchain", "toolchainEqual"),
        ("cacheHit", "bothCacheHitFalse"),
        ("isolatedCargoTarget", "bothIsolatedCargoTargetFalse"),
        ("sequentialSetup", "sequentialSetupExact"),
        ("overlapSetup", "overlapSetupExact"),
        ("selfTest", "selfTestStructureEqual"),
        ("executableSha", "rightExecutableIdentityMatches"),
        ("executableLength", "rightExecutableIdentityMatches"),
        ("runnerImageVersion", "runnerEqual"),
        ("linkerSha", "toolchainEqual"),
        ("rustMatchedKey", "rustDependenciesCacheSafe"),
        ("rustHashFilesFingerprint", "rustDependenciesCacheSafe"),
        ("tauriExpectedKey", "tauriAppBinaryCacheSafe"),
        ("audioRestore", "audioSidecarCacheSafe"),
        ("audioEffective", "audioSidecarCacheSafe"),
        ("tauriConfig", "environmentSafe"),
        ("cargoIncremental", "environmentSafe"),
        ("cacheFlag", "safeguardsSafe"),
        ("cargoTarget", "cargoTargetSafe"),
        ("phaseSchema", "overlapSetupExact"),
        ("runOrder", "sequentialGeneratedBeforeOverlap"),
    ],
)
def test_attestation_pair_gate_is_fail_closed(
    tmp_path: Path, case: str, failed_check: str
) -> None:
    binary = _write_fixture(tmp_path, f"{case}.exe")
    left_payload = _attestation_payload("sequential", binary)
    right_payload = _attestation_payload("overlap", binary)
    _mutate_pair_gate(case, left_payload, right_payload)
    left_attestation = _write_attestation(
        tmp_path, f"{case}-left-attestation.json", left_payload
    )
    right_attestation = _write_attestation(
        tmp_path, f"{case}-right-attestation.json", right_payload
    )

    report = pe_compare.compare_pe_binaries(
        binary, binary, left_attestation, right_attestation
    )

    assert report["comparison"]["checks"]["normalizedEqual"] is True
    assert report["attestationEquivalent"] is False
    assert report["comparison"]["checks"]["attestationEquivalent"] is False
    assert report["equivalent"] is False
    assert report["attestation"]["checks"][failed_check] is False
    assert not all(report["attestation"]["checks"].values())


@pytest.mark.parametrize(
    "case",
    [
        "invalidGeneratedAt",
        "missingRunnerImageVersion",
        "unexpectedEnvironmentKey",
        "invalidCargoTargetPath",
        "missingRustHashFilesFingerprint",
        "invalidLinkerSha",
        "invalidResourceCompilerLength",
        "invalidCacheFlagType",
    ],
)
def test_attestation_parser_rejects_malformed_new_schema(
    tmp_path: Path, case: str
) -> None:
    binary = _write_fixture(tmp_path, f"malformed-{case}.exe")
    payload = _attestation_payload("sequential", binary)

    if case == "invalidGeneratedAt":
        payload["generatedAtUtc"] = "2026-02-30T00:00:00Z"
    elif case == "missingRunnerImageVersion":
        del _mapping(payload["runner"])["imageVersion"]
    elif case == "unexpectedEnvironmentKey":
        _mapping(payload["environment"])["unexpected"] = {
            "present": False,
            "value": None,
        }
    elif case == "invalidCargoTargetPath":
        _mapping(payload["cargoTarget"])["relativePath"] = "../target"
    elif case == "missingRustHashFilesFingerprint":
        cache_context = _mapping(payload["cacheContext"])
        del _mapping(cache_context["rustDependencies"])["hashFilesFingerprint"]
    elif case == "invalidLinkerSha":
        toolchain = _mapping(payload["toolchain"])
        _mapping(toolchain["linker"])["sha256"] = "not-a-sha"
    elif case == "invalidResourceCompilerLength":
        toolchain = _mapping(payload["toolchain"])
        _mapping(toolchain["resourceCompiler"])["length"] = 0
    elif case == "invalidCacheFlagType":
        safeguards = _mapping(payload["safeguards"])
        cache_environment = _mapping(
            safeguards["cacheAndPublicationEnvironment"]
        )
        cache_environment["SCRIBER_SAVE_ACTIONS_CACHES"] = False
    else:  # pragma: no cover - the parameter list is the closed set.
        raise AssertionError(case)

    attestation = _write_attestation(
        tmp_path, f"malformed-{case}-attestation.json", payload
    )
    with pytest.raises(pe_compare.ComparisonError):
        pe_compare._read_attestation(attestation)
