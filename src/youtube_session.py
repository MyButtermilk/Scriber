"""Explicit, YouTube-only authentication held in memory for this app session."""

from __future__ import annotations

import copy
import re
import threading
import time
from http.cookiejar import Cookie
from typing import Protocol

MAX_COOKIE_BYTES = 64 * 1024
_SESSION_SECONDS = 2 * 60 * 60
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class YouTubeSessionError(ValueError):
    pass


def _parse(content: str) -> tuple[Cookie, ...]:
    invalid = "The YouTube sign-in file is invalid or contains no usable YouTube session."
    if len(content.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise YouTubeSessionError(invalid)
    lines = content.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
        raise YouTubeSessionError(invalid)
    selected: list[Cookie] = []
    for line in lines[1:]:
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise YouTubeSessionError(invalid)
        domain, include_subdomains, path, secure, expires, name, value = fields
        # A browser export may include unrelated accounts. None enter Scriber.
        if domain.lower().removeprefix(".") not in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        }:
            continue
        if (
            include_subdomains not in {"TRUE", "FALSE"}
            or secure not in {"TRUE", "FALSE"}
            or not expires.isdecimal()
            or len(expires) > 12
            or not _COOKIE_NAME.fullmatch(name)
            or not path.startswith("/")
            or any(character in path + value for character in "\r\n;\x00")
            or len(value) > 4096
        ):
            raise YouTubeSessionError(invalid)
        expiry = int(expires) or None
        if expiry is not None and expiry <= time.time():
            continue
        selected.append(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain.lower(),
                domain_specified=include_subdomains == "TRUE",
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=True,
                expires=expiry,
                discard=expiry is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""},
                rfc2109=False,
            )
        )
        if len(selected) > 200:
            raise YouTubeSessionError(invalid)
    if not selected:
        raise YouTubeSessionError(invalid)
    return tuple(selected)


class YouTubeSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cookies: tuple[Cookie, ...] = ()
        self._deadline = 0.0

    def connect(self, content: str) -> None:
        parsed = _parse(content)
        with self._lock:
            self._cookies = parsed
            self._deadline = time.monotonic() + _SESSION_SECONDS

    def disconnect(self) -> None:
        with self._lock:
            self._cookies = ()
            self._deadline = 0.0

    def snapshot(self) -> tuple[Cookie, ...]:
        with self._lock:
            if time.monotonic() >= self._deadline:
                self._cookies = ()
            self._cookies = tuple(cookie for cookie in self._cookies if not cookie.is_expired())
            return tuple(copy.copy(cookie) for cookie in self._cookies)

    def status(self) -> dict[str, bool]:
        return {"connected": bool(self.snapshot())}


youtube_session = YouTubeSession()


class _CookieJar(Protocol):
    def set_cookie(self, cookie: Cookie) -> None: ...


class _Downloader(Protocol):
    @property
    def cookiejar(self) -> _CookieJar: ...


def attach_youtube_session(downloader: _Downloader) -> None:
    """Populate a fresh yt-dlp jar without a cookie file or browser-wide access."""
    cookies = youtube_session.snapshot()
    if cookies:
        jar = downloader.cookiejar
        for cookie in cookies:
            jar.set_cookie(cookie)
