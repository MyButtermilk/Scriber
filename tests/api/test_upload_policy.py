"""The upload policy shared by Live Mic file uploads and Meeting imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.upload_policy import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_FILENAME_CHARS,
    VIDEO_EXTENSIONS,
    format_upload_limit,
    safe_upload_filename,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("subdir/evil.mp3", "evil.mp3"),
        ("", "uploaded_file"),
        ("   ", "uploaded_file"),
        (".", "uploaded_file"),
        ("..", "uploaded_file"),
        ("trailing.  ", "trailing"),
    ],
)
def test_a_name_reduces_to_its_final_component(raw, expected):
    assert safe_upload_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "subdir/evil.mp3",
        "..\\..\\escape.wav",
        "../../escape.wav",
        "C:\\Windows\\System32\\cmd.exe.mp3",
        "/etc/passwd.wav",
    ],
)
def test_a_name_can_never_escape_its_directory(raw):
    """Asserted as an invariant, because the exact result is platform-shaped.

    On Windows a backslash is a separator and `Path.name` drops the prefix; on
    POSIX it is an ordinary character that the invalid-character substitution
    replaces instead. Either way the result is a single flat component, which
    is the property that actually matters.
    """
    cleaned = safe_upload_filename(raw)
    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert cleaned not in {".", ".."}
    # A leading ".." is only a traversal when a separator follows it, and
    # none can survive the substitution above.
    assert Path(cleaned).name == cleaned


@pytest.mark.parametrize("raw", ["bad<name>.mp3", 'q:"uote".wav', "pipe|bar.m4a", "null\x00byte.mp3"])
def test_characters_ntfs_rejects_are_replaced(raw):
    cleaned = safe_upload_filename(raw)
    assert not set(cleaned) & set('<>:"/\\|?*')
    assert "\x00" not in cleaned


@pytest.mark.parametrize("reserved", ["CON.mp3", "prn.wav", "AUX", "nul.m4a", "COM1.mp3", "LPT9.wav"])
def test_windows_reserved_device_names_are_prefixed(reserved):
    cleaned = safe_upload_filename(reserved)
    assert cleaned.startswith("_")


def test_a_long_name_is_bounded_but_keeps_its_extension():
    cleaned = safe_upload_filename(f"{'a' * 400}.mp3")
    assert len(cleaned) == MAX_UPLOAD_FILENAME_CHARS
    assert cleaned.endswith(".mp3")


def test_a_long_name_without_an_extension_is_still_bounded():
    assert len(safe_upload_filename("b" * 400)) == MAX_UPLOAD_FILENAME_CHARS


def test_sanitisation_is_idempotent():
    once = safe_upload_filename("../CON<bad>.mp3")
    assert safe_upload_filename(once) == once


def test_both_ingest_paths_accept_the_same_media():
    assert ".mp4" in ALLOWED_UPLOAD_EXTENSIONS
    assert ".mp3" in ALLOWED_UPLOAD_EXTENSIONS
    assert VIDEO_EXTENSIONS < ALLOWED_UPLOAD_EXTENSIONS
    assert ".exe" not in ALLOWED_UPLOAD_EXTENSIONS


@pytest.mark.parametrize(
    ("limit_bytes", "expected"),
    [
        (25 * 1024 * 1024, "25MB"),
        (2 * 1024 * 1024 * 1024, "2GB"),
        (int(1.5 * 1024 * 1024 * 1024), "1.5GB"),
        (0, "0MB"),
    ],
)
def test_limits_are_rendered_for_the_rejection_message(limit_bytes, expected):
    assert format_upload_limit(limit_bytes) == expected
