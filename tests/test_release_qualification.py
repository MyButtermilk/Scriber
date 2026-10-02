from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import release_qualification as qualification
from scripts.ci.release_quality_source import RELEASE_WORKFLOW
from scripts.ci.wait_release_quality_gates import REQUIRED_JOB_SUFFIXES, GateError

REPO = qualification.REPOSITORY
SHA = "a" * 40
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
RELEASE_ID = 100
QUALIFICATION_ID = 90
RELEASE_BASE = f"repos/{REPO}/actions/runs/{RELEASE_ID}"
QUALIFICATION_BASE = f"repos/{REPO}/actions/runs/{QUALIFICATION_ID}"


def _run() -> dict[str, Any]:
    return {
        "id": QUALIFICATION_ID,
        "run_attempt": 1,
        "head_sha": SHA,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "path": qualification.QUALIFICATION_WORKFLOW,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "created_at": "2026-10-02T10:00:00Z",
        "updated_at": "2026-10-02T10:30:00Z",
        "status": "completed",
        "conclusion": "success",
        "referenced_workflows": [
            {"path": f"{REPO}/{path}@{SHA}", "sha": SHA, "ref": "refs/heads/main"}
            for path in qualification.REFERENCED_WORKFLOWS
        ],
    }


class GitHub:
    def __init__(self) -> None:
        self.qualification = _run()
        self.release = {
            **_run(),
            "id": RELEASE_ID,
            "path": RELEASE_WORKFLOW,
            "event": "push",
            "head_branch": "v0.5.130",
            "created_at": "2026-10-02T11:00:00Z",
            "status": "in_progress",
            "conclusion": None,
        }
        self.runs = [self.qualification]
        self.jobs = [
            {
                "id": 1000 + index,
                "run_id": QUALIFICATION_ID,
                "run_attempt": 1,
                "head_sha": SHA,
                "name": "Exact-revision quality gates / " + suffix,
                "status": "completed",
                "conclusion": "success",
            }
            for index, suffix in enumerate(REQUIRED_JOB_SUFFIXES)
        ]
        self.requests: list[str] = []
        self.clock = 0.0

    def api(self, endpoint: str, timeout: float) -> dict[str, Any]:
        assert 0 < timeout <= 30
        self.requests.append(endpoint)
        if endpoint == RELEASE_BASE:
            return deepcopy(self.release)
        if endpoint == QUALIFICATION_BASE:
            return deepcopy(self.qualification)
        if "/workflows/qualify-release.yml/runs?" in endpoint:
            assert f"event=workflow_dispatch&branch=main&head_sha={SHA}&per_page=100&page=1" in endpoint
            return {"workflow_runs": deepcopy(self.runs), "total_count": len(self.runs)}
        if endpoint.startswith(QUALIFICATION_BASE + "/jobs?"):
            return {"jobs": deepcopy(self.jobs), "total_count": len(self.jobs)}
        for run in self.runs:
            if endpoint == f"repos/{REPO}/actions/runs/{run['id']}":
                return deepcopy(run)
        raise AssertionError(f"Unexpected API call: {endpoint}")

    def check(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        arguments = {
            "operation": "select",
            "repo": REPO,
            "run_id": RELEASE_ID,
            "attempt": 1,
            "head_sha": SHA,
            "evidence_path": tmp_path / "qualification.json",
            "api": self.api,
            "now": lambda: NOW,
            "monotonic": lambda: self.clock,
        }
        arguments.update(overrides)
        result = qualification.check_qualification(**arguments)
        assert json.loads(arguments["evidence_path"].read_text(encoding="utf-8")) == result
        return result


def test_successful_pre_tag_run_binds_exact_sha_attempt_and_all_six_gates(tmp_path: Path) -> None:
    github = GitHub()
    result = github.check(tmp_path)
    assert result["status"] == "success"
    assert result["qualificationRunId"] == QUALIFICATION_ID
    assert result["qualificationRunAttempt"] == 1
    assert result["qualificationCompletedAt"] == github.qualification["updated_at"]
    assert len(result["jobs"]) == 6
    assert any("real-browser integration" in job["name"] for job in result["jobs"])
    assert github.requests.count(QUALIFICATION_BASE) == 2
    assert github.requests.count(RELEASE_BASE) == 2


def test_missing_qualification_never_falls_back_to_after_tag_checks(tmp_path: Path) -> None:
    github = GitHub()
    github.runs = []
    result = github.check(tmp_path)
    assert result["status"] == "failure"
    assert result["reason"] == "no_fresh_pre_tag_qualification"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("event", "pull_request"),
        ("event", "push"),
        ("head_branch", "codex/feature"),
        ("repository", {"full_name": "other/Scriber"}),
        ("head_repository", {"full_name": "fork/Scriber"}),
        ("path", ".github/workflows/hybrid-pr-checks.yml"),
        ("id", 0),
        ("run_attempt", 0),
        ("created_at", (NOW - timedelta(hours=24, seconds=1)).isoformat()),
    ],
)
def test_incompatible_runs_cannot_qualify_release(tmp_path: Path, field: str, value: Any) -> None:
    github = GitHub()
    github.qualification[field] = value
    assert github.check(tmp_path)["status"] == "failure"


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("in_progress", None), ("queued", None)]
    + [("completed", value) for value in ("failure", "cancelled", "timed_out", "skipped", "neutral", None)],
)
def test_only_completed_success_is_accepted_before_tag(tmp_path: Path, status: str, conclusion: Any) -> None:
    github = GitHub()
    github.qualification.update(status=status, conclusion=conclusion)
    assert github.check(tmp_path)["reason"] == "qualification_not_successful"


