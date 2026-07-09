from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "summarize_release_artifacts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("summarize_release_artifacts", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_release_artifact_summary_combines_timing_and_cache_evidence(tmp_path: Path) -> None:
    module = load_module()
    artifact_root = tmp_path / "run"
    metadata_root = artifact_root / "release-metadata"
    write_json(
        metadata_root / "build-timing.json",
        {
            "totalDurationMs": 123456,
            "buildMode": {"artifactKind": "installer", "nsisCompression": "lzma"},
            "sidecar": {
                "totalDurationMs": 7051,
                "targetCurrent": False,
                "cache": {
                    "enabled": True,
                    "hit": True,
                    "key": "4fe763b640f08b47aff8a7c1a7da3a3f0ff2a9fd33df8fa2c872a9d3b200da2d5",
                },
                "rustAudioSidecarCopied": {
                    "cacheHit": True,
                    "cacheKey": "2109219f20c58d8e3dd80f2c8f9e07f846b7981760b44d4bf3e354d9ddbd0f34",
                },
                "phases": [
                    {"label": "sidecar-cache-restore", "durationMs": 1899, "ok": True},
                    {"label": "frozen-runtime-import-check", "durationMs": 3060, "ok": True},
                    {"label": "rust-audio-sidecar-build", "durationMs": 105, "ok": True},
                ],
            },
            "phases": [
                {"label": "Tauri Windows bundle", "durationMs": 100000, "ok": True},
                {"label": "Frontend type check", "durationMs": 12000, "ok": True},
                {"label": "Runtime dependency footprint", "durationMs": 500, "ok": True},
            ],
        },
    )
    write_json(
        metadata_root / "release-cache-summary.json",
        {
            "rows": [
                {
                    "Name": "Rust build",
                    "Actions": "restore-key-or-miss",
                    "ReleaseArtifact": "false",
                    "Effective": "actions-cache-restore-key-or-miss",
                },
                {
                    "Name": "Backend sidecar",
                    "Actions": "exact",
                    "ReleaseArtifact": "false",
                    "Effective": "actions-cache-exact",
                },
                {
                    "Name": "FFmpeg Profile B",
                    "Actions": "miss",
                    "ReleaseArtifact": "true",
                    "Effective": "release-artifact",
                },
            ],
            "pathEvidence": [
                {
                    "Name": "Rust build",
                    "Exists": "false",
                    "NonEmpty": "false",
                    "ExistingPaths": "none",
                },
                {
                    "Name": "Backend sidecar",
                    "Exists": "true",
                    "NonEmpty": "true",
                    "ExistingPaths": "build\\tauri-sidecar-cache",
                },
            ],
            "rustBuildReleaseArtifact": {"exact": "false", "restored": "false", "imported": "false"},
            "backendSidecarPrebuilt": True,
        },
    )
    write_json(
        metadata_root / "tauri-bundle-log-summary.json",
        {
            "lineCount": 42,
            "sizeBytes": 4096,
            "counts": {"cargoCompiling": 0, "nsis": 3, "signing": 2},
            "signals": {
                "crateDownloadsDetected": False,
                "crateIndexUpdateDetected": False,
                "cargoCompileDetected": False,
                "nsisDetected": True,
                "signingDetected": True,
            },
            "durations": {
                "firstLineToMakensisSeconds": 91.5,
                "makensisToUpdaterSignatureSeconds": 65.25,
                "firstLineToLastLineSeconds": 157.0,
            },
            "firstCargoCompileLines": [],
        },
    )

    summary = module.summarize_release_artifacts(artifact_root)

    assert summary["ok"] is True
    assert summary["buildTiming"]["totalSeconds"] == 123.456
    assert summary["buildTiming"]["topPhases"][0]["label"] == "Tauri Windows bundle"
    assert summary["buildTiming"]["sidecar"]["present"] is True
    assert summary["buildTiming"]["sidecar"]["totalSeconds"] == 7.051
    assert summary["buildTiming"]["sidecar"]["cacheHit"] is True
    assert summary["buildTiming"]["sidecar"]["cacheKeyPrefix"] == "4fe763b640f0"
    assert summary["buildTiming"]["sidecar"]["rustAudioCacheHit"] is True
    assert summary["buildTiming"]["sidecar"]["rustAudioCacheKeyPrefix"] == "2109219f20c5"
    assert summary["buildTiming"]["sidecar"]["pyInstallerRebuilt"] is False
    assert summary["buildTiming"]["sidecar"]["rustAudioRebuilt"] is False
    assert summary["buildTiming"]["sidecar"]["topPhases"][0]["label"] == "frozen-runtime-import-check"
    assert summary["cacheSummary"]["effectiveCounts"] == {
        "actions-cache-exact": 1,
        "actions-cache-restore-key-or-miss": 1,
        "release-artifact": 1,
    }
    assert summary["cacheSummary"]["actionsCounts"] == {
        "exact": 1,
        "miss": 1,
        "restore-key-or-miss": 1,
    }
    assert summary["tauriBundleLog"]["present"] is True
    assert summary["tauriBundleLog"]["counts"]["cargoCompiling"] == 0
    assert summary["tauriBundleLog"]["signals"]["nsisDetected"] is True
    assert summary["tauriBundleLog"]["durations"]["makensisToUpdaterSignatureSeconds"] == 65.25
    assert [row["name"] for row in summary["cacheSummary"]["uncertainRows"]] == ["Rust build"]
    assert "build_windows total: 123.456s" in summary["oracleBrief"]
    assert (
        "sidecar: total=7.051s cacheHit=True targetCurrent=False "
        "pyInstallerRebuilt=False rustAudioCacheHit=True"
    ) in summary["oracleBrief"]
    assert "uncertain restore-key-or-miss rows: ['Rust build']" in summary["oracleBrief"]
    assert [item["code"] for item in summary["diagnostics"]] == ["ambiguous-actions-restore"]
    assert summary["recommendations"] == [
        {
            "priority": "medium",
            "code": "inspect-path-evidence",
            "action": "Use pathEvidence before treating Actions cache-hit=false as a true cache miss.",
        }
    ]
    assert "diagnostics: ambiguous-actions-restore=info" in summary["oracleBrief"]
    assert "recommendations: inspect-path-evidence=medium" in summary["oracleBrief"]


