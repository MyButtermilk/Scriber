import pytest

from src import web_api


def test_safe_upload_filename_strips_dirs():
    assert web_api._safe_upload_filename("subdir/evil.mp3") == "evil.mp3"


def test_safe_upload_filename_sanitizes_invalid_chars():
    out = web_api._safe_upload_filename("bad<name>.mp3")
    assert "<" not in out
    assert ">" not in out


def test_origin_allowed_defaults(monkeypatch):
    monkeypatch.delenv("SCRIBER_ALLOWED_ORIGINS", raising=False)
    assert web_api._origin_allowed("http://localhost:3000")
    assert web_api._origin_allowed("http://127.0.0.1:1234")
    assert web_api._origin_allowed("http://[::1]:5173")
    assert not web_api._origin_allowed("https://evil.example")
    assert not web_api._origin_allowed("null")


def test_origin_allowed_from_env(monkeypatch):
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "https://example.com, http://localhost:3000")
    assert web_api._origin_allowed("https://example.com")
    assert web_api._origin_allowed("http://localhost:3000")
    assert not web_api._origin_allowed("http://localhost:4000")


def test_origin_allowed_wildcard(monkeypatch):
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "*")
    assert web_api._origin_allowed("https://any.example")


def test_upload_max_bytes_env(monkeypatch):
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_BYTES", "123")
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_upload_max_bytes() == 123

    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_MB", "1")
    assert web_api._get_upload_max_bytes() == 1024 * 1024


def test_audio_upload_max_bytes_defaults_to_soniox_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("soniox") == 524_288_000
    assert web_api._get_audio_upload_max_bytes("soniox_async") == 524_288_000


def test_audio_upload_max_bytes_defaults_to_mistral_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("mistral") == 512 * 1024 * 1024
    assert web_api._get_audio_upload_limit_label("mistral") == "512MB"


def test_audio_upload_max_bytes_defaults_to_assemblyai_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("assemblyai") == 2_200_000_000
    assert web_api._get_audio_upload_limit_label("assemblyai") == "2.2GB"


def test_audio_upload_max_bytes_defaults_to_smallest_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("smallest") == 25 * 1024 * 1024
    assert web_api._get_audio_upload_max_bytes("smallest_async") == 25 * 1024 * 1024
    assert web_api._get_audio_upload_limit_label("smallest") == "25MB"


def test_audio_upload_max_bytes_defaults_to_azure_mai_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("azure_mai") == 70 * 1024 * 1024
    assert web_api._get_audio_upload_limit_label("azure_mai") == "70MB"


