from __future__ import annotations

import os

from src.runtime import debug_logs
from src.runtime import log_clear_state
import pytest


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


def test_debug_logs_do_not_follow_symlink_outside_log_roots(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir()
    data_dir.mkdir()
    repo_dir.mkdir()
    outside = tmp_path / "private.log"
    outside.write_text("must not be exposed\n", encoding="utf-8")
    try:
        (logs_dir / "linked.log").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    monkeypatch.setattr(debug_logs, "data_dir", lambda: data_dir)
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs_dir)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: repo_dir)
    payload = debug_logs.collect_debug_logs(limit=20)

    assert "linked.log" not in payload["sources"]
    assert not any("must not be exposed" in item["message"] for item in payload["items"])


def test_clear_state_ignores_oversized_file(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: logs_dir)
    log_clear_state.clear_state_path().write_bytes(
        b"x" * (log_clear_state._MAX_CLEAR_STATE_BYTES + 1)
    )

    assert log_clear_state.load_clear_offsets() == {}


def test_collect_debug_logs_applies_global_limit_by_time_not_source_name(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir()
    data_dir.mkdir()
    repo_dir.mkdir()
    monkeypatch.setattr(debug_logs, "data_dir", lambda: data_dir)
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs_dir)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: repo_dir)

    newer = logs_dir / "aaa-new.log"
    older = logs_dir / "zzz-old.log"
    newer.write_text("... 12:00:02.000 ERROR newest failure\n", encoding="utf-8")
    older.write_text("... 12:00:01.000 INFO older detail\n", encoding="utf-8")
    # Both entries are time-only, so their file dates come from mtime. Keep the
    # files on the same day while proving filename order cannot decide the cut.
    same_day = 1_700_000_000
    os.utime(newer, (same_day, same_day))
    os.utime(older, (same_day, same_day))

    payload = debug_logs.collect_debug_logs(limit=1)

    assert payload["truncated"] is True
    assert [item["message"] for item in payload["items"]] == ["newest failure"]


def test_clear_marker_resets_when_log_file_is_replaced(monkeypatch, tmp_path):
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
    log_path.write_text("... 12:00:00.000 INFO old entry\n", encoding="utf-8")
    assert debug_logs.clear_debug_logs()["ok"] is True

    # Simulate rotation with a replacement that is at least as large as the
    # cleared file. A size-only marker would incorrectly hide its first bytes.
    log_path.write_text(
        "... 12:00:01.000 ERROR replacement entry that is intentionally longer\n",
        encoding="utf-8",
    )

    payload = debug_logs.collect_debug_logs(limit=20)

    assert [item["message"] for item in payload["items"]] == [
        "replacement entry that is intentionally longer"
    ]
