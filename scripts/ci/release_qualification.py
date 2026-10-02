"""Require completed exact-main qualification before an official tag release.

Qualification invokes the existing quality/browser workflows without release
secrets. Authenticated run and job identities, rather than an uploaded JSON
claim, authorize release planning and are checked again before signing.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.ci.release_quality_source import (
    REFERENCED_WORKFLOWS,
    REPOSITORY,
    _repository_matches,
    _timestamp,
    _write_evidence,
    validate_release_run,
)
from scripts.ci.wait_release_quality_gates import GateError, _evaluate_jobs, _list_jobs, gh_json

QUALIFICATION_WORKFLOW = ".github/workflows/qualify-release.yml"
API = Callable[[str, float], dict[str, Any]]
REQUEST = Callable[[str], dict[str, Any]]


def _arguments(repo: str, head_sha: str, *identities: int) -> None:
    if (
        repo != REPOSITORY
        or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
        or any(type(value) is not int or value < 1 for value in identities)
    ):
        raise GateError("invalid_qualification_arguments")


def _matches(run: dict[str, Any], head_sha: str) -> bool:
    return (
        _repository_matches(run)
        and run.get("path") == QUALIFICATION_WORKFLOW
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == head_sha
        and type(run.get("id")) is int
        and run["id"] > 0
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0
    )


def validate_qualification_run(
    run: dict[str, Any],
    *,
    run_id: int,
    attempt: int,
    head_sha: str,
    now: datetime,
    before: datetime | None = None,
) -> None:
    if not _matches(run, head_sha) or run["id"] != run_id or run["run_attempt"] != attempt:
        raise GateError("qualification_run_identity_mismatch")
    created = _timestamp(run.get("created_at"))
    updated = _timestamp(run.get("updated_at"))
    if not now - timedelta(hours=24) <= created <= updated <= now:
        raise GateError("qualification_expired_or_invalid_timestamp")
    if before is not None:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise GateError("qualification_not_successful")
        if updated > before:
            raise GateError("qualification_not_completed_before_tag_release")
    elif run.get("status") not in {"in_progress", "completed"} or (
        run.get("conclusion") is not None
        and not (run.get("status") == "completed" and run.get("conclusion") == "success")
    ):
        raise GateError("qualification_not_successful")
    references = run.get("referenced_workflows")
    expected = {f"{REPOSITORY}/{path}@{head_sha}" for path in REFERENCED_WORKFLOWS}
    if (
        not isinstance(references, list)
        or len(references) != len(expected)
        or any(
            not isinstance(item, dict)
            or item.get("sha") != head_sha
            or item.get("ref") != "refs/heads/main"
            or item.get("path") not in expected
            for item in references
        )
        or {item["path"] for item in references} != expected
    ):
        raise GateError("qualification_workflow_revision_mismatch")


def _quality_jobs(request: REQUEST, run: dict[str, Any], head_sha: str) -> list[dict[str, Any]]:
    jobs, pending = _evaluate_jobs(
        _list_jobs(request, f"repos/{REPOSITORY}/actions/runs/{run['id']}"),
        run_id=run["id"],
        attempt=run["run_attempt"],
        head_sha=head_sha,
        job_prefix="Exact-revision quality gates",
    )
    if pending:
        raise GateError("qualification_quality_jobs_not_successful")
    return jobs


def _discover(request: REQUEST, head_sha: str, now: datetime) -> dict[str, Any]:
    created_filter = quote(">=" + (now - timedelta(hours=24)).isoformat(), safe="")
    candidates = []
    for page in range(1, 11):
        response = request(
            f"repos/{REPOSITORY}/actions/workflows/qualify-release.yml/runs"
            f"?event=workflow_dispatch&branch=main&head_sha={head_sha}&per_page=100&page={page}&created={created_filter}"
        )
        batch = response.get("workflow_runs")
        total = response.get("total_count")
        if (
            not isinstance(batch, list)
            or len(batch) > 100
            or not all(isinstance(run, dict) for run in batch)
            or type(total) is not int
            or not 0 <= total <= 1000
        ):
            raise GateError("invalid_qualification_discovery_response")
        for run in batch:
            if _matches(run, head_sha) and now - timedelta(hours=24) <= _timestamp(run.get("created_at")) <= now:
                candidates.append(run)
        if len(batch) < 100:
            if (page - 1) * 100 + len(batch) != total:
                raise GateError("incomplete_qualification_discovery_response")
            break
    else:
        raise GateError("qualification_discovery_pagination_limit")
    if not candidates:
        raise GateError("no_fresh_pre_tag_qualification")
    # A newer failed or pending qualification cannot be hidden by an older pass.
    return max(candidates, key=lambda run: (_timestamp(run["created_at"]), run["id"]))


def check_qualification(
    *,
    operation: str,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    qualification_run_id: int = 0,
    qualification_attempt: int = 0,
    api: API = gh_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "runId": run_id,
        "runAttempt": attempt,
        "headSha": head_sha,
        "qualificationRunId": qualification_run_id,
        "qualificationRunAttempt": qualification_attempt,
        "status": "failure",
        "reason": "invalid_qualification_arguments",
        "jobs": [],
    }
    deadline = monotonic() + 120

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("qualification_request_timeout")
        return api(endpoint, min(30, remaining))

    try:
        _arguments(repo, head_sha, run_id, attempt)
        if operation not in {"certify", "select", "verify"}:
            raise GateError("invalid_qualification_arguments")
        base = f"repos/{repo}/actions/runs/{run_id}"
        current = request(base)
        before = None
        if operation == "certify":
            qualification_run_id, qualification_attempt = run_id, attempt
        else:
            validate_release_run(current, run_id=run_id, attempt=attempt, head_sha=head_sha)
            before = _timestamp(current.get("created_at"))
            if before > now():
                raise GateError("invalid_release_run_timestamp")
            if operation == "select":
                selected = _discover(request, head_sha, now())
                qualification_run_id, qualification_attempt = selected["id"], selected["run_attempt"]
            _arguments(repo, head_sha, qualification_run_id, qualification_attempt)
            if qualification_run_id == run_id:
                raise GateError("qualification_cannot_be_release")
        evidence.update(qualificationRunId=qualification_run_id, qualificationRunAttempt=qualification_attempt)
        source_base = f"repos/{repo}/actions/runs/{qualification_run_id}"
        source = current if operation == "certify" else request(source_base)
        binding = {"run_id": qualification_run_id, "attempt": qualification_attempt, "head_sha": head_sha}
        validate_qualification_run(source, **binding, now=now(), before=before)
        evidence["jobs"] = _quality_jobs(request, source, head_sha)
        # Latest effective jobs can include successes retained by a failed-job
        # rerun. Recheck the whole attempt after all pages, and pin it for signing.
        final_source = request(source_base)
        validate_qualification_run(final_source, **binding, now=now(), before=before)
        if source["created_at"] != final_source["created_at"] or (
            before is not None and source["updated_at"] != final_source["updated_at"]
        ):
            raise GateError("qualification_changed_during_snapshot")
        if operation != "certify":
            final_release = request(base)
            validate_release_run(final_release, run_id=run_id, attempt=attempt, head_sha=head_sha)
            if _timestamp(final_release.get("created_at")) != before:
                raise GateError("release_run_identity_mismatch")
        evidence.update(
            status="success",
            reason="exact_main_pre_tag_quality_succeeded",
            qualificationWorkflow=QUALIFICATION_WORKFLOW,
            qualificationCreatedAt=source["created_at"],
            qualificationCompletedAt=source["updated_at"] if before else None,
            referencedWorkflows=source["referenced_workflows"],
        )
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        evidence["reason"] = "qualification_check_unavailable"
    _write_evidence(evidence_path, evidence)
    return evidence


def check_candidate(
    *, repo: str, head_sha: str, candidate_sha: str, event_name: str, ref: str, api: API = gh_json
) -> dict[str, Any]:
    evidence = {"headSha": head_sha, "status": "failure", "reason": "invalid_qualification_candidate"}
    try:
        _arguments(repo, head_sha)
        if event_name != "workflow_dispatch" or ref != "refs/heads/main" or candidate_sha != head_sha:
            raise GateError("qualification_requires_exact_main_dispatch")
        if api(f"repos/{repo}/commits/main", 30).get("sha") != head_sha:
            raise GateError("qualification_candidate_is_not_current_main")
        evidence.update(status="success", reason="exact_main_candidate")
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        evidence["reason"] = "qualification_check_unavailable"
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("candidate", "certify", "select", "verify"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--run-attempt", type=int, default=0)
    parser.add_argument("--qualification-run-id", type=int, default=0)
    parser.add_argument("--qualification-run-attempt", type=int, default=0)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--event-name", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--candidate-sha", default="")
    args = parser.parse_args()
    if args.operation == "candidate":
        result = check_candidate(
            repo=args.repo,
            head_sha=args.head_sha,
            candidate_sha=args.candidate_sha,
            event_name=args.event_name,
            ref=args.ref,
        )
        _write_evidence(args.evidence, result)
    else:
        result = check_qualification(
            operation=args.operation,
            repo=args.repo,
            run_id=args.run_id,
            attempt=args.run_attempt,
            head_sha=args.head_sha,
            evidence_path=args.evidence,
            qualification_run_id=args.qualification_run_id,
            qualification_attempt=args.qualification_run_attempt,
        )
    if args.github_output and args.operation == "select" and result["status"] == "success":
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"qualification-run-id={result['qualificationRunId']}\n")
            output.write(f"qualification-run-attempt={result['qualificationRunAttempt']}\n")
    print(f"Pre-tag qualification: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
