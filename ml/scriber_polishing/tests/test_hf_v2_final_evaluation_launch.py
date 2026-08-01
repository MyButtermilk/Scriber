from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from test_hf_v2_final_evaluation import _built_packet

from scriber_polishing.hf_v2_final_evaluation_launch import (
    V2FinalEvaluationLaunchError,
    launch_v2_final_evaluation_job,
    metadata_only_bucket_entries,
    validate_exact_claim_commit_metadata,
)


class FakeControlPlane:
    def __init__(self, packet: Path) -> None:
        self.packet = packet
        self.remote_packet = False
        self.upload_calls = 0
        self.snapshot_calls = 0
        self.claim_calls = 0
        self.submit_calls = 0
        self.claim_present = False
        self.output_entries: list[dict[str, object]] = []
        self.jobs: list[dict[str, object]] = []
        self.job_snapshots: list[list[dict[str, object]]] | None = None
        self.submitted_argv: list[str] | None = None

    def authoritative_jobs(self) -> list[dict[str, object]]:
        self.snapshot_calls += 1
        if self.job_snapshots is not None:
            return list(self.job_snapshots[self.snapshot_calls - 1])
        return list(self.jobs)

    def packet_entries(self) -> list[dict[str, object]]:
        if not self.remote_packet:
            return []
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

    def upload_packet_once(self, packet_dir: Path) -> None:
        assert packet_dir == self.packet
        self.upload_calls += 1
        if self.remote_packet:
            raise V2FinalEvaluationLaunchError("remote packet already exists")
        self.remote_packet = True

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]:
        assert packet_dir == self.packet
        return {
            path.relative_to(self.packet).as_posix(): "1" * 64
            for path in sorted(self.packet.rglob("*"))
            if path.is_file()
        }

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return list(self.output_entries)

    def claim_exists(self) -> bool:
        return self.claim_present

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.claim_calls += 1
        if self.claim_present:
            raise V2FinalEvaluationLaunchError("claim already exists")
        assert json.loads(payload)["kind"] == "scriber_hf_v2_final_evaluation_launch_claim"
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


class FakeBucketMetadataApi:
    def __init__(self, entries: list[object]) -> None:
        self.entries = entries
        self.calls: list[tuple[str, str | None, bool | None]] = []

    def list_bucket_tree(
        self,
        bucket_id: str,
        prefix: str | None = None,
        *,
        recursive: bool | None = None,
    ) -> list[object]:
        self.calls.append((bucket_id, prefix, recursive))
        return list(self.entries)

    def download_bucket_files(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("metadata verification must never read bucket file content")


def test_bucket_inventory_uses_only_recursive_hf_metadata() -> None:
    prefix = "attempt14/packets/scriber-v2-hardcase-replay-final-evaluation-v1"
    api = FakeBucketMetadataApi(
        [
            SimpleNamespace(type="directory", path=prefix),
            SimpleNamespace(
                type="file",
                path=f"{prefix}/launch-contract.json",
                size=123,
                xet_hash="a" * 64,
            ),
        ]
    )

    entries = metadata_only_bucket_entries(
        api,
        remote_uri=(
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            f"{prefix}"
        ),
    )

    assert api.calls == [
        ("Buttermilk03/scriber-polishing-private-runs", prefix, True)
    ]
    assert entries == [
        {
            "type": "file",
            "path": f"{prefix}/launch-contract.json",
            "size": 123,
            "xet_hash": "a" * 64,
        }
    ]


def test_bucket_inventory_fails_closed_on_ambiguous_metadata() -> None:
    prefix = "attempt14/packets/scriber-v2-hardcase-replay-final-evaluation-v1"
    api = FakeBucketMetadataApi(
        [
            SimpleNamespace(
                type="file",
                path=f"{prefix}/launch-contract.json",
                size=123,
                xet_hash=None,
            )
        ]
    )

    with pytest.raises(V2FinalEvaluationLaunchError, match="metadata"):
        metadata_only_bucket_entries(
            api,
            remote_uri=(
                "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
                f"{prefix}"
            ),
        )


def test_claim_readback_is_verified_from_exact_commit_metadata_only() -> None:
    payload = b'{"claim":"attempt14"}\n'
    metadata = [
        SimpleNamespace(
            path="claims/v2-final-evaluation/attempt14.json",
            size=len(payload),
            blob_id="031b93b7e295ecf4862c9662d3cce34cee4653c5",
            lfs=None,
            xet_hash=None,
        )
    ]

    assert (
        validate_exact_claim_commit_metadata(
            metadata,
            claim_path="claims/v2-final-evaluation/attempt14.json",
            payload=payload,
        )
        == "031b93b7e295ecf4862c9662d3cce34cee4653c5"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "claims/v2-final-evaluation/other.json"),
        ("size", 1),
        ("blob_id", "0" * 40),
        ("lfs", SimpleNamespace(size=22, sha256="0" * 64)),
        ("xet_hash", "0" * 64),
    ],
)
def test_claim_metadata_verification_fails_closed(field: str, value: object) -> None:
    payload = b'{"claim":"attempt14"}\n'
    row = SimpleNamespace(
        path="claims/v2-final-evaluation/attempt14.json",
        size=len(payload),
        blob_id="031b93b7e295ecf4862c9662d3cce34cee4653c5",
        lfs=None,
        xet_hash=None,
    )
    setattr(row, field, value)

    with pytest.raises(V2FinalEvaluationLaunchError, match="claim commit metadata"):
        validate_exact_claim_commit_metadata(
            [row],
            claim_path="claims/v2-final-evaluation/attempt14.json",
            payload=payload,
        )


