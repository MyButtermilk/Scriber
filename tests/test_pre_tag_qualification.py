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


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "provenance-changed", "timeout"])
def test_qualification_waits_for_parallel_main_without_ignoring_failures(tmp_path, outcome):
    github = GitHub()
    github.source.update(status="in_progress", conclusion=None)
    github.jobs.pop()
    main = deepcopy(github.source)
    main.update(id=400, path=SOURCE_WORKFLOW, event="push")
    github.main_runs = [main]
    original = github.api
    elapsed = 0.0
    sleeps = []

    def api(endpoint, timeout):
        if "/runs/400/jobs?" in endpoint:
            return {"jobs": [{**job, "run_id": 400} for job in github.jobs], "total_count": len(github.jobs)}
        return original(endpoint, timeout)

    def sleep(seconds):
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds
        if outcome == "timeout":
            return
        main.update(status="completed", conclusion=outcome)
        if outcome == "provenance-changed":
            main.update(conclusion="success", referenced_workflows=[])

    result = github.check(tmp_path, "qualify", run_id=SOURCE_ID, api=api, sleep=sleep, monotonic=lambda: elapsed)
    assert sleeps and all(0 < seconds <= 5 for seconds in sleeps)
    if outcome == "success":
        assert result["status"] == "success"
        assert len(result["jobs"]) == 6
    else:
        assert result["status"] == "failure"
        assert (
            result["reason"]
            == {
                "failure": "matching_main_run_unsuccessful",
                "cancelled": "matching_main_run_unsuccessful",
                "provenance-changed": "source_workflow_provenance_unavailable",
                "timeout": "qualification_verification_timeout",
            }[outcome]
        )
    assert elapsed == (1200 if outcome == "timeout" else 5)


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


def test_workflow_reuses_main_or_runs_shared_six_gates_without_signing_or_publication():
    root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load((root / gate.WORKFLOW).read_text())
    assert (payload.get("on") or payload[True]) == {"workflow_dispatch": None}
    assert payload["permissions"] == {"contents": "read"}
    assert payload["jobs"]["quality-gates"]["uses"] == "./.github/workflows/quality-gates.yml"
    assert payload["jobs"]["quality-gates"]["needs"] == "qualification-plan"
    assert payload["jobs"]["quality-gates"]["if"] == "needs.qualification-plan.outputs.reuse-quality == 'false'"
    reuse = payload["jobs"]["reuse-main-quality"]
    assert reuse["if"] == "needs.qualification-plan.outputs.reuse-quality == 'true'"
    assert reuse["name"] == (
        "Reuse canonical main quality (${{ needs.qualification-plan.outputs.quality-source-run-id }}/"
        "${{ needs.qualification-plan.outputs.quality-source-run-attempt }})"
    )
    contract = payload["jobs"]["qualification-contract"]
    assert contract["needs"] == ["qualification-plan", "quality-gates", "reuse-main-quality"]
    assert contract["if"] == "always()"
    guard = contract["steps"][0]
    assert 'test "$REUSE_RESULT" = success' in guard["run"]
    assert 'test "$LOCAL_RESULT" = skipped' in guard["run"]
    assert 'test "$LOCAL_RESULT" = success' in guard["run"]
    assert 'test "$REUSE_RESULT" = skipped' in guard["run"]
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


