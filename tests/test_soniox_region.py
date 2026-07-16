from __future__ import annotations

import pytest

from src.soniox_region import (
    normalize_soniox_region,
    soniox_realtime_websocket_url,
    soniox_rest_api_base_url,
)


def test_soniox_region_defaults_to_us_and_rejects_unknown_settings_values():
    assert normalize_soniox_region(None) == "us"
    assert normalize_soniox_region("") == "us"
    assert normalize_soniox_region("unexpected") == "us"
    assert normalize_soniox_region(" EU ", strict=True) == "eu"

    with pytest.raises(ValueError, match="Invalid Soniox region"):
        normalize_soniox_region("jp", strict=True)


def test_soniox_region_resolves_official_rest_and_realtime_domains():
    assert soniox_rest_api_base_url("us") == "https://api.soniox.com/v1"
    assert soniox_realtime_websocket_url("us") == (
        "wss://stt-rt.soniox.com/transcribe-websocket"
    )
    assert soniox_rest_api_base_url("eu") == "https://api.eu.soniox.com/v1"
    assert soniox_realtime_websocket_url("eu") == (
        "wss://stt-rt.eu.soniox.com/transcribe-websocket"
    )
