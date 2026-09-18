"""Join immutable cold products while producers finish optional cache publication.

Only successful build/upload steps and run-bound artifacts authorize a download.
Importers still verify every byte and the quality barrier still gates shipping.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.ci.wait_release_quality_gates import GateError, gh_json

PRODUCTS = {
    "Prepare Tauri binary (cold path)": (
        "Build and attest exact Tauri binary",
        "Upload attested Tauri binary",
        "scriber-cold-tauri-product",
    ),
    "Prepare backend product (cold path)": (
        "Build and attest backend product",
        "Upload attested backend product",
        "scriber-cold-backend-product",
    ),
}


def _items(request: Callable[[str], dict[str, Any]], base: str, kind: str) -> list[dict[str, Any]]:
    items = []
    query = "filter=latest&" if kind == "jobs" else ""
    for page in range(1, 101):
        data = request(f"{base}/{kind}?{query}per_page=100&page={page}")
        batch = data.get(kind)
        total = data.get("total_count")
        if (
            not isinstance(batch, list)
            or not all(isinstance(item, dict) for item in batch)
            or type(total) is not int
            or not 0 <= total <= 10000
            or len(batch) > 100
        ):
            raise GateError("invalid_list_response")
        items.extend(batch)
        if len(batch) < 100:
            if len(items) != total:
                raise GateError("incomplete_list_response")
            return items
    raise GateError("pagination_limit")


def wait_for_cold_products(
    *,
    repo: str,
    run_id: int,
    attempt: int,
    head_sha: str,
    evidence_path: Path,
    timeout_seconds: float = 2400,
    poll_seconds: float = 10,
    api: Callable[[str, float], dict[str, Any]] = gh_json,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    started = monotonic()
    deadline = started + timeout_seconds
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": repo,
        "runId": run_id,
        "runAttempt": attempt,
        "headSha": head_sha,
        "status": "failure",
        "reason": "invalid_arguments",
        "products": [],
        "polls": 0,
    }

    def request(endpoint: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GateError("cold_products_timeout")
        return api(endpoint, min(30, remaining))

    def validate_run(run: dict[str, Any]) -> None:
        repository = run.get("repository")
        if (
            run.get("id") != run_id
            or run.get("run_attempt") != attempt
            or run.get("head_sha") != head_sha
            or not isinstance(repository, dict)
            or str(repository.get("full_name", "")).lower() != repo.lower()
        ):
            raise GateError("run_identity_mismatch")
        if run.get("status") == "completed" and run.get("conclusion") != "success":
            raise GateError("workflow_run_unsuccessful")

    try:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or run_id < 1
            or attempt < 1
            or not 0 < timeout_seconds <= 7200
            or not 0 < poll_seconds <= 60
        ):
            raise GateError("invalid_arguments")
        base = f"repos/{repo}/actions/runs/{run_id}"
        while True:
            evidence["polls"] += 1
            run = request(base)
            validate_run(run)
            jobs = _items(request, base, "jobs")
            artifacts = _items(request, base, "artifacts")
            ready = []
            for name, (build_step, upload_step, artifact_name) in PRODUCTS.items():
                matching_jobs = [job for job in jobs if job.get("name") == name]
                if len(matching_jobs) > 1:
                    raise GateError("duplicate_effective_producer")
                if not matching_jobs:
                    continue
                job = matching_jobs[0]
                if (
                    job.get("run_id") != run_id
                    or job.get("head_sha") != head_sha
                    or type(job.get("id")) is not int
                    or type(job.get("run_attempt")) is not int
                    or not 1 <= job["run_attempt"] <= attempt
                ):
                    raise GateError("producer_identity_mismatch")
                if job.get("status") == "completed" and job.get("conclusion") != "success":
                    raise GateError("producer_unsuccessful")
                if job.get("status") not in {"queued", "waiting", "pending", "in_progress", "completed"} or (
                    job.get("status") != "completed" and job.get("conclusion") is not None
                ):
                    raise GateError("invalid_producer_status")
                steps = job.get("steps")
                if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
                    raise GateError("invalid_producer_steps")
                completed = []
                for step_name in (build_step, upload_step):
                    selected = [step for step in steps if step.get("name") == step_name]
                    if not selected and job.get("status") != "completed":
                        completed.append(False)
                        continue
                    if len(selected) != 1:
                        raise GateError("invalid_required_producer_step")
                    step = selected[0]
                    if step.get("status") == "completed" and step.get("conclusion") != "success":
                        raise GateError("product_step_unsuccessful")
                    completed.append(step.get("status") == "completed" and step.get("conclusion") == "success")
                matches = [artifact for artifact in artifacts if artifact.get("name") == artifact_name]
                if len(matches) > 1:
                    raise GateError("duplicate_product_artifact")
                if not all(completed) or not matches:
                    continue
                artifact = matches[0]
                binding = artifact.get("workflow_run")
                if (
                    not isinstance(binding, dict)
                    or binding.get("id") != run_id
                    or binding.get("head_sha") != head_sha
                    or type(artifact.get("id")) is not int
                    or type(artifact.get("size_in_bytes")) is not int
                    or artifact["size_in_bytes"] <= 0
                    or artifact.get("expired") is not False
                ):
                    raise GateError("artifact_identity_mismatch")
                ready.append(
                    {"jobId": job["id"], "jobName": name, "artifactId": artifact["id"], "artifactName": artifact_name}
                )
            evidence["products"] = ready
            if len(ready) == len(PRODUCTS):
                validate_run(request(base))
                evidence.update(status="success", reason="all_cold_products_ready")
                break
            if run.get("status") == "completed":
                raise GateError("completed_run_missing_products")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise GateError("cold_products_timeout")
            sleep(min(poll_seconds, remaining))
    except GateError as error:
        evidence["reason"] = str(error)
    except Exception:
        evidence["reason"] = "unexpected_product_gate_error"
    finally:
        evidence["elapsedSeconds"] = round(max(0, monotonic() - started), 3)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = evidence_path.with_suffix(evidence_path.suffix + ".tmp")
        temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(evidence_path)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=2400)
    args = parser.parse_args()
    result = wait_for_cold_products(
        repo=args.repo,
        run_id=args.run_id,
        attempt=args.run_attempt,
        head_sha=args.head_sha,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout_seconds,
    )
    if args.github_output and result["status"] == "success":
        artifact_ids = ",".join(str(product["artifactId"]) for product in result["products"])
        with args.github_output.open("a", encoding="utf-8") as target:
            target.write(f"artifact-ids={artifact_ids}\n")
    print(f"Release cold products: {result['status']} ({result['reason']}); evidence: {args.evidence}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
