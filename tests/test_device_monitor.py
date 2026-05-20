import types

import src.device_monitor as device_monitor


class _RecordingLock:
    def __init__(self) -> None:
        self.held = False
        self.enter_count = 0

    def __enter__(self):
        self.held = True
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        return False


def test_enumerate_microphones_queries_portaudio_under_guard(monkeypatch):
    guard = _RecordingLock()
    query_checked_guard = False

    class _FakeSoundDevice:
        default = types.SimpleNamespace(device=(0, None), hostapi=0)

        def query_devices(self):
            nonlocal query_checked_guard
            query_checked_guard = True
            assert guard.held
            return [{"name": "USB Mic, MME", "max_input_channels": 1, "hostapi": 0}]

        def query_hostapis(self):
            return [{"name": "MME"}]

        def check_input_settings(self, **kwargs):
            return None

    monkeypatch.setattr(device_monitor, "_DEVICE_GUARD_LOCK", guard)
    monkeypatch.setattr(device_monitor, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(device_monitor, "sd", _FakeSoundDevice())

    devices = device_monitor._enumerate_microphones()

    assert query_checked_guard is True
    assert guard.enter_count == 1
    assert devices == [
        {"deviceId": "default", "label": "Default"},
        {"deviceId": "USB Mic, MME", "label": "USB Mic, MME (Default)"},
    ]
