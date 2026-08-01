"""Permanently claim and submit exactly one attempt15 HF conversion job.

The permanent claim survives every failure or ambiguous response. This
command never uploads the packet, never downloads packet/claim bytes, and
never retries submission.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from hf_xet import hash_files
from huggingface_hub import CommitOperationAdd, HfApi

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_launch_gate import (  # noqa: E402
    HF_CLI_PREFIX,
    parse_hf_jobs_json,
    reconcile_authoritative_hf_job_snapshots,
    require_claim_never_existed,
    require_clean_checkout_at_head,
    require_hf_control_plane_runtime,
)
from scriber_polishing.hf_v2_release_job import (  # noqa: E402
    V2_RELEASE_PACKET_REMOTE_URI,
)
from scriber_polishing.hf_v2_release_launch import (  # noqa: E402
    V2_RELEASE_CLAIM_REPOSITORY_TYPE,
    V2ReleaseLaunchError,
    launch_v2_release_job,
    parse_hf_cli_v2_release_inspection,
    validate_v2_release_claim_commit_metadata,
)
from scriber_polishing.v2_q8_bf16_release import (  # noqa: E402
    V2_RELEASE_CLAIM_PATH,
    V2_RELEASE_CLAIM_REPOSITORY,
    V2_RELEASE_OUTPUT_URI,
)


class HuggingFaceV2ReleaseControlPlane:
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
            detail = (completed.stderr.strip() or completed.stdout.strip())[-2_000:].decode(
                "utf-8", errors="replace"
            )
            raise V2ReleaseLaunchError(
                "HF attempt15 control-plane command failed closed: " + detail
            )
        return completed.stdout

    def authoritative_jobs(self) -> list[dict[str, object]]:
        api_jobs = list(self.api.list_jobs(namespace="Buttermilk03"))
        cli_jobs = parse_hf_jobs_json(
            self._run_cli(["jobs", "ps", "-a", "--format", "json"]).decode(
                "utf-8", errors="strict"
            )
        )
        return reconcile_authoritative_hf_job_snapshots(api_jobs, cli_jobs)

    @staticmethod
    def _bucket_location(uri: str) -> tuple[str, str]:
        prefix = "hf://buckets/"
        if not uri.startswith(prefix):
            raise V2ReleaseLaunchError("attempt15 bucket URI is invalid")
        parts = uri.removeprefix(prefix).split("/")
        if len(parts) < 3 or any(not part or part in {".", ".."} for part in parts):
            raise V2ReleaseLaunchError("attempt15 bucket URI is invalid")
        return "/".join(parts[:2]), "/".join(parts[2:])

    def _bucket_entries(self, uri: str) -> list[dict[str, object]]:
        bucket_id, prefix = self._bucket_location(uri)
        try:
            entries = list(
                self.api.list_bucket_tree(
                    bucket_id,
                    prefix=prefix,
                    recursive=True,
                )
            )
        except Exception as error:
            raise V2ReleaseLaunchError(
                "HF attempt15 Buckets metadata lookup failed closed"
            ) from error
        result: list[dict[str, object]] = []
        for entry in entries:
            entry_type = getattr(entry, "type", None)
            path = getattr(entry, "path", None)
            if entry_type == "directory" and isinstance(path, str):
                result.append({"type": "directory", "path": path})
                continue
            size = getattr(entry, "size", None)
            xet_hash = getattr(entry, "xet_hash", None)
            if entry_type != "file" or not isinstance(path, str):
                raise V2ReleaseLaunchError(
                    "HF attempt15 Buckets metadata entry is malformed"
                )
            result.append(
                {
                    "type": "file",
                    "path": path,
                    "size": size,
                    "xet_hash": xet_hash,
                }
            )
        return result

    def packet_entries(self) -> list[dict[str, object]]:
        return self._bucket_entries(V2_RELEASE_PACKET_REMOTE_URI)

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]:
        paths = [
            path
            for path in sorted(packet_dir.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]
        try:
            upload_info = hash_files([str(path) for path in paths])
        except Exception as error:
            raise V2ReleaseLaunchError(
                "local attempt15 Xet hashing failed closed"
            ) from error
        if len(upload_info) != len(paths):
            raise V2ReleaseLaunchError(
                "local attempt15 Xet hashing returned incomplete results"
            )
        return {
            path.relative_to(packet_dir).as_posix(): str(info.hash)
            for path, info in zip(paths, upload_info, strict=True)
        }

    def output_prefix_entries(self) -> list[dict[str, object]]:
        return self._bucket_entries(V2_RELEASE_OUTPUT_URI)

    def claim_exists(self) -> bool:
        if not self.api.repo_exists(
            V2_RELEASE_CLAIM_REPOSITORY,
            repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
        ):
            return False
        return V2_RELEASE_CLAIM_PATH in self.api.list_repo_files(
            V2_RELEASE_CLAIM_REPOSITORY,
            repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
        )

    def acquire_permanent_claim(self, payload: bytes) -> str:
        self.api.create_repo(
            V2_RELEASE_CLAIM_REPOSITORY,
            repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
            private=True,
            exist_ok=True,
        )
        info = self.api.repo_info(
            V2_RELEASE_CLAIM_REPOSITORY,
            repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
        )
        head = str(getattr(info, "sha", ""))
        if getattr(info, "private", None) is not True or len(head) != 40:
            raise V2ReleaseLaunchError(
                "attempt15 claim repository is not a private exact-CAS parent"
            )
        require_claim_never_existed(
            self.api,
            repository_id=V2_RELEASE_CLAIM_REPOSITORY,
            repository_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
            claim_path=V2_RELEASE_CLAIM_PATH,
            expected_head_sha=head,
        )
        operation = CommitOperationAdd(
            path_in_repo=V2_RELEASE_CLAIM_PATH,
            path_or_fileobj=payload,
        )
        try:
            commit = self.api.create_commit(
                V2_RELEASE_CLAIM_REPOSITORY,
                repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
                parent_commit=head,
                commit_message="Acquire permanent Scriber V2 Q8/BF16 launch claim",
                operations=[operation],
            )
        except Exception as error:
            raise V2ReleaseLaunchError(
                "permanent attempt15 CAS claim failed; submission is forbidden"
            ) from error
        oid = str(getattr(commit, "oid", ""))
        try:
            exact_info = self.api.repo_info(
                V2_RELEASE_CLAIM_REPOSITORY,
                repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
                revision=oid,
            )
            path_entries = self.api.get_paths_info(
                V2_RELEASE_CLAIM_REPOSITORY,
                [V2_RELEASE_CLAIM_PATH],
                repo_type=V2_RELEASE_CLAIM_REPOSITORY_TYPE,
                revision=oid,
                expand=True,
            )
            validate_v2_release_claim_commit_metadata(
                exact_info,
                path_entries,
                expected_commit=oid,
                payload=payload,
            )
        except V2ReleaseLaunchError:
            raise
        except Exception as error:
            raise V2ReleaseLaunchError(
                "permanent attempt15 claim metadata could not be verified exactly"
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
            timeout=90,
        )
        combined = "\n".join((completed.stdout, completed.stderr)).strip()
        if completed.returncode != 0:
            raise V2ReleaseLaunchError(
                "detached attempt15 submission failed or is ambiguous; "
                "permanent claim retained: " + combined[-2_000:]
            )
        return combined

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]:
        cli = parse_hf_cli_v2_release_inspection(
            self._run_cli(["jobs", "inspect", job_id])
        )
        api_job = self.api.inspect_job(job_id=job_id)
        return cli, api_job, str(getattr(api_job, "url", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    require_hf_control_plane_runtime()
    manifest = json.loads(
        (args.packet / "package-manifest.json").read_text(encoding="utf-8")
    )
    require_clean_checkout_at_head(
        manifest.get("source", {}).get("git_head"),
        repository_root=REPOSITORY_ROOT,
    )
    receipt = launch_v2_release_job(
        HuggingFaceV2ReleaseControlPlane(),
        packet_dir=args.packet,
        receipt_path=args.receipt,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
