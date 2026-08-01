from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_hf_v2_release_job import _fake_source_materializer, _plan_and_binding

from scriber_polishing.hf_v2_release_job import (
    package_v2_release_job,
)
from scriber_polishing.hf_v2_release_launch import (
    V2_RELEASE_PACKET_REMOTE_RELATIVE,
    V2ReleaseLaunchError,
    launch_v2_release_job,
    validate_v2_release_claim_commit_metadata,
)
from scriber_polishing.v2_q8_bf16_release import (
    V2_RELEASE_CLAIM_PATH,
    canonical_json_bytes,
)


class FakeReleaseControlPlane:
    def __init__(self, packet: Path) -> None:
        self.packet = packet
        self.snapshot_calls = 0
        self.xet_hash_calls = 0
        self.claim_calls = 0
        self.submit_calls = 0
        self.claim_present = False
        self.output_entries: list[dict[str, object]] = []
        self.jobs: list[dict[str, object]] = []
        self.job_snapshots: list[list[dict[str, object]]] | None = None
        self.submitted_argv: list[str] | None = None
        self.remote_xet_override: tuple[str, str] | None = None

    def _local_xet_hashes(self) -> dict[str, str]:
        return {
            path.relative_to(self.packet).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(self.packet.rglob("*"))
            if path.is_file()
        }

    def authoritative_jobs(self) -> list[dict[str, object]]:
        self.snapshot_calls += 1
        if self.job_snapshots is not None:
            return list(self.job_snapshots[self.snapshot_calls - 1])
        return list(self.jobs)

    def packet_entries(self) -> list[dict[str, object]]:
        hashes = self._local_xet_hashes()
        if self.remote_xet_override is not None:
            relative, xet_hash = self.remote_xet_override
            hashes[relative] = xet_hash
        return [
            {
                "type": "file",
                "path": (
                    V2_RELEASE_PACKET_REMOTE_RELATIVE
                    + "/"
                    + path.relative_to(self.packet).as_posix()
                ),
                "size": path.stat().st_size,
                "xet_hash": hashes[path.relative_to(self.packet).as_posix()],
            }
            for path in sorted(self.packet.rglob("*"))
            if path.is_file()
        ]

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]:
        assert packet_dir == self.packet
        self.xet_hash_calls += 1
        return self._local_xet_hashes()

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return list(self.output_entries)

    def claim_exists(self) -> bool:
        return self.claim_present

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.claim_calls += 1
        if self.claim_present:
            raise V2ReleaseLaunchError("claim already exists")
        assert json.loads(payload)["kind"] == "scriber_hf_v2_q8_bf16_release_launch_claim"
        self.claim_present = True
        return "4" * 40

    def submit_detached_once(self, argv: list[str]) -> str:
        self.submit_calls += 1
        self.submitted_argv = list(argv)
        return "Detached job 0123456789abcdef01234567 submitted successfully."

    def inspect_submitted_job(self, job_id: str):
        assert self.submitted_argv is not None
        argv = self.submitted_argv
        image_index = next(
            index for index, item in enumerate(argv) if item.startswith("pytorch/pytorch@sha256:")
        )
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
        return row, api_row, f"https://huggingface.co/jobs/Buttermilk03/{job_id}"


@pytest.fixture
def release_packet(tmp_path: Path) -> Path:
    plan, binding = _plan_and_binding()
    plan_path = tmp_path / "plan.json"
    binding_path = tmp_path / "binding.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    binding_path.write_bytes(canonical_json_bytes(binding))
    packet = tmp_path / "packet"
    package_v2_release_job(
        repository_root=Path(__file__).parents[3],
        plan_path=plan_path,
        evaluation_binding_path=binding_path,
        output_dir=packet,
        source_materializer=_fake_source_materializer,
    )
    return packet


def test_attempt15_launcher_claims_once_and_submits_once(
    release_packet: Path, tmp_path: Path
) -> None:
    control = FakeReleaseControlPlane(release_packet)
    receipt = launch_v2_release_job(
        control,
        packet_dir=release_packet,
        receipt_path=tmp_path / "receipt.json",
    )

    assert control.snapshot_calls == 3
    assert control.xet_hash_calls == 1
    assert control.claim_calls == 1
    assert control.submit_calls == 1
    assert receipt["job_id"] == "0123456789abcdef01234567"
    assert receipt["duplicate_retry_allowed"] is False
    assert receipt["authoritative_snapshot_count"] == 3
    assert control.submitted_argv is not None
    assert "attempt=attempt15" in control.submitted_argv
    assert "campaign=v2-q8-bf16-release" in control.submitted_argv
    assert "role=v2-quantization" in control.submitted_argv


