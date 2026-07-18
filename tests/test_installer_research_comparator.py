from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from scripts.installer_research.comparator import (
    GATE_EVIDENCE_CONTRACT,
    MANDATORY_EXTERNAL_GATES,
    accept_baseline,
    evaluate_candidate,
)
from scripts.perf.installer_size.evaluator import validate_result
from scripts.installer_research.inventory import InventoryError


EVALUATOR_HASH = "1" * 64
TOOLCHAIN_HASH = "2" * 64
COMPONENT_MAP_HASH = "3" * 64
SEMANTIC_HASH = "4" * 64
FILE_LIST_HASH = "5" * 64
PYZ_HASH = "6" * 64
SOURCE_COMMIT = "7" * 40
RUN_ID = "12345678-1234-4234-8234-123456789abc"
TIMING_SHA = "d" * 64
REPO_ROOT = Path(__file__).resolve().parents[1]


def _component(raw_bytes: int) -> dict:
    return {
        "rawBytes": raw_bytes,
        "fileCount": 1,
        "allocationCount": 1,
        "fileListSha256": "8" * 64,
        "allocationListSha256": "9" * 64,
    }


def _tree(total_bytes: int, *, semantic_hash: str = SEMANTIC_HASH) -> dict:
    return {
        "totalBytes": total_bytes,
        "fileCount": 1,
        "exactTreeSha256": "a" * 64,
        "semanticTreeSha256": semantic_hash,
        "fileListSha256": FILE_LIST_HASH,
        "components": {"backend-executable": _component(total_bytes)},
        "componentBytesSum": total_bytes,
        "distributions": [],
        "unexpectedDistributions": [],
        "duplicateGroups": [],
        "files": [],
    }


def _inventory(
    *,
    installer_bytes: int = 100_000_000,
    staged_bytes: int = 200_000_000,
    installed_bytes: int | None = 201_000_000,
    semantic_hash: str = SEMANTIC_HASH,
    compression: str = "bzip2",
    run_id: str = RUN_ID,
    source_commit: str = SOURCE_COMMIT,
    replica_id: str = "packet-001",
    build_root_sha256: str = "a" * 64,
) -> dict:
    installed = _tree(installed_bytes, semantic_hash=semantic_hash) if installed_bytes is not None else None
    return {
        "inventoryContract": "InstallerResearchInventoryV1",
        "schemaVersion": 1,
        "generatedAtUtc": "2026-07-18T00:00:00Z",
        "ok": True,
        "reasonCodes": [],
        "runId": run_id,
        "sourceCommit": source_commit,
        "buildProvenance": {
            "replicaId": replica_id,
            "buildRootSha256": build_root_sha256,
        },
        "productVersion": "1.2.3",
        "compression": compression,
        "evaluatorHash": EVALUATOR_HASH,
        "toolchainHash": TOOLCHAIN_HASH,
        "componentMap": {
            "mapId": "installer-component-map-v1",
            "sha256": COMPONENT_MAP_HASH,
        },
        "installer": {
            "name": "Scriber_1.2.3_x64-setup.exe",
            "length": installer_bytes,
            "sha256": "b" * 64,
        },
        "payload": {
            "staged": _tree(staged_bytes, semantic_hash=semantic_hash),
            "installed": installed,
            "stagedInstalledParity": {
                "ok": installed is not None,
                "missingFromInstalled": [],
                "changedInInstalled": [],
                "installedOnly": ["uninstall.exe"] if installed is not None else [],
                "allowedInstalledOnly": ["uninstall.exe"] if installed is not None else [],
                "unexpectedInstalledOnly": [],
            }
            if installed is not None
            else None,
        },
        "backendExecutable": {
            "length": 1_000,
            "virtualPartitionBytes": 1_000,
            "pyinstallerVersion": "6.20.0",
            "regions": [
                {
                    "id": "pe-bootloader-prefix",
                    "rawBytes": 1_000,
                    "countedInStagedPayload": False,
                }
            ],
            "pyzDiagnostics": {
                "countedInStagedPayload": False,
                "inventorySha256": PYZ_HASH,
                "roots": {
                    "json": {
                        "compressedBytes": 100,
                        "moduleCount": 2,
                        "components": {
                            "python-runtime-other": {
                                "compressedBytes": 100,
                                "moduleCount": 2,
                            }
                        },
                    }
                },
            },
        },
    }


