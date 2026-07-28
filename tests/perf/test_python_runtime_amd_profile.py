from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.perf.python_runtime_ab import (
    EvidenceError,
    create_o0_screening_reference,
    load_config,
    select_runtime,
    select_screening_runtime,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "scripts" / "perf" / "profiles" / "python-runtime-amd" / "config.json"
SCHEMA_PATH = ROOT / "scripts" / "perf" / "profiles" / "python-runtime-amd" / "evidence-v1.schema.json"
RUNNER_PATH = ROOT / "scripts" / "perf" / "run.ps1"


def _identity(variant_id: str, config: dict[str, object]) -> dict[str, object]:
    variant = config["variants"][variant_id]  # type: ignore[index]
    jit = bool(variant["jit"])  # type: ignore[index]
    return {
        "pythonVersion": "3.13.14" if variant_id == "A13" else "3.14.6",
        "cacheTag": "cpython-313" if variant_id == "A13" else "cpython-314",
        "runtimeFlavor": variant["runtimeFlavor"],  # type: ignore[index]
        "compiler": "test-compiler",
        "pythonDllSha256": "1" * 64,
        "tailCallInterpreter": variant["tailCallInterpreter"],  # type: ignore[index]
        "jitAvailable": variant_id not in {"A13", "K0"},
        "jitExpected": jit,
        "jitActive": jit,
        "soakMinutes": 60 if jit else 0,
    }


def _variant(variant_id: str, config: dict[str, object], score: float) -> dict[str, object]:
    latency = score * 100.0
    return {
        "identity": _identity(variant_id, config),
        "localWuxSamples": [score] * 30,
        "providerSeries": {"microsoft-5": {"p50": latency, "p95": latency}},
        "protectedP95": {"startup": latency},
        "resources": {
            "activeCpu": score * 50.0,
            "rssPrivateBytes": score * 500_000_000.0,
            "idleCpuPercentagePoints": score,
        },
        "errors": {field: 0 for field in ("text", "focus", "clipboard", "overlay", "longTask", "frame", "token")},
    }


def _evidence(config: dict[str, object]) -> dict[str, object]:
    scores = {"A13": 1.10, "O0": 1.00, "O1": 0.90, "C0": 0.92, "C1": 0.94, "T0": 0.93, "T1": 0.95}
    variants = {variant_id: _variant(variant_id, config, score) for variant_id, score in scores.items()}
    order = list(variants)
    common = {
        "cpuFamily": "amd",
        "hostProfileSha256": "a" * 64,
        "environment": {"defenderEnabled": True, "standardUser": True},
    }
    blocks = [
        {
            **common,
            "blockId": "amd-screening",
            "phase": "screening",
            "cleanInstall": False,
            "rebooted": False,
            "order": order,
            "measurementCounts": {
                "cold": 2,
                "warm": 4,
                "providerReplayPerProviderAndDuration": 5,
                "appUxPerScenario": 1,
                "appUxScenarioCount": 9,
                "frozenImportWarmups": 5,
                "frozenImportSamples": 30,
            },
            "variants": copy.deepcopy(variants),
        }
    ]
    for index, block_order in enumerate((order, list(reversed(order))), start=1):
        blocks.append(
            {
                **common,
                "blockId": f"amd-full-{index}",
                "phase": "full-local",
                "cleanInstall": True,
                "rebooted": True,
                "order": block_order,
                "measurementCounts": {
                    "cold": 15,
                    "warm": 30,
                    "providerReplayPerProviderAndDuration": 30,
                    "appUxPerScenario": 20,
                    "appUxScenarioCount": 9,
                },
                "variants": copy.deepcopy(variants),
            }
        )
    return {
        "evidenceContract": "PythonRuntimeAMDEvidenceV1",
        "schemaVersion": 1,
        "profile": "python-runtime-amd-v1",
        "sourceCommit": "c" * 40,
        "bindings": {
            "dependencyLockSha256": "d" * 64,
            "runtimeLockSha256": "e" * 64,
            "profileConfigSha256": config["_configSha256"],
            "evaluatorSha256": config["_evaluatorSha256"],
            "frontendSha256": "f" * 64,
            "tauriSha256": "1" * 64,
            "backendSha256": "2" * 64,
            "audioSha256": "3" * 64,
            "cacheKey": "runtime-amd-test",
        },
        "blocks": blocks,
    }


def test_amd_profile_preserves_thresholds_and_refuses_universal_promotion() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw["screening"]["hosts"] == ["amd"]
    assert raw["fullLocal"]["hosts"] == ["amd"]
    assert raw["promotion"]["minimumGainFraction"] == 0.05
    assert raw["promotion"]["baselineCvMultiplier"] == 2.0

    config = load_config(CONFIG_PATH)
    config["promotion"]["bootstrapReplicates"] = 1000  # type: ignore[index]
    decision = select_runtime(_evidence(config), config)
    assert decision["selectedVariant"] == "O1"
    assert decision["scope"] == "amd-only"
    assert decision["productionPromotionAuthorized"] is False


def test_amd_evidence_schema_is_bound_to_amd_only() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["evidenceContract"]["const"] == "PythonRuntimeAMDEvidenceV1"
    assert schema["properties"]["profile"]["const"] == "python-runtime-amd-v1"
    assert schema["properties"]["blocks"]["minItems"] == 3
    assert schema["$defs"]["block"]["properties"]["cpuFamily"] == {"const": "amd"}


def _fast_local_screening(
    variant_id: str,
    *,
    provider_latency: float,
    app_ux_latency: float,
    backend_sha256: str,
) -> dict[str, object]:
    metrics: dict[str, object] = {
        "app_ux_p50_ms": app_ux_latency,
        "app_ux_p95_ms": app_ux_latency,
        "text_errors": 0,
        "focus_errors": 0,
        "clipboard_errors": 0,
        "overlay_errors": 0,
        "ui_long_tasks_gt_200ms": 0,
        "idle_cpu_pct": 0.2,
        "working_set_mb": 1000.0,
    }
    for provider in ("microsoft_local", "soniox_local", "speechmatics_local"):
        for duration in (5, 15, 30, 60):
            for kpi in (
                "activation_received_to_final_text_observed",
                "stop_requested_to_final_text_observed",
            ):
                prefix = f"{provider}_{duration}s_{kpi}"
                metrics[f"{prefix}_p50_ms"] = provider_latency
                metrics[f"{prefix}_p95_ms"] = provider_latency
                metrics[f"{prefix}_failure_rate"] = 0.0
                metrics[f"{prefix}_sample_count"] = 5
                metrics[f"{prefix}_capture_attested"] = 1
    scenarios = {
        f"scenario-{index}": {
            "metricEligible": True,
            "sampleCount": 1,
            "p50Ms": app_ux_latency,
            "p95Ms": app_ux_latency,
        }
        for index in range(9)
    }
    attestation = ("a" if variant_id == "O0" else "b") * 64
    return {
        "suite": "FastLocal",
        "sourceCommit": "c" * 40,
        "runtimeAttestationSourceContentSha256": "d" * 64,
        "runtimeAttestationId": attestation,
        "runtimeAttestationManifestSha256": "e" * 64,
        "runtimeAttestationPreValid": True,
        "runtimeAttestationPostValid": True,
        "runtimeAttestationPreId": attestation,
        "runtimeAttestationPostId": attestation,
        "runtimeAttestationDriftDetected": False,
        "desktopSha256": "1" * 64,
        "backendSha256": backend_sha256,
        "audioSidecarSha256": "2" * 64,
        "appUxEvidenceProvided": True,
        "appUxEvidenceSha256": "3" * 64,
        "appUxEvidencePostSha256": "3" * 64,
        "appUxEvidenceDriftDetected": False,
        "endpointProbe": {
            "samplePlan": {
                "overlayCold": 2,
                "overlayWarm": 4,
                "providerReplay": 5,
                "providerReplayDurationsSeconds": [5, 15, 30, 60],
                "appUxPerScenario": 1,
            },
            "metrics": metrics,
            "evidence": {
                "appFrame": {
                    "metricEligible": True,
                    "scenarioResults": scenarios,
                }
            },
        },
    }


def _startup_screening() -> dict[str, object]:
    variants: dict[str, object] = {}
    for variant_id, flavour, backend, mean in (
        ("O0", "Official", "4" * 64, 100.0),
        ("C0", "ClangPgo", "5" * 64, 90.0),
    ):
        variants[variant_id] = {
            "identity": {
                "variantId": variant_id,
                "pythonVersion": "3.14.6",
                "cacheTag": "cpython-314",
                "runtimeFlavor": flavour,
                "compiler": "test-compiler",
                "pythonDll": {"sha256": ("6" if variant_id == "O0" else "7") * 64},
                "runtimeLockSha256": "8" * 64,
                "executable": {"sha256": backend},
                "jitExpected": False,
                "jitActive": False,
                "tailCallInterpreter": False,
            },
            "summary": {
                "count": 30,
                "cv": 0.01,
                "meanMs": mean,
                "p50Ms": mean,
                "p95Ms": mean,
            },
        }
    return {
        "evidenceContract": "ScriberFrozenBackendImportStartupAMDV2",
        "hostScope": "amd-only",
        "productionPromotionAuthorized": False,
        "measurement": {"warmupsPerVariant": 5, "samplesPerVariant": 30},
        "host": {"sha256": "9" * 64},
        "variants": variants,
    }


def test_isolated_o0_reference_selects_o0_from_regressing_c0() -> None:
    config = load_config(CONFIG_PATH)
    startup = _startup_screening()
    o0 = _fast_local_screening(
        "O0",
        provider_latency=100.0,
        app_ux_latency=100.0,
        backend_sha256="4" * 64,
    )
    c0 = _fast_local_screening(
        "C0",
        provider_latency=101.0,
        app_ux_latency=106.0,
        backend_sha256="5" * 64,
    )
    reference = create_o0_screening_reference(
        o0,
        startup,
        config,
        fast_local_sha256="a" * 64,
        startup_sha256="b" * 64,
    )
    decision = select_screening_runtime(
        reference,
        {"C0": c0},
        startup,
        config,
        candidate_sha256={"C0": "c" * 64},
        startup_sha256="b" * 64,
    )
    assert decision["selectedVariant"] == "O0"
    assert decision["reason"] == "fallback-official-3.14.6-jit-disabled"
    assert decision["productionPromotionAuthorized"] is False
    assert "app-ux-gain-below-threshold" in decision["candidates"][0]["reasons"]
    assert any(
        reason.startswith("provider-") and reason.endswith("-regression")
        for reason in decision["candidates"][0]["reasons"]
    )


def test_isolated_o0_reference_can_only_nominate_an_amd_candidate() -> None:
    config = load_config(CONFIG_PATH)
    startup = _startup_screening()
    o0 = _fast_local_screening(
        "O0",
        provider_latency=100.0,
        app_ux_latency=100.0,
        backend_sha256="4" * 64,
    )
    c0 = _fast_local_screening(
        "C0",
        provider_latency=90.0,
        app_ux_latency=90.0,
        backend_sha256="5" * 64,
    )
    reference = create_o0_screening_reference(
        o0,
        startup,
        config,
        fast_local_sha256="a" * 64,
        startup_sha256="b" * 64,
    )
    decision = select_screening_runtime(
        reference,
        {"C0": c0},
        startup,
        config,
        candidate_sha256={"C0": "c" * 64},
        startup_sha256="b" * 64,
    )
    assert decision["selectedVariant"] == "C0"
    assert decision["reason"] == "candidate-nominated-for-full-local"
    assert decision["productionPromotionAuthorized"] is False


def test_isolated_reference_rejects_a_different_startup_packet() -> None:
    config = load_config(CONFIG_PATH)
    startup = _startup_screening()
    o0 = _fast_local_screening(
        "O0",
        provider_latency=100.0,
        app_ux_latency=100.0,
        backend_sha256="4" * 64,
    )
    reference = create_o0_screening_reference(
        o0,
        startup,
        config,
        fast_local_sha256="a" * 64,
        startup_sha256="b" * 64,
    )
    with pytest.raises(EvidenceError, match="O0 reference startup evidence"):
        select_screening_runtime(
            reference,
            {
                "C0": _fast_local_screening(
                    "C0",
                    provider_latency=90.0,
                    app_ux_latency=90.0,
                    backend_sha256="5" * 64,
                )
            },
            startup,
            config,
            candidate_sha256={"C0": "c" * 64},
            startup_sha256="f" * 64,
        )


def test_fast_local_runner_uses_only_explicit_python_runtime_profile_outputs() -> None:
    script = RUNNER_PATH.read_text(encoding="utf-8")
    assert "[string]$PythonRuntimeReferencePath" in script
    assert "[string]$PythonRuntimeDecisionPath" in script
    assert '"--create-o0-screening-reference"' in script
    assert '"--reference", $PythonRuntimeReferencePath' in script
    assert "Python-runtime profile output must not overwrite AutoResearch baseline/champion." in script
