from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import audio_prepare
from src.config import Config
from src.core.provider_audio_formats import AudioInputFormat, AudioSelectionMode
from src.pipeline import ScriberPipeline
from src.transcript_artifacts import freeze_provider_route


class _RecordingProviderTransport:
    def __init__(self) -> None:
        self.providers: list[str] = []

    async def session_view(self, *, provider: str, marker=None):
        self.providers.append(provider)
        if marker is not None:
            marker("request_started", timestamp_ns=1)
            marker("first_request_chunk_sent", timestamp_ns=2)
        return object()


class _RequestStartOnlyTransport:
    async def session_view(self, *, provider: str, marker=None):
        if marker is not None:
            marker("request_started", timestamp_ns=1)
        return object()


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


def _frozen_route(provider: str, *, audio_format: AudioInputFormat | None = None):
    return freeze_provider_route(
        workload="file",
        provider=provider,
        audio_input_format=audio_format,
    ).execution_route()


@pytest.mark.asyncio
async def test_openai_flac_is_prepared_as_wav_before_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flac"
    source.write_bytes(b"original-flac")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "configured")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda path: _probe(
            AudioInputFormat.FLAC
            if Path(path) == source
            else AudioInputFormat.WAV_PCM16,
            byte_length=Path(path).stat().st_size,
        ),
    )
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")

    async def _generate(_command: list[str], target: Path) -> None:
        target.write_bytes(b"prepared-wav")

    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", _generate)
    captured: dict[str, object] = {}

    async def _transcribe(**kwargs):
        captured["filename"] = kwargs["filename"]
        captured["content_type"] = kwargs["content_type"]
        captured["body"] = kwargs["audio_source"].read()
        return {"text": "done"}

    monkeypatch.setattr(
        "src.pipeline.transcribe_with_openai_audio_transcription",
        _transcribe,
    )
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="openai_async",
        execution_route=_frozen_route("openai_async"),
        provider_http_transport=transport,
    )

    await pipeline.transcribe_file_direct(str(source))

    assert str(captured["filename"]).endswith(".wav")
    assert captured["content_type"] == "audio/wav"
    assert captured["body"] == b"prepared-wav"
    assert source.read_bytes() == b"original-flac"
    assert transport.providers == ["openai_async"]
    assert pipeline._provider_request_started is True


@pytest.mark.asyncio
async def test_azure_mp3_is_passed_through_without_second_preparation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-mp3")
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "configured")
    monkeypatch.setattr(Config, "AZURE_MAI_REGION", "northeurope")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: _probe(AudioInputFormat.MP3, byte_length=source.stat().st_size),
    )

    def _unexpected_legacy_preparation(_path):
        raise AssertionError("Azure legacy preparation must not run")

    monkeypatch.setattr(
        "src.pipeline.prepared_azure_mai_audio_file",
        _unexpected_legacy_preparation,
    )
    captured: dict[str, object] = {}

    async def _transcribe(**kwargs):
        captured["filename"] = kwargs["filename"]
        captured["content_type"] = kwargs["content_type"]
        captured["body"] = kwargs["audio_source"].read()
        return {"combinedPhrases": [{"text": "done"}]}

    monkeypatch.setattr("src.pipeline.transcribe_with_azure_mai", _transcribe)
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route=_frozen_route("azure_mai"),
        provider_http_transport=transport,
    )

    await pipeline.transcribe_file_direct(str(source))

    assert captured == {
        "filename": "source.mp3",
        "content_type": "audio/mpeg",
        "body": b"original-mp3",
    }
    assert transport.providers == ["azure_mai"]
    assert pipeline._provider_request_started is True
    assert pipeline._provider_request_state == "result_received"
    pipeline.mark_provider_result_durable()
    assert pipeline._provider_request_state == "result_durable"


@pytest.mark.asyncio
async def test_failure_before_first_request_chunk_remains_safe_to_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-mp3")
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "configured")
    monkeypatch.setattr(Config, "AZURE_MAI_REGION", "northeurope")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: _probe(AudioInputFormat.MP3, byte_length=source.stat().st_size),
    )

    async def fail_before_body(**_kwargs):
        raise ConnectionError("synthetic connect failure")

    monkeypatch.setattr("src.pipeline.transcribe_with_azure_mai", fail_before_body)
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route=_frozen_route("azure_mai"),
        provider_http_transport=_RequestStartOnlyTransport(),
    )

    with pytest.raises(ConnectionError, match="connect failure"):
        await pipeline.transcribe_file_direct(str(source))

    assert pipeline._provider_request_state == "request_started"
    assert pipeline._provider_request_started is False


