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
