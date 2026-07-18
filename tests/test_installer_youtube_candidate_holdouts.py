from __future__ import annotations

import hashlib
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import validate_installer_youtube_candidate_holdouts as holdouts


def _bound_stack_fixture(
    root: Path, *, label: str = "baseline"
) -> tuple[holdouts.StackIdentity, dict[str, object]]:
    contents = {
        "backend/scriber-backend.exe": b"backend",
        "backend/tools/ffmpeg/deno.exe": b"runtime",
        "backend/tools/ffmpeg/js-runtime-manifest.json": b"{}\n",
        "backend/_internal/yt_dlp/__init__.py": b"yt-dlp-code",
        "backend/_internal/yt_dlp_ejs/__init__.py": b"ejs-code",
        "backend/_internal/yt_dlp-2026.7.4.dist-info/METADATA": b"yt-dlp-metadata",
        "backend/_internal/yt_dlp_ejs-0.8.0.dist-info/METADATA": b"ejs-metadata",
    }
    entries: list[dict[str, object]] = []
    for relative, content in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        component = (
            "yt-dlp-ejs"
            if "/yt_dlp" in f"/{relative}" and "/tools/ffmpeg/" not in f"/{relative}"
            else None
        )
        entries.append(
            {
                "path": relative,
                "length": len(content),
                "sha256": holdouts._sha256_file(path),
                "component": component,
            }
        )
    tree = {
        "semanticTreeSha256": "a" * 64,
        "fileListSha256": "b" * 64,
        "files": entries,
    }
    inventory: dict[str, object] = {
        "payload": {"staged": tree, "installed": tree},
    }
    runtime_path = root / "backend/tools/ffmpeg/deno.exe"
    backend_path = root / "backend/scriber-backend.exe"
    stack = holdouts.StackIdentity(
        label=label,
        root=root.resolve(),
        backend_executable=backend_path.resolve(),
        inventory=inventory,
        inventory_sha256="c" * 64,
        runtime=holdouts.RuntimeIdentity(
            kind="deno",
            version="2.5.2",
            executable=runtime_path.resolve(),
            length=runtime_path.stat().st_size,
            sha256=holdouts._sha256_file(runtime_path),
            origin="https://example.test/deno.exe",
            license="MIT",
            provenance="test",
            manifest_sha256="d" * 64,
            provenance_lock_entry=None,
            provenance_lock_sha256=None,
        ),
        yt_dlp=holdouts.DistributionIdentity(
            name="yt-dlp",
            version="2026.7.4",
            content_sha256="e" * 64,
            origin="https://example.test/yt-dlp",
            license="Unlicense",
        ),
        ejs=holdouts.DistributionIdentity(
            name="yt-dlp-ejs",
            version="0.8.0",
            content_sha256="f" * 64,
            origin="https://example.test/ejs",
            license="Unlicense",
        ),
        component_content_sha256="0" * 64,
    )
    return stack, inventory


def _outcome(
    *,
    status: str = "pass",
    duration_ns: int = 100,
    failure_code: str | None = None,
    capabilities: tuple[str, ...] = ("audio-format-url", "js-runtime", "metadata"),
) -> holdouts.ProbeOutcome:
    return holdouts.ProbeOutcome(
        status=status,
        duration_ns=duration_ns,
        failure_code=failure_code,
        semantic_capabilities=capabilities if status == "pass" else (),
        cleanup_verified=True,
    )


