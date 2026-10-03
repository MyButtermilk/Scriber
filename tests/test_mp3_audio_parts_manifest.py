import asyncio
import json
import math
import struct
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from src import openrouter_audio as audio
from src.audio_prepare import ProbedAudioInput, ProviderAudioPreparationError, probe_audio_input_file
from src.core.provider_audio_formats import AudioInputFormat
from src.runtime.ffmpeg_commands import mp3_transcode_args
from src.runtime.media_tools import find_media_tool
from src.runtime.subprocess_utils import hidden_subprocess_kwargs


@pytest.fixture
def recording(tmp_path):
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    source = tmp_path / "source.wav"
    # A short repeating tone and a genuine quiet interval exercise the shipped
    # PCM codec path without depending on full-build lavfi/silencedetect.
    second = struct.pack("<16000h", *(int(2000 * math.sin(i * math.tau * 440 / 16000)) for i in range(16000)))
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        for index in range(65):
            output.writeframes(b"\0" * 32_000 if 25 <= index <= 27 else second)
    target = tmp_path / "source.mp3"
    subprocess.run(
        mp3_transcode_args(ffmpeg, source, target),
        check=True,
        capture_output=True,
        timeout=30,
        **hidden_subprocess_kwargs(),
    )
    return target


@pytest.mark.asyncio
async def test_real_mp3_manifest_is_fixed_complete_and_one_derivative(recording):
    maximum = 300_000
    manifest = await audio.plan_mp3_audio_parts(recording, max_audio_bytes=maximum)
    restored = audio.AudioPartsManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored == manifest
    assert len(manifest.parts) >= 2
    assert manifest.overlap_ms == 8_000
    assert manifest.timestamp_tolerance_ms == 250
    assert manifest.parts[0].core_start_ms == 0
    assert manifest.parts[-1].core_end_ms == probe_audio_input_file(recording).duration_ms
    for left, right in zip(manifest.parts, manifest.parts[1:], strict=False):
        assert left.core_end_ms == right.core_start_ms
        assert left.end_ms - right.start_ms == 8_000
    paths = []
    async with audio.mp3_audio_parts(recording, max_audio_bytes=maximum, manifest=restored) as iterator:
        async for part in iterator:
            assert part.count == len(manifest.parts)
            assert 0 < part.path.stat().st_size <= maximum
            assert probe_audio_input_file(part.path).audio_format == AudioInputFormat.MP3
            assert list(part.path.parent.iterdir()) == [part.path]
            assert part.start_ms == manifest.parts[part.index - 1].start_ms
            assert part.timestamp_tolerance_ms == 250
            paths.append(part.path)
    assert all(not path.exists() for path in paths)
    assert recording.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_delta", [0, 1])
async def test_original_fits_true_limits_above_target_and_is_unchanged(recording, limit_delta):
    original = recording.read_bytes()
    duration = probe_audio_input_file(recording).duration_ms
    maximum = len(original) + limit_delta
    manifest = await audio.plan_mp3_audio_parts(
        recording, max_audio_bytes=maximum, max_duration_ms=duration, target_audio_bytes=1
    )
    assert manifest.passthrough
    assert manifest.timestamp_tolerance_ms == 0
    async with audio.mp3_audio_parts(
        recording, max_audio_bytes=maximum, max_duration_ms=duration, manifest=manifest
    ) as iterator:
        parts = [part async for part in iterator]
        assert len(parts) == 1
        assert parts[0].path == recording
        assert parts[0].timestamp_tolerance_ms == 0
        assert parts[0].path.read_bytes() == original
    assert recording.read_bytes() == original


@pytest.mark.asyncio
async def test_duration_alone_splits_and_verifies_actual_frame_duration(recording):
    maximum = recording.stat().st_size * 2
    duration_limit = 30_000
    manifest = await audio.plan_mp3_audio_parts(recording, max_audio_bytes=maximum, max_duration_ms=duration_limit)
    assert len(manifest.parts) > 1
    async with audio.mp3_audio_parts(
        recording, max_audio_bytes=maximum, max_duration_ms=duration_limit, manifest=manifest
    ) as iterator:
        async for part in iterator:
            assert probe_audio_input_file(part.path).duration_ms <= duration_limit


@pytest.mark.asyncio
async def test_real_bounded_silence_scan_selects_local_pause(recording):
    cut = await audio._silence_cut(find_media_tool("ffmpeg"), recording, 24_500, 20_500, 28_500)
    assert 25_000 <= cut <= 28_000


@pytest.mark.asyncio
async def test_silence_scan_has_bounded_pcm_and_falls_back_on_decode_failure(monkeypatch, tmp_path):
    commands = []

    async def failed(command, **kwargs):
        commands.append((command, kwargs))
        return 1, b"", b"codec unavailable"

    monkeypatch.setattr(audio, "_run_ffmpeg", failed)
    assert await audio._silence_cut("ffmpeg", tmp_path / "audio.mp3", 100_000, 0, 200_000) == 100_000
    command, options = commands[0]
    assert float(command[command.index("-t") + 1]) == 8
    assert options["max_stdout_bytes"] == 128_000
    assert "silencedetect" not in " ".join(command)


