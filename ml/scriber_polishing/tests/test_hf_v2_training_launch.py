from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_hf_v2_training_job import PRODUCTION_POLICY_PATH, _source_repository, _write_mix

from scriber_polishing.hf_v2_training_job import package_v2_training_job
from scriber_polishing.hf_v2_training_launch import (
    V2LaunchError,
    launch_v2_training_job,
    parse_hf_cli_single_job_inspection,
)


class FakeControlPlane:
    def __init__(self, packet: Path) -> None:
        self.packet = packet
        self.snapshot_calls = 0
        self.submit_calls = 0
        self.claim_calls = 0
        self.claim_present = False
        self.jobs: list[dict[str, object]] = []
        self.output_entries: list[dict[str, object]] = []
        self.submitted_argv: list[str] | None = None
        self.job_snapshots: list[list[dict[str, object]]] | None = None

    def authoritative_jobs(self) -> list[dict[str, object]]:
        self.snapshot_calls += 1
        if self.job_snapshots is not None:
            return list(self.job_snapshots[self.snapshot_calls - 1])
        return list(self.jobs)

    def packet_entries(self) -> list[dict[str, object]]:
        return [
            {
                "type": "file",
                "path": path.relative_to(self.packet).as_posix(),
                "size": path.stat().st_size,
                "xet_hash": "1" * 64,
            }
            for path in sorted(self.packet.rglob("*"))
            if path.is_file()
        ]

    def packet_metadata(self, relative: str) -> bytes:
        return (self.packet / relative).read_bytes()

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return list(self.output_entries)

    def claim_exists(self) -> bool:
        return self.claim_present

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.claim_calls += 1
        if self.claim_present:
            raise V2LaunchError("claim already exists")
        assert json.loads(payload)["kind"] == "scriber_hf_v2_training_launch_claim"
        self.claim_present = True
        return "2" * 40

    def submit_detached_once(self, argv: list[str]) -> str:
        self.submit_calls += 1
        self.submitted_argv = list(argv)
        return "Detached job 0123456789abcdef01234567 submitted successfully."

    def inspect_submitted_job(self, job_id: str):
        assert self.submitted_argv is not None
        argv = self.submitted_argv
        image_index = next(index for index, item in enumerate(argv) if item.startswith("pytorch/pytorch@sha256:"))
        labels: dict[str, str] = {}
        volumes: list[str] = []
        index = 0
        while index < image_index:
            if argv[index] == "--label":
                key, value = argv[index + 1].split("=", 1)
                labels[key] = value
                index += 2
            elif argv[index] == "-v":
                volumes.append(argv[index + 1])
                index += 2
            else:
                index += 1
        row = {
            "id": job_id,
            "docker_image": argv[image_index],
            "flavor": argv[argv.index("--flavor") + 1],
            "labels": labels,
            "volumes": volumes,
            "command": argv[image_index + 1 :],
            "arguments": [],
            "environment": {},
            "secrets": {},
            "owner": {"name": "Buttermilk03"},
            "status": {"stage": "SCHEDULING"},
        }
        api_row = dict(row)
        api_row["volumes"] = [
            {
                "type": "bucket",
                "source": source.removeprefix("hf://buckets/"),
                "mount_path": mount_path,
                "path": None,
                "read_only": mode == "ro",
            }
            for source_and_mount, mode in (volume.rsplit(":", 1) for volume in volumes)
            for source, mount_path in [source_and_mount.rsplit(":", 1)]
        ]
        return row, api_row, "https://huggingface.co/jobs/0123456789abcdef01234567"


@pytest.fixture
def packet(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _source_repository(repository)
    mix = tmp_path / "mix"
    _write_mix(mix)
    destination = tmp_path / "packet"
    result = package_v2_training_job(
        repository_root=repository,
        dataset_root=mix,
        policy_path=PRODUCTION_POLICY_PATH,
        output_dir=destination,
    )
    return destination, result


def test_single_shot_launcher_checks_twice_claims_once_and_submits_once(
    packet: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    packet_root, _packet_result = packet
    control = FakeControlPlane(packet_root)
    receipt_path = tmp_path / "receipt.json"

    receipt = launch_v2_training_job(control, packet_dir=packet_root, receipt_path=receipt_path)

    assert control.snapshot_calls == 3
    assert control.claim_calls == 1
    assert control.submit_calls == 1
    assert receipt["job_id"] == "0123456789abcdef01234567"
    assert receipt["duplicate_retry_allowed"] is False
    assert receipt_path.exists()
    assert control.submitted_argv is not None and control.submitted_argv[:4] == ["hf", "jobs", "run", "--detach"]
    assert "--name" not in control.submitted_argv
    assert "name=scriber-v2-hardcase-replay-v1" in control.submitted_argv
    assert "attempt=attempt13" in control.submitted_argv
    assert "output-prefix=attempt13-scriber-v2-hardcase-replay-v1" in control.submitted_argv
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "hf_v2_training_launch_receipt_schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt)


@pytest.mark.parametrize("failure", ["active", "output", "claim"])
def test_launcher_fails_before_submission_for_every_duplicate_signal(
    packet: tuple[Path, dict[str, object]], tmp_path: Path, failure: str
) -> None:
    packet_root, _packet_result = packet
    control = FakeControlPlane(packet_root)
    if failure == "active":
        control.jobs = [{"id": "f" * 24, "status": {"stage": "RUNNING"}}]
    elif failure == "output":
        control.output_entries = [{"type": "file", "path": "training/model.safetensors", "size": 1}]
    else:
        control.claim_present = True

    with pytest.raises(V2LaunchError):
        launch_v2_training_job(control, packet_dir=packet_root, receipt_path=tmp_path / "receipt.json")

    assert control.submit_calls == 0


def test_cli_1_9_inspection_requires_one_list_item() -> None:
    row = parse_hf_cli_single_job_inspection('[{"id":"0123456789abcdef01234567"}]')
    assert row == {"id": "0123456789abcdef01234567"}
    with pytest.raises(V2LaunchError, match="exactly one"):
        parse_hf_cli_single_job_inspection('{"id":"0123456789abcdef01234567"}')
    with pytest.raises(V2LaunchError, match="exactly one"):
        parse_hf_cli_single_job_inspection("[]")


def test_post_claim_job_race_retains_claim_and_forbids_submission(
    packet: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    packet_root, _packet_result = packet
    control = FakeControlPlane(packet_root)
    control.job_snapshots = [
        [],
        [],
        [{"id": "f" * 24, "status": {"stage": "RUNNING"}}],
    ]

    with pytest.raises(V2LaunchError):
        launch_v2_training_job(control, packet_dir=packet_root, receipt_path=tmp_path / "receipt.json")

    assert control.snapshot_calls == 3
    assert control.claim_calls == 1
    assert control.claim_present is True
    assert control.submit_calls == 0