def _synthetic_quickjs_lock(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], bytes, bytes]:
    lock = json.loads(holdouts.PROVENANCE_LOCK_PATH.read_text(encoding="utf-8"))
    entry = lock["entries"][0]
    runtime_bytes = b"locked-quickjs-runtime"
    license_bytes = b"locked MIT license\n"
    runtime_sha256 = hashlib.sha256(runtime_bytes).hexdigest()
    license_sha256 = hashlib.sha256(license_bytes).hexdigest()
    entry["asset"]["length"] = len(runtime_bytes)
    entry["asset"]["sha256"] = runtime_sha256
    entry["asset"]["upstreamPublishedSha256"] = runtime_sha256
    entry["runtimeFiles"][0]["length"] = len(runtime_bytes)
    entry["runtimeFiles"][0]["sha256"] = runtime_sha256
    entry["license"]["length"] = len(license_bytes)
    entry["license"]["sha256"] = license_sha256
    entry["license"]["source"]["length"] = len(license_bytes)
    entry["license"]["source"]["sha256"] = license_sha256
    entry["manifest"] = holdouts._quickjs_manifest_for_entry(
        entry, entry["runtimeFiles"][0]
    )
    manifest_bytes = holdouts._canonical_manifest_bytes(entry["manifest"])
    entry["manifestCanonicalSha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    lock_path = tmp_path / "quickjs-runtime-lock-v1.json"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock_path, entry, runtime_bytes, license_bytes


def _locked_candidate_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    dict[str, object],
    str,
    Path,
    dict[str, object],
    Path,
]:
    lock_path, entry, runtime_bytes, license_bytes = _synthetic_quickjs_lock(tmp_path)
    root = tmp_path / "payload"
    tools = root / "backend/tools/ffmpeg"
    tools.mkdir(parents=True)
    executable_relative = "backend/tools/ffmpeg/qjs.exe"
    executable = root / executable_relative
    executable.write_bytes(runtime_bytes)
    (tools / entry["license"]["installedFileName"]).write_bytes(license_bytes)
    manifest_path = tools / "js-runtime-manifest.json"
    manifest_path.write_bytes(holdouts._canonical_manifest_bytes(entry["manifest"]))
    entries = []
    for path in (executable, tools / entry["license"]["installedFileName"], manifest_path):
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "length": path.stat().st_size,
                "sha256": holdouts._sha256_file(path),
            }
        )
    inventory: dict[str, object] = {"payload": {"staged": {"files": entries}}}
    executable_item = next(item for item in entries if item["path"] == executable_relative)
    return (
        lock_path,
        inventory,
        executable_relative,
        executable,
        executable_item,
        manifest_path,
    )


def test_pair_requires_exact_semantic_capability_parity() -> None:
    baseline = _outcome()
    candidate = _outcome(capabilities=("audio-format-url", "js-runtime"))

    status, reason = holdouts.classify_pair(
        baseline,
        candidate,
        required_capabilities=("metadata", "audio-format-url", "deno-runtime"),
    )

    assert status == "fail"
    assert reason == "candidate_capability_regression"


def test_only_same_paired_failure_is_external_invalid() -> None:
    same = holdouts.classify_pair(
        _outcome(status="fail", failure_code="http_429"),
        _outcome(status="fail", failure_code="http_429"),
        required_capabilities=("metadata",),
    )
    different = holdouts.classify_pair(
        _outcome(status="fail", failure_code="http_429"),
        _outcome(status="fail", failure_code="network_timeout"),
        required_capabilities=("metadata",),
    )

    assert same == ("external_invalid", "http_429")
    assert different == ("fail", "unpaired_failure")
    assert holdouts._scientific_status(["external_invalid"], True)[0] == "not_run"


