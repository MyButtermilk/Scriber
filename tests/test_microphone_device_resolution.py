import asyncio
import sys
import types

import pytest

from src.config import Config
from src.pipeline import _normalize_device_name as normalize_pipeline_device_name
from src.pipeline import _resolve_mic_device
from src.web_api import ScriberWebController


def _install_fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: list[dict[str, object]],
    hostapis: list[dict[str, object]],
    default_input: int = 0,
) -> None:
    module = types.SimpleNamespace()
    module.default = types.SimpleNamespace(device=(default_input, None))

    def query_hostapis():
        return hostapis

    def query_devices(device=None, kind=None):
        if device is None and kind is None:
            return devices
        if kind == "input":
            idx = default_input if device is None else int(device)
            if idx < 0 or idx >= len(devices):
                raise ValueError("invalid device index")
            dev = devices[idx]
            if int(dev.get("max_input_channels", 0) or 0) <= 0:
                raise ValueError("device has no input channels")
            return dev
        if device is None:
            return devices
        idx = int(device)
        if idx < 0 or idx >= len(devices):
            raise ValueError("invalid device index")
        return devices[idx]

    module.query_hostapis = query_hostapis
    module.query_devices = query_devices
    monkeypatch.setitem(sys.modules, "sounddevice", module)


def test_normalize_device_name_ignores_hostapi_and_unstable_index():
    a = "Mikrofon (5- Dock Mic), Windows WASAPI"
    b = "Mikrofon (2- Dock Mic), MME"
    assert normalize_pipeline_device_name(a) == normalize_pipeline_device_name(b)


def test_resolve_mic_device_prefers_favorite_when_available(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
            {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, MME", raising=False)

    resolved = _resolve_mic_device("Built-in Mic, MME")

    assert resolved == "1"


def test_resolve_mic_device_matches_favorite_across_hostapi_suffix_change(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
            {"name": "Mikrofon (7- Dock Mic), Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Mikrofon (2- Dock Mic), MME", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "1"


@pytest.mark.asyncio
async def test_get_settings_prefers_favorite_for_ui_when_available(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    monkeypatch.setattr(Config, "MIC_DEVICE", "Built-in Mic, MME", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Mikrofon (2- Dock Mic), MME", raising=False)
    monkeypatch.setattr(
        ctl,
        "list_microphones",
        lambda: [
            {"deviceId": "default", "label": "Default"},
            {"deviceId": "Built-in Mic, MME", "label": "Built-in Mic"},
            {"deviceId": "Mikrofon (7- Dock Mic), Windows WASAPI", "label": "Dock Mic"},
        ],
    )

    settings = ctl.get_settings()

    assert settings["micDevice"] == "Mikrofon (7- Dock Mic), Windows WASAPI"
    assert settings["favoriteMicAvailable"] is True


@pytest.mark.asyncio
async def test_list_microphones_dedupes_hostapi_variants(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )

    devices = ctl.list_microphones()
    ids = [d["deviceId"] for d in devices]

    assert "Dock Mic, MME" in ids
    assert "Dock Mic, Windows WASAPI" not in ids
