from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scriber_polishing import hf_quantization_pipeline as quantization
from scriber_polishing.hf_llama_cpp_toolchain import (
    CONFIG_DIGEST,
    INDEX_DIGEST,
    MANIFEST_DIGEST,
    REVIEWED_SCRIBER_GIT_HEAD,
    SELECTED_LAYERS,
    TOOLCHAIN_COMMIT,
    TOOLCHAIN_REMOTE_OUTPUT_URI,
    reviewed_toolchain_build_binding,
)
from scriber_polishing.hf_quantization_pipeline import (
    FULL_MODEL_SOURCE_REMOTE_URI,
    FULL_MODEL_SOURCE_TREE_SHA256,
    HfQuantizationError,
    bind_hf_auxiliary_cost_job,
    bind_hf_final_evaluation_cost_attempt,
    bind_llama_cpp_cuda_offload_evidence,
    build_cloud_llama_cpp_binding_from_materialization_result,
    build_hf_actual_cost_evidence,
    build_hf_quantization_budget,
    build_hf_quantization_dry_bind,
    hf_pre_final_evaluation_cost_anchor,
    hf_quantization_runner_payload,
    validate_hf_quantization_plan,
    validate_hf_quantization_result,
    verify_hf_actual_cost_freshness,
    verify_immutable_bf16_baseline,
    verify_llama_cpp_cuda_offload,
    verify_quantization_completed_lane,
)

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_HASH_D = "sha256:" + "d" * 64
_CONTINUATION_RESULT_HASH = "sha256:9e2755ac5411317da0bc70ddfb79716ef9b6704fbf923522f66421c26f343e7d"
_MODEL_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt7/scriber-1100-continuation-v1/training/final-test"
)
_LLAMA_URI = TOOLCHAIN_REMOTE_OUTPUT_URI
_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
_POLICY = {
    "filename": "scriber-protection-policy.json",
    "artifact_sha256": "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
    "policy_id": "gemma_lexical_v1",
    "policy_sha256": "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce",
    "tokenizer_identity_sha256": "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20",
}


def _toolchain_materialization() -> dict[str, object]:
    external_unsigned = {
        "schema_version": 1,
        "kind": "scriber_hf_toolchain_v19_external_provenance",
        "result_sha256": _HASH_D,
        "producer_job_id": "7" * 24,
        "claim_commit": "8" * 40,
        "claim_owner": "9" * 64,
        "remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
    }
    return {
        "schema_version": 1,
        "kind": "scriber_llama_cpp_toolchain_materialization_binding",
        "attempt": "v19",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "packet_tree_sha256": _HASH_B,
        "bundle_payload_tree_sha256": _HASH_C,
        "result_sha256": _HASH_D,
        "flavor": "cpu-basic",
        "timeout_minutes": 20,
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
        "external_provenance": {
            **external_unsigned,
            "binding_sha256": quantization._sha256(
                quantization._canonical_bytes(external_unsigned)
            ),
        },
    }


def _final_model() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_model_binding",
        "start_updates": 846,
        "additional_updates": 254,
        "cumulative_updates": 1100,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "final_model_directory": "final-test",
        "final_model_tree_sha256": _HASH_A,
        "remote_model_uri": _MODEL_URI,
        "mount_mode": "read_only",
        "continuation_result_sha256": _CONTINUATION_RESULT_HASH,
        "training_report_sha256": _HASH_C,
        "preprocessing_policy": {key: _POLICY[key] for key in ("artifact_sha256", "policy_id", "policy_sha256")},
    }


def _suite(record_count: int, split: str) -> dict[str, object]:
    return {
        "records_filename": f"{split}-{record_count}.references.jsonl",
        "manifest_filename": f"{split}-{record_count}.manifest.json",
        "record_count": record_count,
        "split_counts": {split: record_count},
        "records_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "usage": "inference_and_evaluation_only",
    }


def _inputs() -> dict[str, object]:
    suites = {
        "ai_gold": _suite(1750, "test"),
        "critical_regression": _suite(500, "challenge"),
        "regular_test": _suite(1000, "test"),
    }
    reports = {
        name: {
            "record_count": suite["record_count"],
            "evaluation_sha256": "3" * 64,
            "gate_status": "passed",
            "gate_passed": True,
        }
        for name, suite in suites.items()
    }
    final_model = _final_model()
    return {
        "final_evaluation_result": {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_result",
            "status": "complete",
            "training_steps_executed": 0,
            "final_model": deepcopy(final_model),
            "reports": {
                "final": reports,
                "base_model": deepcopy(reports),
            },
            "publication": {
                "allowed": False,
                "destination_kind": "private_hf_bucket",
            },
        },
        "final_evaluation_manifest": {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation",
            "final_model": deepcopy(final_model),
            "references": suites,
            "evaluation": {
                "config_filename": "evaluation.yaml",
                "config_sha256": "4" * 64,
                "candidates": ["final", "base_model"],
                "reports_per_candidate": 3,
            },
        },
        "training_report": {
            "schema_version": 2,
            "run_kind": "improvement",
            "run_fingerprint": _HASH_D,
            "training_completed": True,
            "max_steps": 254,
        },
        "llama_cpp_bundle": {
            "schema_version": 1,
            "kind": "scriber_cloud_llama_cpp_binding",
            "attempt": "v19",
            "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
            "status": "verified",
            "platform": "linux_x86_64_cuda",
            "lock_sha256": _HASH_A,
            "commit": TOOLCHAIN_COMMIT,
            "source_tree_sha256": _HASH_B,
            "build": reviewed_toolchain_build_binding(),
            "build_identity_sha256": _HASH_C,
            "cuda_enabled": True,
            "distribution": {
                "kind": "oci_image_layers",
                "repository": "ghcr.io/ggml-org/llama.cpp",
                "tag": "full-cuda12-b10156",
                "index_digest": INDEX_DIGEST,
                "manifest_digest": MANIFEST_DIGEST,
                "config_digest": CONFIG_DIGEST,
                "revision": TOOLCHAIN_COMMIT,
                "platform": {"os": "linux", "architecture": "amd64"},
                "layers": [
                    {
                        "digest": next(iter(SELECTED_LAYERS)),
                        "compressed_bytes": next(iter(SELECTED_LAYERS.values())),
                        "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "purpose": "native_libraries",
                    },
                    {
                        "digest": list(SELECTED_LAYERS)[1],
                        "compressed_bytes": list(SELECTED_LAYERS.values())[1],
                        "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "purpose": "tools_and_conversion",
                    },
                ],
                "runtime_image": _IMAGE,
            },
            "native_runtime_tree_sha256": _HASH_D,
            "ldd_closure_sha256": "sha256:" + "f" * 64,
            "required_host_driver_sonames": ["libcuda.so.1"],
            "runtime_library_directories": ["runtime/app"],
            "tool_sha256": {
                "convert_hf_to_gguf": _HASH_A,
                "llama_quantize": _HASH_B,
                "llama_cli": _HASH_C,
                "llama_server": _HASH_D,
            },
            "materialization": _toolchain_materialization(),
        },
    }


