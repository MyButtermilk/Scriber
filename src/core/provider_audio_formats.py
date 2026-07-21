"""Verified, route-scoped audio input capabilities for STT providers.

This registry deliberately separates provider documentation from media
preparation.  In particular, a documented OGG or WebM *container* is not
evidence for an Opus codec.  Only exact container/codec combinations may be
selected for a provider request.

The matrix was verified against the normative provider inventory in GitHub
issue #18 on 2026-07-20.  Unknown models, custom endpoints, and inactive routes
receive no inherited format capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Iterable


CAPABILITY_REVISION = "provider-audio-formats-v1"
CAPABILITY_VERIFIED_AT = date(2026, 7, 20)
SPEECHMATICS_BATCH_DEFAULT_BASE_URL = "https://asr.api.speechmatics.com/v2"
SPEECHMATICS_REALTIME_DEFAULT_BASE_URL = "wss://eu2.rt.speechmatics.com/v2"


def speechmatics_batch_endpoint_is_custom(value: str | None) -> bool:
    """Return true without retaining or exposing the configured endpoint."""

    configured = (
        SPEECHMATICS_BATCH_DEFAULT_BASE_URL
        if value is None
        else str(value).strip()
    ).rstrip("/")
    return configured.casefold() != SPEECHMATICS_BATCH_DEFAULT_BASE_URL.casefold()


def speechmatics_realtime_base_url(value: str | None = None) -> str:
    """Resolve the exact realtime endpoint using Pipecat 1.5 semantics."""

    configured = str(value or "").strip() or SPEECHMATICS_REALTIME_DEFAULT_BASE_URL
    return configured.rstrip("/")


def speechmatics_realtime_endpoint_is_custom(value: str | None) -> bool:
    """Return true when the configured realtime endpoint is not Pipecat's default."""

    return (
        speechmatics_realtime_base_url(value).casefold()
        != SPEECHMATICS_REALTIME_DEFAULT_BASE_URL.casefold()
    )


class AudioContainer(StrEnum):
    """Physical framing/container of one audio representation."""

    RAW = "raw"
    WAV = "wav"
    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"
    AIFF = "aiff"
    M4A = "m4a"
    OGG = "ogg"
    WEBM = "webm"
    MP4 = "mp4"
    AMR = "amr"
    SPEEX = "speex"
    G729 = "g729"


class AudioCodec(StrEnum):
    """Codec/sample representation independent of its container."""

    PCM = "pcm"
    PCM_S16LE = "pcm_s16le"
    PCM_S24LE = "pcm_s24le"
    PCM_S32LE = "pcm_s32le"
    PCM_F32LE = "pcm_f32le"
    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"
    ALAC = "alac"
    VORBIS = "vorbis"
    OPUS = "opus"
    MULAW = "mulaw"
    ALAW = "alaw"
    AMR_NB = "amr_nb"
    AMR_WB = "amr_wb"
    SPEEX = "speex"
    G729 = "g729"


class AudioInputFormat(StrEnum):
    """Exact audio representations eligible for capability decisions."""

    WAV_PCM16 = "wav_pcm16"
    WAV_PCM24 = "wav_pcm24"
    WAV_PCM32 = "wav_pcm32"
    RAW_PCM16 = "raw_pcm16"
    RAW_PCM32_FLOAT = "raw_pcm32_float"
    MP3 = "mp3"
    FLAC = "flac"
    AAC = "aac"
    AIFF_PCM = "aiff_pcm"
    M4A_AAC = "m4a_aac"
    M4A_ALAC = "m4a_alac"
    OGG_VORBIS = "ogg_vorbis"
    OGG_OPUS = "ogg_opus"
    WEBM_OPUS = "webm_opus"
    WEBM_VORBIS = "webm_vorbis"
    MP4_AUDIO = "mp4_audio"
    MULAW = "mulaw"
    ALAW = "alaw"
    AMR_NB = "amr_nb"
    AMR_WB = "amr_wb"
    SPEEX = "speex"
    G729 = "g729"

    @property
    def container(self) -> AudioContainer:
        return _FORMAT_PARTS[self][0]

    @property
    def codec(self) -> AudioCodec:
        return _FORMAT_PARTS[self][1]


