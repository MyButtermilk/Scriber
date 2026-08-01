from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import scriber_polishing.hf_llama_cpp_toolchain as toolchain
from scriber_polishing.hf_llama_cpp_toolchain import (
    HfLlamaCppToolchainError,
    build_hf_llama_cpp_toolchain_launch_contract,
    build_hf_toolchain_cost_guard,
    build_refreshed_hf_llama_cpp_toolchain_launch_contract,
    hf_llama_cpp_toolchain_runner_payload,
    validate_hf_llama_cpp_toolchain_manifest,
)
from scriber_polishing.hf_quantization_pipeline import (
    bind_hf_auxiliary_cost_job,
    bind_hf_final_evaluation_cost_attempt,
    build_hf_actual_cost_evidence,
)

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_HASH_D = "sha256:" + "d" * 64
_CONTINUATION_RESULT_HASH = "sha256:9e2755ac5411317da0bc70ddfb79716ef9b6704fbf923522f66421c26f343e7d"


def _canonical_hash(value: object) -> str:
    payload = (
        json.dumps(
            value,
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


def _pre_toolchain_cost_evidence() -> dict[str, object]:
    attempts = [
        bind_hf_final_evaluation_cost_attempt(
            job_id="6a6ba10023ed89c748ec83f1",
            terminal_status="ERROR",
            flavor="l4x1",
            running_seconds=63,
            packet_tree_sha256=("sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"),
            result_sha256=None,
        ),
        bind_hf_final_evaluation_cost_attempt(
            job_id="6a6ba85b23ed89c748ec8491",
            terminal_status="ERROR",
            flavor="l4x1",
            running_seconds=45,
            packet_tree_sha256=("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
            result_sha256=None,
        ),
        bind_hf_final_evaluation_cost_attempt(
            job_id="6a6baf33b36a6516e96a3210",
            terminal_status="CANCELED",
            flavor="l4x1",
            timeout_minutes=120,
            running_seconds=None,
            last_pre_terminal_running_seconds=2810,
            packet_tree_sha256=("sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"),
            result_sha256=None,
        ),
        bind_hf_final_evaluation_cost_attempt(
            job_id="6a6bc2edb36a6516e96a3347",
            terminal_status="ERROR",
            flavor="l4x1",
            timeout_minutes=180,
            running_seconds=78,
            packet_tree_sha256=("sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"),
            result_sha256=None,
        ),
        bind_hf_final_evaluation_cost_attempt(
            job_id="4" * 24,
            terminal_status="COMPLETED",
            flavor="l4x1",
            timeout_minutes=180,
            running_seconds=10_800,
            packet_tree_sha256=_HASH_C,
            result_sha256=_HASH_A,
        ),
    ]
    auxiliary = [
        bind_hf_auxiliary_cost_job(
            job_id=job_id,
            role="packet_mount_inventory",
            terminal_status="COMPLETED",
            flavor="cpu-basic",
            running_seconds=seconds,
            packet_tree_sha256=packet_tree_sha256,
            observed_inventory_tree_sha256=observed_inventory_tree_sha256,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
        for job_id, seconds, packet_tree_sha256, observed_inventory_tree_sha256 in (
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
    return build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=attempts,
        auxiliary_jobs=auxiliary,
        no_other_campaign_jobs_running=True,
    )


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_job",
        "attempt": "v19",
        "source_git_head": toolchain.REVIEWED_SCRIBER_GIT_HEAD,
        "image": toolchain.TOOLCHAIN_IMAGE,
        "flavor": "cpu-basic",
        "timeout_minutes": 20,
        "source": {
            "repository": "https://github.com/ggml-org/llama.cpp",
            "commit": toolchain.TOOLCHAIN_COMMIT,
            "version": "b10156",
            "checkout_inventory": {
                "algorithm": "sha256-canonical-path-kind-size-content-or-target-v1",
                "tree_sha256": _HASH_A,
                "entry_count": 1,
                "total_file_bytes": 1,
            },
            "source_inventory": {
                "algorithm": "sha256-path-size-content-v1",
                "tree_sha256": _HASH_B,
                "file_count": 1,
                "total_bytes": 1,
            },
        },
        "oci": {
            "repository": "ghcr.io/ggml-org/llama.cpp",
            "tag": "full-cuda12-b10156",
            "index_digest": toolchain.INDEX_DIGEST,
            "manifest_digest": toolchain.MANIFEST_DIGEST,
            "config_digest": toolchain.CONFIG_DIGEST,
            "revision": toolchain.TOOLCHAIN_COMMIT,
            "platform": {"os": "linux", "architecture": "amd64"},
            "layers": [
                {
                    "digest": digest,
                    "compressed_bytes": size,
                    "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "purpose": purpose,
                    "packet_filename": f"{digest.removeprefix('sha256:')}.tar.gz",
                }
                for purpose, (digest, size) in zip(
                    ("native_libraries", "tools_and_conversion"),
                    toolchain.SELECTED_LAYERS.items(),
                    strict=True,
                )
            ],
        },
        "build": {
            "llama_cpp_version": "b10156",
            "compiler": {"id": "GNU", "version": "14.2.0"},
            "cuda": {"enabled": True, "version": "12.8.1"},
            "generator": "Unix Makefiles",
            "configuration": "Release",
            "flags": list(toolchain.BUILD_FLAGS),
        },
        "dependencies": {
            "mode": "offline_no_index_require_hashes",
            "requirements_sha256": toolchain._sha256(
                toolchain._offline_requirements_payload()
            ),
            "wheels": [
                {
                    "filename": filename,
                    "bytes": size,
                    "sha256": digest,
                    "requirement": requirement,
                }
                for filename, size, digest, requirement in toolchain.TOOLCHAIN_OFFLINE_WHEELS
            ],
        },
        "cost_guard": {
            "actual_cost_evidence_sha256": _HASH_C,
            "actual_cost_as_of_utc": "2026-07-30T14:00:00Z",
            "actual_campaign_cost_usd": "3.360335",
            "unknown_cost_reservation_usd": "1.600000",
            "accounted_campaign_cost_usd": "4.960335",
            "accounted_campaign_job_ids": ["0" * 24],
            "accounted_campaign_job_ids_sha256": _canonical_hash(["0" * 24]),
            "budget_scope": "user_reported_remaining_hf_credits",
            "remaining_credit_authorization_usd": "5.500",
            "remaining_credit_authorization_sha256": (
                "sha256:ea566717d68b8372a4c5f39c6279e24efa36f906c1dfe06c048339bd6ae86c0d"
            ),
            "toolchain_flavor": "cpu-basic",
            "toolchain_hourly_rate_usd": "0.010",
            "toolchain_timeout_minutes": 20,
            "toolchain_maximum_cost_usd": "0.003333",
            "quantization_reserved_cost_usd": "3.800",
            "post_authorization_actual_spend_usd": "1.476999",
            "new_toolchain_and_quantization_reservation_usd": "3.803333",
            "maximum_authorized_new_spend_usd": "5.280332",
            "remaining_credit_after_reservations_usd": "0.219668",
            "strictly_below_remaining_credit_authorization": True,
        },
        "output": {
            "bucket_root": toolchain.TOOLCHAIN_OUTPUT_BUCKET,
            "mount_uri": toolchain.TOOLCHAIN_OUTPUT_MOUNT_URI,
            "mount_container_path": "/outputs",
            "relative_path": toolchain.TOOLCHAIN_OUTPUT_RELATIVE,
            "container_path": toolchain.TOOLCHAIN_OUTPUT_ROOT,
            "staging_container_path": toolchain.TOOLCHAIN_STAGING_ROOT,
            "remote_uri": toolchain.TOOLCHAIN_REMOTE_OUTPUT_URI,
            "unique": True,
            "overwrite_allowed": False,
            "publication_protocol": "owner_token_stage_rename_noreplace_v1",
        },
        "side_effects": {
            "training_steps_executed": 0,
            "evaluation_reports_written": 0,
            "prediction_records_written": 0,
            "publication_allowed": False,
            "secrets_required": [],
        },
        "runner_sha256": _HASH_D,
        "packet_tree_sha256": _HASH_A,
        "file_count": 1,
        "byte_count": 1,
    }


def test_toolchain_schemas_are_valid_and_manifest_is_exactly_pinned() -> None:
    for name in (
        "hf_llama_cpp_toolchain_job_manifest_schema.json",
        "hf_llama_cpp_toolchain_result_schema.json",
    ):
        schema = json.loads((Path(__file__).parents[1] / "contracts" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

    assert validate_hf_llama_cpp_toolchain_manifest(_manifest()) == _manifest()
    changed = _manifest()
    changed["timeout_minutes"] = 5
    with pytest.raises(HfLlamaCppToolchainError, match="schema failed"):
        validate_hf_llama_cpp_toolchain_manifest(changed)

    fallback = _manifest()
    fallback["flavor"] = "a10g-small"
    with pytest.raises(HfLlamaCppToolchainError, match="schema failed"):
        validate_hf_llama_cpp_toolchain_manifest(fallback)


def test_pre_toolchain_budget_reserves_twenty_minutes_and_full_quantization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _pre_toolchain_cost_evidence()

    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_canonical_hash(evidence),
    )

    accounted_ids = _accounted_job_ids(evidence)
    assert guard == {
        "actual_cost_evidence_sha256": _canonical_hash(evidence),
        "actual_cost_as_of_utc": "2026-07-30T14:00:00Z",
        "actual_campaign_cost_usd": "3.360335",
        "unknown_cost_reservation_usd": "1.600000",
        "accounted_campaign_cost_usd": "4.960335",
        "accounted_campaign_job_ids": accounted_ids,
        "accounted_campaign_job_ids_sha256": _canonical_hash(accounted_ids),
        "budget_scope": "user_reported_remaining_hf_credits",
        "remaining_credit_authorization_usd": "5.500",
        "remaining_credit_authorization_sha256": (
            "sha256:ea566717d68b8372a4c5f39c6279e24efa36f906c1dfe06c048339bd6ae86c0d"
        ),
        "toolchain_flavor": "cpu-basic",
        "toolchain_hourly_rate_usd": "0.010",
        "toolchain_timeout_minutes": 20,
        "toolchain_maximum_cost_usd": "0.003333",
        "quantization_reserved_cost_usd": "3.800",
        "post_authorization_actual_spend_usd": "1.476999",
        "new_toolchain_and_quantization_reservation_usd": "3.803333",
        "maximum_authorized_new_spend_usd": "5.280332",
        "remaining_credit_after_reservations_usd": "0.219668",
        "strictly_below_remaining_credit_authorization": True,
    }
    tampered = json.loads(json.dumps(evidence))
    tampered["final_evaluation_attempts"][-1]["actual_cost_usd"] = "0.000000"
    tampered["final_evaluation_attempts"][-1]["accounted_cost_usd"] = "0.000000"
    with pytest.raises(HfLlamaCppToolchainError, match="minute billing"):
        build_hf_toolchain_cost_guard(
            tampered,
            evidence_sha256=_canonical_hash(tampered),
        )

    monkeypatch.setattr(
        toolchain,
        "REMAINING_HF_CREDITS_AUTHORIZATION_USD",
        Decimal("3.803333"),
    )
    with pytest.raises(HfLlamaCppToolchainError, match="remaining HF credits"):
        build_hf_toolchain_cost_guard(
            evidence,
            evidence_sha256=_canonical_hash(evidence),
        )


def test_v19_cost_guard_rejects_every_non_cpu_fallback() -> None:
    evidence = _pre_toolchain_cost_evidence()

    for flavor in ("l4x1", "a10g-small"):
        with pytest.raises(HfLlamaCppToolchainError, match="only cpu-basic"):
            build_hf_toolchain_cost_guard(
                evidence,
                evidence_sha256=_canonical_hash(evidence),
                flavor=flavor,
            )


def test_cpu_materialization_uses_the_same_pinned_image_with_a_bounded_cost() -> None:
    evidence = _pre_toolchain_cost_evidence()

    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_canonical_hash(evidence),
        flavor="cpu-basic",
    )

    assert guard["toolchain_flavor"] == "cpu-basic"
    assert guard["toolchain_hourly_rate_usd"] == "0.010"
    assert guard["toolchain_maximum_cost_usd"] == "0.003333"
    assert guard["new_toolchain_and_quantization_reservation_usd"] == "3.803333"
    assert guard["maximum_authorized_new_spend_usd"] == "5.280332"
    assert guard["remaining_credit_after_reservations_usd"] == "0.219668"

    manifest = _manifest()
    manifest["flavor"] = "cpu-basic"
    manifest["cost_guard"] = guard
    assert validate_hf_llama_cpp_toolchain_manifest(manifest) == manifest


def test_toolchain_cost_guard_subtracts_prior_post_authorization_attempts() -> None:
    evidence = _pre_toolchain_cost_evidence()
    evidence["auxiliary_jobs"].append(
        bind_hf_auxiliary_cost_job(
            job_id="9" * 24,
            role="toolchain_materialization",
            terminal_status="ERROR",
            flavor="a10g-small",
            running_seconds=642,
            packet_tree_sha256=_HASH_D,
            observed_inventory_tree_sha256=None,
            training_steps_executed=0,
            evaluation_reports_written=0,
            prediction_records_written=0,
        )
    )
    evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:00:00Z",
        final_evaluation_attempts=evidence["final_evaluation_attempts"],
        auxiliary_jobs=evidence["auxiliary_jobs"],
        no_other_campaign_jobs_running=True,
    )

    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_canonical_hash(evidence),
        flavor="cpu-basic",
    )

    assert guard["post_authorization_actual_spend_usd"] == "1.476999"
    assert guard["new_toolchain_and_quantization_reservation_usd"] == "3.803333"
    assert guard["maximum_authorized_new_spend_usd"] == "5.280332"
    assert guard["remaining_credit_after_reservations_usd"] == "0.219668"


def test_toolchain_cost_guard_accounts_for_multiple_completed_evaluations() -> None:
    evidence = _pre_toolchain_cost_evidence()
    evidence["final_evaluation_attempts"].append(
        bind_hf_final_evaluation_cost_attempt(
            job_id="5" * 24,
            terminal_status="COMPLETED",
            flavor="l4x1",
            running_seconds=60,
            packet_tree_sha256=_HASH_D,
            result_sha256=_HASH_B,
        )
    )

    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_canonical_hash(evidence),
        flavor="cpu-basic",
    )

    assert guard["actual_campaign_cost_usd"] == "3.373668"
    assert guard["maximum_authorized_new_spend_usd"] == "5.280332"


def test_toolchain_runner_is_materialization_only_and_uses_fixed_output() -> None:
    runner = hf_llama_cpp_toolchain_runner_payload().decode()

    assert toolchain.TOOLCHAIN_OUTPUT_ROOT in runner
    assert "--mode materialize" in runner
    assert "--mode packet" not in runner
    assert "apt-get" not in runner
    assert "--no-index" in runner
    assert "--require-hashes" in runner
    assert 'python -I -m venv "$VENV_ROOT"' in runner
    assert '"$PYTHON" -m pip install --isolated' in runner
    assert "--force-reinstall" in runner
    assert "--ignore-installed" in runner
    assert "--no-deps" in runner
    assert "--break-system-packages" not in runner
    assert "reviewed_git_probe.py" in runner
    assert "SCRIBER_TRUSTED_PACKET_TREE_SHA256" in runner
    assert 'CUDA_RUNTIME_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib"' in runner
    assert 'CUDA_RUNTIME_LIBRARY="$CUDA_RUNTIME_LIBRARY_PATH/libcudart.so.12"' in runner
    assert 'CUDA_RUNTIME_RESOLVED="$(readlink -f "$CUDA_RUNTIME_LIBRARY")"' in runner
    assert 'CUBLAS_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib"' in runner
    assert 'CUBLAS_LIBRARY="$CUBLAS_LIBRARY_PATH/libcublas.so.12"' in runner
    assert 'CUBLAS_RESOLVED="$(readlink -f "$CUBLAS_LIBRARY")"' in runner
    assert 'NCCL_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib"' in runner
    assert 'NCCL_LIBRARY="$NCCL_LIBRARY_PATH/libnccl.so.2"' in runner
    assert 'NCCL_RESOLVED="$(readlink -f "$NCCL_LIBRARY")"' in runner
    assert (
        'export LD_LIBRARY_PATH="$CUDA_RUNTIME_LIBRARY_PATH:$CUBLAS_LIBRARY_PATH:$NCCL_LIBRARY_PATH'
        '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
    ) in runner
    assert "train_sft" not in runner
    assert "evaluate_predictions" not in runner
    assert "training_steps_executed=0" in runner


def test_git_probe_trusts_only_the_exact_source_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout=b"verified\n", stderr=b"")

    monkeypatch.setattr(toolchain.subprocess, "run", fake_run)

    assert toolchain._run_git(tmp_path, ["rev-parse", "HEAD"], "probe") == "verified"
    assert observed["command"] == [
        "git",
        "-c",
        f"safe.directory={tmp_path}",
        "rev-parse",
        "HEAD",
    ]
    assert observed["cwd"] == tmp_path


