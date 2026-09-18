from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.wait_release_cold_products import PRODUCTS, wait_for_cold_products

SHA = "a" * 40


class GitHub:
    def __init__(self) -> None:
        self.clock = 0.0
        self.run = {
            "id": 42,
            "run_attempt": 2,
            "head_sha": SHA,
            "repository": {"full_name": "MyButtermilk/Scriber"},
            "status": "in_progress",
            "conclusion": None,
        }
        self.jobs = []
        self.artifacts = []
        for index, (name, (build, upload, artifact)) in enumerate(PRODUCTS.items()):
            self.jobs.append(
                {
                    "id": index + 1,
                    "name": name,
                    "run_id": 42,
                    "run_attempt": 2,
                    "head_sha": SHA,
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [
                        {"name": step, "status": "completed", "conclusion": "success"} for step in (build, upload)
                    ]
                    + [{"name": "Optional dependency publication", "status": "in_progress", "conclusion": None}],
                }
            )
            self.artifacts.append(
                {
                    "id": 100 + index,
                    "name": artifact,
                    "expired": False,
                    "size_in_bytes": 123,
                    "workflow_run": {"id": 42, "head_sha": SHA},
                }
            )

    def api(self, endpoint: str, timeout: float) -> dict[str, Any]:
        assert 0 < timeout <= 30
        for kind, items in (("jobs", self.jobs), ("artifacts", self.artifacts)):
            if f"/{kind}?" in endpoint:
                page = int(endpoint.rsplit("=", 1)[1])
                return {kind: deepcopy(items[(page - 1) * 100 : page * 100]), "total_count": len(items)}
        return deepcopy(self.run)

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def wait(self, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        arguments = {
            "repo": "MyButtermilk/Scriber",
            "run_id": 42,
            "attempt": 2,
            "head_sha": SHA,
            "evidence_path": tmp_path / "products.json",
            "timeout_seconds": 10,
            "poll_seconds": 2,
            "api": self.api,
            "monotonic": lambda: self.clock,
            "sleep": self.sleep,
            **overrides,
        }
        result = wait_for_cold_products(**arguments)
        assert json.loads((tmp_path / "products.json").read_text()) == result
        return result


def test_ready_products_do_not_wait_for_optional_cache_publication(tmp_path: Path) -> None:
    github = GitHub()
    result = github.wait(tmp_path)
    assert result["status"] == "success"
    assert len(result["products"]) == 2
    assert github.clock == 0


@pytest.mark.parametrize("missing", ["job", "steps", "artifact"])
def test_queued_producer_or_not_yet_visible_artifact_waits(tmp_path: Path, missing: str) -> None:
    github = GitHub()
    jobs, artifacts = deepcopy(github.jobs), deepcopy(github.artifacts)
    if missing == "job":
        github.jobs.pop()
    elif missing == "steps":
        github.jobs[-1].update(status="queued", steps=[])
    else:
        github.artifacts.pop()

    def finish(seconds: float) -> None:
        github.sleep(seconds)
        github.jobs, github.artifacts = jobs, artifacts

    assert github.wait(tmp_path, sleep=finish)["status"] == "success"
    assert github.clock == 2


def test_artifact_cannot_bypass_incomplete_upload_step(tmp_path: Path) -> None:
    github = GitHub()
    github.jobs[0]["steps"][1].update(status="in_progress", conclusion=None)
    assert github.wait(tmp_path)["reason"] == "cold_products_timeout"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "neutral", None])
def test_unsuccessful_build_or_upload_never_authorizes_download(tmp_path: Path, conclusion: str | None) -> None:
    github = GitHub()
    github.jobs[0]["steps"][0]["conclusion"] = conclusion
    assert github.wait(tmp_path)["reason"] == "product_step_unsuccessful"


@pytest.mark.parametrize("kind", ["job", "artifact", "step"])
def test_duplicate_candidates_fail_closed(tmp_path: Path, kind: str) -> None:
    github = GitHub()
    if kind == "job":
        github.jobs.append(deepcopy(github.jobs[0]))
    elif kind == "artifact":
        github.artifacts.append(deepcopy(github.artifacts[0]))
    else:
        github.jobs[0]["steps"].append(deepcopy(github.jobs[0]["steps"][0]))
    assert github.wait(tmp_path)["status"] == "failure"


@pytest.mark.parametrize("field,value", [("expired", True), ("size_in_bytes", 0), ("id", None)])
def test_invalid_artifacts_fail_closed(tmp_path: Path, field: str, value: Any) -> None:
    github = GitHub()
    github.artifacts[0][field] = value
    assert github.wait(tmp_path)["reason"] == "artifact_identity_mismatch"


@pytest.mark.parametrize("scope", ["run", "job", "artifact"])
def test_wrong_source_cannot_supply_products(tmp_path: Path, scope: str) -> None:
    github = GitHub()
    source = {"run": github.run, "job": github.jobs[0], "artifact": github.artifacts[0]["workflow_run"]}[scope]
    source["head_sha"] = "b" * 40
    assert github.wait(tmp_path)["status"] == "failure"


def test_rerun_during_snapshot_rejects_preceding_attempt(tmp_path: Path) -> None:
    github = GitHub()

    def api(endpoint: str, timeout: float) -> dict[str, Any]:
        value = github.api(endpoint, timeout)
        if "/artifacts?" in endpoint:
            github.run["run_attempt"] += 1
        return value

    assert github.wait(tmp_path, api=api)["reason"] == "run_identity_mismatch"


def test_all_pages_are_bound_before_accepting_artifacts(tmp_path: Path) -> None:
    github = GitHub()
    github.artifacts[:0] = [{"name": f"other-{index}"} for index in range(100)]
    assert github.wait(tmp_path)["status"] == "success"


def test_completed_unsuccessful_job_cannot_supply_stale_artifact(tmp_path: Path) -> None:
    github = GitHub()
    github.jobs[0].update(status="completed", conclusion="failure")
    assert github.wait(tmp_path)["reason"] == "producer_unsuccessful"


def test_api_failure_records_fixed_diagnostic_without_secrets(tmp_path: Path) -> None:
    github = GitHub()

    def api(_endpoint: str, _timeout: float) -> dict[str, Any]:
        raise RuntimeError("secret-token")

    result = github.wait(tmp_path, api=api)
    assert result["reason"] == "unexpected_product_gate_error"
    assert "secret-token" not in json.dumps(result)
