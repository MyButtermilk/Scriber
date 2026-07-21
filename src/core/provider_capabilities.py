from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_live_streaming: bool
    supports_direct_file_upload: bool
    injects_immediately_in_live_mode: bool
    supports_batch_diarization: bool = False
    supports_word_timestamps: bool = False
    # This is a Scriber route capability, not a provider marketing claim. It is
    # true only when the current Meeting finalization transport can carry a
    # worst-case 18,000-second 16-kHz mono track without exceeding a known
    # provider/file boundary.
    supports_five_hour_meeting: bool = False
    meeting_max_duration_seconds: int | None = None


_DEFAULT = ProviderCapabilities(
    supports_live_streaming=False,
    supports_direct_file_upload=False,
    injects_immediately_in_live_mode=False,
)

_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "soniox": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=True,  # when configured realtime
        # File/YouTube always use Soniox's asynchronous upload contract even
        # when this provider key is also selected for realtime dictation.
        supports_batch_diarization=True,
        supports_word_timestamps=True,
        supports_five_hour_meeting=True,
        meeting_max_duration_seconds=18_000,
    ),
    "soniox_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
        supports_five_hour_meeting=True,
        meeting_max_duration_seconds=18_000,
    ),
    "gemini_stt": ProviderCapabilities(
        supports_live_streaming=False,
        # The implemented generate-content route accepts complete audio files.
        # Exact formats are constrained separately by provider_audio_formats.
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=False,
        supports_word_timestamps=False,
        supports_five_hour_meeting=False,
    ),
    "mistral": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        # The active direct-upload request asks for provider segments. Do not
        # advertise word precision until that request and parser use words.
        supports_word_timestamps=False,
        meeting_max_duration_seconds=10_800,
    ),
    "mistral_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=False,
        meeting_max_duration_seconds=10_800,
    ),
    "smallest": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=True,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
    ),
    "smallest_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
    ),
    "assemblyai": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
        # A five-hour 16-kHz mono PCM track is at most 576 MB before FLAC,
        # below AssemblyAI's documented 2.2-GB upload boundary.
        supports_five_hour_meeting=True,
    ),
    "assemblyai_realtime": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "azure_mai": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        # MAI currently returns timed phrases for Scriber's request, not words.
        supports_word_timestamps=False,
        # The active adapter transcodes to mono 64-kbit/s MP3 before upload;
        # five hours remain below MAI's documented 300-MB file boundary.
        supports_five_hour_meeting=True,
    ),
    "gladia": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        # Scriber's parser currently consumes timed provider utterances.
        supports_word_timestamps=False,
        meeting_max_duration_seconds=8_100,
    ),
    "gladia_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=False,
        meeting_max_duration_seconds=8_100,
    ),
    "deepgram": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "deepgram_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
        # The provider accepts large files, but Scriber's current direct route
        # is the synchronous /v1/listen request. Until long tracks are chunked
        # and merged, its processing window is not a verified five-hour path.
        supports_five_hour_meeting=False,
    ),
    "google": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "speechmatics": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "speechmatics_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_batch_diarization=True,
        supports_word_timestamps=True,
    ),
    "modulate": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        # The adapter requests no partials and emits each provider-final text
        # segment immediately without retaining utterance metadata.
        injects_immediately_in_live_mode=True,
        supports_batch_diarization=False,
        supports_word_timestamps=False,
        # Meeting finalization creates a 64-kbit/s WebM/Opus derivative. Three
        # hours remain safely below Modulate's documented 100-MB file limit.
        meeting_max_duration_seconds=10_800,
    ),
    "modulate_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        # Scriber deliberately discards Modulate utterance-level output.
        supports_batch_diarization=False,
        supports_word_timestamps=False,
        meeting_max_duration_seconds=10_800,
    ),
    "openai": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "openai_async": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
        supports_word_timestamps=True,
    ),
    "groq": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "elevenlabs": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "onnx_local": ProviderCapabilities(
        supports_live_streaming=False,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
        supports_five_hour_meeting=True,
    ),
}


def get_capabilities(provider: str) -> ProviderCapabilities:
    key = (provider or "").strip().lower()
    return _CAPABILITIES.get(key, _DEFAULT)


def supports_direct_file_upload(provider: str) -> bool:
    return get_capabilities(provider).supports_direct_file_upload


def injects_immediately_in_live_mode(provider: str) -> bool:
    return get_capabilities(provider).injects_immediately_in_live_mode


def supports_batch_diarization(provider: str) -> bool:
    """Whether the selected batch model returns native speaker attribution."""
    return get_capabilities(provider).supports_batch_diarization


def supports_word_timestamps(provider: str) -> bool:
    return get_capabilities(provider).supports_word_timestamps


def supports_five_hour_meeting(provider: str) -> bool:
    """Whether Scriber's current finalization route is safe for a 5-hour track."""
    return get_capabilities(provider).supports_five_hour_meeting


def meeting_max_duration_seconds(provider: str, model: str | None = None) -> int | None:
    """Hard duration ceiling for the active Meeting finalization route, if known."""
    key = (provider or "").strip().lower()
    if key in {"mistral", "mistral_async"}:
        selected_model = str(
            model
            or os.getenv("SCRIBER_MISTRAL_ASYNC_MODEL", "voxtral-mini-2602")
        ).strip().lower()
        # Voxtral Mini Transcribe 2 (2602) supports three hours. Preserve the
        # older 2507/unknown override conservatively at its 30-minute bound.
        return 10_800 if "2602" in selected_model else 1_800
    return get_capabilities(key).meeting_max_duration_seconds

