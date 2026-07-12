from __future__ import annotations

import errno
import hashlib
import io
import threading
import time
import wave

import pytest

from src import database
from src.data.meeting_store import MeetingConflict, MeetingCreate, MeetingStore
from src.meeting_capture import (
    MeetingAudioRecorder,
    MeetingCaptureStats,
    MeetingDeviceLevelProbe,
)
from src.runtime.audio_frame_pipe import AudioFrameHeader, encode_audio_frame


class ReaderFactory:
    def __init__(self, streams: dict[str, bytes]):
        self.streams = streams

    def __call__(self, path: str, *_args, **_kwargs):
        return io.BytesIO(self.streams[path])


def write_pcm_wav(path, *, frames=16_000, sample_rate=16_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0\0" * frames)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_meeting_recorder_rotates_durable_wav_chunks(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Capture", consent_confirmed=True))["id"]

    frames = b"".join(
        encode_audio_frame(
            AudioFrameHeader(
                payload_len=20,
                sequence=sequence,
                timestamp_micros=sequence * 1_000_000,
                frame_count=10,
                channels=1,
            ),
            bytes([sequence + 1]) * 20,
        )
        for sequence in range(2)
    )
    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meeting-audio",
        store,
        sample_rate=10,
        chunk_seconds=1,
        reader_factory=ReaderFactory({"mic-pipe": frames}),
    )
    recorder.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    deadline = time.monotonic() + 2
    while recorder.snapshot()["microphone"]["chunks"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    snapshot = recorder.stop()

    assert snapshot["microphone"]["frames"] == 2
    assert snapshot["microphone"]["chunks"] == 2
    rows = database._get_connection().execute(
        "SELECT sequence, state, sha256 FROM meeting_audio_chunks ORDER BY sequence"
    ).fetchall()
    assert [row["sequence"] for row in rows] == [0, 1]
    assert all(row["state"] == "complete" and len(row["sha256"]) == 64 for row in rows)
    assert len(list((tmp_path / "meeting-audio" / meeting_id / "audio").glob("*.wav"))) == 2
    database._close_all_connections()


def test_orphaned_partial_chunks_are_quarantined(tmp_path):
    partial = tmp_path / "meeting-audio" / "meeting-1" / "audio" / "system-000001.partial.wav"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"incomplete")
    assert MeetingAudioRecorder.quarantine_orphaned_partials(tmp_path / "meeting-audio") == 1
    assert not partial.exists()
    assert (partial.parent / "quarantine" / partial.name).read_bytes() == b"incomplete"


def test_meeting_device_probe_reports_levels_without_persisting_audio(tmp_path):
    samples = (0, 16_384, -16_384, 32_767)
    pcm = b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(pcm),
            sequence=0,
            timestamp_micros=0,
            frame_count=len(samples),
            channels=1,
        ),
        pcm,
    )
    probe = MeetingDeviceLevelProbe(
        reader_factory=ReaderFactory({"mic-pipe": frame})
    )
    probe.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    deadline = time.monotonic() + 1
    while probe.snapshot()["microphone"]["frames"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    snapshot = probe.stop()

    assert snapshot["microphone"]["active"] is True
    assert snapshot["microphone"]["audioFrames"] == 4
    assert 0.60 < snapshot["microphone"]["rms"] < 0.62
    assert snapshot["microphone"]["peak"] > 0.99
    assert list(tmp_path.iterdir()) == []


def test_meeting_device_probe_keeps_frames_valid_when_native_pipe_closes():
    pcm = (8_192).to_bytes(2, "little", signed=True) * 160
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(pcm), sequence=0, timestamp_micros=0,
            frame_count=160, channels=1,
        ),
        pcm,
    )

    class NativeCloseReader(io.BytesIO):
        def read(self, size=-1):
            if self.tell() >= len(frame):
                raise OSError(errno.EPIPE, "synthetic native stop")
            return super().read(size)

    probe = MeetingDeviceLevelProbe(
        reader_factory=lambda *_args, **_kwargs: NativeCloseReader(frame)
    )
    probe.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    deadline = time.monotonic() + 1
    while probe.snapshot()["microphone"]["frames"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    snapshot = probe.stop()

    assert snapshot["microphone"]["frames"] == 1
    assert snapshot["microphone"]["active"] is True
    assert snapshot["microphone"]["errorCode"] == ""


def test_meeting_recorder_surfaces_disk_full_without_persisting_incomplete_chunk(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(
        MeetingCreate(title="Disk full", consent_confirmed=True)
    )["id"]
    pcm = b"\0\0" * 160
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(pcm), sequence=0, timestamp_micros=0,
            frame_count=160, channels=1,
        ),
        pcm,
    )

    class DiskFullWriter:
        def setnchannels(self, _value):
            pass

        def setsampwidth(self, _value):
            pass

        def setframerate(self, _value):
            pass

        def writeframesraw(self, _payload):
            raise OSError(errno.ENOSPC, "synthetic disk full")

        def close(self):
            pass

    monkeypatch.setattr("src.meeting_capture.wave.open", lambda *_args, **_kwargs: DiskFullWriter())
    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meeting-audio",
        store,
        reader_factory=ReaderFactory({"mic-pipe": frame}),
    )
    recorder.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    deadline = time.monotonic() + 1
    while not recorder.snapshot()["microphone"]["errorCode"] and time.monotonic() < deadline:
        time.sleep(0.005)
    snapshot = recorder.stop()

    assert snapshot["microphone"]["errorCode"] == "disk_full"
    assert snapshot["microphone"]["chunks"] == 0
    assert database._get_connection().execute(
        "SELECT COUNT(*) FROM meeting_audio_chunks WHERE meeting_id=?", (meeting_id,)
    ).fetchone()[0] == 0
    database._close_all_connections()


