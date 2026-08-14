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