_FORMAT_PARTS: dict[AudioInputFormat, tuple[AudioContainer, AudioCodec]] = {
    AudioInputFormat.WAV_PCM16: (AudioContainer.WAV, AudioCodec.PCM_S16LE),
    AudioInputFormat.WAV_PCM24: (AudioContainer.WAV, AudioCodec.PCM_S24LE),
    AudioInputFormat.WAV_PCM32: (AudioContainer.WAV, AudioCodec.PCM_S32LE),
    AudioInputFormat.RAW_PCM16: (AudioContainer.RAW, AudioCodec.PCM_S16LE),
    AudioInputFormat.RAW_PCM32_FLOAT: (AudioContainer.RAW, AudioCodec.PCM_F32LE),
    AudioInputFormat.MP3: (AudioContainer.MP3, AudioCodec.MP3),
    AudioInputFormat.FLAC: (AudioContainer.FLAC, AudioCodec.FLAC),
    AudioInputFormat.AAC: (AudioContainer.AAC, AudioCodec.AAC),
    AudioInputFormat.AIFF_PCM: (AudioContainer.AIFF, AudioCodec.PCM),
    AudioInputFormat.M4A_AAC: (AudioContainer.M4A, AudioCodec.AAC),
    AudioInputFormat.M4A_ALAC: (AudioContainer.M4A, AudioCodec.ALAC),
    AudioInputFormat.OGG_VORBIS: (AudioContainer.OGG, AudioCodec.VORBIS),
    AudioInputFormat.OGG_OPUS: (AudioContainer.OGG, AudioCodec.OPUS),
    AudioInputFormat.WEBM_OPUS: (AudioContainer.WEBM, AudioCodec.OPUS),
    AudioInputFormat.WEBM_VORBIS: (AudioContainer.WEBM, AudioCodec.VORBIS),
    AudioInputFormat.MP4_AUDIO: (AudioContainer.MP4, AudioCodec.AAC),
    AudioInputFormat.MULAW: (AudioContainer.RAW, AudioCodec.MULAW),
    AudioInputFormat.ALAW: (AudioContainer.RAW, AudioCodec.ALAW),
    AudioInputFormat.AMR_NB: (AudioContainer.AMR, AudioCodec.AMR_NB),
    AudioInputFormat.AMR_WB: (AudioContainer.AMR, AudioCodec.AMR_WB),
    AudioInputFormat.SPEEX: (AudioContainer.SPEEX, AudioCodec.SPEEX),
    AudioInputFormat.G729: (AudioContainer.G729, AudioCodec.G729),
}

_FORMAT_BY_PARTS = {parts: audio_format for audio_format, parts in _FORMAT_PARTS.items()}


class ProviderAudioRouteKind(StrEnum):
    BATCH = "batch"
    REALTIME = "realtime"
    LOCAL_NO_UPLOAD = "local_no_upload"


class CapabilityEvidenceKind(StrEnum):
    OFFICIAL_ENDPOINT_DOCS = "official_endpoint_docs"
    OFFICIAL_SDK_SCHEMA = "official_sdk_schema"
    SCRIBER_INTEGRATION_TEST = "scriber_integration_test"
    BOUNDED_LIVE_PROBE = "bounded_live_probe"


class AudioSelectionMode(StrEnum):
    ORIGINAL_PASSTHROUGH = "original_passthrough"
    GENERATED = "generated"


class ProviderAudioCapabilityError(ValueError):
    """Base error for a format decision without exact route evidence."""


class UnsupportedProviderAudioRoute(ProviderAudioCapabilityError):
    pass


class InactiveProviderAudioRoute(UnsupportedProviderAudioRoute):
    pass


