from __future__ import annotations

import asyncio
import json
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
    ProviderReplayNotFound,
    ProviderReplayRegistry,
    ProviderReplayRuntimeGate,
    create_azure_mai_replay_transport,
    install_soniox_replay_receive_observer,
    windows_qpc_snapshot,
)
from src.web_api import ScriberWebController


RUN_ID = "7de1a48651d44f859042b7cbcb30da52"
OTHER_RUN_ID = "8f793212ad894cbdac1118c373788aa5"
SAMPLE_ID = UUID("2b3022ee-3f40-4333-a115-6da089a24962")
SECOND_SAMPLE_ID = UUID("3d4054ff-5041-4444-b226-7eb190b35a73")
SESSION_ID = "4e51660061524555c3378fc2a1c46b84"


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


@pytest.mark.asyncio
async def test_microsoft_replay_transport_is_local_one_shot_and_consumes_audio():
    transport = create_azure_mai_replay_transport()
    kwargs = {
        "session": object(),
        "url": "https://northeurope.api.cognitive.microsoft.com/example",
        "audio_source": b"encoded-mp3",
        "filename": "audio.mp3",
        "content_type": "audio/mpeg",
        "definition": {"enhancedMode": {"model": "mai-transcribe-1.5"}},
        "speech_key": "local-replay",
        "timeout_secs": 10.0,
    }
    status, raw = await transport(**kwargs)
    assert status == 200
    assert json.loads(raw)["combinedPhrases"][0]["text"] == (
        "Scriber deterministic Microsoft provider replay."
    )
    with pytest.raises(RuntimeError, match="one-shot"):
        await transport(**kwargs)


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
async def test_enabled_replay_routes_require_token_and_arm_starts_real_controller_task(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    gate = enabled_gate()
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

    async def capture_target(**_kwargs):
        return object()

    async def start_replay(*, provider_replay_execution=None, **_kwargs):
        assert provider_replay_execution is not None
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
            },
        )
        armed = await arm_response.json()
        assert arm_response.status == 202
        assert armed["state"] == "armed"
        assert armed["sessionId"] == SESSION_ID
        assert armed["markers"] == []
        assert armed["fixtureText"] == "Scriber deterministic Microsoft provider replay."

        duplicate = await client.post(
            f"/api/runtime/benchmark/provider-replay/{SAMPLE_ID.hex}/arm",
            headers=headers,
            json={
                "schemaVersion": 1,
                "runId": RUN_ID,
                "targetProcessId": 999,
                "targetCreationTime100ns": 123456,
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