def test_launcher_uploads_exactly_claims_once_and_submits_once(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    receipt_path = tmp_path / "launch-receipt.json"

    receipt = launch_v2_final_evaluation_job(control, packet_dir=packet, receipt_path=receipt_path)

    assert control.upload_calls == 1
    assert control.snapshot_calls == 3
    assert control.claim_calls == 1
    assert control.submit_calls == 1
    assert receipt["job_id"] == "0123456789abcdef01234567"
    assert receipt["packet_upload"]["performed"] is True
    assert receipt["packet_upload"]["downloaded_for_verification"] is False
    assert receipt["duplicate_retry_allowed"] is False
    assert receipt_path.exists()
    assert control.submitted_argv is not None
    assert control.submitted_argv[:4] == ["hf", "jobs", "run", "--detach"]
    assert control.submitted_argv.count("--detach") == 1
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts/hf_v2_final_evaluation_launch_receipt_schema.json").read_bytes()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt)


def test_launcher_reuses_only_an_exact_existing_remote_packet(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    control.remote_packet = True

    receipt = launch_v2_final_evaluation_job(
        control, packet_dir=packet, receipt_path=tmp_path / "launch-receipt.json"
    )

    assert control.upload_calls == 0
    assert receipt["packet_upload"]["performed"] is False
    assert control.submit_calls == 1


@pytest.mark.parametrize("failure", ["active", "duplicate", "output", "claim"])
def test_launcher_fails_before_upload_or_submission_for_duplicate_signals(
    tmp_path: Path, failure: str
) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    if failure == "active":
        control.jobs = [{"id": "f" * 24, "status": {"stage": "RUNNING"}, "labels": {}}]
    elif failure == "duplicate":
        control.jobs = [
            {
                "id": "f" * 24,
                "status": {"stage": "COMPLETED"},
                "labels": {"campaign": "v2-final-evaluation", "attempt": "attempt14"},
            }
        ]
    elif failure == "output":
        control.output_entries = [{"type": "file", "path": "final/partial.json", "size": 1}]
    else:
        control.claim_present = True

    with pytest.raises(V2FinalEvaluationLaunchError):
        launch_v2_final_evaluation_job(
            control, packet_dir=packet, receipt_path=tmp_path / "launch-receipt.json"
        )

    assert control.upload_calls == 0
    assert control.submit_calls == 0


def test_post_claim_job_race_retains_claim_and_forbids_submission(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    control.job_snapshots = [
        [],
        [],
        [{"id": "f" * 24, "status": {"stage": "RUNNING"}, "labels": {}}],
    ]

    with pytest.raises(V2FinalEvaluationLaunchError):
        launch_v2_final_evaluation_job(
            control, packet_dir=packet, receipt_path=tmp_path / "launch-receipt.json"
        )

    assert control.claim_calls == 1
    assert control.claim_present is True
    assert control.submit_calls == 0


def test_preexisting_receipt_blocks_every_external_action(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    receipt = tmp_path / "launch-receipt.json"
    receipt.write_text("existing", encoding="utf-8")

    with pytest.raises(V2FinalEvaluationLaunchError, match="receipt"):
        launch_v2_final_evaluation_job(control, packet_dir=packet, receipt_path=receipt)

    assert control.snapshot_calls == 0
    assert control.upload_calls == 0
    assert control.claim_calls == 0
    assert control.submit_calls == 0


def test_explicit_preclaim_recovery_reuses_exact_reservation_and_remote_packet(
    tmp_path: Path,
) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    control.remote_packet = True
    receipt = tmp_path / "launch-receipt.json"
    receipt.write_bytes(
        b'{"kind":"scriber_hf_v2_final_evaluation_launch_receipt_reservation",'
        b'"schema_version":1}\n'
    )

    result = launch_v2_final_evaluation_job(
        control,
        packet_dir=packet,
        receipt_path=receipt,
        resume_preclaim_reservation=True,
    )

    assert control.upload_calls == 0
    assert control.snapshot_calls == 3
    assert control.claim_calls == 1
    assert control.submit_calls == 1
    assert result["packet_upload"]["performed"] is False
    assert result["packet_upload"]["downloaded_for_verification"] is False
    assert json.loads(receipt.read_bytes())["job_id"] == "0123456789abcdef01234567"


@pytest.mark.parametrize("failure", ["active", "duplicate", "output", "claim"])
def test_preclaim_recovery_checks_all_duplicate_signals_before_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    control.remote_packet = True
    receipt = tmp_path / "launch-receipt.json"
    reservation = (
        b'{"kind":"scriber_hf_v2_final_evaluation_launch_receipt_reservation",'
        b'"schema_version":1}\n'
    )
    receipt.write_bytes(reservation)
    if failure == "active":
        control.jobs = [{"id": "f" * 24, "status": {"stage": "RUNNING"}, "labels": {}}]
    elif failure == "duplicate":
        control.jobs = [
            {
                "id": "f" * 24,
                "status": {"stage": "COMPLETED"},
                "labels": {"campaign": "v2-final-evaluation", "attempt": "attempt14"},
            }
        ]
    elif failure == "output":
        control.output_entries = [{"type": "file", "path": "final/partial.json", "size": 1}]
    else:
        control.claim_present = True

    with pytest.raises(V2FinalEvaluationLaunchError):
        launch_v2_final_evaluation_job(
            control,
            packet_dir=packet,
            receipt_path=receipt,
            resume_preclaim_reservation=True,
        )

    assert receipt.read_bytes() == reservation
    assert control.upload_calls == 0
    assert control.claim_calls == 0
    assert control.submit_calls == 0


def test_preclaim_recovery_never_reuploads_a_missing_packet(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    receipt = tmp_path / "launch-receipt.json"
    reservation = (
        b'{"kind":"scriber_hf_v2_final_evaluation_launch_receipt_reservation",'
        b'"schema_version":1}\n'
    )
    receipt.write_bytes(reservation)

    with pytest.raises(V2FinalEvaluationLaunchError, match="recovery.*packet"):
        launch_v2_final_evaluation_job(
            control,
            packet_dir=packet,
            receipt_path=receipt,
            resume_preclaim_reservation=True,
        )

    assert receipt.read_bytes() == reservation
    assert control.upload_calls == 0
    assert control.claim_calls == 0
    assert control.submit_calls == 0


def test_preclaim_recovery_rejects_a_modified_reservation_before_remote_reads(
    tmp_path: Path,
) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    receipt = tmp_path / "launch-receipt.json"
    receipt.write_text("modified", encoding="utf-8")

    with pytest.raises(V2FinalEvaluationLaunchError, match="reservation"):
        launch_v2_final_evaluation_job(
            control,
            packet_dir=packet,
            receipt_path=receipt,
            resume_preclaim_reservation=True,
        )

    assert control.snapshot_calls == 0
    assert control.upload_calls == 0
    assert control.claim_calls == 0
    assert control.submit_calls == 0


def test_read_only_preflight_failure_does_not_leave_receipt_reservation(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    control = FakeControlPlane(packet)
    receipt = tmp_path / "launch-receipt.json"

    def fail_read() -> list[dict[str, object]]:
        raise V2FinalEvaluationLaunchError("transient all-jobs read failed")

    control.authoritative_jobs = fail_read  # type: ignore[method-assign]
    with pytest.raises(V2FinalEvaluationLaunchError, match="transient"):
        launch_v2_final_evaluation_job(control, packet_dir=packet, receipt_path=receipt)

    assert not receipt.exists()
    assert control.upload_calls == 0
    assert control.claim_calls == 0
    assert control.submit_calls == 0
