"""The upload policy shared by File transcription uploads and Meeting imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.upload_policy import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_FILENAME_UTF16_UNITS,
    VIDEO_EXTENSIONS,
    FileUploadLimits,
    file_upload_limits,
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


@pytest.mark.parametrize(
    "raw",
    ["bad<name>.mp3", 'q:"uote".wav', "pipe|bar.m4a", "null\x00byte.mp3", "lone\ud800surrogate.mp3"],
)
def test_characters_ntfs_rejects_are_replaced(raw):
    cleaned = safe_upload_filename(raw)
    assert not set(cleaned) & set('<>:"/\\|?*')
    assert "\x00" not in cleaned
    assert not any(0xD800 <= ord(character) <= 0xDFFF for character in cleaned)


@pytest.mark.parametrize(
    "reserved",
    [
        "CON.mp3",
        "prn.wav",
        "AUX",
        "nul.m4a",
        "COM1.mp3",
        "LPT9.wav",
        "COM¹.mp3",
        "COM².wav",
        "COM³.flac",
        "LPT¹.mp3",
        "LPT².wav",
        "LPT³.flac",
        "CON .txt",
        "CON..txt",
        "NUL.extra.log",
    ],
)
def test_windows_reserved_device_names_are_prefixed(reserved):
    cleaned = safe_upload_filename(reserved)
    assert cleaned.startswith("_")


def test_a_long_name_is_bounded_but_keeps_its_extension():
    cleaned = safe_upload_filename(f"{'a' * 400}.mp3")
    assert len(cleaned) == MAX_UPLOAD_FILENAME_UTF16_UNITS
    assert cleaned.endswith(".mp3")


def test_a_long_name_without_an_extension_is_still_bounded():
    assert len(safe_upload_filename("b" * 400)) == MAX_UPLOAD_FILENAME_UTF16_UNITS


def test_a_long_extension_cannot_exceed_the_filename_bound():
    cleaned = safe_upload_filename(f"a.{'x' * 200}")
    assert len(cleaned) <= MAX_UPLOAD_FILENAME_UTF16_UNITS


def test_a_long_reserved_name_stays_prefixed_and_bounded():
    cleaned = safe_upload_filename(f"CON.{'x' * 200}")
    assert cleaned.startswith("_")
    assert len(cleaned) <= MAX_UPLOAD_FILENAME_UTF16_UNITS


def test_truncation_cannot_create_an_unrepaired_reserved_name():
    cleaned = safe_upload_filename(f"CONx.{'a' * 176}")
    assert cleaned.startswith("_")
    assert len(cleaned.encode("utf-16-le")) // 2 <= MAX_UPLOAD_FILENAME_UTF16_UNITS


def test_a_non_bmp_name_is_bounded_in_ntfs_utf16_units():
    cleaned = safe_upload_filename(f"{'😀' * 126}.mp3")
    utf16_units = len(cleaned.encode("utf-16-le")) // 2
    assert utf16_units <= MAX_UPLOAD_FILENAME_UTF16_UNITS
    assert cleaned.endswith(".mp3")


def test_truncation_never_reduces_a_name_to_empty():
    assert safe_upload_filename(f"{'.' * 180}a") == "uploaded_file"


@pytest.mark.parametrize("raw", ["../CON<bad>.mp3", "folder/ file.mp3"])
def test_sanitisation_is_idempotent(raw):
    once = safe_upload_filename(raw)
    assert safe_upload_filename(once) == once


def test_both_ingest_paths_accept_the_same_media():
    assert ".mp4" in ALLOWED_UPLOAD_EXTENSIONS
    assert ".mp3" in ALLOWED_UPLOAD_EXTENSIONS
    assert VIDEO_EXTENSIONS < ALLOWED_UPLOAD_EXTENSIONS
    assert ".exe" not in ALLOWED_UPLOAD_EXTENSIONS


def test_the_shared_media_policy_is_immutable():
    assert isinstance(ALLOWED_UPLOAD_EXTENSIONS, frozenset)
    assert isinstance(VIDEO_EXTENSIONS, frozenset)


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


@pytest.mark.parametrize(
    ("provider", "expected_bytes", "expected_label"),
    [
        ("soniox", 524_288_000, "500MB"),
        ("soniox_async", 524_288_000, "500MB"),
        ("mistral", 512 * 1024 * 1024, "512MB"),
        ("assemblyai", 2_200_000_000, "2.2GB"),
        ("smallest", 25 * 1024 * 1024, "25MB"),
        ("azure_mai", 300 * 1024 * 1024, "300MB"),
        ("openrouter_stt", 300 * 1024 * 1024, "300MB"),
        ("modulate_async", 100 * 1024 * 1024, "100MB"),
        ("openai", 2048 * 1024 * 1024, "2GB"),
    ],
)
def test_provider_bound_limits_have_one_authoritative_value_and_label(
    monkeypatch,
    provider,
    expected_bytes,
    expected_label,
):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)

    limits = file_upload_limits(provider, source_is_video=False)

    assert isinstance(limits, FileUploadLimits)
    assert limits.final_audio.max_bytes == expected_bytes
    assert limits.final_audio.label == expected_label


def test_raw_audio_ingest_can_exceed_the_provider_boundary(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)

    soniox = file_upload_limits("soniox", source_is_video=False)
    assemblyai = file_upload_limits("assemblyai", source_is_video=False)

    assert soniox.ingest.max_bytes == 2048 * 1024 * 1024
    assert assemblyai.ingest.max_bytes == 2_200_000_000


def test_environment_override_is_frozen_into_both_limits(monkeypatch):
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_BYTES", "123")
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)

    limits = file_upload_limits("soniox", source_is_video=False)

    assert limits.final_audio.max_bytes == 123
    assert limits.final_audio.label == "0MB"
    assert limits.ingest.max_bytes == 2048 * 1024 * 1024

    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_MB", "300")
    assert file_upload_limits("soniox", source_is_video=False).final_audio.max_bytes == 300 * 1024 * 1024


def test_video_admission_uses_the_shared_raw_boundary(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_MB", raising=False)

    limits = file_upload_limits("smallest", source_is_video=True)

    assert limits.source_is_video is True
    assert limits.ingest.max_bytes == 2048 * 1024 * 1024
    assert limits.final_audio.max_bytes == 25 * 1024 * 1024
