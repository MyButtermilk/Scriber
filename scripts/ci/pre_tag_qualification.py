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
    PENDING,
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
REUSE_JOB_PREFIX = "Reuse canonical main quality"
API = Callable[[str, float], dict[str, Any]]


def _require_clear_main_quality(
    request: Callable[[str], dict[str, Any]], *, head_sha: str, now: datetime, allow_incompatible: bool = False
) -> None:
    if allow_incompatible:
        # A completed local fallback owns fresh quality evidence. An incomplete
        # main provenance contract cannot replace it, nor can a known failure
        # be hidden by the fallback. Discovery/API failures still fail closed.
        _, pending_main = _discover_main_quality(request, head_sha=head_sha, now=now)
        if pending_main:
            raise GateError("main_quality_not_completed")
        return
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
    require_provenance: bool = True,
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
    if not require_provenance:
        # Planning has not launched reusable jobs. On the reuse path only the
        # pinned main run supplies quality provenance; its own validator checks
        # both immutable reusable workflow revisions independently.
        return
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


def _verify_main_source(
    request: Callable[[str], dict[str, Any]],
    *,
    run_id: int,
    attempt: int,
    head_sha: str,
    now: datetime,
    before: datetime,
) -> dict[str, Any]:
    base = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    run = request(base)
    validate_source_run(run, run_id=run_id, attempt=attempt, head_sha=head_sha, now=now)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise GateError("main_quality_not_completed")
    if not _timestamp(run["created_at"]) <= _timestamp(run.get("updated_at")) <= before:
        raise GateError("main_quality_completed_after_qualification")
    _check_prior_attempts(run, head_sha=head_sha, request=request)
    jobs, pending = _evaluate_jobs(
        _list_jobs(request, base),
        run_id=run_id,
        attempt=attempt,
        head_sha=head_sha,
        job_prefix="Reusable quality gates",
    )
    if pending or any(job["status"] != "completed" or job["conclusion"] != "success" for job in jobs):
        raise GateError("main_quality_jobs_unsuccessful")
    if request(base) != run:
        raise GateError("main_quality_changed")
    return {
        "qualitySourceRunId": run_id,
        "qualitySourceRunAttempt": attempt,
        "qualitySourceCreatedAt": run["created_at"],
        "qualitySourceUpdatedAt": run["updated_at"],
        "referencedWorkflows": run["referenced_workflows"],
        "jobs": jobs,
    }


def _reuse_binding(jobs: list[dict[str, Any]], *, run_id: int, attempt: int, head_sha: str) -> tuple[int, int] | None:
    markers = [job for job in jobs if str(job.get("name", "")).startswith(REUSE_JOB_PREFIX)]
    if not markers:
        return None  # Legacy qualifications ran their own six jobs.
    if len(markers) != 1:
        raise GateError("qualification_reuse_binding_ambiguous")
    marker = markers[0]
    if (
        type(marker.get("id")) is not int
        or marker["id"] < 1
        or marker.get("run_id") != run_id
        or marker.get("run_attempt") != attempt
        or marker.get("head_sha") != head_sha
    ):
        raise GateError("qualification_reuse_binding_identity_mismatch")
    if (
        marker.get("name") == f"{REUSE_JOB_PREFIX} (0/0)"
        and marker.get("status") == "completed"
        and marker.get("conclusion") == "skipped"
    ):
        return None  # Explicit local fallback; own six gates remain mandatory.
    match = re.fullmatch(rf"{REUSE_JOB_PREFIX} \(([1-9][0-9]{{0,19}})/([1-9][0-9]{{0,2}})\)", marker["name"])
    if (
        match is None
        or marker.get("status") != "completed"
        or marker.get("conclusion") != "success"
        or int(match[2]) > 100
        or any(str(job.get("name", "")).startswith("Reusable quality gates / ") for job in jobs)
    ):
        raise GateError("qualification_reuse_binding_invalid")
    return int(match[1]), int(match[2])


