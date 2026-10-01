import hashlib
import io
import shutil
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from src import audio_prepare, cloud_async_stt
from src.core.provider_audio_formats import AudioInputFormat, AudioSelectionMode
from src.runtime.media_tools import find_media_tool


def test_eighteen_mib_mp3_is_not_reencoded_before_upload():
    probe = audio_prepare.ProbedAudioInput(
        audio_format=AudioInputFormat.MP3,
        container_name="mp3",
        codec_name="mp3",
        sample_rate=16_000,
        channels=1,
        duration_ms=3_106_000,
        byte_length=18 * 1024 * 1024,
    )
    _, selected = audio_prepare.resolve_provider_audio_selection(
        provider="openrouter_stt", model="microsoft/mai-transcribe-2", probe=probe
    )
    assert selected.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH
    assert selected.audio_format == AudioInputFormat.MP3


@pytest.mark.asyncio
async def test_eighteen_mib_mp3_reaches_http_byte_for_byte_without_base64(monkeypatch):
    size = 18 * 1024 * 1024
    audio = (b"MP3-fixture-" * (size // len(b"MP3-fixture-") + 1))[:size]
    source = io.BytesIO(audio)
    received = {}

    async def accept(request):
        assert request.content_type == "multipart/form-data"
        received["request_bytes"] = request.content_length
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file":
                digest = hashlib.sha256()
                size = 0
                while chunk := await part.read_chunk():
                    digest.update(chunk)
                    size += len(chunk)
                received["audio"] = (size, digest.hexdigest(), part.filename, part.headers["Content-Type"])
            else:
                received[part.name] = await part.text()
        return web.json_response({"text": "Uploaded MP3"})

    app = web.Application(client_max_size=26_000_000)
    app.router.add_post("/transcriptions", accept)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        cloud_async_stt, "OPENROUTER_STT_URL", f"http://127.0.0.1:{runner.addresses[0][1]}/transcriptions"
    )
    try:
        async with aiohttp.ClientSession() as session:
            result = await cloud_async_stt.transcribe_with_openrouter_audio_transcription(
                session=session,
                api_key="local-test-only",
                audio_source=source,
                filename="private-meeting.mp3",
                content_type="audio/mpeg",
                language="de",
            )
        assert result == {"text": "Uploaded MP3"}
        assert received["audio"] == (len(audio), hashlib.sha256(audio).hexdigest(), "audio.mp3", "audio/mpeg")
        assert len(audio) < received["request_bytes"] < len(audio) + 4096
        assert received["model"] == "microsoft/mai-transcribe-2"
        assert received["language"] == "de"
        assert received["response_format"] == "json"
        assert received["temperature"] == "0"
        assert not source.closed
    finally:
        source.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_video_extraction_preparation_and_upload_contain_real_mp3(monkeypatch, tmp_path):
    from src.api.file_transcription_routes import extract_audio_from_video

    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    source = tmp_path / "video.mp4"
    # Synthetic two-second black video + 440-Hz tone. A checked-in fixture
    # keeps this gate runnable with the shipped audio-only Profile B build.
    shutil.copyfile(Path(__file__).parents[1] / "fixtures/audio/openrouter-video.mp4", source)
    extracted = await extract_audio_from_video(source, tmp_path)
    captured = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"text":"Video audio uploaded"}'

    class Session:
        def post(self, _url, **kwargs):
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {kwargs['headers']['Content-Type']}\r\n\r\n".encode() + kwargs["data"].read()
            )
            part = next(part for part in message.iter_parts() if part.get_filename())
            captured["bytes"] = part.get_payload(decode=True)
            assert part.get_content_type() == "audio/mpeg"
            return Response()

    async with audio_prepare.prepare_provider_audio_file(
        extracted, provider="openrouter_stt", model="microsoft/mai-transcribe-2"
    ) as prepared:
        assert prepared.selected_format == AudioInputFormat.MP3
        expected = prepared.path.read_bytes()
        with prepared.path.open("rb") as audio:
            await cloud_async_stt.transcribe_with_openrouter_audio_transcription(
                session=Session(),
                api_key="local-test-only",
                audio_source=audio,
                filename=prepared.path.name,
                content_type=prepared.content_type,
                language="de",
            )
        assert captured["bytes"] == expected
        generated = prepared.path
    assert not generated.exists()
    uploaded = tmp_path / "uploaded.mp3"
    uploaded.write_bytes(captured["bytes"])
    probe = audio_prepare.probe_audio_input_file(uploaded)
    assert probe.audio_format == AudioInputFormat.MP3
    assert probe.channels == 1
    assert 1_950 <= probe.duration_ms <= 2_250
