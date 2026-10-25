from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pipecat_audio_volume_uses_loudness_without_scipy_import() -> None:
    # Pipecat 1.8+ measures ITU-R BS.1770 loudness over 400 ms with the
    # NumPy-only `loudness` package; SciPy must stay out of the backend.
    script = (
        "import json, sys; "
        "from pipecat.audio.utils import calculate_audio_volume; "
        "audio=(b'\\x00\\x10\\x00\\xf0' * 3200); "
        "volume=calculate_audio_volume(audio, 16000); "
        "print(json.dumps({"
        "'loudness_loaded': 'loudness' in sys.modules, "
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

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["loudness_loaded"] is True
    assert payload["scipy_loaded"] is False
    assert 0 < payload["volume"] <= 1
