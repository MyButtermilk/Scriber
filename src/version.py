from __future__ import annotations

import os
import re

__version__ = "0.5.25"

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def normalize_version(value: str | None) -> str:
    version = (value or __version__).strip()
    if version.startswith("v"):
        version = version[1:]
    if not _SEMVER_RE.match(version):
        raise ValueError(f"Invalid Scriber version: {value!r}")
    return version


def app_version() -> str:
    return normalize_version(os.getenv("SCRIBER_VERSION", __version__))