def _cost_evidence() -> dict[str, object]:
    first_failure = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6ba10023ed89c748ec83f1",
        terminal_status="ERROR",
        flavor="l4x1",
        running_seconds=63,
        packet_tree_sha256=("sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"),
        result_sha256=None,
    )
    second_failure = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6ba85b23ed89c748ec8491",
        terminal_status="ERROR",
        flavor="l4x1",
        running_seconds=45,
        packet_tree_sha256=("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
        result_sha256=None,
    )
    canceled_primary = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6baf33b36a6516e96a3210",
        terminal_status="CANCELED",
        flavor="l4x1",
        timeout_minutes=120,
        running_seconds=None,
        last_pre_terminal_running_seconds=2810,
        packet_tree_sha256=("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
        result_sha256=None,
    )
    failed_amendment = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6bc2edb36a6516e96a3347",
        terminal_status="ERROR",
        flavor="l4x1",
        timeout_minutes=180,
        running_seconds=78,
        packet_tree_sha256=("sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"),
        result_sha256=None,
    )
    completed = bind_hf_final_evaluation_cost_attempt(
        job_id="3" * 24,
        terminal_status="COMPLETED",
        flavor="l4x1",
        timeout_minutes=180,
        running_seconds=10_800,
        packet_tree_sha256=_HASH_C,
        result_sha256=_HASH_A,
    )
    auxiliary_jobs = [
        bind_hf_auxiliary_cost_job(
            job_id=job_id,
            role="packet_mount_inventory",
            terminal_status="COMPLETED",
            flavor="cpu-basic",
            running_seconds=running_seconds,
            packet_tree_sha256=packet_tree_sha256,
            observed_inventory_tree_sha256=observed_inventory_tree_sha256,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
        for job_id, running_seconds, packet_tree_sha256, observed_inventory_tree_sha256 in (
            (
                "6a6ba991b36a6516e96a31bc",
                23,
                "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749",
                "sha256:da912da24b0861de5ebf0ea4a8040f583351966d5af8846a7df049120629113b",
            ),
            (
                "6a6ba9d8b36a6516e96a31be",
                24,
                "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749",
                "sha256:da912da24b0861de5ebf0ea4a8040f583351966d5af8846a7df049120629113b",
            ),
            (
                "6a6bae3cb36a6516e96a320c",
                44,
                "sha256:bc396bdcf45b57d8d12ed9d31a407f9862f96c33eca0f971984ccf2c192ca8ba",
                None,
            ),
        )
    ]
    auxiliary_jobs.append(
        bind_hf_auxiliary_cost_job(
            job_id="7" * 24,
            role="toolchain_materialization",
            terminal_status="COMPLETED",
            flavor="cpu-basic",
            running_seconds=240,
            packet_tree_sha256=_HASH_B,
            observed_inventory_tree_sha256=_HASH_C,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
    )
    return build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=[
            first_failure,
            second_failure,
            canceled_primary,
            failed_amendment,
            completed,
        ],
        auxiliary_jobs=auxiliary_jobs,
        no_other_campaign_jobs_running=True,
    )


def _cost_evidence_hash(evidence: object) -> str:
    payload = (
        json.dumps(
            evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _accounted_job_ids(evidence: dict[str, object]) -> list[str]:
    anchor = evidence["cost_anchor"]
    assert isinstance(anchor, dict)
    chain_jobs = anchor["chain_jobs"]
    assert isinstance(chain_jobs, list)
    return sorted(
        [
            *[str(job["job_id"]) for job in chain_jobs],
            *[
                str(job["job_id"])
                for key in ("final_evaluation_attempts", "auxiliary_jobs")
                for job in evidence[key]
            ],
        ]
    )


def _write_valid_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    marker: str = "locked",
) -> bytes:
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-{version}.dist-info"
    entries = {
        f"{distribution}/__init__.py": f"MARKER = {marker!r}\n".encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: scriber-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
    }
    record_name = f"{dist_info}/RECORD"
    record_rows = []
    for filename, payload in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        record_rows.append([filename, f"sha256={digest}", str(len(payload))])
    record_rows.append([record_name, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(record_rows)
    entries[record_name] = record_buffer.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, payload in entries.items():
            archive.writestr(filename, payload)
    return path.read_bytes()


def _write_fake_quantization_wheelhouse(tmp_path: Path) -> tuple[Path, Path]:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    records: list[dict[str, object]] = []
    for name, version in sorted(quantization.QUANTIZATION_DIRECT_DEPENDENCIES.items()):
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        payload = _write_valid_wheel(
            wheelhouse / filename,
            name=name,
            version=version,
        )
        records.append(
            {
                "name": name,
                "version": version,
                "filename": filename,
                "bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
    lock = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_offline_wheelhouse",
        "wheels": records,
    }
    lock_path = tmp_path / "quantization-wheels.lock.json"
    lock_path.write_text(
        json.dumps(lock, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return wheelhouse, lock_path


def test_quantization_offline_wheelhouse_is_closed_and_hash_bound(
    tmp_path: Path,
) -> None:
    wheelhouse, lock_path = _write_fake_quantization_wheelhouse(tmp_path)

    binding, requirements = quantization._verify_quantization_wheelhouse(
        wheelhouse,
        lock_path,
    )

    assert binding["mode"] == "offline_no_index_require_hashes"
    assert b"transformers==4.57.6 --hash=sha256:" in requirements
    runner = hf_quantization_runner_payload().decode("utf-8")
    assert "--no-index" in runner
    assert "--require-hashes" in runner
    assert "https://" not in runner

    first = next(wheelhouse.iterdir())
    first.write_bytes(first.read_bytes() + b"tampered")
    with pytest.raises(HfQuantizationError, match="differs from lock"):
        quantization._verify_quantization_wheelhouse(wheelhouse, lock_path)


def test_quantization_wheelhouse_rejects_non_wheel_and_record_hash_mismatch(
    tmp_path: Path,
) -> None:
    wheelhouse, lock_path = _write_fake_quantization_wheelhouse(tmp_path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    record = lock["wheels"][0]
    wheel_path = wheelhouse / record["filename"]

    plain_text = b"this is not a wheel archive\n"
    wheel_path.write_bytes(plain_text)
    record["bytes"] = len(plain_text)
    record["sha256"] = "sha256:" + hashlib.sha256(plain_text).hexdigest()
    lock_path.write_bytes(quantization._canonical_bytes(lock))
    with pytest.raises(HfQuantizationError, match="not usable"):
        quantization._verify_quantization_wheelhouse(wheelhouse, lock_path)

    name = str(record["name"])
    version = str(record["version"])
    wheel_payload = _write_valid_wheel(
        wheel_path,
        name=name,
        version=version,
    )
    with zipfile.ZipFile(io.BytesIO(wheel_payload), "r") as source:
        entries = {
            info.filename: source.read(info)
            for info in source.infolist()
        }
    package_name = name.replace("-", "_") + "/__init__.py"
    entries[package_name] = b"MARKER = 'unsafe'\n"
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for filename, payload in entries.items():
            target.writestr(filename, payload)
    wheel_payload = wheel_path.read_bytes()
    record["bytes"] = len(wheel_payload)
    record["sha256"] = "sha256:" + hashlib.sha256(wheel_payload).hexdigest()
    lock_path.write_bytes(quantization._canonical_bytes(lock))
    with pytest.raises(HfQuantizationError, match="RECORD hash differs"):
        quantization._verify_quantization_wheelhouse(wheelhouse, lock_path)


def test_force_reinstall_prevents_same_version_ambient_dependency_bypass(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "environment"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    initial_dir = tmp_path / "initial"
    locked_dir = tmp_path / "locked"
    initial_dir.mkdir()
    locked_dir.mkdir()
    filename = "scriber_ambient_probe-1.0.0-py3-none-any.whl"
    initial_wheel = initial_dir / filename
    locked_wheel = locked_dir / filename
    _write_valid_wheel(
        initial_wheel,
        name="scriber-ambient-probe",
        version="1.0.0",
        marker="ambient",
    )
    locked_payload = _write_valid_wheel(
        locked_wheel,
        name="scriber-ambient-probe",
        version="1.0.0",
        marker="locked",
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--isolated",
            "--no-index",
            "--no-deps",
            str(initial_wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "scriber-ambient-probe==1.0.0 "
        f"--hash=sha256:{hashlib.sha256(locked_payload).hexdigest()}\n",
        encoding="utf-8",
        newline="\n",
    )
    common = [
        str(python),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--no-index",
        "--no-deps",
        "--require-hashes",
        "--find-links",
        str(locked_dir),
        "--requirement",
        str(requirements),
    ]
    subprocess.run(common, check=True, capture_output=True, text=True)
    marker_probe = [
        str(python),
        "-I",
        "-c",
        "import scriber_ambient_probe as p; print(p.MARKER)",
    ]
    unchanged = subprocess.run(
        marker_probe,
        check=True,
        capture_output=True,
        text=True,
    )
    assert unchanged.stdout.strip() == "ambient"

    subprocess.run(
        [*common[0:4], "--force-reinstall", "--ignore-installed", *common[4:]],
        check=True,
        capture_output=True,
        text=True,
    )
    replaced = subprocess.run(
        marker_probe,
        check=True,
        capture_output=True,
        text=True,
    )
    assert replaced.stdout.strip() == "locked"


def test_quantization_bootstrap_executes_one_valid_packet_and_binds_manifest(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    marker = tmp_path / "executed"
    runner = packet / "run-hf-quantization.sh"
    runner.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['BOOTSTRAP_TEST_MARKER']).write_text("
        "os.environ['SCRIBER_TRUSTED_QUANTIZATION_MANIFEST_SHA256'])\n",
        encoding="utf-8",
        newline="\n",
    )
    records, tree = quantization._packet_inventory(packet)
    runner_hash = quantization._sha256(runner.read_bytes())
    manifest = {
        "schema_version": 2,
        "kind": "scriber_hf_quantization_plan",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "image": quantization.QUANTIZATION_IMAGE,
        "flavor": "l4x1",
        "timeout_minutes": 285,
        "packet_tree_sha256": tree,
        "runner_sha256": runner_hash,
        "file_count": len(records),
        "byte_count": sum(int(record["bytes"]) for record in records),
    }
    manifest_path = packet / "job-manifest.json"
    manifest_path.write_bytes(quantization._canonical_bytes(manifest))
    manifest_hash = quantization._sha256(manifest_path.read_bytes())
    environment = os.environ.copy()
    environment["BOOTSTRAP_TEST_MARKER"] = str(marker)
    command = [
        sys.executable,
        "-I",
        "-c",
        quantization.hf_quantization_bootstrap_payload(
            packet,
            runner_executable=sys._base_executable,
        ),
        tree,
        runner_hash,
        manifest_hash,
        "a" * 64,
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == manifest_hash

    marker.unlink()
    manifest["timeout_minutes"] = 284
    manifest_path.write_bytes(quantization._canonical_bytes(manifest))
    rejected = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode != 0
    assert "manifest hash differs" in rejected.stderr
    assert not marker.exists()


def test_quantization_inventory_and_bootstrap_reject_directory_symlinks(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    runner = packet / "run-hf-quantization.sh"
    runner.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    records, tree = quantization._packet_inventory(packet)
    runner_hash = quantization._sha256(runner.read_bytes())
    manifest = {
        "schema_version": 2,
        "kind": "scriber_hf_quantization_plan",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "image": quantization.QUANTIZATION_IMAGE,
        "flavor": "l4x1",
        "timeout_minutes": 285,
        "packet_tree_sha256": tree,
        "runner_sha256": runner_hash,
        "file_count": len(records),
        "byte_count": sum(int(record["bytes"]) for record in records),
    }
    manifest_path = packet / "job-manifest.json"
    manifest_path.write_bytes(quantization._canonical_bytes(manifest))
    external = tmp_path / "external"
    external.mkdir()
    (external / "payload").write_text("outside\n", encoding="utf-8")
    link = packet / "linked-directory"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(HfQuantizationError, match="non-regular"):
        quantization._packet_inventory(packet)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            quantization.hf_quantization_bootstrap_payload(
                packet,
                runner_executable=sys.executable,
            ),
            tree,
            runner_hash,
            quantization._sha256(manifest_path.read_bytes()),
            "a" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()


def _offload_evidence(
    runtime_instance_id: str = _HASH_A,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_llama_cpp_cuda_offload_evidence",
        "status": "verified",
        "device": "cuda",
        "backend": "llama_cpp_cuda",
        "runtime_instance_id": runtime_instance_id,
        "requested_gpu_layers": 999,
        "cuda_device_count": 1,
        "gpu_name": "NVIDIA L4",
        "compute_capability": "8.9",
        "offloaded_layers": 19,
        "total_layers": 19,
        "stderr_sha256": _HASH_D,
    }


def _plan() -> dict[str, object]:
    inputs = _inputs()
    cost_evidence = _cost_evidence()
    return build_hf_quantization_dry_bind(
        final_evaluation_result=inputs["final_evaluation_result"],
        final_evaluation_result_sha256=_HASH_A,
        final_evaluation_manifest=inputs["final_evaluation_manifest"],
        final_evaluation_manifest_sha256=_HASH_B,
        training_report=inputs["training_report"],
        training_report_sha256=_HASH_C,
        remote_model_uri=_MODEL_URI,
        llama_cpp_bundle=inputs["llama_cpp_bundle"],
        remote_llama_cpp_uri=_LLAMA_URI,
        source_git_head=REVIEWED_SCRIBER_GIT_HEAD,
        actual_cost_evidence=cost_evidence,
        actual_cost_evidence_sha256=_cost_evidence_hash(cost_evidence),
    )


def _full_model_plan(
    tmp_path: Path,
    *,
    toolchain_flavor: str = "cpu-basic",
    model_weight_payload: bytes | None = None,
) -> dict[str, object]:
    inputs = _inputs()
    cost_evidence = _cost_evidence()
    inputs["llama_cpp_bundle"]["materialization"]["flavor"] = toolchain_flavor
    for job in cost_evidence["auxiliary_jobs"]:
        if (
            job["role"] == "toolchain_materialization"
            and job["terminal_status"] == "COMPLETED"
        ):
            job["flavor"] = toolchain_flavor
            if toolchain_flavor == "a10g-small":
                job["hourly_rate_usd"] = "1.000"
                job["actual_cost_usd"] = "0.066667"
    reference_specs = {
        "ai_gold_automatic_test_challenge": (1750, {"challenge": 750, "test": 1000}),
        "critical_regression_as_challenge": (500, {"challenge": 500}),
        "regular_test_1000": (1000, {"test": 1000}),
    }
    for stem, (count, splits) in reference_specs.items():
        records_name = f"{stem}.references.jsonl"
        manifest_name = f"{stem}.references.manifest.json"
        records = b"{}\n" * count
        (tmp_path / records_name).write_bytes(records)
        (tmp_path / manifest_name).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "output_filename": records_name,
                    "record_count": count,
                    "records_sha256": hashlib.sha256(records).hexdigest(),
                    "split_counts": splits,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    model_uri = FULL_MODEL_SOURCE_REMOTE_URI
    reports = [
        {"bytes": 100, "path": f"{candidate}/{suite}/evaluation.json", "sha256": _HASH_D}
        for candidate in ("base_model", "final")
        for suite in ("ai_gold", "critical_regression", "regular_test")
    ]
    result = {
        "schema_version": 1,
        "kind": "scriber_full_model_final_evaluation_result",
        "model_tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256,
        "reports": reports,
        "training_steps_executed": 0,
    }
    model_weight_sha256 = (
        "sha256:" + hashlib.sha256(model_weight_payload).hexdigest()
        if model_weight_payload is not None
        else _HASH_B
    )
    model_weight_bytes = (
        len(model_weight_payload)
        if model_weight_payload is not None
        else 4_194_304_000
    )
    manifest = {
        "schema_version": 1,
        "kind": "scriber_full_model_final_evaluation_job",
        "final_model": {
            "remote_uri": model_uri,
            "run_fingerprint": _HASH_D,
            "tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256,
            "weight_sha256": model_weight_sha256,
        },
        "references": {
            "ai_gold_records": 1750,
            "critical_regression_records": 500,
            "regular_test_records": 1000,
            "usage": "inference_and_evaluation_only",
        },
        "training": {"new_data_generated": False, "steps_executed": 0},
        "visibility": "private",
    }
    training = {
        "schema_version": 1,
        "kind": "scriber_hf_full_training_completion",
        "training_completed": True,
        "run_fingerprint": _HASH_D,
        "final_artifact": {
            "remote_uri": model_uri,
            "tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256,
            "model_safetensors_sha256": model_weight_sha256,
            "reload_status": "passed",
        },
        "resume": {
            "checkpoint": "checkpoint-1000",
            "same_run": True,
            "repeated_steps_0_through_1000": False,
        },
        "early_stopping": {"global_step": 3500},
        "job": {"status": "COMPLETED"},
    }
    return build_hf_quantization_dry_bind(
        final_evaluation_result=result,
        final_evaluation_result_sha256=_HASH_A,
        final_evaluation_result_bytes=1097,
        final_evaluation_manifest=manifest,
        final_evaluation_manifest_sha256=_HASH_B,
        final_evaluation_manifest_bytes=2048,
        training_report=training,
        training_report_sha256=_HASH_C,
        remote_model_uri=model_uri,
        model_weight_bytes=model_weight_bytes,
        llama_cpp_bundle=inputs["llama_cpp_bundle"],
        remote_llama_cpp_uri=_LLAMA_URI,
        source_git_head=REVIEWED_SCRIBER_GIT_HEAD,
        actual_cost_evidence=cost_evidence,
        actual_cost_evidence_sha256=_cost_evidence_hash(cost_evidence),
        reference_root=tmp_path,
        evaluation_config_sha256="4" * 64,
    )


def test_full_model_plan_binds_verified_resume_without_claiming_release(
    tmp_path: Path,
) -> None:
    plan = _full_model_plan(tmp_path)

    assert plan["schema_version"] == 2
    assert plan["source_model"]["training_regime"] == "full_120k_sft"
    assert plan["source_model"]["completed_updates"] == 3500
    assert plan["source_model"]["resume_checkpoint"] == "checkpoint-1000"
    assert plan["source_model"]["replayed_updates"] == 0
    assert plan["verified_final_evaluation"] == {
        "result_sha256": _HASH_A,
        "result_bytes": 1097,
        "manifest_sha256": _HASH_B,
        "manifest_bytes": 2048,
        "required_suites": ["ai_gold", "critical_regression", "regular_test"],
        "training_steps_executed": 0,
        "model_tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256,
        "release_eligibility": "not_granted",
        "quantization_scope": "private_candidate_comparison_only",
    }
    assert "successful_final_evaluation" not in plan
    assert plan["evaluation"]["absolute_gate_policy"] == "record_only_private_candidate"
    assert plan["budget"]["remaining_credit_authorization_usd"] == "5.500"
    assert plan["budget"]["quantization_maximum_cost_usd"] == "3.800"
    assert plan["budget"]["post_authorization_actual_spend_usd"] == "1.480332"
    assert plan["budget"]["maximum_remaining_credit_scope_cost_usd"] == "5.280332"
    assert plan["budget"]["remaining_credit_after_quantization_reservation_usd"] == "0.219668"
    assert plan["output"]["durable_resume_allowed"] is False
    assert plan["output"]["duplicate_launch_allowed"] is False
    assert plan["output"]["completed_output_never_overwritten"] is True
    assert plan["immutable_bf16_baseline"] == {
        "schema_version": 1,
        "kind": "scriber_immutable_bf16_evaluation_baseline",
        "remote_uri": (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            "attempt12/scriber-full-120k-v2-final-evaluation-v1/final"
        ),
        "mount_mode": "read_only",
        "source_model_tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256,
        "final_evaluation_result_sha256": _HASH_A,
        "final_evaluation_result_bytes": 1097,
        "final_evaluation_manifest_sha256": _HASH_B,
        "final_evaluation_manifest_bytes": 2048,
        "model_weight_sha256": _HASH_B,
        "model_weight_bytes": 4_194_304_000,
        "mount_root_layout": "final_candidate_suite_roots",
        "training_steps_executed": 0,
        "reuse_policy": "evidence_only_no_materialization_inference_or_evaluation",
        "suites": {
            suite: {
                "source_report_path": f"final/{suite}/evaluation.json",
                "relative_path": f"{suite}/evaluation.json",
                "bytes": 100,
                "sha256": _HASH_D,
            }
            for suite in ("ai_gold", "critical_regression", "regular_test")
        },
    }
    assert plan["variants"][0] == {
        "id": "bf16",
        "backend": "verified_final_evaluation_evidence",
        "evaluation": "immutable_existing_baseline",
    }
    assert plan["evaluation"]["bf16_execution_prohibited"] == [
        "materialization",
        "inference",
        "evaluation",
    ]
    assert validate_hf_quantization_plan(plan) == plan

    result = _result(plan)
    result["variants"]["int8"]["evaluation"]["ai_gold"]["gate_passed"] = False
    assert validate_hf_quantization_result(result, plan=plan) == result


def test_full_model_plan_rejects_non_v19_cpu_materialization(
    tmp_path: Path,
) -> None:
    with pytest.raises(HfQuantizationError, match="materialization binding is invalid"):
        _full_model_plan(tmp_path, toolchain_flavor="a10g-small")


def test_full_model_plan_and_mount_bind_exact_uri_tree_weight_hash_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weight_payload = b"exact immutable model weight"
    plan = _full_model_plan(
        tmp_path,
        model_weight_payload=weight_payload,
    )
    manifest_path = tmp_path / "plan.json"
    manifest_path.write_bytes(quantization._canonical_bytes(plan))
    model_root = tmp_path / "model"
    model_root.mkdir()
    (model_root / quantization.FULL_MODEL_WEIGHT_FILENAME).write_bytes(weight_payload)
    (model_root / "reload-verification.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "run_fingerprint": plan["source_model"]["training_fingerprint"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(
        "scriber_polishing.inference._inventory_local_model_tree",
        lambda _root: {"tree_sha256": FULL_MODEL_SOURCE_TREE_SHA256},
    )
    monkeypatch.setattr(
        quantization,
        "verify_frozen_lexical_policy_artifact",
        lambda _root: deepcopy(_POLICY),
    )

    report = quantization.verify_quantization_source_model(
        manifest_path=manifest_path,
        model_root=model_root,
        output_path=tmp_path / "source-preflight.json",
    )
    assert report["model_weight_bytes"] == len(weight_payload)
    assert report["model_weight_sha256"] == (
        "sha256:" + hashlib.sha256(weight_payload).hexdigest()
    )

    (model_root / quantization.FULL_MODEL_WEIGHT_FILENAME).write_bytes(
        weight_payload + b"tampered"
    )
    with pytest.raises(HfQuantizationError, match="weight differs"):
        quantization.verify_quantization_source_model(
            manifest_path=manifest_path,
            model_root=model_root,
            output_path=tmp_path / "tampered-preflight.json",
        )

    wrong_uri = deepcopy(plan)
    wrong_uri["source_model"]["remote_uri"] = (
        FULL_MODEL_SOURCE_REMOTE_URI + "-alias"
    )
    with pytest.raises(HfQuantizationError, match="source-model binding is invalid"):
        validate_hf_quantization_plan(wrong_uri)


def test_budget_reserves_one_bounded_l4_job_strictly_below_authorized_total() -> None:
    evidence = _cost_evidence()
    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_A,
        expected_toolchain_materialization=_toolchain_materialization(),
    )

    accounted_ids = _accounted_job_ids(evidence)
    assert budget == {
        "total_budget_usd": "13.500",
        "total_budget_authorization_sha256": (
            "sha256:ea566717d68b8372a4c5f39c6279e24efa36f906c1dfe06c048339bd6ae86c0d"
        ),
        "actual_cost_evidence_sha256": _cost_evidence_hash(evidence),
        "actual_cost_as_of_utc": "2026-07-30T14:00:00Z",
        "accounted_campaign_job_ids": accounted_ids,
        "accounted_campaign_job_ids_sha256": _cost_evidence_hash(accounted_ids),
        "cost_anchor_sha256": _cost_evidence()["cost_anchor_sha256"],
        "actual_before_first_final_evaluation_usd": "0.893167",
        "final_evaluation_attempt_count": 5,
        "final_evaluation_error_count": 3,
        "final_evaluation_canceled_count": 1,
        "final_evaluation_completed_count": 1,
        "final_evaluation_observed_billed_minutes": 185,
        "final_evaluation_observed_actual_cost_usd": "2.466667",
        "final_evaluation_unknown_cost_reservation_usd": "1.600000",
        "final_evaluation_accounted_cost_usd": "4.066667",
        "auxiliary_job_count": 4,
        "auxiliary_billed_minutes": 7,
        "auxiliary_actual_cost_usd": "0.001168",
        "toolchain_materialization_attempt_count": 1,
        "toolchain_materialization_error_count": 0,
        "toolchain_materialization_completed_job_id": "7" * 24,
        "toolchain_materialization_packet_tree_sha256": _HASH_B,
        "toolchain_materialization_bundle_tree_sha256": _HASH_C,
        "accounted_before_quantization_usd": "4.961002",
        "quantization_flavor": "l4x1",
        "quantization_hourly_rate_usd": "0.800",
        "quantization_timeout_minutes": 285,
        "quantization_maximum_cost_usd": "3.800",
        "maximum_total_cost_usd": "8.761002",
        "remaining_after_reservations_usd": "4.738998",
        "strictly_below_total_budget": True,
    }

    duplicated_job = deepcopy(evidence)
    duplicated_job["final_evaluation_attempts"].append(deepcopy(duplicated_job["final_evaluation_attempts"][1]))
    with pytest.raises(HfQuantizationError, match="job IDs"):
        build_hf_quantization_budget(
            duplicated_job,
            evidence_sha256=_cost_evidence_hash(duplicated_job),
            expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
            expected_final_evaluation_result_sha256=_HASH_A,
            expected_toolchain_materialization=_toolchain_materialization(),
        )


def test_budget_selects_latest_completed_evaluation_without_erasing_prior_cost() -> None:
    evidence = _cost_evidence()
    prior = deepcopy(evidence["final_evaluation_attempts"][-1])
    prior["job_id"] = "8" * 24
    prior["result_sha256"] = _HASH_D
    evidence["final_evaluation_attempts"].insert(-1, prior)
    evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=evidence["final_evaluation_attempts"],
        auxiliary_jobs=evidence["auxiliary_jobs"],
        no_other_campaign_jobs_running=True,
    )

    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_A,
        expected_toolchain_materialization=_toolchain_materialization(),
    )

    assert budget["final_evaluation_completed_count"] == 2
    assert Decimal(budget["maximum_total_cost_usd"]) > Decimal("8.761002")


def test_full_model_evaluation_cost_supports_l40s_and_unstarted_attempts() -> None:
    running = bind_hf_final_evaluation_cost_attempt(
        job_id="9" * 24,
        terminal_status="ERROR",
        flavor="l40sx1",
        running_seconds=46,
        packet_tree_sha256=_HASH_D,
        result_sha256=None,
    )
    unstarted = bind_hf_final_evaluation_cost_attempt(
        job_id="0" * 24,
        terminal_status="CANCELED",
        flavor="l40sx1",
        running_seconds=0,
        packet_tree_sha256=_HASH_D,
        result_sha256=None,
    )

    assert running["billed_minutes"] == 1
    assert running["hourly_rate_usd"] == "1.800"
    assert running["actual_cost_usd"] == "0.030000"
    assert unstarted["billed_minutes"] == 0
    assert unstarted["actual_cost_usd"] == "0.000000"


def test_full_training_cost_is_counted_without_relabeling_it_as_evaluation() -> None:
    canceled = bind_hf_auxiliary_cost_job(
        job_id="1" * 24,
        role="full_training",
        terminal_status="CANCELED",
        flavor="l40sx1",
        running_seconds=3600,
        packet_tree_sha256=_HASH_A,
        observed_inventory_tree_sha256=None,
        training_steps_executed=1450,
        evaluation_reports_written=0,
        prediction_records_written=0,
    )
    completed = bind_hf_auxiliary_cost_job(
        job_id="2" * 24,
        role="full_training",
        terminal_status="COMPLETED",
        flavor="l40sx1",
        running_seconds=5539,
        packet_tree_sha256=_HASH_A,
        observed_inventory_tree_sha256=_HASH_B,
        training_steps_executed=2500,
        evaluation_reports_written=0,
        prediction_records_written=0,
    )

    assert canceled["actual_cost_usd"] == "1.800000"
    assert completed["billed_minutes"] == 93
    assert completed["actual_cost_usd"] == "2.790000"


def test_full_model_budget_uses_reported_remaining_credits_after_historical_spend() -> None:
    evidence = _cost_evidence()
    evidence["final_evaluation_attempts"].extend(
        [
            bind_hf_final_evaluation_cost_attempt(
                job_id="9" * 24,
                terminal_status="ERROR",
                flavor="l40sx1",
                running_seconds=46,
                packet_tree_sha256=_HASH_D,
                result_sha256=None,
            ),
            bind_hf_final_evaluation_cost_attempt(
                job_id="0" * 24,
                terminal_status="COMPLETED",
                flavor="l4x1",
                running_seconds=5791,
                packet_tree_sha256=_HASH_D,
                result_sha256=_HASH_D,
            ),
        ]
    )
    evidence["auxiliary_jobs"].extend(
        [
            bind_hf_auxiliary_cost_job(
                job_id="1" * 24,
                role="full_training",
                terminal_status="CANCELED",
                flavor="l40sx1",
                running_seconds=3600,
                packet_tree_sha256=_HASH_A,
                observed_inventory_tree_sha256=None,
                training_steps_executed=1450,
                evaluation_reports_written=0,
                prediction_records_written=0,
            ),
            bind_hf_auxiliary_cost_job(
                job_id="2" * 24,
                role="full_training",
                terminal_status="COMPLETED",
                flavor="l40sx1",
                running_seconds=5539,
                packet_tree_sha256=_HASH_A,
                observed_inventory_tree_sha256=_HASH_B,
                training_steps_executed=2500,
                evaluation_reports_written=0,
                prediction_records_written=0,
            ),
        ]
    )
    evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=evidence["final_evaluation_attempts"],
        auxiliary_jobs=evidence["auxiliary_jobs"],
        no_other_campaign_jobs_running=True,
    )
    schema_path = Path(__file__).parents[1] / "contracts/hf_actual_cost_evidence_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(evidence)) == []

    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_D,
        expected_toolchain_materialization=_toolchain_materialization(),
        use_remaining_credit_authorization=True,
    )

    assert Decimal(budget["maximum_total_cost_usd"]) > Decimal(budget["total_budget_usd"])
    assert budget["strictly_below_total_budget"] is False
    assert budget["remaining_credit_authorization_usd"] == "5.500"
    assert budget["post_authorization_actual_spend_usd"] == "1.480332"
    assert budget["maximum_remaining_credit_scope_cost_usd"] == "5.280332"
    assert budget["remaining_credit_after_quantization_reservation_usd"] == "0.219668"
    assert budget["strictly_below_remaining_credit_authorization"] is True


def test_cuda_library_probe_is_costed_as_a_bounded_zero_side_effect_job() -> None:
    probe = bind_hf_auxiliary_cost_job(
        job_id="a" * 24,
        role="toolchain_cuda_library_probe",
        terminal_status="COMPLETED",
        flavor="cpu-basic",
        running_seconds=0,
        packet_tree_sha256=_HASH_A,
        observed_inventory_tree_sha256=None,
        training_steps_executed=0,
        evaluation_reports_written=0,
        prediction_records_written=0,
    )

    assert probe["billed_minutes"] == 0
    assert probe["actual_cost_usd"] == "0.000000"

    evidence = _cost_evidence()
    evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=evidence["final_evaluation_attempts"],
        auxiliary_jobs=[*evidence["auxiliary_jobs"], probe],
        no_other_campaign_jobs_running=True,
    )
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/hf_actual_cost_evidence_schema.json"
        ).read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(schema).iter_errors(evidence)) == []
    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_A,
        expected_toolchain_materialization=_toolchain_materialization(),
        use_remaining_credit_authorization=True,
    )
    assert budget["post_authorization_actual_spend_usd"] == "1.480332"
    assert budget["maximum_remaining_credit_scope_cost_usd"] == "5.280332"


def test_cloud_toolchain_result_binds_without_downloading_bundle_payload(
    tmp_path: Path,
) -> None:
    source = _inputs()["llama_cpp_bundle"]
    result = {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_result",
        "attempt": "v19",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "complete",
        "platform": "linux_x86_64_cuda",
        "image": _IMAGE,
        "flavor": "cpu-basic",
        "timeout_minutes": 20,
        "packet_tree_sha256": _HASH_B,
        "bundle_payload_inventory": {
            "algorithm": "sha256-canonical-path-kind-size-content-or-target-v1",
            "tree_sha256": _HASH_C,
            "entry_count": 12,
            "total_file_bytes": 1234,
        },
        "lock_filename": "llama-cpp-bundle-lock.json",
        "lock_sha256": source["lock_sha256"],
        "commit": source["commit"],
        "source_tree_sha256": source["source_tree_sha256"],
        "build_identity_sha256": source["build_identity_sha256"],
        "build": reviewed_toolchain_build_binding(),
        "distribution": source["distribution"],
        "native_runtime_tree_sha256": source["native_runtime_tree_sha256"],
        "ldd_closure_sha256": source["ldd_closure_sha256"],
        "required_host_driver_sonames": ["libcuda.so.1"],
        "runtime_library_directories": source["runtime_library_directories"],
        "tool_sha256": source["tool_sha256"],
        "runtime_closure_verified": True,
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
        "publication_allowed": False,
    }
    payload = (
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    path = tmp_path / "toolchain-materialization-result.json"
    path.write_bytes(payload)
    result_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()

    binding = build_cloud_llama_cpp_binding_from_materialization_result(
        path,
        remote_uri=_LLAMA_URI,
        expected_result_sha256=result_sha256,
        expected_job_id="7" * 24,
        expected_claim_commit="8" * 40,
        expected_claim_owner="9" * 64,
    )

    assert binding["tool_sha256"] == source["tool_sha256"]
    assert binding["required_host_driver_sonames"] == ["libcuda.so.1"]
    assert binding["materialization"] == {
        "schema_version": 1,
        "kind": "scriber_llama_cpp_toolchain_materialization_binding",
        "attempt": "v19",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "packet_tree_sha256": _HASH_B,
        "bundle_payload_tree_sha256": _HASH_C,
        "result_sha256": result_sha256,
        "flavor": "cpu-basic",
        "timeout_minutes": 20,
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
        "external_provenance": {
            "schema_version": 1,
            "kind": "scriber_hf_toolchain_v19_external_provenance",
            "result_sha256": result_sha256,
            "producer_job_id": "7" * 24,
            "claim_commit": "8" * 40,
            "claim_owner": "9" * 64,
            "remote_uri": _LLAMA_URI,
            "binding_sha256": quantization._sha256(
                quantization._canonical_bytes(
                    {
                        "schema_version": 1,
                        "kind": "scriber_hf_toolchain_v19_external_provenance",
                        "result_sha256": result_sha256,
                        "producer_job_id": "7" * 24,
                        "claim_commit": "8" * 40,
                        "claim_owner": "9" * 64,
                        "remote_uri": _LLAMA_URI,
                    }
                )
            ),
        },
    }

    for changed in (
        {"expected_result_sha256": _HASH_A},
        {"expected_job_id": "not-a-job"},
        {"expected_claim_commit": "not-a-commit"},
        {"expected_claim_owner": "not-an-owner"},
    ):
        with pytest.raises(HfQuantizationError, match="provenance"):
            build_cloud_llama_cpp_binding_from_materialization_result(
                path,
                remote_uri=_LLAMA_URI,
                expected_result_sha256=changed.get(
                    "expected_result_sha256",
                    result_sha256,
                ),
                expected_job_id=changed.get("expected_job_id", "7" * 24),
                expected_claim_commit=changed.get(
                    "expected_claim_commit",
                    "8" * 40,
                ),
                expected_claim_owner=changed.get(
                    "expected_claim_owner",
                    "9" * 64,
                ),
            )

    changed_binding = build_cloud_llama_cpp_binding_from_materialization_result(
        path,
        remote_uri=_LLAMA_URI,
        expected_result_sha256=result_sha256,
        expected_job_id="6" * 24,
        expected_claim_commit="8" * 40,
        expected_claim_owner="9" * 64,
    )
    with pytest.raises(HfQuantizationError, match="externally bound"):
        evidence = _cost_evidence()
        build_hf_quantization_budget(
            evidence,
            evidence_sha256=_cost_evidence_hash(evidence),
            expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
            expected_final_evaluation_result_sha256=_HASH_A,
            expected_toolchain_materialization=changed_binding["materialization"],
            use_remaining_credit_authorization=True,
        )


def test_cost_anchor_binds_real_a10g_recovery_and_cpu_finalization() -> None:
    anchor = hf_pre_final_evaluation_cost_anchor()

    assert anchor["actual_before_first_final_evaluation_usd"] == "0.893167"
    assert anchor["chain_jobs"] == [
        {
            "role": "training_continuation",
            "job_id": "6a6b8a78b36a6516e96a2fb1",
            "owner": "Buttermilk03",
            "terminal_status": "ERROR",
            "flavor": "a10g-small",
            "running_seconds": 912,
            "billed_minutes": 16,
            "hourly_rate_usd": "1.00",
            "actual_cost_usd": "0.266667",
            "packet_tree_sha256": ("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
        },
        {
            "role": "continuation_recovery",
            "job_id": "6a6b94e6b36a6516e96a307c",
            "owner": "Buttermilk03",
            "terminal_status": "ERROR",
            "flavor": "a10g-small",
            "running_seconds": 93,
            "billed_minutes": 2,
            "hourly_rate_usd": "1.00",
            "actual_cost_usd": "0.033333",
            "packet_tree_sha256": ("sha256:8f37fcfdf1ee7b542c2f078eee7dd57b2f9ea5a2573d265890d5c0d7d6ae31ea"),
        },
        {
            "role": "continuation_finalization",
            "job_id": "6a6b9ee723ed89c748ec83a1",
            "owner": "Buttermilk03",
            "terminal_status": "COMPLETED",
            "flavor": "cpu-basic",
            "running_seconds": 20,
            "billed_minutes": 1,
            "hourly_rate_usd": "0.01",
            "actual_cost_usd": "0.000167",
            "packet_tree_sha256": ("sha256:2db2a308dcfe5807209100fd823453619428435dbeff5b4679d5dc25dabd80a5"),
        },
    ]


def test_known_cost_before_primary_evaluation_is_exactly_0_933668() -> None:
    evidence = _cost_evidence()
    known_cost = Decimal(str(evidence["cost_anchor"]["actual_before_first_final_evaluation_usd"]))
    known_cost += sum(
        (Decimal(str(attempt["actual_cost_usd"])) for attempt in evidence["final_evaluation_attempts"][:2]),
        Decimal("0"),
    )
    known_cost += sum(
        (
            Decimal(str(job["actual_cost_usd"]))
            for job in evidence["auxiliary_jobs"]
            if job["role"] != "toolchain_materialization"
        ),
        Decimal("0"),
    )

    assert format(known_cost, ".6f") == "0.933668"


def test_cost_evidence_schema_accepts_arbitrary_terminal_attempt_sequence() -> None:
    schema_path = Path(__file__).parents[1] / "contracts/hf_actual_cost_evidence_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)

    errors = list(Draft202012Validator(schema).iter_errors(_cost_evidence()))

    assert errors == []


def test_cost_evidence_rejects_active_jobs_and_fictitious_continuation_role() -> None:
    with pytest.raises(HfQuantizationError, match="terminal ERROR/COMPLETED"):
        bind_hf_final_evaluation_cost_attempt(
            job_id="4" * 24,
            terminal_status="RUNNING",
            flavor="l4x1",
            running_seconds=63,
            packet_tree_sha256=_HASH_B,
            result_sha256=None,
        )
    with pytest.raises(HfQuantizationError, match="must remain ERROR"):
        bind_hf_final_evaluation_cost_attempt(
            job_id="6a6ba10023ed89c748ec83f1",
            terminal_status="COMPLETED",
            flavor="l4x1",
            running_seconds=63,
            packet_tree_sha256=("sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"),
            result_sha256=_HASH_A,
        )

    fictitious = _cost_evidence()
    fictitious["final_evaluation_attempts"][0]["role"] = "continuation"
    with pytest.raises(HfQuantizationError, match="not canonical"):
        build_hf_quantization_budget(
            fictitious,
            evidence_sha256=_cost_evidence_hash(fictitious),
            expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
            expected_final_evaluation_result_sha256=_HASH_A,
            expected_toolchain_materialization=_toolchain_materialization(),
        )


def test_primary_cancellation_has_unknown_actual_cost_and_full_timeout_reservation() -> None:
    canceled = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6baf33b36a6516e96a3210",
        terminal_status="CANCELED",
        flavor="l4x1",
        timeout_minutes=120,
        running_seconds=None,
        last_pre_terminal_running_seconds=2810,
        packet_tree_sha256=("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
        result_sha256=None,
    )

    assert canceled == {
        "role": "final_evaluation",
        "job_id": "6a6baf33b36a6516e96a3210",
        "owner": "Buttermilk03",
        "terminal_status": "CANCELED",
        "flavor": "l4x1",
        "timeout_minutes": 120,
        "terminal_duration_available": False,
        "running_seconds": None,
        "billed_minutes": None,
        "hourly_rate_usd": "0.800",
        "actual_cost_usd": None,
        "cost_basis": "full_timeout_reservation",
        "reserved_minutes": 120,
        "reserved_cost_usd": "1.600000",
        "accounted_cost_usd": "1.600000",
        "last_pre_terminal_running_seconds": 2810,
        "packet_tree_sha256": ("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
        "result_sha256": None,
    }
    for changed in (
        {"terminal_status": "CANCELLED"},
        {"running_seconds": 2810},
        {"last_pre_terminal_running_seconds": None},
        {"job_id": "8" * 24},
    ):
        arguments = {
            "job_id": "6a6baf33b36a6516e96a3210",
            "terminal_status": "CANCELED",
            "flavor": "l4x1",
            "timeout_minutes": 120,
            "running_seconds": None,
            "last_pre_terminal_running_seconds": 2810,
            "packet_tree_sha256": ("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
            "result_sha256": None,
            **changed,
        }
        with pytest.raises(HfQuantizationError):
            bind_hf_final_evaluation_cost_attempt(**arguments)


def test_failed_amendment_uses_observed_78_seconds_and_two_billed_minutes() -> None:
    failed = bind_hf_final_evaluation_cost_attempt(
        job_id="6a6bc2edb36a6516e96a3347",
        terminal_status="ERROR",
        flavor="l4x1",
        timeout_minutes=180,
        running_seconds=78,
        packet_tree_sha256=("sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"),
        result_sha256=None,
    )

    assert failed["terminal_duration_available"] is True
    assert failed["billed_minutes"] == 2
    assert failed["actual_cost_usd"] == "0.026667"
    assert failed["reserved_cost_usd"] == "0.000000"
    assert failed["accounted_cost_usd"] == "0.026667"


def test_quantization_rejects_unfinished_or_mismatched_toolchain_materialization() -> None:
    evidence = _cost_evidence()
    evidence["auxiliary_jobs"].append(
        bind_hf_auxiliary_cost_job(
            job_id="8" * 24,
            role="toolchain_materialization",
            terminal_status="ERROR",
            flavor="l4x1",
            running_seconds=1,
            packet_tree_sha256=_HASH_D,
            observed_inventory_tree_sha256=None,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
    )
    with pytest.raises(HfQuantizationError, match="final completed toolchain"):
        build_hf_quantization_budget(
            evidence,
            evidence_sha256=_cost_evidence_hash(evidence),
            expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
            expected_final_evaluation_result_sha256=_HASH_A,
            expected_toolchain_materialization=_toolchain_materialization(),
        )

    mismatched = _toolchain_materialization()
    mismatched["bundle_payload_tree_sha256"] = _HASH_D
    with pytest.raises(HfQuantizationError, match="exact packet and bundle inventory"):
        build_hf_quantization_budget(
            _cost_evidence(),
            evidence_sha256=_cost_evidence_hash(_cost_evidence()),
            expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
            expected_final_evaluation_result_sha256=_HASH_A,
            expected_toolchain_materialization=mismatched,
        )


def test_canceled_cpu_toolchain_runtime_is_billed_from_observed_minutes() -> None:
    canceled = bind_hf_auxiliary_cost_job(
        job_id="6a6d27c9a00abefd4b28a534",
        role="toolchain_materialization",
        terminal_status="CANCELED",
        flavor="cpu-basic",
        running_seconds=564,
        packet_tree_sha256=(
            "sha256:48573b95e2b535f2e4bdb5ed9bf88555053aea938a52c0a0c6eab3f827016037"
        ),
        observed_inventory_tree_sha256=None,
        training_steps_executed=0,
        evaluation_reports_written=0,
        prediction_records_written=0,
    )

    assert canceled["billed_minutes"] == 10
    assert canceled["actual_cost_usd"] == "0.001667"


def test_budget_accounts_for_an_arbitrary_number_of_unique_terminal_attempts() -> None:
    evidence = _cost_evidence()
    second_failure = bind_hf_final_evaluation_cost_attempt(
        job_id="5" * 24,
        terminal_status="ERROR",
        flavor="l4x1",
        running_seconds=1,
        packet_tree_sha256=_HASH_D,
        result_sha256=None,
    )
    evidence["final_evaluation_attempts"].insert(4, second_failure)

    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_A,
        expected_toolchain_materialization=_toolchain_materialization(),
    )

    assert budget["final_evaluation_attempt_count"] == 6
    assert budget["final_evaluation_error_count"] == 4
    assert budget["final_evaluation_observed_billed_minutes"] == 186
    assert budget["final_evaluation_observed_actual_cost_usd"] == "2.480000"
    assert budget["final_evaluation_unknown_cost_reservation_usd"] == "1.600000"
    assert budget["accounted_before_quantization_usd"] == "4.974335"


def test_budget_includes_future_cross_platform_self_verify_cpu_job() -> None:
    evidence = _cost_evidence()
    evidence["auxiliary_jobs"].append(
        bind_hf_auxiliary_cost_job(
            job_id="6" * 24,
            role="packet_cross_platform_self_verify",
            terminal_status="COMPLETED",
            flavor="cpu-basic",
            running_seconds=1,
            packet_tree_sha256=_HASH_B,
            observed_inventory_tree_sha256=None,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
    )

    budget = build_hf_quantization_budget(
        evidence,
        evidence_sha256=_cost_evidence_hash(evidence),
        expected_continuation_result_sha256=_CONTINUATION_RESULT_HASH,
        expected_final_evaluation_result_sha256=_HASH_A,
        expected_toolchain_materialization=_toolchain_materialization(),
    )

    assert budget["auxiliary_job_count"] == 5
    assert budget["auxiliary_actual_cost_usd"] == "0.001335"
    assert budget["accounted_before_quantization_usd"] == "4.961169"
    assert budget["maximum_total_cost_usd"] == "8.761169"


def test_cost_evidence_cli_derives_billed_minutes_and_exact_cost(
    tmp_path: Path,
) -> None:
    failed_path = tmp_path / "failed.json"
    second_failed_path = tmp_path / "second-failed.json"
    canceled_primary_path = tmp_path / "canceled-primary.json"
    failed_amendment_path = tmp_path / "failed-amendment.json"
    completed_path = tmp_path / "completed.json"
    auxiliary_paths = [
        tmp_path / "auxiliary-1.json",
        tmp_path / "auxiliary-2.json",
        tmp_path / "auxiliary-3.json",
    ]
    output = tmp_path / "cost-evidence.json"
    failed_path.write_text(
        json.dumps(
            {
                "job_id": "6a6ba10023ed89c748ec83f1",
                "terminal_status": "ERROR",
                "flavor": "l4x1",
                "timeout_minutes": 120,
                "running_seconds": 63,
                "last_pre_terminal_running_seconds": None,
                "packet_tree_sha256": ("sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"),
                "result_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    second_failed_path.write_text(
        json.dumps(
            {
                "job_id": "6a6ba85b23ed89c748ec8491",
                "terminal_status": "ERROR",
                "flavor": "l4x1",
                "timeout_minutes": 120,
                "running_seconds": 45,
                "last_pre_terminal_running_seconds": None,
                "packet_tree_sha256": ("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
                "result_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    canceled_primary_path.write_text(
        json.dumps(
            {
                "job_id": "6a6baf33b36a6516e96a3210",
                "terminal_status": "CANCELED",
                "flavor": "l4x1",
                "timeout_minutes": 120,
                "running_seconds": None,
                "last_pre_terminal_running_seconds": 2810,
                "packet_tree_sha256": ("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
                "result_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    failed_amendment_path.write_text(
        json.dumps(
            {
                "job_id": "6a6bc2edb36a6516e96a3347",
                "terminal_status": "ERROR",
                "flavor": "l4x1",
                "timeout_minutes": 180,
                "running_seconds": 78,
                "last_pre_terminal_running_seconds": None,
                "packet_tree_sha256": ("sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"),
                "result_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    completed_path.write_text(
        json.dumps(
            {
                "job_id": "3" * 24,
                "terminal_status": "COMPLETED",
                "flavor": "l4x1",
                "timeout_minutes": 180,
                "running_seconds": 10_800,
                "last_pre_terminal_running_seconds": None,
                "packet_tree_sha256": _HASH_C,
                "result_sha256": _HASH_A,
            }
        ),
        encoding="utf-8",
    )
    for path, job_id, running_seconds in zip(
        auxiliary_paths,
        (
            "6a6ba991b36a6516e96a31bc",
            "6a6ba9d8b36a6516e96a31be",
            "6a6bae3cb36a6516e96a320c",
        ),
        (23, 24, 44),
        strict=True,
    ):
        path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "role": "packet_mount_inventory",
                    "terminal_status": "COMPLETED",
                    "flavor": "cpu-basic",
                    "running_seconds": running_seconds,
                    "packet_tree_sha256": (
                        "sha256:bc396bdcf45b57d8d12ed9d31a407f9862f96c33eca0f971984ccf2c192ca8ba"
                        if job_id == "6a6bae3cb36a6516e96a320c"
                        else "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"
                    ),
                    "observed_inventory_tree_sha256": (
                        None
                        if job_id == "6a6bae3cb36a6516e96a320c"
                        else "sha256:da912da24b0861de5ebf0ea4a8040f583351966d5af8846a7df049120629113b"
                    ),
                    "training_steps_executed": 0,
                    "evaluation_reports_written": 0,
                    "prediction_records_written": 0,
                }
            ),
            encoding="utf-8",
        )
    script = Path(__file__).parents[1] / "scripts/build_hf_actual_cost_evidence.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--retrieved-at-utc",
            "2026-07-30T14:00:00Z",
            "--final-evaluation-attempt",
            str(failed_path),
            "--final-evaluation-attempt",
            str(second_failed_path),
            "--final-evaluation-attempt",
            str(canceled_primary_path),
            "--final-evaluation-attempt",
            str(failed_amendment_path),
            "--final-evaluation-attempt",
            str(completed_path),
            "--auxiliary-job",
            str(auxiliary_paths[0]),
            "--auxiliary-job",
            str(auxiliary_paths[1]),
            "--auxiliary-job",
            str(auxiliary_paths[2]),
            "--no-other-campaign-jobs-running",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["final_evaluation_attempts"][0]["billed_minutes"] == 2
    assert evidence["final_evaluation_attempts"][0]["actual_cost_usd"] == "0.026667"
    assert evidence["final_evaluation_attempts"][1]["billed_minutes"] == 1
    assert evidence["final_evaluation_attempts"][1]["actual_cost_usd"] == "0.013333"
    assert evidence["final_evaluation_attempts"][2]["billed_minutes"] is None
    assert evidence["final_evaluation_attempts"][2]["actual_cost_usd"] is None
    assert evidence["final_evaluation_attempts"][2]["reserved_cost_usd"] == "1.600000"
    assert evidence["final_evaluation_attempts"][3]["billed_minutes"] == 2
    assert evidence["final_evaluation_attempts"][3]["actual_cost_usd"] == "0.026667"
    assert evidence["final_evaluation_attempts"][4]["billed_minutes"] == 180
    assert evidence["final_evaluation_attempts"][4]["actual_cost_usd"] == "2.400000"
    assert [job["actual_cost_usd"] for job in evidence["auxiliary_jobs"]] == [
        "0.000167",
        "0.000167",
        "0.000167",
    ]


def test_dry_bind_requires_successful_final_evaluation_and_binds_every_variant() -> None:
    plan = _plan()

    assert plan["kind"] == "scriber_hf_quantization_plan"
    assert plan["source_model"]["final_model_tree_sha256"] == _HASH_A
    assert plan["source_model"]["training_fingerprint"] == _HASH_D
    assert plan["source_model"]["protection_policy"] == _POLICY
    assert plan["image"] == _IMAGE
    assert plan["llama_cpp"]["remote_uri"] == _LLAMA_URI
    assert plan["llama_cpp"]["distribution"]["runtime_image"] == _IMAGE
    assert plan["llama_cpp"]["native_runtime_tree_sha256"] == _HASH_D
    assert plan["variants"] == [
        {"id": "bf16", "backend": "transformers", "evaluation": "fresh_full"},
        {"id": "int8", "backend": "transformers_bitsandbytes", "evaluation": "fresh_full"},
        {"id": "int4_nf4", "backend": "transformers_bitsandbytes", "evaluation": "fresh_full"},
        {"id": "Q8_0", "backend": "llama_cpp", "evaluation": "fresh_full"},
        {"id": "Q4_K_M", "backend": "llama_cpp", "evaluation": "fresh_full"},
    ]
    assert plan["publication"] == {
        "allowed": False,
        "destination_kind": "private_hf_bucket",
    }
    assert validate_hf_quantization_plan(plan) == plan


def test_plan_rejects_changed_actual_cost_evidence_or_budget() -> None:
    plan = _plan()
    changed_evidence = deepcopy(plan)
    changed_evidence["actual_cost_evidence"]["final_evaluation_attempts"][0]["terminal_status"] = "RUNNING"
    with pytest.raises(
        HfQuantizationError,
        match="actual-cost evidence hash differs",
    ):
        validate_hf_quantization_plan(changed_evidence)

    changed_budget = deepcopy(plan)
    changed_budget["budget"]["maximum_total_cost_usd"] = "4.919833"
    with pytest.raises(
        HfQuantizationError,
        match="budget differs",
    ):
        validate_hf_quantization_plan(changed_budget)

    changed_driver = deepcopy(plan)
    changed_driver["llama_cpp"]["required_host_driver_sonames"] = []
    with pytest.raises(
        HfQuantizationError,
        match="verified Linux CUDA toolchain",
    ):
        validate_hf_quantization_plan(changed_driver)


def test_actual_cost_snapshot_expires_before_a_delayed_launch() -> None:
    plan = _plan()

    assert verify_hf_actual_cost_freshness(
        plan,
        now_utc=datetime(2026, 7, 30, 14, 10, tzinfo=UTC),
    ) == {
        "retrieved_at_utc": "2026-07-30T14:00:00Z",
        "expires_at_utc": "2026-07-30T14:15:00Z",
    }
    with pytest.raises(HfQuantizationError, match="stale"):
        verify_hf_actual_cost_freshness(
            plan,
            now_utc=datetime(2026, 7, 30, 14, 16, tzinfo=UTC),
        )
    # Structural verification remains valid after submission. Only the local
    # pre-submission freshness gate expires, so scheduler delay cannot break a
    # packet that was fresh when submitted.
    assert validate_hf_quantization_plan(plan) == plan


def test_dry_bind_rejects_failed_evaluation_or_nonprivate_inputs() -> None:
    inputs = _inputs()
    cost_evidence = _cost_evidence()
    inputs["final_evaluation_result"]["reports"]["final"]["regular_test"]["gate_passed"] = False
    with pytest.raises(HfQuantizationError, match="successful final evaluation"):
        build_hf_quantization_dry_bind(
            final_evaluation_result=inputs["final_evaluation_result"],
            final_evaluation_result_sha256=_HASH_A,
            final_evaluation_manifest=inputs["final_evaluation_manifest"],
            final_evaluation_manifest_sha256=_HASH_B,
            training_report=inputs["training_report"],
            training_report_sha256=_HASH_C,
            remote_model_uri=_MODEL_URI,
            llama_cpp_bundle=inputs["llama_cpp_bundle"],
            remote_llama_cpp_uri=_LLAMA_URI,
            source_git_head="6" * 40,
            actual_cost_evidence=cost_evidence,
            actual_cost_evidence_sha256=_cost_evidence_hash(cost_evidence),
        )

    inputs = _inputs()
    with pytest.raises(HfQuantizationError, match="verified Linux CUDA toolchain"):
        build_hf_quantization_dry_bind(
            final_evaluation_result=inputs["final_evaluation_result"],
            final_evaluation_result_sha256=_HASH_A,
            final_evaluation_manifest=inputs["final_evaluation_manifest"],
            final_evaluation_manifest_sha256=_HASH_B,
            training_report=inputs["training_report"],
            training_report_sha256=_HASH_C,
            remote_model_uri=_MODEL_URI,
            llama_cpp_bundle=inputs["llama_cpp_bundle"],
            remote_llama_cpp_uri="https://huggingface.co/public/toolchain",
            source_git_head="6" * 40,
            actual_cost_evidence=cost_evidence,
            actual_cost_evidence_sha256=_cost_evidence_hash(cost_evidence),
        )


def test_runner_uses_exact_l4_and_reuses_bf16_evidence_without_bf16_work() -> None:
    payload = hf_quantization_runner_payload().decode("utf-8")

    assert "materialize_bf16_variant_manifest.py" not in payload
    assert '"$STAGE_ROOT/variants/bf16"' not in payload
    assert "for VARIANT in bf16" not in payload
    assert "quantization=none" not in payload
    assert "materialize_quantized_model.py" in payload
    assert "for VARIANT in int8 int4_nf4" in payload
    assert '--variant "$VARIANT"' in payload
    assert "build_gguf.py" in payload
    assert "Q8_0" in payload and "Q4_K_M" in payload
    assert "run_hf_quantization_gguf_inference.py" in payload
    assert "build_quality_delta_report.py" in payload
    assert '--baseline "$REMOTE_BF16_BASELINE/$SUITE_ID/evaluation.json"' in payload
    assert "REMOTE_BF16_BASELINE=/bf16-baseline" in payload
    assert "--mode bf16-baseline" in payload
    assert "RESUME_ROOT=" not in payload
    assert "scriber_hf_quantization_resume_binding" not in payload
    assert "persist_directory" not in payload
    assert "--mode variant" in payload
    assert "--mode lane" in payload
    assert 'cp -a --no-preserve=ownership "$STAGE_ROOT" "$PUBLISH_STAGE"' in payload
    assert "--mode publish" in payload
    assert 'mv -T "$OUTPUT_TEMP" "$OUTPUT_ROOT"' not in payload
    assert "rm -" not in payload
    assert "HF_HUB_OFFLINE=1" in payload
    assert "PUBLISH_STAGE=" in payload
    assert "HF_TOKEN" not in payload
    assert "--require-fresh-actual-cost" not in payload
    assert "--mode llama-library-path" in payload
    assert 'CUDA_RUNTIME_LIBRARY="$CUDA_RUNTIME_LIBRARY_PATH/libcudart.so.12"' in payload
    assert 'CUBLAS_LIBRARY="$CUBLAS_LIBRARY_PATH/libcublas.so.12"' in payload
    assert 'NCCL_LIBRARY="$NCCL_LIBRARY_PATH/libnccl.so.2"' in payload
    assert (
        'export LD_LIBRARY_PATH="$LLAMA_CPP_LIBRARY_PATH:$CUDA_RUNTIME_LIBRARY_PATH:'
        '$CUBLAS_LIBRARY_PATH:$NCCL_LIBRARY_PATH'
        '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
    ) in payload
    driver_probe = payload.index('ctypes.CDLL("libcuda.so.1"')
    package_install = payload.index('"$PYTHON" -m pip install')
    closure_probe = payload.index("--mode llama-cpp")
    model_copy = payload.index(
        'cp -a --no-preserve=ownership "$REMOTE_MODEL" "$LOCAL_MODEL"'
    )
    assert driver_probe < package_install
    assert package_install < closure_probe
    assert closure_probe < model_copy
    assert "driver.cuInit(0)" in payload
    assert "driver.cuDeviceGetCount" in payload
    assert "device_count.value != 1" in payload
    assert "driver.cuDeviceGetName" in payload
    assert 'gpu_name != "NVIDIA L4"' in payload
    assert "driver.cuDeviceGetAttribute" in payload
    assert "(major.value, minor.value) != (8, 9)" in payload
    assert 'python -I -m venv "$VENV_ROOT"' in payload
    assert "--isolated" in payload
    assert "--force-reinstall" in payload
    assert "--ignore-installed" in payload
    assert "--no-deps" in payload
    assert "--break-system-packages" not in payload


def test_quantization_publication_is_atomic_and_preserves_a_race_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = "sha256:" + "e" * 64
    owner = "f" * 64
    output_parent = tmp_path / "outputs"
    stage = output_parent / ".staging" / f"{owner}-{tree.removeprefix('sha256:')}"
    destination = output_parent / "final"
    stage.mkdir(parents=True)
    (stage / "sentinel").write_text("candidate", encoding="utf-8")
    expected = {"status": "complete"}
    monkeypatch.setattr(
        quantization,
        "verify_hf_quantization_result_file",
        lambda **_kwargs: expected,
    )
    monkeypatch.setattr(
        quantization,
        "_rename_quantization_directory_no_replace",
        lambda source, target: source.rename(target),
    )

    assert quantization.publish_hf_quantization_result_no_clobber(
        manifest_path=stage / "hf-quantization-plan.json",
        staged_root=stage,
        result_path=stage / "hf-quantization-result.json",
        destination_root=destination,
        baseline_root=tmp_path / "baseline",
        expected_packet_tree_sha256=tree,
        owner_token=owner,
    ) == expected
    assert (destination / "sentinel").read_text(encoding="utf-8") == "candidate"

    second_owner = "a" * 64
    second_stage = (
        output_parent
        / ".staging"
        / f"{second_owner}-{tree.removeprefix('sha256:')}"
    )
    second_stage.mkdir()
    race_destination = output_parent / "race-final"

    def collision(_source: Path, target: Path) -> None:
        assert target == race_destination
        target.mkdir()
        (target / "sentinel").write_text("winner", encoding="utf-8")
        raise quantization.HfQuantizationError(
            "completed quantization output already exists"
        )

    monkeypatch.setattr(
        quantization,
        "_rename_quantization_directory_no_replace",
        collision,
    )
    with pytest.raises(quantization.HfQuantizationError, match="preserved"):
        quantization.publish_hf_quantization_result_no_clobber(
            manifest_path=second_stage / "hf-quantization-plan.json",
            staged_root=second_stage,
            result_path=second_stage / "hf-quantization-result.json",
            destination_root=race_destination,
            baseline_root=tmp_path / "baseline",
            expected_packet_tree_sha256=tree,
            owner_token=second_owner,
        )
    assert (race_destination / "sentinel").read_text(encoding="utf-8") == "winner"


@pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is a Linux syscall")
def test_real_renameat2_race_has_exactly_one_intact_winner(tmp_path: Path) -> None:
    output_parent = tmp_path / "outputs"
    stage_parent = output_parent / ".staging"
    stage_parent.mkdir(parents=True)
    sources = [stage_parent / "one", stage_parent / "two"]
    for source, marker in zip(sources, ("one", "two"), strict=True):
        source.mkdir()
        (source / "sentinel").write_text(marker, encoding="utf-8")
    destination = output_parent / "final"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def publish(source: Path) -> None:
        barrier.wait(timeout=5)
        try:
            quantization._rename_quantization_directory_no_replace(
                source,
                destination,
            )
            outcomes.append("winner")
        except HfQuantizationError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=publish, args=(source,)) for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["blocked", "winner"]
    assert (destination / "sentinel").read_text(encoding="utf-8") in {"one", "two"}
    assert sum(source.exists() for source in sources) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is a Linux syscall")
def test_missing_renameat2_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_parent = tmp_path / "outputs"
    source = output_parent / ".staging" / "candidate"
    source.mkdir(parents=True)
    monkeypatch.setattr(quantization.ctypes, "CDLL", lambda *_args, **_kwargs: object())
    with pytest.raises(HfQuantizationError, match="unavailable"):
        quantization._rename_quantization_directory_no_replace(
            source,
            output_parent / "final",
        )


def test_full_model_bf16_baseline_is_hash_bound_and_cannot_be_reexecuted(
    tmp_path: Path,
) -> None:
    plan = _full_model_plan(tmp_path)
    baseline_root = tmp_path / "bf16-baseline"
    for suite_id in ("ai_gold", "critical_regression", "regular_test"):
        report = baseline_root / suite_id / "evaluation.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"candidate": "final", "suite": suite_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        report.write_bytes(payload)
        plan["immutable_bf16_baseline"]["suites"][suite_id].update(
            {
                "bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(plan), encoding="utf-8")

    verified = verify_immutable_bf16_baseline(
        manifest_path=manifest,
        baseline_root=baseline_root,
    )
    assert verified["status"] == "verified"
    assert verified["materialization_runs"] == 0
    assert verified["inference_runs"] == 0
    assert verified["evaluation_runs"] == 0

    with pytest.raises(HfQuantizationError, match="BF16 inference and evaluation"):
        verify_quantization_completed_lane(
            manifest_path=manifest,
            output_root=tmp_path / "output",
            variant="bf16",
            suite_id="ai_gold",
            baseline_root=baseline_root,
        )

    (baseline_root / "ai_gold" / "evaluation.json").write_text(
        '{"tampered":true}',
        encoding="utf-8",
    )
    with pytest.raises(HfQuantizationError, match="differs for ai_gold"):
        verify_immutable_bf16_baseline(
            manifest_path=manifest,
            baseline_root=baseline_root,
        )


def test_llama_cpp_cuda_offload_requires_same_runtime_l4_and_every_layer() -> None:
    stderr = (
        "ggml_cuda_init: found 1 CUDA devices:\n"
        "  Device 0: NVIDIA L4, compute capability 8.9, VMM: yes\n"
        "load_tensors: offloaded 19/19 layers to GPU\n"
        "load_tensors: CUDA0 model buffer size = 2310.25 MiB\n"
    )

    evidence = verify_llama_cpp_cuda_offload(
        stderr,
        requested_gpu_layers=999,
        runtime_instance_id=_HASH_A,
    )

    assert evidence == {
        "schema_version": 1,
        "kind": "scriber_llama_cpp_cuda_offload_evidence",
        "status": "verified",
        "device": "cuda",
        "backend": "llama_cpp_cuda",
        "runtime_instance_id": _HASH_A,
        "requested_gpu_layers": 999,
        "cuda_device_count": 1,
        "gpu_name": "NVIDIA L4",
        "compute_capability": "8.9",
        "offloaded_layers": 19,
        "total_layers": 19,
        "stderr_sha256": ("sha256:" + hashlib.sha256(stderr.encode()).hexdigest()),
    }

    with pytest.raises(HfQuantizationError, match="CUDA offload"):
        verify_llama_cpp_cuda_offload(
            "load_tensors: CPU model buffer size = 2310.25 MiB\n",
            requested_gpu_layers=999,
            runtime_instance_id=_HASH_A,
        )

    with pytest.raises(HfQuantizationError, match="single-L4 offload"):
        verify_llama_cpp_cuda_offload(
            stderr.replace("compute capability 8.9", "compute capability 8.6"),
            requested_gpu_layers=999,
            runtime_instance_id=_HASH_A,
        )


def _result(plan: dict[str, object]) -> dict[str, object]:
    variants = {}
    full_model = plan["schema_version"] == 2
    variant_ids = (
        ("int8", "int4_nf4", "Q8_0", "Q4_K_M")
        if full_model
        else ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
    )
    for variant in variant_ids:
        evaluation = {
            suite: {
                "record_count": plan["references"][suite]["record_count"],
                "exact_case_coverage": True,
                "gate_passed": True,
                "prediction_sha256": _HASH_C,
                "evaluation_sha256": _HASH_D,
                "quality_delta": {
                    "baseline_evaluation_sha256": _HASH_D,
                    "candidate_evaluation_sha256": _HASH_D,
                    "report_sha256": _HASH_A,
                    "maximum_absolute_metric_drop": 0.0,
                    "new_critical_errors": 0,
                    "budget": deepcopy(plan["evaluation"]["quality_delta_budgets"][variant]),
                    "within_budget": True,
                },
            }
            for suite in plan["references"]
        }
        if variant in {"Q8_0", "Q4_K_M"}:
            for suite_id, lane in evaluation.items():
                instance = "sha256:" + hashlib.sha256(f"{variant}/{suite_id}".encode()).hexdigest()
                lane["runtime_evidence"] = bind_llama_cpp_cuda_offload_evidence(
                    _offload_evidence(instance),
                    variant=variant,
                    suite_id=suite_id,
                    model_sha256=_HASH_B,
                    llama_server_sha256=_HASH_D,
                    reference_sha256=("sha256:" + str(plan["references"][suite_id]["records_sha256"])),
                    prediction_sha256=_HASH_C,
                )
        variants[variant] = {
            "artifact_tree_sha256": _HASH_B,
            "source_tree_sha256": plan["source_model"]["final_model_tree_sha256"],
            "training_fingerprint": plan["source_model"]["training_fingerprint"],
            "protection_policy": deepcopy(_POLICY),
            "fresh_reload_passed": True,
            "evaluation": evaluation,
        }
    result = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_result",
        "status": "complete",
        "source_model_tree_sha256": plan["source_model"]["final_model_tree_sha256"],
        "training_fingerprint": plan["source_model"]["training_fingerprint"],
        "protection_policy": deepcopy(_POLICY),
        "variants": variants,
        "budget": deepcopy(plan["budget"]),
        "publication": deepcopy(plan["publication"]),
    }
    if full_model:
        baseline = plan["immutable_bf16_baseline"]
        result["immutable_bf16_baseline"] = {
            "schema_version": 1,
            "kind": "scriber_immutable_bf16_baseline_verification",
            "status": "verified",
            "source_model_tree_sha256": baseline["source_model_tree_sha256"],
            "model_weight_sha256": baseline["model_weight_sha256"],
            "model_weight_bytes": baseline["model_weight_bytes"],
            "final_evaluation_result_sha256": baseline[
                "final_evaluation_result_sha256"
            ],
            "final_evaluation_result_bytes": baseline[
                "final_evaluation_result_bytes"
            ],
            "final_evaluation_manifest_sha256": baseline[
                "final_evaluation_manifest_sha256"
            ],
            "final_evaluation_manifest_bytes": baseline[
                "final_evaluation_manifest_bytes"
            ],
            "remote_uri": baseline["remote_uri"],
            "suites": {
                suite: {
                    "bytes": binding["bytes"],
                    "sha256": binding["sha256"],
                }
                for suite, binding in baseline["suites"].items()
            },
            "materialization_runs": 0,
            "inference_runs": 0,
            "evaluation_runs": 0,
        }
    return result


def test_result_contract_rejects_missing_reevaluation_or_cross_model_artifact() -> None:
    plan = _plan()
    result = _result(plan)

    assert validate_hf_quantization_result(result, plan=plan) == result

    missing = deepcopy(result)
    del missing["variants"]["Q4_K_M"]["evaluation"]["regular_test"]
    with pytest.raises(HfQuantizationError, match="Q4_K_M.*evaluation"):
        validate_hf_quantization_result(missing, plan=plan)

    substituted = deepcopy(result)
    substituted["variants"]["int8"]["source_tree_sha256"] = _HASH_C
    with pytest.raises(HfQuantizationError, match="int8.*source model"):
        validate_hf_quantization_result(substituted, plan=plan)

    quality_regression = deepcopy(result)
    quality_regression["variants"]["Q4_K_M"]["evaluation"]["ai_gold"]["quality_delta"][
        "maximum_absolute_metric_drop"
    ] = 0.02
    with pytest.raises(HfQuantizationError, match="quality delta"):
        validate_hf_quantization_result(quality_regression, plan=plan)

    cpu_fallback = deepcopy(result)
    cpu_fallback["variants"]["Q8_0"]["evaluation"]["ai_gold"]["runtime_evidence"]["offload"]["offloaded_layers"] = 0
    with pytest.raises(HfQuantizationError, match="CUDA offload"):
        validate_hf_quantization_result(cpu_fallback, plan=plan)

    replayed_runtime = deepcopy(result)
    replayed_runtime["variants"]["Q8_0"]["evaluation"]["regular_test"]["runtime_evidence"] = deepcopy(
        replayed_runtime["variants"]["Q8_0"]["evaluation"]["ai_gold"]["runtime_evidence"]
    )
    with pytest.raises(
        HfQuantizationError,
        match="exact binding|reused",
    ):
        validate_hf_quantization_result(
            replayed_runtime,
            plan=plan,
        )
