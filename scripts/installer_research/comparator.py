from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .inventory import (
    INVENTORY_CONTRACT,
    SHA256_RE,
    InventoryError,
    validate_replica_id,
    validate_run_id,
    validate_source_commit,
)


BASELINE_CONTRACT = "InstallerResearchBaselineV2"
BASELINE_SCHEMA_VERSION = 2
LEGACY_BASELINE_CONTRACT = "InstallerResearchBaselineV1"
LEGACY_BASELINE_SCHEMA_VERSION = 1
RESULT_CONTRACT = "InstallerResearchResultV1"
SCHEMA_VERSION = 1
GATE_STATUSES = {"pass", "fail", "not_run", "not_applicable"}
GATE_EVIDENCE_CONTRACT = "InstallerResearchGateEvidenceV1"
TIMING_EVIDENCE_KIND = "installer-ab-timing"
MINIMUM_TIMING_PAIR_COUNT = 20
MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS = 500_000_000
MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR = 100
MANDATORY_EXTERNAL_GATES = (
    "frozenRuntimeImports",
    "mediaPreparation",
    "youtubeWorkflow",
    "liveMic",
    "meetingCapture",
    "diarization",
    "pdfDocxExport",
    "desktopFrontend",
    "cleanInstallUpgradeUninstall",
    "licenseSupplyChain",
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_inventory(inventory: Mapping[str, Any], *, label: str) -> None:
    if not isinstance(inventory, dict):
        raise InventoryError(f"{label} inventory must be a JSON object.")
    if inventory.get("inventoryContract") != INVENTORY_CONTRACT:
        raise InventoryError(f"{label} is not an {INVENTORY_CONTRACT} artifact.")
    if inventory.get("schemaVersion") != 1:
        raise InventoryError(f"{label} has an unsupported inventory schemaVersion.")
    validate_run_id(inventory.get("runId"), field=f"{label}.runId")
    validate_source_commit(
        inventory.get("sourceCommit"), field=f"{label}.sourceCommit"
    )
    provenance = inventory.get("buildProvenance")
    if not isinstance(provenance, dict):
        raise InventoryError(f"{label}.buildProvenance must be an object.")
    validate_replica_id(
        provenance.get("replicaId"), field=f"{label}.buildProvenance.replicaId"
    )
    if not SHA256_RE.fullmatch(str(provenance.get("buildRootSha256", ""))):
        raise InventoryError(
            f"{label}.buildProvenance.buildRootSha256 must be a lowercase SHA-256."
        )
    for field in ("evaluatorHash", "toolchainHash"):
        value = inventory.get(field)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise InventoryError(f"{label}.{field} must be a lowercase SHA-256.")
    component_map = inventory.get("componentMap")
    if not isinstance(component_map, dict) or not SHA256_RE.fullmatch(
        str(component_map.get("sha256", ""))
    ):
        raise InventoryError(f"{label}.componentMap is invalid.")
    installer = inventory.get("installer")
    if not isinstance(installer, dict):
        raise InventoryError(f"{label}.installer is invalid.")
    if (
        isinstance(installer.get("length"), bool)
        or not isinstance(installer.get("length"), int)
        or installer["length"] <= 0
    ):
        raise InventoryError(f"{label}.installer.length is invalid.")
    if not SHA256_RE.fullmatch(str(installer.get("sha256", ""))):
        raise InventoryError(f"{label}.installer.sha256 is invalid.")
    payload = inventory.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("staged"), dict):
        raise InventoryError(f"{label}.payload.staged is invalid.")
    staged = payload["staged"]
    for field in ("totalBytes", "fileCount", "componentBytesSum"):
        if (
            isinstance(staged.get(field), bool)
            or not isinstance(staged.get(field), int)
            or staged[field] < 0
        ):
            raise InventoryError(f"{label}.payload.staged.{field} is invalid.")
    for field in ("exactTreeSha256", "semanticTreeSha256", "fileListSha256"):
        if not SHA256_RE.fullmatch(str(staged.get(field, ""))):
            raise InventoryError(f"{label}.payload.staged.{field} is invalid.")
    if staged.get("componentBytesSum") != staged.get("totalBytes"):
        raise InventoryError(f"{label} physical component partition is not exact.")
    installed = payload.get("installed")
    if installed is not None:
        if not isinstance(installed, dict):
            raise InventoryError(f"{label}.payload.installed is invalid.")
        for field in ("totalBytes", "fileCount", "componentBytesSum"):
            if (
                isinstance(installed.get(field), bool)
                or not isinstance(installed.get(field), int)
                or installed[field] < 0
            ):
                raise InventoryError(f"{label}.payload.installed.{field} is invalid.")
        if installed.get("componentBytesSum") != installed.get("totalBytes"):
            raise InventoryError(
                f"{label} installed component partition is not exact."
            )
    backend = inventory.get("backendExecutable")
    if (
        not isinstance(backend, dict)
        or isinstance(backend.get("length"), bool)
        or not isinstance(backend.get("length"), int)
        or backend.get("length", -1) <= 0
        or backend.get("virtualPartitionBytes") != backend.get("length")
    ):
        raise InventoryError(f"{label} backend virtual partition is not exact.")
    pyz = backend.get("pyzDiagnostics")
    if not isinstance(pyz, dict) or not SHA256_RE.fullmatch(
        str(pyz.get("inventorySha256", ""))
    ):
        raise InventoryError(f"{label} PYZ inventory is invalid.")


def _component_identity(staged: Mapping[str, Any]) -> dict[str, Any]:
    components = staged.get("components")
    if not isinstance(components, dict):
        raise InventoryError("Inventory components must be an object.")
    return {
        str(name): {
            "rawBytes": value.get("rawBytes"),
            "fileCount": value.get("fileCount"),
            "allocationCount": value.get("allocationCount"),
            "fileListSha256": value.get("fileListSha256"),
            "allocationListSha256": value.get("allocationListSha256"),
        }
        for name, value in sorted(components.items())
    }