def test_audio_upload_max_bytes_uses_generic_default_for_other_providers(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_upload_max_bytes("openai") == 2048 * 1024 * 1024


def test_audio_upload_max_bytes_respects_env_override_for_soniox(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_MB", "300")
    assert web_api._get_audio_upload_max_bytes("soniox") == 300 * 1024 * 1024


def test_audio_ingest_max_bytes_allows_precompression_uploads():
    assert web_api._get_audio_ingest_max_bytes("soniox") == 2048 * 1024 * 1024


def test_audio_ingest_max_bytes_expands_for_larger_provider_limit(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    assert web_api._get_audio_ingest_max_bytes("assemblyai") == 2_200_000_000


def test_build_file_upload_limits_uses_provider_metadata(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    limits = web_api._build_file_upload_limits("mistral")
    assert limits["provider"] == "mistral"
    assert limits["audioMaxLabel"] == "512MB"
    assert limits["providerLabel"] == "Mistral (Realtime)"


def test_build_file_upload_limits_uses_smallest_compression_threshold(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)
    limits = web_api._build_file_upload_limits("smallest")
    assert limits["provider"] == "smallest"
    assert limits["audioMaxLabel"] == "25MB"
    assert limits["compressionThresholdLabel"] == "25MB"


@pytest.mark.asyncio
async def test_maybe_compress_audio_upload_skips_small_files(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "_UPLOAD_COMPRESSION_THRESHOLD_BYTES", 2048)
    upload_path = tmp_path / "small.mp3"
    upload_path.write_bytes(b"x" * 1024)

    got = await web_api._maybe_compress_audio_upload(upload_path)

    assert got == upload_path


@pytest.mark.asyncio
async def test_maybe_compress_audio_upload_uses_provider_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "_UPLOAD_COMPRESSION_THRESHOLD_BYTES", 10_000)
    upload_path = tmp_path / "over-provider-limit.mp3"
    upload_path.write_bytes(b"x" * 4096)

    async def _fake_transcode(source_path, target_path, *, bitrate):
        assert source_path == upload_path
        assert bitrate == web_api._COMPRESSED_AUDIO_BITRATE
        target_path.write_bytes(b"y" * 1024)
        return target_path

    monkeypatch.setattr(web_api, "_transcode_media_to_webm_audio", _fake_transcode)

    got = await web_api._maybe_compress_audio_upload(upload_path, max_bytes=2048)

    assert got.suffix == ".webm"
    assert got.exists()


@pytest.mark.asyncio
async def test_maybe_compress_audio_upload_replaces_large_audio_with_webm(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "_UPLOAD_COMPRESSION_THRESHOLD_BYTES", 2048)
    upload_path = tmp_path / "large.mp3"
    upload_path.write_bytes(b"x" * 4096)

    async def _fake_transcode(source_path, target_path, *, bitrate):
        assert source_path == upload_path
        assert target_path.suffix == ".webm"
        assert bitrate == web_api._COMPRESSED_AUDIO_BITRATE
        target_path.write_bytes(b"y" * 2048)
        return target_path

    monkeypatch.setattr(web_api, "_transcode_media_to_webm_audio", _fake_transcode)

    got = await web_api._maybe_compress_audio_upload(upload_path)

    assert got.suffix == ".webm"
    assert got.exists()
    assert not upload_path.exists()


@pytest.mark.asyncio
async def test_maybe_compress_audio_upload_keeps_original_when_not_smaller(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api, "_UPLOAD_COMPRESSION_THRESHOLD_BYTES", 2048)
    upload_path = tmp_path / "large.wav"
    upload_path.write_bytes(b"x" * 4096)

    async def _fake_transcode(_source_path, target_path, *, bitrate):
        assert bitrate == web_api._COMPRESSED_AUDIO_BITRATE
        target_path.write_bytes(b"y" * 8192)
        return target_path

    monkeypatch.setattr(web_api, "_transcode_media_to_webm_audio", _fake_transcode)

    got = await web_api._maybe_compress_audio_upload(upload_path)

    assert got == upload_path
    assert upload_path.exists()


def test_allowed_upload_extensions_include_video_extensions():
    assert web_api._VIDEO_EXTENSIONS.issubset(web_api._ALLOWED_UPLOAD_EXTENSIONS)


def test_validate_default_stt_service_accepts_known():
    assert web_api._validate_default_stt_service(" OpenAI ") == "openai"


def test_validate_default_stt_service_rejects_unknown():
    with pytest.raises(ValueError):
        web_api._validate_default_stt_service("not-a-provider")


def test_validate_summarization_model_accepts_known_prefixes():
    assert web_api._validate_summarization_model("gemini-flash-latest") == "gemini-flash-latest"
    assert web_api._validate_summarization_model("gemini-3.5-flash") == "gemini-3.5-flash"
    assert web_api._validate_summarization_model("gpt-5-mini") == "gpt-5-mini"


def test_validate_summarization_model_rejects_invalid_prefix():
    with pytest.raises(ValueError):
        web_api._validate_summarization_model("claude-3-opus")


def test_validate_summarization_model_rejects_invalid_chars():
    with pytest.raises(ValueError):
        web_api._validate_summarization_model("gpt-5-mini;rm")
