from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import release_quality_source as source
from scripts.ci.wait_release_quality_gates import REQUIRED_JOB_SUFFIXES, GateError

REPO = source.REPOSITORY
SHA = "a" * 40
RELEASE_ID = 100
SOURCE_ID = 200
NOW = datetime(2026, 9, 18, 17, 0, tzinfo=UTC)
RELEASE_BASE = f"repos/{REPO}/actions/runs/{RELEASE_ID}"
SOURCE_BASE = f"repos/{REPO}/actions/runs/{SOURCE_ID}"


def _run(run_id: int, *, main: bool) -> dict:
    return {
        "id": run_id,
        "run_attempt": 1,
        "head_sha": SHA,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "path": source.SOURCE_WORKFLOW if main else source.RELEASE_WORKFLOW,
        "event": "push",
        "head_branch": "main" if main else "v0.5.999",
        "created_at": "2026-09-18T16:00:00Z",
        "status": "in_progress",
        "conclusion": None,
        "referenced_workflows": [
            {"path": f"{REPO}/{path}@{SHA}", "sha": SHA, "ref": "refs/heads/main"}
            for path in source.REFERENCED_WORKFLOWS
        ],
    }


def _jobs(run_id: int, *, main: bool = True, attempt: int = 1) -> list[dict]:
    prefix = "Reusable quality gates" if main else "Exact-revision quality gates"
    return [
        {
            "id": 1000 + index,
            "name": prefix + " / " + suffix,
            "run_id": run_id,
            "run_attempt": attempt,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
        }
        for index, suffix in enumerate(REQUIRED_JOB_SUFFIXES)
    ]


class GitHub:
    def __init__(self) -> None:
        self.release = _run(RELEASE_ID, main=False)
        self.main = _run(SOURCE_ID, main=True)
        self.runs = [self.main]
        self.prior_attempts: dict[int, dict] = {}
        self.jobs = _jobs(SOURCE_ID)
        self.requests: list[str] = []
        self.clock = 0.0

    def api(self, endpoint: str, timeout: float) -> dict[str, Any]:
        assert 0 < timeout <= 30
        self.requests.append(endpoint)
        if endpoint == RELEASE_BASE:
            return deepcopy(self.release)
        if endpoint == SOURCE_BASE:
            return deepcopy(self.main)
        if "/workflows/hybrid-pr-checks.yml/runs?" in endpoint:
            assert f"event=push&branch=main&head_sha={SHA}&per_page=100&page=1" in endpoint
            return {"workflow_runs": deepcopy(self.runs), "total_count": len(self.runs)}
        if endpoint.startswith(SOURCE_BASE + "/attempts/"):
            return deepcopy(self.prior_attempts[int(endpoint.rsplit("/", 1)[1])])
        if "/jobs?" in endpoint:
            return {"jobs": deepcopy(self.jobs), "total_count": len(self.jobs)}
        for run in self.runs:
            if endpoint == f"repos/{REPO}/actions/runs/{run['id']}":
                return deepcopy(run)
        raise AssertionError(f"Unexpected API call: {endpoint}")

    def select(self, tmp_path: Path, **overrides) -> dict:
        arguments = {
            "repo": REPO,
            "run_id": RELEASE_ID,
            "attempt": 1,
            "head_sha": SHA,
            "api": self.api,
            "now": lambda: NOW,
            "evidence_path": tmp_path / "selection.json",
        }
        arguments.update(overrides)
        result = source.select_quality_source(**arguments)
        assert json.loads(arguments["evidence_path"].read_text(encoding="utf-8")) == result
        return result

    def wait(self, tmp_path: Path, **overrides) -> dict:
        arguments = {
            "repo": REPO,
            "run_id": RELEASE_ID,
            "attempt": 1,
            "head_sha": SHA,
            "source_run_id": SOURCE_ID,
            "source_attempt": 1,
            "api": self.api,
            "now": lambda: NOW,
            "evidence_path": tmp_path / "barrier.json",
            "monotonic": lambda: self.clock,
            "sleep": self.sleep,
            "timeout_seconds": 10,
            "poll_seconds": 2,
        }
        arguments.update(overrides)
        result = source.wait_for_selected_quality_source(**arguments)
        assert json.loads(arguments["evidence_path"].read_text(encoding="utf-8")) == result
        return result

    def sleep(self, seconds: float) -> None:
        self.clock += seconds


