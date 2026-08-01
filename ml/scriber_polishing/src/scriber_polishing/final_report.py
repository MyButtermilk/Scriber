"""Evidence-bound, fail-closed composition of the Section 49 final report.

The composer never probes Git, GitHub, Hugging Face, training hardware, or
model artifacts.  It only consumes immutable JSON evidence whose exact bytes
are declared in a source-binding manifest.  Consequently it cannot turn an
absent measurement into zero or turn an absent external action into success.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator


class FinalReportError(ValueError):
    """Raised when final-report evidence is incomplete or inconsistent."""


CONTRACTS_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "contracts"

SOURCE_ROLES: Final = (
    "preflight",
    "dataset",
    "critic",
    "ai_gold",
    "final_training",
    "evaluation",
    "terra_judge",
    "sol_judge",
    "quantization",
    "benchmark",
    "hub_verification",
    "git_pr",
    "release_status",
    "provenance",
)

PROVENANCE_ASSERTIONS: Final = (
    "no_openai_api_used",
    "no_other_generative_model_api_used",
    "no_openai_api_key_used",
    "no_additional_codex_credits_purchased",
    "codex_exclusively_via_chatgpt_pro",
    "all_training_data_ai_or_local_generator_created",
    "no_human_curation",
    "no_human_gold_set",
    "no_manual_example_review_required",
    "bulk_data_generated_locally",
    "test_and_challenge_not_trained",
    "no_private_scriber_data_without_opt_in",
)

PRODUCT_STATUSES: Final = {
    "AI_DATA_FACTORY_IN_PROGRESS",
    "READY_FOR_TRAINING",
    "TRAINING_IN_PROGRESS",
    "READY_FOR_AI_JUDGING",
    "RELEASED",
    "PRIVATE_CANDIDATE",
    "BLOCKED_BY_INCLUDED_CODEX_LIMIT",
    "BLOCKED_BY_EXTERNAL_PREREQUISITE",
    "FAILED",
}

# Every Section 49 fact has a deliberately narrow source allow-list.  Bindings
# contain JSON Pointers, never literals, so the resulting value must occur in
# the hash-bound source bytes.
FACT_SOURCES: Final[dict[str, frozenset[str]]] = {
    "git_branch": frozenset({"git_pr"}),
    "git_commit": frozenset({"git_pr"}),
    "pull_request_url": frozenset({"git_pr"}),
    "base_model": frozenset({"final_training"}),
    "base_model_revision": frozenset({"final_training"}),
    "training_method": frozenset({"final_training"}),
    "gpu": frozenset({"preflight", "final_training"}),
    "actual_vram_mib": frozenset({"preflight"}),
    "operating_system": frozenset({"preflight", "benchmark"}),
    "python_version": frozenset({"preflight", "benchmark"}),
    "cuda_version": frozenset({"preflight", "final_training"}),
    "package_versions": frozenset({"preflight", "final_training"}),
    "final_hyperparameters": frozenset({"final_training"}),
    "training_duration_seconds": frozenset({"final_training"}),
    "peak_vram_mib": frozenset({"final_training"}),
    "synthetic_dataset_size": frozenset({"dataset"}),
    "canonical_document_count": frozenset({"dataset"}),
    "ai_seed_count": frozenset({"dataset", "critic"}),
    "ai_gold_example_count": frozenset({"ai_gold"}),
    "rejected_example_count": frozenset({"critic", "dataset"}),
    "rejection_reasons": frozenset({"critic", "dataset"}),
    "generator_agents": frozenset({"dataset", "critic"}),
    "critic_agents": frozenset({"critic"}),
    "critic_disagreement_rate": frozenset({"critic"}),
    "business_mail_rate": frozenset({"dataset"}),
    "tax_law_rate": frozenset({"dataset"}),
    "formatting_case_count": frozenset({"dataset"}),
    "unit_case_count": frozenset({"dataset"}),
    "legal_citation_case_count": frozenset({"dataset"}),
    "address_pronoun_case_count": frozenset({"dataset"}),
    "orthography_variant_count": frozenset({"dataset"}),
    "data_rejection_rate": frozenset({"critic", "dataset"}),
    "automatic_metrics": frozenset({"evaluation"}),
    "ai_gold_results": frozenset({"ai_gold", "evaluation"}),
    "terra_judge_results": frozenset({"terra_judge"}),
    "sol_judge_results": frozenset({"sol_judge"}),
    "critical_errors": frozenset({"evaluation", "release_status"}),
    "quantization": frozenset({"quantization"}),
    "model_size_bytes": frozenset({"benchmark", "quantization"}),
    "cold_start_seconds": frozenset({"benchmark"}),
    "warm_start_seconds": frozenset({"benchmark"}),
    "latency_p50_seconds": frozenset({"benchmark"}),
    "latency_p95_seconds": frozenset({"benchmark"}),
    "latency_p99_seconds": frozenset({"benchmark"}),
    "hf_model_repository": frozenset({"hub_verification"}),
    "model_revision": frozenset({"hub_verification"}),
    "hf_dataset_repository": frozenset({"hub_verification"}),
    "dataset_revision": frozenset({"hub_verification"}),
    "visibility": frozenset({"hub_verification"}),
    "reload_test": frozenset({"hub_verification"}),
    "final_status": frozenset({"release_status"}),
    "known_limitations": frozenset({"provenance"}),
    "integration_recommendation": frozenset({"provenance"}),
}

AUTO_PROVENANCE_FACTS: Final = frozenset(
    {"known_limitations", "integration_recommendation"}
)

# Identity-bearing facts use exact contract locations.  This prevents a valid
# source from being bound to a semantically unrelated string of the same type.
EXACT_POINTERS: Final = {
    "git_branch": frozenset({"/branch"}),
    "git_commit": frozenset({"/commit"}),
    "pull_request_url": frozenset({"/pull_request/url"}),
    "base_model": frozenset({"/base_model"}),
    "base_model_revision": frozenset({"/base_revision"}),
    "hf_model_repository": frozenset({"/artifacts/model/repo_id"}),
    "model_revision": frozenset({"/artifacts/model/revision"}),
    "hf_dataset_repository": frozenset({"/artifacts/dataset/repo_id"}),
    "dataset_revision": frozenset({"/artifacts/dataset/revision"}),
    "visibility": frozenset({"/visibility"}),
    "final_status": frozenset({"/status"}),
}

_GERMAN_LABELS: Final = {
    "git_branch": "Git-Branch",
    "git_commit": "Git-Commit",
    "pull_request_url": "Pull Request",
    "base_model": "Basismodell",
    "base_model_revision": "Basismodell-Revision",
    "training_method": "Trainingsmethode",
    "gpu": "GPU",
    "actual_vram_mib": "Tatsächlicher VRAM (MiB)",
    "operating_system": "Betriebssystem",
    "python_version": "Python-Version",
    "cuda_version": "CUDA-Version",
    "package_versions": "Paketversionen",
    "final_hyperparameters": "Finale Hyperparameter",
    "training_duration_seconds": "Trainingsdauer (Sekunden)",
    "peak_vram_mib": "Peak VRAM (MiB)",
    "synthetic_dataset_size": "Synthetische Datensatzgröße",
    "canonical_document_count": "Kanonische Dokumente",
    "ai_seed_count": "AI-Seeds",
    "ai_gold_example_count": "AI-Gold-Beispiele",
    "rejected_example_count": "Verworfene Beispiele",
    "rejection_reasons": "Ablehnungsgründe",
    "generator_agents": "Generatoragenten",
    "critic_agents": "Critic-Agenten",
    "critic_disagreement_rate": "Critic-Disagreement-Rate",
    "business_mail_rate": "Geschäftspostquote",
    "tax_law_rate": "Steuerrechtsquote",
    "formatting_case_count": "Formatierungsfälle",
    "unit_case_count": "Einheitenfälle",
    "legal_citation_case_count": "Gesetzeszitate",
    "address_pronoun_case_count": "Anredepronomenfälle",
    "orthography_variant_count": "Orthografievarianten",
    "data_rejection_rate": "Datenablehnungsquote",
    "automatic_metrics": "Automatische Metriken",
    "ai_gold_results": "AI-Gold-Ergebnisse",
    "terra_judge_results": "Terra-Judge-Ergebnisse",
    "sol_judge_results": "Sol-Judge-Ergebnisse",
    "critical_errors": "Kritische Fehler",
    "quantization": "Quantisierung",
    "model_size_bytes": "Modellgröße (Bytes)",
    "cold_start_seconds": "Cold Start (Sekunden)",
    "warm_start_seconds": "Warm Start (Sekunden)",
    "latency_p50_seconds": "Latenz p50 (Sekunden)",
    "latency_p95_seconds": "Latenz p95 (Sekunden)",
    "latency_p99_seconds": "Latenz p99 (Sekunden)",
    "hf_model_repository": "Hugging-Face-Modell-Repository",
    "model_revision": "Modellrevision",
    "hf_dataset_repository": "Hugging-Face-Dataset-Repository",
    "dataset_revision": "Dataset-Revision",
    "visibility": "Sichtbarkeit",
    "reload_test": "Reload-Test",
    "final_status": "Finaler Status",
    "known_limitations": "Bekannte Einschränkungen",
    "integration_recommendation": "Empfehlung zur Scriber-Integration",
}


def canonical_json_bytes(value: object) -> bytes:
    """Return stable UTF-8 JSON bytes used for integrity bindings."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise FinalReportError(f"value is not canonical JSON: {error}") from error
    return encoded.encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FinalReportError(f"evidence is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _load_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalReportError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise FinalReportError(f"{label} must be a JSON object")
    return value


def _load_schema(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinalReportError(f"contract is not a regular file: {path}")
    return _load_json_bytes(path.read_bytes(), f"contract {path.name}")


def _validate_schema(value: object, schema_path: Path, label: str) -> None:
    schema = _load_schema(schema_path)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    raise FinalReportError(f"{label} schema failed at {location}: {first.message}")


def _safe_relative_file(root: Path, declared: str, label: str) -> Path:
    relative = Path(declared)
    if relative.is_absolute():
        raise FinalReportError(f"{label} path must be relative")
    resolved_root = root.resolve()
    candidate = resolved_root / relative
    current = candidate
    while current != resolved_root:
        if current.is_symlink():
            raise FinalReportError(f"{label} path may not traverse symbolic links")
        if current.parent == current:
            break
        current = current.parent
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise FinalReportError(f"{label} path escapes its declared root")
    if resolved.is_symlink() or not resolved.is_file():
        raise FinalReportError(f"{label} is not a regular file: {declared}")
    return resolved


def _pointer(value: object, pointer: str, label: str) -> object:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise FinalReportError(f"{label} is not an RFC 6901 JSON Pointer")
    current = value
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise FinalReportError(f"{label} contains an invalid JSON Pointer escape")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise FinalReportError(f"{label} does not exist at {pointer}")
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            if not token.isdigit():
                raise FinalReportError(f"{label} has a nonnumeric array index at {pointer}")
            index = int(token)
            if index >= len(current):
                raise FinalReportError(f"{label} array index is out of range at {pointer}")
            current = current[index]
        else:
            raise FinalReportError(f"{label} traverses a scalar at {pointer}")
    return current


def _json_copy(value: object) -> Any:
    return json.loads(canonical_json_bytes(value))


def _nonempty_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FinalReportError(f"{label} must be non-empty text")


def _nonnegative_number(value: object, label: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FinalReportError(f"{label} must be numeric")
    if not math.isfinite(float(value)) or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise FinalReportError(f"{label} must be a finite {qualifier} number")


def _nonnegative_integer(value: object, label: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinalReportError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise FinalReportError(f"{label} must be a {qualifier} integer")


def _validate_fact_value(field: str, value: object) -> None:
    label = f"fact {field}"
    if value is None or isinstance(value, bool):
        raise FinalReportError(f"{label} may not be null or boolean")
    if field in {
        "git_branch",
        "git_commit",
        "pull_request_url",
        "base_model",
        "base_model_revision",
        "training_method",
        "gpu",
        "python_version",
        "cuda_version",
        "hf_model_repository",
        "model_revision",
        "hf_dataset_repository",
        "dataset_revision",
        "visibility",
        "integration_recommendation",
    }:
        _nonempty_text(value, label)
        return
    if field == "operating_system":
        if isinstance(value, str):
            _nonempty_text(value, label)
            return
        if (
            not isinstance(value, dict)
            or not value
            or any(
                not isinstance(item, str) or not item.strip()
                for item in value.values()
            )
        ):
            raise FinalReportError(
                f"{label} must be text or a non-empty object of text values"
            )
        return
    if field in {
        "actual_vram_mib",
        "canonical_document_count",
        "ai_seed_count",
        "ai_gold_example_count",
        "rejected_example_count",
        "formatting_case_count",
        "unit_case_count",
        "legal_citation_case_count",
        "address_pronoun_case_count",
        "orthography_variant_count",
    }:
        _nonnegative_integer(value, label, positive=field == "actual_vram_mib")
        return
    if field == "model_size_bytes":
        _nonnegative_integer(value, label, positive=True)
        return
    if field in {
        "training_duration_seconds",
        "peak_vram_mib",
        "latency_p50_seconds",
        "latency_p95_seconds",
        "latency_p99_seconds",
    }:
        _nonnegative_number(value, label)
        return
    if field in {"critic_disagreement_rate", "business_mail_rate", "tax_law_rate", "data_rejection_rate"}:
        _nonnegative_number(value, label)
        if float(value) > 1:
            raise FinalReportError(f"{label} must be between zero and one")
        return
    if field in {"cold_start_seconds", "warm_start_seconds"}:
        if isinstance(value, int | float) and not isinstance(value, bool):
            _nonnegative_number(value, label)
            return
        if not isinstance(value, dict) or set(value) != {"p50", "p95", "p99"}:
            raise FinalReportError(
                f"{label} must be numeric or contain exactly p50, p95, and p99"
            )
        for percentile, measurement in value.items():
            _nonnegative_number(measurement, f"{label}.{percentile}")
        if not value["p50"] <= value["p95"] <= value["p99"]:
            raise FinalReportError(f"{label} percentiles must be monotonic")
        return
    if field in {"generator_agents", "critic_agents"}:
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            raise FinalReportError(f"{label} must be a non-empty unique string list")
        return
    if field == "final_status":
        if value not in PRODUCT_STATUSES:
            raise FinalReportError(f"{label} is not an allowed product status")
        return
    if field == "known_limitations":
        if not isinstance(value, list):
            raise FinalReportError(f"{label} must be a list")
        return
    # Composite measured values must be non-empty JSON containers or text.
    if isinstance(value, str):
        _nonempty_text(value, label)
    elif isinstance(value, list | dict):
        if not value:
            raise FinalReportError(f"{label} must not be empty")
    elif isinstance(value, int | float):
        _nonnegative_number(value, label)
    else:
        raise FinalReportError(f"{label} has an unsupported JSON type")


def provenance_signed_payload(declaration: Mapping[str, Any]) -> dict[str, Any]:
    """Select the exact declaration body covered by the canonical attestation."""

    fields = (
        "schema_version",
        "declaration_id",
        "issued_at_utc",
        "assertions",
        "evidence_sha256s",
        "known_limitations",
        "integration_recommendation",
    )
    return {field: _json_copy(declaration[field]) for field in fields}


def provenance_signature_value(*, signed_payload_sha256: str, signer_id: str) -> str:
    """Compute the tamper-evident canonical attestation signature value."""

    return sha256_bytes(
        canonical_json_bytes(
            {
                "context": "scriber-final-report-provenance-v1",
                "signed_payload_sha256": signed_payload_sha256,
                "signer_id": signer_id,
            }
        )
    )


def _validate_provenance(
    declaration: Mapping[str, Any],
    *,
    declaration_sha256: str,
    source_hashes: Mapping[str, str],
    contracts_root: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    _validate_schema(
        declaration,
        contracts_root / "final_report_provenance_schema.json",
        "provenance declaration",
    )
    signed_payload_hash = sha256_bytes(canonical_json_bytes(provenance_signed_payload(declaration)))
    if declaration["signed_payload_sha256"] != signed_payload_hash:
        raise FinalReportError("provenance signed_payload_sha256 does not match its canonical body")
    signature = declaration["signature"]
    expected_signature = provenance_signature_value(
        signed_payload_sha256=signed_payload_hash,
        signer_id=signature["signer_id"],
    )
    if signature["value"] != expected_signature:
        raise FinalReportError("provenance canonical attestation signature does not verify")

    expected_evidence = {
        role: digest for role, digest in source_hashes.items() if role != "provenance"
    }
    if declaration["evidence_sha256s"] != expected_evidence:
        raise FinalReportError(
            "provenance evidence_sha256s must bind every non-provenance source exactly"
        )

    limitation_by_field: dict[str, str] = {}
    limitation_by_id: dict[str, Any] = {}
    for limitation in declaration["known_limitations"]:
        limitation_id = limitation["id"]
        if limitation_id in limitation_by_id:
            raise FinalReportError(f"duplicate provenance limitation id: {limitation_id}")
        limitation_by_id[limitation_id] = _json_copy(limitation)
        for field in limitation["affected_fields"]:
            if field not in FACT_SOURCES:
                raise FinalReportError(
                    f"provenance limitation {limitation_id} names unknown field {field}"
                )
            if field == "final_status":
                raise FinalReportError("final_status cannot be replaced by an unknown limitation")
            if field in limitation_by_field:
                raise FinalReportError(f"multiple limitations claim final-report field {field}")
            limitation_by_field[field] = limitation_id
    if declaration_sha256 != source_hashes["provenance"]:
        raise AssertionError("validated provenance hash differs from loaded source hash")
    return limitation_by_field, limitation_by_id


def _source_contract_name(role: str) -> str | None:
    return {
        "hub_verification": "hub_verification_schema.json",
        "release_status": "release_verification_report_schema.json",
        "git_pr": "git_pr_evidence_schema.json",
        "provenance": "final_report_provenance_schema.json",
    }.get(role)


def _validate_source_consistency(sources: Mapping[str, Mapping[str, Any]], hashes: Mapping[str, str]) -> None:
    git = sources.get("git_pr")
    if git is not None:
        if git["branch"] != git["push"]["remote_branch"]:
            raise FinalReportError("Git push remote branch differs from the declared branch")
        if git["branch"] != git["pull_request"]["head_branch"]:
            raise FinalReportError("pull-request head branch differs from the declared branch")
        if git["commit"] != git["push"]["remote_head_sha"]:
            raise FinalReportError("remote pushed SHA differs from the declared Git commit")
        if git["commit"] != git["pull_request"]["head_sha"]:
            raise FinalReportError("pull-request head SHA differs from the declared Git commit")

    training = sources.get("final_training")
    hub = sources.get("hub_verification")
    if training is not None and hub is not None:
        if training.get("run_fingerprint") != hub.get("training_run_fingerprint"):
            raise FinalReportError("Hub training fingerprint differs from final training")
        final_model = training.get("final_model")
        inventory = final_model.get("inventory") if isinstance(final_model, Mapping) else None
        training_tree = inventory.get("tree_sha256") if isinstance(inventory, Mapping) else None
        if training_tree != hub.get("bf16_final_model_tree_sha256"):
            raise FinalReportError("Hub BF16 tree hash differs from final training")
        if hub.get("completed_training_report_sha256") != hashes["final_training"]:
            raise FinalReportError("Hub completed-training report hash differs from exact source bytes")

    if hub is not None:
        fingerprint = hub.get("training_run_fingerprint")
        for role in ("quantization", "benchmark"):
            source = sources.get(role)
            if (
                source is not None
                and "training_fingerprint" in source
                and source["training_fingerprint"] != fingerprint
            ):
                raise FinalReportError(
                    f"{role} training fingerprint differs from Hub/final training"
                )

    release = sources.get("release_status")
    if release is not None:
        components = release.get("components")
        if not isinstance(components, Mapping):
            raise FinalReportError("release status components must be an object")
        if training is not None:
            component = components.get("completed_final_training")
            if not isinstance(component, Mapping) or component.get("report_sha256") not in {
                hashes["final_training"],
                hashes["final_training"].removeprefix("sha256:"),
            }:
                raise FinalReportError(
                    "release status does not bind the exact final-training source hash"
                )
        if hub is not None:
            component = components.get("private_hub_publication")
            if not isinstance(component, Mapping) or component.get("report_sha256") not in {
                hashes["hub_verification"],
                hashes["hub_verification"].removeprefix("sha256:"),
            }:
                raise FinalReportError(
                    "release status does not bind the exact Hub-verification source hash"
                )


def _load_sources(
    manifest: Mapping[str, Any],
    *,
    source_root: Path,
    contracts_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    unknown_roles = sorted(set(manifest["sources"]).difference(SOURCE_ROLES))
    if unknown_roles:
        raise FinalReportError(f"unknown final-report source role: {unknown_roles[0]}")
    values: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for role, binding in manifest["sources"].items():
        path = _safe_relative_file(source_root, binding["path"], f"{role} source")
        payload = path.read_bytes()
        actual_hash = sha256_bytes(payload)
        if actual_hash != binding["sha256"]:
            raise FinalReportError(
                f"{role} source hash mismatch: expected {binding['sha256']}, got {actual_hash}"
            )
        value = _load_json_bytes(payload, f"{role} source")
        if value.get("schema_version") != binding["schema_version"]:
            raise FinalReportError(
                f"{role} source schema_version differs from its binding"
            )

        fixed_contract = _source_contract_name(role)
        declared_contract = binding.get("contract")
        if fixed_contract is not None and declared_contract not in {None, fixed_contract}:
            raise FinalReportError(
                f"{role} must use the exact {fixed_contract} contract"
            )
        contract_name = fixed_contract or declared_contract
        evidence_record: dict[str, Any] = {
            "path": binding["path"],
            "sha256": actual_hash,
            "schema_version": binding["schema_version"],
        }
        if contract_name is not None:
            contract_path = _safe_relative_file(
                contracts_root,
                contract_name,
                f"{role} contract",
            )
            contract_hash = sha256_file(contract_path)
            declared_contract_hash = binding.get("contract_sha256")
            if declared_contract_hash is not None and declared_contract_hash != contract_hash:
                raise FinalReportError(f"{role} contract hash differs from its binding")
            _validate_schema(value, contract_path, f"{role} source")
            evidence_record["contract"] = contract_name
            evidence_record["contract_sha256"] = contract_hash
        values[role] = value
        evidence[role] = evidence_record
    return values, evidence


def _extract_bound_fact(
    field: str,
    binding: Mapping[str, Any],
    *,
    sources: Mapping[str, Mapping[str, Any]],
    evidence: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    role = binding["source"]
    if role not in sources:
        raise FinalReportError(f"fact {field} references missing source {role}")
    if role not in FACT_SOURCES[field]:
        allowed = ", ".join(sorted(FACT_SOURCES[field]))
        raise FinalReportError(f"fact {field} must come from one of: {allowed}")
    if "pointer" in binding:
        pointer = binding["pointer"]
        exact = EXACT_POINTERS.get(field)
        if exact is not None and pointer not in exact:
            raise FinalReportError(f"identity fact {field} must use its exact contract pointer")
        value = _pointer(sources[role], pointer, f"fact {field}")
        rendered_binding: str | dict[str, str] = pointer
    else:
        if field in EXACT_POINTERS:
            raise FinalReportError(f"identity fact {field} cannot use a projected binding")
        selection = binding["select"]
        value = {
            name: _pointer(sources[role], pointer, f"fact {field}.{name}")
            for name, pointer in sorted(selection.items())
        }
        rendered_binding = dict(sorted(selection.items()))
    copied = _json_copy(value)
    _validate_fact_value(field, copied)
    return {
        "state": "evidenced",
        "value": copied,
        "source": role,
        "source_sha256": evidence[role]["sha256"],
        "binding": rendered_binding,
    }


def build_final_report(
    manifest: Mapping[str, Any],
    *,
    source_root: Path,
    contracts_root: Path = CONTRACTS_DIRECTORY,
    input_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Compose a final report from exact source bytes without external actions."""

    materialized_manifest = _json_copy(manifest)
    _validate_schema(
        materialized_manifest,
        contracts_root / "final_report_input_schema.json",
        "final-report input",
    )
    if input_manifest_sha256 is None:
        input_manifest_sha256 = sha256_bytes(canonical_json_bytes(materialized_manifest))

    sources, evidence = _load_sources(
        materialized_manifest,
        source_root=source_root,
        contracts_root=contracts_root,
    )
    if "provenance" not in sources:
        raise FinalReportError(
            "a hash-bound canonically signed provenance source is mandatory"
        )
    source_hashes = {role: item["sha256"] for role, item in evidence.items()}
    limitation_by_field, _ = _validate_provenance(
        sources["provenance"],
        declaration_sha256=source_hashes["provenance"],
        source_hashes=source_hashes,
        contracts_root=contracts_root,
    )
    _validate_source_consistency(sources, source_hashes)

    unknown_fact_bindings = sorted(
        set(materialized_manifest["fact_bindings"]).difference(FACT_SOURCES)
    )
    if unknown_fact_bindings:
        raise FinalReportError(f"unknown final-report fact: {unknown_fact_bindings[0]}")
    forbidden_auto_bindings = sorted(
        set(materialized_manifest["fact_bindings"]).intersection(AUTO_PROVENANCE_FACTS)
    )
    if forbidden_auto_bindings:
        raise FinalReportError(
            f"{forbidden_auto_bindings[0]} must come directly from signed provenance"
        )

    facts: dict[str, dict[str, Any]] = {}
    for field, binding in sorted(materialized_manifest["fact_bindings"].items()):
        if field in limitation_by_field:
            raise FinalReportError(
                f"fact {field} is both evidenced and declared unknown by provenance"
            )
        facts[field] = _extract_bound_fact(
            field,
            binding,
            sources=sources,
            evidence=evidence,
        )

    provenance = sources["provenance"]
    provenance_hash = source_hashes["provenance"]
    for field, limitation_id in sorted(limitation_by_field.items()):
        if field in AUTO_PROVENANCE_FACTS:
            raise FinalReportError(
                f"provenance-owned fact {field} cannot itself be declared unknown"
            )
        facts[field] = {
            "state": "unknown",
            "limitation_id": limitation_id,
            "provenance_sha256": provenance_hash,
        }
    facts["known_limitations"] = {
        "state": "evidenced",
        "value": _json_copy(provenance["known_limitations"]),
        "source": "provenance",
        "source_sha256": provenance_hash,
        "binding": "/known_limitations",
    }
    facts["integration_recommendation"] = {
        "state": "evidenced",
        "value": provenance["integration_recommendation"],
        "source": "provenance",
        "source_sha256": provenance_hash,
        "binding": "/integration_recommendation",
    }

    final_status = facts.get("final_status")
    release = sources.get("release_status")
    if (
        final_status is not None
        and release is not None
        and final_status.get("value") != release.get("status")
    ):
        raise FinalReportError("final_status fact differs from release-status source")

    signed_payload_hash = provenance["signed_payload_sha256"]
    confirmations = {
        assertion: {
            "value": True,
            "source": "provenance",
            "source_sha256": provenance_hash,
            "signed_payload_sha256": signed_payload_hash,
        }
        for assertion in PROVENANCE_ASSERTIONS
    }

    missing_sources = sorted(set(SOURCE_ROLES).difference(sources))
    missing_fields = sorted(set(FACT_SOURCES).difference(facts))
    state = "FINAL" if not missing_sources and not missing_fields else "NOT_FINAL"
    body: dict[str, Any] = {
        "schema_version": 1,
        "report_state": state,
        "source_evidence": dict(sorted(evidence.items())),
        "facts": dict(sorted(facts.items())),
        "confirmations": confirmations,
        "missing_required_sources": missing_sources,
        "missing_required_fields": missing_fields,
    }
    report = {
        **body,
        "integrity": {
            "canonicalization": "scriber-canonical-json-sort-v1",
            "input_manifest_sha256": input_manifest_sha256,
            "report_body_sha256": sha256_bytes(canonical_json_bytes(body)),
        },
    }
    _validate_schema(report, contracts_root / "final_report_schema.json", "final report")
    return report


def verify_final_report(
    report: Mapping[str, Any],
    *,
    contracts_root: Path = CONTRACTS_DIRECTORY,
) -> dict[str, Any]:
    """Verify the report schema and its self-contained body hash."""

    materialized = _json_copy(report)
    _validate_schema(materialized, contracts_root / "final_report_schema.json", "final report")
    unknown_facts = sorted(set(materialized["facts"]).difference(FACT_SOURCES))
    if unknown_facts:
        raise FinalReportError(f"final report contains unknown fact {unknown_facts[0]}")
    expected_missing_fields = sorted(set(FACT_SOURCES).difference(materialized["facts"]))
    if materialized["missing_required_fields"] != expected_missing_fields:
        raise FinalReportError("final report missing_required_fields is not exact")
    expected_missing_sources = sorted(
        set(SOURCE_ROLES).difference(materialized["source_evidence"])
    )
    if materialized["missing_required_sources"] != expected_missing_sources:
        raise FinalReportError("final report missing_required_sources is not exact")
    expected_state = (
        "FINAL"
        if not expected_missing_fields and not expected_missing_sources
        else "NOT_FINAL"
    )
    if materialized["report_state"] != expected_state:
        raise FinalReportError("final report state differs from its evidence completeness")

    provenance_evidence = materialized["source_evidence"].get("provenance")
    if not isinstance(provenance_evidence, Mapping):
        raise FinalReportError("final report has no provenance source evidence")
    limitations_fact = materialized["facts"].get("known_limitations")
    if not isinstance(limitations_fact, Mapping) or limitations_fact.get("state") != "evidenced":
        raise FinalReportError("final report has no evidenced known_limitations fact")
    limitation_fields: dict[str, str] = {}
    for limitation in limitations_fact["value"]:
        if not isinstance(limitation, Mapping):
            raise FinalReportError("final report limitation must be an object")
        limitation_id = limitation.get("id")
        affected_fields = limitation.get("affected_fields")
        if not isinstance(limitation_id, str) or not isinstance(affected_fields, list):
            raise FinalReportError("final report limitation is malformed")
        for field in affected_fields:
            if field in limitation_fields:
                raise FinalReportError(f"multiple final report limitations claim {field}")
            limitation_fields[field] = limitation_id

    for field, fact in materialized["facts"].items():
        if fact["state"] == "evidenced":
            if fact["source"] not in FACT_SOURCES[field]:
                raise FinalReportError(f"final report fact {field} has an invalid source role")
            source_evidence = materialized["source_evidence"].get(fact["source"])
            if not isinstance(source_evidence, Mapping):
                raise FinalReportError(
                    f"final report fact {field} references absent source evidence"
                )
            if fact["source_sha256"] != source_evidence["sha256"]:
                raise FinalReportError(f"final report fact {field} source hash differs")
            _validate_fact_value(field, fact["value"])
        else:
            if limitation_fields.get(field) != fact["limitation_id"]:
                raise FinalReportError(
                    f"unknown final report fact {field} lacks its exact limitation"
                )
            if fact["provenance_sha256"] != provenance_evidence["sha256"]:
                raise FinalReportError(
                    f"unknown final report fact {field} provenance hash differs"
                )
    for assertion, confirmation in materialized["confirmations"].items():
        if assertion not in PROVENANCE_ASSERTIONS:
            raise FinalReportError(f"final report contains unknown confirmation {assertion}")
        if confirmation["source_sha256"] != provenance_evidence["sha256"]:
            raise FinalReportError(
                f"final report confirmation {assertion} provenance hash differs"
            )
    body = {key: value for key, value in materialized.items() if key != "integrity"}
    actual = sha256_bytes(canonical_json_bytes(body))
    if materialized["integrity"]["report_body_sha256"] != actual:
        raise FinalReportError("final report body hash does not verify")
    return materialized


def _markdown_value(value: object) -> str:
    if isinstance(value, str):
        return value.replace("\n", "<br>")
    return (
        "`"
        + json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("`", "\\`")
        .replace("\n", " ")
        + "`"
    )


def render_final_report_markdown(report: Mapping[str, Any], *, json_sha256: str) -> str:
    """Render a human-readable German projection of a verified report."""

    verified = verify_final_report(report)
    lines = [
        "# Scriber Polishing – Abschlussbericht",
        "",
        f"Berichtszustand: **{verified['report_state']}**",
        "",
        "## Ergebnisse",
        "",
    ]
    for field in FACT_SOURCES:
        fact = verified["facts"].get(field)
        if fact is None:
            lines.append(f"- **{_GERMAN_LABELS[field]}:** fehlt")
        elif fact["state"] == "unknown":
            lines.append(
                f"- **{_GERMAN_LABELS[field]}:** unbekannt "
                f"(dokumentierte Einschränkung `{fact['limitation_id']}`)"
            )
        else:
            lines.append(
                f"- **{_GERMAN_LABELS[field]}:** {_markdown_value(fact['value'])} "
                f"_(Quelle `{fact['source']}`, `{fact['source_sha256']}`)_"
            )

    lines.extend(["", "## Explizite Provenienzbestätigungen", ""])
    for assertion in PROVENANCE_ASSERTIONS:
        confirmation = verified["confirmations"][assertion]
        lines.append(
            f"- `{assertion}`: **bestätigt** "
            f"(`{confirmation['signed_payload_sha256']}`)"
        )

    lines.extend(["", "## Quellen", "", "| Rolle | Schema | SHA-256 |", "|---|---:|---|"])
    for role, item in verified["source_evidence"].items():
        lines.append(f"| `{role}` | {item['schema_version']} | `{item['sha256']}` |")

    if verified["missing_required_sources"] or verified["missing_required_fields"]:
        lines.extend(["", "## Noch nicht final", ""])
        if verified["missing_required_sources"]:
            lines.append(
                "- Fehlende Pflichtquellen: "
                + ", ".join(f"`{item}`" for item in verified["missing_required_sources"])
            )
        if verified["missing_required_fields"]:
            lines.append(
                "- Fehlende Pflichtwerte: "
                + ", ".join(f"`{item}`" for item in verified["missing_required_fields"])
            )

    lines.extend(
        [
            "",
            "## Integrität",
            "",
            f"- JSON-Datei: `{json_sha256}`",
            f"- Berichtskörper: `{verified['integrity']['report_body_sha256']}`",
            f"- Eingabemanifest: `{verified['integrity']['input_manifest_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_immutable_final_report(
    report: Mapping[str, Any],
    *,
    json_path: Path,
    markdown_path: Path,
) -> tuple[str, str]:
    """Write JSON and Markdown once; existing destinations are never replaced."""

    verified = verify_final_report(report)
    if json_path.resolve() == markdown_path.resolve():
        raise FinalReportError("JSON and Markdown destinations must differ")
    for path in (json_path, markdown_path):
        if path.exists() or path.is_symlink():
            raise FinalReportError(f"refusing to overwrite final-report output: {path}")

    json_payload = (
        json.dumps(
            verified,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    json_hash = sha256_bytes(json_payload)
    markdown_payload = render_final_report_markdown(
        verified,
        json_sha256=json_hash,
    ).encode("utf-8")
    markdown_hash = sha256_bytes(markdown_payload)

    for path in (json_path, markdown_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_temporary = json_path.with_suffix(json_path.suffix + ".tmp")
    markdown_temporary = markdown_path.with_suffix(markdown_path.suffix + ".tmp")
    if json_temporary.exists() or markdown_temporary.exists():
        raise FinalReportError("temporary final-report destination already exists")
    try:
        json_temporary.write_bytes(json_payload)
        markdown_temporary.write_bytes(markdown_payload)
        json_temporary.replace(json_path)
        markdown_temporary.replace(markdown_path)
    finally:
        for temporary in (json_temporary, markdown_temporary):
            if temporary.exists():
                temporary.unlink()
    return json_hash, markdown_hash
