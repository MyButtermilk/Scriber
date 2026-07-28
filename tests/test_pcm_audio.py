from __future__ import annotations

import struct

from scripts.benchmark_pcm16_rms import (
    BENCHMARK_ID,
    deterministic_pcm16le_fixture,
    run_benchmark,
)
from src.meeting_live_stt import _pcm_has_speech
from src.runtime.pcm_audio import pcm16le_meets_rms_threshold, pcm16le_metrics, pcm16le_rms


def _pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_pcm16le_rms_preserves_signed_little_endian_semantics() -> None:
    assert pcm16le_rms(b"") == 0
    assert pcm16le_rms(b"\xff") == 0
    assert pcm16le_rms(_pcm(256, -256)) == 256
    assert pcm16le_rms(_pcm(3, 4)) == 3


def test_pcm16le_rms_ignores_one_trailing_partial_sample_byte() -> None:
    complete = _pcm(120, -360, 840)

    assert pcm16le_rms(complete + b"\xff") == pcm16le_rms(complete)
    assert pcm16le_meets_rms_threshold(
        complete + b"\xff",
        threshold=pcm16le_rms(complete),
    )


def test_pcm16le_metrics_returns_count_rms_and_absolute_peak() -> None:
    samples = _pcm(0, 256, -512, -32_768)

    metrics = pcm16le_metrics(memoryview(samples))

    assert metrics.sample_count == 4
    assert metrics.rms == pcm16le_rms(samples)
    assert metrics.peak == 32_768
    assert pcm16le_metrics(bytearray(samples) + b"\xff") == metrics
    assert pcm16le_metrics(b"\xff").sample_count == 0
    assert pcm16le_metrics(b"\xff").rms == 0
    assert pcm16le_metrics(b"\xff").peak == 0


def test_pcm16le_threshold_and_meeting_gate_share_boundary_semantics() -> None:
    boundary = _pcm(260, -260)

    assert pcm16le_meets_rms_threshold(boundary, threshold=260)
    assert not pcm16le_meets_rms_threshold(boundary, threshold=261)
    assert _pcm_has_speech(boundary, rms_threshold=260)
    assert not _pcm_has_speech(boundary, rms_threshold=261)
    assert not _pcm_has_speech(b"\xff", rms_threshold=0)


def test_pcm16le_microbenchmark_uses_reproducible_fixture_and_checksum() -> None:
    fixture = deterministic_pcm16le_fixture(320)
    first = run_benchmark(iterations=5, warmup=1, sample_count=320)
    second = run_benchmark(iterations=5, warmup=1, sample_count=320)

    assert first["benchmark"] == BENCHMARK_ID
    assert first["fixture"] == second["fixture"]
    assert first["fixture"]["byteCount"] == len(fixture) == 640
    assert first["checksum"] == second["checksum"] == first["fixture"]["rms"] * 5
    assert first["elapsedNs"] > 0
    assert first["nsPerFrame"] > 0
