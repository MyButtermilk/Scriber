from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import subprocess
import time
import wave
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src import web_api
from src.core.rest_contracts import (
    RESTContractError,
    validate_provider_replay_arm_request_payload,
    validate_provider_replay_prepare_request_payload,
    validate_provider_replay_status_query,
)
from src.runtime.provider_replay import (
    LocalSonioxReplayServer,
    ProviderReplayConflict,
    ProviderReplayExecution,
    ProviderReplayNotFound,
    ProviderReplayRegistry,
    ProviderReplayRuntimeGate,
    create_azure_mai_replay_transport,
    create_speechmatics_batch_replay_transport,
    install_soniox_replay_receive_observer,
    prewarm_azure_mai_replay_validation,
    provider_replay_fixture_duration_ms_from_environment,
    windows_qpc_snapshot,
)
from src.runtime.ffmpeg_commands import mp3_encode_pcm_pipe_args
from src.runtime.media_tools import find_media_tool
from src.web_api import ScriberWebController


RUN_ID = "7de1a48651d44f859042b7cbcb30da52"
OTHER_RUN_ID = "8f793212ad894cbdac1118c373788aa5"
SAMPLE_ID = UUID("2b3022ee-3f40-4333-a115-6da089a24962")
SECOND_SAMPLE_ID = UUID("3d4054ff-5041-4444-b226-7eb190b35a73")
SESSION_ID = "4e51660061524555c3378fc2a1c46b84"
AZURE_REPLAY_URL = (
    "https://northeurope.api.cognitive.microsoft.com/"
    "speechtotext/transcriptions:transcribe?api-version=2025-10-15"
)
AZURE_REPLAY_DEFINITION = {
    "enhancedMode": {"enabled": True, "model": "mai-transcribe-1.5"},
    "locales": ["en-US"],
}


def enabled_gate(*, backend_creation: int = 11) -> ProviderReplayRuntimeGate:
    return ProviderReplayRuntimeGate.evaluate(
        raw_run_id=RUN_ID,
        frozen=True,
        runtime_mode="tauri-supervised",
        launch_kind="sidecar",
        platform="win32",
        backend_pid=123,
        backend_creation_time_100ns=backend_creation,
        parent_pid=45,
        parent_executable_name="Scriber-Desktop.EXE",
        parent_creation_time_100ns=22,
    )


def _fixture_pcm(duration_ms: int, *, frequency_hz: float = 440.0) -> bytes:
    frame_count = 48_000 * duration_ms // 1000
    return b"".join(
        int(
            12_000
            * math.sin((2.0 * math.pi * frequency_hz * index) / 48_000)
        ).to_bytes(2, "little", signed=True)
        for index in range(frame_count)
    )


async def _encode_fixture_mp3_or_skip(pcm: bytes) -> bytes:
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is unavailable")

    def encode() -> bytes:
        result = subprocess.run(
            mp3_encode_pcm_pipe_args(
                ffmpeg,
                input_sample_rate=48_000,
                input_channels=1,
                bitrate="64k",
            ),
            input=pcm,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode(
            "utf-8", errors="replace"
        )
        assert result.stdout
        return result.stdout

    return await asyncio.to_thread(encode)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"raw_run_id": "not-a-uuid"}, "run_id_missing_or_invalid"),
        ({"frozen": False}, "backend_not_frozen"),
        ({"runtime_mode": "standalone"}, "runtime_mode_not_tauri_supervised"),
        ({"launch_kind": "source"}, "launch_kind_not_sidecar"),
        ({"platform": "linux"}, "platform_not_windows"),
        ({"parent_executable_name": "python.exe"}, "direct_parent_not_scriber_desktop"),
        ({"parent_creation_time_100ns": None}, "process_generation_unavailable"),
    ],
)
def test_provider_replay_gate_fails_closed(overrides, reason):
    values = {
        "raw_run_id": RUN_ID,
        "frozen": True,
        "runtime_mode": "tauri-supervised",
        "launch_kind": "sidecar",
        "platform": "win32",
        "backend_pid": 123,
        "backend_creation_time_100ns": 11,
        "parent_pid": 45,
        "parent_executable_name": "scriber-desktop.exe",
        "parent_creation_time_100ns": 22,
    }
    values.update(overrides)
    gate = ProviderReplayRuntimeGate.evaluate(**values)
    assert gate.enabled is False
    assert gate.reason == reason
    assert gate.run_id is None
    assert gate.process_generation_fingerprint is None


def test_provider_replay_gate_binds_process_generation():
    first = enabled_gate(backend_creation=11)
    second = enabled_gate(backend_creation=12)
    assert first.enabled is True
    assert first.run_id == RUN_ID
    assert len(first.process_generation_fingerprint or "") == 64
    assert first.process_generation_fingerprint != second.process_generation_fingerprint


def test_windows_qpc_helper_uses_positive_system_wide_clock():
    ticks, frequency = windows_qpc_snapshot()
    assert ticks > 0
    assert frequency > 0