def _validate_accepted_baseline(
    baseline: Mapping[str, Any], inventory: Mapping[str, Any]
) -> None:
    if baseline.get("reasonCodes") != []:
        raise InventoryError("Accepted baseline must have an empty reasonCodes array.")
    staged = inventory["payload"]["staged"]
    installed = inventory["payload"].get("installed")
    expected = {
        "runId": inventory["runId"],
        "sourceCommit": inventory["sourceCommit"],
        "evaluatorHash": inventory["evaluatorHash"],
        "toolchainHash": inventory["toolchainHash"],
        "componentMapSha256": inventory["componentMap"]["sha256"],
        "productVersion": inventory["productVersion"],
        "compression": inventory["compression"],
        "installerLength": inventory["installer"]["length"],
        "stagedBytes": staged["totalBytes"],
        "installedBytes": installed["totalBytes"] if installed is not None else None,
        "semanticTreeSha256": staged["semanticTreeSha256"],
        "fileListSha256": staged["fileListSha256"],
        "pyzInventorySha256": inventory["backendExecutable"]["pyzDiagnostics"][
            "inventorySha256"
        ],
    }
    mismatches = [name for name, value in expected.items() if baseline.get(name) != value]
    if mismatches:
        raise InventoryError(
            "Accepted baseline summary disagrees with its canonical inventory: "
            + ", ".join(mismatches)
        )
    contract = baseline.get("baselineContract")
    schema_version = baseline.get("schemaVersion")
    if contract == BASELINE_CONTRACT and schema_version == BASELINE_SCHEMA_VERSION:
        expected_replica_count = 1
        if baseline.get("baselineInventoryCount") != 1:
            raise InventoryError(
                "Accepted single-inventory baseline must declare baselineInventoryCount=1."
            )
    elif (
        contract == LEGACY_BASELINE_CONTRACT
        and schema_version == LEGACY_BASELINE_SCHEMA_VERSION
    ):
        expected_replica_count = 2
    else:
        raise InventoryError("Accepted baseline contract or schemaVersion is unsupported.")
    replicas = baseline.get("replicas")
    if not isinstance(replicas, list) or len(replicas) != expected_replica_count:
        raise InventoryError(
            f"Accepted baseline must bind exactly {expected_replica_count} inventory document(s)."
        )
    for ordinal, replica in enumerate(replicas, start=1):
        if not isinstance(replica, dict) or replica.get("ordinal") != ordinal:
            raise InventoryError("Accepted baseline replica ordinals are invalid.")
        validate_replica_id(
            replica.get("replicaId"), field=f"baseline.replicas[{ordinal}].replicaId"
        )
        for field in (
            "buildRootSha256",
            "inventorySha256",
            "installerSha256",
            "exactTreeSha256",
            "semanticTreeSha256",
        ):
            if not SHA256_RE.fullmatch(str(replica.get(field, ""))):
                raise InventoryError(
                    f"baseline.replicas[{ordinal}].{field} must be a lowercase SHA-256."
                )
    first = replicas[0]
    if expected_replica_count == 2:
        second = replicas[1]
        if first["replicaId"] == second["replicaId"]:
            raise InventoryError("Accepted baseline replica IDs are not distinct.")
        if first["buildRootSha256"] == second["buildRootSha256"]:
            raise InventoryError("Accepted baseline build roots are not distinct.")
        if first["inventorySha256"] == second["inventorySha256"]:
            raise InventoryError("Accepted baseline inventory documents are not distinct.")
    first_expected = {
        "replicaId": inventory["buildProvenance"]["replicaId"],
        "buildRootSha256": inventory["buildProvenance"]["buildRootSha256"],
        "installerSha256": inventory["installer"]["sha256"],
        "exactTreeSha256": staged["exactTreeSha256"],
        "semanticTreeSha256": staged["semanticTreeSha256"],
    }
    if any(first.get(name) != value for name, value in first_expected.items()):
        raise InventoryError(
            "Accepted baseline first replica disagrees with its canonical inventory."
        )


def _baseline_reason_codes(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    first_inventory_sha256: str,
    second_inventory_sha256: str,
) -> list[str]:
    reasons: list[str] = []
    if first.get("runId") != second.get("runId"):
        reasons.append("run_id_mismatch")
    if first.get("sourceCommit") != second.get("sourceCommit"):
        reasons.append("source_commit_mismatch")
    first_provenance = first["buildProvenance"]
    second_provenance = second["buildProvenance"]
    if first_provenance.get("replicaId") == second_provenance.get("replicaId"):
        reasons.append("replica_id_not_distinct")
    if first_provenance.get("buildRootSha256") == second_provenance.get(
        "buildRootSha256"
    ):
        reasons.append("build_root_not_distinct")
    if (
        first_inventory_sha256 == second_inventory_sha256
        or canonical_json_sha256(first) == canonical_json_sha256(second)
    ):
        reasons.append("inventory_document_not_distinct")
    if not first.get("ok") or not second.get("ok"):
        reasons.append("inventory_not_ok")
    if first.get("evaluatorHash") != second.get("evaluatorHash"):
        reasons.append("evaluator_hash_mismatch")
    if first.get("toolchainHash") != second.get("toolchainHash"):
        reasons.append("toolchain_hash_mismatch")
    if first.get("componentMap", {}).get("sha256") != second.get("componentMap", {}).get("sha256"):
        reasons.append("component_map_mismatch")
    if first.get("productVersion") != second.get("productVersion"):
        reasons.append("product_version_mismatch")
    if first.get("compression") != "bzip2" or second.get("compression") != "bzip2":
        reasons.append("baseline_compression_not_bzip2")
    if first.get("installer", {}).get("length") != second.get("installer", {}).get("length"):
        reasons.append("installer_length_mismatch")

    first_staged = first["payload"]["staged"]
    second_staged = second["payload"]["staged"]
    comparisons = (
        ("totalBytes", "staged_total_bytes_mismatch"),
        ("semanticTreeSha256", "staged_semantic_tree_mismatch"),
        ("fileListSha256", "staged_file_list_mismatch"),
    )
    for field, reason in comparisons:
        if first_staged.get(field) != second_staged.get(field):
            reasons.append(reason)
    if _component_identity(first_staged) != _component_identity(second_staged):
        reasons.append("staged_component_partition_mismatch")

    first_backend = first["backendExecutable"]
    second_backend = second["backendExecutable"]
    if first_backend.get("pyinstallerVersion") != second_backend.get("pyinstallerVersion"):
        reasons.append("pyinstaller_version_mismatch")
    if first_backend["pyzDiagnostics"].get("inventorySha256") != second_backend["pyzDiagnostics"].get(
        "inventorySha256"
    ):
        reasons.append("pyz_inventory_mismatch")
    if first_backend["pyzDiagnostics"].get("roots") != second_backend["pyzDiagnostics"].get("roots"):
        reasons.append("pyz_roots_mismatch")
    if first_backend.get("regions") != second_backend.get("regions"):
        reasons.append("backend_virtual_partition_mismatch")

    first_installed = first["payload"].get("installed")
    second_installed = second["payload"].get("installed")
    if (first_installed is None) != (second_installed is None):
        reasons.append("installed_inventory_presence_mismatch")
    elif first_installed is None:
        reasons.append("installed_inventory_missing")
    elif first_installed is not None and second_installed is not None:
        for field, reason in (
            ("totalBytes", "installed_total_bytes_mismatch"),
            ("semanticTreeSha256", "installed_semantic_tree_mismatch"),
            ("fileListSha256", "installed_file_list_mismatch"),
        ):
            if first_installed.get(field) != second_installed.get(field):
                reasons.append(reason)
        if _component_identity(first_installed) != _component_identity(second_installed):
            reasons.append("installed_component_partition_mismatch")
        for replica, suffix in ((first, "first"), (second, "second")):
            parity = replica["payload"].get("stagedInstalledParity")
            if not isinstance(parity, dict) or not parity.get("ok"):
                reasons.append(f"{suffix}_staged_installed_parity_failed")
    return sorted(set(reasons))


