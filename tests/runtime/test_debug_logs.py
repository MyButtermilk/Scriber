from __future__ import annotations

from src.runtime import debug_logs
from src.runtime import log_clear_state


def test_collect_debug_logs_strips_nul_padding_and_clear_marker_hides_old_entries(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir(parents=True)
    repo_dir.mkdir()
    monkeypatch.setattr(debug_logs, "data_dir", lambda: data_dir)
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs_dir)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: repo_dir)
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: logs_dir)

    log_path = logs_dir / "latest.log"
    log_path.write_bytes(
        b"\x00\x00\x00... 12:00:00.000 INFO  [web_api    ] [------] [web_api        ] before clear\n"
    )

    before_payload = debug_logs.collect_debug_logs(limit=20)
    before_entries = [item for item in before_payload["items"] if item["source"] == "latest.log"]
    assert len(before_entries) == 1
    assert before_entries[0]["message"].endswith("before clear")
    assert "\x00" not in before_entries[0]["message"]

    clear_payload = debug_logs.clear_debug_logs()
    assert clear_payload["ok"] is True
    assert "latest.log" in clear_payload["clearedSources"]
    assert log_path.read_bytes().startswith(b"\x00\x00\x00")

    cleared_payload = debug_logs.collect_debug_logs(limit=20)
    assert not [item for item in cleared_payload["items"] if item["source"] == "latest.log"]

    with log_path.open("ab") as handle:
        handle.write(b"... 12:00:01.000 INFO  [web_api    ] [------] [web_api        ] after clear\n")

    after_payload = debug_logs.collect_debug_logs(limit=20)
    after_messages = [item["message"] for item in after_payload["items"] if item["source"] == "latest.log"]
    assert after_messages == ["[web_api    ] [------] [web_api        ] after clear"]