def _accepted_baseline() -> dict:
    first = _inventory(
        replica_id="baseline-replica-1", build_root_sha256="1" * 64
    )
    second = copy.deepcopy(first)
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }
    second["generatedAtUtc"] = "2026-07-18T00:01:00Z"
    second["installer"]["sha256"] = "c" * 64
    second["payload"]["staged"]["exactTreeSha256"] = "d" * 64
    return accept_baseline(first, second)


def _passing_gates(
    *,
    packet_id: str = "packet-001",
    parent_champion_id: str = "baseline",
    source_commit: str = SOURCE_COMMIT,
) -> dict:
    return {
        "gateEvidenceContract": GATE_EVIDENCE_CONTRACT,
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": packet_id,
        "parentChampionId": parent_champion_id,
        "sourceCommit": source_commit,
        "gates": {
            name: {
                "status": "pass",
                "evidenceSha256": f"{index:x}" * 64,
            }
            for index, name in enumerate(MANDATORY_EXTERNAL_GATES, start=1)
        },
    }


def _timing_evidence(
    parent: dict,
    candidate: dict,
    *,
    packet_id: str = "packet-001",
    parent_champion_id: str = "baseline",
    source_commit: str = SOURCE_COMMIT,
    baseline_values: list[int] | None = None,
    candidate_values: list[int] | None = None,
) -> dict:
    baseline_values = baseline_values or [1000] * 20
    candidate_values = candidate_values or [1050] * 20
    assert len(baseline_values) == len(candidate_values) == 20
    tree_hashes = {"baseline": "e" * 64, "candidate": "f" * 64}
    inventories = {
        "baseline": parent["payload"]["installed"],
        "candidate": candidate["payload"]["installed"],
    }
    samples: list[dict] = []
    sequence = 0
    for variant in ("baseline", "candidate"):
        sequence += 1
        installed = inventories[variant]
        installed_sha = "c" * 64 if variant == "baseline" else "d" * 64
        samples.append(
            {
                "sequence": sequence,
                "pair": None,
                "order": None,
                "position": None,
                "variant": variant,
                "warmup": True,
                "warmupIndex": 1,
                "stableInstallMs": 1000,
                "launcherExitMs": 900,
                "postExitCompletionMs": 100,
                "installedExe": "scriber-desktop.exe",
                "installedVersion": "1.2.3",
                "installedLength": 123,
                "installedSha256": installed_sha,
                "installedFileCount": installed["fileCount"],
                "installedTotalBytes": installed["totalBytes"],
                "installedTreeSha256": tree_hashes[variant],
                "inventoryDurationMs": 10,
            }
        )
    for pair in range(1, 21):
        order = ("baseline", "candidate") if pair % 2 else (
            "candidate",
            "baseline",
        )
        label = "AB" if pair % 2 else "BA"
        for position, variant in enumerate(order, start=1):
            sequence += 1
            values = baseline_values if variant == "baseline" else candidate_values
            installed = inventories[variant]
            installed_sha = "c" * 64 if variant == "baseline" else "d" * 64
            stable_ms = values[pair - 1]
            samples.append(
                {
                    "sequence": sequence,
                    "pair": pair,
                    "order": label,
                    "position": position,
                    "variant": variant,
                    "warmup": False,
                    "warmupIndex": None,
                    "stableInstallMs": stable_ms,
                    "launcherExitMs": 900,
                    "postExitCompletionMs": stable_ms - 900,
                    "installedExe": "scriber-desktop.exe",
                    "installedVersion": "1.2.3",
                    "installedLength": 123,
                    "installedSha256": installed_sha,
                    "installedFileCount": installed["fileCount"],
                    "installedTotalBytes": installed["totalBytes"],
                    "installedTreeSha256": tree_hashes[variant],
                    "inventoryDurationMs": 10,
                }
            )

    def stats(values: list[int]) -> dict:
        ordered = sorted(values)
        return {
            "count": 20,
            "p50Ms": (ordered[9] + ordered[10]) / 2,
            "p95Ms": ordered[18],
            "minimumMs": ordered[0],
            "maximumMs": ordered[-1],
        }

    return {
        "apiVersion": "1",
        "kind": "installer-ab-timing",
        "ok": True,
        "runId": RUN_ID,
        "packetId": packet_id,
        "parentChampionId": parent_champion_id,
        "sourceCommit": source_commit,
        "expectedVersion": candidate["productVersion"],
        "pairCount": 20,
        "warmupPerVariant": 1,
        "stableSamples": 3,
        "sampleIntervalMs": 750,
        "stopwatch": {"isHighResolution": True, "frequency": 10_000_000},
        "cleanup": {
            "outsideTimedIntervals": True,
            "invocationCount": 43,
            "events": [
                {
                    "durationMs": 10,
                    "uninstallerExitCode": None,
                    "forcedProcessCount": 0,
                    "filesRemoved": True,
                    "registryEntriesRemoved": 0,
                }
                for _ in range(43)
            ],
        },
        "cachePolicy": {"osFileCacheFlushed": False},
        "variants": {
            "baseline": {
                "installerName": parent["installer"]["name"],
                "length": parent["installer"]["length"],
                "sha256": parent["installer"]["sha256"],
            },
            "candidate": {
                "installerName": candidate["installer"]["name"],
                "length": candidate["installer"]["length"],
                "sha256": candidate["installer"]["sha256"],
            },
        },
        "samples": samples,
        "statistics": {
            "baseline": stats(baseline_values),
            "candidate": stats(candidate_values),
        },
        "inventoryConsistency": {
            variant: {
                "fileCount": inventories[variant]["fileCount"],
                "totalBytes": inventories[variant]["totalBytes"],
                "treeSha256": tree_hashes[variant],
                "sampleCount": 21,
            }
            for variant in ("baseline", "candidate")
        },
    }


