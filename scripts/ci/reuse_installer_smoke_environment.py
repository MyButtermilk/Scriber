"""Reuse a validated quality venv for installer probes, without rebuilding it.

The media smoke imports the production File, MAI, YouTube and web API modules.
It therefore needs the full locked Python dependency closure, not just aiohttp.
An existing quality cache supplies that closure; on any miss the workflow keeps
its established release-venv/wheelhouse fallback. This never supplies or replaces
the frozen backend's packaged runtime.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.ci import prepare_python_quality_environment as quality

ROOT = Path(__file__).resolve().parents[2]
SMOKE_IMPORT_PROBE = "; ".join(
    (
        "from src.api.file_transcription_routes import extract_audio_from_video, maybe_compress_audio_upload",
        "from src.azure_mai_stt import azure_mai_content_type, prepared_azure_mai_audio_file",
        "from src.runtime.media_tools import find_media_tool, require_media_tool",
        "from src.web_api import _probe_media_duration_seconds",
        "from src.youtube_download import _ensure_audio_only_file",
        "from scripts import smoke_diarization_worker_resource, smoke_local_polishing_runtime",
        "from scripts import smoke_installed_transcription_workflows",
    )
)


def validate_restored_environment(root: Path) -> dict[str, Any]:
    try:
        identity = quality.environment_identity(root)
        quality.validate_environment(root, identity)
        subprocess.run(
            [str(root / "venv" / "Scripts" / "python.exe"), "-c", SMOKE_IMPORT_PROBE],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Other smoke tools discover venv before .venv. Never leave a rejected
        # cache around for them to accidentally select after the fallback.
        quality._remove_environment(root)
        return {"usable": False, "reason": f"{type(error).__name__}: {error}"}
    return {"usable": True, "reason": "exact-quality-environment-and-smoke-imports-validated"}


def select_environment(
    *,
    official: bool,
    refresh: bool,
    cold_product: bool,
    backend_sidecar: bool,
    backend_runtime: bool,
    quality_usable: bool,
) -> str:
    backend_attested = cold_product or backend_sidecar or backend_runtime
    if refresh or not backend_attested:
        return "release"
    if official:
        return "quality" if quality_usable else "release"
    return "base"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "select"))
    for name in ("official", "refresh", "cold-product", "backend-sidecar", "backend-runtime", "quality-usable"):
        parser.add_argument(f"--{name}", choices=("true", "false"), default="false")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operation == "validate":
        result = validate_restored_environment(ROOT)
        outputs = {"usable": str(result["usable"]).lower()}
    else:
        mode = select_environment(
            official=args.official == "true",
            refresh=args.refresh == "true",
            cold_product=args.cold_product == "true",
            backend_sidecar=args.backend_sidecar == "true",
            backend_runtime=args.backend_runtime == "true",
            quality_usable=args.quality_usable == "true",
        )
        executable = (
            Path(sys.executable)
            if mode == "base"
            else ROOT / ("venv" if mode == "quality" else ".venv") / "Scripts" / "python.exe"
        )
        if not executable.is_file():
            raise SystemExit(f"Selected {mode} Python environment is missing: {executable}")
        result = {"mode": mode, "python": str(executable.resolve())}
        outputs = {"mode": mode, "python": result["python"]}
        if args.github_path:
            with args.github_path.open("a", encoding="utf-8") as target:
                target.write(str(executable.resolve().parent) + "\n")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as target:
            for name, value in outputs.items():
                target.write(f"{name}={value}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
