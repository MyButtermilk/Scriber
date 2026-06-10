import types

import pytest

from src.microphone import PythonSoundDeviceFrameSource


class _FakeInputStream:
    instances: list["_FakeInputStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        self.stop_calls = 0
        _FakeInputStream.instances.append(self)

    def start(self):
        self.active = True

    def stop(self):
        self.stop_calls += 1
        self.active = False

    def close(self):
        self.closed = True


def test_python_sounddevice_frame_source_opens_configured_device_with_stable_capture_channels():
    _FakeInputStream.instances.clear()
    fake_sd = types.SimpleNamespace(
        query_devices=lambda device=None, kind=None: {
            "name": "Array Mic",
            "max_input_channels": 4,
        },
        InputStream=lambda **kwargs: _FakeInputStream(**kwargs),
    )

    source = PythonSoundDeviceFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="3",
        sd_module=fake_sd,
    )

    source.open(lambda *_args: None)
    source.start()

    stream = _FakeInputStream.instances[-1]
    assert source.engine == "python"
    assert source.name == "sounddevice"
    assert source.device_index == 3
    assert source.target_channels == 1
    assert source.capture_channels == 4
    assert stream.kwargs["device"] == 3
    assert stream.kwargs["channels"] == 4
    assert stream.kwargs["samplerate"] == 16000
    assert stream.active is True
    assert source.diagnostic_snapshot()["frameSource"] == "sounddevice"


def test_python_sounddevice_frame_source_falls_back_to_default_when_configured_open_fails():
    _FakeInputStream.instances.clear()

    def input_stream(**kwargs):
        if kwargs.get("device") == 7:
            raise RuntimeError("device busy")
        return _FakeInputStream(**kwargs)

    fake_sd = types.SimpleNamespace(
        query_devices=lambda device=None, kind=None: {
            "name": "Default Mic" if device is None else "Busy Mic",
            "max_input_channels": 2,
        },
        InputStream=input_stream,
    )

    source = PythonSoundDeviceFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="7",
        sd_module=fake_sd,
    )

    source.open(lambda *_args: None)

    stream = _FakeInputStream.instances[-1]
    assert source.device_index is None
    assert source.capture_channels == 2
    assert source.fallback_reason == "configuredDeviceOpenFailed"
    assert stream.kwargs["device"] is None
    assert stream.kwargs["channels"] == 2


def test_python_sounddevice_frame_source_stop_can_pause_or_close_stream():
    _FakeInputStream.instances.clear()
    fake_sd = types.SimpleNamespace(
        query_devices=lambda device=None, kind=None: {
            "name": "Default Mic",
            "max_input_channels": 1,
        },
        InputStream=lambda **kwargs: _FakeInputStream(**kwargs),
    )

    source = PythonSoundDeviceFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="default",
        sd_module=fake_sd,
    )

    source.open(lambda *_args: None)
    source.start()
    stream = _FakeInputStream.instances[-1]

    source.stop(close=False)
    assert stream.stop_calls == 1
    assert stream.closed is False
    assert source.stream is stream

    source.stop(close=True)
    assert stream.stop_calls == 2
    assert stream.closed is True
    assert source.stream is None


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("", None),
        ("default", None),
        (None, None),
        ("5", 5),
    ],
)
def test_python_sounddevice_frame_source_device_index_parsing(requested, expected):
    fake_sd = types.SimpleNamespace()
    source = PythonSoundDeviceFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device=requested,
        sd_module=fake_sd,
    )

    assert source._parse_device_index() == expected
