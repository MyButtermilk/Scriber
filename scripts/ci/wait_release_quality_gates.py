"""Wait for this immutable release run's effective quality jobs before signing.

Preparation may overlap testing, but only GitHub's latest effective job results
for the same run authorize the next step. A failed-job rerun may retain an
earlier successful job from this run; results from other runs are never reused.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

QUALITY_PREFIX = "Exact-revision quality gates / "
JOB_PREFIXES = ("Exact-revision quality gates", "Reusable quality gates")
REQUIRED_JOB_SUFFIXES = (
    "Repository-wide Python Ruff",
    "Frontend check, lint, test, and build",
    "Rust fmt, clippy, and tests",
    "Python tests and extended mypy / Python full suite",
    "Python tests and extended mypy / Python real-browser integration",
    "Python tests and extended mypy / Python typecheck",
)
REQUIRED_JOB_NAMES = tuple(QUALITY_PREFIX + suffix for suffix in REQUIRED_JOB_SUFFIXES)
_PENDING_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


class GateError(Exception):
    """A fixed diagnostic code, never an API response or subprocess stderr."""


def gh_json(endpoint: str, timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                endpoint,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        raise GateError("github_request_failed") from None
    if result.returncode:
        raise GateError("github_request_failed")
    try:
        value = json.loads(result.stdout)
    except ValueError, TypeError:
        raise GateError("invalid_github_json") from None
    if not isinstance(value, dict):
        raise GateError("invalid_github_json")
    return value


def _validate_run(run: dict[str, Any], *, repo: str, run_id: int, attempt: int, head_sha: str) -> None:
    repository = run.get("repository")
    if (
        run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("head_sha") != head_sha
        or not isinstance(repository, dict)
        or str(repository.get("full_name", "")).lower() != repo.lower()
    ):
        raise GateError("run_identity_mismatch")
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        raise GateError("workflow_run_unsuccessful")


def _list_jobs(request: Callable[[str], dict[str, Any]], base: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    # The bound prevents malformed pagination from creating an unbounded loop.
    for page in range(1, 101):
        result = request(f"{base}/jobs?filter=latest&per_page=100&page={page}")
        batch = result.get("jobs")
        total = result.get("total_count")
        if (
            not isinstance(batch, list)
            or not all(isinstance(job, dict) for job in batch)
            or type(total) is not int
            or not 0 <= total <= 10000
            or len(batch) > 100
        ):
            raise GateError("invalid_jobs_response")
        jobs.extend(batch)
        if len(batch) < 100:
            if len(jobs) != total:
                raise GateError("incomplete_jobs_response")
            return jobs
    raise GateError("jobs_pagination_limit")


def _evaluate_jobs(
    jobs: list[dict[str, Any]], *, run_id: int, attempt: int, head_sha: str, job_prefix: str
) -> tuple[list[dict[str, Any]], list[str]]:
    required_names = tuple(job_prefix + " / " + suffix for suffix in REQUIRED_JOB_SUFFIXES)
    observed: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or not name.startswith(job_prefix + " / "):
            continue
        if name not in required_names:
            raise GateError("unexpected_quality_job")
        if name in observed:
            # Never select an older green result over an overridden failure.
            raise GateError("duplicate_effective_quality_job")
        source_attempt = job.get("run_attempt")
        if (
            job.get("run_id") != run_id
            or job.get("head_sha") != head_sha
            or type(source_attempt) is not int
            or not 1 <= source_attempt <= attempt
            or type(job.get("id")) is not int
        ):
            raise GateError("job_identity_mismatch")
        observed[name] = {
            "name": name,
            "id": job["id"],
            "runAttempt": source_attempt,
            "status": job.get("status"),
            "conclusion": job.get("conclusion"),
        }
    pending = []
    for name in required_names:
        job = observed.get(name)
        if job is None:
            pending.append(name)
        elif job["status"] == "completed":
            if job["conclusion"] != "success":
                return list(observed.values()), [name]
        elif job["status"] in _PENDING_STATUSES and job["conclusion"] is None:
            pending.append(name)
        else:
            raise GateError("invalid_job_status")
    return list(observed.values()), pending


def wait_for_quality_gates(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    job_prefix: str = "Exact-revision quality gates",
    timeout_seconds: float = 2400,
    poll_seconds: float = 15,
    api: Callable[[str, float], dict[str, Any]] = gh_json,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    started = monotonic()
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "runId": run_id,
        "runAttempt": attempt,
        "headSha": head_sha,
        "requiredJobs": [job_prefix + " / " + suffix for suffix in REQUIRED_JOB_SUFFIXES],
        "jobSelection": "same-run-latest-effective",
        "status": "failure",
        "reason": "invalid_arguments",
        "jobs": [],
        "pendingJobs": [job_prefix + " / " + suffix for suffix in REQUIRED_JOB_SUFFIXES],
        "polls": 0,
    }
    deadline = started + timeout_seconds

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("quality_gates_timeout")
        return api(endpoint, min(30, remaining))

    try:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or run_id < 1
            or attempt < 1
            or job_prefix not in JOB_PREFIXES
            or not 0 < timeout_seconds <= 7200
            or not 0 < poll_seconds <= 60
        ):
            raise GateError("invalid_arguments")
        base = f"repos/{repo}/actions/runs/{run_id}"
        while True:
            evidence["polls"] += 1
            run = request(base)
            _validate_run(run, repo=repo, run_id=run_id, attempt=attempt, head_sha=head_sha)
            jobs, pending = _evaluate_jobs(
                _list_jobs(request, base), run_id=run_id, attempt=attempt, head_sha=head_sha, job_prefix=job_prefix
            )
            evidence["jobs"] = jobs
            evidence["pendingJobs"] = pending
            if any(job["status"] == "completed" and job["conclusion"] != "success" for job in jobs):
                raise GateError("quality_job_unsuccessful")
            if not pending:
                # Recheck after pagination so a concurrent rerun cannot authorize
                # signing using the superseded attempt's snapshot.
                _validate_run(request(base), repo=repo, run_id=run_id, attempt=attempt, head_sha=head_sha)
                evidence["status"] = "success"
                evidence["reason"] = "all_required_quality_jobs_succeeded"
                break
            if run.get("status") == "completed":
                raise GateError("completed_run_missing_quality_jobs")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise GateError("quality_gates_timeout")
            sleep(min(poll_seconds, remaining))
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        # Never serialize unexpected exception text: authentication/network
        # libraries may include credentials or full response bodies in it.
        evidence["reason"] = "unexpected_gate_error"
    finally:
        evidence["elapsedSeconds"] = round(max(0, monotonic() - started), 3)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = evidence_path.with_suffix(evidence_path.suffix + ".tmp")
        temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(evidence_path)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--job-prefix", choices=JOB_PREFIXES, default=JOB_PREFIXES[0])
    parser.add_argument("--timeout-seconds", type=float, default=2400)
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args()
    result = wait_for_quality_gates(
        repo=args.repo,
        run_id=args.run_id,
        attempt=args.run_attempt,
        head_sha=args.head_sha,
        evidence_path=args.evidence,
        job_prefix=args.job_prefix,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    print(f"Release quality gates: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
