from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVIDENCE_CONTRACT = "PythonRuntimeABEvidenceV1"
PROFILE_NAME = "python-runtime-ab-v1"
AMD_EVIDENCE_CONTRACT = "PythonRuntimeAMDEvidenceV1"
AMD_PROFILE_NAME = "python-runtime-amd-v1"
O0_SCREENING_REFERENCE_CONTRACT = "PythonRuntimeO0ScreeningReferenceV1"
SCREENING_DECISION_CONTRACT = "PythonRuntimeScreeningDecisionV1"
SUPPORTED_PROFILE_CONTRACTS = {
    PROFILE_NAME: EVIDENCE_CONTRACT,
    AMD_PROFILE_NAME: AMD_EVIDENCE_CONTRACT,
}
RELEASE_CANDIDATES = ("O1", "C0", "C1", "T0", "T1")
REFERENCES = ("A13", "O0")
ZERO_ERROR_FIELDS = ("text", "focus", "clipboard", "overlay", "longTask", "frame", "token")
SCREENING_ERROR_METRICS = (
    "text_errors",
    "focus_errors",
    "clipboard_errors",
    "overlay_errors",
    "ui_long_tasks_gt_200ms",
)
SCREENING_PROVIDERS = ("microsoft_local", "soniox_local", "speechmatics_local")
SCREENING_DURATIONS = (5, 15, 30, 60)
SCREENING_KPIS = (
    "activation_received_to_final_text_observed",
    "stop_requested_to_final_text_observed",
)


class EvidenceError(ValueError):
    """Raised when runtime evidence is incomplete, ambiguous, or inconsistent."""


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    variant_id: str
    passed: bool
    reasons: tuple[str, ...]
    mean_local_wux: float | None


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{path} must be an array")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{path} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, path: str, *, minimum: float = 0.0, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (exclusive and result == minimum):
        relation = "greater than" if exclusive else "at least"
        raise EvidenceError(f"{path} must be {relation} {minimum}")
    return result


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{path} must be boolean")
    return value


def _sha256(value: Any, path: str) -> str:
    result = _text(value, path).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise EvidenceError(f"{path} must be a lowercase SHA-256")
    return result


def _samples(value: Any, path: str) -> tuple[float, ...]:
    result = tuple(
        _finite_number(item, f"{path}[{index}]", exclusive=True) for index, item in enumerate(_list(value, path))
    )
    if len(result) < 2:
        raise EvidenceError(f"{path} must contain at least two samples")
    return result


def _coefficient_of_variation(samples: Sequence[float]) -> float:
    mean = statistics.fmean(samples)
    if mean <= 0:
        raise EvidenceError("local_wux sample mean must be positive")
    if len(samples) < 2:
        raise EvidenceError("at least two samples are required for coefficient of variation")
    return statistics.stdev(samples) / mean


def _bootstrap_gain_interval(
    candidate: Sequence[float],
    reference: Sequence[float],
    *,
    confidence: float,
    replicates: int,
    seed_material: str,
) -> tuple[float, float]:
    if not 0.0 < confidence < 1.0:
        raise EvidenceError("bootstrap confidence must be between zero and one")
    if replicates < 1000:
        raise EvidenceError("bootstrap replicates must be at least 1000")
    seed = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    gains: list[float] = []
    for _ in range(replicates):
        candidate_mean = statistics.fmean(generator.choice(candidate) for _ in candidate)
        reference_mean = statistics.fmean(generator.choice(reference) for _ in reference)
        if reference_mean <= 0:
            raise EvidenceError("bootstrap reference mean must be positive")
        gains.append((reference_mean - candidate_mean) / reference_mean)
    gains.sort()
    tail = (1.0 - confidence) / 2.0
    lower_index = max(0, min(replicates - 1, math.floor(tail * replicates)))
    upper_index = max(0, min(replicates - 1, math.ceil((1.0 - tail) * replicates) - 1))
    return gains[lower_index], gains[upper_index]


def _runtime_identity(
    variant_id: str,
    variant: Mapping[str, Any],
    configured_variant: Mapping[str, Any],
    path: str,
) -> None:
    identity = _object(variant.get("identity"), f"{path}.identity")
    expected_version = "3.13.14" if variant_id == "A13" else "3.14.7"
    expected_tag = "cpython-313" if variant_id == "A13" else "cpython-314"
    if _text(identity.get("pythonVersion"), f"{path}.identity.pythonVersion") != expected_version:
        raise EvidenceError(f"{path}.identity.pythonVersion does not match {variant_id}")
    if _text(identity.get("cacheTag"), f"{path}.identity.cacheTag") != expected_tag:
        raise EvidenceError(f"{path}.identity.cacheTag does not match {variant_id}")
    if _text(identity.get("runtimeFlavor"), f"{path}.identity.runtimeFlavor") != configured_variant["runtimeFlavor"]:
        raise EvidenceError(f"{path}.identity.runtimeFlavor does not match {variant_id}")
    _text(identity.get("compiler"), f"{path}.identity.compiler")
    _sha256(identity.get("pythonDllSha256"), f"{path}.identity.pythonDllSha256")
    tail = _boolean(identity.get("tailCallInterpreter"), f"{path}.identity.tailCallInterpreter")
    if tail is not bool(configured_variant["tailCallInterpreter"]):
        raise EvidenceError(f"{path}.identity.tailCallInterpreter does not match {variant_id}")
    available = _boolean(identity.get("jitAvailable"), f"{path}.identity.jitAvailable")
    expected = _boolean(identity.get("jitExpected"), f"{path}.identity.jitExpected")
    active = _boolean(identity.get("jitActive"), f"{path}.identity.jitActive")
    soak_minutes = _finite_number(identity.get("soakMinutes"), f"{path}.identity.soakMinutes")
    configured_jit = bool(configured_variant["jit"])
    if expected is not configured_jit or active is not expected:
        raise EvidenceError(f"{path}.identity JIT expected/active state does not match {variant_id}")
    if expected and not available:
        raise EvidenceError(f"{path}.identity expected JIT is unavailable")
    if not expected and soak_minutes != 0.0:
        raise EvidenceError(f"{path}.identity non-JIT variant must report zero soak minutes")


