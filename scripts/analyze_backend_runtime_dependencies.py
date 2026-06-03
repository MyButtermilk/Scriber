from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIDECAR_DIR = (
    REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / "backend"
)
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "Frontend"
    / "src-tauri"
    / "target"
    / "release"
    / "release-metadata"
    / "runtime-dependency-footprint.json"
)
BYTES_PER_MIB = 1024 * 1024

DEPENDENCY_GROUPS: dict[str, dict[str, Any]] = {
    "scipy": {
        "paths": ("scipy", "scipy.libs"),
        "requiredPaths": (),
        "disallowedPaths": ("scipy", "scipy.libs"),
        "expectedPresent": False,
        "reason": "Intentionally absent: local pyloudnorm compatibility avoids SciPy for Pipecat loudness",
    },
    "onnxruntime": {
        "paths": ("onnxruntime", "onnxruntime.libs"),
        "requiredPaths": (
            "onnxruntime",
            "onnxruntime/capi",
            "onnxruntime/capi/onnxruntime.dll",
            "onnxruntime/capi/onnxruntime_pybind11_state.pyd",
        ),
        "disallowedPaths": ("onnxruntime/datasets", "onnxruntime/tools"),
        "expectedPresent": True,
        "reason": "Pipecat Silero VAD native runtime",
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def size_mb(size_bytes: int) -> float:
    return round(size_bytes / BYTES_PER_MIB, 2)


def iter_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    if path.is_file():
        return [path]
    return [item for item in path.rglob("*") if item.is_file()]


def summarize_file(path: Path, *, relative_to: Path) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    return {
        "path": str(path.relative_to(relative_to)),
        "sizeBytes": size_bytes,
        "sizeMb": size_mb(size_bytes),
    }


def summarize_existing_path(path: Path, *, relative_to: Path, top_files_limit: int) -> dict[str, Any]:
    files = iter_files(path)
    total_bytes = sum(item.stat().st_size for item in files)
    top_files = sorted(files, key=lambda item: item.stat().st_size, reverse=True)[
        :top_files_limit
    ]
    return {
        "path": str(path.relative_to(relative_to)),
        "exists": path.exists(),
        "fileCount": len(files),
        "totalBytes": total_bytes,
        "totalMb": size_mb(total_bytes),
        "topFiles": [
            summarize_file(item, relative_to=relative_to) for item in top_files
        ],
    }


def evaluate_budget(value_mb: float, max_mb: float | None) -> dict[str, Any]:
    if max_mb is None or max_mb <= 0:
        return {"maxMb": None, "withinBudget": None}
    return {"maxMb": max_mb, "withinBudget": value_mb <= max_mb}


def summarize_dependency(
    name: str,
    group: dict[str, Any],
    *,
    internal_dir: Path,
    top_files_limit: int,
    max_mb: float | None,
) -> dict[str, Any]:
    path_entries = []
    all_files: list[Path] = []
    for relative in group["paths"]:
        path = internal_dir / relative
        if path.exists():
            path_entries.append(
                summarize_existing_path(
                    path,
                    relative_to=internal_dir,
                    top_files_limit=top_files_limit,
                )
            )
            all_files.extend(iter_files(path))
        else:
            path_entries.append(
                {
                    "path": relative,
                    "exists": False,
                    "fileCount": 0,
                    "totalBytes": 0,
                    "totalMb": 0,
                    "topFiles": [],
                }
            )

    total_bytes = sum(item.stat().st_size for item in all_files)
    top_files = sorted(all_files, key=lambda item: item.stat().st_size, reverse=True)[
        :top_files_limit
    ]
    missing_required = [
        relative
        for relative in group["requiredPaths"]
        if not (internal_dir / relative).exists()
    ]
    disallowed_paths = [
        summarize_existing_path(
            internal_dir / relative,
            relative_to=internal_dir,
            top_files_limit=top_files_limit,
        )
        for relative in group.get("disallowedPaths", ())
        if (internal_dir / relative).exists()
    ]
    total_mb = size_mb(total_bytes)
    budget = evaluate_budget(total_mb, max_mb)
    expected_present = bool(group.get("expectedPresent", True))
    unexpected_present = not expected_present and total_bytes > 0
    return {
        "name": name,
        "reason": group["reason"],
        "expectedPresent": expected_present,
        "unexpectedPresent": unexpected_present,
        "totalBytes": total_bytes,
        "totalMb": total_mb,
        "fileCount": len(all_files),
        "missingRequiredPaths": missing_required,
        "disallowedPaths": disallowed_paths,
        "budget": budget,
        "paths": path_entries,
        "topFiles": [
            summarize_file(item, relative_to=internal_dir) for item in top_files
        ],
    }


def build_report(
    sidecar_dir: Path,
    *,
    top_files_limit: int = 20,
    max_scipy_mb: float | None = None,
    max_onnxruntime_mb: float | None = None,
    max_total_mb: float | None = None,
) -> dict[str, Any]:
    sidecar_dir = sidecar_dir.expanduser().resolve()
    internal_dir = sidecar_dir / "_internal"
    dependencies = {
        "scipy": summarize_dependency(
            "scipy",
            DEPENDENCY_GROUPS["scipy"],
            internal_dir=internal_dir,
            top_files_limit=top_files_limit,
            max_mb=max_scipy_mb,
        ),
        "onnxruntime": summarize_dependency(
            "onnxruntime",
            DEPENDENCY_GROUPS["onnxruntime"],
            internal_dir=internal_dir,
            top_files_limit=top_files_limit,
            max_mb=max_onnxruntime_mb,
        ),
    }
    total_bytes = sum(item["totalBytes"] for item in dependencies.values())
    total_mb = size_mb(total_bytes)
    total_budget = evaluate_budget(total_mb, max_total_mb)
    missing = [
        f"{name}:{relative}"
        for name, item in dependencies.items()
        for relative in item["missingRequiredPaths"]
    ]
    budget_failures = [
        name
        for name, item in dependencies.items()
        if item["budget"]["withinBudget"] is False
    ]
    disallowed = [
        f"{name}:{item['path']}"
        for name, dependency in dependencies.items()
        for item in dependency["disallowedPaths"]
    ]
    unexpected_present = [
        name for name, item in dependencies.items() if item["unexpectedPresent"]
    ]
    if total_budget["withinBudget"] is False:
        budget_failures.append("total")

    return {
        "apiVersion": "1",
        "ok": sidecar_dir.is_dir()
        and internal_dir.is_dir()
        and not missing
        and not disallowed
        and not unexpected_present
        and not budget_failures,
        "generatedAt": _utc_now(),
        "sidecarDir": str(sidecar_dir),
        "internalDir": str(internal_dir),
        "summary": {
            "totalBytes": total_bytes,
            "totalMb": total_mb,
            "missingRequiredPaths": missing,
            "disallowedPaths": disallowed,
            "unexpectedPresentDependencies": unexpected_present,
            "budgetFailures": budget_failures,
        },
        "budgets": {
            "scipy": dependencies["scipy"]["budget"],
            "onnxruntime": dependencies["onnxruntime"]["budget"],
            "total": total_budget,
        },
        "dependencies": dependencies,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def optional_positive(value: float) -> float | None:
    return value if value > 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report and optionally gate the frozen backend sidecar footprint for "
            "expected-absent SciPy and required ONNXRuntime."
        )
    )
    parser.add_argument("--sidecar-dir", type=Path, default=DEFAULT_SIDECAR_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-files-limit", type=int, default=20)
    parser.add_argument("--max-scipy-mb", type=float, default=0.0)
    parser.add_argument("--max-onnxruntime-mb", type=float, default=0.0)
    parser.add_argument("--max-total-mb", type=float, default=0.0)
    args = parser.parse_args(argv)

    report = build_report(
        args.sidecar_dir,
        top_files_limit=args.top_files_limit,
        max_scipy_mb=optional_positive(args.max_scipy_mb),
        max_onnxruntime_mb=optional_positive(args.max_onnxruntime_mb),
        max_total_mb=optional_positive(args.max_total_mb),
    )
    output = args.output.expanduser().resolve()
    write_report(report, output)
    print(json.dumps({"ok": report["ok"], "output": str(output)}, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