@pytest.mark.parametrize("complete", (False, True))
def test_identical_revision_can_use_running_or_successful_main_gates(tmp_path, complete) -> None:
    github = GitHub()
    if complete:
        github.main.update(status="completed", conclusion="success")
    result = github.select(tmp_path)
    assert result["status"] == "success"
    assert result["decision"] == "reuse"
    assert result["sourceRunId"] == SOURCE_ID
    assert result["sourceRunAttempt"] == 1
    assert result["referencedWorkflows"] == github.main["referenced_workflows"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("head_branch", "codex/feature"),
        ("event", "pull_request"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "other/repo"}),
        ("head_repository", {"full_name": "fork/Scriber"}),
        ("created_at", "2026-09-17T16:59:59Z"),
        ("created_at", "2026-09-18T17:00:01Z"),
    ],
)
def test_other_commits_branches_events_repositories_and_old_runs_schedule_local_gates(tmp_path, field, value) -> None:
    github = GitHub()
    github.main[field] = value
    result = github.select(tmp_path)
    assert result["decision"] == "local"
    assert result["status"] == "success"
    assert result["sourceRunId"] == 0


def test_twenty_four_hour_boundary_is_inclusive(tmp_path) -> None:
    github = GitHub()
    github.main["created_at"] = (NOW - timedelta(hours=24)).isoformat()
    assert github.select(tmp_path)["decision"] == "reuse"


@pytest.mark.parametrize("mode", ("missing", "wrong-sha", "branch-only", "extra-workflow", "wrong-ref"))
def test_missing_or_nonimmutable_reusable_workflow_provenance_never_reuses(tmp_path, mode) -> None:
    github = GitHub()
    if mode == "missing":
        github.main.pop("referenced_workflows")
    elif mode == "wrong-sha":
        github.main["referenced_workflows"][0]["sha"] = "b" * 40
    elif mode == "branch-only":
        github.main["referenced_workflows"][0]["path"] = f"{REPO}/{source.REFERENCED_WORKFLOWS[0]}@main"
    elif mode == "wrong-ref":
        github.main["referenced_workflows"][0]["ref"] = "refs/pull/4/merge"
    else:
        github.main["referenced_workflows"].append({"path": "unknown"})
    result = github.select(tmp_path)
    assert result["decision"] == "local"
    assert result["status"] == "success"


def test_api_unavailability_schedules_full_local_gates_without_claiming_successful_evidence(tmp_path) -> None:
    github = GitHub()

    def unavailable(_endpoint, _timeout):
        raise GateError("github_request_failed")

    result = github.select(tmp_path, api=unavailable)
    assert result["decision"] == "local"
    assert result["sourceRunId"] == 0
    assert result["reason"] == "github_request_failed"


@pytest.mark.parametrize("conclusion", ("failure", "timed_out", "cancelled", "neutral", "skipped"))
def test_matching_unsuccessful_main_run_is_fatal_even_without_provenance(tmp_path, conclusion) -> None:
    github = GitHub()
    github.main.update(status="completed", conclusion=conclusion)
    github.main.pop("referenced_workflows")
    result = github.select(tmp_path)
    assert result["decision"] == "failure"
    assert result["status"] == "failure"
    assert result["reason"] == "matching_main_run_unsuccessful"


def test_a_second_green_run_cannot_hide_a_matching_failure(tmp_path) -> None:
    github = GitHub()
    failed = _run(201, main=True)
    failed.update(status="completed", conclusion="failure", created_at="2026-09-18T15:00:00Z")
    github.runs.append(failed)
    assert github.select(tmp_path)["reason"] == "matching_main_run_unsuccessful"


