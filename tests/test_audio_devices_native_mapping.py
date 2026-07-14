import types
import warnings

from src.audio_devices import (
    build_input_endpoint_mappings,
    collect_native_capture_endpoint_inventory,
    hash_native_endpoint_id,
    input_endpoint_mapping_diagnostics,
    normalize_native_endpoint_inventory,
)


def _fake_sounddevice():
    devices = [
        {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 2, "hostapi": 1},
        {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
        {"name": "Speaker Output, Windows WASAPI", "max_input_channels": 0, "hostapi": 1},
    ]
    return types.SimpleNamespace(
        default=types.SimpleNamespace(device=(0, None), hostapi=1),
        query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
        query_devices=lambda device=None, kind=None: (
            devices
            if device is None and kind is None
            else devices[0 if device is None else int(device)]
        ),
        check_input_settings=lambda **_kwargs: None,
    )


def test_hash_native_endpoint_id_is_stable_and_redacted():
    raw = r"SWD\MMDEVAPI\{0.0.1.00000000}.{secret-device-guid}"

    hashed = hash_native_endpoint_id(raw)

    assert hashed == hash_native_endpoint_id(raw)
    assert len(hashed) == 16
    assert "secret-device-guid" not in hashed


def test_normalize_native_endpoint_inventory_filters_render_and_redacts_ids():
    raw_capture_id = r"SWD\MMDEVAPI\{0.0.1.00000000}.{capture-guid}"
    entries = normalize_native_endpoint_inventory(
        [
            {
                "endpointId": raw_capture_id,
                "friendlyName": "Dock Mic",
                "flow": "capture",
                "isDefault": True,
            },
            {
                "endpointId": r"SWD\MMDEVAPI\{0.0.0.00000000}.{render-guid}",
                "friendlyName": "Speaker Output",
                "flow": "render",
            },
            {
                "endpointId": r"SWD\MMDEVAPI\{0.0.2.00000000}.{unknown-render-guid}",
                "friendlyName": "Speaker Output",
                "flow": "unknown",
            },
        ]
    )

    assert len(entries) == 1
    assert entries[0].friendly_name == "Dock Mic"
    assert entries[0].endpoint_id_hash == hash_native_endpoint_id(raw_capture_id)
    assert raw_capture_id not in entries[0].endpoint_id_hash
    assert entries[0].is_default is True


def test_input_endpoint_mapping_matches_by_normalized_name_and_marks_favorite():
    sd = _fake_sounddevice()
    native_id = r"SWD\MMDEVAPI\{0.0.1.00000000}.{dock-guid}"
    mappings = build_input_endpoint_mappings(
        sd,
        favorite_name="Mikrofon (2- Dock Mic), MME",
        native_endpoints=[
            {
                "endpointId": native_id,
                "friendlyName": "Dock Mic",
                "flow": "capture",
                "isDefault": True,
            }
        ],
    )

    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.portaudio_name == "Dock Mic, Windows WASAPI"
    assert mapping.normalized_name == "dock mic"
    assert mapping.favorite_mic_normalized == "dock mic"
    assert mapping.native_endpoint_id_hash == hash_native_endpoint_id(native_id)
    assert mapping.native_default_input_endpoint_id_hash == hash_native_endpoint_id(native_id)
    assert mapping.match_confidence == "name"
    assert mapping.to_diagnostic_dict()["nativeEndpointIdHash"] == hash_native_endpoint_id(native_id)


def test_input_endpoint_mapping_reports_unavailable_native_inventory_without_raw_ids():
    diagnostics = input_endpoint_mapping_diagnostics(
        _fake_sounddevice(),
        favorite_name="Dock Mic",
        native_endpoints=None,
    )

    assert diagnostics["available"] is True
    assert diagnostics["nativeInventoryAvailable"] is False
    assert diagnostics["favoriteMicNormalized"] == "dock mic"
    assert diagnostics["mappings"][0]["nativeEndpointIdHash"] is None
    assert diagnostics["mappings"][0]["matchReason"] == "nativeInventoryUnavailable"


def test_collect_native_capture_endpoint_inventory_is_redacted_and_filters_render():
    class _Device:
        def __init__(self, device_id: str, friendly_name: str, flow: str):
            self.DeviceID = device_id
            self.FriendlyName = friendly_name
            self.flow = flow

    capture_id = r"SWD\MMDEVAPI\{0.0.1.00000000}.{capture-guid}"
    render_id = r"SWD\MMDEVAPI\{0.0.0.00000000}.{render-guid}"
    capture = _Device(capture_id, "Dock Mic", "capture")
    render = _Device(render_id, "Speaker Output", "render")
    audio_utilities = types.SimpleNamespace(
        GetAllDevices=lambda: [capture, render],
        GetMicrophone=lambda: capture,
    )

    inventory = collect_native_capture_endpoint_inventory(audio_utilities)

    assert inventory == [
        {
            "endpointIdHash": hash_native_endpoint_id(capture_id),
            "friendlyName": "Dock Mic",
            "flow": "capture",
            "isDefault": True,
        }
    ]
    assert capture_id not in str(inventory)
    assert render_id not in str(inventory)


def test_collect_native_capture_endpoint_inventory_returns_three_redacted_microphones():
    class _Device:
        def __init__(self, device_id: str, friendly_name: str, flow: str):
            self.DeviceID = device_id
            self.FriendlyName = friendly_name
            self.flow = flow

    raw_capture_ids = [
        rf"SWD\MMDEVAPI\{{0.0.1.00000000}}.{{private-capture-{index}}}"
        for index in range(3)
    ]
    captures = [
        _Device(raw_capture_ids[0], "Jabra Engage 75", "capture"),
        _Device(raw_capture_ids[1], "Insta360 Link", "capture"),
        _Device(raw_capture_ids[2], "Realtek Microphone Array", "capture"),
    ]
    raw_render_id = r"SWD\MMDEVAPI\{0.0.0.00000000}.{private-render}"
    render = _Device(raw_render_id, "Desk Speakers", "render")
    audio_utilities = types.SimpleNamespace(
        GetAllDevices=lambda: [*captures, render],
        GetMicrophone=lambda: captures[1],
    )

    inventory = collect_native_capture_endpoint_inventory(audio_utilities)

    assert [entry["friendlyName"] for entry in inventory] == [
        "Jabra Engage 75",
        "Insta360 Link",
        "Realtek Microphone Array",
    ]
    assert [entry["endpointIdHash"] for entry in inventory] == [
        hash_native_endpoint_id(endpoint_id) for endpoint_id in raw_capture_ids
    ]
    assert [entry["isDefault"] for entry in inventory] == [False, True, False]
    serialized = str(inventory)
    assert all(endpoint_id not in serialized for endpoint_id in raw_capture_ids)
    assert raw_render_id not in serialized


def test_collect_native_capture_endpoint_inventory_infers_flow_and_filters_inactive_devices():
    class _Device:
        def __init__(self, device_id: str, friendly_name: str, state: str):
            self.DeviceID = device_id
            self.FriendlyName = friendly_name
            self.state = state

    active_capture = _Device(
        r"SWD\MMDEVAPI\{0.0.1.00000000}.{active-capture}",
        "Conference microphone",
        "AudioDeviceState.Active",
    )
    inactive_capture = _Device(
        r"SWD\MMDEVAPI\{0.0.1.00000000}.{inactive-capture}",
        "Old conference microphone",
        "AudioDeviceState.NotPresent",
    )
    active_render = _Device(
        r"SWD\MMDEVAPI\{0.0.0.00000000}.{active-render}",
        "Conference speakers",
        "AudioDeviceState.Active",
    )
    audio_utilities = types.SimpleNamespace(
        GetAllDevices=lambda: [active_capture, inactive_capture, active_render],
        GetMicrophone=lambda: active_capture,
    )

    inventory = collect_native_capture_endpoint_inventory(audio_utilities)

    assert inventory == [
        {
            "endpointIdHash": hash_native_endpoint_id(active_capture.DeviceID),
            "friendlyName": "Conference microphone",
            "flow": "capture",
            "isDefault": True,
        }
    ]
    serialized = str(inventory)
    assert inactive_capture.DeviceID not in serialized
    assert active_render.DeviceID not in serialized


def test_collect_native_capture_endpoint_inventory_suppresses_pycaw_property_noise():
    class _Device:
        DeviceID = "capture-id"
        FriendlyName = "Dock Mic"
        flow = "capture"

    def get_all_devices():
        warnings.warn(
            "COMError attempting to get property 26 from device",
            UserWarning,
        )
        return [_Device()]

    audio_utilities = types.SimpleNamespace(
        GetAllDevices=get_all_devices,
        GetMicrophone=lambda: _Device(),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        inventory = collect_native_capture_endpoint_inventory(audio_utilities)

    assert inventory[0]["friendlyName"] == "Dock Mic"
    assert caught == []