class UnsupportedAudioInputFormat(ProviderAudioCapabilityError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderAudioInputCapabilities:
    provider: str
    route: str
    model_family: str
    route_kind: ProviderAudioRouteKind
    batch_formats: frozenset[AudioInputFormat]
    realtime_formats: frozenset[AudioInputFormat]
    direct_passthrough_formats: frozenset[AudioInputFormat]
    batch_generic_containers: frozenset[AudioContainer]
    realtime_generic_containers: frozenset[AudioContainer]
    preferred_lossy_format: AudioInputFormat | None
    preferred_lossless_format: AudioInputFormat | None
    max_upload_bytes: int | None
    evidence_kind: CapabilityEvidenceKind
    evidence_reference: str
    verified_at: date
    capability_id: str
    revision: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class AudioInputSelection:
    audio_format: AudioInputFormat
    mode: AudioSelectionMode
    capability_id: str
    capability_revision: str


def _set(values: Iterable[AudioInputFormat] = ()) -> frozenset[AudioInputFormat]:
    return frozenset(values)


def _containers(
    values: Iterable[AudioContainer] = (),
) -> frozenset[AudioContainer]:
    return frozenset(values)


def _capability(
    provider: str,
    route: str,
    model_family: str,
    route_kind: ProviderAudioRouteKind,
    *,
    batch_formats: Iterable[AudioInputFormat] = (),
    realtime_formats: Iterable[AudioInputFormat] = (),
    direct_passthrough_formats: Iterable[AudioInputFormat] = (),
    batch_generic_containers: Iterable[AudioContainer] = (),
    realtime_generic_containers: Iterable[AudioContainer] = (),
    preferred_lossy_format: AudioInputFormat | None = None,
    preferred_lossless_format: AudioInputFormat | None = None,
    max_upload_bytes: int | None = None,
    evidence_kind: CapabilityEvidenceKind = (
        CapabilityEvidenceKind.OFFICIAL_ENDPOINT_DOCS
    ),
    evidence_reference: str,
    active: bool = True,
) -> ProviderAudioInputCapabilities:
    return ProviderAudioInputCapabilities(
        provider=provider,
        route=route,
        model_family=model_family,
        route_kind=route_kind,
        batch_formats=_set(batch_formats),
        realtime_formats=_set(realtime_formats),
        direct_passthrough_formats=_set(direct_passthrough_formats),
        batch_generic_containers=_containers(batch_generic_containers),
        realtime_generic_containers=_containers(realtime_generic_containers),
        preferred_lossy_format=preferred_lossy_format,
        preferred_lossless_format=preferred_lossless_format,
        max_upload_bytes=max_upload_bytes,
        evidence_kind=evidence_kind,
        evidence_reference=evidence_reference,
        verified_at=CAPABILITY_VERIFIED_AT,
        capability_id=f"{provider}:{route}:{model_family}",
        revision=CAPABILITY_REVISION,
        active=active,
    )


_WAV_MP3_FLAC = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
)
_SONIOX_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.WEBM_OPUS,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.AIFF_PCM,
    AudioInputFormat.M4A_AAC,
)
_SONIOX_VERIFIED_PASSTHROUGH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.WEBM_OPUS,
)
_SMALLEST_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.OGG_VORBIS,
    AudioInputFormat.OGG_OPUS,
    AudioInputFormat.M4A_AAC,
    AudioInputFormat.M4A_ALAC,
    AudioInputFormat.WEBM_OPUS,
    AudioInputFormat.WEBM_VORBIS,
)
_ASSEMBLYAI_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.AIFF_PCM,
    AudioInputFormat.M4A_AAC,
    AudioInputFormat.M4A_ALAC,
    AudioInputFormat.OGG_OPUS,
    AudioInputFormat.AMR_NB,
)
_DEEPGRAM_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.M4A_AAC,
    AudioInputFormat.OGG_OPUS,
)
_GEMINI_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.AIFF_PCM,
    AudioInputFormat.OGG_VORBIS,
)
_GLADIA_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.M4A_AAC,
    AudioInputFormat.OGG_OPUS,
)
_MODULATE_BATCH = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
    AudioInputFormat.AAC,
    AudioInputFormat.AIFF_PCM,
    AudioInputFormat.OGG_OPUS,
)


