from src.core.hot_path_tracer import (
    CANONICAL_HOT_PATH_MARKERS,
    HotPathTracer,
)


def test_hot_path_tracer_reports_segments_in_order():
    ticks = iter([1_000_000_000, 1_050_000_000, 1_170_000_000, 1_190_000_000])
    tracer = HotPathTracer("s1", clock_ns=lambda: next(ticks))

    tracer.mark("hotkey_received")
    tracer.mark("controller_accepted")
    tracer.mark("first_final_token")
    tracer.mark("first_paste")

    report = tracer.report()
    assert report["hotkey_received_to_controller_accepted_ms"] == 50.0
    assert report["controller_accepted_to_first_final_token_ms"] == 120.0
    assert report["first_final_token_to_first_paste_ms"] == 20.0
    assert report["hotkey_received_to_first_final_token_ms"] == 170.0
    assert report["hotkey_received_to_first_paste_ms"] == 190.0


def test_hot_path_tracer_ignores_duplicate_marks():
    ticks = iter([100, 150, 200])
    tracer = HotPathTracer("s2", clock_ns=lambda: next(ticks))
    tracer.mark("a")
    tracer.mark("a")
    tracer.mark("b")

    report = tracer.report()
    assert "a_to_b_ms" in report
    assert report["a_to_b_ms"] == 0.00005


def test_hot_path_tracer_accepts_external_marker_timestamp():
    ticks = iter([1_000_000_000, 1_200_000_000])
    tracer = HotPathTracer("s4", clock_ns=lambda: next(ticks))

    tracer.mark("provider_final_received")
    tracer.mark("clipboard_set", timestamp_ns=1_040_000_000)
    tracer.mark("first_paste")

    report = tracer.report()
    assert report["provider_final_received_to_clipboard_set_ms"] == 40.0
    assert report["clipboard_set_to_first_paste_ms"] == 160.0


def test_hot_path_tracer_binds_privacy_safe_tauri_callback_marker():
    marker = {
        "schemaVersion": 1,
        "marker": "hotkey_received",
        "source": "tauri_global_shortcut",
        "runId": "7de1a48651d44f859042b7cbcb30da52",
        "sampleId": "2b3022ee3f404333a1156da089a24962",
        "processId": 4321,
        "qpcTicks": 10_000_000,
        "qpcFrequency": 10_000_000,
        "timestampNs": 1_000_000_000,
    }
    tracer = HotPathTracer("s-tauri", clock_ns=lambda: 1_125_000_000)

    tracer.bind_tauri_hotkey_received(marker)
    tracer.mark("mic_ready")
    snapshot = tracer.snapshot()

    assert snapshot["tauriHotkeyReceived"] == marker
    assert snapshot["markerNames"] == [
        "activation_received",
        "hotkey_received",
        "tauri_hotkey_received",
        "mic_ready",
    ]
    assert snapshot["referenceMarker"] == "activation_received"
    assert snapshot["segments"]["hotkey_received_to_mic_ready_ms"] == 125.0
    assert "token" not in str(snapshot).lower()
    assert "transcript" not in str(snapshot).lower()


def test_hot_path_tracer_without_enough_marks_is_empty():
    tracer = HotPathTracer("s3", clock_ns=lambda: 123)
    tracer.mark("only_one")
    assert tracer.report() == {}


def test_hot_path_tracer_exposes_canonical_kpis_without_removing_legacy_pairs():
    tracer = HotPathTracer("stage-zero", clock_ns=lambda: 0)
    tracer.mark("activation_received", timestamp_ns=1_000_000_000)
    tracer.mark("button_received", timestamp_ns=1_000_000_000)
    tracer.mark("stop_requested", timestamp_ns=1_400_000_000)
    tracer.mark("provider_final_received", timestamp_ns=1_600_000_000)
    tracer.mark("legacy_marker", timestamp_ns=1_650_000_000)
    tracer.mark("final_text_observed", timestamp_ns=1_700_000_000)

    kpis = tracer.canonical_kpis(authoritative_fixture_duration_ms=350.0)

    assert kpis == {
        "activation_received_to_final_text_observed_ms": 700.0,
        "button_received_to_final_text_observed_ms": 700.0,
        "stop_requested_to_final_text_observed_ms": 300.0,
        "stop_requested_to_provider_final_received_ms": 200.0,
        "provider_final_received_to_final_text_observed_ms": 100.0,
        "non_speech_overhead_ms": 350.0,
    }
    assert tracer.report()["legacy_marker_to_final_text_observed_ms"] == 50.0
    assert "non_speech_overhead_ms" not in tracer.canonical_kpis(
        authoritative_fixture_duration_ms=float("inf")
    )
    assert "hotkey_received" in tracer.missing_canonical_markers()
    assert set(CANONICAL_HOT_PATH_MARKERS) >= {
        "activation_received",
        "paste_requested",
        "final_text_observed",
    }


def test_hot_path_snapshot_uses_activation_reference_for_button_path():
    tracer = HotPathTracer("button", clock_ns=lambda: 0)
    tracer.mark("activation_received", timestamp_ns=100)
    tracer.mark("button_received", timestamp_ns=100)
    tracer.mark("mic_ready", timestamp_ns=1_000_100)

    snapshot = tracer.snapshot()

    assert snapshot["referenceMarker"] == "activation_received"
    assert snapshot["markers"][-1]["sinceActivationMs"] == 1.0
    assert "sinceHotkeyMs" not in snapshot["markers"][-1]