def fake_vbr(monkeypatch, tmp_path):
    source = tmp_path / "vbr.mp3"
    source.write_bytes(b"x" * 500_000)
    probes = {}
    writes = []

    def probe(path):
        path = Path(path)
        duration = 60_000 if path == source else probes[path]
        return ProbedAudioInput(AudioInputFormat.MP3, "mp3", "mp3", 16_000, 1, duration, path.stat().st_size)

    async def copy(command, target):
        start = round(float(command[command.index("-ss") + 1]) * 1000)
        duration = round(float(command[command.index("-t") + 1]) * 1000)
        end = start + duration
        dense_ms = max(0, min(30_000, end) - min(30_000, start))
        size = 256 + dense_ms * 15 + (duration - dense_ms) * 5 // 3
        assert not list(target.parent.iterdir())
        target.write_bytes(b"x" * size)
        probes[target] = duration
        writes.append((start, end, size))

    async def cut(_ffmpeg, _source, candidate, _minimum, _maximum):
        return candidate

    monkeypatch.setattr(audio, "probe_audio_input_file", probe)
    monkeypatch.setattr(audio, "_run_part_command", copy)
    monkeypatch.setattr(audio, "_silence_cut", cut)
    monkeypatch.setattr(audio, "require_media_tool", lambda _tool: "ffmpeg")
    return source, writes


@pytest.mark.asyncio
async def test_vbr_refinement_finishes_before_manifest_and_never_changes_count(monkeypatch, tmp_path):
    source, writes = fake_vbr(monkeypatch, tmp_path)
    manifest = await audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000)
    assert any(size > 250_000 for _, _, size in writes)
    assert len(writes) > len(manifest.parts)
    count = len(manifest.parts)
    planned_writes = len(writes)
    async with audio.mp3_audio_parts(source, max_audio_bytes=250_000, manifest=manifest) as iterator:
        parts = []
        async for part in iterator:
            assert part.count == count
            assert part.path.stat().st_size <= 250_000
            parts.append(part)
    assert len(parts) == count
    assert len(writes) == planned_writes + count
    assert manifest.parts[-1].core_end_ms == 60_000
    assert await audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000) == manifest


@pytest.mark.asyncio
async def test_saved_manifest_rejects_recreated_part_overflow_before_yield(monkeypatch, tmp_path):
    source, _ = fake_vbr(monkeypatch, tmp_path)
    manifest = await audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000)
    original_copy = audio._run_part_command
    derivatives = []

    async def larger(command, target):
        await original_copy(command, target)
        target.write_bytes(b"x" * 250_001)
        derivatives.append(target)

    monkeypatch.setattr(audio, "_run_part_command", larger)
    with pytest.raises(ProviderAudioPreparationError, match="saved size/duration"):
        async with audio.mp3_audio_parts(source, max_audio_bytes=250_000, manifest=manifest) as iterator:
            await anext(iterator)
    assert derivatives and all(not path.exists() for path in derivatives)


@pytest.mark.asyncio
async def test_changed_source_or_provider_limits_reject_saved_manifest(monkeypatch, tmp_path):
    source, _ = fake_vbr(monkeypatch, tmp_path)
    manifest = await audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000)
    with pytest.raises(ProviderAudioPreparationError, match="different provider"):
        async with audio.mp3_audio_parts(source, max_audio_bytes=250_001, manifest=manifest):
            pytest.fail("must reject changed limits before iteration")
    source.write_bytes(b"changed")
    with pytest.raises(ProviderAudioPreparationError, match="no longer matches"):
        async with audio.mp3_audio_parts(source, max_audio_bytes=250_000, manifest=manifest) as iterator:
            await anext(iterator)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(version=999),
        lambda value: value.pop("version"),
        lambda value: value.update(max_audio_bytes=True),
        lambda value: value.update(source_byte_length=0),
        lambda value: value.update(overlap_ms=8001),
        lambda value: value["parts"][0].update(index=2),
        lambda value: value["parts"][0].update(count=2),
        lambda value: value["parts"][0].update(core_start_ms=1),
        lambda value: value["parts"][0].update(end_ms=1001),
        lambda value: value["parts"][0].update(core_end_ms=999),
    ],
)
def test_saved_manifest_strictly_rejects_invalid_bounds_and_types(mutate):
    manifest = audio.AudioPartsManifest(
        1000, 100, 200, None, (audio.AudioPartWindow(1, 1, 0, 1000, 0, 1000),), True, 0, timestamp_tolerance_ms=0
    )
    payload = manifest.to_dict()
    mutate(payload)
    with pytest.raises(ProviderAudioPreparationError):
        audio.AudioPartsManifest.from_dict(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["break", "failure", "cancel"])