def _evaluate(candidate: dict, **kwargs) -> dict:
    baseline = _accepted_baseline()
    packet_id = candidate["buildProvenance"]["replicaId"]
    gates = kwargs.pop("gate_results", _passing_gates(packet_id=packet_id))
    measurements = kwargs.pop(
        "install_measurements",
        _timing_evidence(baseline["inventory"], candidate, packet_id=packet_id),
    )
    return evaluate_candidate(
        baseline,
        candidate,
        run_id=RUN_ID,
        packet_id=packet_id,
        parent_champion_id="baseline",
        hypothesis="Remove one unused deterministic component.",
        source_commit=SOURCE_COMMIT,
        gate_results=gates,
        install_measurements=measurements,
        install_measurements_sha256=(
            kwargs.pop("install_measurements_sha256", TIMING_SHA)
            if measurements is not None
            else None
        ),
        **kwargs,
    )


def test_accept_baseline_allows_exact_hash_and_installer_hash_volatility() -> None:
    baseline = _accepted_baseline()

    assert baseline["baselineContract"] == "InstallerResearchBaselineV1"
    assert baseline["schemaVersion"] == 1
    assert baseline["accepted"] is True
    assert baseline["reasonCodes"] == []
    assert baseline["installerLength"] == 100_000_000
    assert len(baseline["replicas"]) == 2
    assert baseline["replicas"][0]["installerSha256"] != baseline["replicas"][1][
        "installerSha256"
    ]
    assert baseline["inventory"]["inventoryContract"] == "InstallerResearchInventoryV1"


def test_accept_baseline_rejects_semantic_or_component_drift() -> None:
    first = _inventory(
        replica_id="baseline-replica-1", build_root_sha256="1" * 64
    )
    second = _inventory(
        semantic_hash="f" * 64,
        replica_id="baseline-replica-2",
        build_root_sha256="2" * 64,
    )
    second["payload"]["staged"]["components"]["backend-executable"][
        "allocationListSha256"
    ] = "0" * 64

    baseline = accept_baseline(first, second)

    assert baseline["accepted"] is False
    assert "staged_semantic_tree_mismatch" in baseline["reasonCodes"]
    assert "staged_component_partition_mismatch" in baseline["reasonCodes"]