PROVIDER_AUDIO_CAPABILITY_MATRIX: tuple[ProviderAudioInputCapabilities, ...] = (
    _capability(
        "soniox",
        "async_transcription",
        "stt-async-v5",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_SONIOX_BATCH,
        direct_passthrough_formats=_SONIOX_VERIFIED_PASSTHROUGH,
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.WEBM_OPUS,
        preferred_lossless_format=AudioInputFormat.FLAC,
        max_upload_bytes=500 * 1024 * 1024,
        evidence_kind=CapabilityEvidenceKind.SCRIBER_INTEGRATION_TEST,
        evidence_reference="src/pipeline.py Soniox async WebM/Opus route",
    ),
    _capability(
        "soniox_async",
        "async_transcription",
        "stt-async-v5",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_SONIOX_BATCH,
        direct_passthrough_formats=_SONIOX_VERIFIED_PASSTHROUGH,
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.WEBM_OPUS,
        preferred_lossless_format=AudioInputFormat.FLAC,
        max_upload_bytes=500 * 1024 * 1024,
        evidence_kind=CapabilityEvidenceKind.SCRIBER_INTEGRATION_TEST,
        evidence_reference="src/pipeline.py Soniox async WebM/Opus route",
    ),
    _capability(
        "soniox",
        "realtime_transcription",
        "stt-rt-v5",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.RAW_PCM32_FLOAT,
            AudioInputFormat.MULAW,
            AudioInputFormat.ALAW,
        ),
        realtime_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference="https://soniox.com/docs/sdk/node-SDK/reference/types",
    ),
    *(
        _capability(
            provider,
            "audio_transcriptions",
            "voxtral-mini-2602",
            ProviderAudioRouteKind.BATCH,
            batch_formats=_WAV_MP3_FLAC,
            direct_passthrough_formats=_WAV_MP3_FLAC,
            batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
            preferred_lossy_format=AudioInputFormat.MP3,
            preferred_lossless_format=AudioInputFormat.FLAC,
            evidence_reference="https://docs.mistral.ai/resources/known-limitations",
        )
        for provider in ("mistral", "mistral_async")
    ),
    *(
        _capability(
            provider,
            "pulse_pre_recorded",
            "pulse",
            ProviderAudioRouteKind.BATCH,
            batch_formats=_SMALLEST_BATCH,
            direct_passthrough_formats=_SMALLEST_BATCH,
            preferred_lossy_format=AudioInputFormat.OGG_OPUS,
            preferred_lossless_format=AudioInputFormat.FLAC,
            evidence_reference=(
                "https://docs.smallest.ai/waves/v-4-0-0/documentation/"
                "speech-to-text-pulse/pre-recorded/audio-formats"
            ),
        )
        for provider in ("smallest", "smallest_async")
    ),
    _capability(
        "smallest",
        "pulse_realtime",
        "pulse",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.OGG_OPUS,
            AudioInputFormat.MULAW,
            AudioInputFormat.ALAW,
        ),
        preferred_lossy_format=AudioInputFormat.OGG_OPUS,
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference=(
            "https://docs.smallest.ai/waves/documentation/"
            "speech-to-text-pulse/realtime-web-socket/audio-formats"
        ),
    ),
    _capability(
        "assemblyai",
        "pre_recorded",
        "universal-3-5-pro",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_ASSEMBLYAI_BATCH,
        direct_passthrough_formats=_ASSEMBLYAI_BATCH,
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.OGG_OPUS,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference=(
            "https://www.assemblyai.com/docs/faq/"
            "what-audio-and-video-file-types-are-supported-by-your-api"
        ),
    ),
    _capability(
        "assemblyai_realtime",
        "streaming",
        "universal-3-5-pro",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(AudioInputFormat.RAW_PCM16, AudioInputFormat.MULAW),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference=(
            "https://www.assemblyai.com/docs/streaming/guides/"
            "stream_prerecorded_file_realtime"
        ),
    ),
    _capability(
        "deepgram_async",
        "pre_recorded",
        "nova-3",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_DEEPGRAM_BATCH,
        direct_passthrough_formats=_DEEPGRAM_BATCH,
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.OGG_OPUS,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference="https://developers.deepgram.com/docs/supported-audio-formats",
    ),
    _capability(
        "deepgram",
        "nova_streaming",
        "nova-3",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.FLAC,
            AudioInputFormat.MULAW,
            AudioInputFormat.ALAW,
            AudioInputFormat.AMR_NB,
            AudioInputFormat.AMR_WB,
            AudioInputFormat.OGG_OPUS,
            AudioInputFormat.SPEEX,
            AudioInputFormat.G729,
        ),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference="https://developers.deepgram.com/docs/encoding",
    ),
    _capability(
        "openai_async",
        "audio_transcriptions",
        "gpt-4o-mini-transcribe-2025-12-15",
        ProviderAudioRouteKind.BATCH,
        batch_formats=(
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.MP3,
            AudioInputFormat.M4A_AAC,
        ),
        direct_passthrough_formats=(
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.MP3,
            AudioInputFormat.M4A_AAC,
        ),
        batch_generic_containers=(AudioContainer.MP4, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.MP3,
        preferred_lossless_format=AudioInputFormat.WAV_PCM16,
        evidence_reference="https://platform.openai.com/docs/guides/speech-to-text",
    ),
    _capability(
        "openai",
        "realtime_transcription",
        "gpt-realtime-whisper",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.MULAW,
            AudioInputFormat.ALAW,
        ),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference="https://platform.openai.com/docs/api-reference/realtime",
    ),
    _capability(
        "groq",
        "openai_v1_segmented_audio_transcriptions",
        "whisper-large-v3-turbo",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_WAV_MP3_FLAC,
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.MP3,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference="https://console.groq.com/docs/speech-to-text",
    ),
    _capability(
        "azure_mai",
        "llm_speech_batch",
        "mai-transcribe-1.5",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_WAV_MP3_FLAC,
        # Keep the shipped 64-kbit/s MP3 control authoritative.  Provider
        # acceptance of WAV/FLAC does not by itself promote either source
        # representation to Scriber's direct-upload path.
        direct_passthrough_formats=(AudioInputFormat.MP3,),
        preferred_lossy_format=AudioInputFormat.MP3,
        preferred_lossless_format=AudioInputFormat.FLAC,
        max_upload_bytes=300_000_000,
        evidence_reference=(
            "https://learn.microsoft.com/en-us/azure/ai-services/"
            "speech-service/mai-transcribe"
        ),
    ),
    _capability(
        "openrouter_stt",
        "audio_transcriptions",
        "microsoft/mai-transcribe-1.5",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_WAV_MP3_FLAC,
        direct_passthrough_formats=_WAV_MP3_FLAC,
        preferred_lossy_format=AudioInputFormat.MP3,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference=(
            "https://openrouter.ai/microsoft/mai-transcribe-1.5/providers"
        ),
        active=False,
    ),
    _capability(
        "gemini_stt",
        "generate_content_audio",
        "gemini-2.5-flash",
        ProviderAudioRouteKind.BATCH,
        batch_formats=_GEMINI_BATCH,
        direct_passthrough_formats=_GEMINI_BATCH,
        preferred_lossy_format=AudioInputFormat.MP3,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference="https://ai.google.dev/gemini-api/docs/audio",
    ),
    _capability(
        "google",
        "cloud_streaming_v2",
        "latest_long",
        ProviderAudioRouteKind.REALTIME,
        # Google support is API-version dependent.  Scriber's current Pipecat
        # route has only verified its configured linear PCM stream; do not
        # inherit OGG_OPUS/WEBM_OPUS from another Cloud Speech surface.
        realtime_formats=(AudioInputFormat.RAW_PCM16,),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference=(
            "https://cloud.google.com/speech-to-text/docs/reference/rpc/"
            "google.cloud.speech.v2"
        ),
    ),
    *(
        _capability(
            provider,
            "v2_pre_recorded",
            "default",
            ProviderAudioRouteKind.BATCH,
            batch_formats=_GLADIA_BATCH,
            direct_passthrough_formats=_GLADIA_BATCH,
            batch_generic_containers=(AudioContainer.OGG,),
            preferred_lossy_format=AudioInputFormat.OGG_OPUS,
            preferred_lossless_format=AudioInputFormat.FLAC,
            evidence_reference=(
                "https://docs.gladia.io/chapters/limits-and-specifications/"
                "supported-formats"
            ),
        )
        for provider in ("gladia", "gladia_async")
    ),
    _capability(
        "gladia",
        "v2_live",
        "solaria-1",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.WAV_PCM24,
            AudioInputFormat.WAV_PCM32,
            AudioInputFormat.MULAW,
            AudioInputFormat.ALAW,
        ),
        preferred_lossless_format=AudioInputFormat.WAV_PCM16,
        evidence_reference="https://docs.gladia.io/api-reference/v2/live/init",
    ),
    _capability(
        "speechmatics_async",
        "batch_v2",
        "enhanced",
        ProviderAudioRouteKind.BATCH,
        # The public exhaustive list is legacy.  Keep only the representation
        # exercised by Scriber's current batch adapter until SaaS revalidation.
        batch_formats=(AudioInputFormat.WAV_PCM16,),
        direct_passthrough_formats=(AudioInputFormat.WAV_PCM16,),
        batch_generic_containers=(AudioContainer.OGG,),
        preferred_lossless_format=AudioInputFormat.WAV_PCM16,
        evidence_kind=CapabilityEvidenceKind.SCRIBER_INTEGRATION_TEST,
        evidence_reference="src/cloud_async_stt.py Speechmatics WAV batch route",
    ),
    _capability(
        "speechmatics",
        "realtime_v2",
        "enhanced",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.RAW_PCM32_FLOAT,
            AudioInputFormat.MULAW,
        ),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference=(
            "https://legacy.docs.speechmatics.com/en/real-time-appliance/api-v2/"
        ),
    ),
    *(
        _capability(
            provider,
            "velma_2_batch",
            "multilingual",
            ProviderAudioRouteKind.BATCH,
            batch_formats=_MODULATE_BATCH,
            direct_passthrough_formats=_MODULATE_BATCH,
            batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
            preferred_lossy_format=AudioInputFormat.OGG_OPUS,
            preferred_lossless_format=AudioInputFormat.FLAC,
            max_upload_bytes=100 * 1024 * 1024,
            evidence_kind=CapabilityEvidenceKind.OFFICIAL_ENDPOINT_DOCS,
            evidence_reference="https://docs.modulate.ai/quickstart",
        )
        for provider in ("modulate", "modulate_async")
    ),
    _capability(
        "modulate_async",
        "velma_2_batch_english_vfast",
        "english-fast",
        ProviderAudioRouteKind.BATCH,
        batch_formats=(AudioInputFormat.OGG_OPUS,),
        direct_passthrough_formats=(AudioInputFormat.OGG_OPUS,),
        preferred_lossy_format=AudioInputFormat.OGG_OPUS,
        max_upload_bytes=100 * 1024 * 1024,
        evidence_reference="https://docs.modulate.ai/quickstart",
        active=False,
    ),
    _capability(
        "modulate",
        "velma_2_streaming",
        "multilingual",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(
            AudioInputFormat.RAW_PCM16,
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.MP3,
            AudioInputFormat.FLAC,
            AudioInputFormat.AAC,
            AudioInputFormat.AIFF_PCM,
        ),
        realtime_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_reference="https://docs.modulate.ai/quickstart",
    ),
    _capability(
        "elevenlabs",
        "scribe_v2_realtime",
        "scribe_v2_realtime",
        ProviderAudioRouteKind.REALTIME,
        realtime_formats=(AudioInputFormat.RAW_PCM16,),
        preferred_lossless_format=AudioInputFormat.RAW_PCM16,
        evidence_kind=CapabilityEvidenceKind.OFFICIAL_SDK_SCHEMA,
        evidence_reference="https://elevenlabs.io/docs/overview/capabilities/speech-to-text",
    ),
    _capability(
        "elevenlabs",
        "scribe_v2_batch",
        "scribe_v2",
        ProviderAudioRouteKind.BATCH,
        batch_formats=(
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.MP3,
            AudioInputFormat.FLAC,
            AudioInputFormat.AAC,
            AudioInputFormat.AIFF_PCM,
            AudioInputFormat.OGG_OPUS,
            AudioInputFormat.M4A_AAC,
        ),
        direct_passthrough_formats=(
            AudioInputFormat.WAV_PCM16,
            AudioInputFormat.MP3,
            AudioInputFormat.FLAC,
            AudioInputFormat.AAC,
            AudioInputFormat.AIFF_PCM,
            AudioInputFormat.OGG_OPUS,
            AudioInputFormat.M4A_AAC,
        ),
        batch_generic_containers=(AudioContainer.OGG, AudioContainer.WEBM),
        preferred_lossy_format=AudioInputFormat.OGG_OPUS,
        preferred_lossless_format=AudioInputFormat.FLAC,
        evidence_reference="https://elevenlabs.io/docs/overview/capabilities/speech-to-text",
        active=False,
    ),
    _capability(
        "onnx_local",
        "decoded_pcm_local",
        "nemo-parakeet-tdt-0.6b-v3",
        ProviderAudioRouteKind.LOCAL_NO_UPLOAD,
        evidence_kind=CapabilityEvidenceKind.SCRIBER_INTEGRATION_TEST,
        evidence_reference="src/onnx_stt.py local decoded-PCM boundary",
    ),
)


