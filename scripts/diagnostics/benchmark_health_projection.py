"""Paired health-projection benchmark with isolated real path resolution.

The baseline builds the full diagnostic inventory, as the former health path
did. The candidate calls the production narrow health projection. No backend,
microphone, provider, or user database is started.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import web_api


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    if not 20 <= args.iterations <= 1000:
        parser.error("Use 20–1000 iterations.")
    with tempfile.TemporaryDirectory(prefix="scriber-health-benchmark-") as temp:
        controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
        controller._started_at_iso = "2026-09-08T00:00:00+02:00"
        controller._started_at_monotonic = time.monotonic() - 5
        controller._session_id = None
        controller._recording_state_machine = SimpleNamespace(state=SimpleNamespace(value="idle"))
        controller._downloads_dir = Path(temp) / "downloads"
        controller._validate_ws_contracts = True
        controller._transcripts_loaded = True
        controller._device_monitor_enabled = False
        samples = {"baseline": [], "candidate": []}
        with patch.dict("os.environ", {"SCRIBER_DATA_DIR": temp}):
            keys = set(controller.get_health()) - {"ok", "ready"}

            def baseline():
                full = controller.get_runtime_info()
                return {"ok": True, "ready": True, **{key: full[key] for key in keys}}

            first = baseline()
            second = controller.get_health()
            assert {k: v for k, v in first.items() if k != "uptimeSeconds"} == {
                k: v for k, v in second.items() if k != "uptimeSeconds"
            }
            for index in range(args.iterations):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for name in order:
                    started = time.perf_counter()
                    (baseline if name == "baseline" else controller.get_health)()
                    samples[name].append((time.perf_counter() - started) * 1000)
    result = {"iterations": args.iterations, "samePublicFields": True, "alternatingOrder": True}
    for name, values in samples.items():
        values.sort()
        result[name] = {
            "medianMs": round(statistics.median(values), 4),
            "p95Ms": round(values[math.ceil(len(values) * 0.95) - 1], 4),
        }
    result["medianReductionPercent"] = round(
        (1 - statistics.median(samples["candidate"]) / statistics.median(samples["baseline"])) * 100, 2
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