async def test_consumer_exit_cleans_only_derivative(monkeypatch, tmp_path, outcome):
    source, _ = fake_vbr(monkeypatch, tmp_path)
    manifest = await audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000)
    paths = []

    async def consume():
        async with audio.mp3_audio_parts(source, max_audio_bytes=250_000, manifest=manifest) as iterator:
            async for part in iterator:
                paths.append(part.path)
                if outcome == "failure":
                    raise RuntimeError("consumer")
                if outcome == "cancel":
                    raise asyncio.CancelledError
                break

    if outcome == "break":
        await consume()
    else:
        with pytest.raises(RuntimeError if outcome == "failure" else asyncio.CancelledError):
            await consume()
    assert len(paths) == 1
    assert not paths[0].exists()
    assert not paths[0].parent.exists()
    assert source.exists()


@pytest.mark.asyncio
async def test_cancelled_planning_cleans_partial_derivative(monkeypatch, tmp_path):
    source, _ = fake_vbr(monkeypatch, tmp_path)
    started = asyncio.Event()
    paths = []

    async def copy(_command, target):
        target.write_bytes(b"partial")
        paths.append(target)
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(audio, "_run_part_command", copy)
    task = asyncio.create_task(audio.plan_mp3_audio_parts(source, max_audio_bytes=250_000))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(paths) == 1
    assert not paths[0].parent.exists()
    assert source.exists()


@pytest.mark.asyncio
async def test_repeated_cancel_reaps_child_before_return(monkeypatch):
    started = asyncio.Event()
    killed = asyncio.Event()
    reaped = asyncio.Event()

    class Process:
        returncode = None

        def kill(self):
            killed.set()

    process = Process()

    async def spawn(*_args, **_kwargs):
        return process

    async def communicate(_process, **_kwargs):
        started.set()
        await reaped.wait()
        process.returncode = -9
        return b"", b""

    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(audio, "communicate_or_kill_on_cancel", communicate)
    task = asyncio.create_task(audio._run_ffmpeg(["ffmpeg"]))
    await started.wait()
    task.cancel()
    await killed.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    reaped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_cancellation_during_spawn_keeps_and_reaps_child_ownership(monkeypatch):
    spawning = asyncio.Event()
    finish_spawn = asyncio.Event()
    killed = asyncio.Event()

    class Process:
        returncode = None

        def kill(self):
            killed.set()

    process = Process()

    async def spawn(*_args, **_kwargs):
        spawning.set()
        await finish_spawn.wait()
        return process

    async def communicate(_process, **_kwargs):
        await killed.wait()
        process.returncode = -9
        return b"", b""

    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(audio, "communicate_or_kill_on_cancel", communicate)
    task = asyncio.create_task(audio._run_ffmpeg(["ffmpeg"]))
    await spawning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set()
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_impossibly_small_limit_fails_before_upload(monkeypatch, tmp_path):
    source, writes = fake_vbr(monkeypatch, tmp_path)
    with pytest.raises(ProviderAudioPreparationError, match="fit an MP3 part"):
        await audio.plan_mp3_audio_parts(source, max_audio_bytes=1)
    assert len(writes) == 1
    assert source.exists()


@pytest.mark.asyncio
async def test_duration_limit_requires_known_duration(monkeypatch, tmp_path):
    source, _ = fake_vbr(monkeypatch, tmp_path)
    probe = audio.probe_audio_input_file(source)
    monkeypatch.setattr(audio, "probe_audio_input_file", lambda _path: replace(probe, duration_ms=None))
    with pytest.raises(ProviderAudioPreparationError, match="determine the recording duration"):
        await audio.plan_mp3_audio_parts(source, max_audio_bytes=1_000_000, max_duration_ms=30_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample_rate", [8_000, 16_000, 22_050, 44_100, 48_000])
async def test_packet_copy_source_clock_pulse_offset_within_declared_tolerance(tmp_path, sample_rate, record_property):
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    wav = tmp_path / "pulse.wav"
    source = tmp_path / "pulse.mp3"
    target = tmp_path / "part.mp3"
    pulse = [
        int(10_000 * math.sin(index * math.tau * 400 / sample_rate)) if 3.213 <= index / sample_rate < 3.413 else 0
        for index in range(sample_rate * 6)
    ]
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(pulse)}h", *pulse))
    await asyncio.to_thread(
        subprocess.run,
        mp3_transcode_args(ffmpeg, wav, source, sample_rate=sample_rate),
        check=True,
        capture_output=True,
        timeout=30,
        **hidden_subprocess_kwargs(),
    )
    window = audio.AudioPartWindow(1, 1, 2123, 4123, 2123, 4123)
    await audio._run_part_command(audio._copy_command(ffmpeg, source, target, window), target)

    def pulse_onset(path):
        # Full decode here is a six-second test reference, never production.
        pcm = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
            **hidden_subprocess_kwargs(),
        ).stdout
        return (
            next(index for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)) if abs(sample) > 1000)
            * 1000
            / sample_rate
        )

    target_onset = await asyncio.to_thread(pulse_onset, target)
    source_onset = await asyncio.to_thread(pulse_onset, source)
    error_ms = abs(target_onset + window.start_ms - source_onset)
    record_property("packet_copy_source_clock_error_ms", round(error_ms, 4))
    assert error_ms <= audio.AudioPart(target, 1, 1).timestamp_tolerance_ms