def test_discovered_failure_cannot_be_hidden_by_later_api_unavailability(tmp_path) -> None:
    github = GitHub()
    github.main.update(status="completed", conclusion="failure")

    def api(endpoint, timeout):
        if endpoint == SOURCE_BASE:
            raise GateError("github_request_failed")
        return github.api(endpoint, timeout)

    result = github.select(tmp_path, api=api)
    assert result["decision"] == "failure"
    assert result["reason"] == "matching_main_run_unsuccessful"


def test_assertion_failure_already_visible_in_running_main_run_is_fatal(tmp_path) -> None:
    github = GitHub()
    github.jobs[-1]["conclusion"] = "failure"
    assert github.select(tmp_path)["reason"] == "matching_main_quality_job_unsuccessful"


def test_another_green_run_cannot_hide_an_assertion_failure_in_a_running_matching_run(tmp_path) -> None:
    github = GitHub()
    other = _run(201, main=True)
    other["created_at"] = "2026-09-18T15:00:00Z"
    github.runs.append(other)
    failed_jobs = _jobs(201)
    failed_jobs[0]["conclusion"] = "failure"

    def api(endpoint, timeout):
        if f"/runs/{other['id']}/jobs?" in endpoint:
            return {"jobs": failed_jobs, "total_count": len(failed_jobs)}
        return github.api(endpoint, timeout)

    result = github.select(tmp_path, api=api)
    assert result["decision"] == "failure"
    assert result["reason"] == "matching_main_quality_job_unsuccessful"


def test_discovery_is_bounded_and_times_out_to_local_gates(tmp_path) -> None:
    github = GitHub()

    def api(endpoint, timeout):
        result = github.api(endpoint, timeout)
        github.clock += 61
        return result

    result = github.select(tmp_path, api=api, monotonic=lambda: github.clock)
    assert result["decision"] == "local"
    assert result["reason"] == "source_discovery_timeout"


@pytest.mark.parametrize("prior_success", (False, True))
def test_prior_attempt_failures_cannot_be_rerun_into_reusable_green(tmp_path, prior_success) -> None:
    github = GitHub()
    prior = deepcopy(github.main)
    prior.update(status="completed", conclusion="success" if prior_success else "failure")
    github.prior_attempts[1] = prior
    github.main["run_attempt"] = 2
    github.jobs = _jobs(SOURCE_ID, attempt=2)
    result = github.select(tmp_path)
    assert result["decision"] == ("reuse" if prior_success else "failure")
    if prior_success:
        assert result["sourceRunAttempt"] == 2
    else:
        assert result["reason"] == "matching_main_attempt_unsuccessful"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_attempt", 2),
        ("head_sha", "b" * 40),
        ("head_branch", "main"),
        ("event", "pull_request"),
        ("path", source.SOURCE_WORKFLOW),
        ("repository", {"full_name": "other/repo"}),
    ],
)
def test_selector_independently_checks_current_release_identity(tmp_path, field, value) -> None:
    github = GitHub()
    github.release[field] = value
    result = github.select(tmp_path)
    assert result["decision"] == "failure"
    assert result["reason"] == "release_run_identity_mismatch"


def test_barrier_accepts_only_six_required_main_jobs_and_records_both_runs(tmp_path) -> None:
    github = GitHub()
    result = github.wait(tmp_path)
    assert result["status"] == "success"
    assert result["releaseRunId"] == RELEASE_ID
    assert result["sourceRunId"] == SOURCE_ID
    assert result["jobSelection"] == "canonical-main-same-sha-latest-effective"
    assert len(result["jobs"]) == 6
    source_indices = [index for index, endpoint in enumerate(github.requests) if endpoint == SOURCE_BASE]
    assert len(source_indices) == 2
    for index in source_indices:
        assert github.requests[index - 1] == RELEASE_BASE
        assert github.requests[index + 1] == RELEASE_BASE


def test_running_source_is_waited_for_without_testing_again(tmp_path) -> None:
    github = GitHub()
    github.jobs[-1].update(status="in_progress", conclusion=None)

    def finish(seconds):
        github.sleep(seconds)
        github.jobs[-1].update(status="completed", conclusion="success")

    result = github.wait(tmp_path, sleep=finish)
    assert result["status"] == "success"
    assert result["polls"] == 2


