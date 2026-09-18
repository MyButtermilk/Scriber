"""Reuse canonical main quality gates only for the identical release commit.

Missing or unavailable provenance schedules local gates. An observed failure at
the matching main revision never becomes a fallback retry. The release barrier
independently binds the current tag run and the selected source run/attempt.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.ci.wait_release_quality_gates import (
    GateError,
    _evaluate_jobs,
    _list_jobs,
    gh_json,
    wait_for_quality_gates,
)

REPOSITORY = "MyButtermilk/Scriber"
SOURCE_WORKFLOW = ".github/workflows/hybrid-pr-checks.yml"
RELEASE_WORKFLOW = ".github/workflows/release-windows.yml"
REFERENCED_WORKFLOWS = (".github/workflows/quality-gates.yml", ".github/workflows/python-full-suite.yml")
PENDING = {"queued", "in_progress", "waiting", "pending", "requested"}
API = Callable[[str, float], dict[str, Any]]


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GateError("invalid_run_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GateError("invalid_run_timestamp") from None
    if result.tzinfo is None:
        raise GateError("invalid_run_timestamp")
    return result.astimezone(UTC)


def _repository_matches(run: dict[str, Any]) -> bool:
    return all(
        isinstance(run.get(field), dict) and str(run[field].get("full_name", "")).casefold() == REPOSITORY.casefold()
        for field in ("repository", "head_repository")
    )


def validate_release_run(
    run: dict[str, Any], *, run_id: int, attempt: int, head_sha: str, allow_dispatch: bool = False
) -> None:
    branch = run.get("head_branch")
    trigger_valid = (
        isinstance(branch, str)
        and bool(branch)
        and (
            (
                run.get("event") == "push"
                and re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", branch)
            )
            or (allow_dispatch and run.get("event") == "workflow_dispatch")
        )
    )
    if (
        run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("head_sha") != head_sha
        or not _repository_matches(run)
        or run.get("path") != RELEASE_WORKFLOW
        or not trigger_valid
    ):
        raise GateError("release_run_identity_mismatch")
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        raise GateError("release_run_unsuccessful")
    if run.get("status") not in PENDING | {"completed"}:
        raise GateError("invalid_release_run_status")


def _source_matches(run: dict[str, Any], head_sha: str) -> bool:
    return (
        _repository_matches(run)
        and run.get("path") == SOURCE_WORKFLOW
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == head_sha
        and type(run.get("id")) is int
        and run["id"] > 0
        and type(run.get("run_attempt")) is int
        and 1 <= run["run_attempt"] <= 100
    )


def validate_source_run(run: dict[str, Any], *, run_id: int, attempt: int, head_sha: str, now: datetime) -> None:
    if not _source_matches(run, head_sha) or run["id"] != run_id or run["run_attempt"] != attempt:
        raise GateError("source_run_identity_mismatch")
    created = _timestamp(run.get("created_at"))
    if not now - timedelta(hours=24) <= created <= now:
        raise GateError("source_run_expired")
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        raise GateError("matching_main_run_unsuccessful")
    if run.get("status") not in PENDING | {"completed"} or (
        run.get("status") != "completed" and run.get("conclusion") is not None
    ):
        raise GateError("invalid_source_run_status")
    references = run.get("referenced_workflows")
    if not isinstance(references, list) or len(references) != len(REFERENCED_WORKFLOWS):
        raise GateError("source_workflow_provenance_unavailable")
    observed = set()
    for reference in references:
        if not isinstance(reference, dict):
            raise GateError("source_workflow_provenance_unavailable")
        if reference.get("sha") != head_sha or reference.get("ref") != "refs/heads/main":
            raise GateError("source_workflow_revision_mismatch")
        observed.add(reference.get("path"))
    expected = {f"{REPOSITORY}/{path}@{head_sha}" for path in REFERENCED_WORKFLOWS}
    if observed != expected:
        raise GateError("source_workflow_revision_mismatch")


def _check_prior_attempts(run: dict[str, Any], *, head_sha: str, request: Callable[[str], dict[str, Any]]) -> None:
    for attempt in range(1, run["run_attempt"]):
        prior = request(f"repos/{REPOSITORY}/actions/runs/{run['id']}/attempts/{attempt}")
        if not _source_matches(prior, head_sha) or prior["id"] != run["id"] or prior["run_attempt"] != attempt:
            raise GateError("source_attempt_identity_mismatch")
        if prior.get("status") != "completed" or prior.get("conclusion") != "success":
            raise GateError("matching_main_attempt_unsuccessful")


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def select_quality_source(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    api: API = gh_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "releaseRunId": run_id,
        "releaseRunAttempt": attempt,
        "headSha": head_sha,
        "status": "success",
        "decision": "local",
        "reason": "no_matching_main_run",
        "sourceRunId": 0,
        "sourceRunAttempt": 0,
    }

    deadline = monotonic() + 120

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("source_discovery_timeout")
        return api(endpoint, min(30, remaining))

    try:
        if repo != REPOSITORY or run_id < 1 or attempt < 1 or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise GateError("invalid_arguments")
        current = request(f"repos/{repo}/actions/runs/{run_id}")
        validate_release_run(current, run_id=run_id, attempt=attempt, head_sha=head_sha, allow_dispatch=True)
        if current.get("event") == "workflow_dispatch":
            evidence["reason"] = "explicit_dispatch_uses_local_gates"
            _write_evidence(evidence_path, evidence)
            return evidence
        checked_at = now()
        created_filter = quote(">=" + (checked_at - timedelta(hours=24)).isoformat(), safe="")
        candidates = []
        # The API filter is only an optimization: all returned identities and
        # ages are checked independently before any source can be selected.
        for page in range(1, 11):
            result = request(
                f"repos/{repo}/actions/workflows/hybrid-pr-checks.yml/runs"
                f"?event=push&branch=main&head_sha={head_sha}&per_page=100&page={page}&created={created_filter}"
            )
            batch = result.get("workflow_runs")
            total = result.get("total_count")
            if not isinstance(batch, list) or len(batch) > 100 or type(total) is not int or not 0 <= total <= 1000:
                raise GateError("invalid_source_discovery_response")
            for listed in batch:
                if not isinstance(listed, dict):
                    raise GateError("invalid_source_discovery_response")
                if not _source_matches(listed, head_sha):
                    continue
                created = _timestamp(listed.get("created_at"))
                if not checked_at - timedelta(hours=24) <= created <= checked_at:
                    continue
                if listed.get("status") == "completed" and listed.get("conclusion") != "success":
                    raise GateError("matching_main_run_unsuccessful")
                source = request(f"repos/{repo}/actions/runs/{listed['id']}")
                # Check failure before optional provenance: a known failed run
                # cannot be hidden by missing referenced-workflow metadata.
                if not _source_matches(source, head_sha) or source["id"] != listed["id"]:
                    raise GateError("source_run_identity_mismatch")
                if source.get("status") == "completed" and source.get("conclusion") != "success":
                    raise GateError("matching_main_run_unsuccessful")
                _check_prior_attempts(source, head_sha=head_sha, request=request)
                # A second green run must not hide a failure that is already
                # visible in another still-running matching main run.
                jobs, _pending = _evaluate_jobs(
                    _list_jobs(request, f"repos/{repo}/actions/runs/{source['id']}"),
                    run_id=source["id"],
                    attempt=source["run_attempt"],
                    head_sha=head_sha,
                    job_prefix="Reusable quality gates",
                )
                if any(job["status"] == "completed" and job["conclusion"] != "success" for job in jobs):
                    raise GateError("matching_main_quality_job_unsuccessful")
                candidates.append(source)
            if len(batch) < 100:
                if (page - 1) * 100 + len(batch) != total:
                    raise GateError("incomplete_source_discovery_response")
                break
        else:
            raise GateError("source_discovery_pagination_limit")
        if candidates:
            selected = max(candidates, key=lambda source: (_timestamp(source["created_at"]), source["id"]))
            source_id = selected["id"]
            source_attempt = selected["run_attempt"]
            validate_source_run(selected, run_id=source_id, attempt=source_attempt, head_sha=head_sha, now=checked_at)
            validate_source_run(
                request(f"repos/{repo}/actions/runs/{source_id}"),
                run_id=source_id,
                attempt=source_attempt,
                head_sha=head_sha,
                now=checked_at,
            )
            validate_release_run(
                request(f"repos/{repo}/actions/runs/{run_id}"), run_id=run_id, attempt=attempt, head_sha=head_sha
            )
            evidence.update(
                decision="reuse",
                reason="canonical_main_same_revision",
                sourceRunId=source_id,
                sourceRunAttempt=source_attempt,
                sourceWorkflow=SOURCE_WORKFLOW,
                sourceCreatedAt=selected["created_at"],
                referencedWorkflows=selected["referenced_workflows"],
            )
    except GateError as error:
        reason = str(error)
        evidence["reason"] = reason
        if reason.startswith(("matching_main_", "release_run_")) or reason == "invalid_arguments":
            evidence.update(status="failure", decision="failure")
        # Unavailable, incomplete or incompatible source evidence schedules all
        # local gates, instead of treating a cache/API hint as test success.
    except Exception:
        evidence["reason"] = "source_discovery_unavailable"
    _write_evidence(evidence_path, evidence)
    return evidence


def wait_for_selected_quality_source(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    source_run_id: int,
    source_attempt: int,
    evidence_path: Path,
    api: API = gh_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 2400,
    poll_seconds: float = 15,
) -> dict[str, Any]:
    current_base = f"repos/{repo}/actions/runs/{run_id}"
    reused = source_run_id > 0
    source_base = f"repos/{repo}/actions/runs/{source_run_id}"
    references: list[dict[str, Any]] = []

    def guarded_api(endpoint: str, timeout: float) -> dict[str, Any]:
        if repo != REPOSITORY or source_run_id < 0 or source_attempt < 0 or reused != (source_attempt > 0):
            raise GateError("invalid_quality_source_arguments")
        if reused and source_run_id == run_id:
            raise GateError("quality_source_cannot_be_release")
        if not reused:
            result = api(endpoint, timeout)
            if endpoint == current_base:
                validate_release_run(result, run_id=run_id, attempt=attempt, head_sha=head_sha, allow_dispatch=True)
            return result
        if endpoint == source_base:
            validate_release_run(api(current_base, timeout), run_id=run_id, attempt=attempt, head_sha=head_sha)
            source = api(endpoint, timeout)
            validate_source_run(source, run_id=source_run_id, attempt=source_attempt, head_sha=head_sha, now=now())
            _check_prior_attempts(source, head_sha=head_sha, request=lambda path: api(path, timeout))
            references[:] = source["referenced_workflows"]
            # Recheck the release after source/history reads; neither run may
            # rerun or change provenance during the acceptance snapshot.
            validate_release_run(api(current_base, timeout), run_id=run_id, attempt=attempt, head_sha=head_sha)
            return source
        return api(endpoint, timeout)

    evidence = wait_for_quality_gates(
        repo=repo,
        run_id=source_run_id if reused else run_id,
        attempt=source_attempt if reused else attempt,
        head_sha=head_sha,
        evidence_path=evidence_path,
        job_prefix="Reusable quality gates" if reused else "Exact-revision quality gates",
        api=guarded_api,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    evidence.update(
        releaseRunId=run_id,
        releaseRunAttempt=attempt,
        sourceRunId=source_run_id if reused else run_id,
        sourceRunAttempt=source_attempt if reused else attempt,
        sourceWorkflow=SOURCE_WORKFLOW if reused else RELEASE_WORKFLOW,
        referencedWorkflows=references,
        jobSelection="canonical-main-same-sha-latest-effective" if reused else "same-run-latest-effective",
    )
    _write_evidence(evidence_path, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("select", "wait"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--source-run-id", type=int, default=0)
    parser.add_argument("--source-run-attempt", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=2400)
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args()
    arguments = {
        "repo": args.repo,
        "run_id": args.run_id,
        "attempt": args.run_attempt,
        "head_sha": args.head_sha,
        "evidence_path": args.evidence,
    }
    if args.operation == "select":
        result = select_quality_source(**arguments)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"reuse-quality={str(result['decision'] == 'reuse').lower()}\n")
                output.write(f"quality-source-run-id={result['sourceRunId']}\n")
                output.write(f"quality-source-run-attempt={result['sourceRunAttempt']}\n")
    else:
        result = wait_for_selected_quality_source(
            **arguments,
            source_run_id=args.source_run_id,
            source_attempt=args.source_run_attempt,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    print(f"Release quality source: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
