from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.hf_v2_training_postflight import (
    V2TrainingPostflightError,
    verify_v2_training_postflight,
)

JOB_ID = "6a6e05bf6b79c09949c1e5fd"
PACKET_TREE = "sha256:3d11fbc9441ee4e563b46a87bf354a34fd9ff59d04a784325c725375f67ec5b6"
PARENT_TREE = "sha256:4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
LAUNCH_CONTRACT = "sha256:ecb13e460f230f9ee5e265923df82fa8be3975033eddad9cf4e8356104a7a1f9"
POLICY_SHA = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
    "scriber-v2-hardcase-replay-v1"
)
FINGERPRINT = "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
FINAL_DIRECTORY = "final-7b23f23e70f60df9"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _tree(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode())
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode())
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _write(path: Path, value: object) -> bytes:
    payload = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _materialize_evidence(root: Path) -> dict[str, Path]:
    reload_report = {
        "loader": "transformers_local",
        "model_type": "gemma3_text",
        "parameter_count": 268_098_176,
        "protection_policy_sha256": POLICY_SHA,
        "run_fingerprint": FINGERPRINT,
        "schema_version": 1,
        "status": "passed",
        "tokenizer_class": "transformers.models.gemma.tokenization_gemma_fast.GemmaTokenizerFast",
    }
    reload_payload = _canonical(reload_report)
    files = [
        {
            "bytes": 35,
            "path": "added_tokens.json",
            "sha256": "sha256:50b2f405ba56a26d4913fd772089992252d7f942123cc0a034d96424221ba946",
        },
        {
            "bytes": 1_532,
            "path": "chat_template.jinja",
            "sha256": "sha256:7de1c58e208eda46e9c7f86397df37ec49883aeece39fb961e0a6b24088dd3c4",
        },
        {
            "bytes": 1_509,
            "path": "config.json",
            "sha256": "sha256:83c1e2b6544bcc6a03e1105c109bea141edbba8d60727e4b47bb96a72e5e8609",
        },
        {
            "bytes": 168,
            "path": "generation_config.json",
            "sha256": "sha256:e452df866db1e3b43e75bcc5af31539e404e67c5bbc4e02a9a9c22357f1a70df",
        },
        {
            "bytes": 536_223_056,
            "path": "model.safetensors",
            "sha256": "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3",
        },
        {"bytes": len(reload_payload), "path": "reload-verification.json", "sha256": _sha(reload_payload)},
        {"bytes": 4_361, "path": "scriber-protection-policy.json", "sha256": POLICY_SHA},
        {
            "bytes": 662,
            "path": "special_tokens_map.json",
            "sha256": "sha256:2f7b0adf4fb469770bb1490e3e35df87b1dc578246c5e7e6fc76ecf33213a397",
        },
        {
            "bytes": 33_384_568,
            "path": "tokenizer.json",
            "sha256": "sha256:4667f2089529e8e7657cfb6d1c19910ae71ff5f28aa7ab2ff2763330affad795",
        },
        {
            "bytes": 4_689_074,
            "path": "tokenizer.model",
            "sha256": "sha256:1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c",
        },
        {
            "bytes": 1_155_404,
            "path": "tokenizer_config.json",
            "sha256": "sha256:c54055555aa322627af18187adffe5f6b9298fecb8b4a5837a5e1de1895dd693",
        },
        {
            "bytes": 5_969,
            "path": "training_args.bin",
            "sha256": "sha256:016b117c7745a3cecc9fda45a0a7e9de96872bf76e024ccf26210d3ad0e2382a",
        },
    ]
    inventory = {
        "schema_version": 1,
        "variant": "bf16",
        "format": "transformers_safetensors",
        "source_id": f"training-run:{FINGERPRINT}",
        "model_type": "gemma3_text",
        "declared_dtype": "bfloat16",
        "weight_dtype_counts": {"BF16": 236},
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": _tree(files),
        "files": files,
    }
    final_model = {
        "directory": FINAL_DIRECTORY,
        "path": f"/outputs/scriber-v2-hardcase-replay-v1/training/{FINAL_DIRECTORY}",
        "run_fingerprint": FINGERPRINT,
        "inventory": inventory,
        "reload_verification": reload_report,
    }
    training_report = {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "run_kind": "final",
        "run_fingerprint": FINGERPRINT,
        "bf16": True,
        "learning_rate": 0.000005,
        "max_steps": 640,
        "schedule": {"max_steps": 640},
        "early_stopping": False,
        "save_total_limit": 3,
        "warm_start_parent_checkpoint": {
            "relationship": "warm_start_parent_not_resume",
            "tree_sha256": PARENT_TREE,
        },
        "protection_policy": {"sha256": POLICY_SHA},
        "checkpoint_state": {
            "global_step": 640,
            "best_metric": None,
            "best_model_checkpoint": None,
            "last_checkpoint": "checkpoint-600",
            "surviving_checkpoints": [
                {
                    "checkpoint": "checkpoint-600",
                    "global_step": 600,
                    "run_fingerprint": FINGERPRINT,
                    "identity_sha256": "sha256:" + "3" * 64,
                    "trainer_state_sha256": "sha256:" + "4" * 64,
                }
            ],
        },
        "final_model": final_model,
        "training_completed": True,
    }
    report_payload = _canonical(training_report)
    result = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_job_result",
        "packet_tree_sha256": PACKET_TREE,
        "source_git_head": "5" * 40,
        "parent_checkpoint_tree_sha256": PARENT_TREE,
        "training_report_sha256": _sha(report_payload),
        "run_fingerprint": FINGERPRINT,
        "training_completed": True,
        "save_final": True,
        "resume_checkpoint": None,
        "auto_retry_allowed": False,
    }
    runtime = {
        "cuda_available": True,
        "bf16_supported": True,
        "device_count": 1,
        "gpu": "NVIDIA L4",
        "torch_version": "2.11.0+cu128",
        "cuda_runtime": "12.8",
    }
    packet_verification = {
        "source_git_head": "5" * 40,
        "source_git_tree": "6" * 40,
        "packet_tree_sha256": PACKET_TREE,
        "manifest_sha256": "sha256:" + "7" * 64,
        "launch_contract_sha256": LAUNCH_CONTRACT,
        "training": {"epochs": 1, "learning_rate": 0.000005, "bf16": True},
        "parent_checkpoint_tree_sha256": PARENT_TREE,
        "remote_result_uri": REMOTE_URI,
        "file_count": 23,
    }
    parent_files = [dict(item) for item in files]
    next(item for item in parent_files if item["path"] == "model.safetensors")["sha256"] = (
        "sha256:06af1566e1e63139b3b1a98caee2f737c8d0d0ac32e6f58b5b68fee80284b9ad"
    )
    next(item for item in parent_files if item["path"] == "reload-verification.json")["sha256"] = (
        "sha256:50ef95fed9ccae953a97e5f60858028ff7300003eb9039fc27ef009a87514a47"
    )
    parent_training_args = next(item for item in parent_files if item["path"] == "training_args.bin")
    parent_training_args["bytes"] = 6_033
    parent_training_args["sha256"] = "sha256:108082da2045ca3782fa9c0050c1a79f311700dcb7a86d079f5ef37d72aa5594"
    assert _sha(_canonical(parent_files)) == PARENT_TREE
    parent_binding = {
        "tree_sha256": PARENT_TREE,
        "files": parent_files,
        "file_count": len(parent_files),
        "byte_count": sum(int(item["bytes"]) for item in parent_files),
        "ignored_remote_metadata": ["checkpoint-inventory.json"],
    }
    receipt = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_launch_receipt",
        "job_id": JOB_ID,
        "packet_tree_sha256": PACKET_TREE,
        "launch_contract_sha256": LAUNCH_CONTRACT,
        "remote_result_uri": REMOTE_URI,
        "duplicate_retry_allowed": False,
        "job_inspection": {"id": JOB_ID, "exact_identity_verified": True, "stage": "SCHEDULING"},
    }
    receipt_payload = _canonical(receipt)
    terminal = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_terminal_evidence",
        "source": "hugging_face_jobs_rest_api",
        "observed_at_utc": "2026-08-01T15:30:00Z",
        "job_id": JOB_ID,
        "terminal_status": "COMPLETED",
        "packet_tree_sha256": PACKET_TREE,
        "remote_result_uri": REMOTE_URI,
        "launch_receipt_sha256": _sha(receipt_payload),
    }
    metadata = {
        "launch_receipt": root / "launch-receipt.json",
        "terminal_job": root / "terminal-job.json",
        "training_report": root / "training-report.json",
        "training_result": root / "v2-training-result.json",
        "runtime_verification": root / "runtime-verification.json",
        "packet_verification": root / "packet-verification.json",
        "parent_binding": root / "parent-binding.json",
        "final_inventory": root / "checkpoint-inventory.json",
        "reload_verification": root / "reload-verification.json",
        "remote_inventory": root / "remote-inventory.json",
    }
    _write(metadata["launch_receipt"], receipt)
    _write(metadata["terminal_job"], terminal)
    _write(metadata["training_report"], training_report)
    _write(metadata["training_result"], result)
    _write(metadata["runtime_verification"], runtime)
    _write(metadata["packet_verification"], packet_verification)
    _write(metadata["parent_binding"], parent_binding)
    inventory_payload = _write(metadata["final_inventory"], inventory)
    _write(metadata["reload_verification"], reload_report)
    remote_entries = [
        {
            "path": f"training/{FINAL_DIRECTORY}/{item['path']}",
            "size": item["bytes"],
            "xet_hash": "9" * 64,
            "type": "file",
        }
        for item in files
    ]
    remote_entries.extend(
        [
            {
                "path": f"training/{FINAL_DIRECTORY}/checkpoint-inventory.json",
                "size": len(inventory_payload),
                "xet_hash": "a" * 64,
                "type": "file",
            },
            {
                "path": "training-report.json",
                "size": len(report_payload),
                "xet_hash": "b" * 64,
                "type": "file",
            },
            {
                "path": "v2-training-result.json",
                "size": len(_canonical(result)),
                "xet_hash": "c" * 64,
                "type": "file",
            },
            {
                "path": "runtime-verification.json",
                "size": len(_canonical(runtime)),
                "xet_hash": "d" * 64,
                "type": "file",
            },
        ]
    )
    remote_inventory = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_remote_inventory",
        "source": "hugging_face_bucket_tree_api",
        "observed_at_utc": "2026-08-01T15:31:00Z",
        "remote_uri": REMOTE_URI,
        "recursive": True,
        "entries": sorted(remote_entries, key=lambda item: str(item["path"])),
    }
    _write(metadata["remote_inventory"], remote_inventory)
    return metadata


