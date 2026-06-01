from __future__ import annotations

import pytest

from src.version import normalize_version


def test_normalize_version_accepts_semver():
    assert normalize_version("0.1.0") == "0.1.0"
    assert normalize_version("v1.2.3") == "1.2.3"
    assert normalize_version("1.2.3-beta.1") == "1.2.3-beta.1"


def test_normalize_version_rejects_invalid_value():
    with pytest.raises(ValueError, match="Invalid Scriber version"):
        normalize_version("release")
