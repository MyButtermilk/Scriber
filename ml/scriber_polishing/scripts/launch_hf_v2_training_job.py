"""Permanently claim and submit exactly one detached Scriber V2 HF job.

The claim is retained after every failure or ambiguous response. This command
never uploads the packet and never retries submission.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_launch_gate import (  # noqa: E402
    HF_CLI_PREFIX,
    parse_hf_bucket_list,
    parse_hf_jobs_json,
    reconcile_authoritative_hf_job_snapshots,
    require_claim_never_existed,
    require_clean_checkout_at_head,
    require_hf_control_plane_runtime,
)
from scriber_polishing.hf_v2_training_job import (  # noqa: E402
    V2_OUTPUT_REMOTE_URI,
    V2_PACKET_REMOTE_URI,
)
from scriber_polishing.hf_v2_training_launch import (  # noqa: E402
    V2_CLAIM_PATH,
    V2_CLAIM_REPOSITORY_ID,
    V2_CLAIM_REPOSITORY_TYPE,
    V2LaunchError,
    launch_v2_training_job,
    parse_hf_cli_single_job_inspection,
)


class HuggingFaceV2ControlPlane:
    def __init__(self, api: HfApi | None = None) -> None:
        self.api = api or HfApi()

    @staticmethod
    def _run_cli(arguments: list[str], *, timeout_seconds: int = 90) -> bytes:
        completed = subprocess.run(
            [*HF_CLI_PREFIX, "hf", *arguments],
            check=False,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0 or completed.stderr.strip():
            detail = (completed.stderr.strip() or completed.stdout.strip())[-2_000:].decode("utf-8", errors="replace")
            raise V2LaunchError("HF control-plane command failed closed: " + detail)
        return completed.stdout

    def authoritative_jobs(self) -> list[dict[str, object]]:
        api_jobs = list(self.api.list_jobs(namespace="Buttermilk03"))
        cli_jobs = parse_hf_jobs_json(
            self._run_cli(["jobs", "ps", "-a", "--format", "json"]).decode("utf-8", errors="strict")
        )
        return reconcile_authoritative_hf_job_snapshots(api_jobs, cli_jobs)

    def packet_entries(self) -> list[dict[str, object]]:
        return parse_hf_bucket_list(
            self._run_cli(["buckets", "list", V2_PACKET_REMOTE_URI, "-R", "--format", "json"]).decode(
                "utf-8", errors="strict"
            )
        )

    def packet_metadata(self, relative: str) -> bytes:
        if Path(relative).name != relative:
            raise V2LaunchError("remote packet metadata name is unsafe")
        return self._run_cli(["buckets", "cp", f"{V2_PACKET_REMOTE_URI}/{relative}", "-", "--quiet"])

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return parse_hf_bucket_list(
            self._run_cli(["buckets", "list", V2_OUTPUT_REMOTE_URI, "-R", "--format", "json"]).decode(
                "utf-8", errors="strict"
            )
        )

    def claim_exists(self) -> bool:
        if not self.api.repo_exists(V2_CLAIM_REPOSITORY_ID, repo_type=V2_CLAIM_REPOSITORY_TYPE):
            return False
        return V2_CLAIM_PATH in self.api.list_repo_files(V2_CLAIM_REPOSITORY_ID, repo_type=V2_CLAIM_REPOSITORY_TYPE)

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.api.create_repo(
            V2_CLAIM_REPOSITORY_ID,
            repo_type=V2_CLAIM_REPOSITORY_TYPE,
            private=True,
            exist_ok=True,
        )
        info = self.api.repo_info(V2_CLAIM_REPOSITORY_ID, repo_type=V2_CLAIM_REPOSITORY_TYPE)
        head = str(getattr(info, "sha", ""))
        if getattr(info, "private", None) is not True or len(head) != 40:
            raise V2LaunchError("V2 claim repository is not a private exact-CAS parent")
        require_claim_never_existed(
            self.api,
            repository_id=V2_CLAIM_REPOSITORY_ID,
            repository_type=V2_CLAIM_REPOSITORY_TYPE,
            claim_path=V2_CLAIM_PATH,
            expected_head_sha=head,
        )
        try:
            commit = self.api.create_commit(
                V2_CLAIM_REPOSITORY_ID,
                repo_type=V2_CLAIM_REPOSITORY_TYPE,
                parent_commit=head,
                commit_message="Acquire permanent Scriber V2 training launch claim",
                operations=[CommitOperationAdd(path_in_repo=V2_CLAIM_PATH, path_or_fileobj=io.BytesIO(payload))],
            )
        except Exception as error:
            raise V2LaunchError("permanent V2 CAS claim failed; submission is forbidden") from error
        oid = str(getattr(commit, "oid", ""))
        try:
            downloaded = Path(
                hf_hub_download(
                    repo_id=V2_CLAIM_REPOSITORY_ID,
                    filename=V2_CLAIM_PATH,
                    repo_type=V2_CLAIM_REPOSITORY_TYPE,
                    revision=oid,
                    force_download=True,
                )
            )
            if downloaded.is_symlink() or downloaded.read_bytes() != payload:
                raise V2LaunchError("permanent V2 claim readback bytes differ")
        except V2LaunchError:
            raise
        except Exception as error:
            raise V2LaunchError("permanent V2 claim could not be read back exactly") from error
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
            timeout=90,
        )
        combined = "\n".join((completed.stdout, completed.stderr)).strip()
        if completed.returncode != 0:
            raise V2LaunchError(
                "detached V2 submission failed or is ambiguous; permanent claim retained: " + combined[-2_000:]
            )
        return combined

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]:
        cli = parse_hf_cli_single_job_inspection(self._run_cli(["jobs", "inspect", job_id]))
        api_job = self.api.inspect_job(job_id=job_id)
        return cli, api_job, str(getattr(api_job, "url", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    require_hf_control_plane_runtime()
    manifest = json.loads((args.packet / "package-manifest.json").read_text(encoding="utf-8"))
    require_clean_checkout_at_head(manifest.get("source", {}).get("git_head"), repository_root=REPOSITORY_ROOT)
    receipt = launch_v2_training_job(
        HuggingFaceV2ControlPlane(),
        packet_dir=args.packet,
        receipt_path=args.receipt,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
