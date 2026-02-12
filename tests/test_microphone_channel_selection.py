import numpy as np

from src.microphone import _determine_capture_channels, _select_best_mono_channel


def test_select_best_mono_channel_prefers_stronger_channel() -> None:
    # Channel 0 is nearly silent, channel 1 carries speech-like signal.
    indata = np.array(
        [
            [5, 1200],
            [4, -1100],
            [6, 900],
            [3, -950],
        ],
        dtype=np.int16,
    )

    mono, chosen = _select_best_mono_channel(indata, previous_channel=0)
    assert chosen == 1
    assert mono.shape == (4, 1)
    assert np.array_equal(mono[:, 0], indata[:, 1])


def test_select_best_mono_channel_keeps_previous_when_similar_energy() -> None:
    # Energies are close; keep previous channel to avoid audible channel flapping.
    indata = np.array(
        [
            [900, 950],
            [-900, -930],
            [850, 920],
            [-850, -910],
        ],
        dtype=np.int16,
    )

    mono, chosen = _select_best_mono_channel(indata, previous_channel=0)
    assert chosen == 0
    assert np.array_equal(mono[:, 0], indata[:, 0])


def test_select_best_mono_channel_noop_for_single_channel() -> None:
    indata = np.array([[100], [-120], [90]], dtype=np.int16)

    mono, chosen = _select_best_mono_channel(indata, previous_channel=0)
    assert chosen == 0
    assert mono.shape == (3, 1)
    assert np.array_equal(mono, indata)


def test_determine_capture_channels_uses_up_to_eight_channels_for_mono() -> None:
    assert _determine_capture_channels(1, 1) == 1
    assert _determine_capture_channels(1, 2) == 2
    assert _determine_capture_channels(1, 4) == 4
    assert _determine_capture_channels(1, 8) == 8
    assert _determine_capture_channels(1, 12) == 8


def test_select_best_mono_channel_handles_four_channel_arrays() -> None:
    indata = np.array(
        [
            [10, 20, 30, 2000],
            [8, -18, 35, -1900],
            [12, 22, 33, 2100],
            [9, -20, 28, -2050],
        ],
        dtype=np.int16,
    )

    mono, chosen = _select_best_mono_channel(indata, previous_channel=0)
    assert chosen == 3
    assert np.array_equal(mono[:, 0], indata[:, 3])
