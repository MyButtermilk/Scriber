"""Check diagnostic on/off against an explicitly isolated running backend.

The token is read from SCRIBER_SMOKE_SESSION_TOKEN and is never printed. Supply
the isolated data directory; this mutates only that test runtime's settings.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("SCRIBER_SMOKE_SESSION_TOKEN")
    if not token or args.port == 8765:
        parser.error("Use an isolated non-production port and SCRIBER_SMOKE_SESSION_TOKEN.")

    def request(path, body=None):
        req = Request(
            f"http://127.0.0.1:{args.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method="PUT" if body is not None else "GET",
            headers={"Content-Type": "application/json", "X-Scriber-Token": token},
        )
        with urlopen(req, timeout=20) as response:
            return json.load(response)

    def sizes():
        root = args.data_dir / "logs"
        return {path.name: path.stat().st_size for path in root.glob("*") if path.is_file()}

    health = request("/api/health")
    library = request("/api/podcasts")
    before = request("/api/runtime/logs")
    assert before["loggingEnabled"] is False, "Runtime must start with logging disabled."
    startup_sizes = sizes()
    assert not startup_sizes, "Disabled startup wrote diagnostic files."
    assert request("/api/settings", {"diagnosticLoggingEnabled": True})["diagnosticLoggingEnabled"] is True
    enabled_logs = request("/api/runtime/logs")
    assert enabled_logs["loggingEnabled"] is True
    structured_path = args.data_dir / "logs" / "latest.structured.jsonl"
    records = [json.loads(line) for line in structured_path.read_text(encoding="utf-8").splitlines()]
    operation_events = [
        entry
        for entry in records
        if entry.get("record", {}).get("extra", {}).get("event") == "runtime.operation.completed"
        and entry["record"]["extra"].get("workflow") == "settings"
    ]
    assert operation_events, "Enabling did not record the actual settings operation in the isolated log."
    assert request("/api/settings", {"diagnosticLoggingEnabled": False})["diagnosticLoggingEnabled"] is False
    disabled_sizes = sizes()
    request("/api/settings", {"visualizerBarCount": 60})
    time.sleep(1.0)
    after = request("/api/runtime/logs")
    assert after["loggingEnabled"] is False
    assert sizes() == disabled_sizes, "Diagnostic files grew after disabling."
    try:
        request("/api/settings", {"diagnosticLoggingEnabled": "false"})
    except HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("Non-boolean preference was accepted.")
    result = {
        "status": "passed",
        "health": health.get("status", health.get("ok")),
        "podcastSubscriptions": len(library["subscriptions"]),
        "disabledStartupCreatedLogs": bool(startup_sizes),
        "enabledSettingsOperationCount": len(operation_events),
        "bytesWrittenAfterDisabling": sum(sizes().values()) - sum(disabled_sizes.values()),
        "invalidBooleanRejected": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