class ReusedGitHub(GitHub):
    def __init__(self):
        super().__init__()
        self.main = {**deepcopy(self.source), "id": 400, "path": SOURCE_WORKFLOW, "event": "push"}
        self.main.update(created_at="2026-10-02T07:00:00Z", updated_at="2026-10-02T08:20:00Z")
        self.main_jobs = [{**job, "run_id": 400} for job in self.jobs[:-1]]
        self.main_runs = [self.main]
        self.main_prior = {}
        self.source["referenced_workflows"] = []
        self.marker = {**self.jobs[-1], "id": 100, "name": f"{gate.REUSE_JOB_PREFIX} (400/1)"}
        self.jobs = [
            self.marker,
            self.jobs[-1],
            {**self.jobs[0], "name": "Reusable quality gates", "conclusion": "skipped"},
        ]
        self.clock = 0.0
        self.sleeps = []

    def api(self, endpoint, timeout):
        if "/runs/400/jobs?" in endpoint:
            return {"jobs": deepcopy(self.main_jobs), "total_count": len(self.main_jobs)}
        if endpoint.startswith(f"repos/{gate.REPOSITORY}/actions/runs/400/attempts/"):
            return deepcopy(self.main_prior[int(endpoint.rsplit("/", 1)[1])])
        return super().api(endpoint, timeout)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def plan(self, tmp_path, **kwargs):
        self.source.update(status="in_progress", conclusion=None)
        arguments = dict(
            repo=gate.REPOSITORY,
            run_id=SOURCE_ID,
            attempt=1,
            head_sha=SHA,
            evidence_path=tmp_path / "plan.json",
            api=self.api,
            now=lambda: NOW,
            monotonic=lambda: self.clock,
            sleep=self.sleep,
            timeout_seconds=10,
            discovery_grace_seconds=0,
        )
        arguments.update(kwargs)
        return gate.plan_qualification(**arguments)


def test_planner_reuses_completed_main_gates_without_own_workflow_provenance(tmp_path):
    github = ReusedGitHub()
    result = github.plan(tmp_path)
    assert result["status"] == "success"
    assert result["decision"] == "reuse"
    assert result["sourceRunId"] == 400
    assert result["sourceRunAttempt"] == 1
    assert len(result["jobs"]) == 6
    assert not github.sleeps


@pytest.mark.parametrize("appears", [False, True])
def test_planner_waits_for_delayed_main_discovery_before_fallback(tmp_path, appears):
    github = ReusedGitHub()
    github.main_runs = []

    def sleep(seconds):
        github.sleep(seconds)
        if appears:
            github.main_runs = [github.main]

    result = github.plan(tmp_path, discovery_grace_seconds=5, sleep=sleep)
    assert github.clock == 5
    assert result["status"] == "success"
    assert result["decision"] == ("reuse" if appears else "local")


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_planner_waits_for_pending_main_before_provenance_and_jobs_materialize(tmp_path, outcome):
    github = ReusedGitHub()
    references, jobs = github.main["referenced_workflows"], github.main_jobs
    github.main.update(status="queued", conclusion=None, referenced_workflows=[])
    github.main_jobs = []

    def sleep(seconds):
        github.sleep(seconds)
        if outcome != "timeout":
            github.main.update(status="completed", conclusion=outcome, referenced_workflows=references)
            github.main_jobs = jobs

    result = github.plan(tmp_path, sleep=sleep)
    assert result["status"] == ("success" if outcome == "success" else "failure")
    assert github.clock == (10 if outcome == "timeout" else 5)
    if outcome == "success":
        assert result["decision"] == "reuse"
    else:
        assert result["reason"] == (
            "qualification_planning_timeout" if outcome == "timeout" else "matching_main_run_unsuccessful"
        )


@pytest.mark.parametrize("outcome", ["success", "failure", "rerun"])
def test_planner_rediscovers_source_that_changes_during_a_snapshot(tmp_path, outcome):
    github = ReusedGitHub()
    github.main.update(status="in_progress", conclusion=None)
    original = github.api
    source_reads = 0

    def api(endpoint, timeout):
        nonlocal source_reads
        if endpoint.endswith("/runs/400"):
            source_reads += 1
            if source_reads == 2:
                github.main.update(status="completed", conclusion="success", updated_at="2026-10-02T08:21:00Z")
                if outcome == "failure":
                    github.main["conclusion"] = "failure"
                elif outcome == "rerun":
                    github.main_prior[1] = {**github.main, "conclusion": "failure"}
                    github.main["run_attempt"] = 2
        return original(endpoint, timeout)

    result = github.plan(tmp_path, api=api)
    assert github.clock == 5
    assert result["status"] == ("success" if outcome == "success" else "failure")
    if outcome == "success":
        assert result["decision"] == "reuse"
    else:
        assert result["reason"].startswith("matching_main_")