def test_accept_baseline_requires_release_equivalent_bzip2() -> None:
    first = _inventory(
        compression="lzma",
        replica_id="baseline-replica-1",
        build_root_sha256="1" * 64,
    )
    second = copy.deepcopy(first)
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }

    baseline = accept_baseline(first, second)

    assert baseline["accepted"] is False
    assert baseline["reasonCodes"] == ["baseline_compression_not_bzip2"]


def test_accept_baseline_requires_installed_payload_evidence() -> None:
    first = _inventory(
        installed_bytes=None,
        replica_id="baseline-replica-1",
        build_root_sha256="1" * 64,
    )
    second = copy.deepcopy(first)
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }

    baseline = accept_baseline(first, second)

    assert baseline["accepted"] is False
    assert "installed_inventory_missing" in baseline["reasonCodes"]


def test_accept_baseline_rejects_the_same_inventory_document_twice() -> None:
    inventory = _inventory(
        replica_id="baseline-replica-1", build_root_sha256="1" * 64
    )

    baseline = accept_baseline(inventory, inventory)

    assert baseline["accepted"] is False
    assert "inventory_document_not_distinct" in baseline["reasonCodes"]
    assert "replica_id_not_distinct" in baseline["reasonCodes"]
    assert "build_root_not_distinct" in baseline["reasonCodes"]


def test_evaluate_rejects_a_tampered_accepted_baseline_summary() -> None:
    baseline = _accepted_baseline()
    baseline["stagedBytes"] += 1

    with pytest.raises(InventoryError, match="summary disagrees"):
        evaluate_candidate(
            baseline,
            _inventory(installer_bytes=99_000_000),
            run_id=RUN_ID,
            packet_id="packet-001",
            parent_champion_id="baseline",
            hypothesis="Attempt to evaluate against a tampered baseline.",
            source_commit=SOURCE_COMMIT,
        )


def test_evaluate_keeps_exact_absolute_threshold_with_complete_gates() -> None:
    candidate = _inventory(
        installer_bytes=100_000_000 - 256 * 1024,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )

    result = _evaluate(candidate)

    assert result["resultContract"] == "InstallerResearchResultV1"
    assert result["schemaVersion"] == 1
    assert UUID(result["runId"]) == UUID(RUN_ID)
    assert result["decision"] == "keep"
    assert result["reasonCodes"] == []
    assert result["installer"]["deltaBytes"] == -(256 * 1024)
    assert result["payload"]["deltaBytes"] == -1_000_000
    assert result["gates"]["installerReduction"]["requiredReductionBytes"] == 256 * 1024
    assert result["gates"]["installerReduction"]["status"] == "pass"
    assert result["gates"]["installTimingRegression"]["p50RatioBasisPoints"] == 10_500
    assert result["gates"]["installTimingRegression"]["p95RatioBasisPoints"] == 10_500
    assert result["gates"]["combinedInstall50"]["status"] == "not_applicable"
    assert validate_result(result, expected_run_id=RUN_ID) == []


def test_evaluate_discards_one_byte_below_the_integer_threshold() -> None:
    candidate = _inventory(
        installer_bytes=100_000_000 - (256 * 1024) + 1,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )

    result = _evaluate(candidate)

    assert result["decision"] == "discard"
    assert result["reasonCodes"] == ["gate_failed:installerReduction"]


def test_one_arbitrary_external_pass_gate_cannot_authorize_keep() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    weak_evidence = {
        "gateEvidenceContract": GATE_EVIDENCE_CONTRACT,
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "packet-001",
        "parentChampionId": "baseline",
        "sourceCommit": SOURCE_COMMIT,
        "gates": {
            "arbitrary": {"status": "pass", "evidenceSha256": "e" * 64}
        },
    }

    result = _evaluate(candidate, gate_results=weak_evidence)

    assert result["decision"] == "measure_only"
    assert all(
        result["gates"][name]["status"] == "not_run"
        for name in MANDATORY_EXTERNAL_GATES
    )


