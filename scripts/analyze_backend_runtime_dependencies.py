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
    "awsSdk": {
        "paths": ("boto3", "botocore", "s3transfer", "aioboto3", "aiobotocore"),
        "requiredPaths": (),
        "disallowedPaths": ("boto3", "botocore", "s3transfer", "aioboto3", "aiobotocore"),
        "expectedPresent": False,
        "reason": "AWS Transcribe provider is not part of the standard app",
    },
    "pythonGuiRuntime": {
        "paths": ("PySide6", "customtkinter", "tkinter", "_tkinter.pyd", "pystray"),
        "requiredPaths": (),
        "disallowedPaths": ("PySide6", "customtkinter", "tkinter", "_tkinter.pyd", "pystray"),
        "expectedPresent": False,
        "reason": "Installed desktop overlay and shell UI are owned by Tauri/Rust",
    },
    "unusedProviderSdks": {
        "paths": (
            "google/generativeai",
            "google/ai/generativelanguage",
            "google/cloud/texttospeech",
            "googleapiclient",
            "azure/cognitiveservices/speech",
        ),
        "requiredPaths": (),
        "disallowedPaths": (
            "google/generativeai",
            "google/ai/generativelanguage",
            "google/cloud/texttospeech",
            "googleapiclient",
            "azure/cognitiveservices/speech",
        ),
        "expectedPresent": False,
        "reason": "Standard sidecar uses direct HTTP or explicit provider SDKs instead of unused provider SDKs",
    },
}

