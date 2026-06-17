from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "Frontend"
    / "src-tauri"
    / "target"
    / "release"
    / "release-metadata"
    / "size-report.json"
)

BYTES_PER_MIB = 1024 * 1024


def size_mb(size_bytes: int) -> float:
    return round(size_bytes / BYTES_PER_MIB, 2)


def summarize_file(path: Path) -> dict:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "sizeBytes": stat.st_size,
        "sizeMb": size_mb(stat.st_size),
    }


def summarize_directory(root: Path, *, top_files_limit: int) -> dict:
    files = [path for path in root.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    top_files = sorted(files, key=lambda path: path.stat().st_size, reverse=True)[
        :top_files_limit
    ]
    return {
        "path": str(root),
        "fileCount": len(files),
        "totalBytes": total_bytes,
        "totalMb": size_mb(total_bytes),
        "topFiles": [
            {
                "path": str(path.relative_to(root)),
                "sizeBytes": path.stat().st_size,
                "sizeMb": size_mb(path.stat().st_size),
            }
            for path in top_files
        ],
    }


def installed_app_from_smoke_report(path: Path, *, max_installed_mb: float | None) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    install_size = payload.get("installSize")
    if not isinstance(install_size, dict):
        raise ValueError(f"Installed smoke report has no installSize object: {path}")

    installed_app = dict(install_size)
    installed_app["budget"] = evaluate_budget(
        installed_app.get("totalMb"),
        max_installed_mb,
    )
    return installed_app


def evaluate_budget(value_mb: float | None, max_mb: float | None) -> dict:
    if max_mb is None or max_mb <= 0:
        return {"maxMb": None, "withinBudget": None}
    if value_mb is None:
        return {"maxMb": max_mb, "withinBudget": None}
    return {"maxMb": max_mb, "withinBudget": value_mb <= max_mb}


def build_report(
    artifacts: list[Path],
    *,
    install_dir: Path | None = None,
    installed_smoke_report: Path | None = None,
    top_roots: list[Path] | None = None,
    max_installer_mb: float | None = 220.0,
    max_installed_mb: float | None = None,
    top_files_limit: int = 20,
) -> dict:
    if not artifacts:
        raise FileNotFoundError("No release artifacts were provided.")

    missing = [str(path) for path in artifacts if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing release artifacts: {', '.join(missing)}")

    artifact_entries = [summarize_file(path) for path in artifacts]
    largest_artifact_mb = max(entry["sizeMb"] for entry in artifact_entries)
    installer_budget = evaluate_budget(largest_artifact_mb, max_installer_mb)

    installed_app = None
    if installed_smoke_report is not None:
        if not installed_smoke_report.is_file():
            raise FileNotFoundError(
                f"Installed smoke report was not found: {installed_smoke_report}"
            )
        installed_app = installed_app_from_smoke_report(
            installed_smoke_report,
            max_installed_mb=max_installed_mb,
        )
    elif install_dir is not None:
        if not install_dir.is_dir():
            raise FileNotFoundError(f"Install directory was not found: {install_dir}")
        installed_app = summarize_directory(install_dir, top_files_limit=top_files_limit)
        installed_app["budget"] = evaluate_budget(
            installed_app["totalMb"], max_installed_mb
        )

    roots = []
    seen_roots: set[Path] = set()
    for root in top_roots or []:
        if not root.exists() or not root.is_dir():
            continue
        resolved = root.resolve()
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        roots.append(summarize_directory(resolved, top_files_limit=top_files_limit))

    budget_results = [installer_budget["withinBudget"]]
    if installed_app is not None:
        budget_results.append(installed_app["budget"]["withinBudget"])
    failed_budgets = [value for value in budget_results if value is False]

    return {
        "ok": not failed_budgets,
        "generatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "budgets": {
            "installer": installer_budget,
            "installedApp": evaluate_budget(
                installed_app["totalMb"] if installed_app else None,
                max_installed_mb,
            ),
        },
        "artifactCount": len(artifact_entries),
        "artifacts": artifact_entries,
        "largestArtifactMb": largest_artifact_mb,
        "installedApp": installed_app,
        "roots": roots,
    }


def write_report(report: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and gate Scriber release size metadata.")
    parser.add_argument("--artifact", action="append", default=[], help="Release artifact path.")
    parser.add_argument("--install-dir", default="", help="Optional installed app directory to measure.")
    parser.add_argument(
        "--installed-smoke-report",
        default="",
        help="Optional smoke_windows_installer JSON report containing installSize.",
    )
    parser.add_argument("--top-root", action="append", default=[], help="Directory whose largest files should be reported.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON report output path.")
    parser.add_argument("--max-installer-mb", type=float, default=220.0)
    parser.add_argument("--max-installed-mb", type=float, default=0.0)
    parser.add_argument("--top-files-limit", type=int, default=20)
    args = parser.parse_args(argv)

    artifacts = [Path(item).expanduser().resolve() for item in args.artifact]
    install_dir = Path(args.install_dir).expanduser().resolve() if args.install_dir else None
    installed_smoke_report = (
        Path(args.installed_smoke_report).expanduser().resolve()
        if args.installed_smoke_report
        else None
    )
    top_roots = [Path(item).expanduser().resolve() for item in args.top_root]

    report = build_report(
        artifacts,
        install_dir=install_dir,
        installed_smoke_report=installed_smoke_report,
        top_roots=top_roots,
        max_installer_mb=args.max_installer_mb,
        max_installed_mb=args.max_installed_mb if args.max_installed_mb > 0 else None,
        top_files_limit=args.top_files_limit,
    )
    output = Path(args.output).expanduser().resolve()
    write_report(report, output)
    print(json.dumps({"ok": report["ok"], "output": str(output)}, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