def test_p95_gate_uses_nearest_rank_and_exact_110_percent_limit() -> None:
    baseline = list(range(1, 21))

    passed, passing = holdouts.performance_gate(baseline, [value * 11 // 10 for value in baseline])
    failed, failing = holdouts.performance_gate(baseline, [*range(1, 19), 23, 23])

    assert passed is True
    assert passing["baselineP95Ns"] == 19
    assert passing["maximumCandidateP95Ns"] == 20
    assert failed is False
    assert failing["candidateP95Ns"] == 23


def test_quickjs_version_contract_uses_documented_help_shape() -> None:
    runtime = holdouts.RuntimeIdentity(
        kind="quickjs",
        version="2025-04-26",
        executable=Path("qjs.exe"),
        length=1,
        sha256="a" * 64,
        origin="https://example.test/qjs.exe",
        license="MIT",
        provenance="payload-manifest",
        manifest_sha256="b" * 64,
        provenance_lock_entry="quickjs-test",
        provenance_lock_sha256="c" * 64,
    )

    assert holdouts._runtime_version_command(runtime)[-1] == "--help"
    assert holdouts._runtime_version_return_code_ok(runtime, 1) is True
    assert (
        holdouts._runtime_version_from_output(
            runtime, b"QuickJS version 2025-04-26\nusage: qjs [options]"
        )
        is True
    )


def test_quickjs_security_boundary_rejects_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deno_stack, _inventory = _bound_stack_fixture(tmp_path)
    holdouts._require_runtime_security_boundary(deno_stack.runtime)
    candidate = replace(
        deno_stack,
        label="candidate",
        runtime=replace(deno_stack.runtime, kind="quickjs"),
    )
    calls: list[str] = []

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        calls.append("runtime")

    def unexpected_probe(*_args: object, **_kwargs: object) -> None:
        calls.append("probe")

    original_probe = holdouts._probe
    monkeypatch.setattr(holdouts, "_run_bounded", unexpected_run)
    monkeypatch.setattr(holdouts, "_probe_command", unexpected_probe)

    with pytest.raises(holdouts.HoldoutError, match="Restricted-Token/AppContainer"):
        holdouts._runtime_self_tests(candidate.runtime, object())  # type: ignore[arg-type]
    with pytest.raises(holdouts.HoldoutError, match="Restricted-Token/AppContainer"):
        original_probe(
            stack=candidate,
            case={},
            factory=object(),  # type: ignore[arg-type]
            timeout_seconds=30,
            purpose="must-not-run",
            cache_dir=None,
        )

    monkeypatch.setattr(holdouts, "_probe", unexpected_probe)
    with pytest.raises(holdouts.HoldoutError, match="Restricted-Token/AppContainer"):
        holdouts._parallel_candidate_probe(
            candidate=candidate,
            case={},
            factory=object(),  # type: ignore[arg-type]
            timeout_seconds=30,
        )

    assert calls == []


def test_protected_quickjs_lock_prefers_ng_and_pins_official_bytes() -> None:
    entries, lock_sha256 = holdouts._load_quickjs_provenance_lock()

    assert len(lock_sha256) == 64
    assert list(entries) == [
        "quickjs-ng-0.15.0-windows-x86_64",
        "quickjs-2026-06-04-windows-x86_64",
    ]
    primary = entries["quickjs-ng-0.15.0-windows-x86_64"]
    fallback = entries["quickjs-2026-06-04-windows-x86_64"]
    assert primary["asset"]["sha256"] == (
        "f157d58a9e14e958991e4b0f01b3a6d1d7dc25f3ae78f85c6c8da01c19bf77bf"
    )
    assert primary["license"]["sha256"] == (
        "96f73f9d2a16c21a36b418f06073be26e7d6d5e7c1bc99756b21a4f2c74ef171"
    )
    assert fallback["asset"]["sha256"] == (
        "8d10e75796656f49a3797e2c14465bd67c1f085dba505cf0c8d8a14bf5b19cb4"
    )
    assert fallback["runtimeFiles"][0]["sha256"] == (
        "433a35a59bd6ff8950c57e6c7e809cae3fa01302ff673c121488b45f738afe3a"
    )
    assert fallback["runtimeFiles"][1]["sha256"] == (
        "1933c3f02ede171b7d9432204a89dfbdd846b86819df3040b0804fe1d02a8b16"
    )
    assert fallback["license"]["sha256"] == (
        "598fd7fc928e4350abce36e337ba5a1346923c5c692f5be92c3d8e29ddd7c18d"
    )


def test_candidate_manifest_and_files_must_match_protected_lock_bytes(
    tmp_path: Path,
) -> None:
    (
        lock_path,
        inventory,
        relative,
        executable,
        executable_item,
        manifest_path,
    ) = _locked_candidate_fixture(tmp_path)

    identity = holdouts._runtime_manifest_identity(
        root=executable.parents[3],
        inventory=inventory,
        installed=False,
        relative=relative,
        kind="quickjs",
        executable=executable,
        executable_item=executable_item,
        provenance_lock_path=lock_path,
    )

    assert identity is not None
    assert identity.provenance == "protected-provenance-lock"
    assert identity.provenance_lock_entry == "quickjs-ng-0.15.0-windows-x86_64"
    assert identity.provenance_lock_sha256 == holdouts._sha256_file(lock_path)

    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["runtime"]["origin"] = "https://github.com/quickjs-ng/quickjs"
    manifest_path.write_bytes(holdouts._canonical_manifest_bytes(tampered))
    manifest_entry = next(
        item
        for item in inventory["payload"]["staged"]["files"]
        if item["path"] == "backend/tools/ffmpeg/js-runtime-manifest.json"
    )
    manifest_entry["length"] = manifest_path.stat().st_size
    manifest_entry["sha256"] = holdouts._sha256_file(manifest_path)

    with pytest.raises(holdouts.HoldoutError, match="byte-exact"):
        holdouts._runtime_manifest_identity(
            root=executable.parents[3],
            inventory=inventory,
            installed=False,
            relative=relative,
            kind="quickjs",
            executable=executable,
            executable_item=executable_item,
            provenance_lock_path=lock_path,
        )

    locked_manifest = json.loads(lock_path.read_text(encoding="utf-8"))["entries"][0][
        "manifest"
    ]
    manifest_path.write_text(json.dumps(locked_manifest, indent=2) + "\n", encoding="utf-8")
    manifest_entry["length"] = manifest_path.stat().st_size
    manifest_entry["sha256"] = holdouts._sha256_file(manifest_path)
    with pytest.raises(holdouts.HoldoutError, match="byte-exact"):
        holdouts._runtime_manifest_identity(
            root=executable.parents[3],
            inventory=inventory,
            installed=False,
            relative=relative,
            kind="quickjs",
            executable=executable,
            executable_item=executable_item,
            provenance_lock_path=lock_path,
        )

    manifest_path.write_bytes(holdouts._canonical_manifest_bytes(locked_manifest))
    manifest_entry["length"] = manifest_path.stat().st_size
    manifest_entry["sha256"] = holdouts._sha256_file(manifest_path)
    license_path = manifest_path.parent / locked_manifest["runtime"]["licenseFile"]
    license_path.write_bytes(b"self-attested replacement license")
    license_entry = next(
        item
        for item in inventory["payload"]["staged"]["files"]
        if item["path"] == license_path.relative_to(executable.parents[3]).as_posix()
    )
    license_entry["length"] = license_path.stat().st_size
    license_entry["sha256"] = holdouts._sha256_file(license_path)
    with pytest.raises(holdouts.HoldoutError, match="protected lock"):
        holdouts._runtime_manifest_identity(
            root=executable.parents[3],
            inventory=inventory,
            installed=False,
            relative=relative,
            kind="quickjs",
            executable=executable,
            executable_item=executable_item,
            provenance_lock_path=lock_path,
        )


def test_probe_outcome_accepts_only_bounded_frozen_contract() -> None:
    payload = {
        "probeContract": holdouts.FROZEN_PROBE_CONTRACT,
        "schemaVersion": 1,
        "caseId": "regular-audio",
        "runtimeKind": "quickjs",
        "ytDlpVersion": "2026.7.4",
        "ejsVersion": "0.8.0",
        "policy": {
            "configDiscovery": False,
            "externalPlugins": False,
            "remoteComponents": False,
            "download": False,
            "explicitSingleRuntime": True,
        },
        "status": "pass",
        "videoId": "abcdefghijk",
        "durationNs": 500,
        "observedCapabilities": ["audio-format-url", "js-runtime", "metadata"],
    }
    result = holdouts.CommandResult(
        status="completed",
        return_code=0,
        elapsed_ns=1_000,
        stdout=json.dumps(payload).encode(),
        stderr=b"",
        cleanup_verified=True,
        workspace_fingerprint="a" * 64,
    )

    outcome = holdouts._probe_outcome(
        result,
        case_id="regular-audio",
        expected_video_id="abcdefghijk",
        runtime_kind="quickjs",
        yt_dlp_version="2026.7.4",
        ejs_version="0.8.0",
    )

    assert outcome.status == "pass"
    assert outcome.duration_ns == 500
    assert outcome.semantic_capabilities == (
        "audio-format-url",
        "js-runtime",
        "metadata",
    )


def test_probe_outcome_rejects_policy_or_version_drift() -> None:
    payload = {
        "probeContract": holdouts.FROZEN_PROBE_CONTRACT,
        "schemaVersion": 1,
        "caseId": "regular-audio",
        "runtimeKind": "deno",
        "ytDlpVersion": "different",
        "ejsVersion": "0.8.0",
        "policy": {
            "configDiscovery": False,
            "externalPlugins": False,
            "remoteComponents": True,
            "download": False,
            "explicitSingleRuntime": True,
        },
        "status": "pass",
        "videoId": "abcdefghijk",
        "durationNs": 1,
        "observedCapabilities": ["js-runtime"],
    }
    result = holdouts.CommandResult(
        status="completed",
        return_code=0,
        elapsed_ns=10,
        stdout=json.dumps(payload).encode(),
        stderr=b"",
        cleanup_verified=True,
        workspace_fingerprint="b" * 64,
    )

    outcome = holdouts._probe_outcome(
        result,
        case_id="regular-audio",
        expected_video_id="abcdefghijk",
        runtime_kind="deno",
        yt_dlp_version="2026.7.4",
        ejs_version="0.8.0",
    )

    assert outcome.status == "fail"
    assert outcome.failure_code == "probe_contract_invalid"


def test_private_runner_cleans_success_error_timeout_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(holdouts, "_private_acl", lambda _path: "test-private")
    factory = holdouts.PrivateWorkspaceFactory(tmp_path)
    try:
        success = holdouts._run_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            factory=factory,
            purpose="test-success",
            timeout_seconds=5,
            stdin_bytes=b"bounded",
        )
        error = holdouts._run_bounded(
            [sys.executable, "-c", "raise SystemExit(7)"],
            factory=factory,
            purpose="test-error",
            timeout_seconds=5,
        )
        timeout = holdouts._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            factory=factory,
            purpose="test-timeout",
            timeout_seconds=0.15,
        )
        cancelled_event = threading.Event()
        timer = threading.Timer(0.1, cancelled_event.set)
        timer.start()
        try:
            cancelled = holdouts._run_bounded(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                factory=factory,
                purpose="test-cancel",
                timeout_seconds=5,
                cancel_event=cancelled_event,
            )
        finally:
            timer.cancel()
    finally:
        factory.close()

    assert success.status == "completed"
    assert success.stdout == b"bounded"
    assert error.return_code == 7
    assert timeout.status == "timeout"
    assert cancelled.status == "cancelled"
    assert factory.created_count == factory.cleanup_count == 4
    assert not factory.root.exists()