@pytest.mark.parametrize("which", ("release", "source"))
def test_either_run_rerunning_during_job_snapshot_fails_closed(tmp_path, which) -> None:
    github = GitHub()

    def api(endpoint, timeout):
        result = github.api(endpoint, timeout)
        if "/jobs?" in endpoint:
            (github.release if which == "release" else github.main)["run_attempt"] = 2
        return result

    result = github.wait(tmp_path, api=api)
    assert result["status"] == "failure"
    assert result["reason"] == f"{which}_run_identity_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("head_repository", {"full_name": "fork/Scriber"}),
        ("event", "pull_request"),
        ("path", source.RELEASE_WORKFLOW),
        ("referenced_workflows", []),
        ("created_at", "2026-09-17T16:59:59Z"),
    ],
)
def test_source_provenance_drift_at_barrier_is_fatal_without_fallback(tmp_path, field, value) -> None:
    github = GitHub()
    github.main[field] = value
    assert github.wait(tmp_path)["status"] == "failure"


def test_changed_current_release_after_source_read_cannot_authorize_signing(tmp_path) -> None:
    github = GitHub()

    def api(endpoint, timeout):
        result = github.api(endpoint, timeout)
        if endpoint == SOURCE_BASE:
            github.release["head_sha"] = "b" * 40
        return result

    result = github.wait(tmp_path, api=api)
    assert result["reason"] == "release_run_identity_mismatch"
    assert not any("/jobs?" in endpoint for endpoint in github.requests)


def test_main_failure_after_selection_still_blocks_publication(tmp_path) -> None:
    github = GitHub()
    assert github.select(tmp_path)["decision"] == "reuse"
    github.main.update(status="completed", conclusion="failure")
    assert github.wait(tmp_path)["reason"] == "matching_main_run_unsuccessful"


def test_wrong_job_prefix_cannot_satisfy_source_barrier(tmp_path) -> None:
    github = GitHub()
    github.jobs = _jobs(SOURCE_ID, main=False)
    github.main.update(status="completed", conclusion="success")
    assert github.wait(tmp_path)["reason"] == "completed_run_missing_quality_jobs"


def test_local_fallback_still_uses_current_release_required_jobs(tmp_path) -> None:
    github = GitHub()
    github.jobs = _jobs(RELEASE_ID, main=False)
    result = github.wait(tmp_path, source_run_id=0, source_attempt=0)
    assert result["status"] == "success"
    assert result["sourceRunId"] == RELEASE_ID
    assert result["sourceWorkflow"] == source.RELEASE_WORKFLOW
    assert SOURCE_BASE not in github.requests


def test_explicit_non_tag_dispatch_keeps_local_quality_gates(tmp_path) -> None:
    github = GitHub()
    github.release.update(event="workflow_dispatch", head_branch="main")
    github.jobs = _jobs(RELEASE_ID, main=False)
    assert github.wait(tmp_path, source_run_id=0, source_attempt=0)["status"] == "success"


@pytest.mark.parametrize("branch", ("main", "codex/feature", "v0.5.999"))
def test_explicit_dispatch_selects_local_gates_without_discovering_main_runs(tmp_path, branch) -> None:
    github = GitHub()
    github.release.update(event="workflow_dispatch", head_branch=branch)
    result = github.select(tmp_path)
    assert result["status"] == "success"
    assert result["decision"] == "local"
    assert result["reason"] == "explicit_dispatch_uses_local_gates"
    assert result["sourceRunId"] == 0
    assert result["sourceRunAttempt"] == 0
    assert github.requests == [RELEASE_BASE]


def test_source_barrier_unavailability_fails_closed_instead_of_scheduling_new_tests(tmp_path) -> None:
    github = GitHub()

    def unavailable(_endpoint, _timeout):
        raise GateError("github_request_failed")

    result = github.wait(tmp_path, api=unavailable)
    assert result["status"] == "failure"
    assert result["reason"] == "github_request_failed"