def test_meeting_recorder_retries_transient_named_pipe_open(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Resume", consent_confirmed=True))["id"]
    pcm = b"\0\0" * 160
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(pcm), sequence=0, timestamp_micros=0,
            frame_count=160, channels=1,
        ),
        pcm,
    )

    class TransientPipeFactory:
        calls = 0

        def __call__(self, _path, *_args, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                raise OSError(errno.ENOENT, "synthetic named-pipe startup race")
            return io.BytesIO(frame)

    factory = TransientPipeFactory()
    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meeting-audio",
        store,
        reader_factory=factory,
        reader_open_timeout_seconds=0.25,
    )
    recorder.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    snapshot = recorder.stop()

    assert factory.calls == 3
    assert snapshot["microphone"]["frames"] == 1
    assert snapshot["microphone"]["errorCode"] == ""
    database._close_all_connections()


def test_meeting_recorder_rejects_sources_that_never_open(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Resume failure", consent_confirmed=True))["id"]

    def unavailable_pipe(*_args, **_kwargs):
        raise OSError(errno.ENOENT, "synthetic unavailable pipe")

    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meeting-audio",
        store,
        reader_factory=unavailable_pipe,
        reader_open_timeout_seconds=0.05,
    )
    with pytest.raises(RuntimeError, match="microphone:pipe_unavailable"):
        recorder.start([{"source": "microphone", "framePipe": "missing-pipe"}])

    assert recorder.snapshot()["microphone"]["errorCode"] == "FileNotFoundError"
    assert not list((tmp_path / "meeting-audio" / meeting_id / "audio").glob("*.partial.wav"))
    database._close_all_connections()


def test_expected_native_disconnect_clears_only_pipe_errors(tmp_path):
    recorder = MeetingAudioRecorder("meeting", tmp_path, object())
    recorder._stats = {
        "microphone": MeetingCaptureStats(error_code="OSError"),
        "system": MeetingCaptureStats(error_code="disk_full"),
    }

    snapshot = recorder.stop(expected_disconnect=True)

    assert snapshot["microphone"]["errorCode"] == ""
    assert snapshot["system"]["errorCode"] == "disk_full"


def test_recovery_finishes_prepared_partial_after_crash(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "prepared-partial.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Prepared partial"))["id"]
    root = tmp_path / "meetings"
    partial = root / meeting_id / "audio" / "microphone-000000.partial.wav"
    digest = write_pcm_wav(partial)
    store.prepare_audio_chunk(
        meeting_id,
        source="microphone",
        sequence=0,
        relative_path=f"{meeting_id}/audio/microphone-000000.wav",
        started_at_ms=0,
        ended_at_ms=1_000,
        sha256=digest,
    )

    result = store.reconcile_audio_chunks(root)

    final = partial.with_name("microphone-000000.wav")
    assert result["completed"] == 1
    assert final.is_file() and not partial.exists()
    assert final.read_bytes() and hashlib.sha256(final.read_bytes()).hexdigest() == digest
    assert store.audio_chunks(meeting_id)[0]["state"] == "complete"
    assert store.transcript_checkpoints(meeting_id)[0]["frontiers"]["logical"] == {
        "microphone": 1_000
    }
    database._close_all_connections()


def test_recovery_finishes_prepared_final_after_rename_crash(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "prepared-final.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Prepared final"))["id"]
    root = tmp_path / "meetings"
    final = root / meeting_id / "audio" / "system-000000.wav"
    digest = write_pcm_wav(final)
    store.prepare_audio_chunk(
        meeting_id,
        source="system",
        sequence=0,
        relative_path=f"{meeting_id}/audio/system-000000.wav",
        started_at_ms=0,
        ended_at_ms=1_000,
        sha256=digest,
    )

    assert store.reconcile_audio_chunks(root)["completed"] == 1
    assert final.is_file()
    assert store.audio_chunks(meeting_id, "system")[0]["sha256"] == digest
    database._close_all_connections()


