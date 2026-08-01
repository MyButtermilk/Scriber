"""Upload exactly and launch one permanently claimed attempt14 HF Job.

This is intentionally single-shot. It retains the receipt reservation and any
permanent claim after an ambiguous failure and never retries submission.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

from hf_xet import hash_files
from huggingface_hub import CommitOperationAdd, HfApi

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_launch_gate import (  # noqa: E402
    HF_CLI_PREFIX,
    parse_hf_jobs_json,
    reconcile_authoritative_hf_job_snapshots,
    require_claim_never_existed,
    require_hf_control_plane_runtime,
)
from scriber_polishing.hf_v2_final_evaluation import (  # noqa: E402
    V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI,
    V2_EVALUATION_PACKET_REMOTE_URI,
)
from scriber_polishing.hf_v2_final_evaluation_launch import (  # noqa: E402
    V2_EVALUATION_CLAIM_PATH,
    V2_EVALUATION_CLAIM_REPOSITORY_ID,
    V2_EVALUATION_CLAIM_REPOSITORY_TYPE,
    V2FinalEvaluationLaunchError,
    launch_v2_final_evaluation_job,
    metadata_only_bucket_entries,
    validate_exact_claim_commit_metadata,
)


class HuggingFaceV2EvaluationControlPlane:
    def __init__(self, api: HfApi | None = None) -> None:
        self.api = api or HfApi()

    @staticmethod
    def _run_cli(arguments: list[str], *, timeout_seconds: int = 120) -> bytes:
        completed = subprocess.run(
            [*HF_CLI_PREFIX, "hf", *arguments],
            check=False,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0 or completed.stderr.strip():
            detail = (completed.stderr.strip() or completed.stdout.strip())[-2_000:].decode(
                "utf-8", errors="replace"
            )
            raise V2FinalEvaluationLaunchError("HF attempt14 control-plane command failed closed: " + detail)
        return completed.stdout

    def authoritative_jobs(self) -> list[dict[str, object]]:
        api_jobs = list(self.api.list_jobs(namespace="Buttermilk03"))
        cli_jobs = parse_hf_jobs_json(
            self._run_cli(["jobs", "ps", "-a", "--format", "json"]).decode("utf-8", errors="strict")
        )
        return reconcile_authoritative_hf_job_snapshots(api_jobs, cli_jobs)

    def packet_entries(self) -> list[dict[str, object]]:
        return metadata_only_bucket_entries(
            self.api,
            remote_uri=V2_EVALUATION_PACKET_REMOTE_URI,
        )

    def upload_packet_once(self, packet_dir: Path) -> None:
        completed = subprocess.run(
            [
                *HF_CLI_PREFIX,
                "hf",
                "buckets",
                "sync",
                str(packet_dir),
                V2_EVALUATION_PACKET_REMOTE_URI,
                "--no-delete",
                "--quiet",
            ],
            check=False,
            capture_output=True,
            shell=False,
            timeout=600,
        )
        if completed.returncode != 0:
            detail = (completed.stderr.strip() or completed.stdout.strip())[-2_000:].decode(
                "utf-8", errors="replace"
            )
            raise V2FinalEvaluationLaunchError("exact attempt14 packet sync failed: " + detail)

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]:
        paths = [path for path in sorted(packet_dir.rglob("*")) if path.is_file() and not path.is_symlink()]
        upload_info = hash_files([str(path) for path in paths])
        if len(upload_info) != len(paths):
            raise V2FinalEvaluationLaunchError("local attempt14 Xet hashing returned incomplete results")
        return {
            path.relative_to(packet_dir).as_posix(): str(info.hash)
            for path, info in zip(paths, upload_info, strict=True)
        }

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return metadata_only_bucket_entries(
            self.api,
            remote_uri=V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI,
        )

    def claim_exists(self) -> bool:
        if not self.api.repo_exists(
            V2_EVALUATION_CLAIM_REPOSITORY_ID, repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE
        ):
            return False
        return V2_EVALUATION_CLAIM_PATH in self.api.list_repo_files(
            V2_EVALUATION_CLAIM_REPOSITORY_ID, repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE
        )

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.api.create_repo(
            V2_EVALUATION_CLAIM_REPOSITORY_ID,
            repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE,
            private=True,
            exist_ok=True,
        )
        info = self.api.repo_info(
            V2_EVALUATION_CLAIM_REPOSITORY_ID, repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE
        )
        head = str(getattr(info, "sha", ""))
        if getattr(info, "private", None) is not True or len(head) != 40:
            raise V2FinalEvaluationLaunchError("attempt14 claim repository is not a private exact-CAS parent")
        require_claim_never_existed(
            self.api,
            repository_id=V2_EVALUATION_CLAIM_REPOSITORY_ID,
            repository_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE,
            claim_path=V2_EVALUATION_CLAIM_PATH,
            expected_head_sha=head,
        )
        try:
            commit = self.api.create_commit(
                V2_EVALUATION_CLAIM_REPOSITORY_ID,
                repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE,
                parent_commit=head,
                commit_message="Acquire permanent Scriber attempt14 evaluation launch claim",
                operations=[
                    CommitOperationAdd(
                        path_in_repo=V2_EVALUATION_CLAIM_PATH,
                        path_or_fileobj=io.BytesIO(payload),
                    )
                ],
            )
        except Exception as error:
            raise V2FinalEvaluationLaunchError(
                "permanent attempt14 CAS claim failed; submission is forbidden"
            ) from error
        oid = str(getattr(commit, "oid", ""))
        try:
            metadata = self.api.get_paths_info(
                V2_EVALUATION_CLAIM_REPOSITORY_ID,
                [V2_EVALUATION_CLAIM_PATH],
                repo_type=V2_EVALUATION_CLAIM_REPOSITORY_TYPE,
                revision=oid,
            )
            validate_exact_claim_commit_metadata(
                metadata,
                claim_path=V2_EVALUATION_CLAIM_PATH,
                payload=payload,
            )
        except V2FinalEvaluationLaunchError:
            raise
        except Exception as error:
            raise V2FinalEvaluationLaunchError(
                "permanent attempt14 claim metadata verification failed"
            ) from error
        return oid

    def submit_detached_once(self, argv: list[str]) -> str:
        completed = subprocess.run(
            [*HF_CLI_PREFIX, *argv],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=120,
        )
        combined = "\n".join((completed.stdout, completed.stderr)).strip()
        if completed.returncode != 0:
            raise V2FinalEvaluationLaunchError(
                "detached attempt14 submission failed or is ambiguous; permanent claim retained: "
                + combined[-2_000:]
            )
        return combined

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]:
        try:
            value = json.loads(self._run_cli(["jobs", "inspect", job_id]).decode("utf-8", errors="strict"))
        except json.JSONDecodeError as error:
            raise V2FinalEvaluationLaunchError("attempt14 CLI inspection is invalid JSON") from error
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise V2FinalEvaluationLaunchError("attempt14 CLI inspection must contain exactly one job")
        api_job = self.api.inspect_job(job_id=job_id)
        return value[0], api_job, str(getattr(api_job, "url", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--resume-preclaim-reservation",
        action="store_true",
        help=(
            "Resume only an exact pre-claim receipt reservation after proving no claim, "
            "attempt14 job, active job, or output exists; never re-upload the packet."
        ),
    )
    args = parser.parse_args(argv)
    require_hf_control_plane_runtime()
    receipt = launch_v2_final_evaluation_job(
        HuggingFaceV2EvaluationControlPlane(),
        packet_dir=args.packet,
        receipt_path=args.receipt,
        resume_preclaim_reservation=args.resume_preclaim_reservation,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