def test_provider_replay_rest_contracts_reject_payload_and_query_expansion():
    validated = validate_provider_replay_prepare_request_payload(
        {
            "schemaVersion": 1,
            "runId": RUN_ID,
            "provider": "speechmatics",
        },
        configured_run_id=RUN_ID,
    )
    assert validated["provider"] == "speechmatics"

    with pytest.raises(RESTContractError):
        validate_provider_replay_prepare_request_payload(
            {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "provider": "microsoft",
                "text": "arbitrary text is forbidden",
            },
            configured_run_id=RUN_ID,
        )
    with pytest.raises(RESTContractError):
        validate_provider_replay_arm_request_payload(
            {
                "schemaVersion": 1,
                "runId": RUN_ID,
                "targetProcessId": 0,
                "targetCreationTime100ns": 123,
            },
            configured_run_id=RUN_ID,
        )
    with pytest.raises(RESTContractError):
        validate_provider_replay_status_query(
            {"runId": RUN_ID, "marker": "forged"},
            configured_run_id=RUN_ID,
        )


@pytest.mark.parametrize(
    ("provider", "env_name", "env_value", "expected"),
    [
        (
            "microsoft",
            "SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3",
            "0",
            "post_stop_ffmpeg_mp3_v1",
        ),
        (
            "microsoft",
            "SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3",
            "1",
            "capture_time_ffmpeg_mp3_v1",
        ),
        (
            "speechmatics",
            "SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV",
            "0",
            "python_reserved_wav_header_v1",
        ),
        (
            "speechmatics",
            "SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV",
            "1",
            "wav_pcm16_file_v1",
        ),
    ],
)
def test_provider_replay_freezes_expected_audio_preparation(
    monkeypatch,
    provider,
    env_name,
    env_value,
    expected,
):
    monkeypatch.setenv(env_name, env_value)
    assert web_api._provider_replay_audio_preparation_snapshot(provider) == expected


def test_provider_replay_audio_preparation_defaults_off(monkeypatch):
    monkeypatch.delenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", raising=False)
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV", raising=False)

    assert (
        web_api._provider_replay_audio_preparation_snapshot("microsoft")
        == "post_stop_ffmpeg_mp3_v1"
    )
    assert (
        web_api._provider_replay_audio_preparation_snapshot("speechmatics")
        == "python_reserved_wav_header_v1"
    )


def test_registry_is_one_active_sample_and_arm_is_one_shot():
    ids = iter((SAMPLE_ID, SECOND_SAMPLE_ID))
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: next(ids),
        qpc_clock=lambda: (100, 10_000_000),
    )
    prepared = registry.prepare(run_id=RUN_ID, provider="microsoft")
    assert prepared["sampleId"] == SAMPLE_ID.hex
    assert prepared["state"] == "prepared"
    assert prepared["sessionId"] is None
    assert prepared["markers"] == []

    with pytest.raises(ProviderReplayConflict):
        registry.prepare(run_id=RUN_ID, provider="soniox")

    unsupported = registry.arm_unsupported(
        run_id=RUN_ID,
        sample_id=SAMPLE_ID.hex,
        target_process_id=999,
        target_creation_time_100ns=123456,
    )
    assert unsupported["state"] == "unsupported"
    assert len(unsupported["targetGenerationSha256"] or "") == 64
    assert unsupported["sessionId"] is None
    assert unsupported["markers"] == []

    with pytest.raises(ProviderReplayConflict):
        registry.arm_unsupported(
            run_id=RUN_ID,
            sample_id=SAMPLE_ID.hex,
            target_process_id=999,
            target_creation_time_100ns=123456,
        )

    next_prepared = registry.prepare(run_id=RUN_ID, provider="soniox")
    assert next_prepared["sampleId"] == SECOND_SAMPLE_ID.hex


def test_arm_binds_target_process_generation_without_exposing_raw_identity():
    first = ProviderReplayRegistry(enabled_gate(), uuid_factory=lambda: SAMPLE_ID)
    first_sample = first.prepare(run_id=RUN_ID, provider="microsoft")
    first_status = first.arm_unsupported(
        run_id=RUN_ID,
        sample_id=first_sample["sampleId"],
        target_process_id=999,
        target_creation_time_100ns=123456,
    )
    second = ProviderReplayRegistry(enabled_gate(), uuid_factory=lambda: SAMPLE_ID)
    second_sample = second.prepare(run_id=RUN_ID, provider="microsoft")
    second_status = second.arm_unsupported(
        run_id=RUN_ID,
        sample_id=second_sample["sampleId"],
        target_process_id=999,
        target_creation_time_100ns=123457,
    )
    assert first_status["targetGenerationSha256"] != second_status[
        "targetGenerationSha256"
    ]
    assert "targetProcessId" not in first_status
    assert "targetCreationTime100ns" not in first_status


def test_registry_expires_samples_and_hides_wrong_run():
    now = [10.0]
    registry = ProviderReplayRegistry(
        enabled_gate(),
        ttl_seconds=1.0,
        monotonic=lambda: now[0],
        uuid_factory=lambda: SAMPLE_ID,
    )
    prepared = registry.prepare(run_id=RUN_ID, provider="microsoft")
    with pytest.raises(ProviderReplayNotFound):
        registry.status(run_id=OTHER_RUN_ID, sample_id=prepared["sampleId"])
    now[0] = 11.1
    with pytest.raises(ProviderReplayNotFound):
        registry.status(run_id=RUN_ID, sample_id=prepared["sampleId"])


def test_registry_binds_authoritative_fixture_duration(monkeypatch):
    registry = ProviderReplayRegistry(
        enabled_gate(),
        ttl_seconds=720.0,
        authoritative_fixture_duration_ms=600_000,
        uuid_factory=lambda: SAMPLE_ID,
    )
    prepared = registry.prepare(run_id=RUN_ID, provider="microsoft")
    assert prepared["authoritativeFixtureDurationMs"] == 600_000

    monkeypatch.setenv("SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_DURATION_MS", "15000")
    assert provider_replay_fixture_duration_ms_from_environment() == 15_000
    monkeypatch.setenv(
        "SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_DURATION_MS",
        "600001",
    )
    assert provider_replay_fixture_duration_ms_from_environment() == 350