@pytest.mark.parametrize("timestamp", ("created_at", "updated_at"))
def test_qualification_after_tag_cannot_rescue_consumed_tag(tmp_path: Path, timestamp: str) -> None:
    github = GitHub()
    github.qualification[timestamp] = "2026-10-02T11:00:01Z"
    if timestamp == "created_at":
        github.qualification["updated_at"] = "2026-10-02T11:00:02Z"
    assert github.check(tmp_path)["reason"] == "qualification_not_completed_before_tag_release"


def test_freshness_boundary_is_inclusive_and_checked_again_before_signing(tmp_path: Path) -> None:
    github = GitHub()
    github.qualification["created_at"] = (NOW - timedelta(hours=24)).isoformat()
    assert github.check(tmp_path)["status"] == "success"
    result = github.check(
        tmp_path,
        operation="verify",
        qualification_run_id=QUALIFICATION_ID,
        qualification_attempt=1,
        now=lambda: NOW + timedelta(seconds=1),
    )
    assert result["reason"] == "qualification_expired_or_invalid_timestamp"


@pytest.mark.parametrize("mode", ("missing", "sha", "ref", "path", "duplicate", "extra"))
def test_reusable_workflows_must_be_immutable_and_same_main_sha(tmp_path: Path, mode: str) -> None:
    github = GitHub()
    references = github.qualification["referenced_workflows"]
    if mode == "missing":
        github.qualification.pop("referenced_workflows")
    elif mode == "sha":
        references[0]["sha"] = "b" * 40
    elif mode == "ref":
        references[0]["ref"] = "refs/pull/74/merge"
    elif mode == "path":
        references[0]["path"] = f"{REPO}/{qualification.REFERENCED_WORKFLOWS[0]}@main"
    elif mode == "duplicate":
        references[1] = references[0]
    else:
        references.append(references[0])
    assert github.check(tmp_path)["reason"] == "qualification_workflow_revision_mismatch"


@pytest.mark.parametrize("mode", ("missing-browser", "failed", "skipped", "pending", "duplicate", "wrong-sha"))
def test_green_run_without_complete_successful_quality_jobs_is_rejected(tmp_path: Path, mode: str) -> None:
    github = GitHub()
    browser = next(job for job in github.jobs if "real-browser integration" in job["name"])
    if mode == "missing-browser":
        github.jobs.remove(browser)
    elif mode in {"failed", "skipped"}:
        browser["conclusion"] = "failure" if mode == "failed" else "skipped"
    elif mode == "pending":
        browser.update(status="in_progress", conclusion=None)
    elif mode == "duplicate":
        github.jobs.append(deepcopy(browser))
    else:
        browser["head_sha"] = "b" * 40
    assert github.check(tmp_path)["status"] == "failure"


@pytest.mark.parametrize("target", ("qualification", "release"))
def test_attempt_changes_during_job_pagination_fail_closed(tmp_path: Path, target: str) -> None:
    github = GitHub()

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        result = github.api(endpoint, timeout)
        if "/jobs?" in endpoint:
            getattr(github, target)["run_attempt"] = 2
        return result

    assert github.check(tmp_path, api=api)["reason"] == (
        "qualification_run_identity_mismatch" if target == "qualification" else "release_run_identity_mismatch"
    )


def test_signing_rechecks_the_pinned_attempt_without_rediscovery(tmp_path: Path) -> None:
    github = GitHub()
    assert github.check(tmp_path)["status"] == "success"
    github.requests.clear()
    github.qualification["run_attempt"] = 2
    result = github.check(tmp_path, operation="verify", qualification_run_id=QUALIFICATION_ID, qualification_attempt=1)
    assert result["reason"] == "qualification_run_identity_mismatch"
    assert not any("/workflows/" in endpoint for endpoint in github.requests)


def test_newer_failed_qualification_cannot_be_hidden_by_older_success(tmp_path: Path) -> None:
    github = GitHub()
    newer = {**_run(), "id": 91, "created_at": "2026-10-02T10:40:00Z", "updated_at": "2026-10-02T10:50:00Z"}
    newer["conclusion"] = "failure"
    github.runs.append(newer)
    assert github.check(tmp_path)["reason"] == "qualification_not_successful"


