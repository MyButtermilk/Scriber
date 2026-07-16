from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def find_first(root: Path, name: str) -> Path | None:
    direct = root / name
    if direct.is_file():
        return direct
    matches = sorted(root.rglob(name), key=lambda path: (len(path.parts), str(path)))
    return matches[0] if matches else None


def load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def seconds(value_ms: Any) -> float | None:
    if not isinstance(value_ms, (int, float)):
        return None
    return round(float(value_ms) / 1000.0, 3)


def normalize_phases(phases: Any) -> list[dict[str, Any]]:
    if not isinstance(phases, list):
        return []

    normalized_phases: list[dict[str, Any]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        normalized_phases.append(
            {
                "label": str(phase.get("label", "")),
                "seconds": seconds(phase.get("durationMs")),
                "ok": phase.get("ok"),
            }
        )
    normalized_phases.sort(key=lambda item: item["seconds"] or 0.0, reverse=True)
    return normalized_phases


def summarize_sidecar(sidecar: Any) -> dict[str, Any]:
    if not isinstance(sidecar, dict):
        return {"present": False}

    cache = sidecar.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    runtime_layer = sidecar.get("runtimeLayer")
    if not isinstance(runtime_layer, dict):
        runtime_layer = {}
    rust_audio = sidecar.get("rustAudioSidecarCopied")
    if not isinstance(rust_audio, dict):
        rust_audio = {}
    phases = normalize_phases(sidecar.get("phases"))

    phase_labels = {phase.get("label") for phase in phases}
    return {
        "present": True,
        "totalSeconds": seconds(sidecar.get("totalDurationMs")),
        "targetCurrent": sidecar.get("targetCurrent"),
        "cacheEnabled": cache.get("enabled"),
        "cacheHit": cache.get("hit"),
        "cacheKeyPrefix": str(cache.get("key", ""))[:12] if cache.get("key") else None,
        "runtimeCacheHit": runtime_layer.get("cacheHit"),
        "runtimeCacheKeyPrefix": str(runtime_layer.get("cacheKey", ""))[:12]
        if runtime_layer.get("cacheKey")
        else None,
        "rustAudioCacheHit": rust_audio.get("cacheHit"),
        "rustAudioCacheKeyPrefix": str(rust_audio.get("cacheKey", ""))[:12]
        if rust_audio.get("cacheKey")
        else None,
        "pyInstallerRebuilt": "pyinstaller-build" in phase_labels,
        "rustAudioRebuilt": "rust-audio-sidecar-build" in phase_labels
        and rust_audio.get("cacheHit") is not True,
        "topPhases": phases[:8],
        "failedPhases": [phase for phase in phases if phase.get("ok") is False],
    }


def summarize_build_timing(data: dict[str, Any] | None, path: Path | None) -> dict[str, Any]:
    if data is None:
        return {"present": False, "path": None}

    normalized_phases = normalize_phases(data.get("phases"))

    return {
        "present": True,
        "path": str(path) if path else None,
        "totalSeconds": seconds(data.get("totalDurationMs")),
        "buildMode": data.get("buildMode") if isinstance(data.get("buildMode"), dict) else {},
        "topPhases": normalized_phases[:8],
        "failedPhases": [phase for phase in normalized_phases if phase.get("ok") is False],
        "sidecar": summarize_sidecar(data.get("sidecar")),
    }


def summarize_tauri_bundle_log(data: dict[str, Any] | None, path: Path | None) -> dict[str, Any]:
    if data is None:
        return {"present": False, "path": None}

    counts = data.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    signals = data.get("signals")
    if not isinstance(signals, dict):
        signals = {}
    milestones = data.get("milestones")
    if not isinstance(milestones, dict):
        milestones = {}
    durations = data.get("durations")
    if not isinstance(durations, dict):
        durations = {}
    first_compile_lines = data.get("firstCargoCompileLines")
    if not isinstance(first_compile_lines, list):
        first_compile_lines = []

    return {
        "present": True,
        "path": str(path) if path else None,
        "lineCount": data.get("lineCount"),
        "sizeBytes": data.get("sizeBytes"),
        "counts": counts,
        "signals": signals,
        "milestones": milestones,
        "durations": durations,
        "firstCargoCompileLines": [str(line) for line in first_compile_lines[:12]],
    }


def truthy_string(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def summarize_cache(data: dict[str, Any] | None, path: Path | None) -> dict[str, Any]:
    if data is None:
        return {"present": False, "path": None}

    rows = data.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    path_evidence = data.get("pathEvidence", [])
    if not isinstance(path_evidence, list):
        path_evidence = []

    evidence_by_name = {
        str(item.get("Name")): item
        for item in path_evidence
        if isinstance(item, dict) and item.get("Name") is not None
    }

    normalized_rows: list[dict[str, Any]] = []
    effective_counts: Counter[str] = Counter()
    actions_counts: Counter[str] = Counter()
    uncertain_rows: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name", ""))
        actions = str(row.get("Actions", ""))
        effective = str(row.get("Effective", ""))
        evidence = evidence_by_name.get(name, {})
        non_empty = truthy_string(evidence.get("NonEmpty")) if evidence else False
        normalized = {
            "name": name,
            "actions": actions,
            "releaseArtifact": str(row.get("ReleaseArtifact", "")),
            "effective": effective,
            "pathNonEmpty": non_empty,
            "existingPaths": str(evidence.get("ExistingPaths", "none")) if evidence else "none",
        }
        normalized_rows.append(normalized)
        effective_counts[effective] += 1
        actions_counts[actions] += 1
        if actions == "restore-key-or-miss" and not non_empty and effective != "release-artifact":
            uncertain_rows.append(normalized)

    return {
        "present": True,
        "path": str(path) if path else None,
        "effectiveCounts": dict(sorted(effective_counts.items())),
        "actionsCounts": dict(sorted(actions_counts.items())),
        "uncertainRows": uncertain_rows,
        "rows": normalized_rows,
        "rustBuildReleaseArtifact": data.get("rustBuildReleaseArtifact", {}),
        "backendSidecarPrebuilt": data.get("backendSidecarPrebuilt"),
    }


def build_oracle_brief(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    build = summary.get("buildTiming", {})
    cache = summary.get("cacheSummary", {})
    tauri_log = summary.get("tauriBundleLog", {})
    diagnostics = summary.get("diagnostics", [])
    recommendations = summary.get("recommendations", [])
    if build.get("present"):
        lines.append(f"build_windows total: {build.get('totalSeconds')}s")
        for phase in build.get("topPhases", [])[:5]:
            lines.append(f"phase {phase.get('label')}: {phase.get('seconds')}s ok={phase.get('ok')}")
        sidecar = build.get("sidecar", {})
        if sidecar.get("present"):
            lines.append(
                "sidecar: "
                f"total={sidecar.get('totalSeconds')}s "
                f"cacheHit={sidecar.get('cacheHit')} "
                f"targetCurrent={sidecar.get('targetCurrent')} "
                f"pyInstallerRebuilt={sidecar.get('pyInstallerRebuilt')} "
                f"runtimeCacheHit={sidecar.get('runtimeCacheHit')} "
                f"rustAudioCacheHit={sidecar.get('rustAudioCacheHit')}"
            )
    else:
        lines.append("build-timing.json missing")

    if cache.get("present"):
        lines.append(f"cache effective counts: {cache.get('effectiveCounts')}")
        uncertain = cache.get("uncertainRows", [])
        if uncertain:
            lines.append(f"uncertain restore-key-or-miss rows: {[row.get('name') for row in uncertain]}")
    else:
        lines.append("release-cache-summary.json missing")

    if tauri_log.get("present"):
        counts = tauri_log.get("counts", {})
        signals = tauri_log.get("signals", {})
        durations = tauri_log.get("durations", {})
        lines.append(
            "tauri bundle log: "
            f"cargoCompiling={counts.get('cargoCompiling')} "
            f"crateDownloadsDetected={signals.get('crateDownloadsDetected')} "
            f"nsisDetected={signals.get('nsisDetected')} "
            f"signingDetected={signals.get('signingDetected')}"
        )
        if durations:
            lines.append(
                "tauri bundle durations: "
                f"firstLineToMakensis={durations.get('firstLineToMakensisSeconds')}s "
                f"makensisToUpdaterSignature={durations.get('makensisToUpdaterSignatureSeconds')}s "
                f"firstLineToLastLine={durations.get('firstLineToLastLineSeconds')}s"
            )
    if diagnostics:
        lines.append(
            "diagnostics: "
            + "; ".join(f"{item.get('code')}={item.get('severity')}" for item in diagnostics)
        )
    if recommendations:
        lines.append(
            "recommendations: "
            + "; ".join(f"{item.get('code')}={item.get('priority')}" for item in recommendations)
        )
    return lines


def phase_seconds(build: dict[str, Any], label: str) -> float | None:
    for phase in build.get("topPhases", []):
        if phase.get("label") == label:
            value = phase.get("seconds")
            return value if isinstance(value, (int, float)) else None
    return None


def add_diagnostic(
    diagnostics: list[dict[str, str]],
    severity: str,
    code: str,
    message: str,
) -> None:
    diagnostics.append({"severity": severity, "code": code, "message": message})


def analyze_summary(summary: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    build = summary.get("buildTiming", {})
    cache = summary.get("cacheSummary", {})
    tauri_log = summary.get("tauriBundleLog", {})

    if not build.get("present"):
        add_diagnostic(diagnostics, "error", "missing-build-timing", "build-timing.json is missing.")
    if not cache.get("present"):
        add_diagnostic(
            diagnostics,
            "warning",
            "missing-cache-summary",
            "release-cache-summary.json is missing; cache attribution is incomplete.",
        )

    sidecar = build.get("sidecar", {}) if isinstance(build, dict) else {}
    if sidecar.get("present"):
        if sidecar.get("pyInstallerRebuilt"):
            add_diagnostic(
                diagnostics,
                "warning",
                "pyinstaller-rebuilt",
                "Backend PyInstaller rebuilt; inspect backend-sidecar cache inputs.",
            )
        if sidecar.get("rustAudioRebuilt"):
            add_diagnostic(
                diagnostics,
                "warning",
                "rust-audio-rebuilt",
                "Rust audio sidecar rebuilt; inspect rust-audio-sidecar cache inputs.",
            )
        if (
            sidecar.get("cacheHit") is not True
            and sidecar.get("targetCurrent") is not True
            and sidecar.get("runtimeCacheHit") is not True
        ):
            add_diagnostic(
                diagnostics,
                "warning",
                "backend-sidecar-cache-not-hot",
                "Backend sidecar was neither target-current nor an internal cache hit.",
            )

    if cache.get("present"):
        miss_rows = [row.get("name") for row in cache.get("rows", []) if row.get("effective") == "miss"]
        if miss_rows:
            add_diagnostic(
                diagnostics,
                "warning",
                "effective-cache-miss",
                "Cache rows without Actions or release-artifact fallback: " + ", ".join(miss_rows),
            )
        uncertain_rows = [row.get("name") for row in cache.get("uncertainRows", [])]
        if uncertain_rows:
            add_diagnostic(
                diagnostics,
                "info",
                "ambiguous-actions-restore",
                "Actions restore-key-or-miss rows need path evidence: " + ", ".join(uncertain_rows),
            )

    tauri_bundle_seconds = phase_seconds(build, "Tauri Windows bundle")
    build_total = build.get("totalSeconds")
    sidecar_hot = (
        sidecar.get("cacheHit") is True
        or sidecar.get("targetCurrent") is True
        or sidecar.get("runtimeCacheHit") is True
    )
    if isinstance(tauri_bundle_seconds, (int, float)) and tauri_bundle_seconds >= 120 and sidecar_hot:
        add_diagnostic(
            diagnostics,
            "info",
            "tauri-bundle-dominant",
            "Tauri Windows bundle is the dominant residual phase after sidecar cache reuse.",
        )
        if tauri_log.get("present"):
            counts = tauri_log.get("counts", {})
            signals = tauri_log.get("signals", {})
            durations = tauri_log.get("durations", {})
            cargo_compiling = counts.get("cargoCompiling")
            if signals.get("crateDownloadsDetected") is True or signals.get("crateIndexUpdateDetected") is True:
                add_diagnostic(
                    diagnostics,
                    "warning",
                    "tauri-crate-downloads-detected",
                    "Tauri bundle log shows Cargo registry/index or crate downloads during the residual bundle phase.",
                )
            if isinstance(cargo_compiling, int) and cargo_compiling > 0:
                add_diagnostic(
                    diagnostics,
                    "warning" if cargo_compiling >= 20 else "info",
                    "tauri-cargo-compile-detected",
                    f"Tauri bundle log shows {cargo_compiling} Cargo compile lines.",
                )
            if (
                signals.get("crateDownloadsDetected") is not True
                and signals.get("crateIndexUpdateDetected") is not True
                and not (isinstance(cargo_compiling, int) and cargo_compiling > 0)
            ):
                add_diagnostic(
                    diagnostics,
                    "info",
                    "tauri-bundle-no-cargo-rebuild-detected",
                    "Tauri bundle log does not show Cargo dependency downloads or compile lines.",
                )
            makensis_to_signature = durations.get("makensisToUpdaterSignatureSeconds")
            if isinstance(makensis_to_signature, (int, float)) and makensis_to_signature >= 60:
                add_diagnostic(
                    diagnostics,
                    "info",
                    "tauri-nsis-signing-heavy",
                    "Tauri bundle log shows at least 60 seconds between makensis start and updater signature completion.",
                )
    if isinstance(build_total, (int, float)) and build_total >= 300:
        add_diagnostic(
            diagnostics,
            "info",
            "workflow-still-slow",
            "build_windows.ps1 still exceeds five minutes; compare phase timings before changing caches.",
        )

    if not diagnostics:
        add_diagnostic(
            diagnostics,
            "info",
            "no-obvious-cache-regression",
            "No obvious cache miss or sidecar rebuild was detected in the summary inputs.",
        )
    return diagnostics


def build_recommendations(diagnostics: list[dict[str, str]]) -> list[dict[str, str]]:
    codes = {item.get("code") for item in diagnostics}
    recommendations: list[dict[str, str]] = []

    def add(priority: str, code: str, action: str) -> None:
        recommendations.append({"priority": priority, "code": code, "action": action})

    if "pyinstaller-rebuilt" in codes or "backend-sidecar-cache-not-hot" in codes:
        add(
            "high",
            "inspect-backend-sidecar-cache",
            "Compare backend-sidecar cache-key fingerprints and sidecar.cache.key before changing Python dependencies.",
        )
    if "rust-audio-rebuilt" in codes:
        add(
            "high",
            "inspect-rust-audio-cache",
            "Inspect rust-audio-sidecar cache inputs and avoid touching app-version-only Cargo metadata.",
        )
    if "effective-cache-miss" in codes:
        add(
            "high",
            "inspect-effective-cache-misses",
            "Check rows with Effective=miss and decide whether main/manual cache refresh is required.",
        )
    if "ambiguous-actions-restore" in codes:
        add(
            "medium",
            "inspect-path-evidence",
            "Use pathEvidence before treating Actions cache-hit=false as a true cache miss.",
        )
    if "tauri-bundle-dominant" in codes:
        add(
            "medium",
            "profile-tauri-bundle",
            "Focus the next experiment on Tauri/Cargo/NSIS timing; do not tune Python, npm, or FFmpeg first.",
        )
    if "tauri-crate-downloads-detected" in codes or "tauri-cargo-compile-detected" in codes:
        add(
            "high",
            "inspect-tauri-cargo-fingerprints",
            "Enable SCRIBER_CARGO_LOG=cargo::core::compiler::fingerprint=info and inspect why the main Tauri crate or dependencies rebuilt.",
        )
    if "tauri-bundle-no-cargo-rebuild-detected" in codes:
        add(
            "medium",
            "measure-nsis-signing",
            "Treat the remaining Tauri bundle time as NSIS/signing/bundler overhead before changing dependency caches.",
        )
    if "tauri-nsis-signing-heavy" in codes:
        add(
            "medium",
            "profile-nsis-compression-signing",
            "Compare NSIS compression/signing timing before changing dependency caches; tag releases may be dominated by real installer packaging.",
        )
    if "missing-cache-summary" in codes:
        add(
            "medium",
            "regenerate-cache-summary",
            "Use a newer workflow artifact or regenerate release-cache-summary.json before cache attribution.",
        )
    if "workflow-still-slow" in codes:
        add(
            "medium",
            "compare-phase-timings",
            "Compare build-timing phases across the previous hot run before changing cache keys.",
        )
    if not recommendations:
        add(
            "low",
            "repeat-hot-run",
            "Repeat a version-only hot run before making more cache changes.",
        )
    return recommendations


def summarize_release_artifacts(root: Path) -> dict[str, Any]:
    root = root.resolve()
    timing_path = find_first(root, "build-timing.json")
    cache_path = find_first(root, "release-cache-summary.json")
    tauri_bundle_log_path = find_first(root, "tauri-bundle-log-summary.json")
    summary = {
        "artifactRoot": str(root),
        "buildTiming": summarize_build_timing(load_json(timing_path), timing_path),
        "cacheSummary": summarize_cache(load_json(cache_path), cache_path),
        "tauriBundleLog": summarize_tauri_bundle_log(
            load_json(tauri_bundle_log_path), tauri_bundle_log_path
        ),
    }
    summary["diagnostics"] = analyze_summary(summary)
    summary["recommendations"] = build_recommendations(summary["diagnostics"])
    summary["oracleBrief"] = build_oracle_brief(summary)
    summary["ok"] = bool(summary["buildTiming"].get("present") and summary["cacheSummary"].get("present"))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Scriber GitHub release artifacts for installer speed triage."
    )
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    summary = summarize_release_artifacts(args.artifact_root)
    payload = json.dumps(summary, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote release artifact summary: {args.output}")
    else:
        print(payload)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