def test_registry_preserves_capture_timeout_watchdog_failure_code():
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
    )
    prepared = registry.prepare(run_id=RUN_ID, provider="microsoft")

    failed = registry.fail(
        run_id=RUN_ID,
        sample_id=prepared["sampleId"],
        error_code="capture_timeout",
    )

    assert failed["state"] == "failed"
    assert failed["errorCode"] == "capture_timeout"


def test_qpc_markers_are_run_sample_session_and_process_bound():
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
        qpc_clock=lambda: (777, 10_000_000),
    )
    sample = registry.prepare(run_id=RUN_ID, provider="microsoft")
    registry.bind_session(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        session_id=SESSION_ID,
    )

    with pytest.raises(ProviderReplayConflict):
        registry.record_marker(
            run_id=RUN_ID,
            sample_id=sample["sampleId"],
            session_id=OTHER_RUN_ID,
            marker="provider_response_complete",
        )

    marker = registry.record_marker(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        session_id=SESSION_ID,
        marker="provider_response_complete",
    )
    assert marker == {
        "ok": True,
        "apiVersion": 1,
        "runId": RUN_ID,
        "sampleId": SAMPLE_ID.hex,
        "sessionId": SESSION_ID,
        "processGenerationFingerprint": enabled_gate().process_generation_fingerprint,
        "source": "installed_backend_provider_event",
        "marker": "provider_response_complete",
        "qpcTicks": 777,
        "qpcFrequency": 10_000_000,
    }
    with pytest.raises(ProviderReplayConflict):
        registry.record_marker(
            run_id=RUN_ID,
            sample_id=sample["sampleId"],
            session_id=SESSION_ID,
            marker="provider_response_complete",
        )


def test_execution_preserves_prebind_activation_qpc_until_session_binding():
    ticks = iter(((100, 1_000), (200, 1_000)))
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
        qpc_clock=lambda: next(ticks),
    )
    sample = registry.prepare(run_id=RUN_ID, provider="microsoft")
    execution = ProviderReplayExecution(
        registry=registry,
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        provider="microsoft",
        injection_target_guard=object(),
    )

    execution.marker("activation_received", qpc_snapshot=(50, 1_000))
    armed = execution.bind_session(SESSION_ID)
    execution.marker("stop_requested")
    status = registry.status(run_id=RUN_ID, sample_id=sample["sampleId"])

    assert armed["markers"] == []
    assert [item["marker"] for item in status["markers"]] == [
        "activation_received",
        "stop_requested",
    ]
    assert status["markers"][0]["qpcTicks"] == 50
    assert status["markers"][0]["source"] == "tauri_activation_boundary"


def test_started_replay_completes_only_after_bound_session_finished_marker():
    ticks = iter((101, 102))
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
        qpc_clock=lambda: (next(ticks), 10_000_000),
    )
    sample = registry.prepare(run_id=RUN_ID, provider="microsoft")
    starting = registry.begin_arm(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        target_process_id=999,
        target_creation_time_100ns=123456,
        activation_kind="hotkey",
    )
    assert starting["state"] == "activation_armed"
    starting = registry.claim_activation(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        activation_kind="hotkey",
    )
    assert starting["state"] == "starting"
    armed = registry.bind_session(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        session_id=SESSION_ID,
    )
    assert armed["state"] == "armed"
    registry.record_marker(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        session_id=SESSION_ID,
        marker="provider_response_complete",
    )
    registry.record_marker(
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        session_id=SESSION_ID,
        marker="session_finished_emitted",
    )
    completed = registry.status(run_id=RUN_ID, sample_id=sample["sampleId"])
    assert completed["state"] == "completed"
    assert [item["marker"] for item in completed["markers"]] == [
        "provider_response_complete",
        "session_finished_emitted",
    ]


def test_capture_attestation_is_bound_and_allowlisted_before_completion():
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
        qpc_clock=lambda: (101, 10_000_000),
    )
    sample = registry.prepare(run_id=RUN_ID, provider="microsoft")
    execution = ProviderReplayExecution(
        registry=registry,
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        provider="microsoft",
        injection_target_guard=object(),
    )
    execution.bind_session(SESSION_ID)
    capture = execution.attach_capture_attestation(
        {
            "fixturePcmSha256": "1" * 64,
            "capturedPcmSha256": "2" * 64,
            "sampleRate": 48_000,
            "channels": 1,
            "sampleWidthBytes": 2,
            "fixturePayloadBytesRead": 960_000,
            "fixtureAudioFramesRead": 480_000,
            "payloadBytesRead": 960_512,
            "audioFramesRead": 480_256,
            "trailingZeroFrames": 256,
            "expectedTrailingZeroFrames": 256,
            "captureBlockSizeFrames": 512,
            "exactFixtureEndAccepted": True,
            "eosFramesRead": 1,
            "eosObserved": True,
            "sidecarEosWritten": True,
            "droppedFrameCount": 0,
            "sequenceErrorCount": 0,
            "protocolErrorCount": 0,
            "prebufferAfterLiveCount": 0,
            "readerEndReason": "endOfStream",
            "tailKind": "zero_pcm_s16le",
            "fixturePrefixMatched": True,
            "tailAllZero": True,
            "path": r"C:\private\fixture.pcm",
        }
    )

    assert capture["runId"] == sample["runId"]
    assert capture["sampleId"] == sample["sampleId"]
    assert capture["sessionId"] == UUID(SESSION_ID).hex
    assert capture["processGenerationFingerprint"] == (
        enabled_gate().process_generation_fingerprint
    )
    assert capture["source"] == "rust_audio_frame_pipe_reader"
    assert "path" not in capture
    armed = registry.status(run_id=RUN_ID, sample_id=sample["sampleId"])
    assert armed["state"] == "armed"
    assert armed["captureAttestation"] == capture

    execution.marker("session_finished_emitted")
    completed = registry.status(run_id=RUN_ID, sample_id=sample["sampleId"])
    assert completed["state"] == "completed"
    assert completed["captureAttestation"] == capture