def _single_baseline_reason_codes(inventory: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not inventory.get("ok"):
        reasons.append("inventory_not_ok")
    if inventory.get("compression") != "bzip2":
        reasons.append("baseline_compression_not_bzip2")
    installed = inventory["payload"].get("installed")
    if installed is None:
        reasons.append("installed_inventory_missing")
    parity = inventory["payload"].get("stagedInstalledParity")
    if not isinstance(parity, dict) or not parity.get("ok"):
        reasons.append("staged_installed_parity_failed")
    return sorted(set(reasons))


def _replica_summary(
    inventory: Mapping[str, Any],
    *,
    ordinal: int,
    inventory_sha256: str | None,
) -> dict[str, Any]:
    staged = inventory["payload"]["staged"]
    digest = inventory_sha256 or canonical_json_sha256(inventory)
    if not SHA256_RE.fullmatch(digest):
        raise InventoryError("Replica inventory SHA-256 is invalid.")
    return {
        "ordinal": ordinal,
        "replicaId": inventory["buildProvenance"]["replicaId"],
        "buildRootSha256": inventory["buildProvenance"]["buildRootSha256"],
        "inventorySha256": digest,
        "installerSha256": inventory["installer"]["sha256"],
        "exactTreeSha256": staged["exactTreeSha256"],
        "semanticTreeSha256": staged["semanticTreeSha256"],
    }


def accept_baseline(
    first: Mapping[str, Any],
    second: Mapping[str, Any] | None = None,
    *,
    first_inventory_sha256: str | None = None,
    second_inventory_sha256: str | None = None,
) -> dict[str, Any]:
    _validate_inventory(first, label="first")
    first_digest = first_inventory_sha256 or canonical_json_sha256(first)
    if not SHA256_RE.fullmatch(first_digest):
        raise InventoryError("Baseline inventory SHA-256 is invalid.")
    if second is None:
        if second_inventory_sha256 is not None:
            raise InventoryError(
                "second_inventory_sha256 is invalid without a second inventory."
            )
        reason_codes = _single_baseline_reason_codes(first)
        contract = BASELINE_CONTRACT
        schema_version = BASELINE_SCHEMA_VERSION
        replicas = [
            _replica_summary(
                first,
                ordinal=1,
                inventory_sha256=first_digest,
            )
        ]
    else:
        # Retain validation of already-recorded V1 pair evidence, but the active
        # installer-size campaign creates only the V2 single-inventory form.
        _validate_inventory(second, label="second")
        second_digest = second_inventory_sha256 or canonical_json_sha256(second)
        if not SHA256_RE.fullmatch(second_digest):
            raise InventoryError("Replica inventory SHA-256 is invalid.")
        reason_codes = _baseline_reason_codes(
            first,
            second,
            first_inventory_sha256=first_digest,
            second_inventory_sha256=second_digest,
        )
        contract = LEGACY_BASELINE_CONTRACT
        schema_version = LEGACY_BASELINE_SCHEMA_VERSION
        replicas = [
            _replica_summary(
                first,
                ordinal=1,
                inventory_sha256=first_digest,
            ),
            _replica_summary(
                second,
                ordinal=2,
                inventory_sha256=second_digest,
            ),
        ]
    staged = first["payload"]["staged"]
    installed = first["payload"].get("installed")
    result = {
        "baselineContract": contract,
        "schemaVersion": schema_version,
        "accepted": not reason_codes,
        "reasonCodes": reason_codes,
        "acceptedAtUtc": _utc_now() if not reason_codes else None,
        "runId": first["runId"],
        "sourceCommit": first["sourceCommit"],
        "evaluatorHash": first["evaluatorHash"],
        "toolchainHash": first["toolchainHash"],
        "componentMapSha256": first["componentMap"]["sha256"],
        "productVersion": first["productVersion"],
        "compression": first["compression"],
        "installerLength": first["installer"]["length"],
        "stagedBytes": staged["totalBytes"],
        "installedBytes": installed["totalBytes"] if installed is not None else None,
        "semanticTreeSha256": staged["semanticTreeSha256"],
        "fileListSha256": staged["fileListSha256"],
        "pyzInventorySha256": first["backendExecutable"]["pyzDiagnostics"]["inventorySha256"],
        "replicas": replicas,
        "inventory": dict(first),
    }
    if second is None:
        result["baselineInventoryCount"] = 1
    return result


def _missing_external_gates(reason: str) -> dict[str, dict[str, Any]]:
    return {
        name: {"status": "not_run", "reason": reason}
        for name in MANDATORY_EXTERNAL_GATES
    }


def _validate_external_gates(
    value: Mapping[str, Any] | None,
    *,
    run_id: str,
    packet_id: str,
    parent_champion_id: str,
    source_commit: str,
) -> dict[str, dict[str, Any]]:
    if value is None:
        return _missing_external_gates(
            "No external functional gate evidence was supplied."
        )
    if not isinstance(value, dict):
        raise InventoryError("gate_results must be a JSON object.")
    unexpected_document_fields = set(value) - {
        "gateEvidenceContract",
        "schemaVersion",
        "runId",
        "packetId",
        "parentChampionId",
        "sourceCommit",
        "gates",
    }
    if unexpected_document_fields:
        raise InventoryError(
            "gate_results contains unsupported fields: "
            + ", ".join(sorted(unexpected_document_fields))
        )
    if value.get("gateEvidenceContract") != GATE_EVIDENCE_CONTRACT:
        raise InventoryError(
            f"gate_results.gateEvidenceContract must be {GATE_EVIDENCE_CONTRACT}."
        )
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise InventoryError("gate_results.schemaVersion must be 1.")
    bindings = {
        "runId": value.get("runId") == run_id,
        "packetId": value.get("packetId") == packet_id,
        "parentChampionId": value.get("parentChampionId") == parent_champion_id,
        "sourceCommit": value.get("sourceCommit") == source_commit,
    }
    if not all(bindings.values()):
        mismatches = ", ".join(name for name, ok in bindings.items() if not ok)
        raise InventoryError(f"gate_results provenance mismatch: {mismatches}.")
    gates_value: Any = value.get("gates")
    if not isinstance(gates_value, dict):
        raise InventoryError("gate_results.gates must be a JSON object.")
    gates: dict[str, dict[str, Any]] = {}
    for name, gate_value in sorted(gates_value.items()):
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 96
            or not name[0].isalnum()
            or not name.replace("_", "a").replace("-", "a").replace(".", "a").isalnum()
        ):
            raise InventoryError("Gate names must be bounded safe identifiers.")
        if not isinstance(gate_value, dict):
            raise InventoryError(f"Gate {name!r} must be a JSON object.")
        unexpected_fields = set(gate_value) - {
            "status",
            "evidenceSha256",
            "reasonCode",
        }
        if unexpected_fields:
            raise InventoryError(
                f"Gate {name!r} contains unsupported fields: "
                + ", ".join(sorted(unexpected_fields))
            )
        status = gate_value.get("status")
        if status not in GATE_STATUSES:
            raise InventoryError(f"Gate {name!r} has invalid status {status!r}.")
        evidence_sha = gate_value.get("evidenceSha256")
        if status == "pass" and not SHA256_RE.fullmatch(str(evidence_sha or "")):
            raise InventoryError(
                f"Passing gate {name!r} requires a lowercase evidenceSha256."
            )
        if evidence_sha is not None and not SHA256_RE.fullmatch(str(evidence_sha)):
            raise InventoryError(f"Gate {name!r} has an invalid evidenceSha256.")
        reason_code = gate_value.get("reasonCode")
        if reason_code is not None and (
            not isinstance(reason_code, str)
            or not reason_code
            or len(reason_code) > 128
            or not reason_code.replace("_", "a")
            .replace("-", "a")
            .replace(".", "a")
            .replace(":", "a")
            .isalnum()
        ):
            raise InventoryError(f"Gate {name!r} has an invalid reasonCode.")
        gate = {"status": status}
        if evidence_sha is not None:
            gate["evidenceSha256"] = evidence_sha
        if reason_code is not None:
            gate["reasonCode"] = reason_code
        gates[name] = gate
    for name in MANDATORY_EXTERNAL_GATES:
        if name not in gates:
            gates[name] = {
            "status": "not_run",
                "reason": "Mandatory functional evidence was not supplied.",
            }
    return gates