def test_private_workspace_acl_failure_removes_only_its_created_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    attempted: list[Path] = []

    def reject_acl(path: Path) -> str:
        attempted.append(path)
        raise holdouts.HoldoutError("synthetic ACL failure")

    monkeypatch.setattr(holdouts, "_private_acl", reject_acl)

    with pytest.raises(holdouts.HoldoutError, match="synthetic ACL failure"):
        holdouts.PrivateWorkspaceFactory(tmp_path)

    assert len(attempted) == 1
    assert not attempted[0].exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_private_workspace_acl_failure_never_deletes_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved_created_root = tmp_path / "moved-created-root"
    replacement_sentinel: Path | None = None

    def replace_then_reject(path: Path) -> str:
        nonlocal replacement_sentinel
        path.rename(moved_created_root)
        path.mkdir()
        replacement_sentinel = path / "not-owned.txt"
        replacement_sentinel.write_text("not-owned", encoding="utf-8")
        raise holdouts.HoldoutError("synthetic swapped ACL failure")

    monkeypatch.setattr(holdouts, "_private_acl", replace_then_reject)

    with pytest.raises(holdouts.HoldoutError, match="synthetic swapped ACL failure"):
        holdouts.PrivateWorkspaceFactory(tmp_path)

    assert moved_created_root.is_dir()
    assert replacement_sentinel is not None
    assert replacement_sentinel.read_text(encoding="utf-8") == "not-owned"