def _discover_main_quality(
    request: Callable[[str], dict[str, Any]], *, head_sha: str, now: datetime
) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    pending = False
    incompatible = False
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
            if (
                not _source_matches(listed, head_sha)
                or not now - timedelta(hours=24) <= _timestamp(listed.get("created_at")) <= now
            ):
                continue
            if listed.get("status") == "completed" and listed.get("conclusion") != "success":
                raise GateError("matching_main_run_unsuccessful")
            base = f"repos/{REPOSITORY}/actions/runs/{listed['id']}"
            run = request(base)
            if not _source_matches(run, head_sha) or run["id"] != listed["id"]:
                raise GateError("main_quality_identity_changed")
            if run.get("status") == "completed" and run.get("conclusion") != "success":
                raise GateError("matching_main_run_unsuccessful")
            _check_prior_attempts(run, head_sha=head_sha, request=request)
            jobs, unfinished = _evaluate_jobs(
                _list_jobs(request, base),
                run_id=run["id"],
                attempt=run["run_attempt"],
                head_sha=head_sha,
                job_prefix="Reusable quality gates",
            )
            if any(job["status"] == "completed" and job["conclusion"] != "success" for job in jobs):
                raise GateError("matching_main_quality_job_unsuccessful")
            if run.get("status") in PENDING and run.get("conclusion") is None:
                # Referenced workflows and child jobs can appear only after a
                # queued run starts. Their absence must not trigger duplicate CI.
                pending = True
            else:
                try:
                    validate_source_run(run, run_id=run["id"], attempt=run["run_attempt"], head_sha=head_sha, now=now)
                except GateError:
                    incompatible = True
                else:
                    if unfinished:
                        incompatible = True
                    else:
                        candidates.append(run)
            if request(base) != run:
                raise GateError("main_quality_changed")
        if len(batch) < 100:
            if (page - 1) * 100 + len(batch) != total:
                raise GateError("incomplete_main_quality_discovery")
            # Finish auditing every matching run before falling back: missing
            # provenance on one run cannot conceal another known failure.
            return ([] if incompatible else candidates), pending
    raise GateError("main_quality_pagination_limit")


def plan_qualification(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    api: API = gh_json,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 1200,
    discovery_grace_seconds: float = 180,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "headSha": head_sha,
        "status": "failure",
        "decision": "local",
        "sourceRunId": 0,
        "sourceRunAttempt": 0,
    }
    started = monotonic()
    deadline = started + timeout_seconds

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("qualification_planning_timeout")
        return api(endpoint, min(30, remaining))

    def validate_current() -> None:
        checked_at = now()
        validate_qualification(
            request(f"repos/{repo}/actions/runs/{run_id}"),
            run_id=run_id,
            attempt=attempt,
            head_sha=head_sha,
            now=checked_at,
            before=checked_at,
            qualifying=True,
            require_provenance=False,
        )

    try:
        if (
            repo != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or run_id < 1
            or not 1 <= attempt <= 100
            or not 0 < timeout_seconds <= 1200
            or not 0 <= discovery_grace_seconds <= timeout_seconds
        ):
            raise GateError("invalid_qualification_arguments")
        validate_current()
        try:
            while True:
                try:
                    candidates, pending = _discover_main_quality(request, head_sha=head_sha, now=now())
                except GateError as error:
                    if str(error) != "main_quality_changed":
                        raise
                    # A queued/running source may advance between the two API
                    # reads. Rediscover it through the full failure/history
                    # audit instead of starting a duplicate local suite.
                    candidates, pending = [], True
                if not pending and candidates:
                    selected = max(candidates, key=lambda run: (_timestamp(run["created_at"]), run["id"]))
                    quality = _verify_main_source(
                        request,
                        run_id=selected["id"],
                        attempt=selected["run_attempt"],
                        head_sha=head_sha,
                        now=now(),
                        before=now(),
                    )
                    evidence.update(
                        decision="reuse",
                        reason="canonical_main_same_revision",
                        sourceRunId=selected["id"],
                        sourceRunAttempt=selected["run_attempt"],
                        **quality,
                    )
                    break
                if not pending and monotonic() - started >= discovery_grace_seconds:
                    evidence["reason"] = "no_reusable_main_quality"
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise GateError("qualification_planning_timeout")
                sleep(min(5, remaining))
        except GateError as error:
            if str(error).startswith(("matching_main_", "qualification_")) or str(error) in {
                "main_quality_identity_changed",
                "source_run_identity_mismatch",
                "source_attempt_identity_mismatch",
                "main_quality_changed",
            }:
                raise
            evidence.update(reason=str(error), decision="local", sourceRunId=0, sourceRunAttempt=0)
        except Exception:
            evidence.update(reason="main_quality_discovery_unavailable", decision="local")
        validate_current()
        evidence["status"] = "success"
    except GateError as error:
        evidence.update(status="failure", reason=str(error))
    except Exception:
        evidence.update(status="failure", reason="qualification_api_unavailable")
    _write_evidence(evidence_path, evidence)
    return evidence


