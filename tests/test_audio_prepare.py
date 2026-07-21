from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import audio_prepare
from src.core.provider_audio_formats import (
    AudioInputFormat,
    AudioSelectionMode,
    UnsupportedProviderAudioRoute,
)


def _probe(
    audio_format: AudioInputFormat,
    *,
    byte_length: int = 128,
) -> audio_prepare.ProbedAudioInput:
    return audio_prepare.ProbedAudioInput(
        audio_format=audio_format,
        container_name=audio_format.container.value,
        codec_name=audio_format.codec.value,
        sample_rate=16_000,
        channels=1,
        duration_ms=1_000,
        byte_length=byte_length,
    )


@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        ("opus", AudioInputFormat.OGG_OPUS),
        ("vorbis", AudioInputFormat.OGG_VORBIS),
    ],
)
def test_probe_requires_exact_ogg_codec(codec: str, expected: AudioInputFormat) -> None:
    got, _container, _codec = audio_prepare._format_from_probe_payload(
        {
            "streams": [{"codec_name": codec, "codec_type": "audio"}],
            "format": {"format_name": "ogg"},
        },
        suffix=".ogg",
    )
    assert got == expected


def test_probe_never_infers_opus_from_generic_webm() -> None:
    with pytest.raises(audio_prepare.AudioFormatProbeError, match="WebM"):
        audio_prepare._format_from_probe_payload(
            {
                "streams": [{"codec_name": "aac", "codec_type": "audio"}],
                "format": {"format_name": "matroska,webm"},
            },
            suffix=".webm",
        )


@pytest.mark.parametrize(
    ("suffix", "codec", "container"),
    [
        (".wav", "pcm_s16le", "mp3"),
        (".mp3", "mp3", "wav"),
        (".flac", "flac", "ogg"),
        (".aac", "aac", "wav"),
        (".m4a", "aac", "wav"),
        (".aiff", "pcm_s16be", "wav"),
        (".ogg", "opus", "matroska"),
        (".webm", "opus", "ogg"),
    ],
)
def test_probe_rejects_renamed_payload_without_matching_container(
    suffix: str,
    codec: str,
    container: str,
) -> None:
    with pytest.raises(audio_prepare.AudioFormatProbeError, match="container"):
        audio_prepare._format_from_probe_payload(
            {
                "streams": [{"codec_name": codec, "codec_type": "audio"}],
                "format": {"format_name": container},
            },
            suffix=suffix,
        )


def test_probe_audio_input_file_uses_container_and_codec(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fixture.webm"
    source.write_bytes(b"fixture")
    payload = {
        "streams": [
            {
                "codec_name": "vorbis",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 2,
            }
        ],
        "format": {"format_name": "matroska,webm", "duration": "1.25"},
    }
    monkeypatch.setattr(
        audio_prepare.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        ),
    )
    result = audio_prepare.probe_audio_input_file(source, ffprobe="ffprobe")
    assert result.audio_format == AudioInputFormat.WEBM_VORBIS
    assert result.sample_rate == 48_000
    assert result.channels == 2
    assert result.duration_ms == 1_250


def test_selection_passes_through_exact_azure_mp3() -> None:
    capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai",
        model="mai-transcribe-1.5",
        probe=_probe(AudioInputFormat.MP3),
    )
    assert capability.provider == "azure_mai"
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


def test_azure_unaccepted_source_retains_promoted_mp3_control() -> None:
    _capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai",
        model="mai-transcribe-1.5",
        probe=_probe(AudioInputFormat.OGG_OPUS),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


@pytest.mark.parametrize("source_format", [AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC])
def test_azure_non_mp3_sources_retain_promoted_mp3_control(source_format) -> None:
    _capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai",
        model="mai-transcribe-1.5",
        probe=_probe(source_format),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


def test_selection_fails_closed_for_custom_endpoint() -> None:
    with pytest.raises(UnsupportedProviderAudioRoute):
        audio_prepare.resolve_provider_audio_selection(
            provider="azure_mai",
            model="mai-transcribe-1.5",
            probe=_probe(AudioInputFormat.MP3),
            custom_endpoint=True,
        )


@pytest.mark.asyncio
async def test_generated_preparation_is_cleaned(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fixture.ogg"
    source.write_bytes(b"source")

    def probe(path: Path):
        return _probe(
            AudioInputFormat.OGG_OPUS
            if Path(path) == source
            else AudioInputFormat.MP3,
            byte_length=6,
        )

    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        probe,
    )
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")

    async def generate(_command: list[str], target: Path) -> None:
        target.write_bytes(b"RIFF" + (b"\0" * 64))

    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", generate)
    generated_path: Path | None = None
    async with audio_prepare.prepare_provider_audio_file(
        source,
        provider="azure_mai",
        model="mai-transcribe-1.5",
        work_dir=tmp_path,
    ) as prepared:
        generated_path = prepared.path
        assert prepared.generated is True
        assert prepared.selected_format == AudioInputFormat.MP3
        assert prepared.implementation == "current_ffmpeg_mp3_fallback"
        assert generated_path.is_file()
    assert generated_path is not None
    assert not generated_path.exists()
    assert source.read_bytes() == b"source"


@pytest.mark.asyncio
async def test_generated_preparation_rejects_wrong_container_codec(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.ogg"
    source.write_bytes(b"source")
    generated_paths: list[Path] = []

    def probe(path: Path):
        return _probe(
            AudioInputFormat.OGG_OPUS
            if Path(path) == source
            else AudioInputFormat.WAV_PCM16,
            byte_length=6,
        )

    async def generate(_command: list[str], target: Path) -> None:
        generated_paths.append(target)
        target.write_bytes(b"wrong-format")

    monkeypatch.setattr(audio_prepare, "probe_audio_input_file", probe)
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")
    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", generate)

    with pytest.raises(
        audio_prepare.ProviderAudioPreparationError,
        match="exact container/codec",
    ):
        async with audio_prepare.prepare_provider_audio_file(
            source,
            provider="azure_mai",
            model="mai-transcribe-1.5",
            work_dir=tmp_path,
        ):
            pass

    assert generated_paths and not generated_paths[0].exists()


@pytest.mark.asyncio
async def test_frozen_passthrough_must_match_probed_source(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fixture.mp3"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: _probe(AudioInputFormat.MP3, byte_length=6),
    )
    _capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="deepgram_async",
        model="nova-3",
        probe=_probe(AudioInputFormat.FLAC),
    )
    assert selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH
    with pytest.raises(
        audio_prepare.ProviderAudioPreparationError,
        match="does not match",
    ):
        async with audio_prepare.prepare_provider_audio_file(
            source,
            provider="deepgram_async",
            model="nova-3",
            frozen_selection=selection,
        ):
            pass
