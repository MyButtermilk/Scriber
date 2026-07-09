from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

import pyloudnorm


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_local_pyloudnorm_meter_returns_loudness_without_scipy() -> None:
    sample_rate = 16_000
    duration = 0.4
    t = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    audio = 0.2 * np.sin(2 * np.pi * 440.0 * t)

    loudness = pyloudnorm.Meter(sample_rate, block_size=duration).integrated_loudness(audio)

    assert np.isfinite(loudness)
    assert str(REPO_ROOT / "pyloudnorm") in pyloudnorm.__file__


def test_pipecat_audio_utils_uses_local_pyloudnorm_without_scipy_import() -> None:
    script = (
        "import json, sys; "
        "import pyloudnorm; "
        "from pipecat.audio.utils import calculate_audio_volume; "
        "audio=(b'\\x00\\x01' * 6400); "
        "volume=calculate_audio_volume(audio, 16000); "
        "print(json.dumps({"
        "'pyloudnorm_file': pyloudnorm.__file__, "
        "'scipy_loaded': 'scipy' in sys.modules, "
        "'volume': volume"
        "}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )

    payload = json.loads(completed.stdout)
    assert str(REPO_ROOT / "pyloudnorm") in payload["pyloudnorm_file"]
    assert payload["scipy_loaded"] is False
    assert 0 <= payload["volume"] <= 1