def reuse_main_quality(
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
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"status": "failure", "headSha": head_sha}
    deadline = time.monotonic() + 120

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("qualification_verification_timeout")
        return api(endpoint, min(30, remaining))

    try:
        if (
            repo != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or run_id < 1
            or not 1 <= attempt <= 100
            or source_run_id < 1
            or not 1 <= source_attempt <= 100
        ):
            raise GateError("invalid_qualification_arguments")
        base = f"repos/{repo}/actions/runs/{run_id}"
        run = request(base)
        validate_qualification(
            run,
            run_id=run_id,
            attempt=attempt,
            head_sha=head_sha,
            now=now(),
            before=now(),
            qualifying=True,
            require_provenance=False,
        )
        evidence.update(
            _verify_main_source(
                request,
                run_id=source_run_id,
                attempt=source_attempt,
                head_sha=head_sha,
                now=now(),
                before=now(),
            )
        )
        _require_clear_main_quality(request, head_sha=head_sha, now=now())
        if request(base) != run:
            raise GateError("qualification_changed_during_verification")
        evidence.update(status="success", reason="canonical_main_same_revision")
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        evidence["reason"] = "qualification_api_unavailable"
    _write_evidence(evidence_path, evidence)
    return evidence


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
    raw_jobs = _list_jobs(request, base)
    binding = _reuse_binding(raw_jobs, run_id=run_id, attempt=attempt, head_sha=head_sha)
    validate_qualification(
        run,
        run_id=run_id,
        attempt=attempt,
        head_sha=head_sha,
        now=now,
        before=before,
        qualifying=qualifying,
        require_provenance=binding is None,
    )
    # A failed-job rerun must not erase a failed qualification attempt.
    for prior_attempt in range(1, attempt):
        prior = request(f"{base}/attempts/{prior_attempt}")
        validate_qualification(
            prior,
            run_id=run_id,
            attempt=prior_attempt,
            head_sha=head_sha,
            now=now,
            before=before,
            require_provenance=binding is None,
        )
    quality: dict[str, Any]
    if binding is None:
        jobs, pending = _evaluate_jobs(
            raw_jobs, run_id=run_id, attempt=attempt, head_sha=head_sha, job_prefix="Reusable quality gates"
        )
        if pending or any(job["status"] != "completed" or job["conclusion"] != "success" for job in jobs):
            raise GateError("qualification_quality_jobs_unsuccessful")
        quality = {"referencedWorkflows": run["referenced_workflows"], "jobs": jobs}
    else:
        quality = _verify_main_source(
            request,
            run_id=binding[0],
            attempt=binding[1],
            head_sha=head_sha,
            now=now,
            before=before if qualifying else _timestamp(run["updated_at"]),
        )
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
        after,
        run_id=run_id,
        attempt=attempt,
        head_sha=head_sha,
        now=now,
        before=before,
        qualifying=qualifying,
        require_provenance=binding is None,
    )
    if after != run:
        raise GateError("qualification_changed_during_verification")
    return {
        "sourceRunId": run_id,
        "sourceRunAttempt": attempt,
        "sourceCreatedAt": run["created_at"],
        "sourceUpdatedAt": run["updated_at"],
        "qualityMode": "main"
        if binding
        else "local"
        if any(str(job.get("name", "")).startswith(REUSE_JOB_PREFIX) for job in raw_jobs)
        else "legacy",
        **quality,
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
                _require_clear_main_quality(
                    request, head_sha=head_sha, now=now(), allow_incompatible=evidence.get("qualityMode") == "local"
                )
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
    parser.add_argument("operation", choices=("plan", "reuse", "qualify", "select", "release", "verify", "create-tag"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--source-run-id", type=int, default=0)
    parser.add_argument("--source-run-attempt", type=int, default=0)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    parser.add_argument("--discovery-grace-seconds", type=float, default=180)
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
    arguments = {
        "repo": args.repo,
        "run_id": args.run_id,
        "attempt": args.run_attempt,
        "head_sha": args.head_sha,
        "evidence_path": args.evidence,
    }
    if args.operation == "plan":
        result = plan_qualification(
            **arguments,
            timeout_seconds=args.timeout_seconds,
            discovery_grace_seconds=args.discovery_grace_seconds,
        )
    elif args.operation == "reuse":
        result = reuse_main_quality(
            **arguments, source_run_id=args.source_run_id, source_attempt=args.source_run_attempt
        )
    else:
        result = qualification_evidence(
            **arguments,
            source_run_id=args.source_run_id,
            source_attempt=args.source_run_attempt,
            operation=args.operation,
        )
    if args.github_output and result["status"] == "success":
        with args.github_output.open("a", encoding="utf-8") as output:
            if args.operation == "plan":
                output.write(f"reuse-quality={str(result['decision'] == 'reuse').lower()}\n")
                output.write(f"quality-source-run-id={result['sourceRunId']}\n")
                output.write(f"quality-source-run-attempt={result['sourceRunAttempt']}\n")
            elif args.operation != "reuse":
                output.write(f"qualification-run-id={result['sourceRunId']}\n")
                output.write(f"qualification-run-attempt={result['sourceRunAttempt']}\n")
    print(f"Pre-tag qualification: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