@pytest.mark.asyncio
async def test_external_prepared_audio_is_borrowed_without_probe_or_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.flac"
    original.write_bytes(b"original")
    prepared_path = tmp_path / "prepared.wav"
    prepared_path.write_bytes(b"prepared")
    route = _frozen_route("azure_mai", audio_format=AudioInputFormat.WAV_PCM16)
    prepared = audio_prepare.PreparedProviderAudio(
        path=prepared_path,
        source_format=AudioInputFormat.FLAC,
        selected_format=AudioInputFormat.WAV_PCM16,
        selection_mode=AudioSelectionMode.GENERATED,
        implementation="ffmpeg_wav_pcm16_control",
        content_type="audio/wav",
        capability_id=route["provider_audio_capability_id"],
        capability_revision=route["provider_audio_capability_revision"],
        byte_length=prepared_path.stat().st_size,
        generated=True,
    )
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "configured")
    monkeypatch.setattr(Config, "AZURE_MAI_REGION", "northeurope")

    def _unexpected_preparation(*_args, **_kwargs):
        raise AssertionError("prepared audio must not be prepared twice")

    monkeypatch.setattr(
        "src.pipeline.prepare_provider_audio_file",
        _unexpected_preparation,
    )
    monkeypatch.setattr(
        "src.pipeline.prepared_azure_mai_audio_file",
        _unexpected_preparation,
    )
    captured: dict[str, object] = {}

    async def _transcribe(**kwargs):
        captured["filename"] = kwargs["filename"]
        captured["content_type"] = kwargs["content_type"]
        captured["body"] = kwargs["audio_source"].read()
        return {"combinedPhrases": [{"text": "done"}]}

    monkeypatch.setattr("src.pipeline.transcribe_with_azure_mai", _transcribe)
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route=route,
    )

    await pipeline.transcribe_file_direct(
        str(original),
        prepared_audio=prepared,
    )

    assert captured == {
        "filename": "prepared.wav",
        "content_type": "audio/wav",
        "body": b"prepared",
    }
    assert prepared_path.read_bytes() == b"prepared"


@pytest.mark.parametrize(
    ("suffix", "container_name", "label"),
    [
        (".ogg", "ogg", "OGG"),
        (".webm", "matroska,webm", "WebM"),
    ],
)
@pytest.mark.asyncio
async def test_generic_container_codec_mismatch_fails_before_provider_session(
    monkeypatch,
    tmp_path: Path,
    suffix: str,
    container_name: str,
    label: str,
) -> None:
    source = tmp_path / f"mismatch{suffix}"
    source.write_bytes(b"not-the-claimed-codec")
    monkeypatch.setattr(Config, "OPENAI_API_KEY", "configured")
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffprobe")
    probe_payload = {
        "streams": [{"codec_name": "aac", "codec_type": "audio"}],
        "format": {"format_name": container_name, "duration": "1.0"},
    }
    monkeypatch.setattr(
        audio_prepare.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(probe_payload).encode("utf-8"),
            stderr=b"",
        ),
    )
    provider_called = False

    async def _transcribe(**_kwargs):
        nonlocal provider_called
        provider_called = True
        return {"text": "unexpected"}

    monkeypatch.setattr(
        "src.pipeline.transcribe_with_openai_audio_transcription",
        _transcribe,
    )
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="openai_async",
        execution_route=_frozen_route("openai_async"),
        provider_http_transport=transport,
    )

    with pytest.raises(audio_prepare.AudioFormatProbeError, match=label):
        await pipeline.transcribe_file_direct(str(source))

    assert provider_called is False
    assert transport.providers == []
    assert pipeline._provider_request_started is False
    assert pipeline.is_active is False


@pytest.mark.asyncio
async def test_stale_capability_revision_fails_before_probe_or_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    route = _frozen_route("azure_mai")
    route["provider_audio_capability_revision"] = "stale-revision"
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route=route,
        provider_http_transport=transport,
    )

    with pytest.raises(
        audio_prepare.ProviderAudioPreparationError,
        match="active registry",
    ):
        await pipeline.transcribe_file_direct(str(source))

    assert transport.providers == []
    assert pipeline._provider_request_started is False


@pytest.mark.asyncio
async def test_unknown_frozen_direct_upload_route_fails_before_probe_or_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="soniox",
        execution_route={
            "provider": "soniox",
            "model": "unverified-custom-model",
            "transport": "direct_upload",
            "provider_route": "async_transcription",
        },
        provider_http_transport=transport,
    )

    with pytest.raises(
        audio_prepare.ProviderAudioPreparationError,
        match="no verified provider audio capability",
    ):
        await pipeline.transcribe_file_direct(str(source))

    assert transport.providers == []
    assert pipeline._provider_request_started is False


@pytest.mark.asyncio
async def test_frozen_exact_format_must_match_prepared_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flac"
    source.write_bytes(b"flac")
    route = _frozen_route("azure_mai", audio_format=AudioInputFormat.WAV_PCM16)
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda path: _probe(
            AudioInputFormat.FLAC
            if Path(path) == source
            else AudioInputFormat.MP3,
            byte_length=Path(path).stat().st_size,
        ),
    )
    monkeypatch.setattr(audio_prepare, "require_media_tool", lambda _tool: "ffmpeg")

    async def generate(_command: list[str], target: Path) -> None:
        target.write_bytes(b"prepared-mp3")

    monkeypatch.setattr(audio_prepare, "_run_generated_preparation", generate)
    transport = _RecordingProviderTransport()
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        execution_route=route,
        provider_http_transport=transport,
    )

    with pytest.raises(
        audio_prepare.ProviderAudioPreparationError,
        match="frozen exact audio format",
    ):
        await pipeline.transcribe_file_direct(str(source))

    assert transport.providers == []
    assert pipeline._provider_request_started is False
