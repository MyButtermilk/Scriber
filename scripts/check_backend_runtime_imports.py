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
    ("yt_dlp", "YouTube media extraction dependency"),
    ("yt_dlp_ejs", "YouTube external JavaScript challenge scripts"),
    ("pipecat.frames.frames", "Pipecat startup dependency"),
    ("pipecat.pipeline.pipeline", "Pipecat pipeline graph dependency"),
    ("pipecat.pipeline.task", "Pipecat pipeline task dependency"),
    ("pipecat.pipeline.runner", "Pipecat pipeline runner dependency"),
    ("pipecat.processors.frame_processor", "Pipecat frame processor dependency"),
    ("pipecat.services.ai_service", "Pipecat AI service dependency"),
    ("pipecat.services.settings", "Pipecat STT settings dependency"),
    ("pipecat.services.stt_service", "Pipecat STT base dependency"),
    ("pipecat.transcriptions.language", "Pipecat language dependency"),
    ("pipecat.transports.base_input", "Pipecat audio input transport dependency"),
    ("pipecat.transports.base_transport", "Pipecat transport dependency"),
    ("pipecat.utils.time", "Pipecat timestamp dependency"),
    ("pipecat.audio.vad.vad_analyzer", "Pipecat VAD startup dependency"),
    ("pipecat.audio.vad.silero", "Silero VAD startup dependency"),
    ("pipecat.processors.audio.vad_processor", "Pipecat VAD processor dependency"),
    ("pipecat.turns.user_start", "Pipecat user-turn start dependency"),
    ("pipecat.turns.user_stop", "Pipecat user-turn stop dependency"),
    ("pipecat.turns.user_turn_processor", "Pipecat user-turn processor dependency"),
    ("pipecat.turns.user_turn_strategies", "Pipecat user-turn strategy dependency"),
    (
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        "Pipecat 1.5 local Smart Turn startup dependency",
    ),
    ("src.microphone", "live microphone application runtime"),
    ("src.audio_file_input", "file audio application runtime"),
    ("src.pipeline", "transcription pipeline application runtime"),
    ("src.web_api", "backend API entry point"),
)
REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    *CORE_RUNTIME_IMPORTS,
    *STANDARD_PROVIDER_RUNTIME_IMPORTS,
)
REQUIRED_PACKAGE_VERSIONS: tuple[tuple[str, str], ...] = (
    ("pipecat-ai", "1.5.0"),
    ("yt-dlp", "2026.7.4"),
    ("yt-dlp-ejs", "0.8.0"),
)

SENTENCE_SEGMENTATION_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "english_abbreviation",
        "Dr. Smith arrived. The meeting started.",
        "Dr. Smith arrived.",
    ),
    (
        "decimal",
        "The value is 3.14. Next item.",
        "The value is 3.14.",
    ),
    (
        "german_abbreviation",
        "Dr. Müller kommt. Danach geht es weiter.",
        "Dr. Müller kommt.",
    ),
    ("cjk_punctuation", "これは文です。次です", "これは文です。"),
    ("arabic_punctuation", "مرحبا؟التالي", "مرحبا؟"),
)


def check_sentence_segmentation(
    *,
    import_module: Callable[[str], object] = importlib.import_module,
    frozen: bool | None = None,
    frozen_root: Path | None = None,
) -> list[dict[str, str]]:
    """Exercise the bundled NLTK data through Pipecat without network fallback."""

    nltk_module: object | None = None
    original_download: object | None = None
    try:
        nltk_module = import_module("nltk")
        nltk_data = getattr(nltk_module, "data")
        nltk_data_path = getattr(nltk_data, "path")
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if is_frozen:
            root = frozen_root
            if root is None:
                raw_root = getattr(sys, "_MEIPASS", None)
                if not isinstance(raw_root, (str, bytes)):
                    raise RuntimeError("frozen runtime has no bundled data root")
                root = Path(raw_root)
            bundled_nltk_data = root / "nltk_data"
            if not bundled_nltk_data.is_dir():
                raise RuntimeError("bundled nltk_data directory is missing")
            # Replace, rather than prepend to, the search list so a developer or
            # user cache can never make an incomplete frozen bundle look valid.
            nltk_data_path[:] = [str(bundled_nltk_data)]

        original_download = getattr(nltk_module, "download")
        if not callable(original_download):
            raise RuntimeError("NLTK downloader boundary is unavailable")

        def reject_download(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("NLTK download fallback was attempted")

        setattr(nltk_module, "download", reject_download)
        pipecat_string = import_module("pipecat.utils.string")
        match_endofsentence = getattr(pipecat_string, "match_endofsentence")
        if not callable(match_endofsentence):
            raise RuntimeError("Pipecat sentence segmenter is unavailable")

        for label, text, expected_first_sentence in SENTENCE_SEGMENTATION_PROBES:
            expected_end = len(expected_first_sentence)
            actual_end = match_endofsentence(text)
            if actual_end != expected_end:
                raise RuntimeError(
                    f"sentence segmentation probe {label} returned {actual_end!r}; "
                    f"expected {expected_end}"
                )
    except Exception as exc:
        return [
            {
                "module": "pipecat.utils.string.match_endofsentence",
                "reason": "bundled NLTK/Pipecat sentence segmentation runtime",
                # Keep frozen diagnostics independent of install paths and user data.
                "error": f"{type(exc).__name__}: sentence segmentation probe failed",
            }
        ]
    finally:
        if nltk_module is not None and callable(original_download):
            setattr(nltk_module, "download", original_download)

    return []


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
    segmentation_failures = check_sentence_segmentation()
    version_failures = check_package_versions()
    if segmentation_failures:
        # Pipecat's module import attempts an NLTK download when punkt data is
        # absent. Do not run the broader imports after the no-network gate has
        # failed, or that fallback could mask a broken frozen bundle.
        return [*segmentation_failures, *version_failures]
    return [*check_imports(), *version_failures]


def main() -> int:
    missing = check_runtime_requirements()
    result = {"ok": not missing, "missing": missing}
    print(json.dumps(result, separators=(",", ":")))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
