from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from scripts import verify_tauri_updater_publication as publication


def signed_latest_json_bytes(*, signature: str = "signed-update") -> bytes:
    return json.dumps(
        {
            "version": "0.1.0",
            "notes": "Release notes",
            "pub_date": "2026-06-02T12:00:00Z",
            "platforms": {
                "windows-x86_64": {
                    "signature": signature,
                    "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                }
            },
            "artifacts": [
                {
                    "name": "Scriber_0.1.0_x64-setup.exe",
                    "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                    "sha256": "a" * 64,
                    "sizeBytes": 123,
                    "signature": signature,
                }
            ],
        },
        sort_keys=True,
    ).encode("utf-8")


def test_publication_report_accepts_signed_https_metadata(tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    report = publication.build_publication_report(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        status_code=200,
        body=body,
        local_metadata_path=local_metadata,
        final_url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
    )

    assert report["ok"] is True
    assert report["requireSignatures"] is True
    assert report["metadataSha256"] == sha256(body).hexdigest()
    assert report["metadataMatchesLocal"] is True
    assert report["failures"] == []


def test_publication_report_rejects_non_https_final_url(tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    report = publication.build_publication_report(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        status_code=200,
        body=body,
        local_metadata_path=local_metadata,
        final_url="http://example.test/latest.json",
    )

    assert report["ok"] is False
    assert "updater publication finalUrl must be absolute HTTPS" in report["failures"]


def test_publication_report_rejects_non_https_url(tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    report = publication.build_publication_report(
        url="http://example.test/latest.json",
        status_code=200,
        body=body,
        local_metadata_path=local_metadata,
    )

    assert report["ok"] is False
    assert "updater publication URL must be absolute HTTPS" in report["failures"]


def test_publication_report_rejects_unsigned_metadata(tmp_path: Path) -> None:
    body = signed_latest_json_bytes(signature="")
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    report = publication.build_publication_report(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        status_code=200,
        body=body,
        local_metadata_path=local_metadata,
        final_url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
    )

    assert report["ok"] is False
    assert any("signature is required" in failure for failure in report["failures"])


def test_publication_report_rejects_local_mismatch(tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(signed_latest_json_bytes(signature="other-signature"))

    report = publication.build_publication_report(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        status_code=200,
        body=body,
        local_metadata_path=local_metadata,
        final_url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
    )

    assert report["ok"] is False
    assert report["metadataMatchesLocal"] is False
    assert "published latest.json SHA256 does not match local latest.json" in report["failures"]


def test_verify_publication_uses_fetcher_and_writes_expected_report(monkeypatch, tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    def fake_fetch(url: str, *, timeout_sec: float) -> tuple[int, bytes, str]:
        assert url == "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
        assert timeout_sec == 3.0
        return 200, body, url

    monkeypatch.setattr(publication, "fetch_published_metadata", fake_fetch)

    report = publication.verify_publication(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        local_metadata_path=local_metadata,
        timeout_sec=3.0,
    )

    assert report["ok"] is True
    assert report["statusCode"] == 200
    assert report["metadataSha256"] == sha256(body).hexdigest()
    assert report["attempt"] == 1
    assert report["attempts"] == 1


def test_verify_publication_rejects_non_https_redirect_target(monkeypatch, tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)

    def fake_fetch(url: str, *, timeout_sec: float) -> tuple[int, bytes, str]:
        return 200, body, "http://example.test/latest.json"

    monkeypatch.setattr(publication, "fetch_published_metadata", fake_fetch)

    report = publication.verify_publication(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        local_metadata_path=local_metadata,
    )

    assert report["ok"] is False
    assert report["finalUrl"] == "http://example.test/latest.json"
    assert "updater publication finalUrl must be absolute HTTPS" in report["failures"]


def test_verify_publication_retries_until_metadata_is_available(monkeypatch, tmp_path: Path) -> None:
    body = signed_latest_json_bytes()
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(body)
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_sec: float) -> tuple[int, bytes, str]:
        calls.append(url)
        if len(calls) == 1:
            return 404, b"not found", url
        return 200, body, url

    monkeypatch.setattr(publication, "fetch_published_metadata", fake_fetch)
    monkeypatch.setattr(publication.time, "sleep", lambda _seconds: None)

    report = publication.verify_publication(
        url="https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        local_metadata_path=local_metadata,
        attempts=2,
        retry_delay_sec=0.01,
    )

    assert report["ok"] is True
    assert report["attempt"] == 2
    assert report["attempts"] == 2
    assert len(calls) == 2


def test_verify_publication_does_not_fetch_non_https_url(monkeypatch, tmp_path: Path) -> None:
    local_metadata = tmp_path / "latest.json"
    local_metadata.write_bytes(signed_latest_json_bytes())

    def forbidden_fetch(url: str, *, timeout_sec: float) -> tuple[int, bytes, str]:
        raise AssertionError("non-HTTPS URL should not be fetched")

    monkeypatch.setattr(publication, "fetch_published_metadata", forbidden_fetch)

    report = publication.verify_publication(
        url="http://example.test/latest.json",
        local_metadata_path=local_metadata,
    )

    assert report["ok"] is False
    assert report["statusCode"] == 0
    assert "updater publication URL must be absolute HTTPS" in report["failures"]