def test_bound_input_snapshot_rehashes_all_relevant_payload_files(
    tmp_path: Path,
) -> None:
    stack, _inventory = _bound_stack_fixture(tmp_path)

    snapshot = holdouts._capture_bound_inputs(stack, installed=False)

    assert snapshot.file_count == 7
    assert len(snapshot.content_sha256) == 64
    assert sum("METADATA" in relative for relative, _length, _sha in snapshot.files) == 2
    assert any(relative.endswith("js-runtime-manifest.json") for relative, *_ in snapshot.files)


def test_bound_input_snapshot_rejects_changed_file_even_when_inventory_is_unchanged(
    tmp_path: Path,
) -> None:
    stack, inventory = _bound_stack_fixture(tmp_path)
    before_inventory = json.dumps(inventory, sort_keys=True)
    holdouts._capture_bound_inputs(stack, installed=False)
    changed = tmp_path / "backend/_internal/yt_dlp/__init__.py"
    changed.write_bytes(b"changed-code")

    with pytest.raises(holdouts.HoldoutError, match="bound inventory"):
        holdouts._capture_bound_inputs(stack, installed=False)

    assert json.dumps(inventory, sort_keys=True) == before_inventory


@pytest.mark.parametrize(
    ("baseline_replica", "candidate_replica", "message"),
    [
        ("baseline-2", "packet-7", "baseline inventory"),
        ("baseline-1", "packet-other", "candidate inventory"),
    ],
)
def test_replica_bindings_reject_wrong_baseline_or_candidate_replica(
    baseline_replica: str, candidate_replica: str, message: str
) -> None:
    baseline = {"buildProvenance": {"replicaId": baseline_replica}}
    candidate = {"buildProvenance": {"replicaId": candidate_replica}}
    parent = {"buildProvenance": {"replicaId": "baseline-1"}}

    with pytest.raises(holdouts.HoldoutError, match=message):
        holdouts._validate_replica_bindings(
            baseline_inventory=baseline,
            candidate_inventory=candidate,
            parent_inventory=parent,
            packet_id="packet-7",
            parent_id="baseline",
        )