@pytest.mark.parametrize("missing", ["provenance", "job", "source", "api"])
def test_planner_uses_local_gates_when_source_evidence_is_unavailable(tmp_path, missing):
    github = ReusedGitHub()
    if missing == "provenance":
        github.main["referenced_workflows"] = []
    elif missing == "job":
        github.main_jobs.pop()
    elif missing == "source":
        github.main_runs = []
    original = github.api

    def api(endpoint, timeout):
        if missing == "api" and "/workflows/hybrid-pr-checks.yml/" in endpoint:
            raise GateError("github_request_failed")
        return original(endpoint, timeout)

    result = github.plan(tmp_path, api=api)
    assert result["status"] == "success"
    assert result["decision"] == "local"
    assert result["sourceRunId"] == result["sourceRunAttempt"] == 0


@pytest.mark.parametrize("failure", ["run", "job", "prior-attempt", "other-run"])
def test_planner_never_falls_back_past_a_known_failure(tmp_path, failure):
    github = ReusedGitHub()
    if failure == "run":
        github.main.update(conclusion="failure", referenced_workflows=[])
    elif failure == "job":
        github.main.update(status="in_progress", conclusion=None, referenced_workflows=[])
        github.main_jobs[0]["conclusion"] = "failure"
    elif failure == "prior-attempt":
        github.main_prior[1] = {**github.main, "conclusion": "failure"}
        github.main["run_attempt"] = 2
    else:
        github.main["referenced_workflows"] = []
        github.main_runs.append({**github.main, "id": 401, "conclusion": "failure"})
    result = github.plan(tmp_path)
    assert result["status"] == "failure"
    assert result["reason"].startswith("matching_main_")


@pytest.mark.parametrize("operation", ["select", "verify", "release", "qualify"])
def test_reused_qualification_rechecks_api_bound_source_at_every_boundary(tmp_path, operation):
    github = ReusedGitHub()
    kwargs = {}
    if operation == "qualify":
        # GitHub can leave updated_at at dispatch time while its contract runs.
        # The selected main run completes later, during the planner's wait.
        github.source.update(status="in_progress", conclusion=None, updated_at=github.source["created_at"])
        kwargs["run_id"] = SOURCE_ID
    result = github.check(tmp_path, operation, **kwargs)
    assert result["status"] == "success"
    assert result["sourceRunId"] == SOURCE_ID
    assert result["qualitySourceRunId"] == 400
    assert result["qualitySourceRunAttempt"] == 1
    assert result["qualityMode"] == "main"
    assert len(result["jobs"]) == 6


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "failed", "skipped", "wrong-run", "attempt", "sha", "id", "malformed", "mixed"]
)
def test_reuse_marker_must_be_unique_successful_and_bound_to_this_qualification(tmp_path, mutation):
    github = ReusedGitHub()
    if mutation == "missing":
        github.jobs.remove(github.marker)
    elif mutation == "duplicate":
        github.jobs.append(deepcopy(github.marker))
    elif mutation in {"failed", "skipped"}:
        github.marker["conclusion"] = mutation
    elif mutation == "wrong-run":
        github.marker["run_id"] = 999
    elif mutation == "attempt":
        github.marker["run_attempt"] = 2
    elif mutation == "sha":
        github.marker["head_sha"] = "b" * 40
    elif mutation == "id":
        github.marker["id"] = -1
    elif mutation == "malformed":
        github.marker["name"] = gate.REUSE_JOB_PREFIX + " (400/01)"
    else:
        github.jobs.append({**github.main_jobs[0], "run_id": SOURCE_ID})
    assert github.check(tmp_path)["status"] == "failure"