_CAPABILITY_INDEX = {
    (entry.provider, entry.route, entry.model_family): entry
    for entry in PROVIDER_AUDIO_CAPABILITY_MATRIX
}

_BATCH_ROUTE_BY_PROVIDER = {
    "soniox": "async_transcription",
    "soniox_async": "async_transcription",
    "mistral": "audio_transcriptions",
    "mistral_async": "audio_transcriptions",
    "smallest": "pulse_pre_recorded",
    "smallest_async": "pulse_pre_recorded",
    "assemblyai": "pre_recorded",
    "deepgram_async": "pre_recorded",
    "openai_async": "audio_transcriptions",
    "groq": "openai_v1_segmented_audio_transcriptions",
    "azure_mai": "llm_speech_batch",
    "openrouter_stt": "audio_transcriptions",
    "gemini_stt": "generate_content_audio",
    "gladia": "v2_pre_recorded",
    "gladia_async": "v2_pre_recorded",
    "speechmatics_async": "batch_v2",
    "modulate": "velma_2_batch",
    "modulate_async": "velma_2_batch",
    "onnx_local": "decoded_pcm_local",
}

_REALTIME_ROUTE_BY_PROVIDER = {
    "soniox": "realtime_transcription",
    "smallest": "pulse_realtime",
    "assemblyai_realtime": "streaming",
    "deepgram": "nova_streaming",
    "openai": "realtime_transcription",
    "google": "cloud_streaming_v2",
    "gladia": "v2_live",
    "speechmatics": "realtime_v2",
    "modulate": "velma_2_streaming",
    "elevenlabs": "scribe_v2_realtime",
}