def test_remote_source_binding_can_skip_redundant_git_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[list[str]] = []

    def fake_git(_source: Path, arguments: list[str], _label: str) -> str:
        probes.append(arguments)
        if arguments[0] == "rev-parse":
            return toolchain.TOOLCHAIN_COMMIT
        if arguments[0] == "remote":
            return toolchain.SOURCE_REPOSITORY
        raise AssertionError(f"unexpected Git probe: {arguments}")

    monkeypatch.setattr(toolchain, "_run_git", fake_git)
    monkeypatch.setattr(
        toolchain,
        "_tree_inventory",
        lambda *_args, **_kwargs: ({}, {"tree_sha256": _HASH_A}),
    )
    monkeypatch.setattr(
        toolchain,
        "_llama_source_inventory",
        lambda *_args, **_kwargs: {"tree_sha256": _HASH_B},
    )
    (tmp_path / ".git").mkdir()

    binding = toolchain._verify_source_checkout(tmp_path, require_clean_status=False)

    assert binding["commit"] == toolchain.TOOLCHAIN_COMMIT
    assert [probe[0] for probe in probes] == ["rev-parse", "remote"]


def test_launch_contract_is_bounded_private_and_never_starts_a_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _pre_toolchain_cost_evidence()
    manifest = _manifest()
    manifest["cost_guard"] = build_hf_toolchain_cost_guard(
        original,
        evidence_sha256=_canonical_hash(original),
    )
    manifest["cost_guard"]["actual_cost_as_of_utc"] = "2026-07-30T14:00:00Z"
    (tmp_path / "job-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        toolchain,
        "verify_hf_llama_cpp_toolchain_packet",
        lambda *args, **kwargs: manifest,
    )

    launch = build_hf_llama_cpp_toolchain_launch_contract(
        tmp_path,
        remote_packet_uri="hf://buckets/Buttermilk03/scriber-polishing-private-runs/packets/toolchain-test",
        now_utc=datetime(2026, 7, 30, 14, 1, tzinfo=UTC),
    )

    assert launch["timeout"] == "20m"
    assert launch["maximum_job_cost_usd"] == "0.003333"
    assert launch["maximum_authorized_new_spend_usd"] == "5.280332"
    assert launch["remote_result_uri"] == toolchain.TOOLCHAIN_REMOTE_OUTPUT_URI
    assert launch["remote_output_absence_required"] is True
    assert launch["external_job_started"] is False
    assert launch["secret_names"] == []
    assert launch["argv"][:8] == [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        toolchain.TOOLCHAIN_JOB_NAME,
        "--flavor",
        "cpu-basic",
    ]
    assert toolchain.TOOLCHAIN_CAMPAIGN_LABEL in launch["argv"]
    assert toolchain.TOOLCHAIN_ROLE_LABEL in launch["argv"]
    assert "--name" in launch["argv"]
    assert toolchain.TOOLCHAIN_ATTEMPT_LABEL in launch["argv"]
    assert f"launch-claim={toolchain.TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER}" in launch["argv"]
    assert toolchain.hf_llama_cpp_toolchain_bootstrap_payload() in launch["argv"]
    assert launch["output_volume"] == f"{toolchain.TOOLCHAIN_OUTPUT_MOUNT_URI}:/outputs:rw"


