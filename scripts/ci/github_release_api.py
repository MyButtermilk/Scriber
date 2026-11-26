"""Small fail-closed GitHub REST client for release workflow gates."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import select
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_REQUEST_BYTES = 64 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_DOWNLOAD_REDIRECTS = 5
KEEPALIVE_IDLE_TTL_SEC = 15.0
GET_KEEPALIVE_IDLE_TTL_SEC = 60.0
MUTATING_KEEPALIVE_IDLE_TTL_SEC = 1.0
UPLOAD_HOST = "uploads.github.com"
_DEFAULT_URLOPEN = urlopen
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_STALE_KEEPALIVE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    ConnectionResetError,
    BrokenPipeError,
)


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
        # Hold back the final chunk until EOF and SHA-256 verification succeed
        # so a same-size mid-upload mutation aborts before Content-Length completes.
        pending_chunk: bytes | None = None
        with self._source.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > self._expected_size or size_bytes > MAX_ASSET_BYTES:
                    raise GitHubApiError("GitHub release asset changed or exceeded its upload-size limit.")
                digest.update(chunk)
                if pending_chunk is not None:
                    yield pending_chunk
                pending_chunk = chunk
        if size_bytes != self._expected_size or digest.hexdigest() != self._expected_sha256:
            raise GitHubApiError("GitHub release asset changed while it was being uploaded.")
        if pending_chunk is not None:
            yield pending_chunk
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
        parsed_api = urlsplit(self._api_url)
        self._allow_loopback_http = parsed_api.scheme.lower() == "http" and (parsed_api.hostname or "").lower() in {
            "127.0.0.1",
            "localhost",
        }
        self._timeout_sec = timeout_sec
        self._ssl_context = ssl.create_default_context()
        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._tracked_connections: list[http.client.HTTPConnection] = []

    @property
    def supports_concurrent_downloads(self) -> bool:
        return urlopen is _DEFAULT_URLOPEN

    def close(self) -> None:
        with self._connections_lock:
            tracked = list(self._tracked_connections)
            self._tracked_connections.clear()
        for connection in tracked:
            with contextlib.suppress(OSError):
                connection.close()
        pool = getattr(self._local, "pool", None)
        if isinstance(pool, dict):
            pool.clear()

    def _pool(self) -> dict[tuple[str, str, int], tuple[http.client.HTTPConnection, float]]:
        pool = getattr(self._local, "pool", None)
        if not isinstance(pool, dict):
            pool = {}
            self._local.pool = pool
        return pool

    @staticmethod
    def _is_connection_alive(connection: http.client.HTTPConnection) -> bool:
        sock = connection.sock
        if sock is None:
            return False
        try:
            # A healthy idle keep-alive socket must not already be readable;
            # readiness indicates a server FIN/RST or unexpected trailing data.
            readable, _, exceptional = select.select([sock], [], [sock], 0.0)
            return not readable and not exceptional
        except OSError, ValueError:
            return False

    @staticmethod
    def _remaining_timeout(deadline: float, *, label: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitHubApiError(f"GitHub API {label} timed out.")
        return remaining

    @staticmethod
    def _apply_socket_timeout(connection: http.client.HTTPConnection, timeout_sec: float) -> None:
        connection.timeout = timeout_sec
        if connection.sock is not None:
            connection.sock.settimeout(timeout_sec)

    def _acquire_connection(
        self,
        *,
        scheme: str,
        host: str,
        port: int,
        timeout_sec: float,
        max_idle_sec: float,
    ) -> tuple[http.client.HTTPConnection, bool]:
        key = (scheme, host, port)
        pool = self._pool()
        entry = pool.get(key)
        now = time.monotonic()
        if entry is not None:
            existing, last_used = entry
            if (now - last_used) <= max_idle_sec and self._is_connection_alive(existing):
                try:
                    self._apply_socket_timeout(existing, timeout_sec)
                    return existing, True
                except OSError:
                    pass
            self._discard_connection(scheme=scheme, host=host, port=port)
        if scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout_sec,
                context=self._ssl_context,
            )
        elif scheme == "http" and self._allow_loopback_http and host in {"127.0.0.1", "localhost"}:
            connection = http.client.HTTPConnection(
                host,
                port,
                timeout=timeout_sec,
            )
        else:
            raise GitHubApiError(f"Refusing non-HTTPS connection to {host!r}.")
        pool[key] = (connection, now)
        with self._connections_lock:
            self._tracked_connections.append(connection)
        return connection, False

    def _touch_connection(self, *, scheme: str, host: str, port: int) -> None:
        key = (scheme, host, port)
        pool = self._pool()
        entry = pool.get(key)
        if entry is not None:
            pool[key] = (entry[0], time.monotonic())

    def _discard_connection(self, *, scheme: str, host: str, port: int) -> None:
        entry = self._pool().pop((scheme, host, port), None)
        if entry is not None:
            connection = entry[0]
            with self._connections_lock, contextlib.suppress(ValueError):
                self._tracked_connections.remove(connection)
            with contextlib.suppress(OSError):
                connection.close()

    @staticmethod
    def _parse_endpoint(url: str) -> tuple[str, str, int, str]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        if not host or scheme not in {"http", "https"}:
            raise GitHubApiError("GitHub API URL is invalid.")
        port = parsed.port or (443 if scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return scheme, host, port, target

    def _request_keepalive(
        self,
        path: str,
        *,
        method: str,
        expected_status: int,
        accept: str,
        body: bytes | None,
        content_type: str | None,
        timeout_sec: float | None = None,
    ) -> bytes:
        effective_timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        if effective_timeout <= 0:
            raise GitHubApiError(f"GitHub API {method} {path} timed out.")
        deadline = time.monotonic() + effective_timeout
        scheme, host, port, target = self._parse_endpoint(f"{self._api_url}{path}")
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "Connection": "keep-alive",
            "User-Agent": "scriber-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if body is not None:
            headers["Content-Length"] = str(len(body))

        max_attempts = 2 if method == "GET" else 1
        max_idle_sec = GET_KEEPALIVE_IDLE_TTL_SEC if method == "GET" else MUTATING_KEEPALIVE_IDLE_TTL_SEC
        for attempt in range(max_attempts):
            remaining = self._remaining_timeout(deadline, label=f"{method} {path}")
            connection, reused = self._acquire_connection(
                scheme=scheme,
                host=host,
                port=port,
                timeout_sec=remaining,
                max_idle_sec=max_idle_sec,
            )
            try:
                connection.request(method, target, body=body, headers=headers)
                self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"{method} {path}"))
                response = connection.getresponse()
                status = int(response.status)
                self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"{method} {path}"))
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if response.will_close or len(payload) > MAX_RESPONSE_BYTES:
                    self._discard_connection(scheme=scheme, host=host, port=port)
                else:
                    self._touch_connection(scheme=scheme, host=host, port=port)
            except GitHubApiError:
                self._discard_connection(scheme=scheme, host=host, port=port)
                raise
            except _STALE_KEEPALIVE_ERRORS as error:
                self._discard_connection(scheme=scheme, host=host, port=port)
                if reused and attempt + 1 < max_attempts:
                    continue
                raise GitHubApiError(f"GitHub API {method} {path} could not be reached.") from error
            except (http.client.HTTPException, OSError) as error:
                self._discard_connection(scheme=scheme, host=host, port=port)
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

        raise GitHubApiError(f"GitHub API {method} {path} could not be reached.")

    def _request(
        self,
        path: str,
        *,
        method: str,
        expected_status: int,
        accept: str = "application/vnd.github+json",
        body: bytes | None = None,
        content_type: str | None = None,
        timeout_sec: float | None = None,
    ) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API paths must be absolute API paths.")
        effective_timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        if urlopen is _DEFAULT_URLOPEN:
            return self._request_keepalive(
                path,
                method=method,
                expected_status=expected_status,
                accept=accept,
                body=body,
                content_type=content_type,
                timeout_sec=effective_timeout,
            )
        request = self._build_request(
            path,
            method=method,
            accept=accept,
            body=body,
            content_type=content_type,
        )
        try:
            with urlopen(request, timeout=effective_timeout) as response:
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

    def get_json(self, path: str, *, timeout_sec: float | None = None) -> dict[str, Any]:
        payload = self._request(path, method="GET", expected_status=200, timeout_sec=timeout_sec)
        return self._parse_json_object(payload, method="GET", path=path)

    def get_json_list(self, path: str, *, timeout_sec: float | None = None) -> list[dict[str, Any]]:
        payload = self._request(path, method="GET", expected_status=200, timeout_sec=timeout_sec)
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
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        if expected_status not in {200, 201}:
            raise ValueError("GitHub API JSON POST status must be 200 or 201.")
        payload = self._request(
            path,
            method="POST",
            expected_status=expected_status,
            body=self._encode_json_object(value, method="POST", path=path),
            content_type="application/json",
            timeout_sec=timeout_sec,
        )
        return self._parse_json_object(payload, method="POST", path=path)

    def patch_json(
        self,
        path: str,
        value: dict[str, Any],
        *,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            path,
            method="PATCH",
            expected_status=200,
            body=self._encode_json_object(value, method="PATCH", path=path),
            content_type="application/json",
            timeout_sec=timeout_sec,
        )
        return self._parse_json_object(payload, method="PATCH", path=path)

    def _download_keepalive(
        self,
        path: str,
        *,
        destination: Path,
        max_bytes: int,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        effective_timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        if effective_timeout <= 0:
            raise GitHubApiError(f"GitHub API GET {path} timed out.")
        deadline = time.monotonic() + effective_timeout
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.partial-{uuid4().hex}")
        current_url = f"{self._api_url}{path}"
        send_authorization = True
        try:
            for _redirect in range(MAX_DOWNLOAD_REDIRECTS + 1):
                scheme, host, port, target = self._parse_endpoint(current_url)
                headers = {
                    "Accept": "application/octet-stream",
                    "Connection": "keep-alive",
                    "User-Agent": "scriber-release-gate",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                if send_authorization:
                    headers["Authorization"] = f"Bearer {self._token}"

                connection: http.client.HTTPConnection | None = None
                response: http.client.HTTPResponse | None = None
                for attempt in range(2):
                    remaining = self._remaining_timeout(deadline, label=f"GET {path}")
                    connection, reused = self._acquire_connection(
                        scheme=scheme,
                        host=host,
                        port=port,
                        timeout_sec=remaining,
                        max_idle_sec=GET_KEEPALIVE_IDLE_TTL_SEC,
                    )
                    try:
                        connection.request("GET", target, headers=headers)
                        self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"GET {path}"))
                        response = connection.getresponse()
                        break
                    except GitHubApiError:
                        self._discard_connection(scheme=scheme, host=host, port=port)
                        raise
                    except _STALE_KEEPALIVE_ERRORS as error:
                        self._discard_connection(scheme=scheme, host=host, port=port)
                        if reused and attempt == 0:
                            continue
                        raise GitHubApiError(f"GitHub API GET {path} could not be reached.") from error
                    except (http.client.HTTPException, OSError) as error:
                        self._discard_connection(scheme=scheme, host=host, port=port)
                        raise GitHubApiError(f"GitHub API GET {path} could not be reached.") from error
                if response is None or connection is None:
                    raise GitHubApiError(f"GitHub API GET {path} could not be reached.")

                status = int(response.status)
                if status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    discarded = False
                    try:
                        self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"GET {path}"))
                        redirect_body = response.read(MAX_RESPONSE_BYTES + 1)
                        if len(redirect_body) > MAX_RESPONSE_BYTES:
                            self._discard_connection(scheme=scheme, host=host, port=port)
                            discarded = True
                    except GitHubApiError, http.client.HTTPException, OSError:
                        self._discard_connection(scheme=scheme, host=host, port=port)
                        discarded = True
                    if not discarded:
                        if response.will_close:
                            self._discard_connection(scheme=scheme, host=host, port=port)
                        else:
                            self._touch_connection(scheme=scheme, host=host, port=port)
                    if not location:
                        raise GitHubApiError(
                            f"GitHub API GET {path} returned redirect HTTP {status} without Location.",
                            status_code=status,
                        )
                    current_url = urljoin(current_url, location)
                    # Never forward API credentials to a redirected object-storage URL.
                    send_authorization = False
                    continue

                if status != 200:
                    discarded = False
                    try:
                        self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"GET {path}"))
                        error_body = response.read(MAX_RESPONSE_BYTES + 1)
                        if len(error_body) > MAX_RESPONSE_BYTES:
                            self._discard_connection(scheme=scheme, host=host, port=port)
                            discarded = True
                    except GitHubApiError, http.client.HTTPException, OSError:
                        self._discard_connection(scheme=scheme, host=host, port=port)
                        discarded = True
                    if not discarded:
                        if response.will_close:
                            self._discard_connection(scheme=scheme, host=host, port=port)
                        else:
                            self._touch_connection(scheme=scheme, host=host, port=port)
                    raise GitHubApiError(
                        f"GitHub API GET {path} returned HTTP {status}, expected 200.",
                        status_code=status,
                    )

                size_bytes = 0
                digest = hashlib.sha256()
                try:
                    with temporary.open("wb") as output:
                        while True:
                            self._apply_socket_timeout(
                                connection, self._remaining_timeout(deadline, label=f"GET {path}")
                            )
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            size_bytes += len(chunk)
                            if size_bytes > max_bytes:
                                self._discard_connection(scheme=scheme, host=host, port=port)
                                raise GitHubApiError(f"GitHub release asset {path} exceeded the download-size limit.")
                            digest.update(chunk)
                            output.write(chunk)
                except GitHubApiError:
                    self._discard_connection(scheme=scheme, host=host, port=port)
                    raise
                except (http.client.HTTPException, OSError) as error:
                    self._discard_connection(scheme=scheme, host=host, port=port)
                    raise GitHubApiError(f"GitHub API GET {path} could not be reached.") from error
                if response.will_close:
                    self._discard_connection(scheme=scheme, host=host, port=port)
                else:
                    self._touch_connection(scheme=scheme, host=host, port=port)
                temporary.replace(destination)
                return {
                    "sizeBytes": size_bytes,
                    "sha256": digest.hexdigest(),
                }

            raise GitHubApiError(f"GitHub API GET {path} exceeded the redirect limit.")
        finally:
            temporary.unlink(missing_ok=True)

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
        if urlopen is _DEFAULT_URLOPEN:
            return self._download_keepalive(
                path,
                destination=destination,
                max_bytes=max_bytes,
            )
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

    def _upload_keepalive(
        self,
        *,
        base_url: str,
        query: str,
        path_label: str,
        body: _FileUploadBody,
        expected_size: int,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        effective_timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        if effective_timeout <= 0:
            raise GitHubApiError(f"GitHub API POST {path_label} timed out.")
        deadline = time.monotonic() + effective_timeout
        scheme, host, port, target = self._parse_endpoint(f"{base_url}?{query}")
        connection, _reused = self._acquire_connection(
            scheme=scheme,
            host=host,
            port=port,
            timeout_sec=self._remaining_timeout(deadline, label=f"POST {path_label}"),
            max_idle_sec=MUTATING_KEEPALIVE_IDLE_TTL_SEC,
        )
        try:
            connection.putrequest("POST", target)
            connection.putheader("Accept", "application/vnd.github+json")
            connection.putheader("Authorization", f"Bearer {self._token}")
            connection.putheader("Connection", "keep-alive")
            connection.putheader("Content-Length", str(expected_size))
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("User-Agent", "scriber-release-gate")
            connection.putheader("X-GitHub-Api-Version", "2022-11-28")
            connection.endheaders()
            for chunk in body:
                self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"POST {path_label}"))
                connection.send(chunk)
            self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"POST {path_label}"))
            response = connection.getresponse()
            status = int(response.status)
            self._apply_socket_timeout(connection, self._remaining_timeout(deadline, label=f"POST {path_label}"))
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if response.will_close or len(payload) > MAX_RESPONSE_BYTES:
                self._discard_connection(scheme=scheme, host=host, port=port)
            else:
                self._touch_connection(scheme=scheme, host=host, port=port)
        except GitHubApiError:
            self._discard_connection(scheme=scheme, host=host, port=port)
            raise
        except (http.client.HTTPException, OSError) as error:
            self._discard_connection(scheme=scheme, host=host, port=port)
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
        path_label = f"/repos/{repository}/releases/{release_id}/assets"
        if urlopen is _DEFAULT_URLOPEN:
            return self._upload_keepalive(
                base_url=base_url,
                query=query,
                path_label=path_label,
                body=body,
                expected_size=expected_size,
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
