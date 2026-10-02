from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from scripts.ci import pre_tag_qualification as gate
from scripts.ci.release_quality_source import RELEASE_WORKFLOW, SOURCE_WORKFLOW
from scripts.ci.wait_release_quality_gates import REQUIRED_JOB_SUFFIXES, GateError

SHA = "a" * 40
NOW = datetime(2026, 10, 2, 10, tzinfo=UTC)
SOURCE_ID = 200
RELEASE_ID = 300
SOURCE_BASE = f"repos/{gate.REPOSITORY}/actions/runs/{SOURCE_ID}"
RELEASE_BASE = f"repos/{gate.REPOSITORY}/actions/runs/{RELEASE_ID}"


class GitHub:
    def __init__(self):
        self.source = {
            "id": SOURCE_ID,
            "run_attempt": 1,
            "head_sha": SHA,
            "repository": {"full_name": gate.REPOSITORY},
            "head_repository": {"full_name": gate.REPOSITORY},
            "path": gate.WORKFLOW,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "created_at": "2026-10-02T08:00:00Z",
            "updated_at": "2026-10-02T08:30:00Z",
            "status": "completed",
            "conclusion": "success",
            "referenced_workflows": [
                {"path": f"{gate.REPOSITORY}/{path}@{SHA}", "sha": SHA, "ref": "refs/heads/main"}
                for path in gate.REFERENCED_WORKFLOWS
            ],
        }
        self.release = {
            **self.source,
            "id": RELEASE_ID,
            "path": RELEASE_WORKFLOW,
            "event": "push",
            "head_branch": "v0.5.130",
            "created_at": "2026-10-02T09:00:00Z",
            "status": "in_progress",
            "conclusion": None,
        }
        self.jobs = [
            {
                "id": index + 1,
                "run_id": SOURCE_ID,
                "run_attempt": 1,
                "head_sha": SHA,
                "name": "Reusable quality gates / " + suffix,
                "status": "completed",
                "conclusion": "success",
            }
            for index, suffix in enumerate(REQUIRED_JOB_SUFFIXES)
        ]
        self.jobs.append({**self.jobs[0], "id": 99, "name": gate.CONTRACT_JOB})
        self.prior = {}
        self.runs = [self.source]
        self.requests = []
        self.total = None
        self.main_runs = []

    def api(self, endpoint, timeout):
        assert 0 < timeout <= 30
        self.requests.append(endpoint)
        if endpoint == SOURCE_BASE:
            return deepcopy(self.source)
        if endpoint == RELEASE_BASE:
            return deepcopy(self.release)
        if endpoint.startswith(SOURCE_BASE + "/attempts/"):
            return deepcopy(self.prior[int(endpoint.rsplit("/", 1)[1])])
        if "/jobs?" in endpoint:
            return {"jobs": deepcopy(self.jobs), "total_count": len(self.jobs)}
        if "/workflows/release-qualification.yml/runs?" in endpoint:
            assert f"event=workflow_dispatch&branch=main&head_sha={SHA}&per_page=100&page=1" in endpoint
            return {
                "workflow_runs": deepcopy(self.runs),
                "total_count": self.total if self.total is not None else len(self.runs),
            }
        if "/workflows/hybrid-pr-checks.yml/runs?" in endpoint:
            return {"workflow_runs": deepcopy(self.main_runs), "total_count": len(self.main_runs)}
        for run in self.main_runs:
            if endpoint.endswith(f"/runs/{run['id']}"):
                return deepcopy(run)
        raise AssertionError(endpoint)

    def check(self, tmp_path, operation="select", **kwargs):
        arguments = dict(
            repo=gate.REPOSITORY,
            run_id=RELEASE_ID,
            attempt=1,
            head_sha=SHA,
            evidence_path=tmp_path / "qualification.json",
            operation=operation,
            api=self.api,
            now=lambda: NOW,
        )
        if operation != "select":
            arguments.update(source_run_id=SOURCE_ID, source_attempt=self.source["run_attempt"])
        arguments.update(kwargs)
        result = gate.qualification_evidence(**arguments)
        assert json.loads(arguments["evidence_path"].read_text()) == result
        return result


@pytest.mark.parametrize("operation", ["select", "release", "verify"])
def test_accepts_completed_exact_sha_pre_tag_evidence(tmp_path, operation):
    result = GitHub().check(tmp_path, operation)
    assert result["status"] == "success"
    assert result["sourceRunId"] == SOURCE_ID
    assert result["sourceRunAttempt"] == 1
    assert len(result["jobs"]) == 6


