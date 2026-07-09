from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SESSION_FILES = [
    "autoresearch.md",
    "autoresearch.jsonl",
    "autoresearch.config.json",
    "autoresearch.ideas.md",
]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def git_status(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return [f"git status failed: {result.stderr.strip()}"]
    return [line for line in result.stdout.splitlines() if line.strip()]


def is_instrumentation_only_keep(row: dict[str, Any]) -> bool:
    if row.get("status") != "keep":
        return False
    asi = row.get("asi") if isinstance(row.get("asi"), dict) else {}
    return str(asi.get("lane", "")).lower() in {"instrument", "instrumentation"}


def file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(repo_root: Path) -> dict[str, Any]:
    config = read_json(repo_root / "autoresearch.config.json")
    ledger = read_ledger(repo_root / "autoresearch.jsonl")
    latest = ledger[-1] if ledger else {}
    return {
        "config": config,
        "ledger": ledger,
        "latest": latest,
        "gitStatus": git_status(repo_root),
        "goalHash": file_sha256(repo_root / "GOAL.md"),
    }


def current_blocker(state: dict[str, Any]) -> str:
    config = state.get("config") if isinstance(state.get("config"), dict) else {}
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    if runtime.get("currentBlocker"):
        return str(runtime["currentBlocker"])
    if runtime.get("blocker"):
        return str(runtime["blocker"])
    latest = state.get("latest") if isinstance(state.get("latest"), dict) else {}
    if latest.get("blocker"):
        return str(latest["blocker"])
    return ""


def baseline_accepted(config: dict[str, Any]) -> bool:
    baseline = config.get("baseline") if isinstance(config.get("baseline"), dict) else {}
    return baseline.get("accepted") is True or str(baseline.get("status", "")).lower() == "accepted"


def cmd_state(repo_root: Path, compact: bool) -> int:
    state = load_state(repo_root)
    config = state["config"]
    latest = state["latest"]
    blocker = current_blocker(state)
    if compact:
        print(
            json.dumps(
                {
                    "session": config.get("sessionName", ""),
                    "metric": config.get("primaryMetric", ""),
                    "segment": config.get("segment", ""),
                    "latestStatus": latest.get("status", "none"),
                    "blocker": blocker,
                    "ledgerRows": len(state["ledger"]),
                    "dirtyCount": len(state["gitStatus"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"Session: {config.get('sessionName', 'unconfigured')}")
    print(f"Segment: {config.get('segment', 'unknown')}")
    print(f"Primary metric: {config.get('primaryMetric', 'unknown')} ({config.get('direction', 'unknown')})")
    print(f"Ledger rows: {len(state['ledger'])}")
    print(f"Latest status: {latest.get('status', 'none')}")
    print(f"Blocker: {blocker or 'none'}")
    print("Git status:")
    for line in state["gitStatus"] or ["clean"]:
        print(f"  {line}")
    return 0


def cmd_recommend_next(repo_root: Path, compact: bool, checklist: bool) -> int:
    state = load_state(repo_root)
    blocker = current_blocker(state)
    config = state.get("config", {}) if isinstance(state.get("config"), dict) else {}
    latest = state.get("latest", {}) if isinstance(state.get("latest"), dict) else {}
    decision_rows = [
        row
        for row in state.get("ledger", [])
        if isinstance(row, dict)
        and row.get("status") in {"keep", "discard", "crash", "no-op", "blocked"}
    ]
    kept_rows = [
        row
        for row in decision_rows
        if row.get("status") == "keep" and not is_instrumentation_only_keep(row)
    ]
    latest_decision = decision_rows[-1] if decision_rows else latest
    latest_champion = kept_rows[-1] if kept_rows else latest_decision
    latest_decision_index = -1
    if latest_champion is not latest:
        for index, row in enumerate(state.get("ledger", [])):
            if row is latest_champion:
                latest_decision_index = index
                break
    rows_after_decision = (
        state.get("ledger", [])[latest_decision_index + 1 :]
        if latest_decision_index >= 0
        else []
    )
    final_full_local_events = {
        "final_fulllocal_confirmation_overlay_prepare",
        "fulllocal_confirmation_startup_defer",
    }
    final_live_holdout_events = {
        "final_live_holdouts_overlay_prepare",
        "final_live_holdouts_startup_defer",
    }
    has_final_full_local = any(
        isinstance(row, dict) and row.get("event") in final_full_local_events
        for row in rows_after_decision
    )
    has_final_live_holdouts = any(
        isinstance(row, dict) and row.get("event") in final_live_holdout_events
        for row in rows_after_decision
    )
    baseline = config.get("baseline", {}) if isinstance(config.get("baseline"), dict) else {}
    segment = str(config.get("segment", ""))
    latest_decision_metrics = (
        latest_champion.get("metrics", {}) if isinstance(latest_champion.get("metrics"), dict) else {}
    )
    latest_local_wux = latest_decision_metrics.get("local_wux")
    if "provider_text_replay_harness_missing" in blocker:
        safe_next_step = "implement-provider-text-replay-harness"
    elif "overlay_visible_frame_missing" in blocker:
        safe_next_step = "debug-visible-overlay-endpoint-probe"
    elif "app_ux_or_resource_metrics_missing" in blocker:
        safe_next_step = "instrument-app-ux-overlay-timing-and-resource-metrics"
    elif "user_endpoint_probe_failed" in blocker:
        safe_next_step = "debug-external-user-endpoint-probe"
    else:
        if blocker:
            safe_next_step = "resolve-runtime-blocker"
        elif (
            baseline.get("accepted") is True
            and segment.startswith("B4-")
            and latest_champion.get("status") == "keep"
            and isinstance(latest_local_wux, (int, float))
            and latest_local_wux <= 0.75
            and has_final_full_local
            and has_final_live_holdouts
        ):
            safe_next_step = "optimize-remaining-overlay-warm-and-app-ux-targets"
        elif (
            baseline.get("accepted") is True
            and segment.startswith("B4-")
            and latest_champion.get("status") == "keep"
            and isinstance(latest_local_wux, (int, float))
            and latest_local_wux <= 0.75
        ):
            safe_next_step = "run-full-local-and-live-provider-confirmation"
        elif baseline.get("accepted") is True and segment.startswith("B4-"):
            safe_next_step = "start-segment-b4-product-optimization-packet"
        elif baseline.get("accepted") is True:
            safe_next_step = "start-first-local-optimization-packet"
        else:
            safe_next_step = "run-baseline-measurement"
    if safe_next_step == "optimize-remaining-overlay-warm-and-app-ux-targets":
        operator_checklist = [
            "Ensure no unrelated Scriber desktop instance holds the single-instance mutex.",
            "Use the final overlayPrepare plus startup-defer build as current champion; do not rerun local STT paths.",
            "Prioritize remaining warm-overlay product candidates; treat App-UX p95 as observer-bound unless a trace proves product UI churn.",
            "Run .\\doctor.ps1 -CheckBenchmark -Explain and .\\next.ps1 -Suite FastLocal after a candidate, then log ASI before continuing.",
        ]
    elif safe_next_step == "run-full-local-and-live-provider-confirmation":
        operator_checklist = [
            "Ensure no unrelated Scriber desktop instance holds the single-instance mutex.",
            "Run .\\next.ps1 -Suite FullLocal to broaden local distribution evidence for the kept B4 change.",
            "Run .\\next.ps1 -Suite LiveMicrosoft and .\\next.ps1 -Suite LiveSoniox only with real credentials, microphone access, and network available.",
            "Log each confirmation or blocker with ASI; do not claim product-grade completion without Microsoft/Soniox live holdout.",
        ]
    elif baseline.get("accepted") is True and segment.startswith("B4-"):
        operator_checklist = [
            "Ensure no unrelated Scriber desktop instance holds the single-instance mutex.",
            "Use benchmarks\\results\\baseline.json as the Segment B4 comparator; do not compare against pre-B4 provider-tail packets.",
            "Run .\\doctor.ps1 -CheckBenchmark -Explain before measuring a candidate.",
            "Run one focused .\\next.ps1 -Suite FastLocal after a candidate, then log keep/discard/measure with ASI before the next experiment.",
        ]
    else:
        operator_checklist = [
            "Ensure no unrelated Scriber desktop instance holds the single-instance mutex.",
            "Run .\\doctor.ps1 -CheckBenchmark -Explain.",
            "Run .\\next.ps1 -Suite FastLocal only after doctor is clean.",
            "Log the first valid unchanged package as measure: Baseline measurement.",
        ]
    recommendation = {
        "safeNextStep": safe_next_step,
        "blocker": blocker,
        "latestEvent": latest.get("event", ""),
        "operatorChecklist": operator_checklist,
    }
    if compact:
        print(json.dumps(recommendation, ensure_ascii=False))
    else:
        print(f"Safe next step: {recommendation['safeNextStep']}")
        if blocker:
            print(f"Blocker: {blocker}")
        if checklist:
            print("Operator checklist:")
            for item in recommendation["operatorChecklist"]:
                print(f"- {item}")
    return 0


def cmd_onboarding(repo_root: Path, compact: bool) -> int:
    state = load_state(repo_root)
    config = state["config"]
    payload = {
        "sessionFiles": {name: (repo_root / name).exists() for name in SESSION_FILES},
        "sessionName": config.get("sessionName", ""),
        "benchmark": config.get("benchmarkCommand", ""),
        "checks": config.get("checksCommand", ""),
        "blocker": current_blocker(state),
        "goalHash": state["goalHash"],
    }
    if compact:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_finalize(repo_root: Path) -> int:
    state = load_state(repo_root)
    config = state["config"]
    ledger = state["ledger"]
    keeps = [row for row in ledger if row.get("status") == "keep"]
    blocker = current_blocker(state)
    baseline_ok = baseline_accepted(config)
    preview = {
        "readiness": "blocked" if blocker else "not_ready",
        "blocker": blocker or ("" if baseline_ok else "No local baseline package has been accepted yet."),
        "baselineAccepted": baseline_ok,
        "dirtyTree": state["gitStatus"],
        "acceptedKeeps": keeps,
        "claimCoverage": {
            "overlay": "missing",
            "microsoftAsync": "missing",
            "sonioxRealtime": "missing",
            "appUx": "missing",
            "generalSafety": "missing",
        },
        "evidenceStatus": "experimental",
        "productGradeAllowed": False,
    }
    print(json.dumps(preview, indent=2, ensure_ascii=False))
    return 1 if blocker else 0


def cmd_next(repo_root: Path, suite: str) -> int:
    started = utc_now()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "autoresearch.ps1"),
            "-Suite",
            suite,
        ],
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = {
        "schemaVersion": 1,
        "suite": suite,
        "startedAtUtc": started,
        "finishedAtUtc": utc_now(),
        "exitCode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    out_dir = repo_root / ".git" / "autoresearch"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "last-run.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scriber Windows autoresearch state helpers.")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state-report")
    sub.add_parser("state-compact")
    rec = sub.add_parser("recommend-next")
    rec.add_argument("--compact", action="store_true")
    rec.add_argument("--operator-checklist", action="store_true")
    onboard = sub.add_parser("onboarding-packet")
    onboard.add_argument("--compact", action="store_true")
    sub.add_parser("finalize-preview")
    nxt = sub.add_parser("next")
    nxt.add_argument("--suite", default="FastLocal")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if args.command == "state-report":
        return cmd_state(repo_root, compact=False)
    if args.command == "state-compact":
        return cmd_state(repo_root, compact=True)
    if args.command == "recommend-next":
        return cmd_recommend_next(repo_root, compact=args.compact, checklist=args.operator_checklist)
    if args.command == "onboarding-packet":
        return cmd_onboarding(repo_root, compact=args.compact)
    if args.command == "finalize-preview":
        return cmd_finalize(repo_root)
    if args.command == "next":
        return cmd_next(repo_root, suite=args.suite)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
