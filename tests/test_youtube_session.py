from __future__ import annotations

from urllib.request import Request

import pytest

from src.youtube_session import YouTubeSession, YouTubeSessionError


def cookies(*lines: str) -> str:
    return "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"


def test_session_retains_only_youtube_cookies_and_never_exposes_values():
    session = YouTubeSession()
    session.connect(
        cookies(
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-session-value",
            ".google.com\tTRUE\t/\tTRUE\t0\tSID\tother-account-value",
            ".youtube.com.evil.test\tTRUE\t/\tTRUE\t0\tSID\tforeign-value",
        )
    )
    assert session.status() == {"connected": True}
    snapshot = session.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].domain == ".youtube.com"
    assert "private-session-value" not in str(session.status())
    session.disconnect()
    assert session.snapshot() == ()
    assert session.status() == {"connected": False}


def test_snapshot_is_private_to_each_download_and_session_expires(monkeypatch):
    monkeypatch.setattr("src.youtube_session.time.monotonic", lambda: 100.0)
    session = YouTubeSession()
    session.connect(cookies("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret"))
    first = session.snapshot()
    first[0].value = "changed-by-extractor"
    assert session.snapshot()[0].value == "secret"
    monkeypatch.setattr("src.youtube_session.time.monotonic", lambda: 7301.0)
    assert session.snapshot() == ()
    assert session.status() == {"connected": False}


@pytest.mark.parametrize(
    "content",
    [
        "not a cookie file",
        cookies(".google.com\tTRUE\t/\tTRUE\t0\tSID\tsecret"),
        cookies(".youtube.com\tTRUE\t/\tTRUE\t1\tSID\tsecret"),
        cookies(".youtube.com\tTRUE\t/\tTRUE\twrong\tSID\tsecret"),
        cookies(".youtube.com\tTRUE\t/\tTRUE\t0\tbad;name\tsecret"),
        "x" * (64 * 1024 + 1),
    ],
    ids=["header", "foreign", "expired", "invalid-expiry", "invalid-name", "oversized"],
)
def test_invalid_import_is_safe_and_preserves_previous_session(content):
    session = YouTubeSession()
    session.connect(cookies(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprevious"))
    with pytest.raises(YouTubeSessionError) as caught:
        session.connect(content)
    assert "secret" not in str(caught.value)
    assert session.snapshot()[0].value == "previous"


def test_cookies_are_sent_only_to_youtube_over_https():
    from http.cookiejar import CookieJar

    session = YouTubeSession()
    session.connect(cookies(".youtube.com\tTRUE\t/\tFALSE\t0\tSID\tsecret"))
    jar = CookieJar()
    for cookie in session.snapshot():
        jar.set_cookie(cookie)
    for url, expected in [
        ("https://www.youtube.com/watch?v=BFKcC0VyuZA", "SID=secret"),
        ("http://www.youtube.com/", None),
        ("https://www.google.com/", None),
        ("https://youtube.com.evil.test/", None),
    ]:
        request = Request(url)
        jar.add_cookie_header(request)
        assert request.get_header("Cookie") == expected