def _validate_metric_payload(
    variant_id: str,
    variant: Mapping[str, Any],
    configured_variant: Mapping[str, Any],
    path: str,
) -> None:
    _runtime_identity(variant_id, variant, configured_variant, path)
    _samples(variant.get("localWuxSamples"), f"{path}.localWuxSamples")
    provider_series = _object(variant.get("providerSeries"), f"{path}.providerSeries")
    if not provider_series:
        raise EvidenceError(f"{path}.providerSeries must not be empty")
    for series_name, series_value in provider_series.items():
        series = _object(series_value, f"{path}.providerSeries.{series_name}")
        _finite_number(series.get("p50"), f"{path}.providerSeries.{series_name}.p50", exclusive=True)
        _finite_number(series.get("p95"), f"{path}.providerSeries.{series_name}.p95", exclusive=True)
    protected = _object(variant.get("protectedP95"), f"{path}.protectedP95")
    if not protected:
        raise EvidenceError(f"{path}.protectedP95 must not be empty")
    for metric_name, metric_value in protected.items():
        _finite_number(metric_value, f"{path}.protectedP95.{metric_name}")
    resources = _object(variant.get("resources"), f"{path}.resources")
    for field in ("activeCpu", "rssPrivateBytes", "idleCpuPercentagePoints"):
        _finite_number(resources.get(field), f"{path}.resources.{field}")
    errors = _object(variant.get("errors"), f"{path}.errors")
    for field in ZERO_ERROR_FIELDS:
        value = errors.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceError(f"{path}.errors.{field} must be a non-negative integer")


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"could not load runtime profile config {path}: {exc}") from exc
    config = dict(_object(payload, "config"))
    profile = config.get("profile")
    if config.get("schemaVersion") != 1 or profile not in SUPPORTED_PROFILE_CONTRACTS:
        raise EvidenceError("runtime profile config identity is invalid")
    variants = _object(config.get("variants"), "config.variants")
    expected = {"A13", "O0", "O1", "C0", "C1", "T0", "T1", "K0"}
    if set(variants) != expected:
        raise EvidenceError("runtime profile config variant matrix is incomplete")
    config["_configSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    config["_evaluatorSha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    config["_evidenceContract"] = SUPPORTED_PROFILE_CONTRACTS[str(profile)]
    return config


def validate_evidence(payload: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    profile = _text(config.get("profile"), "config.profile")
    if payload.get("evidenceContract") != config.get("_evidenceContract"):
        raise EvidenceError("evidenceContract is invalid")
    if payload.get("schemaVersion") != 1 or payload.get("profile") != profile:
        raise EvidenceError("runtime evidence profile identity is invalid")
    source_commit = _text(payload.get("sourceCommit"), "sourceCommit").lower()
    if len(source_commit) != 40 or any(character not in "0123456789abcdef" for character in source_commit):
        raise EvidenceError("sourceCommit must be a lowercase 40-character Git SHA")
    bindings = _object(payload.get("bindings"), "bindings")
    for field in (
        "dependencyLockSha256",
        "runtimeLockSha256",
        "profileConfigSha256",
        "evaluatorSha256",
        "frontendSha256",
        "tauriSha256",
        "backendSha256",
        "audioSha256",
    ):
        _sha256(bindings.get(field), f"bindings.{field}")
    if bindings["profileConfigSha256"] != config.get("_configSha256"):
        raise EvidenceError("bindings.profileConfigSha256 does not match the active profile")
    if bindings["evaluatorSha256"] != config.get("_evaluatorSha256"):
        raise EvidenceError("bindings.evaluatorSha256 does not match the active evaluator")
    _text(bindings.get("cacheKey"), "bindings.cacheKey")

    configured_variants = _object(config.get("variants"), "config.variants")
    blocks = tuple(
        _object(block, f"blocks[{index}]") for index, block in enumerate(_list(payload.get("blocks"), "blocks"))
    )
    if not blocks:
        raise EvidenceError("blocks must not be empty")
    screening = _object(config.get("screening"), "config.screening")
    full_local = _object(config.get("fullLocal"), "config.fullLocal")
    screening_hosts = tuple(
        _text(host, "config.screening.hosts") for host in _list(screening.get("hosts"), "config.screening.hosts")
    )
    full_hosts = tuple(
        _text(host, "config.fullLocal.hosts") for host in _list(full_local.get("hosts"), "config.fullLocal.hosts")
    )
    if not screening_hosts or len(set(screening_hosts)) != len(screening_hosts):
        raise EvidenceError("config.screening.hosts must contain unique hosts")
    if screening_hosts != full_hosts:
        raise EvidenceError("screening and FullLocal host scopes must match")
    if any(host not in {"intel", "amd"} for host in full_hosts):
        raise EvidenceError("runtime profile contains an unsupported CPU family")
    full_blocks_per_host = int(full_local.get("cleanInstallRebootBlocksPerHost", 0))
    if full_blocks_per_host < 1:
        raise EvidenceError("config.fullLocal.cleanInstallRebootBlocksPerHost must be positive")
    block_ids: set[str] = set()
    full_counts = dict.fromkeys(full_hosts, 0)
    screening_counts = dict.fromkeys(screening_hosts, 0)
    host_bindings: dict[str, str] = {}
    for index, block in enumerate(blocks):
        path = f"blocks[{index}]"
        block_id = _text(block.get("blockId"), f"{path}.blockId")
        if block_id in block_ids:
            raise EvidenceError(f"duplicate blockId {block_id!r}")
        block_ids.add(block_id)
        phase = _text(block.get("phase"), f"{path}.phase")
        if phase not in {"screening", "full-local"}:
            raise EvidenceError(f"{path}.phase is invalid")
        cpu_family = _text(block.get("cpuFamily"), f"{path}.cpuFamily")
        if cpu_family not in full_counts:
            raise EvidenceError(f"{path}.cpuFamily is invalid")
        host_sha = _sha256(block.get("hostProfileSha256"), f"{path}.hostProfileSha256")
        previous_host_sha = host_bindings.setdefault(cpu_family, host_sha)
        if previous_host_sha != host_sha:
            raise EvidenceError(f"{cpu_family} blocks do not bind one stable host profile")
        clean_install = _boolean(block.get("cleanInstall"), f"{path}.cleanInstall")
        rebooted = _boolean(block.get("rebooted"), f"{path}.rebooted")
        if phase == "full-local":
            if not clean_install or not rebooted:
                raise EvidenceError(f"{path} FullLocal block must be a clean-install/reboot block")
            full_counts[cpu_family] += 1
        else:
            screening_counts[cpu_family] += 1
        measurement_counts = _object(block.get("measurementCounts"), f"{path}.measurementCounts")
        if phase == "screening":
            expected_counts = {
                **_object(
                    _object(config["screening"], "config.screening")["fastLocalSamples"],
                    "config.screening.fastLocalSamples",
                ),
                "appUxScenarioCount": int(_object(config["screening"], "config.screening")["appUxScenarioCount"]),
                "frozenImportWarmups": int(
                    _object(
                        _object(config["screening"], "config.screening")["frozenImportStartup"],
                        "config.screening.frozenImportStartup",
                    )["warmups"]
                ),
                "frozenImportSamples": int(
                    _object(
                        _object(config["screening"], "config.screening")["frozenImportStartup"],
                        "config.screening.frozenImportStartup",
                    )["samples"]
                ),
            }
        else:
            expected_counts = {
                **_object(
                    _object(config["fullLocal"], "config.fullLocal")["samples"],
                    "config.fullLocal.samples",
                ),
                "appUxScenarioCount": int(_object(config["fullLocal"], "config.fullLocal")["appUxScenarioCount"]),
            }
        if dict(measurement_counts) != expected_counts:
            raise EvidenceError(f"{path}.measurementCounts does not match the frozen {phase} profile")
        environment = _object(block.get("environment"), f"{path}.environment")
        defender_enabled = _boolean(
            environment.get("defenderEnabled"),
            f"{path}.environment.defenderEnabled",
        )
        standard_user = _boolean(environment.get("standardUser"), f"{path}.environment.standardUser")
        if phase == "full-local" and (not defender_enabled or not standard_user):
            raise EvidenceError(f"{path} FullLocal block requires Defender and a standard user")
        order = tuple(_text(item, f"{path}.order") for item in _list(block.get("order"), f"{path}.order"))
        if len(order) != len(set(order)):
            raise EvidenceError(f"{path}.order contains duplicate variants")
        variants = _object(block.get("variants"), f"{path}.variants")
        if not {"A13", "O0"}.issubset(variants):
            raise EvidenceError(f"{path}.variants is missing A13 or O0")
        if set(order) != set(variants):
            raise EvidenceError(f"{path}.order must contain every measured variant exactly once")
        for variant_id, variant_value in variants.items():
            if variant_id not in configured_variants:
                raise EvidenceError(f"{path}.variants contains unknown variant {variant_id!r}")
            _validate_metric_payload(
                variant_id,
                _object(variant_value, f"{path}.variants.{variant_id}"),
                _object(configured_variants[variant_id], f"config.variants.{variant_id}"),
                f"{path}.variants.{variant_id}",
            )
            if (
                phase == "full-local"
                and bool(_object(configured_variants[variant_id], f"config.variants.{variant_id}")["jit"])
                and float(
                    _object(
                        _object(variant_value, f"{path}.variants.{variant_id}")["identity"],
                        f"{path}.variants.{variant_id}.identity",
                    )["soakMinutes"]
                )
                < float(_object(config["fullLocal"], "config.fullLocal")["jitSoakMinutes"])
            ):
                raise EvidenceError(f"{path}.variants.{variant_id} is missing the required JIT soak")
    expected_full_counts = dict.fromkeys(full_hosts, full_blocks_per_host)
    expected_screening_counts = dict.fromkeys(screening_hosts, 1)
    if full_counts != expected_full_counts:
        raise EvidenceError(
            f"evidence must contain exactly {full_blocks_per_host} FullLocal blocks per configured host"
        )
    if screening_counts != expected_screening_counts:
        if profile == PROFILE_NAME:
            raise EvidenceError("evidence must contain one screening block for Intel and AMD")
        raise EvidenceError("evidence must contain one screening block per configured host")
    full_blocks = tuple(block for block in blocks if block["phase"] == "full-local")
    orders = [tuple(block["order"]) for block in full_blocks]
    if len(set(orders)) < 2:
        raise EvidenceError("FullLocal execution order is not counterbalanced")
    screening_orders = {tuple(block["order"]) for block in blocks if block["phase"] == "screening"}
    if bool(screening.get("rotatedOrderRequired")) and len(screening_orders) < 2:
        raise EvidenceError("screening execution order is not rotated across hosts")
    return blocks


def _compare_guardrails(
    candidate_id: str,
    candidate: Mapping[str, Any],
    reference_id: str,
    reference: Mapping[str, Any],
    *,
    protected_p95_limit: float,
    active_cpu_limit: float,
    rss_limit: float,
    idle_cpu_points: float,
    path: str,
) -> list[str]:
    reasons: list[str] = []
    candidate_provider = _object(candidate["providerSeries"], f"{path}.{candidate_id}.providerSeries")
    reference_provider = _object(reference["providerSeries"], f"{path}.{reference_id}.providerSeries")
    if set(candidate_provider) != set(reference_provider):
        return [f"{path}:provider-series-set-mismatch:{reference_id}"]
    for name in sorted(reference_provider):
        candidate_series = _object(candidate_provider[name], f"{path}.{candidate_id}.providerSeries.{name}")
        reference_series = _object(reference_provider[name], f"{path}.{reference_id}.providerSeries.{name}")
        for percentile in ("p50", "p95"):
            if float(candidate_series[percentile]) > float(reference_series[percentile]):
                reasons.append(f"{path}:provider-{name}-{percentile}-regression:{reference_id}")

    candidate_protected = _object(candidate["protectedP95"], f"{path}.{candidate_id}.protectedP95")
    reference_protected = _object(reference["protectedP95"], f"{path}.{reference_id}.protectedP95")
    if set(candidate_protected) != set(reference_protected):
        reasons.append(f"{path}:protected-p95-set-mismatch:{reference_id}")
    else:
        for name in sorted(reference_protected):
            reference_value = float(reference_protected[name])
            candidate_value = float(candidate_protected[name])
            if candidate_value > reference_value * (1.0 + protected_p95_limit):
                reasons.append(f"{path}:protected-{name}-p95-regression:{reference_id}")

    candidate_resources = _object(candidate["resources"], f"{path}.{candidate_id}.resources")
    reference_resources = _object(reference["resources"], f"{path}.{reference_id}.resources")
    if float(candidate_resources["activeCpu"]) > float(reference_resources["activeCpu"]) * (1.0 + active_cpu_limit):
        reasons.append(f"{path}:active-cpu-regression:{reference_id}")
    if float(candidate_resources["rssPrivateBytes"]) > float(reference_resources["rssPrivateBytes"]) * (
        1.0 + rss_limit
    ):
        reasons.append(f"{path}:rss-private-bytes-regression:{reference_id}")
    if float(candidate_resources["idleCpuPercentagePoints"]) > (
        float(reference_resources["idleCpuPercentagePoints"]) + idle_cpu_points
    ):
        reasons.append(f"{path}:idle-cpu-regression:{reference_id}")
    return reasons


def evaluate_candidate(
    candidate_id: str,
    blocks: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> CandidateDecision:
    if candidate_id not in RELEASE_CANDIDATES:
        raise EvidenceError(f"{candidate_id} is not an optimized release candidate")
    promotion = _object(config.get("promotion"), "config.promotion")
    minimum_gain = float(promotion["minimumGainFraction"])
    cv_multiplier = float(promotion["baselineCvMultiplier"])
    confidence = float(promotion["bootstrapConfidence"])
    replicates = int(promotion["bootstrapReplicates"])
    reasons: list[str] = []
    all_samples: list[float] = []
    measured_blocks = 0
    for block in blocks:
        if block["phase"] != "full-local":
            continue
        variants = _object(block["variants"], f"{block['blockId']}.variants")
        if candidate_id not in variants:
            reasons.append(f"{block['blockId']}:candidate-not-measured")
            continue
        measured_blocks += 1
        candidate = _object(variants[candidate_id], f"{block['blockId']}.{candidate_id}")
        candidate_samples = _samples(candidate["localWuxSamples"], f"{block['blockId']}.{candidate_id}.localWuxSamples")
        all_samples.extend(candidate_samples)
        errors = _object(candidate["errors"], f"{block['blockId']}.{candidate_id}.errors")
        for field in ZERO_ERROR_FIELDS:
            if int(errors[field]) != 0:
                reasons.append(f"{block['blockId']}:{field}-errors")
        for reference_id in REFERENCES:
            reference = _object(variants[reference_id], f"{block['blockId']}.{reference_id}")
            reference_samples = _samples(
                reference["localWuxSamples"],
                f"{block['blockId']}.{reference_id}.localWuxSamples",
            )
            candidate_mean = statistics.fmean(candidate_samples)
            reference_mean = statistics.fmean(reference_samples)
            observed_gain = (reference_mean - candidate_mean) / reference_mean
            required_gain = max(minimum_gain, cv_multiplier * _coefficient_of_variation(reference_samples))
            if observed_gain < required_gain:
                reasons.append(f"{block['blockId']}:gain-below-threshold:{reference_id}")
            lower, _upper = _bootstrap_gain_interval(
                candidate_samples,
                reference_samples,
                confidence=confidence,
                replicates=replicates,
                seed_material=f"{block['blockId']}:{candidate_id}:{reference_id}",
            )
            if lower <= 0.0:
                reasons.append(f"{block['blockId']}:bootstrap-ci-crosses-zero:{reference_id}")
            reasons.extend(
                _compare_guardrails(
                    candidate_id,
                    candidate,
                    reference_id,
                    reference,
                    protected_p95_limit=float(promotion["maximumProtectedP95RegressionFraction"]),
                    active_cpu_limit=float(promotion["maximumActiveCpuRegressionFraction"]),
                    rss_limit=float(promotion["maximumRssPrivateBytesRegressionFraction"]),
                    idle_cpu_points=float(promotion["maximumIdleCpuRegressionPercentagePoints"]),
                    path=str(block["blockId"]),
                )
            )
    full_local = _object(config.get("fullLocal"), "config.fullLocal")
    expected_measured_blocks = len(_list(full_local.get("hosts"), "config.fullLocal.hosts")) * int(
        full_local.get("cleanInstallRebootBlocksPerHost", 0)
    )
    if measured_blocks != expected_measured_blocks:
        reasons.append(f"candidate-must-be-measured-in-all-{expected_measured_blocks}-full-local-blocks")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return CandidateDecision(
        variant_id=candidate_id,
        passed=not unique_reasons,
        reasons=unique_reasons,
        mean_local_wux=statistics.fmean(all_samples) if all_samples else None,
    )


def _preference_key(variant_id: str, config: Mapping[str, Any]) -> tuple[int, int, int, str]:
    variant = _object(_object(config["variants"], "config.variants")[variant_id], f"config.variants.{variant_id}")
    official_rank = 0 if variant["runtimeFlavor"] == "Official" else 1
    jit_rank = 1 if variant["jit"] else 0
    tail_rank = 1 if variant["tailCallInterpreter"] else 0
    return official_rank, jit_rank, tail_rank, variant_id


def _statistically_tied(
    left: str,
    right: str,
    blocks: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> bool:
    left_samples: list[float] = []
    right_samples: list[float] = []
    for block in blocks:
        if block["phase"] != "full-local":
            continue
        variants = _object(block["variants"], f"{block['blockId']}.variants")
        left_samples.extend(_samples(_object(variants[left], left)["localWuxSamples"], left))
        right_samples.extend(_samples(_object(variants[right], right)["localWuxSamples"], right))
    promotion = _object(config["promotion"], "config.promotion")
    lower, upper = _bootstrap_gain_interval(
        left_samples,
        right_samples,
        confidence=float(promotion["bootstrapConfidence"]),
        replicates=int(promotion["bootstrapReplicates"]),
        seed_material=f"tie:{left}:{right}",
    )
    return lower <= 0.0 <= upper


def select_runtime(payload: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    blocks = validate_evidence(payload, config)
    # O0 is the non-negotiable correctness fallback, not an unconditional
    # escape hatch. It still must have zero protected error outcomes.
    for block in blocks:
        if block["phase"] != "full-local":
            continue
        errors = _object(_object(block["variants"], "variants")["O0"]["errors"], "O0.errors")
        nonzero = [field for field in ZERO_ERROR_FIELDS if int(errors[field]) != 0]
        if nonzero:
            raise EvidenceError(f"O0 has protected errors in {block['blockId']}: {', '.join(nonzero)}")

    decisions = [evaluate_candidate(candidate, blocks, config) for candidate in RELEASE_CANDIDATES]
    passing = [decision for decision in decisions if decision.passed]
    selected = "O0"
    selection_reason = "fallback-official-3.14.7-jit-disabled"
    if passing:
        passing.sort(
            key=lambda item: (float(item.mean_local_wux or math.inf), _preference_key(item.variant_id, config))
        )
        best = passing[0]
        tied = [best]
        for candidate in passing[1:]:
            if _statistically_tied(best.variant_id, candidate.variant_id, blocks, config):
                tied.append(candidate)
        selected_decision = min(tied, key=lambda item: _preference_key(item.variant_id, config))
        selected = selected_decision.variant_id
        selection_reason = (
            "optimized-candidate-promoted" if len(tied) == 1 else "statistical-tie-resolved-by-official-jit-tail-order"
        )
    amd_only = config.get("profile") == AMD_PROFILE_NAME
    return {
        "decisionContract": "PythonRuntimeAMDDecisionV1" if amd_only else "PythonRuntimeABDecisionV1",
        "schemaVersion": 1,
        "profile": config["profile"],
        "scope": "amd-only" if amd_only else "intel-and-amd",
        "productionPromotionAuthorized": not amd_only,
        "sourceCommit": payload["sourceCommit"],
        "selectedVariant": selected,
        "reason": selection_reason,
        "candidates": [
            {
                "variantId": decision.variant_id,
                "passed": decision.passed,
                "meanLocalWux": decision.mean_local_wux,
                "reasons": list(decision.reasons),
            }
            for decision in decisions
        ],
    }


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"could not hash evidence file {path}: {exc}") from exc


def _screening_provider_series_names() -> tuple[str, ...]:
    return tuple(
        f"{provider}_{duration}s_{kpi}"
        for provider in SCREENING_PROVIDERS
        for duration in SCREENING_DURATIONS
        for kpi in SCREENING_KPIS
    )


def _screening_fast_local(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    variant_id: str,
) -> dict[str, Any]:
    if payload.get("suite") != "FastLocal":
        raise EvidenceError(f"{variant_id} evidence must be a FastLocal result")
    configured_variants = _object(config.get("variants"), "config.variants")
    if variant_id not in configured_variants:
        raise EvidenceError(f"{variant_id} is not present in the active runtime profile")
    source_commit = _text(payload.get("sourceCommit"), f"{variant_id}.sourceCommit").lower()
    if len(source_commit) != 40 or any(character not in "0123456789abcdef" for character in source_commit):
        raise EvidenceError(f"{variant_id}.sourceCommit must be a lowercase 40-character Git SHA")
    source_content_sha256 = _sha256(
        payload.get("runtimeAttestationSourceContentSha256"),
        f"{variant_id}.runtimeAttestationSourceContentSha256",
    )
    attestation_id = _sha256(payload.get("runtimeAttestationId"), f"{variant_id}.runtimeAttestationId")
    for phase in ("Pre", "Post"):
        if not _boolean(
            payload.get(f"runtimeAttestation{phase}Valid"),
            f"{variant_id}.runtimeAttestation{phase}Valid",
        ):
            raise EvidenceError(f"{variant_id} runtime attestation {phase.lower()} gate did not pass")
        if (
            _sha256(
                payload.get(f"runtimeAttestation{phase}Id"),
                f"{variant_id}.runtimeAttestation{phase}Id",
            )
            != attestation_id
        ):
            raise EvidenceError(f"{variant_id} runtime attestation identity drifted")
    if _boolean(
        payload.get("runtimeAttestationDriftDetected"),
        f"{variant_id}.runtimeAttestationDriftDetected",
    ):
        raise EvidenceError(f"{variant_id} runtime attestation drifted during FastLocal")
    if not _boolean(payload.get("appUxEvidenceProvided"), f"{variant_id}.appUxEvidenceProvided"):
        raise EvidenceError(f"{variant_id} must bind explicit App UX evidence")
    app_ux_sha256 = _sha256(payload.get("appUxEvidenceSha256"), f"{variant_id}.appUxEvidenceSha256")
    if _sha256(payload.get("appUxEvidencePostSha256"), f"{variant_id}.appUxEvidencePostSha256") != app_ux_sha256:
        raise EvidenceError(f"{variant_id} App UX evidence drifted during FastLocal")
    if _boolean(payload.get("appUxEvidenceDriftDetected"), f"{variant_id}.appUxEvidenceDriftDetected"):
        raise EvidenceError(f"{variant_id} App UX evidence drifted during FastLocal")

    endpoint = _object(payload.get("endpointProbe"), f"{variant_id}.endpointProbe")
    metrics = _object(endpoint.get("metrics"), f"{variant_id}.endpointProbe.metrics")
    sample_plan = _object(endpoint.get("samplePlan"), f"{variant_id}.endpointProbe.samplePlan")
    screening = _object(config.get("screening"), "config.screening")
    configured_counts = _object(screening.get("fastLocalSamples"), "config.screening.fastLocalSamples")
    expected_sample_plan = {
        "overlayCold": int(configured_counts["cold"]),
        "overlayWarm": int(configured_counts["warm"]),
        "providerReplay": int(configured_counts["providerReplayPerProviderAndDuration"]),
        "providerReplayDurationsSeconds": list(
            _list(
                screening.get("providerReplayDurationsSeconds"),
                "config.screening.providerReplayDurationsSeconds",
            )
        ),
        "appUxPerScenario": int(configured_counts["appUxPerScenario"]),
    }
    if dict(sample_plan) != expected_sample_plan:
        raise EvidenceError(f"{variant_id} FastLocal sample plan does not match the active runtime profile")

    provider_series: dict[str, dict[str, float]] = {}
    expected_provider_samples = int(configured_counts["providerReplayPerProviderAndDuration"])
    for series_name in _screening_provider_series_names():
        p50 = _finite_number(
            metrics.get(f"{series_name}_p50_ms"),
            f"{variant_id}.metrics.{series_name}_p50_ms",
            exclusive=True,
        )
        p95 = _finite_number(
            metrics.get(f"{series_name}_p95_ms"),
            f"{variant_id}.metrics.{series_name}_p95_ms",
            exclusive=True,
        )
        failure_rate = _finite_number(
            metrics.get(f"{series_name}_failure_rate"),
            f"{variant_id}.metrics.{series_name}_failure_rate",
        )
        sample_count = metrics.get(f"{series_name}_sample_count")
        capture_attested = metrics.get(f"{series_name}_capture_attested")
        if failure_rate != 0.0:
            raise EvidenceError(f"{variant_id}.{series_name} has provider replay failures")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count != expected_provider_samples
        ):
            raise EvidenceError(f"{variant_id}.{series_name} must contain exactly {expected_provider_samples} samples")
        if isinstance(capture_attested, bool) or capture_attested != 1:
            raise EvidenceError(f"{variant_id}.{series_name} is not capture-attested")
        provider_series[series_name] = {"p50": p50, "p95": p95}

    evidence = _object(endpoint.get("evidence"), f"{variant_id}.endpointProbe.evidence")
    app_frame = _object(evidence.get("appFrame"), f"{variant_id}.endpointProbe.evidence.appFrame")
    if not _boolean(app_frame.get("metricEligible"), f"{variant_id}.appFrame.metricEligible"):
        raise EvidenceError(f"{variant_id} App UX evidence is not metric-eligible")
    scenario_results = _object(app_frame.get("scenarioResults"), f"{variant_id}.appFrame.scenarioResults")
    expected_scenarios = int(screening["appUxScenarioCount"])
    if len(scenario_results) != expected_scenarios:
        raise EvidenceError(f"{variant_id} App UX evidence must contain exactly {expected_scenarios} scenarios")
    app_ux_scenarios: dict[str, dict[str, float]] = {}
    for scenario_name in sorted(scenario_results):
        scenario = _object(scenario_results[scenario_name], f"{variant_id}.appFrame.{scenario_name}")
        if not _boolean(
            scenario.get("metricEligible"),
            f"{variant_id}.appFrame.{scenario_name}.metricEligible",
        ):
            raise EvidenceError(f"{variant_id} App UX scenario {scenario_name!r} is not metric-eligible")
        sample_count = scenario.get("sampleCount")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count != int(configured_counts["appUxPerScenario"])
        ):
            raise EvidenceError(f"{variant_id} App UX scenario {scenario_name!r} has the wrong sample count")
        app_ux_scenarios[scenario_name] = {
            "p50": _finite_number(
                scenario.get("p50Ms"),
                f"{variant_id}.appFrame.{scenario_name}.p50Ms",
                exclusive=True,
            ),
            "p95": _finite_number(
                scenario.get("p95Ms"),
                f"{variant_id}.appFrame.{scenario_name}.p95Ms",
                exclusive=True,
            ),
        }
    app_ux = {
        "p50": _finite_number(
            metrics.get("app_ux_p50_ms"),
            f"{variant_id}.metrics.app_ux_p50_ms",
            exclusive=True,
        ),
        "p95": _finite_number(
            metrics.get("app_ux_p95_ms"),
            f"{variant_id}.metrics.app_ux_p95_ms",
            exclusive=True,
        ),
        "scenarios": app_ux_scenarios,
    }
    errors: dict[str, int] = {}
    for field in SCREENING_ERROR_METRICS:
        value = metrics.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceError(f"{variant_id}.metrics.{field} must be a non-negative integer")
        errors[field] = value
    resources = {
        "idleCpuPercentagePoints": _finite_number(
            metrics.get("idle_cpu_pct"),
            f"{variant_id}.metrics.idle_cpu_pct",
        ),
        "workingSetMb": _finite_number(
            metrics.get("working_set_mb"),
            f"{variant_id}.metrics.working_set_mb",
            exclusive=True,
        ),
    }
    return {
        "sourceCommit": source_commit,
        "sourceContentSha256": source_content_sha256,
        "runtimeAttestationId": attestation_id,
        "runtimeAttestationManifestSha256": _sha256(
            payload.get("runtimeAttestationManifestSha256"),
            f"{variant_id}.runtimeAttestationManifestSha256",
        ),
        "desktopSha256": _sha256(payload.get("desktopSha256"), f"{variant_id}.desktopSha256"),
        "backendSha256": _sha256(payload.get("backendSha256"), f"{variant_id}.backendSha256"),
        "audioSha256": _sha256(payload.get("audioSidecarSha256"), f"{variant_id}.audioSidecarSha256"),
        "appUxEvidenceSha256": app_ux_sha256,
        "providerSeries": provider_series,
        "appUx": app_ux,
        "resources": resources,
        "errors": errors,
    }


def _screening_startup(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    variants: Sequence[str],
) -> dict[str, Any]:
    if payload.get("evidenceContract") != "ScriberFrozenBackendImportStartupAMDV2":
        raise EvidenceError("startup evidence contract is invalid")
    if payload.get("hostScope") != "amd-only":
        raise EvidenceError("startup evidence must be bound to the AMD host")
    if payload.get("productionPromotionAuthorized") is not False:
        raise EvidenceError("startup evidence must remain diagnostic-only")
    measurement = _object(payload.get("measurement"), "startup.measurement")
    configured = _object(
        _object(config.get("screening"), "config.screening").get("frozenImportStartup"),
        "config.screening.frozenImportStartup",
    )
    if int(measurement.get("warmupsPerVariant", -1)) != int(configured["warmups"]):
        raise EvidenceError("startup warmup count does not match the active runtime profile")
    if int(measurement.get("samplesPerVariant", -1)) != int(configured["samples"]):
        raise EvidenceError("startup sample count does not match the active runtime profile")
    host = _object(payload.get("host"), "startup.host")
    host_sha256 = _sha256(host.get("sha256"), "startup.host.sha256")
    configured_variants = _object(config.get("variants"), "config.variants")
    measured_variants = _object(payload.get("variants"), "startup.variants")
    result: dict[str, Any] = {}
    for variant_id in variants:
        if variant_id not in measured_variants:
            raise EvidenceError(f"startup evidence does not contain {variant_id}")
        configured_variant = _object(configured_variants.get(variant_id), f"config.variants.{variant_id}")
        measured = _object(measured_variants[variant_id], f"startup.variants.{variant_id}")
        identity = _object(measured.get("identity"), f"startup.variants.{variant_id}.identity")
        if identity.get("variantId") != variant_id:
            raise EvidenceError(f"startup identity does not match {variant_id}")
        if identity.get("pythonVersion") != "3.14.7" or identity.get("cacheTag") != "cpython-314":
            raise EvidenceError(f"startup {variant_id} is not CPython 3.14.7")
        if identity.get("runtimeFlavor") != configured_variant["runtimeFlavor"]:
            raise EvidenceError(f"startup runtime flavour does not match {variant_id}")
        if identity.get("jitExpected") is not bool(configured_variant["jit"]):
            raise EvidenceError(f"startup JIT expectation does not match {variant_id}")
        if identity.get("jitActive") is not bool(configured_variant["jit"]):
            raise EvidenceError(f"startup JIT state does not match {variant_id}")
        if identity.get("tailCallInterpreter") is not bool(configured_variant["tailCallInterpreter"]):
            raise EvidenceError(f"startup tail-call state does not match {variant_id}")
        summary = _object(measured.get("summary"), f"startup.variants.{variant_id}.summary")
        count = summary.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != int(configured["samples"]):
            raise EvidenceError(f"startup {variant_id} summary has the wrong sample count")
        result[variant_id] = {
            "identity": {
                "runtimeFlavor": identity["runtimeFlavor"],
                "compiler": _text(identity.get("compiler"), f"startup.{variant_id}.compiler"),
                "pythonDllSha256": _sha256(
                    _object(identity.get("pythonDll"), f"startup.{variant_id}.pythonDll").get("sha256"),
                    f"startup.{variant_id}.pythonDll.sha256",
                ),
                "runtimeLockSha256": _sha256(
                    identity.get("runtimeLockSha256"),
                    f"startup.{variant_id}.runtimeLockSha256",
                ),
                "backendSha256": _sha256(
                    _object(identity.get("executable"), f"startup.{variant_id}.executable").get("sha256"),
                    f"startup.{variant_id}.executable.sha256",
                ),
                "jitExpected": bool(identity["jitExpected"]),
                "jitActive": bool(identity["jitActive"]),
                "tailCallInterpreter": bool(identity["tailCallInterpreter"]),
            },
            "summary": {
                "count": count,
                "cv": _finite_number(summary.get("cv"), f"startup.{variant_id}.cv"),
                "meanMs": _finite_number(
                    summary.get("meanMs"),
                    f"startup.{variant_id}.meanMs",
                    exclusive=True,
                ),
                "p50Ms": _finite_number(
                    summary.get("p50Ms"),
                    f"startup.{variant_id}.p50Ms",
                    exclusive=True,
                ),
                "p95Ms": _finite_number(
                    summary.get("p95Ms"),
                    f"startup.{variant_id}.p95Ms",
                    exclusive=True,
                ),
            },
        }
    return {"hostProfileSha256": host_sha256, "variants": result}


def create_o0_screening_reference(
    fast_local_payload: Mapping[str, Any],
    startup_payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    fast_local_sha256: str,
    startup_sha256: str,
) -> dict[str, Any]:
    o0 = _screening_fast_local(fast_local_payload, config, variant_id="O0")
    startup = _screening_startup(startup_payload, config, variants=("O0",))
    startup_o0 = _object(_object(startup["variants"], "startup.variants")["O0"], "startup.O0")
    startup_identity = _object(startup_o0["identity"], "startup.O0.identity")
    if startup_identity["backendSha256"] != o0["backendSha256"]:
        raise EvidenceError("O0 FastLocal and startup evidence do not bind the same backend")
    if any(int(value) != 0 for value in _object(o0["errors"], "O0.errors").values()):
        raise EvidenceError("O0 has protected FastLocal errors and cannot become the fallback reference")
    return {
        "referenceContract": O0_SCREENING_REFERENCE_CONTRACT,
        "schemaVersion": 1,
        "profile": config["profile"],
        "scope": "amd-only" if config["profile"] == AMD_PROFILE_NAME else "intel-and-amd",
        "referenceVariant": "O0",
        "fallbackVariant": _object(config["promotion"], "config.promotion")["fallbackVariant"],
        "profileConfigSha256": config["_configSha256"],
        "evaluatorSha256": config["_evaluatorSha256"],
        "sourceCommit": o0["sourceCommit"],
        "sourceContentSha256": o0["sourceContentSha256"],
        "hostProfileSha256": startup["hostProfileSha256"],
        "fastLocalEvidenceSha256": _sha256(fast_local_sha256, "fastLocalEvidenceSha256"),
        "startupEvidenceSha256": _sha256(startup_sha256, "startupEvidenceSha256"),
        "runtime": startup_identity,
        "fastLocal": o0,
        "startup": startup_o0["summary"],
    }


def _validate_o0_screening_reference(
    reference: Mapping[str, Any],
    config: Mapping[str, Any],
) -> None:
    if reference.get("referenceContract") != O0_SCREENING_REFERENCE_CONTRACT:
        raise EvidenceError("O0 screening reference contract is invalid")
    if reference.get("schemaVersion") != 1 or reference.get("profile") != config.get("profile"):
        raise EvidenceError("O0 screening reference profile identity is invalid")
    if reference.get("referenceVariant") != "O0" or reference.get("fallbackVariant") != "O0":
        raise EvidenceError("O0 screening reference must bind O0 as the fallback")
    if _sha256(reference.get("profileConfigSha256"), "reference.profileConfigSha256") != config.get("_configSha256"):
        raise EvidenceError("O0 screening reference does not bind the active profile config")
    if _sha256(reference.get("evaluatorSha256"), "reference.evaluatorSha256") != config.get("_evaluatorSha256"):
        raise EvidenceError("O0 screening reference does not bind the active evaluator")
    for field in (
        "sourceContentSha256",
        "hostProfileSha256",
        "fastLocalEvidenceSha256",
        "startupEvidenceSha256",
    ):
        _sha256(reference.get(field), f"reference.{field}")


def select_screening_runtime(
    reference: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]],
    startup_payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    candidate_sha256: Mapping[str, str],
    startup_sha256: str,
) -> dict[str, Any]:
    _validate_o0_screening_reference(reference, config)
    if not candidates:
        raise EvidenceError("at least one screening candidate is required")
    if _sha256(startup_sha256, "startupEvidenceSha256") != reference["startupEvidenceSha256"]:
        raise EvidenceError("candidate comparison does not use the O0 reference startup evidence")
    candidate_ids = tuple(sorted(candidates))
    for candidate_id in candidate_ids:
        if candidate_id not in RELEASE_CANDIDATES:
            raise EvidenceError(f"{candidate_id} is not an optimized release candidate")
    startup = _screening_startup(startup_payload, config, variants=("O0", *candidate_ids))
    if startup["hostProfileSha256"] != reference["hostProfileSha256"]:
        raise EvidenceError("candidate comparison does not use the O0 reference host")
    startup_o0 = _object(_object(startup["variants"], "startup.variants")["O0"], "startup.O0")
    reference_runtime = _object(reference.get("runtime"), "reference.runtime")
    if startup_o0["identity"] != reference_runtime:
        raise EvidenceError("startup O0 identity does not match the explicit reference")

    promotion = _object(config.get("promotion"), "config.promotion")
    minimum_gain = float(promotion["minimumGainFraction"])
    maximum_protected_p95 = float(promotion["maximumProtectedP95RegressionFraction"])
    maximum_rss = float(promotion["maximumRssPrivateBytesRegressionFraction"])
    maximum_idle_points = float(promotion["maximumIdleCpuRegressionPercentagePoints"])
    o0 = _object(reference.get("fastLocal"), "reference.fastLocal")
    o0_provider = _object(o0.get("providerSeries"), "reference.fastLocal.providerSeries")
    o0_app_ux = _object(o0.get("appUx"), "reference.fastLocal.appUx")
    o0_resources = _object(o0.get("resources"), "reference.fastLocal.resources")
    o0_startup = _object(reference.get("startup"), "reference.startup")
    decisions: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        candidate = _screening_fast_local(candidates[candidate_id], config, variant_id=candidate_id)
        candidate_startup = _object(
            _object(startup["variants"], "startup.variants")[candidate_id],
            f"startup.{candidate_id}",
        )
        if candidate["sourceCommit"] != reference["sourceCommit"]:
            raise EvidenceError(f"{candidate_id} source commit differs from the O0 reference")
        if candidate["sourceContentSha256"] != reference["sourceContentSha256"]:
            raise EvidenceError(f"{candidate_id} source content differs from the O0 reference")
        if candidate_startup["identity"]["backendSha256"] != candidate["backendSha256"]:
            raise EvidenceError(f"{candidate_id} FastLocal and startup evidence bind different backends")

        reasons: list[str] = []
        for field, value in _object(candidate["errors"], f"{candidate_id}.errors").items():
            if int(value) != 0:
                reasons.append(f"{field}-nonzero")
        candidate_provider = _object(candidate["providerSeries"], f"{candidate_id}.providerSeries")
        if set(candidate_provider) != set(o0_provider):
            reasons.append("provider-series-set-mismatch")
        else:
            for series_name in sorted(o0_provider):
                for percentile in ("p50", "p95"):
                    if float(candidate_provider[series_name][percentile]) > float(o0_provider[series_name][percentile]):
                        reasons.append(f"provider-{series_name}-{percentile}-regression")

        candidate_app_ux = _object(candidate["appUx"], f"{candidate_id}.appUx")
        app_ux_ratio = 0.40 * float(candidate_app_ux["p50"]) / float(o0_app_ux["p50"]) + 0.60 * float(
            candidate_app_ux["p95"]
        ) / float(o0_app_ux["p95"])
        app_ux_gain = 1.0 - app_ux_ratio
        if app_ux_gain < minimum_gain:
            reasons.append("app-ux-gain-below-threshold")
        candidate_scenarios = _object(candidate_app_ux.get("scenarios"), f"{candidate_id}.appUx.scenarios")
        reference_scenarios = _object(o0_app_ux.get("scenarios"), "reference.appUx.scenarios")
        if set(candidate_scenarios) != set(reference_scenarios):
            reasons.append("app-ux-scenario-set-mismatch")
        else:
            for scenario_name in sorted(reference_scenarios):
                if float(candidate_scenarios[scenario_name]["p95"]) > float(
                    reference_scenarios[scenario_name]["p95"]
                ) * (1.0 + maximum_protected_p95):
                    reasons.append(f"app-ux-{scenario_name}-p95-regression")

        startup_summary = _object(candidate_startup["summary"], f"startup.{candidate_id}.summary")
        startup_gain = {
            field: (float(o0_startup[field]) - float(startup_summary[field])) / float(o0_startup[field])
            for field in ("meanMs", "p50Ms", "p95Ms")
        }
        if float(startup_summary["p95Ms"]) > float(o0_startup["p95Ms"]) * (1.0 + maximum_protected_p95):
            reasons.append("startup-p95-regression")

        candidate_resources = _object(candidate["resources"], f"{candidate_id}.resources")
        if float(candidate_resources["workingSetMb"]) > float(o0_resources["workingSetMb"]) * (1.0 + maximum_rss):
            reasons.append("working-set-regression")
        if (
            float(candidate_resources["idleCpuPercentagePoints"])
            > float(o0_resources["idleCpuPercentagePoints"]) + maximum_idle_points
        ):
            reasons.append("idle-cpu-regression")

        unique_reasons = list(dict.fromkeys(reasons))
        decisions.append(
            {
                "variantId": candidate_id,
                "passedScreening": not unique_reasons,
                "fastLocalEvidenceSha256": _sha256(
                    candidate_sha256.get(candidate_id),
                    f"candidateSha256.{candidate_id}",
                ),
                "runtimeAttestationId": candidate["runtimeAttestationId"],
                "appUxCompositeRatio": round(app_ux_ratio, 9),
                "appUxGainFraction": round(app_ux_gain, 9),
                "startupGainFraction": {key: round(value, 9) for key, value in startup_gain.items()},
                "reasons": unique_reasons,
            }
        )

    passing = [decision for decision in decisions if decision["passedScreening"]]
    selected = "O0"
    reason = "fallback-official-3.14.7-jit-disabled"
    if passing:
        passing.sort(
            key=lambda decision: (
                float(decision["appUxCompositeRatio"]),
                _preference_key(str(decision["variantId"]), config),
            )
        )
        best_ratio = float(passing[0]["appUxCompositeRatio"])
        tied = [
            decision
            for decision in passing
            if math.isclose(
                float(decision["appUxCompositeRatio"]),
                best_ratio,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ]
        selected = str(
            min(
                tied,
                key=lambda decision: _preference_key(str(decision["variantId"]), config),
            )["variantId"]
        )
        reason = (
            "candidate-nominated-for-full-local"
            if len(tied) == 1
            else "screening-tie-resolved-by-official-jit-tail-order"
        )
    amd_only = config.get("profile") == AMD_PROFILE_NAME
    return {
        "decisionContract": SCREENING_DECISION_CONTRACT,
        "schemaVersion": 1,
        "profile": config["profile"],
        "scope": "amd-only" if amd_only else "intel-and-amd-screening",
        "productionPromotionAuthorized": False,
        "referenceContract": reference["referenceContract"],
        "referenceVariant": "O0",
        "fallbackVariant": "O0",
        "selectedVariant": selected,
        "reason": reason,
        "minimumGainFraction": minimum_gain,
        "tieBreakOrder": list(_object(config["promotion"], "config.promotion")["tieBreakOrder"]),
        "candidates": decisions,
    }


def _write_isolated_output(path: Path, payload: Mapping[str, Any]) -> None:
    resolved = path.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    protected = {
        (repo_root / "benchmarks" / "results" / "baseline.json").resolve(),
        (repo_root / "benchmarks" / "results" / "champion.json").resolve(),
    }
    if resolved in protected:
        raise EvidenceError("Python-runtime profile output must not overwrite AutoResearch baseline/champion")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"could not load {label} {path}: {exc}") from exc
    return dict(_object(value, label))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and evaluate locked Scriber Python-runtime evidence.")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--create-o0-screening-reference", action="store_true")
    parser.add_argument("--fast-local", type=Path)
    parser.add_argument("--startup", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="VARIANT=FASTLOCAL_JSON",
        help="Installed screening candidate; may be repeated.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "profiles" / "python-runtime" / "config.json",
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = load_config(arguments.config.resolve())
        if arguments.create_o0_screening_reference:
            if arguments.evidence or arguments.reference or arguments.candidate:
                raise EvidenceError("O0 reference creation cannot be combined with selection inputs")
            if not arguments.fast_local or not arguments.startup or not arguments.output:
                raise EvidenceError("O0 reference creation requires --fast-local, --startup, and --output")
            fast_local_path = arguments.fast_local.resolve()
            startup_path = arguments.startup.resolve()
            decision = create_o0_screening_reference(
                _load_json(fast_local_path, "O0 FastLocal evidence"),
                _load_json(startup_path, "startup evidence"),
                config,
                fast_local_sha256=_file_sha256(fast_local_path),
                startup_sha256=_file_sha256(startup_path),
            )
        elif arguments.reference:
            if arguments.evidence or arguments.fast_local or not arguments.startup or not arguments.output:
                raise EvidenceError("screening selection requires --reference, --startup, --candidate, and --output")
            candidate_payloads: dict[str, Mapping[str, Any]] = {}
            candidate_hashes: dict[str, str] = {}
            for raw_candidate in arguments.candidate:
                variant_id, separator, raw_path = raw_candidate.partition("=")
                variant_id = variant_id.strip()
                if not separator or not variant_id or not raw_path.strip():
                    raise EvidenceError("--candidate must use VARIANT=FASTLOCAL_JSON")
                if variant_id in candidate_payloads:
                    raise EvidenceError(f"duplicate screening candidate {variant_id}")
                candidate_path = Path(raw_path.strip()).resolve()
                candidate_payloads[variant_id] = _load_json(
                    candidate_path,
                    f"{variant_id} FastLocal evidence",
                )
                candidate_hashes[variant_id] = _file_sha256(candidate_path)
            startup_path = arguments.startup.resolve()
            decision = select_screening_runtime(
                _load_json(arguments.reference.resolve(), "O0 screening reference"),
                candidate_payloads,
                _load_json(startup_path, "startup evidence"),
                config,
                candidate_sha256=candidate_hashes,
                startup_sha256=_file_sha256(startup_path),
            )
        else:
            if not arguments.evidence:
                raise EvidenceError("--evidence is required for FullLocal runtime selection")
            evidence = _load_json(arguments.evidence.resolve(), "evidence")
            decision = select_runtime(evidence, config)
    except EvidenceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    rendered = json.dumps(decision, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        try:
            _write_isolated_output(arguments.output, decision)
        except EvidenceError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 2
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