def _is_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _median_twice(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle] * 2
    return ordered[middle - 1] + ordered[middle]


def _nearest_rank_p95(values: list[int]) -> int:
    ordered = sorted(values)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def _ratio_basis_points(candidate_twice: int, parent_twice: int) -> int | None:
    if parent_twice == 0:
        return 10_000 if candidate_twice == 0 else None
    return (candidate_twice * 10_000 + parent_twice - 1) // parent_twice


def _timing_summary(
    value: Mapping[str, Any],
    *,
    evidence_sha256: str | None,
    baseline_statistics: Mapping[str, Any] | None = None,
    candidate_statistics: Mapping[str, Any] | None = None,
    parent_installer_bytes: int,
    candidate_installer_bytes: int,
) -> dict[str, Any]:
    baseline_stats = baseline_statistics or {}
    candidate_stats = candidate_statistics or {}
    baseline_p50 = baseline_stats.get("p50Ms")
    baseline_p95 = baseline_stats.get("p95Ms")
    candidate_p50 = candidate_stats.get("p50Ms")
    candidate_p95 = candidate_stats.get("p95Ms")

    def total_seconds(installer_bytes: int, milliseconds: Any) -> float | None:
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
            return None
        return round(installer_bytes * 8 / 50_000_000 + float(milliseconds) / 1000, 6)

    def total_nanoseconds(installer_bytes: int, milliseconds: Any) -> int | None:
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
            return None
        return installer_bytes * 160 + int(round(float(milliseconds) * 1_000_000))

    return {
        "evidenceSha256": evidence_sha256,
        "apiVersion": value.get("apiVersion"),
        "kind": value.get("kind"),
        "runId": value.get("runId"),
        "packetId": value.get("packetId"),
        "parentChampionId": value.get("parentChampionId"),
        "sourceCommit": value.get("sourceCommit"),
        "pairCount": value.get("pairCount"),
        "baseline": {
            "count": baseline_stats.get("count"),
            "p50Ms": baseline_p50,
            "p95Ms": baseline_p95,
            "totalInstallSeconds50P50": total_seconds(
                parent_installer_bytes, baseline_p50
            ),
            "totalInstallSeconds50P95": total_seconds(
                parent_installer_bytes, baseline_p95
            ),
            "totalInstallNanoseconds50P50": total_nanoseconds(
                parent_installer_bytes, baseline_p50
            ),
            "totalInstallNanoseconds50P95": total_nanoseconds(
                parent_installer_bytes, baseline_p95
            ),
        },
        "candidate": {
            "count": candidate_stats.get("count"),
            "p50Ms": candidate_p50,
            "p95Ms": candidate_p95,
            "totalInstallSeconds50P50": total_seconds(
                candidate_installer_bytes, candidate_p50
            ),
            "totalInstallSeconds50P95": total_seconds(
                candidate_installer_bytes, candidate_p95
            ),
            "totalInstallNanoseconds50P50": total_nanoseconds(
                candidate_installer_bytes, candidate_p50
            ),
            "totalInstallNanoseconds50P95": total_nanoseconds(
                candidate_installer_bytes, candidate_p95
            ),
        },
    }