def test_passing_external_gate_requires_an_evidence_hash() -> None:
    candidate = _inventory(installer_bytes=99_000_000)
    evidence = _passing_gates()
    del evidence["gates"][MANDATORY_EXTERNAL_GATES[0]]["evidenceSha256"]

    with pytest.raises(InventoryError, match="requires a lowercase evidenceSha256"):
        _evaluate(candidate, gate_results=evidence)


def test_timing_p95_above_five_percent_discards_candidate() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    baseline = _accepted_baseline()
    timing = _timing_evidence(
        baseline["inventory"],
        candidate,
        baseline_values=[1000] * 20,
        candidate_values=[1000] * 18 + [1060] * 2,
    )

    result = _evaluate(candidate, install_measurements=timing)

    assert result["decision"] == "discard"
    assert result["gates"]["installTimingRegression"]["p50WithinLimit"] is True
    assert result["gates"]["installTimingRegression"]["p95WithinLimit"] is False
    assert "gate_failed:installTimingRegression" in result["reasonCodes"]


def test_timing_provenance_mismatch_is_invalid_measurement() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    baseline = _accepted_baseline()
    timing = _timing_evidence(baseline["inventory"], candidate)
    timing["sourceCommit"] = "8" * 40

    result = _evaluate(candidate, install_measurements=timing)

    assert result["decision"] == "invalid_measurement"
    assert result["reasonCodes"] == ["invalid_install_timing_evidence"]
    assert "sourceCommit_mismatch" in result["gates"]["installTimingRegression"][
        "errorCodes"
    ]


def test_ten_timing_pairs_are_insufficient_for_keep() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    baseline = _accepted_baseline()
    timing = _timing_evidence(baseline["inventory"], candidate)
    timing["pairCount"] = 10

    result = _evaluate(candidate, install_measurements=timing)

    assert result["decision"] == "invalid_measurement"
    assert "pair_count_below_twenty" in result["gates"][
        "installTimingRegression"
    ]["errorCodes"]
    assert "result_install_measurement_pairs_invalid" in {
        finding["code"] for finding in validate_result(result, expected_run_id=RUN_ID)
    }


def test_relative_threshold_uses_ceiling_and_can_dominate_absolute_threshold() -> None:
    baseline_first = _inventory(
        installer_bytes=200_000_001,
        replica_id="baseline-replica-1",
        build_root_sha256="1" * 64,
    )
    baseline_second = copy.deepcopy(baseline_first)
    baseline_second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }
    baseline = accept_baseline(baseline_first, baseline_second)
    required = 500_001
    candidate = _inventory(
        installer_bytes=200_000_001 - required,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
        replica_id="packet-relative",
    )

    result = evaluate_candidate(
        baseline,
        candidate,
        run_id=RUN_ID,
        packet_id="packet-relative",
        parent_champion_id="baseline",
        hypothesis="Meet the exact 25-basis-point boundary.",
        source_commit=SOURCE_COMMIT,
        gate_results=_passing_gates(packet_id="packet-relative"),
        install_measurements=_timing_evidence(
            baseline["inventory"], candidate, packet_id="packet-relative"
        ),
        install_measurements_sha256=TIMING_SHA,
    )

    assert result["decision"] == "keep"
    assert result["gates"]["installerReduction"]["requiredReductionBytes"] == required


def test_missing_external_or_installed_evidence_can_never_be_a_keep() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=None,
        semantic_hash="f" * 64,
        replica_id="packet-measure",
    )

    result = evaluate_candidate(
        _accepted_baseline(),
        candidate,
        run_id=RUN_ID,
        packet_id="packet-measure",
        parent_champion_id="baseline",
        hypothesis="Measure a staged-only candidate.",
        source_commit=SOURCE_COMMIT,
    )

    assert result["decision"] == "measure_only"
    assert "gate_not_run:frozenRuntimeImports" in result["reasonCodes"]
    assert "gate_not_run:installTimingRegression" in result["reasonCodes"]
    assert "gate_not_run:installedPayloadNonGrowth" in result["reasonCodes"]