@pytest.mark.parametrize("mutation", ["rerun", "failed", "provenance", "expired", "too-late", "missing-browser"])
def test_reused_source_cannot_change_or_be_replaced_by_a_newer_green_run(tmp_path, mutation):
    github = ReusedGitHub()
    assert github.check(tmp_path)["status"] == "success"
    if mutation == "rerun":
        github.main["run_attempt"] = 2
    elif mutation == "failed":
        github.main["conclusion"] = "failure"
    elif mutation == "provenance":
        github.main["referenced_workflows"] = []
    elif mutation == "expired":
        github.main["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    elif mutation == "too-late":
        github.main["updated_at"] = "2026-10-02T08:31:00Z"
    else:
        github.main_jobs = [job for job in github.main_jobs if "real-browser" not in job["name"]]
    github.main_runs.append({**GitHub().source, "id": 401, "path": SOURCE_WORKFLOW, "event": "push"})
    assert github.check(tmp_path, "release")["status"] == "failure"


def test_explicit_local_fallback_uses_own_gates_without_trusting_incompatible_main(tmp_path):
    github = ReusedGitHub()
    github.jobs = GitHub().jobs + [{**github.marker, "name": gate.REUSE_JOB_PREFIX + " (0/0)", "conclusion": "skipped"}]
    github.source["referenced_workflows"] = GitHub().source["referenced_workflows"]
    github.main["referenced_workflows"] = []
    result = github.check(tmp_path)
    assert result["status"] == "success"
    assert result["qualityMode"] == "local"
    assert "qualitySourceRunId" not in result
    github.main["conclusion"] = "failure"
    assert github.check(tmp_path)["reason"] == "matching_main_run_unsuccessful"


def test_final_local_fallback_still_rejects_unavailable_main_failure_audit(tmp_path):
    github = ReusedGitHub()
    github.jobs = GitHub().jobs + [{**github.marker, "name": gate.REUSE_JOB_PREFIX + " (0/0)", "conclusion": "skipped"}]
    github.source["referenced_workflows"] = GitHub().source["referenced_workflows"]
    original = github.api

    def api(endpoint, timeout):
        if "/workflows/hybrid-pr-checks.yml/" in endpoint:
            raise GateError("github_request_failed")
        return original(endpoint, timeout)

    assert github.check(tmp_path, "release", api=api)["reason"] == "github_request_failed"


def test_reuse_job_revalidates_the_planners_pinned_source(tmp_path):
    github = ReusedGitHub()
    github.source.update(status="in_progress", conclusion=None)
    arguments = dict(
        repo=gate.REPOSITORY,
        run_id=SOURCE_ID,
        attempt=1,
        head_sha=SHA,
        source_run_id=400,
        source_attempt=1,
        evidence_path=tmp_path / "reuse.json",
        api=github.api,
        now=lambda: NOW,
    )
    assert gate.reuse_main_quality(**arguments)["status"] == "success"
    github.main["run_attempt"] = 2
    assert gate.reuse_main_quality(**arguments)["reason"] == "source_run_identity_mismatch"


def test_plan_cli_publishes_only_api_verified_source_ids(monkeypatch, tmp_path):
    evidence = tmp_path / "plan.json"
    output = tmp_path / "outputs"
    github = ReusedGitHub()
    github.source.update(status="in_progress", conclusion=None)
    original = gate.plan_qualification
    monkeypatch.setattr(
        gate, "plan_qualification", lambda **kwargs: original(**kwargs, api=github.api, now=lambda: NOW)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "pre_tag_qualification",
            "plan",
            "--repo",
            gate.REPOSITORY,
            "--run-id",
            str(SOURCE_ID),
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
    assert gate.main() == 0
    assert output.read_text() == "reuse-quality=true\nquality-source-run-id=400\nquality-source-run-attempt=1\n"