def test_release_artifact_summary_flags_tauri_cargo_work_from_bundle_log(tmp_path: Path) -> None:
    module = load_module()
    artifact_root = tmp_path / "run"
    metadata_root = artifact_root / "release-metadata"
    write_json(
        metadata_root / "build-timing.json",
        {
            "totalDurationMs": 240000,
            "sidecar": {
                "totalDurationMs": 7000,
                "targetCurrent": False,
                "cache": {"enabled": True, "hit": True, "key": "abc123"},
                "rustAudioSidecarCopied": {"cacheHit": True, "cacheKey": "def456"},
                "phases": [{"label": "sidecar-cache-restore", "durationMs": 1000, "ok": True}],
            },
            "phases": [
                {"label": "Tauri Windows bundle", "durationMs": 180000, "ok": True},
                {"label": "Frontend type check", "durationMs": 12000, "ok": True},
            ],
        },
    )
    write_json(
        metadata_root / "release-cache-summary.json",
        {
            "rows": [
                {
                    "Name": "Backend sidecar",
                    "Actions": "exact",
                    "ReleaseArtifact": "false",
                    "Effective": "actions-cache-exact",
                },
            ],
            "pathEvidence": [
                {
                    "Name": "Backend sidecar",
                    "Exists": "true",
                    "NonEmpty": "true",
                    "ExistingPaths": "build\\tauri-sidecar-cache",
                },
            ],
        },
    )
    write_json(
        metadata_root / "tauri-bundle-log-summary.json",
        {
            "lineCount": 900,
            "sizeBytes": 65000,
            "counts": {
                "cargoUpdatingIndex": 1,
                "cargoDownloaded": 8,
                "cargoDownloading": 3,
                "cargoCompiling": 31,
                "nsis": 5,
                "signing": 2,
            },
            "signals": {
                "crateIndexUpdateDetected": True,
                "crateDownloadsDetected": True,
                "cargoCompileDetected": True,
                "nsisDetected": True,
                "signingDetected": True,
            },
            "firstCargoCompileLines": ["Compiling tauri v2.0.0", "Compiling scriber-desktop v0.1.0"],
            "durations": {
                "firstLineToMakensisSeconds": 95.0,
                "makensisToUpdaterSignatureSeconds": 76.7,
                "firstLineToLastLineSeconds": 173.0,
            },
        },
    )

    summary = module.summarize_release_artifacts(artifact_root)

    codes = {item["code"] for item in summary["diagnostics"]}
    assert "tauri-bundle-dominant" in codes
    assert "tauri-crate-downloads-detected" in codes
    assert "tauri-cargo-compile-detected" in codes
    assert "tauri-nsis-signing-heavy" in codes
    assert "tauri-bundle-no-cargo-rebuild-detected" not in codes
    recommendation_codes = {item["code"] for item in summary["recommendations"]}
    assert "inspect-tauri-cargo-fingerprints" in recommendation_codes
    assert "profile-nsis-compression-signing" in recommendation_codes
    assert any(line.startswith("tauri bundle log: cargoCompiling=31") for line in summary["oracleBrief"])
    assert any(
        line.startswith("tauri bundle durations: firstLineToMakensis=95.0s")
        for line in summary["oracleBrief"]
    )
    assert summary["tauriBundleLog"]["firstCargoCompileLines"] == [
        "Compiling tauri v2.0.0",
        "Compiling scriber-desktop v0.1.0",
    ]


