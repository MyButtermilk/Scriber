from __future__ import annotations

import json
import os

import pytest

from src.runtime import debug_logs, log_clear_state


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
    log_path.write_bytes(b"\x00\x00\x00... 12:00:00.000 INFO  [web_api    ] [------] [web_api        ] before clear\n")

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
    log_clear_state.clear_state_path().write_bytes(b"x" * (log_clear_state._MAX_CLEAR_STATE_BYTES + 1))

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


def test_structured_logs_expose_only_bounded_redacted_debug_context(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir()
    data_dir.mkdir()
    repo_dir.mkdir()
    monkeypatch.setattr(debug_logs, "data_dir", lambda: data_dir)
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs_dir)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: repo_dir)

    structured = {
        "record": {
            "message": "Hot path timing captured (af466854)",
            "level": {"name": "INFO"},
            "time": {"repr": "2026-07-16T09:10:11+02:00"},
            "extra": {
                "component": "web_api",
                "event": "metrics.hot_path.reported",
                "workflow": "live_mic",
                "stage": "hot_path_report",
                "provider": "modulate",
                "duration_ms": 1281.0015,
                "outcome": "success",
                "milestone": True,
                "session_id": "private-session-id",
                "meta": {
                    "hotkey_received_to_mic_ready_ms": 175.9594,
                    "stop_requested_to_first_paste_ms": 1281.0015,
                    "model": "velma-2-stt-streaming",
                    "mode": "realtime",
                    "error": "private provider response text",
                    "api_key": "must-never-leave-the-log-file",
                    "transcript": "private spoken words",
                    "nested": {
                        "provider_error_code": "network_error",
                        "access_token": "also-secret",
                    },
                },
            },
        }
    }
    (logs_dir / "latest.structured.jsonl").write_text(
        json.dumps(structured) + "\n",
        encoding="utf-8",
    )

    payload = debug_logs.collect_debug_logs(limit=20)
    entry = payload["items"][0]

    assert entry["message"] == "Hot path timing captured (af466854)"
    assert entry["context"] == {
        "event": "metrics.hot_path.reported",
        "workflow": "live_mic",
        "stage": "hot_path_report",
        "provider": "modulate",
        "outcome": "success",
        "durationMs": 1281.0015,
        "milestone": True,
        "meta": {
            "hotkey_received_to_mic_ready_ms": 175.9594,
            "stop_requested_to_first_paste_ms": 1281.0015,
            "model": "velma-2-stt-streaming",
            "mode": "realtime",
            "nested": {"provider_error_code": "network_error"},
        },
    }
    serialized = json.dumps(entry)
    assert "private-session-id" not in serialized
    assert "must-never-leave" not in serialized
    assert "also-secret" not in serialized
    assert "private spoken words" not in serialized
    assert "private provider response text" not in serialized


def test_structured_log_context_caps_legacy_metric_dumps(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir()
    data_dir.mkdir()
    repo_dir.mkdir()
    monkeypatch.setattr(debug_logs, "data_dir", lambda: data_dir)
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs_dir)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: repo_dir)

    oversized_meta = {
        f"marker_{index}_to_next_ms": float(index) for index in range(debug_logs._MAX_PUBLIC_META_ITEMS * 4)
    }
    structured = {
        "record": {
            "message": "Legacy hot path timing",
            "level": {"name": "INFO"},
            "extra": {
                "component": "web_api",
                "event": "metrics.hot_path.reported",
                "meta": oversized_meta,
            },
        }
    }
    (logs_dir / "latest.structured.jsonl").write_text(
        json.dumps(structured) + "\n",
        encoding="utf-8",
    )

    payload = debug_logs.collect_debug_logs(limit=20)
    public_meta = payload["items"][0]["context"]["meta"]

    assert len(public_meta) <= debug_logs._MAX_PUBLIC_META_ITEMS
    assert len(public_meta) < len(oversized_meta)


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

    assert [item["message"] for item in payload["items"]] == ["replacement entry that is intentionally longer"]


def test_unchanged_poll_reuses_redacted_entries_but_sees_append_rewrite_and_clear(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs)
    monkeypatch.setattr(debug_logs, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(debug_logs, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: logs)
    path = logs / "latest.jsonl"
    path.write_text('{"message":"first","meta":{"status":"ready"}}\n', encoding="utf-8")
    original_read = debug_logs._read_tail
    reads = []

    def observed_read(*args, **kwargs):
        reads.append(args[0])
        return original_read(*args, **kwargs)

    monkeypatch.setattr(debug_logs, "_read_tail", observed_read)
    first = debug_logs.collect_debug_logs(limit=20)
    assert debug_logs.collect_debug_logs(limit=20) == first
    assert len(reads) == 1
    # A caller cannot poison the next response's cached public context.
    first["items"][0]["context"]["meta"]["status"] = "caller mutated"
    assert debug_logs.collect_debug_logs(limit=20)["items"][0]["context"]["meta"]["status"] == "ready"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"message":"second"}\n')
    assert [item["message"] for item in debug_logs.collect_debug_logs(limit=20)["items"]] == ["first", "second"]
    assert len(reads) == 2
    assert [item["message"] for item in debug_logs.collect_debug_logs(limit=1)["items"]] == ["second"]
    path.write_text('{"message":"replacement"}\n', encoding="utf-8")
    assert [item["message"] for item in debug_logs.collect_debug_logs(limit=20)["items"]] == ["replacement"]
    assert debug_logs.clear_debug_logs()["ok"]
    assert debug_logs.collect_debug_logs(limit=20)["items"] == []


