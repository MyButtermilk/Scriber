import asyncio
import sys
import types

import pytest

from src.audio_devices import get_default_input_device_index
from src.config import Config
from src.pipeline import _normalize_device_name as normalize_pipeline_device_name
from src.pipeline import _resolve_mic_device
from src.pipeline import invalidate_mic_device_resolution_cache
from src.web_api import ScriberWebController


@pytest.fixture(autouse=True)
def _disable_background_device_monitor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")


def _install_fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: list[dict[str, object]],
    hostapis: list[dict[str, object]],
    default_input: int = 0,
    default_hostapi: int | None = None,
    invalid_check_indices: set[int] | None = None,
) -> types.SimpleNamespace:
    module = types.SimpleNamespace()
    if default_hostapi is None and 0 <= default_input < len(devices):
        try:
            default_hostapi = int(devices[default_input].get("hostapi", 0))
        except (TypeError, ValueError):
            default_hostapi = 0
    module.default = types.SimpleNamespace(device=(default_input, None), hostapi=default_hostapi)

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

    def check_input_settings(device=None, samplerate=None, channels=None, dtype=None):
        idx = default_input if device is None else int(device)
        if invalid_check_indices and idx in invalid_check_indices:
            raise ValueError("invalid sample rate")
        if idx < 0 or idx >= len(devices):
            raise ValueError("invalid device index")
        dev = devices[idx]
        max_input = int(dev.get("max_input_channels", 0) or 0)
        if max_input <= 0:
            raise ValueError("device has no input channels")
        ch = 1 if channels is None else int(channels)
        if ch <= 0 or ch > max_input:
            raise ValueError("invalid channel count")
        return None

    module.check_input_settings = check_input_settings
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    invalidate_mic_device_resolution_cache()
    return module


def test_normalize_device_name_ignores_hostapi_and_unstable_index():
    a = "Mikrofon (5- Dock Mic), Windows WASAPI"
    b = "Mikrofon (2- Dock Mic), MME"
    assert normalize_pipeline_device_name(a) == normalize_pipeline_device_name(b)


def test_normalize_device_name_matches_mic_wrapper_and_plain_name():
    a = "Mikrofon (5- Dock Mic), Windows WASAPI"
    b = "Dock Mic, MME"
    assert normalize_pipeline_device_name(a) == normalize_pipeline_device_name(b)


def test_get_default_input_device_index_supports_pair_like_default_device() -> None:
    class _PairLike:
        def __init__(self, input_idx: int, output_idx: int) -> None:
            self._values = (input_idx, output_idx)

        def __getitem__(self, index: int) -> int:
            return self._values[index]

    fake_sd = types.SimpleNamespace(
        default=types.SimpleNamespace(device=_PairLike(2, 7))
    )

    assert get_default_input_device_index(fake_sd) == 2


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