_REALTIME_PCM_PREPARATION_IMPLEMENTATION_BY_PROVIDER = {
    "assemblyai_realtime": "pipecat_assemblyai_streaming_raw_pcm16",
    "deepgram": "pipecat_deepgram_nova_streaming_raw_pcm16",
    "openai": "pipecat_openai_realtime_pcm16_24khz",
    "google": "pipecat_google_speech_v2_raw_pcm16",
    "elevenlabs": "pipecat_elevenlabs_scribe_v2_realtime_raw_pcm16",
    "speechmatics": "pipecat_speechmatics_realtime_v2_raw_pcm16",
}

# Exact, implementation-owned model markers mapped to the normative family
# key.  No prefix/wildcard matching is allowed here.
_BATCH_MODEL_FAMILY_ALIASES = {
    ("gladia", "pre-recorded-v2"): "default",
    ("gladia_async", "pre-recorded-v2"): "default",
    ("speechmatics_async", "batch-v2"): "enhanced",
    ("modulate", "velma-2-stt-batch"): "multilingual",
    ("modulate_async", "velma-2-stt-batch"): "multilingual",
}


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _coerce_route_kind(value: ProviderAudioRouteKind | str) -> ProviderAudioRouteKind:
    try:
        return (
            value
            if isinstance(value, ProviderAudioRouteKind)
            else ProviderAudioRouteKind(_normalized(value))
        )
    except ValueError as exc:
        raise UnsupportedProviderAudioRoute("Unknown provider audio route kind.") from exc