def test_release_artifact_summary_diagnoses_rebuilds_and_cache_misses(tmp_path: Path) -> None:
    module = load_module()
    artifact_root = tmp_path / "run"
    metadata_root = artifact_root / "release-metadata"
    write_json(
        metadata_root / "build-timing.json",
        {
            "totalDurationMs": 400000,
            "sidecar": {
                "totalDurationMs": 120000,
                "targetCurrent": False,
                "cache": {"enabled": True, "hit": False, "key": "abc123"},
                "rustAudioSidecarCopied": {"cacheHit": False, "cacheKey": "def456"},
                "phases": [
                    {"label": "pyinstaller-build", "durationMs": 90000, "ok": True},
                    {"label": "rust-audio-sidecar-build", "durationMs": 30000, "ok": True},
                ],
            },
            "phases": [
                {"label": "Tauri Windows bundle", "durationMs": 150000, "ok": True},
                {"label": "Tauri sidecar preparation", "durationMs": 120000, "ok": True},
            ],
        },
    )
    write_json(
        metadata_root / "release-cache-summary.json",
        {
            "rows": [
                {
                    "Name": "Backend sidecar",
                    "Actions": "miss",
                    "ReleaseArtifact": "false",
                    "Effective": "miss",
                }
            ],
            "pathEvidence": [
                {
                    "Name": "Backend sidecar",
                    "Exists": "false",
                    "NonEmpty": "false",
                    "ExistingPaths": "none",
                }
            ],
        },
    )

    summary = module.summarize_release_artifacts(artifact_root)

    codes = {item["code"] for item in summary["diagnostics"]}
    assert {
        "pyinstaller-rebuilt",
        "rust-audio-rebuilt",
        "backend-sidecar-cache-not-hot",
        "effective-cache-miss",
        "workflow-still-slow",
    }.issubset(codes)
    assert "tauri-bundle-dominant" not in codes
    assert any(item["severity"] == "warning" for item in summary["diagnostics"])
    recommendation_codes = {item["code"] for item in summary["recommendations"]}
    assert {
        "inspect-backend-sidecar-cache",
        "inspect-rust-audio-cache",
        "inspect-effective-cache-misses",
        "compare-phase-timings",
    }.issubset(recommendation_codes)
    assert any(item["priority"] == "high" for item in summary["recommendations"])
    assert any(line.startswith("diagnostics:") for line in summary["oracleBrief"])
    assert summary["oracleBrief"][-1].startswith("recommendations:")


def test_release_artifact_summary_reports_missing_inputs(tmp_path: Path) -> None:
    module = load_module()
    artifact_root = tmp_path / "run"
    write_json(
        artifact_root / "build-timing.json",
        {
            "totalDurationMs": 1000,
            "phases": [],
        },
    )
    output = tmp_path / "summary.json"

    summary = module.summarize_release_artifacts(artifact_root)
    exit_code = module.main([str(artifact_root), "--output", str(output)])

    assert summary["ok"] is False
    assert summary["buildTiming"]["present"] is True
    assert summary["cacheSummary"]["present"] is False
    assert "release-cache-summary.json missing" in summary["oracleBrief"]
    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["ok"] is False
