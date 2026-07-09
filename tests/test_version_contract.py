from __future__ import annotations

import pytest

from src import version


def test_app_version_prefers_runtime_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBER_VERSION", "9.9.9")

    assert version.app_version() == "9.9.9"


def test_app_version_normalizes_runtime_v_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBER_VERSION", "v9.9.9")

    assert version.app_version() == "9.9.9"


def test_app_version_rejects_invalid_runtime_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRIBER_VERSION", "not-a-version")

    with pytest.raises(ValueError, match="Invalid Scriber version"):
        version.app_version()
