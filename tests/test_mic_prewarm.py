import types

from src.config import Config
from src.device_monitor import get_active_stream_count
from src.mic_prewarm import MicrophonePrewarmManager
import src.mic_prewarm as mic_prewarm


class _FakeInputStream:
    instances: list["_FakeInputStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        _FakeInputStream.instances.append(self)

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


def _install_fake_sounddevice(monkeypatch):
    _FakeInputStream.instances.clear()
    fake_sd = types.SimpleNamespace(
        default=types.SimpleNamespace(device=(0, None), hostapi=0),
        InputStream=lambda **kwargs: _FakeInputStream(**kwargs),
        query_hostapis=lambda: [{"name": "MME"}],
        query_devices=lambda device=None, kind=None: (
            [
                {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
            ]
            if device is None and kind is None
            else {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0}
        ),
        check_input_settings=lambda **_kwargs: None,
    )
    monkeypatch.setattr(mic_prewarm, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(mic_prewarm, "sd", fake_sd)
    return fake_sd


def test_mic_prewarm_starts_and_stops_idle_stream(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    before_count = get_active_stream_count()

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    assert manager.is_active is True
    assert get_active_stream_count() == before_count + 1
    assert _FakeInputStream.instances[-1].kwargs["device"] == 0

    manager.stop()

    assert manager.is_active is False
    assert _FakeInputStream.instances[-1].closed is True
    assert get_active_stream_count() == before_count


def test_mic_prewarm_stays_paused_during_active_capture(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()
    assert manager.start_if_enabled() is True
    manager.pause_for_active_capture()

    assert manager.is_active is False
    assert manager.start_if_enabled() is False
    assert len(_FakeInputStream.instances) == 1

    assert manager.resume_after_active_capture() is True
    assert manager.is_active is True
    assert len(_FakeInputStream.instances) == 2

    manager.stop()


def test_mic_prewarm_stays_paused_during_device_refresh(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()
    assert manager.start_if_enabled() is True
    manager.quiesce_for_device_refresh()

    assert manager.is_active is False
    assert manager.start_if_enabled() is False
    assert len(_FakeInputStream.instances) == 1

    assert manager.resume_after_device_refresh() is True
    assert manager.is_active is True
    assert len(_FakeInputStream.instances) == 2

    manager.stop()


def test_mic_prewarm_honors_disabled_setting(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is False
    assert manager.is_active is False
    assert _FakeInputStream.instances == []
