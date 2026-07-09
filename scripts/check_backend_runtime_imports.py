from __future__ import annotations

import importlib
import importlib.metadata
import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.runtime.provider_dependencies import STANDARD_PROVIDER_RUNTIME_IMPORTS


CORE_RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("pyloudnorm", "local Pipecat loudness compatibility dependency"),
    ("onnxruntime", "Silero VAD native runtime dependency"),
    ("onnx_asr", "bundled local ONNX speech-to-text runtime dependency"),
    ("pipecat.frames.frames", "Pipecat startup dependency"),
    ("pipecat.audio.vad.vad_analyzer", "Pipecat VAD startup dependency"),
    ("pipecat.audio.vad.silero", "Silero VAD startup dependency"),
    (
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        "Pipecat 1.5 local Smart Turn startup dependency",
    ),
    ("src.web_api", "backend API entry point"),
)
REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    *CORE_RUNTIME_IMPORTS,
    *STANDARD_PROVIDER_RUNTIME_IMPORTS,
)
REQUIRED_PACKAGE_VERSIONS: tuple[tuple[str, str], ...] = (
    ("pipecat-ai", "1.5.0"),
)


def check_imports(
    imports: Iterable[tuple[str, str]] = REQUIRED_IMPORTS,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for module_name, reason in imports:
        try:
            import_module(module_name)
        except Exception as exc:
            missing.append(
                {
                    "module": module_name,
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return missing


def check_package_versions(
    requirements: Iterable[tuple[str, str]] = REQUIRED_PACKAGE_VERSIONS,
    version_for: Callable[[str], str] = importlib.metadata.version,
) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for package_name, expected_version in requirements:
        try:
            installed_version = version_for(package_name)
        except Exception as exc:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if installed_version != expected_version:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"VersionMismatch: installed {installed_version}",
                }
            )
    return mismatches


def check_runtime_requirements() -> list[dict[str, str]]:
    return [*check_imports(), *check_package_versions()]


def main() -> int:
    missing = check_runtime_requirements()
    result = {"ok": not missing, "missing": missing}
    print(json.dumps(result, separators=(",", ":")))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
