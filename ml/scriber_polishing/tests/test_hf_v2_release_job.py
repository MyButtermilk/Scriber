from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_v2_q8_bf16_release import _accepted_evaluation_binding

from scriber_polishing.hf_v2_release_job import (
    V2ReleaseJobError,
    package_v2_release_job,
    verify_v2_release_job_packet,
)
from scriber_polishing.v2_q8_bf16_release import (
    build_v2_release_plan,
    canonical_json_bytes,
    sha256_bytes,
)


def _file_record(path: Path, root: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _fake_source_materializer(_repository: Path, destination: Path):
    project = Path(__file__).parents[1]
    sources = {
        "ml/scriber_polishing/src/scriber_polishing/v2_q8_bf16_release.py": (
            project / "src/scriber_polishing/v2_q8_bf16_release.py"
        ),
        "ml/scriber_polishing/src/scriber_polishing/quantization.py": (
            project / "src/scriber_polishing/quantization.py"
        ),
        "ml/scriber_polishing/scripts/run_v2_q8_bf16_quantization.py": (
            project / "scripts/run_v2_q8_bf16_quantization.py"
        ),
        "ml/scriber_polishing/configs/training-policy-gemma-lexical-v1.json": (
            project / "configs/training-policy-gemma-lexical-v1.json"
        ),
    }
    records: list[dict[str, object]] = []
    for relative, source in sources.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        records.append(_file_record(target, destination.parent))
    records.sort(key=lambda row: str(row["path"]))
    return "2" * 40, "3" * 40, records


def _plan_and_binding() -> tuple[dict[str, object], dict[str, object]]:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=sha256_bytes(canonical_json_bytes(binding)),
        source_git_head="2" * 40,
    )
    return plan, binding


def test_attempt15_packet_mounts_v2_and_verified_v19_without_local_model_copy(
    tmp_path: Path,
) -> None:
    plan, binding = _plan_and_binding()
    plan_path = tmp_path / "plan.json"
    binding_path = tmp_path / "binding.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    binding_path.write_bytes(canonical_json_bytes(binding))

    built = package_v2_release_job(
        repository_root=Path(__file__).parents[3],
        plan_path=plan_path,
        evaluation_binding_path=binding_path,
        output_dir=tmp_path / "packet",
        source_materializer=_fake_source_materializer,
    )
    packet = Path(built["packet_dir"])
    verified = verify_v2_release_job_packet(
        packet,
        expected_packet_tree_sha256=str(built["packet_tree_sha256"]),
        expected_manifest_sha256=str(built["manifest_sha256"]),
        expected_launch_contract_sha256=str(built["launch_contract_sha256"]),
    )
    contract = json.loads((packet / "launch-contract.json").read_bytes())

    assert verified["status"] == "ready"
    assert contract["attempt"] == "attempt15"
    assert contract["model_remote_uri"] == plan["source_model"]["remote_uri"]
    assert contract["llama_cpp_remote_uri"].endswith(
        "/toolchains/llama-cpp-b10156-linux-cuda-v1/v19/final"
    )
    assert contract["output_mount_uri"] == plan["launch"]["output_uri"]
    assert contract["remote_output_absence_required"] is True
    assert contract["active_job_snapshots_required"] == 3
    assert contract["external_job_started"] is False
    assert contract["secret_names"] == []
    assert "Q4_K_M" not in json.dumps(contract)
    packet_paths = {
        path.relative_to(packet).as_posix()
        for path in packet.rglob("*")
        if path.is_file()
    }
    assert not any(path.endswith("model.safetensors") for path in packet_paths)


def test_attempt15_packet_rejects_an_unaccepted_terra_gate(tmp_path: Path) -> None:
    plan, binding = _plan_and_binding()
    binding["terra_judge"]["gate"]["decision"] = "failed"
    plan_path = tmp_path / "plan.json"
    binding_path = tmp_path / "binding.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    binding_path.write_bytes(canonical_json_bytes(binding))

    with pytest.raises(V2ReleaseJobError, match="binding|Terra|plan"):
        package_v2_release_job(
            repository_root=Path(__file__).parents[3],
            plan_path=plan_path,
            evaluation_binding_path=binding_path,
            output_dir=tmp_path / "packet",
            source_materializer=_fake_source_materializer,
        )