def test_qualification_contract_can_validate_its_own_running_run(tmp_path):
    github = GitHub()
    github.source.update(status="in_progress", conclusion=None)
    github.jobs.pop()  # The contract itself cannot be complete while it runs.
    assert github.check(tmp_path, "qualify", run_id=SOURCE_ID)["status"] == "success"
    assert github.check(tmp_path)["reason"] == "qualification_not_successful"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 201),
        ("run_attempt", 2),
        ("head_sha", "b" * 40),
        ("repository", {"full_name": "fork/Scriber"}),
        ("head_repository", {"full_name": "fork/Scriber"}),
        ("event", "pull_request"),
        ("event", "push"),
        ("head_branch", "feature"),
        ("path", ".github/workflows/hybrid-pr-checks.yml"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("conclusion", "skipped"),
        ("created_at", "2026-10-01T09:59:59Z"),
        ("updated_at", "2026-10-02T09:00:01Z"),
        ("updated_at", "2026-10-02T07:59:59Z"),
        ("created_at", "malformed"),
        ("referenced_workflows", []),
    ],
)
def test_rejects_invalid_qualification_without_fallback(tmp_path, field, value):
    github = GitHub()
    # Verify uses caller-pinned attempt; source metadata cannot choose its own.
    github.source[field] = value
    result = github.check(tmp_path, "release", source_attempt=1)
    assert result["status"] == "failure"


@pytest.mark.parametrize("mutation", ["sha", "ref", "path", "duplicate"])
def test_rejects_nonimmutable_reusable_workflow_provenance(tmp_path, mutation):
    github = GitHub()
    ref = github.source["referenced_workflows"][0]
    if mutation == "duplicate":
        github.source["referenced_workflows"][1] = deepcopy(ref)
    else:
        ref[mutation] = "untrusted"
    assert github.check(tmp_path)["reason"] == "qualification_provenance_mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-browser",
        "failed-browser",
        "skipped-browser",
        "pending-browser",
        "duplicate",
        "wrong-sha",
        "wrong-run",
        "future-attempt",
        "missing-contract",
        "failed-contract",
        "contract-attempt",
    ],
)
def test_rejects_missing_failed_skipped_or_mismatched_jobs(tmp_path, mutation):
    github = GitHub()
    browser = next(job for job in github.jobs if "real-browser" in job["name"])
    if mutation == "missing-browser":
        github.jobs.remove(browser)
    elif mutation.endswith("-browser"):
        browser["conclusion"] = {"failed-browser": "failure", "skipped-browser": "skipped", "pending-browser": None}[
            mutation
        ]
        if mutation == "pending-browser":
            browser["status"] = "in_progress"
    elif mutation == "duplicate":
        github.jobs.append(deepcopy(browser))
    elif mutation == "wrong-sha":
        browser["head_sha"] = "b" * 40
    elif mutation == "wrong-run":
        browser["run_id"] = 999
    elif mutation == "future-attempt":
        browser["run_attempt"] = 2
    elif mutation == "missing-contract":
        github.jobs.pop()
    elif mutation == "failed-contract":
        github.jobs[-1]["conclusion"] = "failure"
    else:
        github.jobs[-1]["run_attempt"] = 2
    assert github.check(tmp_path)["status"] == "failure"


def test_freshness_boundary_is_inclusive_and_rechecked_before_signing(tmp_path):
    github = GitHub()
    github.source["created_at"] = (NOW - timedelta(hours=24)).isoformat()
    assert github.check(tmp_path)["status"] == "success"
    assert (
        github.check(tmp_path, "release", now=lambda: NOW + timedelta(seconds=1))["reason"]
        == "qualification_expired_or_post_tag"
    )


def test_tag_run_created_before_qualification_completion_is_rejected(tmp_path):
    github = GitHub()
    github.release["created_at"] = "2026-10-02T08:29:59Z"
    assert github.check(tmp_path)["reason"] == "qualification_expired_or_post_tag"


def test_green_rerun_cannot_hide_failed_attempt(tmp_path):
    github = GitHub()
    github.prior[1] = deepcopy(github.source)
    github.prior[1]["conclusion"] = "failure"
    github.source["run_attempt"] = 2
    for job in github.jobs:
        job["run_attempt"] = 2
    assert github.check(tmp_path)["reason"] == "qualification_not_successful"


def test_second_green_run_cannot_hide_matching_failure(tmp_path):
    github = GitHub()
    failed = deepcopy(github.source)
    failed.update(id=201, conclusion="failure")
    github.runs.append(failed)
    original = github.api

    def api(endpoint, timeout):
        if endpoint.endswith("/runs/201"):
            return failed
        return original(endpoint, timeout)

    assert github.check(tmp_path, api=api)["reason"] == "qualification_not_successful"


