from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.wait_release_quality_gates import (
    REQUIRED_JOB_NAMES,
    GateError,
    gh_json,
    wait_for_quality_gates,
)

REPO = "MyButtermilk/Scriber"
RUN_ID = 123
ATTEMPT = 2
SHA = "a" * 40
BASE = f"repos/{REPO}/actions/runs/{RUN_ID}"


def successful_jobs() -> list[dict[str, Any]]:
    return [
        {
            "id": index + 1,
            "name": name,
            "run_id": RUN_ID,
            "run_attempt": ATTEMPT,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
        }
        for index, name in enumerate(REQUIRED_JOB_NAMES)
    ]


class FakeGitHub:
    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs = jobs if jobs is not None else successful_jobs()
        self.run = {
            "id": RUN_ID,
            "run_attempt": ATTEMPT,
            "head_sha": SHA,
            "repository": {"full_name": REPO},
            "status": "in_progress",
            "conclusion": None,
        }
        self.requests: list[str] = []
        self.clock = 0.0

    def api(self, endpoint: str, timeout: float) -> dict[str, Any]:
        assert 0 < timeout <= 30
        self.requests.append(endpoint)
        if endpoint == BASE:
            return deepcopy(self.run)
        assert endpoint.startswith(BASE + "/jobs?filter=latest&per_page=100&page=")
        page = int(endpoint.rsplit("=", 1)[1])
        return {"total_count": len(self.jobs), "jobs": deepcopy(self.jobs[(page - 1) * 100 : page * 100])}

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def wait(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        evidence = tmp_path / "evidence.json"
        arguments: dict[str, Any] = {
            "repo": REPO,
            "run_id": RUN_ID,
            "attempt": ATTEMPT,
            "head_sha": SHA,
            "evidence_path": evidence,
            "timeout_seconds": 10,
            "poll_seconds": 2,
            "api": self.api,
            "monotonic": lambda: self.clock,
            "sleep": self.sleep,
        }
        arguments.update(overrides)
        result = wait_for_quality_gates(**arguments)
        assert json.loads(evidence.read_text(encoding="utf-8")) == result
        return result


def test_all_effective_gates_succeed_without_waiting_for_packager(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.jobs.append({"name": "Build and sign Windows installer", "status": "in_progress"})
    result = github.wait(tmp_path)
    assert result["status"] == "success"
    assert result["elapsedSeconds"] == 0
    assert len(result["jobs"]) == len(REQUIRED_JOB_NAMES)
    assert github.requests == [BASE, BASE + "/jobs?filter=latest&per_page=100&page=1", BASE]


def test_latest_effective_jobs_can_retain_same_run_success_from_prior_attempt(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.jobs[0]["run_attempt"] = 1
    result = github.wait(tmp_path)
    assert result["status"] == "success"
    assert result["jobs"][0]["runAttempt"] == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", "neutral", None])
def test_any_non_successful_completed_gate_fails_immediately(tmp_path: Path, conclusion: str | None) -> None:
    github = FakeGitHub()
    github.jobs[-1]["conclusion"] = conclusion
    result = github.wait(tmp_path)
    assert result["reason"] == "quality_job_unsuccessful"
    assert result["status"] == "failure"
    assert github.clock == 0


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_running_gate_times_out_with_evidence(tmp_path: Path, missing: bool) -> None:
    github = FakeGitHub()
    if missing:
        github.jobs.pop()
    else:
        github.jobs[-1].update(status="in_progress", conclusion=None)
    result = github.wait(tmp_path)
    assert result["reason"] == "quality_gates_timeout"
    assert result["pendingJobs"] == [REQUIRED_JOB_NAMES[-1]]
    assert github.clock == 10


def test_pending_gate_can_finish_during_polling(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.jobs[-1].update(status="queued", conclusion=None)

    def finish(seconds: float) -> None:
        github.sleep(seconds)
        github.jobs[-1].update(status="completed", conclusion="success")

    result = github.wait(tmp_path, sleep=finish)
    assert result["status"] == "success"
    assert result["polls"] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", 999), ("head_sha", "b" * 40), ("run_attempt", 1), ("repository", {"full_name": "other/repo"})],
)
def test_wrong_run_binding_fails_before_jobs_are_read(tmp_path: Path, field: str, value: Any) -> None:
    github = FakeGitHub()
    github.run[field] = value
    result = github.wait(tmp_path)
    assert result["reason"] == "run_identity_mismatch"
    assert github.requests == [BASE]


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_id", 999), ("head_sha", "b" * 40), ("run_attempt", 3), ("run_attempt", None), ("id", None)],
)
def test_wrong_job_binding_never_authorizes_release(tmp_path: Path, field: str, value: Any) -> None:
    github = FakeGitHub()
    github.jobs[0][field] = value
    assert github.wait(tmp_path)["reason"] == "job_identity_mismatch"


def test_rerun_during_snapshot_fails_closed(tmp_path: Path) -> None:
    github = FakeGitHub()

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        result = github.api(endpoint, timeout)
        if "/jobs?" in endpoint:
            github.run["run_attempt"] = ATTEMPT + 1
        return result

    assert github.wait(tmp_path, api=api)["reason"] == "run_identity_mismatch"


def test_duplicate_old_success_cannot_override_current_failure(tmp_path: Path) -> None:
    github = FakeGitHub()
    old = deepcopy(github.jobs[0])
    old["run_attempt"] = 1
    old["id"] = 999
    github.jobs[0]["conclusion"] = "failure"
    github.jobs.append(old)
    assert github.wait(tmp_path)["reason"] == "duplicate_effective_quality_job"


def test_new_unlisted_quality_gate_requires_explicit_contract_update(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.jobs.append({"name": "Exact-revision quality gates / New gate"})
    assert github.wait(tmp_path)["reason"] == "unexpected_quality_job"


def test_fetches_every_page_before_accepting_success(tmp_path: Path) -> None:
    github = FakeGitHub([{"name": f"Other job {index}"} for index in range(100)] + successful_jobs())
    result = github.wait(tmp_path)
    assert result["status"] == "success"
    assert BASE + "/jobs?filter=latest&per_page=100&page=2" in github.requests


def test_truncated_page_fails_closed(tmp_path: Path) -> None:
    github = FakeGitHub()

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        result = github.api(endpoint, timeout)
        if "/jobs?" in endpoint:
            result["total_count"] += 1
        return result

    assert github.wait(tmp_path, api=api)["reason"] == "incomplete_jobs_response"


def test_completed_run_cannot_wait_forever_for_a_missing_gate(tmp_path: Path) -> None:
    github = FakeGitHub(successful_jobs()[:-1])
    github.run.update(status="completed", conclusion="success")
    assert github.wait(tmp_path)["reason"] == "completed_run_missing_quality_jobs"


def test_hybrid_verification_uses_same_gate_suffixes(tmp_path: Path) -> None:
    github = FakeGitHub()
    for job in github.jobs:
        job["name"] = job["name"].replace("Exact-revision quality gates", "Reusable quality gates")
    assert github.wait(tmp_path, job_prefix="Reusable quality gates")["status"] == "success"
    assert github.wait(tmp_path, job_prefix="anything")["reason"] == "invalid_arguments"


def test_api_failure_writes_evidence_without_exception_details(tmp_path: Path) -> None:
    github = FakeGitHub()

    def api(_endpoint: str, _timeout: float) -> dict[str, Any]:
        raise RuntimeError("token-that-must-not-leak")

    result = github.wait(tmp_path, api=api)
    assert result["reason"] == "unexpected_gate_error"
    assert "token-that-must-not-leak" not in json.dumps(result)


def test_gh_client_uses_argument_list_and_never_exposes_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert arguments[0:4] == ["gh", "api", "--method", "GET"]
        assert arguments[-1] == BASE
        assert "shell" not in kwargs
        assert kwargs["timeout"] == 4
        return subprocess.CompletedProcess(arguments, 1, "", "token-that-must-not-leak")

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(GateError, match="^github_request_failed$"):
        gh_json(BASE, 4)