def test_resolve_mic_device_prefers_active_hostapi_variant(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, MME", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "0"


def test_resolve_mic_device_falls_back_to_next_host_variant_on_invalid_sample_rate(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Mikrofon (4- Insta360 Link), Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Mikrofon (4- Insta360 Link), MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
        invalid_check_indices={0},
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Mikrofon (4- Insta360 Link)", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "1"


def test_resolve_mic_device_skips_unavailable_favorite_and_uses_available_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
        invalid_check_indices={0},
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, Windows WASAPI", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "1"


def test_resolve_mic_device_uses_nondefault_when_system_default_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
        invalid_check_indices={0},
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "1"


def test_resolve_mic_device_fallback_ignores_soundmapper(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Microsoft Soundmapper - Input", "max_input_channels": 2, "hostapi": 0},
            {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 4, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    resolved = _resolve_mic_device("default")

    assert resolved == "1"


def test_resolve_mic_device_uses_cache_for_repeated_resolution(monkeypatch: pytest.MonkeyPatch):
    query_count = 0
    module = _install_fake_sounddevice(
        monkeypatch,
        devices=[
            {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
            {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
        ],
        hostapis=[{"name": "MME"}],
        default_input=0,
    )
    original_query_devices = module.query_devices

    def counting_query_devices(*args, **kwargs):
        nonlocal query_count
        query_count += 1
        return original_query_devices(*args, **kwargs)

    module.query_devices = counting_query_devices
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, MME", raising=False)

    assert _resolve_mic_device("default") == "1"
    first_query_count = query_count
    assert first_query_count > 0

    assert _resolve_mic_device("default") == "1"
    assert query_count == first_query_count


@pytest.mark.asyncio
async def test_get_settings_prefers_favorite_for_ui_when_available(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
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
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_get_settings_falls_back_to_first_available_when_favorite_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        monkeypatch.setattr(Config, "MIC_DEVICE", "Mikrofon (7- Dock Mic), Windows WASAPI", raising=False)
        monkeypatch.setattr(Config, "FAVORITE_MIC", "Mikrofon (7- Dock Mic), Windows WASAPI", raising=False)
        monkeypatch.setattr(
            ctl,
            "list_microphones",
            lambda: [
                {"deviceId": "default", "label": "Default"},
                {"deviceId": "Built-in Mic, MME", "label": "Built-in Mic"},
            ],
        )

        settings = ctl.get_settings()

        assert settings["micDevice"] == "Built-in Mic, MME"
        assert settings["favoriteMicAvailable"] is False
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_dedupes_hostapi_variants(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
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

        assert "Dock Mic, Windows WASAPI" in ids
        assert "Dock Mic, MME" not in ids
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_trusts_default_only_monitor_cache(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        ctl._device_monitor_enabled = True
        monkeypatch.setattr(
            ctl._device_monitor,
            "get_devices",
            lambda: [{"deviceId": "default", "label": "Default"}],
        )
        monkeypatch.setattr(
            "src.web_api.list_unique_input_microphones",
            lambda *_args, **_kwargs: pytest.fail("cached monitor result must avoid PortAudio enumeration"),
        )

        assert ctl.list_microphones() == [{"deviceId": "default", "label": "Default"}]
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_prefers_wasapi_even_if_default_points_to_mme(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice(
            monkeypatch,
            devices=[
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
            default_input=0,
        )

        devices = ctl.list_microphones()
        ids = [d["deviceId"] for d in devices]

        assert "Dock Mic, Windows WASAPI" in ids
        assert "Dock Mic, MME" not in ids
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_uses_single_hostapi_list(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice(
            monkeypatch,
            devices=[
                {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "USB Mic, MME", "max_input_channels": 1, "hostapi": 0},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
            default_input=0,
        )

        devices = ctl.list_microphones()
        ids = [d["deviceId"] for d in devices]

        assert "Dock Mic, Windows WASAPI" in ids
        assert "Dock Mic, MME" not in ids
        assert "USB Mic, MME" not in ids
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_prefers_directsound_over_mme_when_wasapi_unusable(
    monkeypatch: pytest.MonkeyPatch,
):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice(
            monkeypatch,
            devices=[
                {"name": "Mic Device, MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "Mic Device, Windows DirectSound", "max_input_channels": 1, "hostapi": 1},
                {"name": "Mic Device, Windows WASAPI", "max_input_channels": 1, "hostapi": 2},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}],
            default_input=0,
            invalid_check_indices={2},
        )

        devices = ctl.list_microphones()
        ids = [d["deviceId"] for d in devices]

        assert "Mic Device, Windows DirectSound" in ids
        assert "Mic Device, MME" not in ids
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_falls_back_when_only_mme_has_inputs(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice(
            monkeypatch,
            devices=[
                {"name": "Input ()", "max_input_channels": 1, "hostapi": 1},
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
            default_input=0,
        )

        devices = ctl.list_microphones()
        ids = [d["deviceId"] for d in devices]

        assert "Input ()" not in ids
        assert "Dock Mic, MME" in ids
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_list_microphones_skips_unavailable_endpoints(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice(
            monkeypatch,
            devices=[
                {"name": "Microphone Array (Realtek) , MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "Mikrofon (5- Insta360 Link), Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
            default_input=0,
            invalid_check_indices={1},
        )

        devices = ctl.list_microphones()
        ids = [d["deviceId"] for d in devices]

        assert "Microphone Array (Realtek) , MME" in ids
        assert "Mikrofon (5- Insta360 Link), Windows WASAPI" not in ids
    finally:
        ctl.shutdown()
