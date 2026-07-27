"""Small fail-closed GitHub REST client for release workflow gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_REQUEST_BYTES = 64 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
UPLOAD_HOST = "uploads.github.com"


class GitHubApiError(RuntimeError):
    """A bounded GitHub API failure that never includes credentials."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _FileUploadBody:
    def __init__(
        self,
        *,
        source: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        self._source = source
        self._expected_size = expected_size
        self._expected_sha256 = expected_sha256
        self._started = False
        self.completed = False

    def __iter__(self) -> Iterator[bytes]:
        if self._started:
            raise GitHubApiError("GitHub release asset upload body cannot be replayed.")
        self._started = True
        size_bytes = 0
        digest = hashlib.sha256()
        with self._source.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > self._expected_size or size_bytes > MAX_ASSET_BYTES:
                    raise GitHubApiError("GitHub release asset changed or exceeded its upload-size limit.")
                digest.update(chunk)
                yield chunk
        if size_bytes != self._expected_size or digest.hexdigest() != self._expected_sha256:
            raise GitHubApiError("GitHub release asset changed while it was being uploaded.")
        self.completed = True


class GitHubApi:
    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://api.github.com",
        timeout_sec: float = 30.0,
    ) -> None:
        token = token.strip()
        if not token:
            raise ValueError("A GitHub API token is required.")
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def _request(
        self,
        path: str,
        *,
        method: str,
        expected_status: int,
        accept: str = "application/vnd.github+json",
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API paths must be absolute API paths.")
        request = self._build_request(
            path,
            method=method,
            accept=accept,
            body=body,
            content_type=content_type,
        )
        try:
            with urlopen(request, timeout=self._timeout_sec) as response:
                status = int(response.status)
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise GitHubApiError(
                f"GitHub API {method} {path} returned HTTP {error.code}.",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise GitHubApiError(f"GitHub API {method} {path} could not be reached.") from error
        if status != expected_status:
            raise GitHubApiError(
                f"GitHub API {method} {path} returned HTTP {status}, expected {expected_status}.",
                status_code=status,
            )
        if len(payload) > MAX_RESPONSE_BYTES:
            raise GitHubApiError(
                f"GitHub API {method} {path} exceeded the response-size limit.",
                status_code=status,
            )
        return payload

    def _build_request(
        self,
        path: str,
        *,
        method: str,
        accept: str,
        body: object | None = None,
        content_type: str | None = None,
    ) -> Request:
        headers = {
            "Accept": accept,
            "User-Agent": "scriber-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(
            f"{self._api_url}{path}",
            method=method,
            headers=headers,
            data=body,
        )
        # urllib copies ordinary headers to redirects. Keep credentials on the
        # initial api.github.com request only; release asset downloads may
        # redirect to a separately signed object-storage URL.
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        return request

    @staticmethod
    def _parse_json_object(payload: bytes, *, method: str, path: str) -> dict[str, Any]:
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError(f"GitHub API {method} {path} did not return valid JSON.") from error
        if not isinstance(parsed, dict):
            raise GitHubApiError(f"GitHub API {method} {path} did not return a JSON object.")
        return parsed

    @staticmethod
    def _encode_json_object(value: dict[str, Any], *, method: str, path: str) -> bytes:
        try:
            payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError(f"GitHub API {method} {path} request is not JSON serializable.") from error
        if len(payload) > MAX_JSON_REQUEST_BYTES:
            raise ValueError(f"GitHub API {method} {path} request exceeded the JSON-size limit.")
        return payload

    def get_json(self, path: str) -> dict[str, Any]:
        payload = self._request(path, method="GET", expected_status=200)
        return self._parse_json_object(payload, method="GET", path=path)

    def get_json_list(self, path: str) -> list[dict[str, Any]]:
        payload = self._request(path, method="GET", expected_status=200)
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError(f"GitHub API GET {path} did not return valid JSON.") from error
        if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
            raise GitHubApiError(f"GitHub API GET {path} did not return a JSON object list.")
        return parsed

    def post_json(
        self,
        path: str,
        value: dict[str, Any],
        *,
        expected_status: int = 201,
    ) -> dict[str, Any]:
        if expected_status not in {200, 201}:
            raise ValueError("GitHub API JSON POST status must be 200 or 201.")
        payload = self._request(
            path,
            method="POST",
            expected_status=expected_status,
            body=self._encode_json_object(value, method="POST", path=path),
            content_type="application/json",
        )
        return self._parse_json_object(payload, method="POST", path=path)

    def patch_json(self, path: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            path,
            method="PATCH",
            expected_status=200,
            body=self._encode_json_object(value, method="PATCH", path=path),
            content_type="application/json",
        )
        return self._parse_json_object(payload, method="PATCH", path=path)

    def download_asset(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int = MAX_ASSET_BYTES,
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API paths must be absolute API paths.")
        if max_bytes <= 0 or max_bytes > MAX_ASSET_BYTES:
            raise ValueError(f"Asset download limit must be between 1 and {MAX_ASSET_BYTES} bytes.")
        request = self._build_request(
            path,
            method="GET",
            accept="application/octet-stream",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.partial-{uuid4().hex}")
        size_bytes = 0
        digest = hashlib.sha256()
        try:
            try:
                response = urlopen(request, timeout=self._timeout_sec)
            except HTTPError as error:
                raise GitHubApiError(
                    f"GitHub API GET {path} returned HTTP {error.code}.",
                    status_code=error.code,
                ) from error
            except URLError as error:
                raise GitHubApiError(f"GitHub API GET {path} could not be reached.") from error
            with response, temporary.open("wb") as output:
                if int(response.status) != 200:
                    raise GitHubApiError(
                        f"GitHub API GET {path} returned HTTP {response.status}, expected 200.",
                        status_code=int(response.status),
                    )
                while chunk := response.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise GitHubApiError(f"GitHub release asset {path} exceeded the download-size limit.")
                    digest.update(chunk)
                    output.write(chunk)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "sizeBytes": size_bytes,
            "sha256": digest.hexdigest(),
        }

    def upload_asset(
        self,
        upload_url: str,
        *,
        repository: str,
        release_id: int,
        asset_name: str,
        source: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(release_id, int)
            or release_id <= 0
            or not asset_name
            or asset_name in {".", ".."}
            or "/" in asset_name
            or "\\" in asset_name
        ):
            raise ValueError("GitHub release asset upload identity is invalid.")
        if (
            not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > MAX_ASSET_BYTES
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("GitHub release asset upload bounds or SHA-256 are invalid.")
        if not source.is_file() or source.is_symlink() or source.stat().st_size != expected_size:
            raise ValueError("GitHub release asset source is missing, unsafe, or changed.")

        template_suffix = "{?name,label}"
        if not upload_url.endswith(template_suffix):
            raise ValueError("GitHub release upload URL has an unexpected template.")
        base_url = upload_url.removesuffix(template_suffix)
        parsed_url = urlsplit(base_url)
        expected_path = f"/repos/{repository}/releases/{release_id}/assets"
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != UPLOAD_HOST
            or parsed_url.port is not None
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path != expected_path
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("GitHub release upload URL is not bound to the exact release.")
        query = urlencode({"name": asset_name}, quote_via=quote)
        body = _FileUploadBody(
            source=source,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        request = Request(
            f"{base_url}?{query}",
            method="POST",
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Length": str(expected_size),
                "Content-Type": "application/octet-stream",
                "User-Agent": "scriber-release-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        path_label = f"/repos/{repository}/releases/{release_id}/assets"
        try:
            with urlopen(request, timeout=self._timeout_sec) as response:
                status = int(response.status)
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise GitHubApiError(
                f"GitHub API POST {path_label} returned HTTP {error.code}.",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise GitHubApiError(f"GitHub API POST {path_label} could not be reached.") from error
        if status != 201:
            raise GitHubApiError(
                f"GitHub API POST {path_label} returned HTTP {status}, expected 201.",
                status_code=status,
            )
        if len(payload) > MAX_RESPONSE_BYTES:
            raise GitHubApiError(
                f"GitHub API POST {path_label} exceeded the response-size limit.",
                status_code=status,
            )
        if not body.completed:
            raise GitHubApiError("GitHub release asset upload body was not completely consumed.")
        return self._parse_json_object(payload, method="POST", path=path_label)

    def delete(self, path: str) -> None:
        self._request(path, method="DELETE", expected_status=204)