def test_postflight_binds_completed_job_and_final_model_without_model_bytes(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path)

    result = verify_v2_training_postflight(**evidence)

    assert result["kind"] == "scriber_hf_v2_training_completion_model_binding"
    assert result["status"] == "complete"
    assert result["job"] == {
        "id": JOB_ID,
        "terminal_status": "COMPLETED",
        "remote_result_uri": REMOTE_URI,
    }
    assert result["training"] == {
        "completed_steps": 640,
        "max_steps": 640,
        "learning_rate": 0.000005,
        "bf16": True,
    }
    assert result["model"]["directory"] == FINAL_DIRECTORY
    assert result["model"]["run_fingerprint"] == FINGERPRINT
    assert result["model"]["weight_files"] == [
        {
            "path": "model.safetensors",
            "bytes": 536_223_056,
            "sha256": "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3",
        }
    ]
    assert result["model"]["protection_policy_sha256"] == POLICY_SHA
    assert result["runtime"]["cuda_available"] is True
    assert result["runtime"]["bf16_supported"] is True
    assert result["verification"] == {
        "metadata_only": True,
        "model_downloaded": False,
        "external_mutation_performed": False,
    }


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _rebind_fingerprint(evidence: dict[str, Path], fingerprint: str) -> None:
    directory = "final-" + fingerprint.removeprefix("sha256:")[:16]
    reload_report = _read(evidence["reload_verification"])
    reload_report["run_fingerprint"] = fingerprint
    reload_payload = _write(evidence["reload_verification"], reload_report)
    inventory = _read(evidence["final_inventory"])
    inventory["source_id"] = f"training-run:{fingerprint}"
    files = inventory["files"]
    assert isinstance(files, list)
    reload_row = next(item for item in files if isinstance(item, dict) and item.get("path") == "reload-verification.json")
    reload_row["bytes"] = len(reload_payload)
    reload_row["sha256"] = _sha(reload_payload)
    inventory["tree_sha256"] = _tree(files)
    inventory_payload = _write(evidence["final_inventory"], inventory)
    report = _read(evidence["training_report"])
    report["run_fingerprint"] = fingerprint
    checkpoints = report["checkpoint_state"]
    assert isinstance(checkpoints, dict)
    survivors = checkpoints["surviving_checkpoints"]
    assert isinstance(survivors, list) and isinstance(survivors[0], dict)
    survivors[0]["run_fingerprint"] = fingerprint
    final_model = report["final_model"]
    assert isinstance(final_model, dict)
    final_model.update(
        {
            "directory": directory,
            "path": f"/outputs/scriber-v2-hardcase-replay-v1/training/{directory}",
            "run_fingerprint": fingerprint,
            "inventory": inventory,
            "reload_verification": reload_report,
        }
    )
    report_payload = _write(evidence["training_report"], report)
    result = _read(evidence["training_result"])
    result["run_fingerprint"] = fingerprint
    result["training_report_sha256"] = _sha(report_payload)
    result_payload = _write(evidence["training_result"], result)
    remote = _read(evidence["remote_inventory"])
    entries = remote["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        path = str(entry["path"])
        if path.startswith("training/final-"):
            entry["path"] = f"training/{directory}/" + path.split("/", 2)[2]
        if entry["path"] == f"training/{directory}/reload-verification.json":
            entry["size"] = len(reload_payload)
        elif entry["path"] == f"training/{directory}/checkpoint-inventory.json":
            entry["size"] = len(inventory_payload)
        elif entry["path"] == "training-report.json":
            entry["size"] = len(report_payload)
        elif entry["path"] == "v2-training-result.json":
            entry["size"] = len(result_payload)
    entries.sort(key=lambda item: str(item["path"]) if isinstance(item, dict) else "")
    _write(evidence["remote_inventory"], remote)


def test_postflight_rejects_a_self_consistent_different_run(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path)
    _rebind_fingerprint(evidence, "sha256:" + "e" * 64)

    with pytest.raises(V2TrainingPostflightError, match="run fingerprint"):
        verify_v2_training_postflight(**evidence)


def test_postflight_rejects_self_consistent_different_weights(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path)
    inventory = _read(evidence["final_inventory"])
    files = inventory["files"]
    assert isinstance(files, list)
    weight = next(item for item in files if isinstance(item, dict) and item.get("path") == "model.safetensors")
    weight["sha256"] = "sha256:" + "e" * 64
    inventory["tree_sha256"] = _tree(files)
    inventory_payload = _write(evidence["final_inventory"], inventory)
    report = _read(evidence["training_report"])
    final_model = report["final_model"]
    assert isinstance(final_model, dict)
    final_model["inventory"] = inventory
    report_payload = _write(evidence["training_report"], report)
    result = _read(evidence["training_result"])
    result["training_report_sha256"] = _sha(report_payload)
    result_payload = _write(evidence["training_result"], result)
    remote = _read(evidence["remote_inventory"])
    entries = remote["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        if entry["path"] == f"training/{FINAL_DIRECTORY}/checkpoint-inventory.json":
            entry["size"] = len(inventory_payload)
        elif entry["path"] == "training-report.json":
            entry["size"] = len(report_payload)
        elif entry["path"] == "v2-training-result.json":
            entry["size"] = len(result_payload)
    _write(evidence["remote_inventory"], remote)

    with pytest.raises(V2TrainingPostflightError, match="weight"):
        verify_v2_training_postflight(**evidence)


def _use_authoritative_hf_api_shapes(evidence: dict[str, Path]) -> None:
    claim_owner = "7ea1e14af8fa888249e00869d7f26d94981f64624d6f99e7fec03aeca6c33805"
    command = ["python", "-I", "-c", "verified bootstrap", PACKET_TREE, LAUNCH_CONTRACT, claim_owner]
    receipt = _read(evidence["launch_receipt"])
    receipt["owner_token"] = claim_owner
    inspection = receipt["job_inspection"]
    assert isinstance(inspection, dict)
    inspection["command_sha256"] = _sha(_canonical(command))
    _write(evidence["launch_receipt"], receipt)
    job = {
        "id": JOB_ID,
        "docker_image": "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be",
        "command": command,
        "arguments": [],
        "environment": {},
        "secrets": [],
        "flavor": "l4x1",
        "labels": {
            "name": "scriber-v2-hardcase-replay-v1",
            "project": "scriber-polishing",
            "campaign": "v2-hardcase-replay",
            "attempt": "attempt13",
            "role": "v2-training",
            "output-prefix": "attempt13-scriber-v2-hardcase-replay-v1",
            "launch-claim": claim_owner,
        },
        "volumes": [
            {
                "type": "bucket",
                "source": "Buttermilk03/scriber-polishing-private-runs",
                "mount_path": "/input/packet",
                "read_only": True,
                "path": "attempt13/packets/scriber-v2-hardcase-replay-v1",
            },
            {
                "type": "bucket",
                "source": "Buttermilk03/scriber-polishing-private-runs",
                "mount_path": "/input/parent",
                "read_only": True,
                "path": "attempt11/scriber-full-120k-v2/training/final-84458762af7b00ac",
            },
            {
                "type": "bucket",
                "source": "Buttermilk03/scriber-polishing-private-runs",
                "mount_path": "/outputs",
                "read_only": False,
                "path": "attempt13",
            },
        ],
        "status": {"stage": "COMPLETED"},
        "owner": {"name": "Buttermilk03"},
        "url": f"https://huggingface.co/jobs/Buttermilk03/{JOB_ID}",
    }
    evidence["terminal_job"].write_text(json.dumps([job], indent=2) + "\n", encoding="utf-8", newline="\n")
    wrapped = _read(evidence["remote_inventory"])
    entries = wrapped["entries"]
    assert isinstance(entries, list)
    raw_entries = []
    prefix = "attempt13/scriber-v2-hardcase-replay-v1/"
    for entry in entries:
        assert isinstance(entry, dict)
        raw_entries.append(
            {
                "path": prefix + str(entry["path"]),
                "size": entry["size"],
                "xet_hash": entry["xet_hash"],
            }
        )
    raw_entries.reverse()
    evidence["remote_inventory"].write_text(
        json.dumps(raw_entries, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_postflight_accepts_read_only_hf_job_and_bucket_api_snapshots(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path)
    _use_authoritative_hf_api_shapes(evidence)

    result = verify_v2_training_postflight(**evidence)

    assert result["job"]["terminal_status"] == "COMPLETED"
    assert result["verification"]["model_downloaded"] is False


def test_postflight_rejects_parent_file_list_not_bound_by_its_tree(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path)
    parent = _read(evidence["parent_binding"])
    files = parent["files"]
    assert isinstance(files, list) and isinstance(files[0], dict)
    files[0]["bytes"] = int(files[0]["bytes"]) + 1
    parent["byte_count"] = int(parent["byte_count"]) + 1
    _write(evidence["parent_binding"], parent)

    with pytest.raises(V2TrainingPostflightError, match="parent"):
        verify_v2_training_postflight(**evidence)


def test_cli_writes_one_canonical_completion_binding(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path / "evidence")
    _use_authoritative_hf_api_shapes(evidence)
    report = tmp_path / "completion-model-binding.json"
    script = Path(__file__).parents[1] / "scripts" / "verify_hf_v2_training_postflight.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--launch-receipt",
            str(evidence["launch_receipt"]),
            "--job-inspect",
            str(evidence["terminal_job"]),
            "--training-report",
            str(evidence["training_report"]),
            "--training-result",
            str(evidence["training_result"]),
            "--runtime-verification",
            str(evidence["runtime_verification"]),
            "--packet-verification",
            str(evidence["packet_verification"]),
            "--parent-binding",
            str(evidence["parent_binding"]),
            "--final-inventory",
            str(evidence["final_inventory"]),
            "--reload-verification",
            str(evidence["reload_verification"]),
            "--bucket-inventory",
            str(evidence["remote_inventory"]),
            "--report",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    value = json.loads(completed.stdout)
    assert value["status"] == "complete"
    assert report.read_bytes() == _canonical(value)


@pytest.mark.parametrize(
    "case",
    [
        "terminal_status",
        "completed_steps",
        "learning_rate",
        "runtime_bf16",
        "reload_status",
        "remote_weight_size",
        "packet_tree",
        "noncanonical_report",
    ],
)
def test_postflight_rejects_each_required_completion_signal(tmp_path: Path, case: str) -> None:
    evidence = _materialize_evidence(tmp_path)
    if case == "terminal_status":
        value = _read(evidence["terminal_job"])
        value["terminal_status"] = "ERROR"
        _write(evidence["terminal_job"], value)
    elif case == "completed_steps":
        value = _read(evidence["training_report"])
        state = value["checkpoint_state"]
        assert isinstance(state, dict)
        state["global_step"] = 639
        _write(evidence["training_report"], value)
    elif case == "learning_rate":
        value = _read(evidence["training_report"])
        value["learning_rate"] = 0.00001
        _write(evidence["training_report"], value)
    elif case == "runtime_bf16":
        value = _read(evidence["runtime_verification"])
        value["bf16_supported"] = False
        _write(evidence["runtime_verification"], value)
    elif case == "reload_status":
        value = _read(evidence["reload_verification"])
        value["status"] = "failed"
        _write(evidence["reload_verification"], value)
    elif case == "remote_weight_size":
        value = _read(evidence["remote_inventory"])
        entries = value["entries"]
        assert isinstance(entries, list)
        row = next(
            item
            for item in entries
            if isinstance(item, dict) and item.get("path") == f"training/{FINAL_DIRECTORY}/model.safetensors"
        )
        row["size"] = int(row["size"]) + 1
        _write(evidence["remote_inventory"], value)
    elif case == "packet_tree":
        value = _read(evidence["packet_verification"])
        value["packet_tree_sha256"] = "sha256:" + "e" * 64
        _write(evidence["packet_verification"], value)
    else:
        value = _read(evidence["training_report"])
        evidence["training_report"].write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(V2TrainingPostflightError):
        verify_v2_training_postflight(**evidence)


def test_cli_failure_leaves_no_success_report(tmp_path: Path) -> None:
    evidence = _materialize_evidence(tmp_path / "evidence")
    runtime = _read(evidence["runtime_verification"])
    runtime["cuda_available"] = False
    _write(evidence["runtime_verification"], runtime)
    report = tmp_path / "must-not-exist.json"
    script = Path(__file__).parents[1] / "scripts" / "verify_hf_v2_training_postflight.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--launch-receipt",
            str(evidence["launch_receipt"]),
            "--job-inspect",
            str(evidence["terminal_job"]),
            "--training-report",
            str(evidence["training_report"]),
            "--training-result",
            str(evidence["training_result"]),
            "--runtime-verification",
            str(evidence["runtime_verification"]),
            "--packet-verification",
            str(evidence["packet_verification"]),
            "--parent-binding",
            str(evidence["parent_binding"]),
            "--final-inventory",
            str(evidence["final_inventory"]),
            "--reload-verification",
            str(evidence["reload_verification"]),
            "--bucket-inventory",
            str(evidence["remote_inventory"]),
            "--report",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode != 0
    assert not report.exists()