def test_recovery_defers_database_failure_without_losing_final(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "prepared-db-failure.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Prepared DB failure"))["id"]
    root = tmp_path / "meetings"
    final = root / meeting_id / "audio" / "system-000000.wav"
    digest = write_pcm_wav(final)
    store.prepare_audio_chunk(
        meeting_id,
        source="system",
        sequence=0,
        relative_path=f"{meeting_id}/audio/system-000000.wav",
        started_at_ms=0,
        ended_at_ms=1_000,
        sha256=digest,
    )
    with database._get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER reject_recovery_checkpoint
               BEFORE INSERT ON meeting_transcript_checkpoints
               BEGIN SELECT RAISE(ABORT, 'synthetic checkpoint failure'); END"""
        )
        conn.commit()

    first = store.reconcile_audio_chunks(root)
    raw = database._get_connection().execute(
        "SELECT state FROM meeting_audio_chunks WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert first["deferred"] == 1
    assert raw["state"] == "prepared" and final.is_file()

    with database._get_connection() as conn:
        conn.execute("DROP TRIGGER reject_recovery_checkpoint")
        conn.commit()
    assert store.reconcile_audio_chunks(root)["completed"] == 1
    assert store.audio_chunks(meeting_id)[0]["state"] == "complete"
    database._close_all_connections()


def test_legacy_rowless_final_is_only_adopted_at_unambiguous_next_sequence(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "legacy-orphan.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Legacy orphan"))["id"]
    root = tmp_path / "meetings"
    valid = root / meeting_id / "audio" / "microphone-000000.wav"
    write_pcm_wav(valid)

    assert store.reconcile_audio_chunks(root)["adopted"] == 1
    assert store.audio_chunks(meeting_id, "microphone")[0]["sequence"] == 0

    ambiguous = root / meeting_id / "audio" / "microphone-000002.wav"
    write_pcm_wav(ambiguous)
    result = store.reconcile_audio_chunks(root)
    assert result["quarantined"] == 1
    assert not ambiguous.exists()
    assert (ambiguous.parent / "quarantine" / ambiguous.name).is_file()
    database._close_all_connections()


def test_recorder_never_overwrites_unknown_final(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "no-overwrite.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="No overwrite"))["id"]
    final = tmp_path / "meetings" / meeting_id / "audio" / "microphone-000000.wav"
    final.parent.mkdir(parents=True)
    sentinel = b"unknown-existing-audio"
    final.write_bytes(sentinel)
    pcm = b"\0\0" * 160
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(pcm), sequence=0, timestamp_micros=0,
            frame_count=160, channels=1,
        ),
        pcm,
    )
    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meetings",
        store,
        reader_factory=ReaderFactory({"mic-pipe": frame}),
    )

    recorder.start([{"source": "microphone", "framePipe": "mic-pipe"}])
    deadline = time.monotonic() + 1
    while not recorder.snapshot()["microphone"]["errorCode"] and time.monotonic() < deadline:
        time.sleep(0.005)
    recorder.stop()

    assert recorder.snapshot()["microphone"]["errorCode"] == "FileExistsError"
    assert final.read_bytes() == sentinel
    assert not list(final.parent.glob("*.partial.wav"))
    assert database._get_connection().execute(
        "SELECT COUNT(*) FROM meeting_audio_chunks WHERE meeting_id=?", (meeting_id,)
    ).fetchone()[0] == 0
    database._close_all_connections()


def test_stop_retains_blocked_reader_and_prevents_restart(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "blocked-reader.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Blocked reader"))["id"]
    release = threading.Event()

    class BlockingReader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            release.wait(timeout=2)
            return b""

    recorder = MeetingAudioRecorder(
        meeting_id,
        tmp_path / "meetings",
        store,
        reader_factory=lambda *_args, **_kwargs: BlockingReader(),
    )
    recorder.start([{"source": "microphone", "framePipe": "blocked"}])

    with pytest.raises(RuntimeError, match="did not stop"):
        recorder.stop(timeout=0.01)
    assert len(recorder._threads) == 1 and recorder._threads[0].is_alive()
    with pytest.raises(RuntimeError, match="still active"):
        recorder.start([{"source": "microphone", "framePipe": "second"}])

    release.set()
    recorder.stop(timeout=1)
    assert recorder._threads == []
    database._close_all_connections()


def test_prepared_sequence_is_immutable(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "immutable-sequence.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting_id = store.create(MeetingCreate(title="Immutable sequence"))["id"]
    kwargs = {
        "source": "system", "sequence": 0,
        "relative_path": f"{meeting_id}/audio/system-000000.wav",
        "started_at_ms": 0, "ended_at_ms": 1_000, "sha256": "a" * 64,
    }
    store.prepare_audio_chunk(meeting_id, **kwargs)
    with pytest.raises(MeetingConflict, match="already reserved"):
        store.prepare_audio_chunk(meeting_id, **{**kwargs, "sha256": "b" * 64})
    raw = database._get_connection().execute(
        "SELECT state,sha256 FROM meeting_audio_chunks WHERE meeting_id=?", (meeting_id,)
    ).fetchone()
    assert (raw["state"], raw["sha256"]) == ("prepared", "a" * 64)
    database._close_all_connections()