@pytest.mark.parametrize(
    "failure", ["missing", "incomplete", "unavailable", "timeout", "source-rerun", "release-rerun"]
)
def test_discovery_and_identity_races_fail_closed(tmp_path, failure):
    github = GitHub()
    original = github.api
    if failure == "missing":
        github.runs = []
    if failure == "incomplete":
        github.total = 2

    def api(endpoint, timeout):
        if failure == "unavailable":
            raise GateError("github_request_failed")
        if failure == "source-rerun" and endpoint == SOURCE_BASE and github.requests.count(SOURCE_BASE):
            github.source["run_attempt"] = 2
        if failure == "release-rerun" and endpoint == RELEASE_BASE and github.requests.count(RELEASE_BASE):
            github.release["run_attempt"] = 2
        return original(endpoint, timeout)

    kwargs = {"api": api}
    if failure == "timeout":
        clock = iter([0, 121])
        kwargs["monotonic"] = lambda: next(clock)
    assert github.check(tmp_path, **kwargs)["status"] == "failure"


@pytest.mark.parametrize("accepted", [False, True])
def test_tag_creation_occurs_only_after_qualification_and_uses_exact_sha(monkeypatch, tmp_path, accepted):
    calls = []
    monkeypatch.setattr(gate.subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(
        gate.subprocess,
        "check_output",
        lambda args, **kwargs: (
            f"https://github.com/{gate.REPOSITORY}.git"
            if "remote" in args
            else SHA + "\n"
            if "rev-parse" in args
            else '__version__ = "0.5.130"\n'
        ),
    )

    def qualification(**kwargs):
        assert not any("tag" in command or "push" in command for command in calls)
        assert kwargs["head_sha"] == SHA
        calls.append(["qualified"])
        return {
            "status": "success" if accepted else "failure",
            "reason": "not_qualified",
            "sourceCreatedAt": datetime.now(UTC).isoformat(),
        }

    monkeypatch.setattr(gate, "qualification_evidence", qualification)
    monkeypatch.setattr(gate, "gh_json", lambda *args: {"ref": "refs/heads/main", "object": {"sha": SHA}})
    arguments = dict(
        head_sha=SHA,
        tag="v0.5.130",
        source_run_id=SOURCE_ID,
        source_attempt=1,
        evidence_path=tmp_path / "evidence.json",
    )
    if accepted:
        gate.create_qualified_tag(**arguments)
        assert calls[-2][:5] == ["git", "tag", "-a", "v0.5.130", SHA]
        assert calls[-1] == ["git", "push", "origin", "refs/tags/v0.5.130:refs/tags/v0.5.130"]
        assert all("--force" not in command for command in calls)
    else:
        with pytest.raises(GateError, match="not_qualified"):
            gate.create_qualified_tag(**arguments)
        assert not any("tag" in command or "push" in command for command in calls)


def test_workflow_runs_shared_six_gates_without_signing_or_publication():
    root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load((root / gate.WORKFLOW).read_text())
    assert (payload.get("on") or payload[True]) == {"workflow_dispatch": None}
    assert payload["permissions"] == {"contents": "read"}
    assert payload["jobs"]["quality-gates"]["uses"] == "./.github/workflows/quality-gates.yml"
    assert payload["jobs"]["quality-gates"]["needs"] == "qualification-plan"
    assert payload["jobs"]["qualification-contract"]["needs"] == "quality-gates"
    text = (root / gate.WORKFLOW).read_text()
    assert "github.ref == 'refs/heads/main'" in text
    for prohibited in ["secrets.", "contents: write", "actions: write", "build_windows.ps1", "publish", "signing"]:
        assert prohibited not in text
    for file in ["quality-gates.yml", "python-full-suite.yml"]:
        shared = (root / ".github/workflows" / file).read_text()
        assert "ref: ${{ github.sha }}" in shared
    assert "smoke_real_file_upload_browser.py" in shared


@pytest.mark.parametrize("failure", ["main", "version", "remote", "live-main", "expired"])
def test_tag_helper_refuses_pre_tag_failures_without_creating_tag(monkeypatch, tmp_path, failure):
    calls = []
    monkeypatch.setattr(gate.subprocess, "run", lambda args, **kwargs: calls.append(args))

    def output(args, **kwargs):
        if "remote" in args:
            return (
                "https://github.com/other/Scriber.git"
                if failure == "remote"
                else f"https://github.com/{gate.REPOSITORY}.git"
            )
        if "rev-parse" in args:
            return "b" * 40 if failure == "main" else SHA
        return '__version__ = "0.5.129"' if failure == "version" else '__version__ = "0.5.130"'

    monkeypatch.setattr(gate.subprocess, "check_output", output)
    monkeypatch.setattr(
        gate,
        "qualification_evidence",
        lambda **kwargs: {
            "status": "success",
            "sourceCreatedAt": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        },
    )
    monkeypatch.setattr(
        gate,
        "gh_json",
        lambda *args: {"ref": "refs/heads/main", "object": {"sha": SHA if failure == "expired" else "b" * 40}},
    )
    with pytest.raises(GateError):
        gate.create_qualified_tag(
            head_sha=SHA,
            tag="v0.5.130",
            source_run_id=SOURCE_ID,
            source_attempt=1,
            evidence_path=tmp_path / "evidence.json",
        )
    assert not any("tag" in command or "push" in command for command in calls)


def test_release_cannot_accept_unspecified_source_identity(tmp_path):
    assert (
        GitHub().check(tmp_path, "release", source_run_id=0, source_attempt=0)["reason"]
        == "invalid_qualification_arguments"
    )


def test_verification_rejects_other_failed_qualification_before_tag(tmp_path):
    github = GitHub()
    failed = deepcopy(github.source)
    failed.update(id=201, conclusion="failure")
    github.runs.append(failed)
    original = github.api

    def api(endpoint, timeout):
        return failed if endpoint.endswith("/runs/201") else original(endpoint, timeout)

    assert github.check(tmp_path, "verify", api=api)["reason"] == "qualification_not_successful"


@pytest.mark.parametrize("state", ["success", "failure", "in_progress", "failed-rerun"])
@pytest.mark.parametrize("operation", ["verify", "release"])
def test_known_main_quality_is_checked_before_tag_and_signing_even_after_planning_passes(tmp_path, state, operation):
    github = GitHub()
    assert github.check(tmp_path)["status"] == "success"  # Planning saw no matching main run.
    main = deepcopy(github.source)
    main.update(id=400, path=SOURCE_WORKFLOW, event="push", status="completed", conclusion=state)
    if state == "in_progress":
        main.update(status=state, conclusion=None)
    if state == "failed-rerun":
        main.update(conclusion="success", run_attempt=2)
    github.main_runs = [main]
    original = github.api

    def api(endpoint, timeout):
        if endpoint.endswith("/runs/400/attempts/1"):
            return {**main, "run_attempt": 1, "conclusion": "failure"}
        if "/runs/400/jobs?" in endpoint:
            return {"jobs": [{**job, "run_id": 400} for job in github.jobs], "total_count": len(github.jobs)}
        return original(endpoint, timeout)

    result = github.check(tmp_path, operation, api=api)
    assert result["status"] == ("success" if state == "success" else "failure")


@pytest.mark.parametrize("state", ["failure", "in_progress", "failed-rerun"])
def test_delayed_main_run_discovered_during_final_identity_reads_blocks_signing(tmp_path, state):
    github = GitHub()
    main = deepcopy(github.source)
    main.update(id=400, path=SOURCE_WORKFLOW, event="push", conclusion=state)
    if state == "in_progress":
        main.update(status=state, conclusion=None)
    if state == "failed-rerun":
        main.update(conclusion="success", run_attempt=2)
    original = github.api

    def api(endpoint, timeout):
        if endpoint == RELEASE_BASE and github.requests.count(RELEASE_BASE):
            # The first main discovery was empty; the run becomes visible
            # during the final release-identity read, before signing.
            github.main_runs = [main]
        if endpoint.endswith("/runs/400/attempts/1"):
            return {**main, "run_attempt": 1, "conclusion": "failure"}
        return original(endpoint, timeout)

    result = github.check(tmp_path, "release", api=api)
    assert result["status"] == "failure"
    assert sum("/workflows/hybrid-pr-checks.yml/runs?" in endpoint for endpoint in github.requests) == 2


def test_tag_release_requires_qualification_at_plan_and_before_signing_and_preserves_barriers():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/release-windows.yml").read_text()
    payload = yaml.safe_load(text)
    plan = payload["jobs"]["release-plan"]["steps"]
    qualification = next(step for step in plan if step.get("id") == "pre-tag-qualification")
    assert qualification["if"] == "steps.official-release.outputs.official-release == 'true'"
    assert "pre_tag_qualification select" in qualification["run"]
    assert not qualification.get("continue-on-error")
    build = payload["jobs"]["build-windows"]["steps"]
    names = [step["name"] for step in build]
    assert (
        names.index("Require successful release quality gates")
        < names.index("Recheck pre-tag qualification before signing")
        < names.index("Build Windows installer")
    )
    barrier = build[names.index("Recheck pre-tag qualification before signing")]
    assert "pre_tag_qualification release" in barrier["run"]
    assert "needs.release-plan.outputs.qualification-run-id" in barrier["run"]
    assert "needs.release-plan.outputs.qualification-run-attempt" in barrier["run"]
    assert not barrier.get("continue-on-error")
    assert "release_quality_source wait" in text
    assert (payload.get("on") or payload[True])["push"]["tags"] == ["v*"]
