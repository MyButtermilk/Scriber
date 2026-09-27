from __future__ import annotations

import json
from dataclasses import replace
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
        model="MAI-Transcribe-2",
        probe=_probe(AudioInputFormat.MP3),
    )
    assert capability.provider == "azure_mai"
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


@pytest.mark.asyncio
async def test_mai_tagged_mp3_remux_preserves_audio_and_cleans_copy(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "podcast.mp3"
    source.write_bytes(b"ID3metadata-audio")
    tagged = replace(_probe(AudioInputFormat.MP3), has_id3_metadata=True)
    monkeypatch.setattr(
        audio_prepare, "probe_audio_input_file", lambda path: tagged if path == source else _probe(AudioInputFormat.MP3)
    )
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")
    commands = []

    async def remux(command, target):
        commands.append(command)
        target.write_bytes(b"audio")

    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", remux)
    _, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai", model="MAI-Transcribe-2", probe=tagged
    )
    assert selection.mode == AudioSelectionMode.AUDIO_ONLY_REMUX
    async with audio_prepare.prepare_provider_audio_file(
        source, provider="azure_mai", model="MAI-Transcribe-2", frozen_selection=selection
    ) as prepared:
        output = prepared.path
        assert output.read_bytes() == b"audio"
        assert prepared.implementation == "ffmpeg_mp3_audio_only_remux"
        assert prepared.generated
    assert not output.exists()
    assert source.read_bytes() == b"ID3metadata-audio"
    assert commands[0][commands[0].index("-c:a") + 1] == "copy"
    assert commands[0][commands[0].index("-id3v2_version") + 1] == "0"
    _, other = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai", model="mai-transcribe-1.5", probe=tagged
    )
    assert other.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


def test_azure_unaccepted_source_retains_promoted_mp3_control() -> None:
    _capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai",
        model="MAI-Transcribe-2",
        probe=_probe(AudioInputFormat.OGG_OPUS),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


@pytest.mark.parametrize("source_format", [AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC])
def test_azure_non_mp3_sources_retain_promoted_mp3_control(source_format) -> None:
    _capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="azure_mai",
        model="MAI-Transcribe-2",
        probe=_probe(source_format),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


def test_selection_fails_closed_for_custom_endpoint() -> None:
    with pytest.raises(UnsupportedProviderAudioRoute):
        audio_prepare.resolve_provider_audio_selection(
            provider="azure_mai",
            model="MAI-Transcribe-2",
            probe=_probe(AudioInputFormat.MP3),
            custom_endpoint=True,
        )


@pytest.mark.asyncio
async def test_generated_preparation_is_cleaned(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "fixture.ogg"
    source.write_bytes(b"source")

    def probe(path: Path):
        return _probe(
            AudioInputFormat.OGG_OPUS if Path(path) == source else AudioInputFormat.MP3,
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
        model="MAI-Transcribe-2",
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
            AudioInputFormat.OGG_OPUS if Path(path) == source else AudioInputFormat.WAV_PCM16,
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
            model="MAI-Transcribe-2",
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
        probe=_probe(AudioInputFormat.OGG_OPUS),
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


@pytest.mark.parametrize("model", ["microsoft/mai-transcribe-2", "microsoft/mai-transcribe-1.5"])
@pytest.mark.parametrize(
    "source_format,source_bytes",
    [
        (AudioInputFormat.WEBM_OPUS, 17_300_000),
        (AudioInputFormat.WAV_PCM16, 61_942_444),
        (AudioInputFormat.MP3, 31_000_000),
    ],
)
def test_openrouter_long_import_uses_compact_mp3(model, source_format, source_bytes):
    probe = replace(_probe(source_format, byte_length=source_bytes), duration_ms=1_935_700)
    capability, selection = audio_prepare.resolve_provider_audio_selection(
        provider="openrouter_stt",
        model=model,
        probe=probe,
    )
    assert capability.max_upload_bytes == 18_000_000
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


@pytest.mark.parametrize("model", ["microsoft/mai-transcribe-2", "microsoft/mai-transcribe-1.5"])
@pytest.mark.parametrize("source_bytes", [128, 18_000_000])
def test_openrouter_mp3_within_budget_remains_unchanged(model, source_bytes):
    _, selection = audio_prepare.resolve_provider_audio_selection(
        provider="openrouter_stt",
        model=model,
        probe=_probe(AudioInputFormat.MP3, byte_length=source_bytes),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


@pytest.mark.parametrize("model", ["microsoft/mai-transcribe-2", "microsoft/mai-transcribe-1.5"])
@pytest.mark.parametrize(
    "source_format",
    [AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC, AudioInputFormat.WEBM_OPUS, AudioInputFormat.M4A_AAC],
)
@pytest.mark.parametrize("duration_ms", [None, 1_000, 1_935_700])
def test_openrouter_non_mp3_uses_mp3_even_below_budget_at_any_duration(model, source_format, duration_ms):
    _, selection = audio_prepare.resolve_provider_audio_selection(
        provider="openrouter_stt",
        model=model,
        probe=replace(_probe(source_format), duration_ms=duration_ms),
    )
    assert selection.audio_format == AudioInputFormat.MP3
    assert selection.mode == AudioSelectionMode.GENERATED


@pytest.mark.asyncio
async def test_openrouter_preparation_rejects_oversize_output_and_cleans_only_generated_file(monkeypatch, tmp_path):
    from src.core.provider_errors import ProviderTransportError

    source = tmp_path / "recording.webm"
    source.write_bytes(b"original")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda path: _probe(
            AudioInputFormat.WEBM_OPUS if path == source else AudioInputFormat.MP3,
        ),
    )
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")
    generated = []

    async def prepare(command, target):
        assert "libmp3lame" in command
        generated.append(target)
        with target.open("wb") as output:
            output.truncate(18_000_001)

    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", prepare)
    with pytest.raises(ProviderTransportError) as caught:
        async with audio_prepare.prepare_provider_audio_file(
            source,
            provider="openrouter_stt",
            model="microsoft/mai-transcribe-2",
        ):
            pytest.fail("Oversize audio must never reach the provider")
    assert caught.value.code == "audio_limit_exceeded"
    assert caught.value.retryable is False
    assert generated and not generated[0].exists()
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_openrouter_frozen_oversize_wav_fails_without_silently_changing_route(monkeypatch, tmp_path):
    from src.core.provider_audio_formats import AudioInputSelection
    from src.core.provider_errors import ProviderTransportError

    source = tmp_path / "legacy.wav"
    with source.open("wb") as output:
        output.truncate(61_942_444)
    probe = _probe(AudioInputFormat.WAV_PCM16, byte_length=source.stat().st_size)
    monkeypatch.setattr(audio_prepare, "probe_audio_input_file", lambda _path: probe)
    capability, _ = audio_prepare.resolve_provider_audio_selection(
        provider="openrouter_stt",
        model="microsoft/mai-transcribe-1.5",
        probe=probe,
    )
    frozen = AudioInputSelection(
        audio_format=AudioInputFormat.WAV_PCM16,
        mode=AudioSelectionMode.ORIGINAL_PASSTHROUGH,
        capability_id=capability.capability_id,
        capability_revision=capability.revision,
    )
    with pytest.raises(ProviderTransportError, match="audio_limit_exceeded"):
        async with audio_prepare.prepare_provider_audio_file(
            source,
            provider="openrouter_stt",
            model="microsoft/mai-transcribe-1.5",
            frozen_selection=frozen,
        ):
            pytest.fail("Frozen oversize input must fail before upload")
    assert source.exists()
    assert list(tmp_path.iterdir()) == [source]
