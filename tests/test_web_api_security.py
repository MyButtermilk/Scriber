import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src import web_api
from src.web_api import ScriberWebController


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
    assert web_api._origin_allowed("http://tauri.localhost")
    assert web_api._origin_allowed("https://tauri.localhost")
    assert web_api._origin_allowed("tauri://localhost")
    assert not web_api._origin_allowed("https://evil.example")
    assert not web_api._origin_allowed("http://evil.localhost")
    assert not web_api._origin_allowed("null")


def test_origin_allowed_from_env(monkeypatch):
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "https://example.com, http://localhost:3000")
    assert web_api._origin_allowed("https://example.com")
    assert web_api._origin_allowed("http://localhost:3000")
    assert not web_api._origin_allowed("http://localhost:4000")


def test_origin_allowed_wildcard(monkeypatch):
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "*")
    assert web_api._origin_allowed("https://any.example")


class _FakeTransport:
    def __init__(self, peername=("127.0.0.1", 12345)):
        self._peername = peername

    def get_extra_info(self, name):
        if name == "peername":
            return self._peername
        return None


class _FakeRequest:
    def __init__(self, *, headers=None, query=None, peername=("127.0.0.1", 12345)):
        self.headers = headers or {}
        self.query = query or {}
        self.transport = _FakeTransport(peername)


def test_session_token_accepts_header_authorization_and_query():
    assert web_api._request_has_valid_session_token(
        _FakeRequest(headers={"X-Scriber-Token": "secret"}),
        "secret",
    )
    assert web_api._request_has_valid_session_token(
        _FakeRequest(headers={"Authorization": "Bearer secret"}),
        "secret",
    )
    assert web_api._request_has_valid_session_token(
        _FakeRequest(query={"scriberToken": "secret"}),
        "secret",
    )
    assert not web_api._request_has_valid_session_token(
        _FakeRequest(headers={"X-Scriber-Token": "wrong"}),
        "secret",
    )


def test_request_microphone_refresh_schedules_device_monitor(monkeypatch, tmp_path):
    monkeypatch.setattr(web_api.DeviceMonitor, "start", lambda self: None)
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "0")
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    loop = asyncio.new_event_loop()
    ctl = ScriberWebController(loop)
    called = {"count": 0}

    def request_refresh():
        called["count"] += 1

    monkeypatch.setattr(ctl._device_monitor, "request_refresh", request_refresh)

    try:
        result = ctl.request_microphone_refresh()
    finally:
        loop.close()

    assert result == {"scheduled": True, "deviceMonitor": "running"}
    assert called["count"] == 1


def test_loopback_request_detection():
    assert web_api._is_loopback_request(_FakeRequest(peername=("127.0.0.1", 12345)))
    assert web_api._is_loopback_request(_FakeRequest(peername=("::1", 12345)))
    assert web_api._is_loopback_request(_FakeRequest(peername=("::ffff:127.0.0.1", 12345)))
    assert not web_api._is_loopback_request(_FakeRequest(peername=("10.0.0.2", 12345)))


