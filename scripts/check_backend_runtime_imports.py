from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("scipy", "Pipecat loudness/VAD dependency"),
    ("scipy.signal", "pyloudnorm filter dependency"),
    ("pyloudnorm", "Pipecat audio utility dependency"),
    ("pipecat.frames.frames", "Pipecat startup dependency"),
    ("pipecat.audio.vad.vad_analyzer", "Pipecat VAD startup dependency"),
    ("src.web_api", "backend API entry point"),
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


def main() -> int:
    missing = check_imports()
    result = {"ok": not missing, "missing": missing}
    print(json.dumps(result, separators=(",", ":")))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