def coerce_audio_input_format(value: AudioInputFormat | str) -> AudioInputFormat:
    """Return one exact format; generic ``ogg``/``webm`` strings are invalid."""

    if isinstance(value, AudioInputFormat):
        return value
    try:
        return AudioInputFormat(_normalized(value))
    except ValueError as exc:
        raise UnsupportedAudioInputFormat(
            "Audio input must name an exact verified container/codec format."
        ) from exc


def exact_audio_input_format(
    container: AudioContainer | str,
    codec: AudioCodec | str,
) -> AudioInputFormat:
    """Resolve an exact pair without inferring a codec from its container."""

    try:
        container_value = (
            container
            if isinstance(container, AudioContainer)
            else AudioContainer(_normalized(container))
        )
        codec_value = (
            codec if isinstance(codec, AudioCodec) else AudioCodec(_normalized(codec))
        )
        return _FORMAT_BY_PARTS[(container_value, codec_value)]
    except (KeyError, ValueError) as exc:
        raise UnsupportedAudioInputFormat(
            "Container and codec do not identify a registered exact audio format."
        ) from exc


def batch_route_for_provider(provider: str) -> str | None:
    return _BATCH_ROUTE_BY_PROVIDER.get(_normalized(provider))


def realtime_route_for_provider(provider: str) -> str | None:
    return _REALTIME_ROUTE_BY_PROVIDER.get(_normalized(provider))


def realtime_pcm_preparation_implementation(provider: str) -> str | None:
    """Return the exact Pipecat PCM boundary implemented for a realtime key."""

    return _REALTIME_PCM_PREPARATION_IMPLEMENTATION_BY_PROVIDER.get(
        _normalized(provider)
    )


def resolve_provider_audio_capabilities(
    provider: str,
    route: str,
    model_family: str,
    *,
    custom_endpoint: bool = False,
    include_inactive: bool = False,
) -> ProviderAudioInputCapabilities:
    """Resolve only an exact, verified provider/route/model capability key."""

    if custom_endpoint:
        raise UnsupportedProviderAudioRoute(
            "Custom provider endpoints have no verified audio format capability."
        )
    key = (_normalized(provider), _normalized(route), _normalized(model_family))
    capability = _CAPABILITY_INDEX.get(key)
    if capability is None:
        raise UnsupportedProviderAudioRoute(
            "Provider route/model has no verified audio format capability."
        )
    if not capability.active and not include_inactive:
        raise InactiveProviderAudioRoute(
            "Provider audio route is documented but not active in Scriber."
        )
    return capability


def resolve_batch_provider_audio_capabilities(
    provider: str,
    model: str,
    *,
    custom_endpoint: bool = False,
    include_inactive: bool = False,
) -> ProviderAudioInputCapabilities:
    """Resolve Scriber's exact batch route for a configured provider/model."""

    normalized_provider = _normalized(provider)
    route = batch_route_for_provider(normalized_provider)
    if route is None:
        raise UnsupportedProviderAudioRoute(
            "Provider key has no verified batch audio route."
        )
    normalized_model = _normalized(model)
    model_family = _BATCH_MODEL_FAMILY_ALIASES.get(
        (normalized_provider, normalized_model), normalized_model
    )
    return resolve_provider_audio_capabilities(
        normalized_provider,
        route,
        model_family,
        custom_endpoint=custom_endpoint,
        include_inactive=include_inactive,
    )


def try_resolve_batch_provider_audio_capabilities(
    provider: str,
    model: str,
    *,
    custom_endpoint: bool = False,
) -> ProviderAudioInputCapabilities | None:
    """Fail closed without forcing legacy route-freezing callers to throw."""

    try:
        return resolve_batch_provider_audio_capabilities(
            provider,
            model,
            custom_endpoint=custom_endpoint,
        )
    except UnsupportedProviderAudioRoute:
        return None


def supports_exact_audio_input_format(
    capability: ProviderAudioInputCapabilities,
    audio_format: AudioInputFormat | str,
    *,
    route_kind: ProviderAudioRouteKind | str,
) -> bool:
    """Test exact support; generic container evidence is intentionally ignored."""

    try:
        exact_format = coerce_audio_input_format(audio_format)
        kind = _coerce_route_kind(route_kind)
    except ProviderAudioCapabilityError:
        return False
    if not capability.active or kind != capability.route_kind:
        return False
    if kind == ProviderAudioRouteKind.BATCH:
        return exact_format in capability.batch_formats
    if kind == ProviderAudioRouteKind.REALTIME:
        return exact_format in capability.realtime_formats
    return False


def require_exact_audio_input_format(
    capability: ProviderAudioInputCapabilities,
    audio_format: AudioInputFormat | str,
    *,
    route_kind: ProviderAudioRouteKind | str,
) -> AudioInputFormat:
    exact_format = coerce_audio_input_format(audio_format)
    if not supports_exact_audio_input_format(
        capability, exact_format, route_kind=route_kind
    ):
        raise UnsupportedAudioInputFormat(
            "Exact audio format is not verified for this provider route/model."
        )
    return exact_format