def test_compression_comparison_requires_identical_semantic_payload() -> None:
    candidate = _inventory(
        installer_bytes=96_000_000,
        compression="zlib",
    )

    passing = _evaluate(candidate, comparison_kind="compression")
    assert passing["decision"] == "keep"
    assert passing["gates"]["semanticPayloadIdentity"]["status"] == "pass"
    assert passing["gates"]["combinedInstall50"]["status"] == "pass"
    assert passing["gates"]["combinedInstall50"]["improvementNanoseconds"] == 590_000_000
    assert passing["gates"]["combinedInstall50"]["requiredImprovementNanoseconds"] == 500_000_000

    changed = _inventory(
        installer_bytes=96_000_000,
        compression="zlib",
        semantic_hash="f" * 64,
    )
    failed = _evaluate(changed, comparison_kind="compression")
    assert failed["decision"] == "discard"
    assert "gate_failed:semanticPayloadIdentity" in failed["reasonCodes"]


def test_compression_requires_combined_download_and_install_improvement() -> None:
    candidate = _inventory(
        installer_bytes=100_000_000 - 256 * 1024,
        compression="zlib",
    )

    result = _evaluate(candidate, comparison_kind="compression")

    assert result["gates"]["installerReduction"]["status"] == "pass"
    assert result["gates"]["installTimingRegression"]["status"] == "pass"
    assert result["gates"]["combinedInstall50"]["status"] == "fail"
    assert result["decision"] == "discard"
    assert "gate_failed:combinedInstall50" in result["reasonCodes"]


def test_combined_gate_uses_exact_ceiling_when_one_percent_dominates() -> None:
    first = _inventory(
        installer_bytes=400_000_001,
        replica_id="baseline-replica-1",
        build_root_sha256="1" * 64,
    )
    second = copy.deepcopy(first)
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }
    baseline = accept_baseline(first, second)

    def evaluate_with_savings(savings: int, packet_id: str) -> dict:
        candidate = _inventory(
            installer_bytes=400_000_001 - savings,
            compression="zlib",
            replica_id=packet_id,
        )
        return evaluate_candidate(
            baseline,
            candidate,
            run_id=RUN_ID,
            packet_id=packet_id,
            parent_champion_id="baseline",
            hypothesis="Exercise the exact cumulative combined-time boundary.",
            source_commit=SOURCE_COMMIT,
            comparison_kind="compression",
            gate_results=_passing_gates(packet_id=packet_id),
            install_measurements=_timing_evidence(
                baseline["inventory"], candidate, packet_id=packet_id
            ),
            install_measurements_sha256=TIMING_SHA,
        )

    passing = evaluate_with_savings(4_375_001, "packet-combined-pass")
    failing = evaluate_with_savings(4_375_000, "packet-combined-fail")

    assert passing["decision"] == "keep"
    assert passing["gates"]["combinedInstall50"] == {
        "status": "pass",
        "bandwidthBitsPerSecond": 50_000_000,
        "parentTotalSecondsP50": 65.0,
        "candidateTotalSecondsP50": 64.35,
        "parentTotalNanosecondsP50": 65_000_000_160,
        "candidateTotalNanosecondsP50": 64_350_000_000,
        "improvementNanoseconds": 650_000_160,
        "requiredImprovementNanoseconds": 650_000_002,
        "minimumAbsoluteImprovementNanoseconds": 500_000_000,
        "minimumRelativeImprovementBasisPoints": 100,
        "improvementSeconds": 0.65,
    }
    assert failing["decision"] == "discard"
    assert failing["gates"]["combinedInstall50"]["improvementNanoseconds"] == 650_000_000
    assert failing["gates"]["combinedInstall50"]["requiredImprovementNanoseconds"] == 650_000_002


def test_binding_drift_fails_closed() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    candidate["toolchainHash"] = "0" * 64

    result = _evaluate(candidate)

    assert result["decision"] == "discard"
    assert result["gates"]["bindings"]["status"] == "fail"
    assert result["reasonCodes"] == ["gate_failed:bindings"]


