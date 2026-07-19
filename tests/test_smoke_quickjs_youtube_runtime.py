from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from scripts import smoke_quickjs_youtube_runtime as smoke


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPO_ROOT
    / "scripts"
    / "perf"
    / "profiles"
    / "installer-size"
    / "youtube-holdouts.json"
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _candidate_payload(tmp_path: Path) -> tuple[Path, Path]:
    payload = tmp_path / "candidate"
    backend_root = payload / "backend"
    tools = backend_root / "tools" / "ffmpeg"
    tools.mkdir(parents=True)
    (backend_root / "scriber-backend.exe").write_bytes(b"frozen-backend")
    wrapper = b"bounded-wrapper"
    engine = b"quickjs-engine"
    (tools / "qjs.exe").write_bytes(wrapper)
    (tools / "qjs-engine.exe").write_bytes(engine)
    (tools / "LICENSE.quickjs-ng.txt").write_bytes(b"MIT license")
    manifest = {
        "contract": "ScriberYoutubeJsRuntimeManifestV3",
        "schemaVersion": 3,
        "runtime": {
            "kind": "quickjs",
            "implementation": "bounded-quickjs-wrapper",
            "version": "0.15.0",
            "protocol": "ScriberYtDlpQuickJsFileV1",
            "executable": "qjs.exe",
            "length": len(wrapper),
            "sha256": _sha256(wrapper),
            "engine": "qjs-engine.exe",
            "engineLength": len(engine),
            "engineSha256": _sha256(engine),
            "licenseFile": "LICENSE.quickjs-ng.txt",
        },
        "policy": {
            "remoteComponents": False,
            "firstRunDownloads": False,
            "maximumStdoutBytes": 4 * 1024 * 1024,
            "maximumStderrBytes": 256 * 1024,
            "timeoutMilliseconds": 45_000,
            "exactArgumentProtocol": True,
            "engineHashVerified": True,
            "killOnJobClose": True,
        },
    }
    (tools / "js-runtime-manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )
    return payload, tools / "qjs.exe"


def _args(tmp_path: Path, payload: Path, runtime: Path) -> argparse.Namespace:
    return argparse.Namespace(
        candidate_payload=payload,
        runtime=runtime,
        fixture=FIXTURE,
        scratch_root=tmp_path / "scratch",
        timeout_seconds=30,
        output=tmp_path / "evidence" / "quickjs-smoke.json",
    )


def _pass_response(request: dict[str, object], elapsed_ns: int) -> bytes:
    cases, _binding = smoke._load_cases(FIXTURE)
    case = next(item for item in cases if item["caseId"] == request["caseId"])
    capabilities = sorted(set(case["requiredCapabilities"]) | {"js-runtime"})
    return json.dumps(
        {
            "probeContract": smoke.FROZEN_PROBE_CONTRACT,
            "schemaVersion": 1,
            "caseId": request["caseId"],
            "runtimeKind": "quickjs",
            "ytDlpVersion": "2026.7.4",
            "ejsVersion": "0.8.0",
            "policy": smoke.PROBE_POLICY,
            "status": "pass",
            "videoId": request["expectedVideoId"],
            "durationNs": elapsed_ns // 2,
            "observedCapabilities": capabilities,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_product_smoke_runs_all_six_frozen_cases_and_writes_redacted_evidence(
    tmp_path: Path,
) -> None:
    payload, runtime = _candidate_payload(tmp_path)
    args = _args(tmp_path, payload, runtime)
    args.scratch_root.mkdir()
    requests: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> smoke.CommandResult:
        assert command == [
            str((payload / "backend" / "scriber-backend.exe").resolve()),
            "--installer-youtube-holdout-probe",
        ]
        request = json.loads(kwargs["stdin_bytes"])
        requests.append(request)
        assert request["runtimeKind"] == "quickjs"
        assert request["runtimePath"] == str(runtime.resolve())
        assert request["cacheMode"] == "cold"
        assert kwargs["env"]["YTDLP_NO_PLUGINS"] == "1"
        elapsed_ns = 10_000_000
        return smoke.CommandResult(
            status="completed",
            return_code=0,
            elapsed_ns=elapsed_ns,
            stdout=_pass_response(request, elapsed_ns),
            stderr=b"must not be persisted",
            cleanup_verified=True,
        )

    evidence = smoke.run_smoke(args, runner=runner)

    assert evidence["status"] == "pass"
    assert evidence["reasonCodes"] == []
    assert len(requests) == 6
    assert len({request["caseId"] for request in requests}) == 6
    assert evidence["inputImmutabilityVerified"] is True
    assert all(row["status"] == "pass" for row in evidence["cases"])
    encoded = args.output.read_text(encoding="utf-8").casefold()
    assert "youtube.com/" not in encoded
    assert "youtu.be/" not in encoded
    assert "videoid" not in encoded
    assert "runtimepath" not in encoded
    assert "must not be persisted" not in encoded
    assert str(tmp_path).casefold() not in encoded


def test_product_smoke_fails_when_a_required_capability_is_missing(
    tmp_path: Path,
) -> None:
    payload, runtime = _candidate_payload(tmp_path)
    args = _args(tmp_path, payload, runtime)
    args.scratch_root.mkdir()

    def runner(_command: list[str], **kwargs: object) -> smoke.CommandResult:
        request = json.loads(kwargs["stdin_bytes"])
        elapsed_ns = 10_000_000
        response = json.loads(_pass_response(request, elapsed_ns))
        response["observedCapabilities"] = ["js-runtime"]
        return smoke.CommandResult(
            status="completed",
            return_code=0,
            elapsed_ns=elapsed_ns,
            stdout=json.dumps(response, separators=(",", ":")).encode(),
            stderr=b"https://www.youtube.com/watch?v=secret",
            cleanup_verified=True,
        )

    evidence = smoke.run_smoke(args, runner=runner)

    assert evidence["status"] == "fail"
    assert evidence["reasonCodes"] == ["candidate_capability_missing"]
    assert any(row["missingRequiredCapabilities"] for row in evidence["cases"])
    assert "youtube.com" not in args.output.read_text(encoding="utf-8").casefold()


def test_product_smoke_rejects_runtime_outside_exact_candidate_location(
    tmp_path: Path,
) -> None:
    payload, _runtime = _candidate_payload(tmp_path)
    outside = tmp_path / "qjs.exe"
    outside.write_bytes(b"bounded-wrapper")
    args = _args(tmp_path, payload, outside)
    args.scratch_root.mkdir()

    with pytest.raises(
        smoke.SmokeError, match="candidate-runtime-not-explicit-payload-runtime"
    ):
        smoke.run_smoke(args, runner=lambda *_args, **_kwargs: None)


def test_product_smoke_detects_candidate_input_mutation(tmp_path: Path) -> None:
    payload, runtime = _candidate_payload(tmp_path)
    args = _args(tmp_path, payload, runtime)
    args.scratch_root.mkdir()
    mutated = False

    def runner(_command: list[str], **kwargs: object) -> smoke.CommandResult:
        nonlocal mutated
        request = json.loads(kwargs["stdin_bytes"])
        if not mutated:
            runtime.write_bytes(b"changed-during-smoke")
            mutated = True
        elapsed_ns = 10_000_000
        return smoke.CommandResult(
            status="completed",
            return_code=0,
            elapsed_ns=elapsed_ns,
            stdout=_pass_response(request, elapsed_ns),
            stderr=b"",
            cleanup_verified=True,
        )

    evidence = smoke.run_smoke(args, runner=runner)

    assert evidence["status"] == "fail"
    assert evidence["reasonCodes"] == ["input_changed"]
    assert evidence["inputImmutabilityVerified"] is False


def test_bounded_runner_times_out_and_verifies_cleanup(tmp_path: Path) -> None:
    result = smoke._run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdin_bytes=b"{}",
        timeout_seconds=0.15,
    )

    assert result.status == "timeout"
    assert result.cleanup_verified is True
    assert result.elapsed_ns < 10_000_000_000


def test_bounded_runner_stops_on_output_limit(tmp_path: Path) -> None:
    result = smoke._run_bounded(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                f"sys.stdout.write('x'*{smoke.MAX_STDOUT_BYTES * 4}); "
                "sys.stdout.flush(); time.sleep(30)"
            ),
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdin_bytes=b"{}",
        timeout_seconds=5,
    )

    assert result.status == "output_limit"
    assert result.cleanup_verified is True
    assert len(result.stdout) <= smoke.MAX_STDOUT_BYTES + 1


def test_main_does_not_echo_an_invalid_candidate_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_path = tmp_path / "SECRET-CANDIDATE-PATH"

    exit_code = smoke.main(
        [
            "--candidate-payload",
            str(secret_path),
            "--runtime",
            str(secret_path / "qjs.exe"),
            "--fixture",
            str(FIXTURE),
            "--output",
            str(tmp_path / "output.json"),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "SECRET-CANDIDATE-PATH" not in output
    assert json.loads(output)["reasonCodes"] == ["candidate-payload-invalid"]