def test_toolchain_packet_source_requires_reviewed_clean_git_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def clean_run(argv: list[str], **_kwargs):
        calls.append(argv)
        output = toolchain.REVIEWED_SCRIBER_GIT_HEAD + "\n" if "rev-parse" in argv else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(toolchain.subprocess, "run", clean_run)
    assert toolchain._git_head() == toolchain.REVIEWED_SCRIBER_GIT_HEAD
    assert calls == [
        ["git", "rev-parse", "HEAD"],
        [
            "git",
            "merge-base",
            "--is-ancestor",
            toolchain.REVIEWED_SCRIBER_GIT_HEAD,
            toolchain.REVIEWED_SCRIBER_GIT_HEAD,
        ],
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
    ]

    def dirty_run(argv: list[str], **_kwargs):
        if "rev-parse" in argv:
            output = toolchain.REVIEWED_SCRIBER_GIT_HEAD + "\n"
        elif "status" in argv:
            output = "?? unreviewed.py\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(toolchain.subprocess, "run", dirty_run)
    with pytest.raises(toolchain.HfLlamaCppToolchainError, match="dirty"):
        toolchain._git_head()


def test_cpu_launch_contract_keeps_the_identical_pinned_image_and_twenty_minute_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _pre_toolchain_cost_evidence()
    manifest = _manifest()
    manifest["flavor"] = "cpu-basic"
    manifest["cost_guard"] = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_canonical_hash(evidence),
        flavor="cpu-basic",
    )
    (tmp_path / "job-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        toolchain,
        "verify_hf_llama_cpp_toolchain_packet",
        lambda *args, **kwargs: manifest,
    )

    launch = build_hf_llama_cpp_toolchain_launch_contract(
        tmp_path,
        remote_packet_uri="hf://buckets/Buttermilk03/scriber-polishing-private-runs/packets/toolchain-cpu-test",
        now_utc=datetime(2026, 7, 30, 14, 1, tzinfo=UTC),
    )

    assert launch["timeout"] == "20m"
    assert launch["maximum_job_cost_usd"] == "0.003333"
    assert launch["maximum_authorized_new_spend_usd"] == "5.280332"
    assert launch["argv"][:8] == [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        toolchain.TOOLCHAIN_JOB_NAME,
        "--flavor",
        "cpu-basic",
    ]
    assert toolchain.TOOLCHAIN_IMAGE in launch["argv"]
    assert launch["external_job_started"] is False