def test_partial_jsonl_tail_is_not_exposed_as_raw_log_text(monkeypatch, tmp_path):
    monkeypatch.setattr(debug_logs, "_candidate_log_files", lambda: [tmp_path / "latest.jsonl"])
    monkeypatch.setattr(debug_logs, "load_clear_offsets", lambda: {})
    path = tmp_path / "latest.jsonl"
    path.write_text('{"message":"complete"}\n{"message":"unfinished private transcript', encoding="utf-8")
    payload = debug_logs.collect_debug_logs(limit=20)
    assert [item["message"] for item in payload["items"]] == ["complete"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write('","meta":{"transcript":"private content"}}\n')
    assert len(debug_logs.collect_debug_logs(limit=20)["items"]) == 2


def test_invalid_log_timestamp_does_not_fail_entire_console():
    entry = debug_logs.DebugLogEntry("a.log", 1, "INFO", "hello", timestamp="99:99:99")
    assert debug_logs._entry_sort_value(entry, 1700000000000) > 0


def test_empty_structured_message_never_falls_back_to_private_record():
    for payload in (
        {"record": {"message": "", "extra": {"transcript": "PRIVATE_CONTENT_PROBE"}}},
        {"transcript": "PRIVATE_CONTENT_PROBE"},
    ):
        entry = debug_logs._parse_log_line(json.dumps(payload), source="latest.structured.jsonl", line_number=1)
        assert entry is not None
        assert "PRIVATE_CONTENT_PROBE" not in json.dumps(entry.to_public())
        assert entry.message == "Structured diagnostic event"


@pytest.mark.parametrize("nested_record", [False, True])
def test_json_escaped_credentials_are_redacted_after_decoding(nested_record):
    synthetic_key = "sk-" + "a" * 12
    payload = {
        "message": synthetic_key,
        "timestamp": synthetic_key,
        "component": synthetic_key,
    }
    if nested_record:
        payload = {
            "record": {
                "message": synthetic_key,
                "time": {"repr": synthetic_key},
                "extra": {"component": synthetic_key},
            }
        }
    encoded = json.dumps(payload).replace(synthetic_key, "sk-" + "\\u0061" * 12)

    entry = debug_logs._parse_log_line(encoded, source="latest.structured.jsonl", line_number=1)

    assert entry is not None
    assert entry.message == "[REDACTED]"
    assert entry.component == "[REDACTED]"
    assert entry.timestamp == "[REDACTED]"
    assert synthetic_key not in json.dumps(entry.to_public())


def test_provider_failure_details_reach_debug_console_without_raw_response(monkeypatch, tmp_path):
    from src.core.provider_errors import provider_transport_error

    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs)
    monkeypatch.setattr(debug_logs, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(debug_logs, "repo_root", lambda: tmp_path / "repo")
    error = provider_transport_error(
        "openrouter_stt",
        "transcription",
        status=413,
        request_bytes=82_590_000,
        response_body='{"error":{"code":413,"message":"Payload too large: private words"}}',
    )
    metadata = {"error_type": type(error).__name__, **error.diagnostic_metadata()}
    (logs / "latest.structured.jsonl").write_text(
        json.dumps(
            {
                "record": {
                    "message": "File job failed: audio upload is too large (HTTP 413)",
                    "level": {"name": "ERROR"},
                    "extra": {
                        "event": "api.job.failed",
                        "workflow": "file",
                        "provider": "openrouter_stt",
                        "error_category": "audio_invalid",
                        "meta": metadata,
                    },
                }
            }
        )
        + "\n"
    )
    entry = debug_logs.collect_debug_logs(limit=10)["items"][0]
    assert entry["context"]["errorCategory"] == "audio_invalid"
    assert entry["context"]["meta"] == metadata
    assert "private words" not in json.dumps(entry)


@pytest.mark.parametrize("prefix", ["", "tr_"])
def test_file_and_podcast_context_exposes_safe_correlation_and_lifecycle(prefix):
    correlation_id = "0123456789abcdef" * 2
    metadata = {
        "from_status": "downloading",
        "to_status": "transcribing",
        "attempt": 2,
        "downloaded_bytes": 4096,
        "source_reused": True,
        "explicit_retry": False,
        "resume": True,
        "download_only": False,
        "http_status": 429,
        "error_type": "ProviderTransportError",
    }
    context = debug_logs._public_log_context(
        {
            "trace_id": prefix + correlation_id,
            "transcript_id": "private-transcript-id",
            "meta": {
                **metadata,
                "episode_id": "private-episode-id",
                "subscription_id": "private-subscription-id",
                "filename": "private-audio.wav",
                "feed_url": "https://private.example/feed",
                "transcript": "private spoken content",
                "response_body": "private provider reply",
            },
        }
    )

    assert context == {"correlationId": correlation_id, "meta": metadata}


@pytest.mark.parametrize(
    "trace_id",
    [
        "private-transcript-id",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "tr_private",
        "Bearer abc",
        "01234567-89ab-cdef-0123-456789abcdef",
    ],
)
def test_debug_context_rejects_unvalidated_correlation_identifiers(trace_id):
    assert debug_logs._public_log_context({"trace_id": trace_id}) is None


def test_podcast_queue_and_file_execution_keep_uuid_links_across_correlation_change():
    episode_id, subscription_id, job_id, transcript_id = (char * 32 for char in "abcd")
    links = {"episode_id": episode_id, "subscription_id": subscription_id}
    queued = debug_logs._public_log_context({"trace_id": episode_id, "meta": links})
    running = debug_logs._public_log_context(
        {"trace_id": transcript_id, "job_id": job_id, "transcript_id": transcript_id, "meta": links}
    )

    assert queued == {"correlationId": episode_id, "meta": links}
    assert running == {
        "correlationId": transcript_id,
        "meta": {**links, "job_id": job_id, "transcript_id": transcript_id},
    }


@pytest.mark.parametrize(
    "invalid_id",
    [False, 42, "a" * 31, "A" * 32, "tr_" + "a" * 32, "private-name", ["a" * 32], {"status": "a" * 32}],
)
def test_workflow_links_reject_arbitrary_ids_and_containers(invalid_id):
    identifiers = dict.fromkeys(("episode_id", "subscription_id", "job_id", "transcript_id"), invalid_id)
    assert debug_logs._public_log_context({**identifiers, "meta": identifiers}) is None


def test_console_prefers_recent_generations_and_clear_marks_every_candidate(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(debug_logs, "logs_dir", lambda: logs)
    monkeypatch.setattr(debug_logs, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(debug_logs, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: logs)
    for index in range(debug_logs._MAX_FILES + 2):
        path = logs / f"generation-{index:03d}.log"
        path.write_text(f"entry-{index}\n", encoding="utf-8")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    payload = debug_logs.collect_debug_logs(limit=100)
    assert "generation-000.log" not in payload["sources"]
    assert f"generation-{debug_logs._MAX_FILES + 1:03d}.log" in payload["sources"]
    assert debug_logs.clear_debug_logs()["cleared"] == debug_logs._MAX_FILES + 2
    offsets = log_clear_state.load_clear_offsets()
    assert all(
        log_clear_state.clear_offset_for_path(path, offsets) == path.stat().st_size for path in logs.glob("*.log")
    )


@pytest.mark.parametrize("archive_name", ["latest.1.log", "latest.2026-10-04_01-02-03_123456.log"])
def test_clear_boundary_follows_rotated_generation_without_hiding_new_active(monkeypatch, tmp_path, archive_name):
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: tmp_path)
    active = tmp_path / "latest.log"
    active.write_text("cleared generation\n", encoding="utf-8")
    log_clear_state.record_clear_state([active])
    with active.open("a", encoding="utf-8") as handle:
        handle.write("after clear\n")
    archived = active.rename(tmp_path / archive_name)
    active.write_text("new generation with different bytes\n", encoding="utf-8")

    offsets = log_clear_state.load_clear_offsets()
    archived_offset = log_clear_state.clear_offset_for_path(archived, offsets)
    assert debug_logs._read_tail(archived, start_offset=archived_offset)[0].splitlines() == ["after clear"]
    assert log_clear_state.clear_offset_for_path(active, offsets) == 0
    assert debug_logs._read_tail(active)[0].splitlines() == ["new generation with different bytes"]


def test_clear_boundary_follows_numbered_archive_between_slots(monkeypatch, tmp_path):
    monkeypatch.setattr(log_clear_state, "logs_dir", lambda: tmp_path)
    active = tmp_path / "tauri-backend.log"
    first_archive = tmp_path / "tauri-backend.1.log"
    active.write_text("current generation\n", encoding="utf-8")
    first_archive.write_text("older archived generation\n", encoding="utf-8")
    log_clear_state.record_clear_state([active, first_archive])
    second_archive = first_archive.rename(tmp_path / "tauri-backend.2.log")
    offsets = log_clear_state.load_clear_offsets()

    assert log_clear_state.clear_offset_for_path(second_archive, offsets) == second_archive.stat().st_size
    # Matching bytes in a different family must not inherit another log's clear.
    unrelated = tmp_path / "tauri-shell.2.log"
    unrelated.write_bytes(second_archive.read_bytes())
    assert log_clear_state.clear_offset_for_path(unrelated, offsets) == 0
