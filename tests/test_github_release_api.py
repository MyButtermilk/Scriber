from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import github_release_api
from scripts.ci.github_release_api import GitHubApi, GitHubApiError


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.status = status
        self._stream = BytesIO(payload)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._stream.read(limit)


def test_github_api_requires_a_token() -> None:
    with pytest.raises(ValueError, match="token is required"):
        GitHubApi(token="")


def test_github_api_rejects_oversized_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"x" * (github_release_api.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        github_release_api,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(GitHubApiError, match="response-size limit"):
        GitHubApi(token="test-token").get_json("/repos/example/project/git/ref/heads/main")


def test_github_api_does_not_expose_token_in_validation_errors() -> None:
    token = "private-release-token"
    api = GitHubApi(token=token)

    with pytest.raises(ValueError) as caught:
        api.get_json("relative/path")

    assert token not in str(caught.value)


def test_github_api_keeps_authorization_out_of_redirected_headers() -> None:
    api = GitHubApi(token="private-release-token")

    request = api._build_request(
        "/repos/example/project/releases/assets/42",
        method="GET",
        accept="application/octet-stream",
    )

    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Bearer private-release-token"


def test_github_api_streams_and_hashes_release_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"release asset bytes"
    monkeypatch.setattr(
        github_release_api,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    destination = tmp_path / "asset.exe"

    report = GitHubApi(token="test-token").download_asset(
        "/repos/example/project/releases/assets/42",
        destination=destination,
        max_bytes=len(payload),
    )

    assert destination.read_bytes() == payload
    assert report == {
        "sizeBytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
    }


def test_github_api_rejects_oversized_asset_without_publishing_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        github_release_api,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"too large"),
    )
    destination = tmp_path / "asset.exe"

    with pytest.raises(GitHubApiError, match="download-size limit"):
        GitHubApi(token="test-token").download_asset(
            "/repos/example/project/releases/assets/42",
            destination=destination,
            max_bytes=2,
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_github_api_posts_and_patches_bounded_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    def fake_urlopen(request: Any, **_kwargs: object) -> _Response:
        requests.append(request)
        status = 200 if request.full_url.endswith("/generate-notes") else (201 if request.method == "POST" else 200)
        return _Response(b'{"id":42}', status=status)

    monkeypatch.setattr(github_release_api, "urlopen", fake_urlopen)
    api = GitHubApi(token="test-token")

    assert api.post_json("/repos/example/project/releases", {"draft": True}) == {"id": 42}
    assert api.post_json(
        "/repos/example/project/releases/generate-notes",
        {"tag_name": "v1.2.3", "target_commitish": "a" * 40},
        expected_status=200,
    ) == {"id": 42}
    assert api.patch_json("/repos/example/project/releases/42", {"draft": False}) == {"id": 42}

    assert [request.method for request in requests] == ["POST", "POST", "PATCH"]
    assert [request.data for request in requests] == [
        b'{"draft":true}',
        (b'{"tag_name":"v1.2.3","target_commitish":"' + (b"a" * 40) + b'"}'),
        b'{"draft":false}',
    ]
    assert all("Authorization" not in request.headers for request in requests)


def test_github_api_rejects_oversized_json_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_urlopen(*_args: object, **_kwargs: object) -> _Response:
        raise AssertionError("Network must not be reached.")

    monkeypatch.setattr(github_release_api, "urlopen", unexpected_urlopen)

    with pytest.raises(ValueError, match="JSON-size limit"):
        GitHubApi(token="test-token").post_json(
            "/repos/example/project/releases",
            {"body": "x" * github_release_api.MAX_JSON_REQUEST_BYTES},
        )

    with pytest.raises(ValueError, match="status must be 200 or 201"):
        GitHubApi(token="test-token").post_json(
            "/repos/example/project/releases/generate-notes",
            {"tag_name": "v1.2.3"},
            expected_status=202,
        )


def test_github_api_uploads_streamed_asset_only_to_bound_numeric_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"bounded upload bytes"
    source = tmp_path / "Scriber setup.exe"
    source.write_bytes(content)
    captured: dict[str, object] = {}

    def fake_urlopen(request: Any, **_kwargs: object) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["unredirected"] = request.unredirected_hdrs
        captured["body"] = b"".join(request.data)
        return _Response(
            (
                '{"id":99,"name":"Scriber setup.exe","state":"uploaded",'
                f'"size":{len(content)},"digest":"sha256:{sha256(content).hexdigest()}"'
                "}"
            ).encode(),
            status=201,
        )

    monkeypatch.setattr(github_release_api, "urlopen", fake_urlopen)
    response = GitHubApi(token="private-release-token").upload_asset(
        "https://uploads.github.com/repos/example/project/releases/42/assets{?name,label}",
        repository="example/project",
        release_id=42,
        asset_name=source.name,
        source=source,
        expected_size=len(content),
        expected_sha256=sha256(content).hexdigest(),
    )

    assert response["id"] == 99
    assert captured["url"] == (
        "https://uploads.github.com/repos/example/project/releases/42/assets?name=Scriber%20setup.exe"
    )
    assert captured["body"] == content
    assert "Authorization" not in captured["headers"]
    assert captured["unredirected"]["Authorization"] == "Bearer private-release-token"


def test_github_api_rejects_upload_url_for_different_release_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "asset.bin"
    source.write_bytes(b"x")

    def unexpected_urlopen(*_args: object, **_kwargs: object) -> _Response:
        raise AssertionError("Network must not be reached.")

    monkeypatch.setattr(github_release_api, "urlopen", unexpected_urlopen)
    with pytest.raises(ValueError, match="not bound"):
        GitHubApi(token="private-release-token").upload_asset(
            "https://uploads.github.com/repos/example/project/releases/43/assets{?name,label}",
            repository="example/project",
            release_id=42,
            asset_name=source.name,
            source=source,
            expected_size=1,
            expected_sha256=sha256(b"x").hexdigest(),
        )


def test_github_api_keepalive_reuses_connection_and_strips_auth_on_download_redirect(
    tmp_path: Path,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen_auth: dict[str, list[str | None]] = {"api": [], "blob": []}
    connection_ids: list[int] = []
    payload = b"redirected-installer-bytes"

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            connection_ids.append(id(self.connection))
            if self.path == "/repos/example/project/releases/42":
                seen_auth["api"].append(self.headers.get("Authorization"))
                body = b'{"id":42,"draft":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/repos/example/project/releases/assets/99":
                seen_auth["api"].append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", "/blob/asset-99")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/blob/asset-99":
                seen_auth["blob"].append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = GitHubApi(
        token="private-release-token",
        api_url=f"http://127.0.0.1:{server.server_port}",
        timeout_sec=5.0,
    )
    try:
        assert api.supports_concurrent_downloads is True
        assert api.get_json("/repos/example/project/releases/42") == {"id": 42, "draft": True}
        assert api.get_json("/repos/example/project/releases/42") == {"id": 42, "draft": True}
        destination = tmp_path / "asset.exe"
        downloaded = api.download_asset(
            "/repos/example/project/releases/assets/99",
            destination=destination,
            max_bytes=len(payload),
        )
        assert destination.read_bytes() == payload
        assert downloaded == {"sizeBytes": len(payload), "sha256": sha256(payload).hexdigest()}
    finally:
        api.close()
        server.shutdown()
        server.server_close()

    assert len(set(connection_ids)) == 1
    assert seen_auth["api"] == [
        "Bearer private-release-token",
        "Bearer private-release-token",
        "Bearer private-release-token",
    ]
    assert seen_auth["blob"] == [None]


def test_github_api_keepalive_handles_stale_sockets_uploads_redirects_timeouts_and_threads(
    tmp_path: Path,
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    post_attempts = 0
    upload_bodies: list[bytes] = []
    close_after_response = {"enabled": False}

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            if self.path == "/ping":
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if close_after_response["enabled"]:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                if close_after_response["enabled"]:
                    self.close_connection = True
                return
            if self.path == "/slow":
                time.sleep(0.35)
                body = b'{"slow":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/redirect-loop":
                self.send_response(302)
                self.send_header("Location", "/redirect-loop")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/assets/"):
                payload = f"asset-bytes-{self.path}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            nonlocal post_attempts
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.close_connection = True
                return
            if self.path == "/drop-mid-response":
                post_attempts += 1
                self.close_connection = True
                return
            if self.path.startswith("/repos/example/project/releases/42/assets"):
                upload_bodies.append(raw)
                body = (
                    f'{{"id":101,"name":"demo.bin","state":"uploaded","size":{len(raw)},'
                    f'"digest":"sha256:{sha256(raw).hexdigest()}"}}'
                ).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b'{"created":true}'
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    api = GitHubApi(token="private-release-token", api_url=base, timeout_sec=5.0)
    try:
        # 1. Server closes idle connection silently -> liveness check replaces it for POST.
        assert api.get_json("/ping") == {"ok": True}
        pooled_conn = next(iter(api._pool().values()))[0]
        assert pooled_conn.sock is not None
        # Simulate peer FIN by closing the server-accepted half via Connection: close first,
        # and also verify _tracked_connections shrinks on discard.
        close_after_response["enabled"] = True
        assert api.get_json("/ping") == {"ok": True}
        assert len(api._tracked_connections) == 0
        close_after_response["enabled"] = False

        # 2. Mutating POST never retries when server drops after receiving request bytes.
        assert api.get_json("/ping") == {"ok": True}
        with pytest.raises(GitHubApiError, match="could not be reached"):
            api.post_json("/drop-mid-response", {"draft": True})
        assert post_attempts == 1

        # 3. _upload_keepalive streams and succeeds cleanly; tampered file fails before completion.
        payload = b"streamed-keepalive-upload-bytes"
        source = tmp_path / "demo.bin"
        source.write_bytes(payload)
        uploaded = api._upload_keepalive(
            base_url=f"{base}/repos/example/project/releases/42/assets",
            query="name=demo.bin",
            path_label="/repos/example/project/releases/42/assets",
            body=github_release_api._FileUploadBody(
                source=source,
                expected_size=len(payload),
                expected_sha256=sha256(payload).hexdigest(),
            ),
            expected_size=len(payload),
        )
        assert uploaded["id"] == 101
        assert upload_bodies == [payload]

        # Same-size mutation detected before final chunk is sent -> server never stores it.
        tampered = tmp_path / "tampered.bin"
        tampered.write_bytes(b"X" * len(payload))
        with pytest.raises(GitHubApiError, match="changed while it was being uploaded"):
            api._upload_keepalive(
                base_url=f"{base}/repos/example/project/releases/42/assets",
                query="name=tampered.bin",
                path_label="/repos/example/project/releases/42/assets",
                body=github_release_api._FileUploadBody(
                    source=tampered,
                    expected_size=len(payload),
                    expected_sha256=sha256(payload).hexdigest(),
                ),
                expected_size=len(payload),
            )
        assert len(upload_bodies) == 1

        # 4. Redirect limit is enforced.
        with pytest.raises(GitHubApiError, match="redirect limit"):
            api.download_asset("/redirect-loop", destination=tmp_path / "loop.bin", max_bytes=128)

        # 5. Per-request timeout_sec is enforced even on a reused pooled socket.
        assert api.get_json("/ping", timeout_sec=5.0) == {"ok": True}
        with pytest.raises(GitHubApiError, match="could not be reached|timed out"):
            api.get_json("/slow", timeout_sec=0.05)

        # 6. Concurrent threaded downloads isolate sockets per thread.
        def _dl(idx: int) -> bytes:
            dest = tmp_path / f"asset-{idx}.bin"
            api.download_asset(f"/assets/{idx}", destination=dest, max_bytes=128)
            return dest.read_bytes()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_dl, range(6)))
        assert results == [f"asset-bytes-/assets/{idx}".encode() for idx in range(6)]
    finally:
        api.close()
        server.shutdown()
        server.server_close()


def test_github_api_https_client_refuses_loopback_http_downgrade() -> None:
    https_api = GitHubApi(token="private-release-token", api_url="https://api.github.com")
    with pytest.raises(GitHubApiError, match="Refusing non-HTTPS connection"):
        https_api._acquire_connection(
            scheme="http",
            host="127.0.0.1",
            port=8080,
            timeout_sec=5.0,
            max_idle_sec=15.0,
        )


def test_github_api_get_retries_windows_connection_aborted_on_reused_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[bool] = []

    class _FakeResponse:
        status = 200
        will_close = False

        def read(self, _limit: int) -> bytes:
            return b'{"recovered":true}'

    class _FakeConnection:
        def __init__(self, *, fail_once: bool) -> None:
            self.fail_once = fail_once
            self.timeout = 5.0
            self.sock = None

        def request(self, *_args: object, **_kwargs: object) -> None:
            return None

        def getresponse(self) -> _FakeResponse:
            if self.fail_once:
                raise ConnectionAbortedError("WinError 10053")
            return _FakeResponse()

        def close(self) -> None:
            return None

    api = GitHubApi(token="private-release-token", api_url="https://api.github.com")

    def fake_acquire(**_kwargs: object) -> tuple[Any, bool]:
        reused = len(attempts) == 0
        attempts.append(reused)
        return _FakeConnection(fail_once=reused), reused

    monkeypatch.setattr(api, "_acquire_connection", fake_acquire)
    assert api.get_json("/repos/example/project/releases/42") == {"recovered": True}
    assert attempts == [True, False]