@pytest.mark.asyncio
async def test_session_token_middleware_and_shutdown_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    ctl = ScriberWebController(asyncio.get_running_loop())
    app = web_api.create_app(ctl)
    shutdown_event = asyncio.Event()
    app["shutdown_event"] = shutdown_event

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        health = await client.get("/api/health")
        assert health.status == 200

        unauthorized = await client.get("/api/runtime")
        assert unauthorized.status == 401

        authorized = await client.get("/api/runtime?scriberToken=secret")
        assert authorized.status == 200
        payload = await authorized.json()
        assert payload["featureFlags"]["sessionTokenRequired"] is True

        frontend_ready_unauthorized = await client.get("/api/runtime/frontend-ready")
        assert frontend_ready_unauthorized.status == 401

        frontend_ready = await client.post(
            "/api/runtime/frontend-ready",
            headers={"X-Scriber-Token": "secret", "Origin": "http://tauri.localhost"},
            json={
                "apiVersion": "1",
                "tauriRuntime": True,
                "backendBaseUrl": "http://127.0.0.1:8765",
                "locationOrigin": "http://tauri.localhost",
                "path": "/",
            },
        )
        assert frontend_ready.status == 200
        frontend_payload = await frontend_ready.json()
        assert frontend_payload["ready"] is True
        assert frontend_payload["lastSeen"]["tauriRuntime"] is True
        assert frontend_payload["lastSeen"]["backendBaseUrl"] == "http://127.0.0.1:8765"
        assert frontend_payload["lastSeen"]["origin"] == "http://tauri.localhost"

        frontend_ready_invalid = await client.post(
            "/api/runtime/frontend-ready",
            headers={"X-Scriber-Token": "secret", "Origin": "http://tauri.localhost"},
            json={
                "tauriRuntime": True,
                "backendBaseUrl": "http://127.0.0.1:8765",
                "locationOrigin": "http://tauri.localhost",
                "path": "/",
            },
        )
        assert frontend_ready_invalid.status == 400

        frontend_ready_get = await client.get(
            "/api/runtime/frontend-ready?scriberToken=secret",
            headers={"Origin": "http://tauri.localhost"},
        )
        assert frontend_ready_get.status == 200
        assert (await frontend_ready_get.json())["lastSeen"]["locationOrigin"] == "http://tauri.localhost"

        support_unauthorized = await client.post("/api/runtime/support-bundle")
        assert support_unauthorized.status == 401

        support = await client.post("/api/runtime/support-bundle", headers={"X-Scriber-Token": "secret"})
        assert support.status == 200
        assert (await support.read()).startswith(b"PK")

        shutdown_unauthorized = await client.post("/api/runtime/shutdown")
        assert shutdown_unauthorized.status == 401

        shutdown = await client.post("/api/runtime/shutdown", headers={"X-Scriber-Token": "secret"})
        assert shutdown.status == 200
        assert shutdown_event.is_set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_static_frontend_routes_do_not_bypass_api_session_token(monkeypatch, tmp_path):
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<html><body>Scriber App</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('scriber')", encoding="utf-8")

    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_FRONTEND_DIST_DIR", str(frontend))

    ctl = ScriberWebController(asyncio.get_running_loop())
    app = web_api.create_app(ctl)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        index = await client.get("/")
        assert index.status == 200
        assert "Scriber App" in await index.text()

        spa_route = await client.get("/settings")
        assert spa_route.status == 200
        assert "Scriber App" in await spa_route.text()

        asset = await client.get("/assets/app.js")
        assert asset.status == 200
        assert "scriber" in await asset.text()

        missing_asset = await client.get("/assets/missing.js")
        assert missing_asset.status == 404

        api_without_token = await client.get("/api/runtime")
        assert api_without_token.status == 401

        api_with_token = await client.get("/api/runtime", headers={"X-Scriber-Token": "secret"})
        assert api_with_token.status == 200
    finally:
        await client.close()
        ctl.shutdown()


@pytest.mark.asyncio
async def test_tauri_origin_can_fetch_health(monkeypatch, tmp_path):
    monkeypatch.delenv("SCRIBER_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")

    ctl = ScriberWebController(asyncio.get_running_loop())
    app = web_api.create_app(ctl)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/api/health", headers={"Origin": "http://tauri.localhost"})
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == "http://tauri.localhost"
        assert response.headers["Access-Control-Allow-Credentials"] == "true"
    finally:
        await client.close()
        ctl.shutdown()


@pytest.mark.asyncio
async def test_tauri_origin_can_post_frontend_ready(monkeypatch, tmp_path):
    monkeypatch.delenv("SCRIBER_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")

    ctl = ScriberWebController(asyncio.get_running_loop())
    app = web_api.create_app(ctl)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post(
            "/api/runtime/frontend-ready?scriberToken=secret",
            headers={"Origin": "http://tauri.localhost"},
            json={
                "apiVersion": "1",
                "tauriRuntime": True,
                "backendBaseUrl": "http://127.0.0.1:8765",
                "locationOrigin": "http://tauri.localhost",
                "path": "/",
            },
        )
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == "http://tauri.localhost"
        payload = await response.json()
        assert payload["ready"] is True
        assert payload["lastSeen"]["tauriRuntime"] is True
    finally:
        await client.close()
        ctl.shutdown()


def test_frontend_file_for_request_blocks_path_traversal(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "index.html").write_text("index", encoding="utf-8")

    assert web_api._frontend_file_for_request(root, "/../secret.txt") is None
    assert web_api._frontend_file_for_request(root, "/nested/route") == root / "index.html"


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