_BATCH_GENERATION_ORDER = (
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.OGG_OPUS,
    AudioInputFormat.WEBM_OPUS,
    AudioInputFormat.MP3,
    AudioInputFormat.FLAC,
)
_REALTIME_GENERATION_ORDER = (
    AudioInputFormat.RAW_PCM16,
    AudioInputFormat.WAV_PCM16,
    AudioInputFormat.OGG_OPUS,
    AudioInputFormat.WEBM_OPUS,
    AudioInputFormat.MULAW,
    AudioInputFormat.ALAW,
    AudioInputFormat.RAW_PCM32_FLOAT,
    AudioInputFormat.FLAC,
)

# Production format promotion is deliberately narrower than provider-native
# acceptance. Azure's existing shipped control is mono 64-kbit/s MP3; retain it
# until an installed end-to-end benchmark promotes WAV or FLAC for a duration
# and network bucket. Other routes keep the conservative WAV control.
_PROMOTED_BATCH_GENERATION_ORDER: dict[str, tuple[AudioInputFormat, ...]] = {
    "azure_mai:llm_speech_batch:mai-transcribe-1.5": (
        AudioInputFormat.MP3,
        AudioInputFormat.WAV_PCM16,
        AudioInputFormat.FLAC,
    ),
}


def select_audio_input_format(
    capability: ProviderAudioInputCapabilities,
    *,
    route_kind: ProviderAudioRouteKind | str,
    original_format: AudioInputFormat | str | None = None,
    allow_inactive: bool = False,
) -> AudioInputSelection:
    """Choose one verified representation, with exact pass-through first."""

    kind = _coerce_route_kind(route_kind)
    if not capability.active and not allow_inactive:
        raise InactiveProviderAudioRoute(
            "Provider audio route is documented but not active in Scriber."
        )
    if kind != capability.route_kind or kind == ProviderAudioRouteKind.LOCAL_NO_UPLOAD:
        raise UnsupportedProviderAudioRoute(
            "Provider capability does not support this audio route kind."
        )

    accepted = (
        capability.batch_formats
        if kind == ProviderAudioRouteKind.BATCH
        else capability.realtime_formats
    )
    if original_format is not None:
        exact_original = coerce_audio_input_format(original_format)
        if (
            kind == ProviderAudioRouteKind.BATCH
            and exact_original in accepted
            and exact_original in capability.direct_passthrough_formats
        ):
            return AudioInputSelection(
                audio_format=exact_original,
                mode=AudioSelectionMode.ORIGINAL_PASSTHROUGH,
                capability_id=capability.capability_id,
                capability_revision=capability.revision,
            )

    order = (
        _PROMOTED_BATCH_GENERATION_ORDER.get(
            capability.capability_id,
            _BATCH_GENERATION_ORDER,
        )
        if kind == ProviderAudioRouteKind.BATCH
        else _REALTIME_GENERATION_ORDER
    )
    for candidate in order:
        if candidate in accepted:
            return AudioInputSelection(
                audio_format=candidate,
                mode=AudioSelectionMode.GENERATED,
                capability_id=capability.capability_id,
                capability_revision=capability.revision,
            )
    raise UnsupportedAudioInputFormat(
        "Provider route/model has no verified generated audio representation."
    )


def select_provider_audio_input_format(
    *,
    provider: str,
    route: str,
    model_family: str,
    route_kind: ProviderAudioRouteKind | str,
    original_format: AudioInputFormat | str | None = None,
    custom_endpoint: bool = False,
    include_inactive: bool = False,
) -> AudioInputSelection:
    capability = resolve_provider_audio_capabilities(
        provider,
        route,
        model_family,
        custom_endpoint=custom_endpoint,
        include_inactive=include_inactive,
    )
    return select_audio_input_format(
        capability,
        route_kind=route_kind,
        original_format=original_format,
        allow_inactive=include_inactive,
    )


def _validate_registry() -> None:
    if len(_CAPABILITY_INDEX) != len(PROVIDER_AUDIO_CAPABILITY_MATRIX):
        raise RuntimeError("Provider audio capability keys must be unique.")
    for capability in PROVIDER_AUDIO_CAPABILITY_MATRIX:
        if not capability.direct_passthrough_formats <= capability.batch_formats:
            raise RuntimeError(
                f"{capability.capability_id} pass-through formats must be batch formats."
            )
        accepted = capability.batch_formats | capability.realtime_formats
        for preferred in (
            capability.preferred_lossy_format,
            capability.preferred_lossless_format,
        ):
            if preferred is not None and preferred not in accepted:
                raise RuntimeError(
                    f"{capability.capability_id} has an unsupported preferred format."
                )
        if capability.verified_at != CAPABILITY_VERIFIED_AT:
            raise RuntimeError(
                f"{capability.capability_id} has an unexpected verification date."
            )


_validate_registry()