COMPONENT_GROUPS: dict[str, dict[str, Any]] = {
    "backend": {
        "paths": (".",),
        "requiredPaths": (),
        "reason": "Complete frozen backend sidecar footprint",
    },
    "internal": {
        "paths": ("_internal",),
        "requiredPaths": ("_internal",),
        "reason": "PyInstaller bundled Python runtime and dependencies",
    },
    "mediaTools": {
        "paths": ("tools/ffmpeg",),
        "requiredPaths": (
            "tools/ffmpeg",
            "tools/ffmpeg/ffmpeg.exe",
            "tools/ffmpeg/ffprobe.exe",
        ),
        "reason": "Bundled ffmpeg and ffprobe required for media preparation",
    },
    "pyside6": {
        "paths": ("_internal/PySide6",),
        "requiredPaths": (),
        "disallowedPaths": ("_internal/PySide6",),
        "reason": "Disallowed legacy overlay runtime; Tauri WebView owns the recording overlay",
    },
    "pythonGuiRuntime": {
        "paths": ("_internal/customtkinter", "_internal/tkinter", "_internal/_tkinter.pyd", "_internal/pystray"),
        "requiredPaths": (),
        "disallowedPaths": ("_internal/customtkinter", "_internal/tkinter", "_internal/_tkinter.pyd", "_internal/pystray"),
        "reason": "Disallowed legacy Tk runtime; installed desktop UI is Tauri-owned",
    },
    "googleGrpc": {
        "paths": ("_internal/google", "_internal/grpc"),
        "requiredPaths": ("_internal/grpc",),
        "reason": "Google Cloud STT gRPC runtime support; Google Python modules may be embedded in the PYZ archive",
    },
    "legacyExportStack": {
        "paths": (
            "_internal/PIL",
            "_internal/docx",
            "_internal/lxml",
            "_internal/reportlab",
        ),
        "requiredPaths": (),
        "disallowedPaths": (
            "_internal/PIL",
            "_internal/docx",
            "_internal/lxml",
            "_internal/reportlab",
        ),
        "reason": "Disallowed legacy PDF/DOCX dependencies; text export is standard-library-only",
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


def summarize_disallowed_sidecar_path(
    path: Path,
    *,
    sidecar_dir: Path,
    top_files_limit: int,
) -> dict[str, Any]:
    if path.is_file():
        return summarize_file(path, relative_to=sidecar_dir)
    return summarize_existing_path(
        path,
        relative_to=sidecar_dir,
        top_files_limit=top_files_limit,
    )


def summarize_component(
    name: str,
    group: dict[str, Any],
    *,
    sidecar_dir: Path,
    top_files_limit: int,
    max_mb: float | None,
) -> dict[str, Any]:
    path_entries = []
    all_files: list[Path] = []
    for relative in group["paths"]:
        path = sidecar_dir / relative
        if path.exists():
            path_entries.append(
                summarize_existing_path(
                    path,
                    relative_to=sidecar_dir,
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
        if not (sidecar_dir / relative).exists()
    ]
    disallowed_paths = [
        summarize_disallowed_sidecar_path(
            sidecar_dir / relative,
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
        )
        for relative in group.get("disallowedPaths", ())
        if (sidecar_dir / relative).exists()
    ]
    total_mb = size_mb(total_bytes)
    return {
        "name": name,
        "reason": group["reason"],
        "totalBytes": total_bytes,
        "totalMb": total_mb,
        "fileCount": len(all_files),
        "missingRequiredPaths": missing_required,
        "disallowedPaths": disallowed_paths,
        "budget": evaluate_budget(total_mb, max_mb),
        "paths": path_entries,
        "topFiles": [
            summarize_file(item, relative_to=sidecar_dir) for item in top_files
        ],
    }


def build_report(
    sidecar_dir: Path,
    *,
    top_files_limit: int = 20,
    max_scipy_mb: float | None = None,
    max_onnxruntime_mb: float | None = None,
    max_total_mb: float | None = None,
    max_backend_mb: float | None = None,
    max_internal_mb: float | None = None,
    max_media_tools_mb: float | None = None,
    max_pyside6_mb: float | None = None,
    max_google_grpc_mb: float | None = None,
    max_pillow_mb: float | None = None,
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
        "awsSdk": summarize_dependency(
            "awsSdk",
            DEPENDENCY_GROUPS["awsSdk"],
            internal_dir=internal_dir,
            top_files_limit=top_files_limit,
            max_mb=None,
        ),
        "pythonGuiRuntime": summarize_dependency(
            "pythonGuiRuntime",
            DEPENDENCY_GROUPS["pythonGuiRuntime"],
            internal_dir=internal_dir,
            top_files_limit=top_files_limit,
            max_mb=None,
        ),
        "unusedProviderSdks": summarize_dependency(
            "unusedProviderSdks",
            DEPENDENCY_GROUPS["unusedProviderSdks"],
            internal_dir=internal_dir,
            top_files_limit=top_files_limit,
            max_mb=None,
        ),
    }
    components = {
        "backend": summarize_component(
            "backend",
            COMPONENT_GROUPS["backend"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_backend_mb,
        ),
        "internal": summarize_component(
            "internal",
            COMPONENT_GROUPS["internal"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_internal_mb,
        ),
        "mediaTools": summarize_component(
            "mediaTools",
            COMPONENT_GROUPS["mediaTools"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_media_tools_mb,
        ),
        "pyside6": summarize_component(
            "pyside6",
            COMPONENT_GROUPS["pyside6"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_pyside6_mb,
        ),
        "pythonGuiRuntime": summarize_component(
            "pythonGuiRuntime",
            COMPONENT_GROUPS["pythonGuiRuntime"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=None,
        ),
        "googleGrpc": summarize_component(
            "googleGrpc",
            COMPONENT_GROUPS["googleGrpc"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_google_grpc_mb,
        ),
        "legacyExportStack": summarize_component(
            "legacyExportStack",
            COMPONENT_GROUPS["legacyExportStack"],
            sidecar_dir=sidecar_dir,
            top_files_limit=top_files_limit,
            max_mb=max_pillow_mb,
        ),
    }
    dependency_total_bytes = sum(item["totalBytes"] for item in dependencies.values())
    total_bytes = components["backend"]["totalBytes"]
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
    component_missing = [
        f"{name}:{relative}"
        for name, item in components.items()
        for relative in item["missingRequiredPaths"]
    ]
    component_budget_failures = [
        name
        for name, item in components.items()
        if item["budget"]["withinBudget"] is False
    ]
    component_disallowed = [
        f"{name}:{item['path']}"
        for name, component in components.items()
        for item in component["disallowedPaths"]
    ]

    return {
        "apiVersion": "1",
        "ok": sidecar_dir.is_dir()
        and internal_dir.is_dir()
        and not missing
        and not component_missing
        and not component_disallowed
        and not disallowed
        and not unexpected_present
        and not budget_failures
        and not component_budget_failures,
        "generatedAt": _utc_now(),
        "sidecarDir": str(sidecar_dir),
        "internalDir": str(internal_dir),
        "summary": {
            "totalBytes": total_bytes,
            "totalMb": total_mb,
            "dependencyTotalBytes": dependency_total_bytes,
            "dependencyTotalMb": size_mb(dependency_total_bytes),
            "missingRequiredPaths": missing,
            "disallowedPaths": disallowed,
            "unexpectedPresentDependencies": unexpected_present,
            "budgetFailures": budget_failures,
            "componentMissingRequiredPaths": component_missing,
            "componentDisallowedPaths": component_disallowed,
            "componentBudgetFailures": component_budget_failures,
        },
        "budgets": {
            "scipy": dependencies["scipy"]["budget"],
            "onnxruntime": dependencies["onnxruntime"]["budget"],
            "total": total_budget,
            "components": {
                name: item["budget"] for name, item in components.items()
            },
        },
        "dependencies": dependencies,
        "components": components,
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
    parser.add_argument("--max-backend-mb", type=float, default=0.0)
    parser.add_argument("--max-internal-mb", type=float, default=0.0)
    parser.add_argument("--max-media-tools-mb", type=float, default=0.0)
    parser.add_argument("--max-pyside6-mb", type=float, default=0.0)
    parser.add_argument("--max-google-grpc-mb", type=float, default=0.0)
    parser.add_argument("--max-pillow-mb", type=float, default=0.0)
    args = parser.parse_args(argv)

    report = build_report(
        args.sidecar_dir,
        top_files_limit=args.top_files_limit,
        max_scipy_mb=optional_positive(args.max_scipy_mb),
        max_onnxruntime_mb=optional_positive(args.max_onnxruntime_mb),
        max_total_mb=optional_positive(args.max_total_mb),
        max_backend_mb=optional_positive(args.max_backend_mb),
        max_internal_mb=optional_positive(args.max_internal_mb),
        max_media_tools_mb=optional_positive(args.max_media_tools_mb),
        max_pyside6_mb=optional_positive(args.max_pyside6_mb),
        max_google_grpc_mb=optional_positive(args.max_google_grpc_mb),
        max_pillow_mb=optional_positive(args.max_pillow_mb),
    )
    output = args.output.expanduser().resolve()
    write_report(report, output)
    print(json.dumps({"ok": report["ok"], "output": str(output)}, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
