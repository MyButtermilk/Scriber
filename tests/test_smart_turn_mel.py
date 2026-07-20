from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from src.runtime import smart_turn_mel


def _restore_filters(module: object, original: object) -> None:
    module._MEL_FILTERS = original


def test_bundled_smart_turn_mel_model_matches_attested_identity() -> None:
    model = Path(smart_turn_mel.__file__).with_name("smart-turn-mel-matmul.onnx")
    assert model.stat().st_size == 128_859
    assert hashlib.sha256(model.read_bytes()).hexdigest() == smart_turn_mel._MODEL_SHA256


def test_onnx_mel_projection_is_byte_exact_and_idempotent() -> None:
    from pipecat.audio.turn.smart_turn import _whisper_features

    original = _whisper_features._MEL_FILTERS
    rng = np.random.default_rng(20260720)
    audio = rng.standard_normal(128_000).astype(np.float32) * np.float32(0.05)
    try:
        reference = _whisper_features.compute_whisper_log_mel_features(
            audio,
            do_normalize=True,
        )
        assert smart_turn_mel.install_smart_turn_mel_acceleration(force=True) is True
        accelerated = _whisper_features.compute_whisper_log_mel_features(
            audio,
            do_normalize=True,
        )
        assert smart_turn_mel.install_smart_turn_mel_acceleration(force=True) is False
        assert np.array_equal(accelerated, reference)
    finally:
        _restore_filters(_whisper_features, original)


def test_mel_filter_drift_is_rejected_before_install() -> None:
    from pipecat.audio.turn.smart_turn import _whisper_features

    original = _whisper_features._MEL_FILTERS
    drifted = np.array(original, copy=True)
    drifted[0, 0] += 1.0
    _whisper_features._MEL_FILTERS = drifted
    try:
        with pytest.raises(RuntimeError, match="do not match"):
            smart_turn_mel.install_smart_turn_mel_acceleration(force=True)
    finally:
        _restore_filters(_whisper_features, original)