def test_replica_bindings_accept_packet_and_nonbaseline_parent() -> None:
    holdouts._validate_replica_bindings(
        baseline_inventory={"buildProvenance": {"replicaId": "baseline-1"}},
        candidate_inventory={"buildProvenance": {"replicaId": "packet-7"}},
        parent_inventory={"buildProvenance": {"replicaId": "packet-6"}},
        packet_id="packet-7",
        parent_id="packet-6",
    )


def test_replica_bindings_reject_malformed_provenance() -> None:
    with pytest.raises(holdouts.HoldoutError, match="baseline.*provenance"):
        holdouts._validate_replica_bindings(
            baseline_inventory={"buildProvenance": None},
            candidate_inventory={"buildProvenance": {"replicaId": "packet-7"}},
            parent_inventory={"buildProvenance": {"replicaId": "baseline-1"}},
            packet_id="packet-7",
            parent_id="baseline",
        )


def test_write_immutable_publishes_once_without_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"

    holdouts._write_immutable(output, {"status": "pass"})

    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "pass"}
    with pytest.raises(holdouts.HoldoutError, match="already exists"):
        holdouts._write_immutable(output, {"status": "replacement"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "pass"}


def test_write_immutable_loses_publish_race_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "evidence.json"

    def racing_link(
        _source: Path, destination: Path, *, follow_symlinks: bool = True
    ) -> None:
        assert follow_symlinks is False
        Path(destination).write_text("racer", encoding="utf-8")
        raise FileExistsError("synthetic publish race")

    monkeypatch.setattr(holdouts.os, "link", racing_link)

    with pytest.raises(holdouts.HoldoutError, match="appeared concurrently"):
        holdouts._write_immutable(output, {"status": "must-not-overwrite"})

    assert output.read_text(encoding="utf-8") == "racer"
    assert list(tmp_path.glob(".evidence.json.*.tmp")) == []


def test_evidence_redaction_rejects_urls_paths_and_raw_streams() -> None:
    holdouts._assert_redacted({"caseId": "regular-audio", "status": "pass"})

    with pytest.raises(holdouts.HoldoutError, match="forbidden"):
        holdouts._assert_redacted({"stdout": "raw"})
    with pytest.raises(holdouts.HoldoutError, match="unredacted"):
        holdouts._assert_redacted({"origin": "https://www.youtube.com/watch?v=secret"})


def test_validator_invokes_only_inventory_bound_frozen_backend_probe() -> None:
    source = Path(holdouts.__file__).read_text(encoding="utf-8")

    assert "--installer-youtube-holdout-probe" in source
    assert "-m yt_dlp" not in source
    assert "_isolated-yt-dlp" not in source
    assert "standalone yt-dlp CLI" not in source
    assert '"YTDLP_NO_PLUGINS": "1"' in source
    assert '"PYTHONPATH"' in source