def test_failed_job_rerun_completed_before_tag_can_retain_prior_successes(tmp_path: Path) -> None:
    github = GitHub()
    github.qualification["run_attempt"] = 2
    github.jobs[-1]["run_attempt"] = 2
    assert github.check(tmp_path)["status"] == "success"


def test_certification_can_verify_its_own_still_running_workflow(tmp_path: Path) -> None:
    github = GitHub()
    github.qualification.update(status="in_progress", conclusion=None)
    result = github.check(tmp_path, operation="certify", run_id=QUALIFICATION_ID)
    assert result["status"] == "success"
    assert result["qualificationCompletedAt"] is None
    assert RELEASE_BASE not in github.requests


@pytest.mark.parametrize("mode", ("truncated", "malformed", "unavailable", "timeout"))
def test_discovery_and_api_failures_never_authorize_release(tmp_path: Path, mode: str) -> None:
    github = GitHub()

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        if mode == "unavailable":
            raise RuntimeError("secret-token-must-not-leak")
        result = github.api(endpoint, timeout)
        if "/workflows/" in endpoint:
            if mode == "truncated":
                result["total_count"] += 1
            elif mode == "malformed":
                result["workflow_runs"].append(None)
        if mode == "timeout":
            github.clock += 61
        return result

    result = github.check(tmp_path, api=api)
    assert result["status"] == "failure"
    assert "secret-token" not in json.dumps(result)


def test_discovery_and_jobs_follow_all_pages(tmp_path: Path) -> None:
    github = GitHub()
    unrelated_runs = [{**_run(), "head_sha": "b" * 40, "id": index + 1000} for index in range(100)]
    unrelated_jobs = [{"name": f"Unrelated job {index}"} for index in range(100)]

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        if "/workflows/" in endpoint:
            return {"total_count": 101, "workflow_runs": unrelated_runs if "page=1&" in endpoint else [_run()]}
        if "/jobs?" in endpoint:
            return {"total_count": 106, "jobs": unrelated_jobs if endpoint.endswith("page=1") else github.jobs}
        return github.api(endpoint, timeout)

    assert github.check(tmp_path, api=api)["status"] == "success"


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "fork/Scriber"},
        {"head_sha": "main"},
        {"candidate_sha": "b" * 40},
        {"ref": "refs/tags/v0.5.130"},
        {"ref": "refs/heads/codex/feature"},
        {"event_name": "push"},
    ],
)
def test_candidate_admission_rejects_wrong_repository_event_ref_or_sha(overrides: dict[str, str]) -> None:
    arguments = {
        "repo": REPO,
        "head_sha": SHA,
        "candidate_sha": SHA,
        "event_name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "api": lambda _endpoint, _timeout: {"sha": SHA},
    }
    arguments.update(overrides)
    assert qualification.check_candidate(**arguments)["status"] == "failure"


@pytest.mark.parametrize("main_sha", (SHA, "b" * 40))
def test_candidate_admission_binds_dispatch_snapshot_to_current_main(main_sha: str) -> None:
    result = qualification.check_candidate(
        repo=REPO,
        head_sha=SHA,
        candidate_sha=SHA,
        event_name="workflow_dispatch",
        ref="refs/heads/main",
        api=lambda _endpoint, _timeout: {"sha": main_sha},
    )
    assert result["status"] == ("success" if main_sha == SHA else "failure")


def test_github_request_failure_is_a_fixed_diagnostic(tmp_path: Path) -> None:
    github = GitHub()

    def api(_endpoint: str, _timeout: float) -> dict[str, Any]:
        raise GateError("github_request_failed")

    assert github.check(tmp_path, api=api)["reason"] == "github_request_failed"


@pytest.mark.parametrize("successful", (True, False))
def test_cli_only_exports_pinned_identities_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successful: bool
) -> None:
    github = GitHub()
    if not successful:
        github.runs = []
    output = tmp_path / "github-output"
    evidence = tmp_path / "cli-evidence.json"
    original = qualification.check_qualification

    def check(**arguments: Any) -> dict[str, Any]:
        return original(**arguments, api=github.api, now=lambda: NOW)

    monkeypatch.setattr(qualification, "check_qualification", check)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_qualification",
            "select",
            "--repo",
            REPO,
            "--run-id",
            str(RELEASE_ID),
            "--run-attempt",
            "1",
            "--head-sha",
            SHA,
            "--evidence",
            str(evidence),
            "--github-output",
            str(output),
        ],
    )
    assert qualification.main() == (0 if successful else 1)
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == ("success" if successful else "failure")
    if successful:
        assert output.read_text(encoding="utf-8") == (
            f"qualification-run-id={QUALIFICATION_ID}\nqualification-run-attempt=1\n"
        )
    else:
        assert not output.exists()