def validate_install_measurements(
    value: Mapping[str, Any] | None,
    *,
    evidence_sha256: str | None,
    run_id: str,
    packet_id: str,
    parent_champion_id: str,
    source_commit: str,
    parent: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
    """Validate Timing V1 and return its safe summary, gate, and invalid flag."""
    if value is None:
        return (
            None,
            _computed_gate(
                "not_run",
                reason="No counterbalanced installer timing evidence was supplied.",
                minimumPairCount=MINIMUM_TIMING_PAIR_COUNT,
            ),
            False,
        )

    errors: list[str] = []
    if not isinstance(value, dict):
        errors.append("document_not_object")
        value = {}
    if not isinstance(evidence_sha256, str) or not SHA256_RE.fullmatch(
        evidence_sha256
    ):
        errors.append("evidence_sha256_invalid")
    if value.get("apiVersion") != "1":
        errors.append("api_version_mismatch")
    if value.get("kind") != TIMING_EVIDENCE_KIND:
        errors.append("kind_mismatch")
    if value.get("ok") is not True:
        errors.append("measurement_not_ok")
    for field, expected in (
        ("runId", run_id),
        ("packetId", packet_id),
        ("parentChampionId", parent_champion_id),
        ("sourceCommit", source_commit),
    ):
        if value.get(field) != expected:
            errors.append(f"{field}_mismatch")

    pair_count = value.get("pairCount")
    if (
        not _is_nonnegative_int(pair_count)
        or pair_count < MINIMUM_TIMING_PAIR_COUNT
    ):
        errors.append("pair_count_below_twenty")
        pair_count = 0
    warmup_count = value.get("warmupPerVariant")
    if not _is_nonnegative_int(warmup_count) or warmup_count < 1:
        errors.append("warmup_count_invalid")
        warmup_count = 0
    stable_samples = value.get("stableSamples")
    if not _is_nonnegative_int(stable_samples) or stable_samples < 2:
        errors.append("stable_sample_policy_invalid")
    sample_interval_ms = value.get("sampleIntervalMs")
    if not _is_nonnegative_int(sample_interval_ms) or sample_interval_ms <= 0:
        errors.append("sample_interval_invalid")
    stopwatch = value.get("stopwatch")
    if (
        not isinstance(stopwatch, dict)
        or stopwatch.get("isHighResolution") is not True
        or not _is_nonnegative_int(stopwatch.get("frequency"))
        or stopwatch.get("frequency", 0) <= 0
    ):
        errors.append("stopwatch_invalid")
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("outsideTimedIntervals") is not True:
        errors.append("cleanup_not_outside_timing")
    cache_policy = value.get("cachePolicy")
    if (
        not isinstance(cache_policy, dict)
        or cache_policy.get("osFileCacheFlushed") is not False
    ):
        errors.append("cache_policy_mismatch")
    if value.get("expectedVersion") != candidate.get("productVersion"):
        errors.append("candidate_version_mismatch")
    if parent.get("productVersion") != candidate.get("productVersion"):
        errors.append("parent_version_mismatch")

    variants = value.get("variants")
    if not isinstance(variants, dict):
        variants = {}
        errors.append("variants_missing")
    for variant_name, expected_inventory in (
        ("baseline", parent),
        ("candidate", candidate),
    ):
        variant = variants.get(variant_name)
        if not isinstance(variant, dict):
            errors.append(f"variant_{variant_name}_missing")
            continue
        expected_installer = expected_inventory["installer"]
        if variant.get("installerName") != expected_installer.get("name"):
            errors.append(f"variant_{variant_name}_name_mismatch")
        if variant.get("length") != expected_installer.get("length"):
            errors.append(f"variant_{variant_name}_length_mismatch")
        if variant.get("sha256") != expected_installer.get("sha256"):
            errors.append(f"variant_{variant_name}_sha256_mismatch")

    inventory_consistency = value.get("inventoryConsistency")
    if not isinstance(inventory_consistency, dict):
        inventory_consistency = {}
        errors.append("inventory_consistency_missing")
    for variant_name, expected_inventory in (
        ("baseline", parent),
        ("candidate", candidate),
    ):
        evidence = inventory_consistency.get(variant_name)
        installed = expected_inventory.get("payload", {}).get("installed")
        if not isinstance(evidence, dict) or not isinstance(installed, dict):
            errors.append(f"inventory_{variant_name}_missing")
            continue
        if evidence.get("totalBytes") != installed.get("totalBytes"):
            errors.append(f"inventory_{variant_name}_bytes_mismatch")
        if evidence.get("fileCount") != installed.get("fileCount"):
            errors.append(f"inventory_{variant_name}_files_mismatch")
        if evidence.get("sampleCount") != pair_count + warmup_count:
            errors.append(f"inventory_{variant_name}_sample_count_mismatch")
        if not SHA256_RE.fullmatch(str(evidence.get("treeSha256", ""))):
            errors.append(f"inventory_{variant_name}_tree_hash_invalid")

    samples_value = value.get("samples")
    samples = samples_value if isinstance(samples_value, list) else []
    if not isinstance(samples_value, list):
        errors.append("samples_missing")
    expected_sample_count = 2 * (pair_count + warmup_count)
    if len(samples) != expected_sample_count:
        errors.append("sample_count_mismatch")
    cleanup_events = cleanup.get("events") if isinstance(cleanup, dict) else None
    expected_cleanup_count = len(samples) + 1
    if (
        not isinstance(cleanup_events, list)
        or cleanup.get("invocationCount") != expected_cleanup_count
        or len(cleanup_events) != expected_cleanup_count
    ):
        errors.append("cleanup_evidence_count_mismatch")
    else:
        for event in cleanup_events:
            if (
                not isinstance(event, dict)
                or not _is_nonnegative_int(event.get("durationMs"))
                or not _is_nonnegative_int(event.get("forcedProcessCount"))
                or not _is_nonnegative_int(event.get("registryEntriesRemoved"))
                or not isinstance(event.get("filesRemoved"), bool)
                or event.get("uninstallerExitCode") not in (None, 0)
            ):
                errors.append("cleanup_event_invalid")
                break
    measured_by_pair: dict[int, list[Mapping[str, Any]]] = {}
    measured_values: dict[str, list[int]] = {"baseline": [], "candidate": []}
    warmups: dict[str, int] = {"baseline": 0, "candidate": 0}
    installed_executable_identities: dict[str, set[tuple[str, int, str]]] = {
        "baseline": set(),
        "candidate": set(),
    }
    sequences: set[int] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            errors.append("sample_not_object")
            continue
        sequence = sample.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            errors.append("sample_sequence_invalid")
        elif sequence in sequences:
            errors.append("sample_sequence_duplicate")
        else:
            sequences.add(sequence)
        variant = sample.get("variant")
        if variant not in measured_values:
            errors.append("sample_variant_invalid")
            continue
        stable_ms = sample.get("stableInstallMs")
        if not _is_nonnegative_int(stable_ms):
            errors.append("sample_duration_invalid")
            continue
        launcher_exit_ms = sample.get("launcherExitMs")
        post_exit_ms = sample.get("postExitCompletionMs")
        if (
            not _is_nonnegative_int(launcher_exit_ms)
            or launcher_exit_ms > stable_ms
            or not _is_nonnegative_int(post_exit_ms)
            or post_exit_ms != stable_ms - launcher_exit_ms
        ):
            errors.append("sample_completion_timing_invalid")
        installed_exe = sample.get("installedExe")
        normalized_exe = (
            installed_exe.replace("\\", "/")
            if isinstance(installed_exe, str)
            else ""
        )
        if (
            not normalized_exe
            or normalized_exe.startswith("/")
            or ":" in normalized_exe
            or ".." in normalized_exe.split("/")
        ):
            errors.append("sample_installed_executable_path_invalid")
        installed_length = sample.get("installedLength")
        installed_sha256 = sample.get("installedSha256")
        if (
            not _is_nonnegative_int(installed_length)
            or installed_length <= 0
            or not SHA256_RE.fullmatch(str(installed_sha256 or ""))
        ):
            errors.append("sample_installed_executable_identity_invalid")
        else:
            installed_executable_identities[variant].add(
                (normalized_exe, installed_length, installed_sha256)
            )
        if sample.get("installedVersion") != value.get("expectedVersion"):
            errors.append("sample_installed_version_mismatch")
        if not _is_nonnegative_int(sample.get("inventoryDurationMs")):
            errors.append("sample_inventory_duration_invalid")
        consistency = inventory_consistency.get(variant, {})
        if sample.get("installedTotalBytes") != consistency.get("totalBytes"):
            errors.append("sample_installed_bytes_mismatch")
        if sample.get("installedFileCount") != consistency.get("fileCount"):
            errors.append("sample_installed_files_mismatch")
        if sample.get("installedTreeSha256") != consistency.get("treeSha256"):
            errors.append("sample_installed_tree_mismatch")
        if sample.get("warmup") is True:
            warmups[variant] += 1
            continue
        if sample.get("warmup") is not False:
            errors.append("sample_warmup_flag_invalid")
            continue
        pair = sample.get("pair")
        if not isinstance(pair, int) or isinstance(pair, bool) or not (1 <= pair <= pair_count):
            errors.append("sample_pair_invalid")
            continue
        measured_by_pair.setdefault(pair, []).append(sample)
        measured_values[variant].append(stable_ms)

    for variant_name, count in warmups.items():
        if count != warmup_count:
            errors.append(f"warmup_{variant_name}_count_mismatch")
        if len(installed_executable_identities[variant_name]) != 1:
            errors.append(f"installed_executable_{variant_name}_changed")
    if sequences != set(range(1, len(samples) + 1)):
        errors.append("sample_sequence_not_contiguous")
    for pair in range(1, pair_count + 1):
        pair_samples = measured_by_pair.get(pair, [])
        expected_order = ("baseline", "candidate") if pair % 2 else (
            "candidate",
            "baseline",
        )
        expected_label = "AB" if pair % 2 else "BA"
        if len(pair_samples) != 2:
            errors.append(f"pair_{pair}_sample_count_mismatch")
            continue
        by_position = {sample.get("position"): sample for sample in pair_samples}
        if set(by_position) != {1, 2}:
            errors.append(f"pair_{pair}_positions_invalid")
            continue
        for position, expected_variant in enumerate(expected_order, start=1):
            sample = by_position[position]
            if sample.get("variant") != expected_variant:
                errors.append(f"pair_{pair}_order_invalid")
            if sample.get("order") != expected_label:
                errors.append(f"pair_{pair}_label_invalid")

    statistics = value.get("statistics")
    if not isinstance(statistics, dict):
        statistics = {}
        errors.append("statistics_missing")
    validated_statistics: dict[str, dict[str, Any]] = {}
    for variant_name in ("baseline", "candidate"):
        values = measured_values[variant_name]
        report_stats = statistics.get(variant_name)
        if not isinstance(report_stats, dict) or not values:
            errors.append(f"statistics_{variant_name}_invalid")
            continue
        p50_twice = _median_twice(values)
        p50 = p50_twice // 2 if p50_twice % 2 == 0 else p50_twice / 2
        p95 = _nearest_rank_p95(values)
        reported_p50 = report_stats.get("p50Ms")
        if (
            isinstance(reported_p50, bool)
            or not isinstance(reported_p50, (int, float))
            or not math.isfinite(float(reported_p50))
            or float(reported_p50) * 2 != p50_twice
        ):
            errors.append(f"statistics_{variant_name}_p50_mismatch")
        reported_p95 = report_stats.get("p95Ms")
        if (
            isinstance(reported_p95, bool)
            or not isinstance(reported_p95, int)
            or reported_p95 != p95
        ):
            errors.append(f"statistics_{variant_name}_p95_mismatch")
        if report_stats.get("count") != len(values) or len(values) != pair_count:
            errors.append(f"statistics_{variant_name}_count_mismatch")
        if report_stats.get("minimumMs") != min(values):
            errors.append(f"statistics_{variant_name}_minimum_mismatch")
        if report_stats.get("maximumMs") != max(values):
            errors.append(f"statistics_{variant_name}_maximum_mismatch")
        validated_statistics[variant_name] = {
            "count": len(values),
            "p50Ms": p50,
            "p95Ms": p95,
            "p50Twice": p50_twice,
        }

    summary = _timing_summary(
        value,
        evidence_sha256=evidence_sha256,
        baseline_statistics=validated_statistics.get("baseline"),
        candidate_statistics=validated_statistics.get("candidate"),
        parent_installer_bytes=int(parent["installer"]["length"]),
        candidate_installer_bytes=int(candidate["installer"]["length"]),
    )
    if errors:
        return (
            summary,
            _computed_gate(
                "fail",
                invalidEvidence=True,
                evidenceSha256=evidence_sha256,
                minimumPairCount=MINIMUM_TIMING_PAIR_COUNT,
                errorCodes=sorted(set(errors)),
            ),
            True,
        )

    baseline_stats = validated_statistics["baseline"]
    candidate_stats = validated_statistics["candidate"]
    p50_pass = (
        int(candidate_stats["p50Twice"]) * 100
        <= int(baseline_stats["p50Twice"]) * 105
    )
    p95_pass = int(candidate_stats["p95Ms"]) * 100 <= int(
        baseline_stats["p95Ms"]
    ) * 105
    p50_ratio = _ratio_basis_points(
        int(candidate_stats["p50Twice"]), int(baseline_stats["p50Twice"])
    )
    p95_ratio = _ratio_basis_points(
        int(candidate_stats["p95Ms"]) * 2,
        int(baseline_stats["p95Ms"]) * 2,
    )
    return (
        summary,
        _computed_gate(
            "pass" if p50_pass and p95_pass else "fail",
            invalidEvidence=False,
            evidenceSha256=evidence_sha256,
            minimumPairCount=MINIMUM_TIMING_PAIR_COUNT,
            maximumRegressionBasisPoints=500,
            p50WithinLimit=p50_pass,
            p95WithinLimit=p95_pass,
            p50RatioBasisPoints=p50_ratio,
            p95RatioBasisPoints=p95_ratio,
        ),
        False,
    )


def _computed_gate(status: str, **details: Any) -> dict[str, Any]:
    if status not in GATE_STATUSES:
        raise AssertionError(f"Invalid computed gate status: {status}")
    return {"status": status, **details}


def _component_deltas(
    parent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, int]:
    parent_components = parent.get("components", {})
    candidate_components = candidate.get("components", {})
    names = sorted(set(parent_components) | set(candidate_components))
    return {
        name: int(candidate_components.get(name, {}).get("rawBytes", 0))
        - int(parent_components.get(name, {}).get("rawBytes", 0))
        for name in names
    }


def _pyz_root_deltas(
    parent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, int]:
    names = sorted(set(parent) | set(candidate), key=lambda item: (item.casefold(), item))
    return {
        name: int(candidate.get(name, {}).get("compressedBytes", 0))
        - int(parent.get(name, {}).get("compressedBytes", 0))
        for name in names
    }


def _delta_percent(delta_bytes: int, baseline_bytes: int) -> float | None:
    if baseline_bytes <= 0:
        return None
    return round(delta_bytes * 100.0 / baseline_bytes, 6)


def evaluate_candidate(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    run_id: str,
    packet_id: str,
    parent_champion_id: str,
    hypothesis: str,
    source_commit: str,
    parent_inventory: Mapping[str, Any] | None = None,
    comparison_kind: str = "payload",
    gate_results: Mapping[str, Any] | None = None,
    install_measurements: Mapping[str, Any] | None = None,
    install_measurements_sha256: str | None = None,
    min_absolute_reduction_bytes: int = 256 * 1024,
    min_relative_basis_points: int = 25,
) -> dict[str, Any]:
    contract_and_schema = (
        baseline.get("baselineContract"),
        baseline.get("schemaVersion"),
    )
    if contract_and_schema not in {
        (BASELINE_CONTRACT, BASELINE_SCHEMA_VERSION),
        (LEGACY_BASELINE_CONTRACT, LEGACY_BASELINE_SCHEMA_VERSION),
    }:
        raise InventoryError("baseline contract or schemaVersion is unsupported.")
    if not baseline.get("accepted"):
        raise InventoryError("Candidate evaluation requires an accepted baseline.")
    baseline_inventory = baseline.get("inventory")
    if not isinstance(baseline_inventory, dict):
        raise InventoryError("Accepted baseline has no embedded canonical inventory.")
    _validate_inventory(baseline_inventory, label="baseline")
    _validate_accepted_baseline(baseline, baseline_inventory)
    _validate_inventory(candidate, label="candidate")
    parent = parent_inventory if parent_inventory is not None else baseline_inventory
    _validate_inventory(parent, label="parent")

    run_id = validate_run_id(run_id)
    packet_id = validate_replica_id(packet_id, field="packet_id")
    if parent_champion_id != "baseline":
        parent_champion_id = validate_replica_id(
            parent_champion_id, field="parent_champion_id"
        )
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise InventoryError("hypothesis must be a non-empty string.")
    source_commit = validate_source_commit(source_commit)
    if comparison_kind not in {"payload", "compression"}:
        raise InventoryError("comparison_kind must be payload or compression.")
    if min_absolute_reduction_bytes < 0 or min_relative_basis_points < 0:
        raise InventoryError("Reduction thresholds must be non-negative.")

    parent_installer_bytes = int(parent["installer"]["length"])
    candidate_installer_bytes = int(candidate["installer"]["length"])
    installer_reduction = parent_installer_bytes - candidate_installer_bytes
    proportional_numerator = parent_installer_bytes * min_relative_basis_points
    proportional_threshold = (proportional_numerator + 9_999) // 10_000
    required_reduction = max(min_absolute_reduction_bytes, proportional_threshold)

    parent_staged = parent["payload"]["staged"]
    candidate_staged = candidate["payload"]["staged"]
    parent_installed = parent["payload"].get("installed")
    candidate_installed = candidate["payload"].get("installed")

    binding_checks = {
        "baselineRunId": baseline.get("runId") == run_id,
        "baselineInventoryRunId": baseline_inventory.get("runId") == run_id,
        "baselineSourceCommit": baseline.get("sourceCommit")
        == baseline_inventory.get("sourceCommit"),
        "candidateInventoryOk": bool(candidate.get("ok")),
        "candidateRunId": candidate.get("runId") == run_id,
        "candidateSourceCommit": candidate.get("sourceCommit") == source_commit,
        "candidatePacketId": candidate.get("buildProvenance", {}).get("replicaId")
        == packet_id,
        "evaluatorHash": candidate.get("evaluatorHash") == baseline.get("evaluatorHash"),
        "toolchainHash": candidate.get("toolchainHash") == baseline.get("toolchainHash"),
        "componentMap": candidate.get("componentMap", {}).get("sha256")
        == baseline.get("componentMapSha256"),
        "productVersion": candidate.get("productVersion") == baseline.get("productVersion"),
        "pyinstallerVersion": candidate.get("backendExecutable", {}).get(
            "pyinstallerVersion"
        )
        == baseline_inventory.get("backendExecutable", {}).get("pyinstallerVersion"),
        "parentInventoryOk": bool(parent.get("ok")),
        "parentRunId": parent.get("runId") == run_id,
        "parentChampionId": (
            parent_champion_id == "baseline"
            if parent_inventory is None
            else parent.get("buildProvenance", {}).get("replicaId")
            == parent_champion_id
        ),
        "parentEvaluatorHash": parent.get("evaluatorHash") == baseline.get("evaluatorHash"),
        "parentToolchainHash": parent.get("toolchainHash") == baseline.get("toolchainHash"),
        "parentComponentMap": parent.get("componentMap", {}).get("sha256")
        == baseline.get("componentMapSha256"),
        "parentProductVersion": parent.get("productVersion") == baseline.get("productVersion"),
    }
    computed_gates: dict[str, dict[str, Any]] = {
        "bindings": _computed_gate(
            "pass" if all(binding_checks.values()) else "fail",
            checks=binding_checks,
        ),
        "installerReduction": _computed_gate(
            "pass" if installer_reduction >= required_reduction else "fail",
            parentBytes=parent_installer_bytes,
            candidateBytes=candidate_installer_bytes,
            reductionBytes=installer_reduction,
            requiredReductionBytes=required_reduction,
            minAbsoluteReductionBytes=min_absolute_reduction_bytes,
            minRelativeBasisPoints=min_relative_basis_points,
        ),
        "stagedPayloadNonGrowth": _computed_gate(
            "pass"
            if int(candidate_staged["totalBytes"]) <= int(parent_staged["totalBytes"])
            else "fail",
            parentBytes=parent_staged["totalBytes"],
            candidateBytes=candidate_staged["totalBytes"],
        ),
        "componentPartition": _computed_gate(
            "pass"
            if candidate_staged.get("componentBytesSum") == candidate_staged.get("totalBytes")
            else "fail",
            totalBytes=candidate_staged.get("totalBytes"),
            componentBytesSum=candidate_staged.get("componentBytesSum"),
        ),
        "pyinstallerPartition": _computed_gate(
            "pass"
            if candidate["backendExecutable"].get("virtualPartitionBytes")
            == candidate["backendExecutable"].get("length")
            else "fail",
            executableBytes=candidate["backendExecutable"].get("length"),
            partitionBytes=candidate["backendExecutable"].get("virtualPartitionBytes"),
        ),
    }
    if parent_installed is None or candidate_installed is None:
        computed_gates["installedPayloadNonGrowth"] = _computed_gate(
            "not_run",
            reason="Both parent and candidate installed inventories are required.",
        )
    else:
        computed_gates["installedPayloadNonGrowth"] = _computed_gate(
            "pass"
            if int(candidate_installed["totalBytes"]) <= int(parent_installed["totalBytes"])
            else "fail",
            parentBytes=parent_installed["totalBytes"],
            candidateBytes=candidate_installed["totalBytes"],
        )

    if comparison_kind == "payload":
        compression_same = candidate.get("compression") == parent.get("compression")
        computed_gates["compressionBinding"] = _computed_gate(
            "pass" if compression_same else "fail",
            parent=parent.get("compression"),
            candidate=candidate.get("compression"),
        )
        computed_gates["semanticPayloadIdentity"] = _computed_gate(
            "not_applicable",
            reason="Payload experiments are expected to change semantic payload identity.",
        )
    else:
        semantic_same = (
            candidate_staged.get("semanticTreeSha256")
            == parent_staged.get("semanticTreeSha256")
            and candidate_staged.get("fileListSha256") == parent_staged.get("fileListSha256")
            and candidate_staged.get("totalBytes") == parent_staged.get("totalBytes")
        )
        computed_gates["compressionBinding"] = _computed_gate(
            "not_applicable",
            reason="Compression experiments may intentionally change the NSIS method.",
        )
        computed_gates["semanticPayloadIdentity"] = _computed_gate(
            "pass" if semantic_same else "fail",
            parentSemanticTreeSha256=parent_staged.get("semanticTreeSha256"),
            candidateSemanticTreeSha256=candidate_staged.get("semanticTreeSha256"),
            parentBytes=parent_staged.get("totalBytes"),
            candidateBytes=candidate_staged.get("totalBytes"),
        )

    timing_summary, timing_gate, timing_invalid = validate_install_measurements(
        install_measurements,
        evidence_sha256=install_measurements_sha256,
        run_id=run_id,
        packet_id=packet_id,
        parent_champion_id=parent_champion_id,
        source_commit=source_commit,
        parent=parent,
        candidate=candidate,
    )
    computed_gates["installTimingRegression"] = timing_gate
    if comparison_kind == "payload":
        computed_gates["combinedInstall50"] = _computed_gate(
            "not_applicable",
            reason="Combined download/install promotion is compression-lane-only.",
        )
    elif install_measurements is None:
        computed_gates["combinedInstall50"] = _computed_gate(
            "not_run",
            reason="Compression promotion requires counterbalanced timing evidence.",
        )
    elif timing_invalid:
        computed_gates["combinedInstall50"] = _computed_gate(
            "fail",
            reason="Compression timing evidence is invalid.",
        )
    else:
        baseline_total = timing_summary["baseline"]["totalInstallSeconds50P50"]
        candidate_total = timing_summary["candidate"]["totalInstallSeconds50P50"]
        baseline_total_ns = timing_summary["baseline"][
            "totalInstallNanoseconds50P50"
        ]
        candidate_total_ns = timing_summary["candidate"][
            "totalInstallNanoseconds50P50"
        ]
        improvement_nanoseconds = baseline_total_ns - candidate_total_ns
        relative_required_nanoseconds = (
            baseline_total_ns
            + MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR
            - 1
        ) // MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR
        required_improvement_nanoseconds = max(
            MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS,
            relative_required_nanoseconds,
        )
        combined_improves = (
            improvement_nanoseconds >= required_improvement_nanoseconds
        )
        computed_gates["combinedInstall50"] = _computed_gate(
            "pass" if combined_improves else "fail",
            bandwidthBitsPerSecond=50_000_000,
            parentTotalSecondsP50=baseline_total,
            candidateTotalSecondsP50=candidate_total,
            parentTotalNanosecondsP50=baseline_total_ns,
            candidateTotalNanosecondsP50=candidate_total_ns,
            improvementNanoseconds=improvement_nanoseconds,
            requiredImprovementNanoseconds=required_improvement_nanoseconds,
            minimumAbsoluteImprovementNanoseconds=(
                MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS
            ),
            minimumRelativeImprovementBasisPoints=100,
            improvementSeconds=round(improvement_nanoseconds / 1_000_000_000, 6),
        )

    external_gates = _validate_external_gates(
        gate_results,
        run_id=run_id,
        packet_id=packet_id,
        parent_champion_id=parent_champion_id,
        source_commit=source_commit,
    )
    duplicate_gate_names = set(computed_gates) & set(external_gates)
    if duplicate_gate_names:
        raise InventoryError(
            "External gates collide with evaluator-owned gates: "
            + ", ".join(sorted(duplicate_gate_names))
        )
    gates = {**computed_gates, **external_gates}
    computed_failures = sorted(
        name for name, gate in computed_gates.items() if gate["status"] == "fail"
    )
    external_failures = sorted(
        name for name, gate in external_gates.items() if gate["status"] == "fail"
    )
    mandatory_not_applicable = sorted(
        name
        for name in MANDATORY_EXTERNAL_GATES
        if external_gates[name]["status"] == "not_applicable"
    )
    incomplete_gates = sorted(
        name for name, gate in gates.items() if gate["status"] == "not_run"
    )
    if timing_invalid:
        decision = "invalid_measurement"
        reason_codes = ["invalid_install_timing_evidence"]
    elif computed_failures:
        decision = "discard"
        reason_codes = [f"gate_failed:{name}" for name in computed_failures]
    elif external_failures or mandatory_not_applicable:
        decision = "checks_failed"
        reason_codes = [f"gate_failed:{name}" for name in external_failures]
        reason_codes.extend(
            f"gate_not_applicable:{name}" for name in mandatory_not_applicable
        )
    elif incomplete_gates:
        decision = "measure_only"
        reason_codes = [f"gate_not_run:{name}" for name in incomplete_gates]
    else:
        decision = "keep"
        reason_codes = []

    baseline_inventory_for_delta = baseline["inventory"]
    baseline_installer_bytes = int(baseline_inventory_for_delta["installer"]["length"])
    baseline_staged_bytes = int(
        baseline_inventory_for_delta["payload"]["staged"]["totalBytes"]
    )
    candidate_installed_bytes = (
        int(candidate_installed["totalBytes"]) if candidate_installed is not None else None
    )
    parent_installed_bytes = (
        int(parent_installed["totalBytes"]) if parent_installed is not None else None
    )
    installer_delta = candidate_installer_bytes - parent_installer_bytes
    staged_delta = int(candidate_staged["totalBytes"]) - int(parent_staged["totalBytes"])
    installed_delta = (
        candidate_installed_bytes - parent_installed_bytes
        if candidate_installed_bytes is not None and parent_installed_bytes is not None
        else None
    )

    return {
        "resultContract": RESULT_CONTRACT,
        "schemaVersion": SCHEMA_VERSION,
        "evaluatedAtUtc": _utc_now(),
        "runId": run_id,
        "packetId": packet_id,
        "parentChampionId": parent_champion_id,
        "hypothesis": hypothesis.strip(),
        "sourceCommit": source_commit,
        "comparisonKind": comparison_kind,
        "evaluatorHash": candidate["evaluatorHash"],
        "toolchainHash": candidate["toolchainHash"],
        "compression": candidate["compression"],
        "installer": {
            "path": candidate["installer"]["name"],
            "name": candidate["installer"]["name"],
            "length": candidate_installer_bytes,
            "sha256": candidate["installer"]["sha256"],
            "deltaBytes": installer_delta,
            "deltaPercent": _delta_percent(installer_delta, parent_installer_bytes),
            "baselineDeltaBytes": candidate_installer_bytes - baseline_installer_bytes,
            "baselineDeltaPercent": _delta_percent(
                candidate_installer_bytes - baseline_installer_bytes,
                baseline_installer_bytes,
            ),
        },
        "payload": {
            "stagedBytes": candidate_staged["totalBytes"],
            "installedBytes": candidate_installed_bytes,
            "exactTreeSha256": candidate_staged["exactTreeSha256"],
            "semanticTreeSha256": candidate_staged["semanticTreeSha256"],
            "fileListSha256": candidate_staged["fileListSha256"],
            "deltaBytes": staged_delta,
            "installedDeltaBytes": installed_delta,
            "baselineDeltaBytes": int(candidate_staged["totalBytes"]) - baseline_staged_bytes,
        },
        "attribution": {
            "componentMapSha256": candidate["componentMap"]["sha256"],
            "components": candidate_staged["components"],
            "pyzInventorySha256": candidate["backendExecutable"]["pyzDiagnostics"]["inventorySha256"],
            "componentDeltas": _component_deltas(parent_staged, candidate_staged),
            "pyzRootDeltas": _pyz_root_deltas(
                parent["backendExecutable"]["pyzDiagnostics"]["roots"],
                candidate["backendExecutable"]["pyzDiagnostics"]["roots"],
            ),
        },
        "installMeasurements": timing_summary,
        "gates": gates,
        "decision": decision,
        "reasonCodes": reason_codes,
    }