def test_candidate_inventory_is_bound_to_packet_and_source_commit() -> None:
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
        replica_id="different-packet",
        source_commit="8" * 40,
    )
    baseline = _accepted_baseline()

    result = evaluate_candidate(
        baseline,
        candidate,
        run_id=RUN_ID,
        packet_id="packet-001",
        parent_champion_id="baseline",
        hypothesis="Attempt to reuse evidence from a different packet.",
        source_commit=SOURCE_COMMIT,
        gate_results=_passing_gates(),
        install_measurements=_timing_evidence(baseline["inventory"], candidate),
        install_measurements_sha256=TIMING_SHA,
    )

    assert result["decision"] == "discard"
    checks = result["gates"]["bindings"]["checks"]
    assert checks["candidatePacketId"] is False
    assert checks["candidateSourceCommit"] is False


def test_accept_baseline_cli_binds_both_replica_files(tmp_path: Path) -> None:
    from scripts.perf.installer_size.doctor import current_installer_evaluator_hash

    evaluator_hash = current_installer_evaluator_hash(REPO_ROOT)
    first = _inventory()
    second = copy.deepcopy(first)
    first["buildProvenance"] = {
        "replicaId": "baseline-replica-1",
        "buildRootSha256": "1" * 64,
    }
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }
    first["evaluatorHash"] = evaluator_hash
    second["evaluatorHash"] = evaluator_hash
    second["installer"]["sha256"] = "c" * 64
    first_path = tmp_path / "replica-1.json"
    second_path = tmp_path / "replica-2.json"
    output = tmp_path / "baseline.json"
    first_path.write_text(json.dumps(first, indent=2) + "\n", encoding="utf-8")
    second_path.write_text(json.dumps(second, indent=2) + "\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/installer_research.py",
            "accept-baseline",
            "--first-inventory",
            str(first_path),
            "--second-inventory",
            str(second_path),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    baseline = json.loads(output.read_text(encoding="utf-8"))
    assert baseline["accepted"] is True
    assert baseline["evaluatorHash"] == evaluator_hash
    assert len(baseline["replicas"]) == 2
    assert baseline["replicas"][0]["inventorySha256"] != baseline["replicas"][1][
        "inventorySha256"
    ]


def test_evaluate_cli_hashes_and_validates_timing_evidence(tmp_path: Path) -> None:
    from scripts.perf.installer_size.doctor import current_installer_evaluator_hash

    evaluator_hash = current_installer_evaluator_hash(REPO_ROOT)
    first = _inventory(
        replica_id="baseline-replica-1", build_root_sha256="1" * 64
    )
    second = copy.deepcopy(first)
    first["evaluatorHash"] = evaluator_hash
    second["evaluatorHash"] = evaluator_hash
    second["buildProvenance"] = {
        "replicaId": "baseline-replica-2",
        "buildRootSha256": "2" * 64,
    }
    baseline = accept_baseline(first, second)
    candidate = _inventory(
        installer_bytes=99_000_000,
        staged_bytes=199_000_000,
        installed_bytes=200_000_000,
        semantic_hash="f" * 64,
    )
    candidate["evaluatorHash"] = evaluator_hash
    gates = _passing_gates()
    timing = _timing_evidence(baseline["inventory"], candidate)

    paths = {
        "baseline": tmp_path / "baseline.json",
        "candidate": tmp_path / "candidate.json",
        "gates": tmp_path / "gates.json",
        "timing": tmp_path / "timing.json",
        "result": tmp_path / "result.json",
    }
    for name, payload in (
        ("baseline", baseline),
        ("candidate", candidate),
        ("gates", gates),
        ("timing", timing),
    ):
        paths[name].write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/installer_research.py",
            "evaluate",
            "--baseline",
            str(paths["baseline"]),
            "--candidate-inventory",
            str(paths["candidate"]),
            "--run-id",
            RUN_ID,
            "--packet-id",
            "packet-001",
            "--parent-champion-id",
            "baseline",
            "--hypothesis",
            "Validate the complete evaluator CLI evidence path.",
            "--source-commit",
            SOURCE_COMMIT,
            "--gate-results",
            str(paths["gates"]),
            "--install-measurements",
            str(paths["timing"]),
            "--output",
            str(paths["result"]),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(paths["result"].read_text(encoding="utf-8"))
    assert result["decision"] == "keep"
    assert result["installMeasurements"]["evidenceSha256"] == hashlib.sha256(
        paths["timing"].read_bytes()
    ).hexdigest()