def test_launch_authorization_refreshes_without_rebuilding_immutable_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _pre_toolchain_cost_evidence()
    manifest = _manifest()
    manifest["cost_guard"] = build_hf_toolchain_cost_guard(
        original,
        evidence_sha256=_canonical_hash(original),
    )
    (tmp_path / "job-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        toolchain,
        "verify_hf_llama_cpp_toolchain_packet",
        lambda *args, **kwargs: manifest,
    )
    refreshed = build_hf_actual_cost_evidence(
        retrieved_at_utc="2026-07-30T14:20:00Z",
        final_evaluation_attempts=original["final_evaluation_attempts"],
        auxiliary_jobs=original["auxiliary_jobs"],
        no_other_campaign_jobs_running=True,
    )
    evidence_path = tmp_path / "fresh-cost.json"
    evidence_path.write_text(
        json.dumps(refreshed, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    launch = build_refreshed_hf_llama_cpp_toolchain_launch_contract(
        tmp_path,
        actual_cost_evidence_path=evidence_path,
        remote_packet_uri="hf://buckets/Buttermilk03/scriber-polishing-private-runs/packets/toolchain-test",
        now_utc=datetime(2026, 7, 30, 14, 21, tzinfo=UTC),
    )

    assert launch["packet_tree_sha256"] == manifest["packet_tree_sha256"]
    assert launch["packet_actual_cost_evidence_sha256"] == _canonical_hash(original)
    assert launch["actual_cost_evidence_sha256"] == _canonical_hash(refreshed)
    assert launch["actual_cost_evidence_expires_at_utc"] == "2026-07-30T14:35:00Z"
    assert launch["launch_cost_guard_refreshed"] is True
    assert launch["external_job_started"] is False


def test_writable_bucket_prefix_must_not_overlap_any_read_only_input() -> None:
    toolchain.validate_no_writable_read_only_uri_overlap(
        writable_uri=(
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            "toolchains/llama-cpp-b10156-linux-cuda-v1/v19"
        ),
        read_only_uris=(
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/packets/v19",
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt11/model",
        ),
    )
    for read_only in (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs",
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/toolchains",
        (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            "toolchains/llama-cpp-b10156-linux-cuda-v1/v19"
        ),
        (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            "toolchains/llama-cpp-b10156-linux-cuda-v1/v19/final"
        ),
    ):
        with pytest.raises(HfLlamaCppToolchainError, match="overlaps"):
            toolchain.validate_no_writable_read_only_uri_overlap(
                writable_uri=toolchain.TOOLCHAIN_OUTPUT_MOUNT_URI,
                read_only_uris=(read_only,),
            )


def test_trusted_bootstrap_rejects_tampering_before_packet_runner_executes(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    marker = tmp_path / "runner-executed"
    runner = packet / "run-hf-llama-cpp-toolchain.sh"
    runner.write_text(
        "from pathlib import Path\nimport os\nPath(os.environ['BOOTSTRAP_TEST_MARKER']).write_text('ran')\n",
        encoding="utf-8",
        newline="\n",
    )
    _, inventory = toolchain._tree_inventory(
        packet,
        excluded=toolchain._PACKET_EXCLUDED,
    )
    runner_hash = toolchain._sha256_file(runner, "test runner")
    manifest = {
        "attempt": "v19",
        "byte_count": inventory["total_file_bytes"],
        "file_count": inventory["entry_count"],
        "flavor": "cpu-basic",
        "packet_tree_sha256": inventory["tree_sha256"],
        "runner_sha256": runner_hash,
        "source_git_head": toolchain.REVIEWED_SCRIBER_GIT_HEAD,
        "timeout_minutes": 20,
    }
    (packet / "job-manifest.json").write_bytes(toolchain._canonical_bytes(manifest))
    manifest_hash = toolchain._sha256_file(
        packet / "job-manifest.json",
        "test manifest",
    )
    runner.write_text("raise SystemExit('tampered packet code')\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["BOOTSTRAP_TEST_MARKER"] = str(marker)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            toolchain.hf_llama_cpp_toolchain_bootstrap_payload(
                packet,
                runner_executable=sys.executable,
            ),
            str(inventory["tree_sha256"]),
            runner_hash,
            manifest_hash,
            "a" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode != 0
    assert not marker.exists()
    assert "packet tree differs" in completed.stderr


def test_toolchain_source_inventory_and_bootstrap_reject_directory_symlinks(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "outside.txt").write_text("outside\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    source_link = source / "linked-directory"
    try:
        source_link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(HfLlamaCppToolchainError, match="non-regular"):
        toolchain._llama_source_inventory(source)

    packet = tmp_path / "packet"
    packet.mkdir()
    runner = packet / "run-hf-llama-cpp-toolchain.sh"
    runner.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    _, inventory = toolchain._tree_inventory(
        packet,
        excluded=toolchain._PACKET_EXCLUDED,
    )
    runner_hash = toolchain._sha256_file(runner, "test runner")
    manifest = {
        "attempt": "v19",
        "byte_count": inventory["total_file_bytes"],
        "file_count": inventory["entry_count"],
        "flavor": "cpu-basic",
        "packet_tree_sha256": inventory["tree_sha256"],
        "runner_sha256": runner_hash,
        "source_git_head": toolchain.REVIEWED_SCRIBER_GIT_HEAD,
        "timeout_minutes": 20,
    }
    manifest_path = packet / "job-manifest.json"
    manifest_path.write_bytes(toolchain._canonical_bytes(manifest))
    packet_link = packet / "linked-directory"
    packet_link.symlink_to(external, target_is_directory=True)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            toolchain.hf_llama_cpp_toolchain_bootstrap_payload(
                packet,
                runner_executable=sys.executable,
            ),
            str(inventory["tree_sha256"]),
            runner_hash,
            toolchain._sha256_file(manifest_path, "test manifest"),
            "a" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "non-regular entry" in completed.stderr


def test_two_publishers_cannot_remove_or_replace_the_verified_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "final"
    stage_parent = tmp_path / ".staging"
    stage_parent.mkdir()
    stages = [stage_parent / "one", stage_parent / "two"]
    expected = [{"winner": "one"}, {"winner": "two"}]
    for stage, result in zip(stages, expected, strict=True):
        stage.mkdir()
        (stage / "result.json").write_text(json.dumps(result), encoding="utf-8")

    def fake_verify(root: Path, **_kwargs: object) -> dict[str, object]:
        return json.loads((Path(root) / "result.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(toolchain, "verify_hf_llama_cpp_toolchain_result", fake_verify)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def publish(index: int) -> None:
        barrier.wait()
        try:
            value = toolchain._publish_toolchain_no_clobber(
                stages[index],
                output,
                expected_packet_tree_sha256=_HASH_A,
                expected_result=expected[index],
            )
            outcomes.append(("ok", value))
        except HfLlamaCppToolchainError as error:
            outcomes.append(("blocked", str(error)))

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(kind for kind, _value in outcomes) == ["blocked", "ok"]
    winner = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert winner in expected
    assert output.is_dir()
