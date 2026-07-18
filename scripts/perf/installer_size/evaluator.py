from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.installer_research.comparator import (
    MANDATORY_EXTERNAL_GATES,
    MINIMUM_TIMING_PAIR_COUNT,
)
from scripts.perf.autoresearch_profiles import canonical_run_id


RESULT_CONTRACT = "InstallerResearchResultV1"
SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DECISIONS = {
    "baseline_accept",
    "keep",
    "discard",
    "checks_failed",
    "invalid_measurement",
    "crash",
    "measure_only",
}
GATE_STATUSES = {"pass", "fail", "not_run", "not_applicable"}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def document_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _finding(code: str, message: str, *, field: str = "") -> dict[str, Any]:
    finding: dict[str, Any] = {"level": "block", "code": code, "message": message}
    if field:
        finding["field"] = field
    return finding


def _nested(payload: dict[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _valid_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _validate_gate_value(name: str, value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        status = value
    elif isinstance(value, dict):
        status = value.get("status")
    else:
        return [_finding("result_gate_invalid", f"gate {name} has no explicit status", field=f"gates.{name}")]
    if status not in GATE_STATUSES:
        return [
            _finding(
                "result_gate_status_invalid",
                f"gate {name} status must be one of {sorted(GATE_STATUSES)}",
                field=f"gates.{name}",
            )
        ]
    return []


def validate_result(payload: dict[str, Any], *, expected_run_id: str | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if payload.get("resultContract") != RESULT_CONTRACT:
        findings.append(_finding("result_contract_mismatch", "resultContract must be InstallerResearchResultV1"))
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        findings.append(_finding("result_schema_mismatch", "result schemaVersion must be 1"))
    try:
        run_id = canonical_run_id(str(payload.get("runId") or ""))
    except ValueError:
        findings.append(_finding("result_run_id_invalid", "result RunId is not a canonical RFC 4122 UUID"))
        run_id = ""
    if expected_run_id and run_id != expected_run_id:
        findings.append(_finding("result_run_id_mismatch", "result RunId does not match the active run"))
    packet_id = payload.get("packetId")
    if not isinstance(packet_id, str) or not packet_id.strip():
        findings.append(_finding("result_packet_id_missing", "result packetId is required"))
    if not isinstance(payload.get("hypothesis"), (dict, str)):
        findings.append(_finding("result_hypothesis_missing", "result hypothesis must be an object or string"))
    parent_champion_id = payload.get("parentChampionId")
    if not isinstance(parent_champion_id, str) or not parent_champion_id.strip():
        findings.append(_finding("result_parent_champion_missing", "result parentChampionId is required"))
    if payload.get("comparisonKind") not in {"payload", "compression"}:
        findings.append(_finding("result_comparison_kind_invalid", "comparisonKind must be payload or compression"))
    source_commit = str(payload.get("sourceCommit") or "")
    if not (COMMIT_PATTERN.fullmatch(source_commit) or re.fullmatch(r"^[0-9a-f]{64}$", source_commit)):
        findings.append(_finding("result_source_commit_invalid", "sourceCommit must be a lowercase 40- or 64-hex source identity"))
    for field in ("evaluatorHash", "toolchainHash"):
        if not SHA256_PATTERN.fullmatch(str(payload.get(field) or "")):
            findings.append(_finding("result_hash_invalid", f"{field} must be a SHA-256", field=field))
    if payload.get("compression") not in {"bzip2", "zlib", "lzma"}:
        findings.append(_finding("result_compression_invalid", "compression is not allowlisted"))

    installer = payload.get("installer")
    if not isinstance(installer, dict):
        findings.append(_finding("result_installer_missing", "installer result object is required"))
    else:
        name = installer.get("name")
        if not isinstance(name, str) or not name.startswith("Scriber_") or not name.endswith("_x64-setup.exe"):
            findings.append(_finding("result_installer_name_invalid", "installer name is not the explicit NSIS setup artifact"))
        if not _valid_nonnegative_int(installer.get("length")) or installer.get("length") == 0:
            findings.append(_finding("result_installer_length_invalid", "installer length must be a positive integer"))
        if not SHA256_PATTERN.fullmatch(str(installer.get("sha256") or "")):
            findings.append(_finding("result_installer_hash_invalid", "installer sha256 is invalid"))
        delta_percent = installer.get("deltaPercent")
        if delta_percent is not None and (
            isinstance(delta_percent, bool)
            or not isinstance(delta_percent, (int, float))
            or not math.isfinite(float(delta_percent))
        ):
            findings.append(_finding("result_installer_delta_invalid", "installer deltaPercent must be finite or null"))

    payload_result = payload.get("payload")
    if not isinstance(payload_result, dict):
        findings.append(_finding("result_payload_missing", "payload result object is required"))
    else:
        if not _valid_nonnegative_int(payload_result.get("stagedBytes")) or payload_result.get("stagedBytes") == 0:
            findings.append(_finding("result_staged_bytes_invalid", "payload stagedBytes must be positive"))
        installed = payload_result.get("installedBytes")
        if installed is not None and (not _valid_nonnegative_int(installed) or installed == 0):
            findings.append(_finding("result_installed_bytes_invalid", "payload installedBytes must be positive or null"))
        for field in ("exactTreeSha256", "semanticTreeSha256", "fileListSha256"):
            if not SHA256_PATTERN.fullmatch(str(payload_result.get(field) or "")):
                findings.append(_finding("result_payload_hash_invalid", f"payload {field} is invalid", field=f"payload.{field}"))

    attribution = payload.get("attribution")
    if not isinstance(attribution, dict):
        findings.append(_finding("result_attribution_missing", "attribution result object is required"))
    else:
        for field in ("componentMapSha256", "pyzInventorySha256"):
            if not SHA256_PATTERN.fullmatch(str(attribution.get(field) or "")):
                findings.append(_finding("result_attribution_hash_invalid", f"attribution {field} is invalid"))
        components = attribution.get("components")
        if not isinstance(components, dict) or not components:
            findings.append(_finding("result_components_missing", "attribution components must be a non-empty object"))
        for field in ("componentDeltas", "pyzRootDeltas"):
            if not isinstance(attribution.get(field), dict):
                findings.append(_finding("result_delta_object_invalid", f"attribution {field} must be an object"))

    gates = payload.get("gates")
    if not isinstance(gates, dict):
        findings.append(_finding("result_gates_missing", "gates must be an object"))
    else:
        for name, value in gates.items():
            findings.extend(_validate_gate_value(str(name), value))
        for name in MANDATORY_EXTERNAL_GATES:
            gate = gates.get(name)
            if isinstance(gate, dict) and gate.get("status") == "pass" and not SHA256_PATTERN.fullmatch(
                str(gate.get("evidenceSha256") or "")
            ):
                findings.append(
                    _finding(
                        "result_gate_evidence_hash_invalid",
                        f"passing mandatory gate {name} requires a lowercase evidenceSha256",
                        field=f"gates.{name}.evidenceSha256",
                    )
                )
    measurements = payload.get("installMeasurements")
    if measurements is not None and not isinstance(measurements, dict):
        findings.append(_finding("result_install_measurements_invalid", "installMeasurements must be an object or null"))
    elif isinstance(measurements, dict):
        expected_measurement_bindings = {
            "runId": run_id,
            "packetId": packet_id,
            "parentChampionId": parent_champion_id,
            "sourceCommit": source_commit,
        }
        for field, expected in expected_measurement_bindings.items():
            if measurements.get(field) != expected:
                findings.append(_finding("result_install_measurement_binding_mismatch", f"installMeasurements {field} differs from the result"))
        if not SHA256_PATTERN.fullmatch(str(measurements.get("evidenceSha256") or "")):
            findings.append(_finding("result_install_measurement_hash_invalid", "installMeasurements evidenceSha256 is invalid"))
        if measurements.get("apiVersion") != "1" or measurements.get("kind") != "installer-ab-timing":
            findings.append(_finding("result_install_measurement_contract_invalid", "installMeasurements contract is invalid"))
        if (
            isinstance(measurements.get("pairCount"), bool)
            or not isinstance(measurements.get("pairCount"), int)
            or measurements.get("pairCount", 0) < MINIMUM_TIMING_PAIR_COUNT
        ):
            findings.append(
                _finding(
                    "result_install_measurement_pairs_invalid",
                    f"installMeasurements requires at least {MINIMUM_TIMING_PAIR_COUNT} pairs",
                )
            )
    if payload.get("decision") not in DECISIONS:
        findings.append(_finding("result_decision_invalid", "result decision is not allowlisted"))
    reason_codes = payload.get("reasonCodes")
    if not isinstance(reason_codes, list) or any(not isinstance(item, str) for item in reason_codes):
        findings.append(_finding("result_reason_codes_invalid", "reasonCodes must be an array of strings"))
    return findings


def load_result(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid result JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"result must contain a JSON object: {path}")
    return value