@pytest.mark.parametrize("failure", ["active", "prior", "output", "claim"])
def test_attempt15_launcher_blocks_every_duplicate_signal_before_submit(
    release_packet: Path, tmp_path: Path, failure: str
) -> None:
    control = FakeReleaseControlPlane(release_packet)
    if failure == "active":
        control.jobs = [{"id": "f" * 24, "status": {"stage": "RUNNING"}}]
    elif failure == "prior":
        control.jobs = [
            {
                "id": "f" * 24,
                "status": {"stage": "COMPLETED"},
                "labels": {"campaign": "v2-q8-bf16-release"},
            }
        ]
    elif failure == "output":
        control.output_entries = [{"type": "file", "path": "quantization/result.json", "size": 1}]
    else:
        control.claim_present = True

    with pytest.raises(V2ReleaseLaunchError):
        launch_v2_release_job(
            control,
            packet_dir=release_packet,
            receipt_path=tmp_path / "receipt.json",
        )
    assert control.submit_calls == 0


def test_attempt15_ambiguous_submit_retains_claim_and_never_retries(
    release_packet: Path, tmp_path: Path
) -> None:
    control = FakeReleaseControlPlane(release_packet)

    def fail_once(_argv: list[str]) -> str:
        control.submit_calls += 1
        raise TimeoutError("unknown whether HF accepted the request")

    control.submit_detached_once = fail_once  # type: ignore[method-assign]
    with pytest.raises(V2ReleaseLaunchError, match="claim retained"):
        launch_v2_release_job(
            control,
            packet_dir=release_packet,
            receipt_path=tmp_path / "receipt.json",
        )
    assert control.claim_present is True
    assert control.submit_calls == 1
    assert not (tmp_path / "receipt.json").exists()


def test_attempt15_post_claim_race_retains_claim_without_submit(
    release_packet: Path, tmp_path: Path
) -> None:
    control = FakeReleaseControlPlane(release_packet)
    control.job_snapshots = [[], [], [{"id": "f" * 24, "status": {"stage": "RUNNING"}}]]

    with pytest.raises(V2ReleaseLaunchError):
        launch_v2_release_job(
            control,
            packet_dir=release_packet,
            receipt_path=tmp_path / "receipt.json",
        )
    assert control.claim_present is True
    assert control.claim_calls == 1
    assert control.submit_calls == 0


def test_attempt15_remote_packet_xet_mismatch_blocks_before_claim(
    release_packet: Path, tmp_path: Path
) -> None:
    control = FakeReleaseControlPlane(release_packet)
    relative = next(iter(control._local_xet_hashes()))
    control.remote_xet_override = (relative, "f" * 64)

    with pytest.raises(V2ReleaseLaunchError, match="Xet inventory differs"):
        launch_v2_release_job(
            control,
            packet_dir=release_packet,
            receipt_path=tmp_path / "receipt.json",
        )

    assert control.xet_hash_calls == 1
    assert control.claim_calls == 0
    assert control.submit_calls == 0


def _git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def test_attempt15_claim_is_verified_from_exact_commit_metadata_only() -> None:
    payload = b'{"claim":"attempt15"}\n'
    commit = "4" * 40
    result = validate_v2_release_claim_commit_metadata(
        {"sha": commit, "private": True},
        [
            {
                "path": V2_RELEASE_CLAIM_PATH,
                "size": len(payload),
                "blob_id": _git_blob_oid(payload),
                "lfs": None,
                "last_commit": {"oid": commit},
            }
        ],
        expected_commit=commit,
        payload=payload,
    )

    assert result["metadata_only"] is True
    assert result["storage_identity"] == {
        "kind": "git_blob",
        "oid": _git_blob_oid(payload),
    }


@pytest.mark.parametrize(
    ("repository_info", "path_entry", "error"),
    [
        (
            {"sha": "5" * 40, "private": True},
            {},
            "exact-revision metadata differs",
        ),
        (
            {"sha": "4" * 40, "private": True},
            {"path": V2_RELEASE_CLAIM_PATH, "size": 1},
            "path or size metadata differs",
        ),
        (
            {"sha": "4" * 40, "private": True},
            {
                "path": V2_RELEASE_CLAIM_PATH,
                "size": len(b'{"claim":"attempt15"}\n'),
                "blob_id": "5" * 40,
                "lfs": None,
                "last_commit": {"oid": "4" * 40},
            },
            "Git blob metadata differs",
        ),
    ],
)
def test_attempt15_claim_metadata_mismatch_fails_closed(
    repository_info: dict[str, object],
    path_entry: dict[str, object],
    error: str,
) -> None:
    payload = b'{"claim":"attempt15"}\n'
    with pytest.raises(V2ReleaseLaunchError, match=error):
        validate_v2_release_claim_commit_metadata(
            repository_info,
            [path_entry],
            expected_commit="4" * 40,
            payload=payload,
        )


def test_attempt15_launcher_has_no_remote_byte_readback() -> None:
    source = (
        Path(__file__).parents[1] / "scripts" / "launch_hf_v2_release_job.py"
    ).read_text(encoding="utf-8")

    assert "hf_hub_download" not in source
    assert '"buckets", "cp"' not in source
    assert ".list_bucket_tree(" in source
    assert ".get_paths_info(" in source
