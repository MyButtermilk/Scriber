"""Require completed, same-SHA pre-tag gates before creating or releasing a tag.

GitHub API identities and effective jobs authorize release, never a downloaded
JSON artifact. Qualification has no signing or publication permissions.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.ci.release_quality_source import (
    REFERENCED_WORKFLOWS,
    REPOSITORY,
    _check_prior_attempts,
    _repository_matches,
    _source_matches,
    _timestamp,
    _write_evidence,
    validate_release_run,
    validate_source_run,
)
from scripts.ci.wait_release_quality_gates import GateError, _evaluate_jobs, _list_jobs, gh_json

WORKFLOW = ".github/workflows/release-qualification.yml"
CONTRACT_JOB = "Verify pre-tag quality contract"
API = Callable[[str, float], dict[str, Any]]


def _require_clear_main_quality(request: Callable[[str], dict[str, Any]], *, head_sha: str, now: datetime) -> None:
    # The existing release selector rejects known matching main failures. Check
    # that same constraint before tag creation, even if a separate qualification
    # has passed. Otherwise the helper could still consume an unusable tag.
    for page in range(1, 11):
        result = request(
            f"repos/{REPOSITORY}/actions/workflows/hybrid-pr-checks.yml/runs"
            f"?event=push&branch=main&head_sha={head_sha}&per_page=100&page={page}"
        )
        batch, total = result.get("workflow_runs"), result.get("total_count")
        if not isinstance(batch, list) or len(batch) > 100 or type(total) is not int or not 0 <= total <= 1000:
            raise GateError("invalid_main_quality_discovery")
        for listed in batch:
            if not isinstance(listed, dict):
                raise GateError("invalid_main_quality_discovery")
            if not _source_matches(listed, head_sha) or _timestamp(listed.get("created_at")) < now - timedelta(
                hours=24
            ):
                continue
            base = f"repos/{REPOSITORY}/actions/runs/{listed['id']}"
            run = request(base)
            validate_source_run(run, run_id=listed["id"], attempt=listed["run_attempt"], head_sha=head_sha, now=now)
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                raise GateError("main_quality_not_completed")
            _check_prior_attempts(run, head_sha=head_sha, request=request)
            jobs, pending = _evaluate_jobs(
                _list_jobs(request, base),
                run_id=run["id"],
                attempt=run["run_attempt"],
                head_sha=head_sha,
                job_prefix="Reusable quality gates",
            )
            if pending or any(job["conclusion"] != "success" for job in jobs):
                raise GateError("main_quality_jobs_unsuccessful")
            after = request(base)
            validate_source_run(after, run_id=run["id"], attempt=run["run_attempt"], head_sha=head_sha, now=now)
            if after != run:
                raise GateError("main_quality_changed")
        if len(batch) < 100:
            if (page - 1) * 100 + len(batch) != total:
                raise GateError("incomplete_main_quality_discovery")
            return
    raise GateError("main_quality_pagination_limit")


def validate_qualification(
    run: dict[str, Any],
    *,
    run_id: int,
    attempt: int,
    head_sha: str,
    now: datetime,
    before: datetime,
    qualifying: bool = False,
) -> None:
    if (
        run.get("id") != run_id
        or type(run.get("id")) is not int
        or run_id < 1
        or run.get("run_attempt") != attempt
        or type(run.get("run_attempt")) is not int
        or not 1 <= attempt <= 100
        or run.get("head_sha") != head_sha
        or not _repository_matches(run)
        or run.get("path") != WORKFLOW
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
    ):
        raise GateError("qualification_identity_mismatch")
    created = _timestamp(run.get("created_at"))
    updated = _timestamp(run.get("updated_at"))
    if not now - timedelta(hours=24) <= created <= updated <= min(now, before):
        raise GateError("qualification_expired_or_post_tag")
    if not qualifying and (run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise GateError("qualification_not_successful")
    if qualifying and (run.get("status") != "in_progress" or run.get("conclusion") is not None):
        raise GateError("qualification_not_running")
    references = run.get("referenced_workflows")
    if not isinstance(references, list) or len(references) != len(REFERENCED_WORKFLOWS):
        raise GateError("qualification_provenance_missing")
    expected = {f"{REPOSITORY}/{path}@{head_sha}" for path in REFERENCED_WORKFLOWS}
    if (
        any(
            not isinstance(ref, dict) or ref.get("sha") != head_sha or ref.get("ref") != "refs/heads/main"
            for ref in references
        )
        or {ref.get("path") for ref in references} != expected
    ):
        raise GateError("qualification_provenance_mismatch")


def _verify(
    request: Callable[[str], dict[str, Any]],
    *,
    run_id: int,
    attempt: int,
    head_sha: str,
    now: datetime,
    before: datetime,
    qualifying: bool = False,
) -> dict[str, Any]:
    base = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    run = request(base)
    validate_qualification(
        run, run_id=run_id, attempt=attempt, head_sha=head_sha, now=now, before=before, qualifying=qualifying
    )
    # A failed-job rerun must not erase a failed qualification attempt.
    for prior_attempt in range(1, attempt):
        prior = request(f"{base}/attempts/{prior_attempt}")
        validate_qualification(prior, run_id=run_id, attempt=prior_attempt, head_sha=head_sha, now=now, before=before)
    raw_jobs = _list_jobs(request, base)
    jobs, pending = _evaluate_jobs(
        raw_jobs, run_id=run_id, attempt=attempt, head_sha=head_sha, job_prefix="Reusable quality gates"
    )
    if pending or any(job["status"] != "completed" or job["conclusion"] != "success" for job in jobs):
        raise GateError("qualification_quality_jobs_unsuccessful")
    if not qualifying:
        contracts = [job for job in raw_jobs if job.get("name") == CONTRACT_JOB]
        if len(contracts) != 1 or any(
            job.get("run_id") != run_id
            or job.get("run_attempt") != attempt
            or job.get("head_sha") != head_sha
            or job.get("status") != "completed"
            or job.get("conclusion") != "success"
            for job in contracts
        ):
            raise GateError("qualification_contract_missing")
    after = request(base)
    validate_qualification(
        after, run_id=run_id, attempt=attempt, head_sha=head_sha, now=now, before=before, qualifying=qualifying
    )
    if after != run:
        raise GateError("qualification_changed_during_verification")
    return {
        "sourceRunId": run_id,
        "sourceRunAttempt": attempt,
        "sourceCreatedAt": run["created_at"],
        "sourceUpdatedAt": run["updated_at"],
        "referencedWorkflows": run["referenced_workflows"],
        "jobs": jobs,
    }


def qualification_evidence(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    operation: str,
    source_run_id: int = 0,
    source_attempt: int = 0,
    api: API = gh_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "headSha": head_sha,
        "status": "failure",
        "sourceRunId": 0,
        "sourceRunAttempt": 0,
    }
    deadline = monotonic() + (1200 if operation == "qualify" else 120)

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("qualification_verification_timeout")
        return api(endpoint, min(30, remaining))

    def require_clear_main_quality() -> None:
        while True:
            try:
                _require_clear_main_quality(request, head_sha=head_sha, now=now())
                return
            except GateError as error:
                # The main push and qualification run share gates but can finish
                # in either order. Only this read-only contract may wait; tag and
                # signing verification still reject pending or failed evidence.
                if operation != "qualify" or str(error) != "main_quality_not_completed":
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise GateError("qualification_verification_timeout") from None
                sleep(min(5, remaining))

    try:
        if (
            repo != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or run_id < 1
            or attempt < 1
            or operation not in {"select", "release", "verify", "qualify"}
            or source_run_id < 0
            or source_attempt < 0
            or (operation == "release" and (source_run_id < 1 or source_attempt < 1))
        ):
            raise GateError("invalid_qualification_arguments")
        checked_at = now()
        before = checked_at
        release = None
        if operation in {"select", "release"}:
            release = request(f"repos/{repo}/actions/runs/{run_id}")
            validate_release_run(release, run_id=run_id, attempt=attempt, head_sha=head_sha)
            before = _timestamp(release.get("created_at"))
        if operation in {"release", "verify"}:
            _verify(
                request,
                run_id=source_run_id or run_id,
                attempt=source_attempt or attempt,
                head_sha=head_sha,
                now=checked_at,
                before=before,
            )
        if operation != "qualify":
            candidates = []
            for page in range(1, 11):
                result = request(
                    f"repos/{repo}/actions/workflows/release-qualification.yml/runs"
                    f"?event=workflow_dispatch&branch=main&head_sha={head_sha}&per_page=100&page={page}"
                )
                batch = result.get("workflow_runs")
                total = result.get("total_count")
                if not isinstance(batch, list) or type(total) is not int or not 0 <= total <= 1000 or len(batch) > 100:
                    raise GateError("invalid_qualification_discovery")
                for listed in batch:
                    if not isinstance(listed, dict):
                        raise GateError("invalid_qualification_discovery")
                    # Ignore unrelated and expired runs; never trust the API filters alone.
                    if (
                        listed.get("head_sha") != head_sha
                        or listed.get("path") != WORKFLOW
                        or listed.get("event") != "workflow_dispatch"
                        or listed.get("head_branch") != "main"
                        or not _repository_matches(listed)
                        or _timestamp(listed.get("created_at")) < checked_at - timedelta(hours=24)
                    ):
                        continue
                    candidate = _verify(
                        request,
                        run_id=listed["id"],
                        attempt=listed["run_attempt"],
                        head_sha=head_sha,
                        now=checked_at,
                        before=before,
                    )
                    candidates.append(candidate)
                if len(batch) < 100:
                    if (page - 1) * 100 + len(batch) != total:
                        raise GateError("incomplete_qualification_discovery")
                    break
            else:
                raise GateError("qualification_pagination_limit")
            if not candidates:
                raise GateError("missing_pre_tag_qualification")
            if operation == "select":
                selected = max(
                    candidates, key=lambda value: (_timestamp(value["sourceCreatedAt"]), value["sourceRunId"])
                )
            else:
                pinned = [
                    candidate
                    for candidate in candidates
                    if candidate["sourceRunId"] == (source_run_id or run_id)
                    and candidate["sourceRunAttempt"] == (source_attempt or attempt)
                ]
                if len(pinned) != 1:
                    raise GateError("pinned_qualification_not_found")
                selected = pinned[0]
            evidence.update(
                _verify(
                    request,
                    run_id=selected["sourceRunId"],
                    attempt=selected["sourceRunAttempt"],
                    head_sha=head_sha,
                    now=checked_at,
                    before=before,
                )
            )
        else:
            evidence.update(
                _verify(
                    request,
                    run_id=source_run_id or run_id,
                    attempt=source_attempt or attempt,
                    head_sha=head_sha,
                    now=checked_at,
                    before=before,
                    qualifying=operation == "qualify",
                )
            )
        require_clear_main_quality()
        evidence.update(
            _verify(
                request,
                run_id=evidence["sourceRunId"],
                attempt=evidence["sourceRunAttempt"],
                head_sha=head_sha,
                now=now(),
                before=before if release is not None else now(),
                qualifying=operation == "qualify",
            )
        )
        if release is not None:
            after = request(f"repos/{repo}/actions/runs/{run_id}")
            validate_release_run(after, run_id=run_id, attempt=attempt, head_sha=head_sha)
            if after.get("created_at") != release.get("created_at"):
                raise GateError("release_run_changed")
        # A delayed main push run can appear while source/release identities
        # are rechecked. Make main discovery the final state read, so absence
        # in the first snapshot cannot authorize signing after it becomes visible.
        require_clear_main_quality()
        if now() - _timestamp(evidence["sourceCreatedAt"]) > timedelta(hours=24):
            raise GateError("qualification_expired_during_final_checks")
        evidence.update(status="success", reason="same_sha_pre_tag_qualification", sourceWorkflow=WORKFLOW)
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        evidence["reason"] = "qualification_api_unavailable"
    _write_evidence(evidence_path, evidence)
    return evidence


def create_qualified_tag(
    *, head_sha: str, tag: str, source_run_id: int, source_attempt: int, evidence_path: Path
) -> None:
    # Fetch first; no version tag is created until all API checks have passed.
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise GateError("invalid_release_tag")
    remote = subprocess.check_output(["git", "remote", "get-url", "--push", "origin"], text=True).strip()
    if remote not in {
        f"{base}{suffix}"
        for base in (f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}")
        for suffix in ("", ".git")
    }:
        raise GateError("tag_remote_repository_mismatch")
    subprocess.run(["git", "fetch", "origin", "main"], check=True)
    main_sha = subprocess.check_output(["git", "rev-parse", "FETCH_HEAD"], text=True).strip()
    version_source = subprocess.check_output(["git", "show", f"{head_sha}:src/version.py"], text=True)
    if main_sha != head_sha or f'__version__ = "{tag[1:]}"' not in version_source:
        raise GateError("tag_main_or_version_mismatch")
    result = qualification_evidence(
        repo=REPOSITORY,
        run_id=source_run_id,
        attempt=source_attempt,
        head_sha=head_sha,
        source_run_id=source_run_id,
        source_attempt=source_attempt,
        operation="verify",
        evidence_path=evidence_path,
    )
    if result["status"] != "success":
        raise GateError(result["reason"])
    live_main = gh_json(f"repos/{REPOSITORY}/git/ref/heads/main", 30)
    if live_main.get("ref") != "refs/heads/main" or live_main.get("object", {}).get("sha") != head_sha:
        raise GateError("main_changed_before_tag")
    if datetime.now(UTC) - _timestamp(result.get("sourceCreatedAt")) > timedelta(hours=24):
        raise GateError("qualification_expired_before_tag")
    subprocess.run(
        [
            "git",
            "tag",
            "-a",
            tag,
            head_sha,
            "-m",
            f"Scriber {tag[1:]}; qualification {source_run_id}/{source_attempt}; SHA {head_sha}",
        ],
        check=True,
    )
    subprocess.run(["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("qualify", "select", "release", "verify", "create-tag"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--source-run-id", type=int, default=0)
    parser.add_argument("--source-run-attempt", type=int, default=0)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    if args.operation == "create-tag":
        if args.repo != REPOSITORY or not args.tag:
            parser.error("create-tag requires the canonical repository and --tag")
        create_qualified_tag(
            head_sha=args.head_sha,
            tag=args.tag,
            source_run_id=args.run_id,
            source_attempt=args.run_attempt,
            evidence_path=args.evidence,
        )
        return 0
    result = qualification_evidence(
        repo=args.repo,
        run_id=args.run_id,
        attempt=args.run_attempt,
        head_sha=args.head_sha,
        source_run_id=args.source_run_id,
        source_attempt=args.source_run_attempt,
        operation=args.operation,
        evidence_path=args.evidence,
    )
    if args.github_output and result["status"] == "success":
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"qualification-run-id={result['sourceRunId']}\n")
            output.write(f"qualification-run-attempt={result['sourceRunAttempt']}\n")
    print(f"Pre-tag qualification: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