def test_capture_attestation_rejects_missing_or_duplicate_reader_evidence():
    registry = ProviderReplayRegistry(
        enabled_gate(),
        uuid_factory=lambda: SAMPLE_ID,
    )
    sample = registry.prepare(run_id=RUN_ID, provider="soniox")
    execution = ProviderReplayExecution(
        registry=registry,
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        provider="soniox",
        injection_target_guard=object(),
    )
    execution.bind_session(SESSION_ID)

    with pytest.raises(ProviderReplayConflict, match="unavailable"):
        execution.attach_capture_attestation(None)

    execution.attach_capture_attestation({"eosObserved": False})
    with pytest.raises(ProviderReplayConflict, match="already recorded"):
        execution.attach_capture_attestation({"eosObserved": True})


def test_audio_preparation_attestation_is_expected_actual_and_one_shot():
    registry = ProviderReplayRegistry(enabled_gate(), uuid_factory=lambda: SAMPLE_ID)
    sample = registry.prepare(
        run_id=RUN_ID,
        provider="speechmatics",
        expected_audio_preparation_implementation="wav_pcm16_file_v1",
    )
    execution = ProviderReplayExecution(
        registry=registry,
        run_id=RUN_ID,
        sample_id=sample["sampleId"],
        provider="speechmatics",
        injection_target_guard=object(),
        expected_audio_preparation_implementation="wav_pcm16_file_v1",
    )
    execution.bind_session(SESSION_ID)

    with pytest.raises(ProviderReplayConflict, match="mismatch"):
        execution.attach_audio_preparation_attestation(
            "python_reserved_wav_header_v1"
        )
    execution.attach_audio_preparation_attestation("wav_pcm16_file_v1")
    status = registry.status(run_id=RUN_ID, sample_id=sample["sampleId"])
    assert status["audioPreparationImplementationExpected"] == (
        "wav_pcm16_file_v1"
    )
    assert status["audioPreparationImplementationActual"] == (
        "wav_pcm16_file_v1"
    )
    with pytest.raises(ProviderReplayConflict, match="already recorded"):
        execution.attach_audio_preparation_attestation("wav_pcm16_file_v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preparation",
    [
        "post_stop_ffmpeg_mp3_v1",
        "capture_time_ffmpeg_mp3_v1",
    ],
)
async def test_microsoft_replay_transport_validates_baseline_and_candidate_mp3(
    tmp_path,
    preparation,
):
    duration_ms = 100
    capture_block_size_frames = 256
    pcm = _fixture_pcm(duration_ms)
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(pcm)
    zero_tail_frames = (-(48_000 * duration_ms // 1000)) % capture_block_size_frames
    mp3 = await _encode_fixture_mp3_or_skip(pcm + b"\0\0" * zero_tail_frames)
    await prewarm_azure_mai_replay_validation(
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        authoritative_fixture_pcm_path=fixture_path,
        capture_block_size_frames=capture_block_size_frames,
    )
    validated = []
    transport = create_azure_mai_replay_transport(
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        authoritative_fixture_pcm_path=fixture_path,
        capture_block_size_frames=capture_block_size_frames,
        expected_audio_preparation_implementation=preparation,
        on_audio_preparation_validated=validated.append,
    )
    kwargs = {
        "session": object(),
        "url": AZURE_REPLAY_URL,
        "audio_source": mp3,
        "filename": "audio.mp3",
        "content_type": "audio/mpeg",
        "definition": AZURE_REPLAY_DEFINITION,
        "speech_key": "local-replay",
        "timeout_secs": 10.0,
        "audio_preparation_implementation": preparation,
    }
    status, raw = await transport(**kwargs)
    assert status == 200
    assert json.loads(raw)["combinedPhrases"][0]["text"] == (
        "Scriber deterministic Microsoft provider replay."
    )
    assert validated == [preparation]
    with pytest.raises(RuntimeError, match="one-shot"):
        await transport(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_kind",
    [
        "corrupt_mp3",
        "wrong_fixture",
        "wrong_duration",
        "overlong_zero_tail",
        "oversized_mp3",
        "fixture_digest_mismatch",
        "credential_sentinel",
    ],
)
async def test_microsoft_replay_transport_rejects_unbound_mp3_without_attestation(
    tmp_path,
    invalid_kind,
):
    duration_ms = 100
    capture_block_size_frames = 256
    pcm = _fixture_pcm(duration_ms)
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(
        _fixture_pcm(duration_ms, frequency_hz=880.0)
        if invalid_kind == "fixture_digest_mismatch"
        else pcm
    )
    if invalid_kind == "corrupt_mp3":
        mp3 = b"not-an-mp3"
    elif invalid_kind == "wrong_fixture":
        mp3 = await _encode_fixture_mp3_or_skip(
            _fixture_pcm(duration_ms, frequency_hz=880.0)
        )
    elif invalid_kind == "wrong_duration":
        mp3 = await _encode_fixture_mp3_or_skip(pcm[: len(pcm) // 2])
    elif invalid_kind == "overlong_zero_tail":
        mp3 = await _encode_fixture_mp3_or_skip(
            pcm + b"\0\0" * (48_000 * 2100 // 1000)
        )
    elif invalid_kind == "oversized_mp3":
        mp3 = b"x" * (256 * 1024 + 1)
    else:
        exact_tail_frames = (
            -(48_000 * duration_ms // 1000)
        ) % capture_block_size_frames
        mp3 = await _encode_fixture_mp3_or_skip(
            pcm + b"\0\0" * exact_tail_frames
        )
    if invalid_kind not in {"fixture_digest_mismatch", "credential_sentinel"}:
        await prewarm_azure_mai_replay_validation(
            authoritative_fixture_duration_ms=duration_ms,
            expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
            authoritative_fixture_pcm_path=fixture_path,
            capture_block_size_frames=capture_block_size_frames,
        )
    validated = []
    transport = create_azure_mai_replay_transport(
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        authoritative_fixture_pcm_path=fixture_path,
        capture_block_size_frames=capture_block_size_frames,
        expected_audio_preparation_implementation="post_stop_ffmpeg_mp3_v1",
        on_audio_preparation_validated=validated.append,
    )

    with pytest.raises(RuntimeError):
        await transport(
            session=object(),
            url=AZURE_REPLAY_URL,
            audio_source=mp3,
            filename="audio.mp3",
            content_type="audio/mpeg",
            definition=AZURE_REPLAY_DEFINITION,
            speech_key=(
                "must-not-reach-replay-transport"
                if invalid_kind == "credential_sentinel"
                else "local-replay"
            ),
            timeout_secs=10.0,
            audio_preparation_implementation="post_stop_ffmpeg_mp3_v1",
        )
    assert validated == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "definition", "error"),
    [
        (
            AZURE_REPLAY_URL.replace("northeurope", "eastus"),
            AZURE_REPLAY_DEFINITION,
            "adapter URL mismatch",
        ),
        (
            AZURE_REPLAY_URL.replace("api-version=2025-10-15", "api-version=2024-11-15"),
            AZURE_REPLAY_DEFINITION,
            "adapter URL mismatch",
        ),
        (
            AZURE_REPLAY_URL.replace(
                "speechtotext/transcriptions:transcribe",
                "speechtotext/transcriptions",
            ),
            AZURE_REPLAY_DEFINITION,
            "adapter URL mismatch",
        ),
        (
            AZURE_REPLAY_URL,
            {
                "enhancedMode": {
                    "enabled": True,
                    "model": "mai-transcribe-regressed",
                },
                "locales": ["en-US"],
            },
            "definition mismatch",
        ),
        (
            AZURE_REPLAY_URL,
            {
                **AZURE_REPLAY_DEFINITION,
                "unexpected": True,
            },
            "definition mismatch",
        ),
    ],
)
async def test_microsoft_replay_transport_rejects_request_contract_regression(
    tmp_path,
    url,
    definition,
    error,
):
    validated = []
    transport = create_azure_mai_replay_transport(
        authoritative_fixture_duration_ms=100,
        expected_fixture_pcm_sha256="a" * 64,
        authoritative_fixture_pcm_path=tmp_path / "must-not-be-read.pcm",
        capture_block_size_frames=256,
        expected_audio_preparation_implementation="post_stop_ffmpeg_mp3_v1",
        on_audio_preparation_validated=validated.append,
    )

    with pytest.raises(RuntimeError, match=error):
        await transport(
            session=object(),
            url=url,
            audio_source=b"must-not-be-read",
            filename="audio.mp3",
            content_type="audio/mpeg",
            definition=definition,
            speech_key="local-replay",
            timeout_secs=10.0,
            audio_preparation_implementation="post_stop_ffmpeg_mp3_v1",
        )
    assert validated == []


@pytest.mark.asyncio
async def test_speechmatics_replay_uses_real_batch_adapter_parser_and_validates_wav():
    from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from src.core.provider_audio_formats import SPEECHMATICS_BATCH_DEFAULT_BASE_URL
    from src.pipeline import ScriberPipeline
    from src.transcript_artifacts import freeze_provider_route

    duration_ms = 100
    frame_count = 48_000 * duration_ms // 1000
    pcm = b"".join(
        int(12_000 * math.sin((2.0 * math.pi * 440.0 * index) / 48_000)).to_bytes(
            2,
            "little",
            signed=True,
        )
        for index in range(frame_count)
    )
    endpoint_sha256 = hashlib.sha256(
        SPEECHMATICS_BATCH_DEFAULT_BASE_URL.encode("utf-8")
    ).hexdigest()
    route = freeze_provider_route(
        workload="live_mic",
        provider="speechmatics_async",
        model="batch-v2",
        provider_route="batch_v2",
        audio_input_format="wav_pcm16",
        audio_selection_mode="generated",
        audio_preparation_implementation="python_reserved_wav_header_v1",
        provider_endpoint_sha256=endpoint_sha256,
        language="en-US",
        custom_vocab="",
        diarization_requested=False,
    ).execution_route()
    timeline: list[str] = []
    transport = create_speechmatics_batch_replay_transport(
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        capture_block_size_frames=480,
        expected_audio_preparation_implementation=(
            "python_reserved_wav_header_v1"
        ),
        on_audio_preparation_validated=timeline.append,
    )
    pipeline = ScriberPipeline(
        service_name="speechmatics_async",
        execution_route=route,
        speechmatics_batch_raw_transport=transport,
        speechmatics_capture_time_wav_enabled=False,
        on_provider_response_complete=lambda: timeline.append("provider_boundary"),
    )
    processor = pipeline._create_stt_service(object())
    frames: list[object] = []

    async def capture_frame(frame, *_args, **_kwargs):
        frames.append(frame)
        if isinstance(frame, TranscriptionFrame):
            timeline.append("parser_output")

    processor.push_frame = capture_frame
    await processor.process_frame(
        InputAudioRawFrame(audio=pcm, sample_rate=48_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    transcripts = [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)]
    assert transcripts == ["Scriber deterministic Speechmatics provider replay."]
    assert timeline == [
        "python_reserved_wav_header_v1",
        "provider_boundary",
        "parser_output",
    ]
    assert processor._api_key == "local-replay"
    assert processor._base_url == SPEECHMATICS_BATCH_DEFAULT_BASE_URL

    with pytest.raises(RuntimeError, match="one-shot"):
        await transport(
            session=object(),
            base_url=SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
            api_key="local-replay",
            audio_source=b"unused",
            filename="audio.wav",
            content_type="audio/wav",
            config={
                "type": "transcription",
                "transcription_config": {
                    "language": "en",
                    "operating_point": "enhanced",
                },
            },
            timeout_secs=10.0,
            poll_interval_secs=1.0,
            audio_preparation_implementation=(
                "python_reserved_wav_header_v1"
            ),
        )

    corrupt_wav = io.BytesIO()
    with wave.open(corrupt_wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48_000)
        writer.writeframes(pcm)
    corrupted = bytearray(corrupt_wav.getvalue())
    corrupted[-1] ^= 0x01
    rejecting_transport = create_speechmatics_batch_replay_transport(
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        capture_block_size_frames=480,
        expected_audio_preparation_implementation=(
            "python_reserved_wav_header_v1"
        ),
    )
    with pytest.raises(RuntimeError, match="fixture prefix mismatch"):
        await rejecting_transport(
            session=object(),
            base_url=SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
            api_key="local-replay",
            audio_source=bytes(corrupted),
            filename="audio.wav",
            content_type="audio/wav",
            config={
                "type": "transcription",
                "transcription_config": {
                    "language": "en",
                    "operating_point": "enhanced",
                },
            },
            timeout_secs=10.0,
            poll_interval_secs=1.0,
            audio_preparation_implementation=(
                "python_reserved_wav_header_v1"
            ),
        )


@pytest.mark.asyncio
async def test_replay_transports_reject_requested_actual_preparation_mismatch(tmp_path):
    validated: list[str] = []
    azure_transport = create_azure_mai_replay_transport(
        authoritative_fixture_duration_ms=100,
        expected_fixture_pcm_sha256="a" * 64,
        authoritative_fixture_pcm_path=tmp_path / "unused.pcm",
        capture_block_size_frames=256,
        expected_audio_preparation_implementation=(
            "capture_time_ffmpeg_mp3_v1"
        ),
        on_audio_preparation_validated=validated.append,
    )
    with pytest.raises(RuntimeError, match="audio preparation mismatch"):
        await azure_transport(
            session=object(),
            url=AZURE_REPLAY_URL,
            audio_source=b"encoded-mp3",
            filename="audio.mp3",
            content_type="audio/mpeg",
            definition=AZURE_REPLAY_DEFINITION,
            speech_key="local-replay",
            timeout_secs=10.0,
            audio_preparation_implementation="post_stop_ffmpeg_mp3_v1",
        )

    speechmatics_transport = create_speechmatics_batch_replay_transport(
        authoritative_fixture_duration_ms=100,
        expected_fixture_pcm_sha256="a" * 64,
        capture_block_size_frames=480,
        expected_audio_preparation_implementation="wav_pcm16_file_v1",
        on_audio_preparation_validated=validated.append,
    )
    with pytest.raises(RuntimeError, match="audio preparation mismatch"):
        await speechmatics_transport(
            session=object(),
            base_url="https://asr.api.speechmatics.com/v2",
            api_key="local-replay",
            audio_source=b"not-read-after-mismatch",
            filename="audio.wav",
            content_type="audio/wav",
            config={
                "type": "transcription",
                "transcription_config": {
                    "language": "en",
                    "operating_point": "enhanced",
                },
            },
            timeout_secs=10.0,
            poll_interval_secs=1.0,
            audio_preparation_implementation=(
                "python_reserved_wav_header_v1"
            ),
        )
    assert validated == []


@pytest.mark.asyncio
async def test_soniox_loopback_uses_real_websocket_messages_and_receive_boundary():
    from websockets.asyncio.client import connect

    server = await LocalSonioxReplayServer().start()
    markers: list[str] = []
    try:
        async with connect(server.url, compression=None) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "api_key": "local-replay",
                        "model": "stt-rt-v5",
                        "audio_format": "pcm_s16le",
                    }
                )
            )
            await websocket.send(b"\x01\x00" * 160)
            await websocket.send("")

            class Service:
                def _get_websocket(self):
                    return websocket

            service = Service()
            install_soniox_replay_receive_observer(
                service,
                final_message_sha256=server.final_message_sha256,
                on_last_final_token_received=lambda: markers.append("received"),
            )
            observed = [message async for message in service._get_websocket()]
        assert len(observed) == 1
        payload = json.loads(observed[0])
        assert payload["finished"] is True
        assert "".join(
            token["text"]
            for token in payload["tokens"]
            if token["text"] != "<end>"
        ) == "Scriber deterministic Soniox provider replay."
        assert markers == ["received"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_soniox_replay_traverses_real_pipecat_service_and_parser(monkeypatch):
    from pipecat.frames.frames import TranscriptionFrame

    from src.config import Config
    from src.pipeline import ScriberPipeline

    monkeypatch.setattr(Config, "SONIOX_API_KEY", "must-not-reach-loopback")
    monkeypatch.setattr(Config, "SONIOX_MODE", "async")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "must-not-reach-loopback")
    server = await LocalSonioxReplayServer().start()
    markers: list[str] = []
    frames: list[object] = []
    service = None
    try:
        pipeline = ScriberPipeline(
            service_name="soniox",
            soniox_replay_url=server.url,
            soniox_replay_final_message_sha256=server.final_message_sha256,
            on_soniox_last_final_token_received=lambda: markers.append("received"),
            soniox_replay_model="stt-rt-v5",
        )
        service = pipeline._create_stt_service(object())

        async def capture_frame(frame, *_args, **_kwargs):
            frames.append(frame)

        service.push_frame = capture_frame
        await service._connect_websocket()
        receive_task = asyncio.create_task(service._receive_messages())
        await service._websocket.send(b"\x01\x00" * 160)
        await service._websocket.send("")
        await asyncio.wait_for(receive_task, timeout=5.0)

        transcripts = [frame for frame in frames if isinstance(frame, TranscriptionFrame)]
        assert [frame.text for frame in transcripts] == [
            "Scriber deterministic Soniox provider replay."
        ]
        assert markers == ["received"]
        assert service._api_key == "local-replay"
        assert service._settings.model == "stt-rt-v5"
        assert server.error_code is None
    finally:
        if service is not None:
            await service._disconnect_websocket()
        await server.close()


def test_soniox_replay_pipeline_rejects_non_loopback_or_partial_configuration():
    from src.pipeline import ScriberPipeline

    with pytest.raises(ValueError, match="complete"):
        ScriberPipeline(
            service_name="soniox",
            soniox_replay_url="ws://127.0.0.1:1234/transcribe-websocket",
        )
    with pytest.raises(ValueError, match="loopback"):
        ScriberPipeline(
            service_name="soniox",
            soniox_replay_url="wss://stt-rt.soniox.com/transcribe-websocket",
            soniox_replay_final_message_sha256="a" * 64,
            on_soniox_last_final_token_received=lambda: None,
            soniox_replay_model="stt-rt-v5",
        )

@pytest.mark.asyncio
async def test_disabled_replay_control_plane_is_404_before_token_auth(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(
        web_api.ProviderReplayRuntimeGate,
        "from_environment",
        classmethod(lambda cls: ProviderReplayRuntimeGate.disabled("test")),
    )
    ctl = ScriberWebController(asyncio.get_running_loop())
    client = TestClient(TestServer(web_api.create_app(ctl)))
    await client.start_server()
    try:
        response = await client.post(
            "/api/runtime/benchmark/provider-replay/prepare",
            json={"schemaVersion": 1, "runId": RUN_ID, "provider": "microsoft"},
        )
        assert response.status == 404
        assert await response.json() == {"message": "Not found"}
    finally:
        await client.close()
        ctl.shutdown()


@pytest.mark.asyncio
async def test_enabled_replay_routes_require_native_activation_before_controller_start(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID", RUN_ID)
    monkeypatch.setenv("SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_PCM_SHA256", "a" * 64)
    monkeypatch.setenv(
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH",
        str(tmp_path / "fixture.pcm"),
    )
    gate = enabled_gate()
    monkeypatch.setattr(web_api.Config, "MIC_BLOCK_SIZE", 256)
    monkeypatch.setattr(
        web_api.ProviderReplayRuntimeGate,
        "from_environment",
        classmethod(lambda cls: gate),
    )
    monkeypatch.setattr(
        web_api,
        "ProviderReplayRegistry",
        lambda runtime_gate: ProviderReplayRegistry(
            runtime_gate,
            uuid_factory=lambda: SAMPLE_ID,
        ),
    )
    ctl = ScriberWebController(asyncio.get_running_loop())
    start_calls = 0
    prewarm_calls = []

    async def capture_target(**_kwargs):
        return object()

    async def prewarm_azure_validator(**kwargs):
        prewarm_calls.append(kwargs)

    async def start_replay(
        *,
        tauri_hotkey_marker=None,
        provider_replay_execution=None,
        **_kwargs,
    ):
        nonlocal start_calls
        start_calls += 1
        assert provider_replay_execution is not None
        assert tauri_hotkey_marker is not None
        assert tauri_hotkey_marker["marker"] == "hotkey_received"
        assert tauri_hotkey_marker["activationKind"] == "hotkey"
        ctl._provider_replay_execution = provider_replay_execution
        ctl._session_id = SESSION_ID
        ctl._is_listening = True
        ctl._pipeline_task = asyncio.create_task(asyncio.Event().wait())
        return None

    monkeypatch.setattr(
        web_api,
        "_capture_provider_replay_injection_target",
        capture_target,
    )
    monkeypatch.setattr(
        web_api,
        "prewarm_azure_mai_replay_validation",
        prewarm_azure_validator,
    )
    monkeypatch.setattr(
        web_api,
        "call_shell_ipc",
        lambda command, payload, **_kwargs: {
            "success": command == "benchmarkProviderReplayArm",
            "payload": {
                "armed": command == "benchmarkProviderReplayArm",
                "activationKind": payload.get("activationKind"),
            },
        },
    )
    monkeypatch.setattr(ctl, "start_listening", start_replay)
    client = TestClient(TestServer(web_api.create_app(ctl)))
    await client.start_server()
    headers = {"X-Scriber-Token": "secret"}
    try:
        unauthorized = await client.post(
            "/api/runtime/benchmark/provider-replay/prepare",
            json={"schemaVersion": 1, "runId": RUN_ID, "provider": "microsoft"},
        )
        assert unauthorized.status == 401

        extra_field = await client.post(
            "/api/runtime/benchmark/provider-replay/prepare",
            headers=headers,
            json={
                "schemaVersion": 1,
                "runId": RUN_ID,
                "provider": "microsoft",
                "text": "must not be accepted",
            },
        )
        assert extra_field.status == 400

        wrong_run = await client.post(
            "/api/runtime/benchmark/provider-replay/prepare",
            headers=headers,
            json={
                "schemaVersion": 1,
                "runId": OTHER_RUN_ID,
                "provider": "microsoft",
            },
        )
        assert wrong_run.status == 404

        prepared_response = await client.post(
            "/api/runtime/benchmark/provider-replay/prepare",
            headers=headers,
            json={"schemaVersion": 1, "runId": RUN_ID, "provider": "microsoft"},
        )
        prepared = await prepared_response.json()
        assert prepared_response.status == 201
        assert prepared["sampleId"] == SAMPLE_ID.hex
        assert prepared["processGenerationFingerprint"] == gate.process_generation_fingerprint
        assert prewarm_calls == [
            {
                "authoritative_fixture_duration_ms": 350,
                "expected_fixture_pcm_sha256": "a" * 64,
                "authoritative_fixture_pcm_path": str(tmp_path / "fixture.pcm"),
                "capture_block_size_frames": 256,
            }
        ]

        status_response = await client.get(
            f"/api/runtime/benchmark/provider-replay/{SAMPLE_ID.hex}",
            headers=headers,
            params={"runId": RUN_ID},
        )
        assert status_response.status == 200
        assert (await status_response.json())["state"] == "prepared"

        arm_response = await client.post(
            f"/api/runtime/benchmark/provider-replay/{SAMPLE_ID.hex}/arm",
            headers=headers,
            json={
                "schemaVersion": 1,
                "runId": RUN_ID,
                "targetProcessId": 999,
                "targetCreationTime100ns": 123456,
                "activationKind": "hotkey",
            },
        )
        armed = await arm_response.json()
        assert arm_response.status == 202
        assert armed["state"] == "activation_armed"
        assert armed["sessionId"] is None
        assert armed["activationKind"] == "hotkey"
        assert armed["markers"] == []
        assert armed["fixtureText"] == "Scriber deterministic Microsoft provider replay."
        assert start_calls == 0

        unmarked_start = await client.post(
            "/api/live-mic/start",
            headers=headers,
        )
        assert unmarked_start.status == 409
        assert start_calls == 0

        activation_qpc = time.perf_counter_ns()
        activation_payload = {
            "benchmarkActivationMarker": {
                "schemaVersion": 1,
                "marker": "hotkey_received",
                "source": "tauri_global_shortcut",
                "runId": RUN_ID,
                "sampleId": SAMPLE_ID.hex,
                "processId": os.getppid(),
                "qpcTicks": activation_qpc,
                "qpcFrequency": 1_000_000_000,
                "timestampNs": activation_qpc,
            }
        }
        activated_response = await client.post(
            "/api/live-mic/start",
            headers=headers,
            json=activation_payload,
        )
        assert activated_response.status == 200
        assert start_calls == 1

        activated_status = await client.get(
            f"/api/runtime/benchmark/provider-replay/{SAMPLE_ID.hex}",
            headers=headers,
            params={"runId": RUN_ID},
        )
        activated = await activated_status.json()
        assert activated["state"] == "armed"
        assert activated["sessionId"] == SESSION_ID
        assert [marker["marker"] for marker in activated["markers"]] == [
            "activation_received",
            "hotkey_received",
        ]
        assert (
            activated["markers"][0]["qpcTicks"]
            == activated["markers"][1]["qpcTicks"]
            == activation_qpc
        )

        duplicate_activation = await client.post(
            "/api/live-mic/start",
            headers=headers,
            json=activation_payload,
        )
        assert duplicate_activation.status == 409
        assert start_calls == 1

        duplicate = await client.post(
            f"/api/runtime/benchmark/provider-replay/{SAMPLE_ID.hex}/arm",
            headers=headers,
            json={
                "schemaVersion": 1,
                "runId": RUN_ID,
                "targetProcessId": 999,
                "targetCreationTime100ns": 123456,
                "activationKind": "hotkey",
            },
        )
        assert duplicate.status == 409
    finally:
        if ctl._pipeline_task is not None:
            ctl._pipeline_task.cancel()
            await asyncio.gather(ctl._pipeline_task, return_exceptions=True)
            ctl._pipeline_task = None
        if ctl._provider_replay_execution is not None:
            await ctl._provider_replay_execution.close()
            ctl._provider_replay_execution = None
        await client.close()
        ctl.shutdown()
