from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a privacy-minimal Soniox diarization and Meeting-analysis E2E against generated audio."
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--minimum-speakers", type=int, default=2)
    parser.add_argument("--result-json", type=Path)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from src.config import Config
    from src.meeting_analysis import analyze_meeting
    from src.pipeline import ScriberPipeline
    from src.provider_transcript import normalize_provider_segments
    from src.summarization import generate_meeting_analysis_text

    audio = args.audio.expanduser().resolve()
    if not audio.is_file():
        raise RuntimeError("Generated Meeting audio is missing.")
    if not Config.get_api_key("soniox"):
        raise RuntimeError("Soniox API key is not configured.")

    transcript_parts: list[str] = []

    def on_transcription(text: str, is_final: bool) -> None:
        if is_final and str(text or "").strip():
            transcript_parts.append(str(text).strip())

    pipeline = ScriberPipeline(
        service_name="soniox_async",
        on_transcription=on_transcription,
        enable_speaker_diarization=True,
        direct_file_speaker_diarization=True,
        text_injection_enabled=False,
        direct_file_expected_duration_seconds=60.0,
    )
    stt_started = time.monotonic()
    await pipeline.transcribe_file_direct(str(audio))
    stt_elapsed_ms = round((time.monotonic() - stt_started) * 1_000)
    payload = pipeline.last_structured_transcript_payload
    normalized = normalize_provider_segments("soniox_async", payload, "system")
    speaker_keys = {
        str(segment.get("speakerKey") or "").strip()
        for segment in normalized
        if str(segment.get("speakerKey") or "").strip()
    }
    if len(speaker_keys) < max(1, args.minimum_speakers):
        raise RuntimeError(
            f"Soniox returned {len(speaker_keys)} speaker(s); expected at least {args.minimum_speakers}."
        )
    if not transcript_parts or not normalized:
        raise RuntimeError("Soniox returned no usable generated Meeting transcript.")

    segments = [
        {
            "id": f"generated-e2e-{index:04d}",
            "source": "system",
            "speakerLabel": str(segment.get("speakerLabel") or "Meeting audio"),
            "startMs": int(segment["startMs"]),
            "endMs": int(segment["endMs"]),
            "text": str(segment.get("text") or "").strip(),
            "isFinal": True,
            "sequence": index,
        }
        for index, segment in enumerate(normalized)
        if str(segment.get("text") or "").strip()
    ]

    analysis_results: list[dict[str, Any]] = []
    for model in args.models:
        if model.startswith("minimax/") and not Config.OPENROUTER_API_KEY:
            raise RuntimeError("OpenRouter API key is not configured.")
        if model.startswith("muse-") and not Config.MODEL_API_KEY:
            raise RuntimeError("Meta Model API key is not configured.")
        analysis_started = time.monotonic()
        analysis = await analyze_meeting(
            "Generated three-speaker planning meeting",
            segments,
            [],
            model=model,
            generate=generate_meeting_analysis_text,
            fallback_language="de",
        )
        analysis_results.append(
            {
                "model": model,
                "elapsedMs": round((time.monotonic() - analysis_started) * 1_000),
                "outputLanguage": analysis.get("outputLanguage"),
                "topicCount": len(analysis.get("topics") or []),
                "decisionCount": len(analysis.get("decisions") or []),
                "actionItemCount": len(analysis.get("actionItems") or []),
                "openQuestionCount": len(analysis.get("openQuestions") or []),
                "chapterCount": len(analysis.get("chapters") or []),
            }
        )

    return {
        "schemaVersion": 1,
        "ok": True,
        "fixture": {
            "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            "byteLength": audio.stat().st_size,
            "durationMs": 60_000,
        },
        "stt": {
            "provider": "soniox_async",
            "model": Config.SONIOX_ASYNC_MODEL,
            "speakerDiarizationRequested": True,
            "speakerCount": len(speaker_keys),
            "segmentCount": len(segments),
            "elapsedMs": stt_elapsed_ms,
        },
        "analyses": analysis_results,
    }


def main() -> int:
    args = _parse_args()
    result = asyncio.run(_run(args))
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True)
    if args.result_json is not None:
        target = args.result_json.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
