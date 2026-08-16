from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.perf.python_runtime_ab import EvidenceError, load_config, select_runtime, validate_evidence

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "scripts" / "perf" / "profiles" / "python-runtime" / "config.json"


def _identity(variant_id: str, config: dict[str, object]) -> dict[str, object]:
    variant = config["variants"][variant_id]  # type: ignore[index]
    jit = bool(variant["jit"])  # type: ignore[index]
    return {
        "pythonVersion": "3.13.14" if variant_id == "A13" else "3.14.7",
        "cacheTag": "cpython-313" if variant_id == "A13" else "cpython-314",
        "runtimeFlavor": variant["runtimeFlavor"],  # type: ignore[index]
        "compiler": "test-compiler",
        "pythonDllSha256": "1" * 64,
        "tailCallInterpreter": variant["tailCallInterpreter"],  # type: ignore[index]
        "jitAvailable": variant_id != "A13" and variant_id != "K0",
        "jitExpected": jit,
        "jitActive": jit,
        "soakMinutes": 60 if jit else 0,
    }


def _variant(
    variant_id: str,
    config: dict[str, object],
    *,
    local_wux: float,
) -> dict[str, object]:
    latency = local_wux * 100.0
    return {
        "identity": _identity(variant_id, config),
        "localWuxSamples": [local_wux] * 30,
        "providerSeries": {
            "microsoft-5": {"p50": latency, "p95": latency},
            "soniox-60": {"p50": latency, "p95": latency},
        },
        "protectedP95": {"startup": latency, "meeting-finalize": latency},
        "resources": {
            "activeCpu": local_wux * 50.0,
            "rssPrivateBytes": local_wux * 500_000_000.0,
            "idleCpuPercentagePoints": local_wux,
        },
        "errors": {
            "text": 0,
            "focus": 0,
            "clipboard": 0,
            "overlay": 0,
            "longTask": 0,
            "frame": 0,
            "token": 0,
        },
    }


def _evidence(
    config: dict[str, object],
    candidate_scores: dict[str, float] | None = None,
) -> dict[str, object]:
    scores = {
        "A13": 1.10,
        "O0": 1.00,
        "O1": 0.90,
        "C0": 0.92,
        "C1": 0.94,
        "T0": 0.93,
        "T1": 0.95,
    }
    scores.update(candidate_scores or {})
    variants = {variant_id: _variant(variant_id, config, local_wux=score) for variant_id, score in scores.items()}
    first_order = list(variants)
    second_order = list(reversed(first_order))
    blocks = []
    for cpu_family, order in (("intel", first_order), ("amd", second_order)):
        blocks.append(
            {
                "blockId": f"{cpu_family}-screening",
                "phase": "screening",
                "cpuFamily": cpu_family,
                "hostProfileSha256": ("a" if cpu_family == "intel" else "b") * 64,
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
                "environment": {
                    "defenderEnabled": True,
                    "standardUser": True,
                },
                "variants": copy.deepcopy(variants),
            }
        )
    for cpu_family in ("intel", "amd"):
        for index in range(2):
            blocks.append(
                {
                    "blockId": f"{cpu_family}-{index + 1}",
                    "phase": "full-local",
                    "cpuFamily": cpu_family,
                    "hostProfileSha256": ("a" if cpu_family == "intel" else "b") * 64,
                    "cleanInstall": True,
                    "rebooted": True,
                    "order": first_order if index == 0 else second_order,
                    "measurementCounts": {
                        "cold": 15,
                        "warm": 30,
                        "providerReplayPerProviderAndDuration": 30,
                        "appUxPerScenario": 20,
                        "appUxScenarioCount": 9,
                    },
                    "environment": {
                        "defenderEnabled": True,
                        "standardUser": True,
                    },
                    "variants": copy.deepcopy(variants),
                }
            )
    return {
        "evidenceContract": "PythonRuntimeABEvidenceV1",
        "schemaVersion": 1,
        "profile": "python-runtime-ab-v1",
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
            "cacheKey": "runtime-ab-test",
        },
        "blocks": blocks,
    }


@pytest.fixture
def config() -> dict[str, object]:
    result = load_config(CONFIG_PATH)
    result["promotion"]["bootstrapReplicates"] = 1000  # type: ignore[index]
    return result


def test_profile_keeps_complete_variant_and_measurement_matrix() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert set(config["variants"]) == {"A13", "O0", "O1", "C0", "C1", "T0", "T1", "K0"}
    assert config["screening"]["fastLocalSamples"] == {
        "cold": 2,
        "warm": 4,
        "providerReplayPerProviderAndDuration": 5,
        "appUxPerScenario": 1,
    }
    assert config["screening"]["frozenImportStartup"] == {"warmups": 5, "samples": 30}
    assert config["fullLocal"]["samples"] == {
        "cold": 15,
        "warm": 30,
        "providerReplayPerProviderAndDuration": 30,
        "appUxPerScenario": 20,
    }
    assert config["promotion"]["fallbackVariant"] == "O0"


def test_official_jit_variant_wins_when_every_block_and_guardrail_passes(
    config: dict[str, object],
) -> None:
    result = select_runtime(_evidence(config), config)
    assert result["selectedVariant"] == "O1"
    assert result["reason"] == "optimized-candidate-promoted"
    o1 = next(candidate for candidate in result["candidates"] if candidate["variantId"] == "O1")
    assert o1["passed"] is True
    assert o1["reasons"] == []


def test_no_optimized_winner_falls_back_to_official_jit_disabled(
    config: dict[str, object],
) -> None:
    result = select_runtime(
        _evidence(
            config,
            {
                "O1": 1.0,
                "C0": 1.0,
                "C1": 1.0,
                "T0": 1.0,
                "T1": 1.0,
            },
        ),
        config,
    )
    assert result["selectedVariant"] == "O0"
    assert result["reason"] == "fallback-official-3.14.7-jit-disabled"


def test_provider_regression_blocks_even_a_large_local_wux_gain(
    config: dict[str, object],
) -> None:
    evidence = _evidence(config)
    for block in evidence["blocks"]:  # type: ignore[union-attr]
        block["variants"]["O1"]["providerSeries"]["microsoft-5"]["p95"] = 101.0
    result = select_runtime(evidence, config)
    o1 = next(candidate for candidate in result["candidates"] if candidate["variantId"] == "O1")
    assert o1["passed"] is False
    assert any("provider-microsoft-5-p95-regression:O0" in reason for reason in o1["reasons"])


def test_jit_expectation_must_match_observed_state(
    config: dict[str, object],
) -> None:
    evidence = _evidence(config)
    evidence["blocks"][0]["variants"]["O1"]["identity"]["jitActive"] = False  # type: ignore[index]
    with pytest.raises(EvidenceError, match="JIT expected/active state"):
        validate_evidence(evidence, config)


def test_missing_screening_host_is_rejected(
    config: dict[str, object],
) -> None:
    evidence = _evidence(config)
    evidence["blocks"] = [  # type: ignore[index]
        block
        for block in evidence["blocks"]  # type: ignore[union-attr]
        if block["blockId"] != "amd-screening"
    ]
    with pytest.raises(EvidenceError, match="one screening block for Intel and AMD"):
        validate_evidence(evidence, config)


def test_o0_protected_error_keeps_goal_open_instead_of_falling_back(
    config: dict[str, object],
) -> None:
    evidence = _evidence(config)
    evidence["blocks"][2]["variants"]["O0"]["errors"]["token"] = 1  # type: ignore[index]
    with pytest.raises(EvidenceError, match="O0 has protected errors"):
        select_runtime(evidence, config)
