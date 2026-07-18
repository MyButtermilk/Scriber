from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "prune_obsolete_release_caches.ps1"
REPO = "MyButtermilk/Scriber"
HEAD_SHA = "9" * 40


def _utc_text(value: datetime | None = None) -> str:
    instant = value or datetime.now(timezone.utc)
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cache(
    cache_id: int,
    generation: int,
    fingerprint: str,
    created_at: str,
    *,
    ref: str = "refs/heads/main",
) -> dict[str, object]:
    return {
        "id": cache_id,
        "key": f"scriber-tauri-app-binary-v{generation}-Windows-{fingerprint}",
        "ref": ref,
        "sizeInBytes": 1024 * cache_id,
        "createdAt": created_at,
        "lastAccessedAt": created_at,
    }


def _evidence(cache: dict[str, object], *, generation: int) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "generatedAtUtc": _utc_text(),
        "repo": REPO,
        "cache": {
            "id": cache["id"],
            "key": cache["key"],
            "ref": cache["ref"],
            "generation": generation,
            "fingerprint": str(cache["key"]).rsplit("-", 1)[1],
        },
        "run": {
            "id": 42,
            "attempt": 2,
            "headSha": HEAD_SHA,
            "headBranch": "main",
        },
        "reuse": {
            "exactCacheHit": True,
            "imported": True,
            "seedMiss": False,
        },
    }


def _successful_run() -> dict[str, object]:
    return {
        "id": 42,
        "run_attempt": 2,
        "head_sha": HEAD_SHA,
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
        "updated_at": _utc_text(),
        "repository": {"full_name": REPO},
    }


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is required for release cache promotion tests")
    return executable


def _install_fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True)
    fake_script = fake_bin / "fake_gh.py"
    fake_script.write_text(
        """
import json
import os
from pathlib import Path
import sys


args = sys.argv[1:]


def emit_env(name, fallback):
    print(os.environ.get(name, fallback))


def record_mutation(kind, value):
    path = Path(os.environ["FAKE_GH_MUTATION_LOG"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "value": value}) + "\\n")


if args[:2] == ["cache", "list"]:
    emit_env("FAKE_GH_CACHES", "[]")
elif args[:2] == ["cache", "delete"]:
    record_mutation("cache-delete", int(args[2]))
elif args[:2] == ["release", "list"]:
    print("[]")
elif args[:2] == ["release", "view"]:
    print('{"assets": []}')
elif args[:2] == ["release", "delete"]:
    record_mutation("release-delete", args[2])
elif args[:2] == ["release", "delete-asset"]:
    record_mutation("release-asset-delete", "/".join(args[2:4]))
elif args[:1] == ["api"]:
    if os.environ.get("FAKE_GH_API_FAIL") == "1":
        raise SystemExit(1)
    emit_env("FAKE_GH_RUN", "{}")
else:
    print(f"unexpected fake gh arguments: {args!r}", file=sys.stderr)
    raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )

    if os.name == "nt":
        launcher = fake_bin / "gh.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = fake_bin / "gh"
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(fake_script))} \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)

    mutation_log = tmp_path / "mutations.jsonl"
    return fake_bin, mutation_log


def _write_evidence(tmp_path: Path, content: dict[str, object] | bytes) -> tuple[Path, str]:
    evidence_path = tmp_path / "tauri-promotion-evidence.json"
    raw = content if isinstance(content, bytes) else json.dumps(content).encode("utf-8")
    evidence_path.write_bytes(raw)
    return evidence_path, hashlib.sha256(raw).hexdigest()


def _run_prune(
    tmp_path: Path,
    caches: list[dict[str, object]],
    *,
    extra_args: list[str] | None = None,
    run: dict[str, object] | None = None,
    expected_failure_contains: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    fake_bin, mutation_log = _install_fake_gh(tmp_path)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["FAKE_GH_CACHES"] = json.dumps(caches)
    env["FAKE_GH_RUN"] = json.dumps(run or _successful_run())
    env["FAKE_GH_MUTATION_LOG"] = str(mutation_log)
    env["GITHUB_REF"] = "refs/heads/main"

    command = [
        _powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PRUNE_SCRIPT),
        "-Repo",
        REPO,
    ]
    command.extend(extra_args or [])
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if expected_failure_contains is not None:
        assert completed.returncode != 0
        normalized_output = " ".join((completed.stdout + completed.stderr).split())
        assert " ".join(expected_failure_contains.split()) in normalized_output
        return {}, []
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"

    result = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("apiVersion") == "1":
            result = candidate
            break
    assert result is not None, completed.stdout

    mutations = []
    if mutation_log.exists():
        mutations = [json.loads(line) for line in mutation_log.read_text(encoding="utf-8").splitlines()]
    return result, mutations


def _mutation_ids(mutations: list[dict[str, object]]) -> list[int]:
    return sorted(
        int(mutation["value"])
        for mutation in mutations
        if mutation["kind"] == "cache-delete"
    )


def test_tauri_retention_keeps_one_cache_per_generation_without_promotion(
    tmp_path: Path,
) -> None:
    older_v2 = _cache(10, 2, "a" * 64, "2026-07-17T10:00:00Z")
    newer_v2 = _cache(11, 2, "b" * 64, "2026-07-17T11:00:00Z")
    seed_v3 = _cache(20, 3, "c" * 64, "2026-07-17T12:00:00Z")

    result, mutations = _run_prune(
        tmp_path,
        [older_v2, newer_v2, seed_v3],
        extra_args=["-Apply"],
    )

    assert _mutation_ids(mutations) == [10]
    assert result["mutationSuppressed"] is False
    assert result["tauriPromotionAuthorized"] is False
    assert result["tauriPromotionReason"] == "not-requested"
    assert result["tauriPromotedCache"] is None
    assert result["tauriRetentionProposedDeletionIds"] == [10]
    assert result["tauriRetentionDeletedIds"] == [10]
    assert result["tauriPromotionProposedDeletionIds"] == []
    assert result["deletedCacheIds"] == [10]


def test_valid_promotion_keeps_exact_evidenced_cache_and_retires_older_generations(
    tmp_path: Path,
) -> None:
    old_v2 = _cache(10, 2, "a" * 64, "2026-07-17T10:00:00Z")
    new_v2 = _cache(11, 2, "b" * 64, "2026-07-17T11:00:00Z")
    promoted_v3 = _cache(20, 3, "c" * 64, "2026-07-17T12:00:00Z")
    newer_duplicate_v3 = _cache(21, 3, "d" * 64, "2026-07-17T13:00:00Z")
    evidence_path, evidence_sha = _write_evidence(
        tmp_path,
        _evidence(promoted_v3, generation=3),
    )

    result, mutations = _run_prune(
        tmp_path,
        [old_v2, new_v2, promoted_v3, newer_duplicate_v3],
        extra_args=[
            "-Apply",
            "-TauriPromotionEvidencePath",
            str(evidence_path),
            "-ExpectedTauriPromotionEvidenceSha256",
            evidence_sha,
            "-ExpectedTauriAppKey",
            str(promoted_v3["key"]),
        ],
    )

    assert _mutation_ids(mutations) == [10, 11, 21]
    assert result["mutationSuppressed"] is False
    assert result["tauriPromotionAuthorized"] is True
    assert result["tauriPromotionReason"] == "validated-successful-cache-reuse"
    assert result["tauriPromotionEvidenceSha256"] == evidence_sha
    assert result["tauriPromotedCache"] == {
        "id": 20,
        "key": promoted_v3["key"],
        "ref": "refs/heads/main",
        "generation": 3,
        "fingerprint": "c" * 64,
    }
    assert result["tauriPromotionProposedDeletionIds"] == [10, 11, 21]
    assert result["tauriPromotionDeletedIds"] == [10, 11, 21]
    assert result["tauriRetentionProposedDeletionIds"] == []
    assert result["deletedCacheIds"] == [10, 11, 21]


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("incomplete-pair", "paired-parameters-required"),
        ("missing-file", "evidence-file-missing"),
        ("hash-mismatch", "evidence-sha256-mismatch"),
        ("malformed", "evidence-malformed"),
        ("stale-evidence", "evidence-stale"),
        ("cache-mismatch", "evidence-cache-not-unique"),
        ("failed-run", "github-run-not-successful"),
        ("run-binding-mismatch", "github-run-binding-mismatch"),
        ("seed-miss", "seed-miss-not-promotable"),
        ("unexpected-key", "evidence-cache-key-not-expected"),
    ],
)
def test_invalid_promotion_evidence_suppresses_every_mutation(
    tmp_path: Path,
    case: str,
    expected_reason: str,
) -> None:
    old_v2 = _cache(10, 2, "a" * 64, "2026-07-17T10:00:00Z")
    new_v2 = _cache(11, 2, "b" * 64, "2026-07-17T11:00:00Z")
    promoted_v3 = _cache(20, 3, "c" * 64, "2026-07-17T12:00:00Z")
    obsolete_generic = {
        "id": 30,
        "key": "setup-python-obsolete",
        "ref": "refs/heads/main",
        "sizeInBytes": 30 * 1024,
        "createdAt": "2026-07-17T09:00:00Z",
        "lastAccessedAt": "2026-07-17T09:00:00Z",
    }
    evidence = _evidence(promoted_v3, generation=3)
    run = _successful_run()
    expected_tauri_key = str(promoted_v3["key"])

    if case == "stale-evidence":
        evidence["generatedAtUtc"] = _utc_text(datetime.now(timezone.utc) - timedelta(hours=25))
    elif case == "cache-mismatch":
        evidence["cache"]["id"] = 999  # type: ignore[index]
    elif case == "failed-run":
        run["conclusion"] = "failure"
    elif case == "run-binding-mismatch":
        run["head_sha"] = "8" * 40
    elif case == "seed-miss":
        evidence["reuse"]["seedMiss"] = True  # type: ignore[index]
    elif case == "unexpected-key":
        expected_tauri_key = f"scriber-tauri-app-binary-v3-Windows-{'d' * 64}"

    if case == "malformed":
        evidence_path, evidence_sha = _write_evidence(tmp_path, b"{")
    else:
        evidence_path, evidence_sha = _write_evidence(tmp_path, evidence)

    if case == "incomplete-pair":
        evidence_args = [
            "-TauriPromotionEvidencePath",
            str(evidence_path),
            "-ExpectedTauriAppKey",
            expected_tauri_key,
        ]
    elif case == "missing-file":
        evidence_args = [
            "-TauriPromotionEvidencePath",
            str(tmp_path / "does-not-exist.json"),
            "-ExpectedTauriPromotionEvidenceSha256",
            evidence_sha,
            "-ExpectedTauriAppKey",
            expected_tauri_key,
        ]
    else:
        evidence_args = [
            "-TauriPromotionEvidencePath",
            str(evidence_path),
            "-ExpectedTauriPromotionEvidenceSha256",
            "0" * 64 if case == "hash-mismatch" else evidence_sha,
            "-ExpectedTauriAppKey",
            expected_tauri_key,
        ]

    result, mutations = _run_prune(
        tmp_path,
        [old_v2, new_v2, promoted_v3, obsolete_generic],
        extra_args=["-Apply", *evidence_args],
        run=run,
    )

    assert mutations == []
    assert result["mutationSuppressed"] is True
    assert result["tauriPromotionAuthorized"] is False
    assert result["tauriPromotionReason"] == expected_reason
    assert result["tauriDeletedIds"] == []
    assert result["deletedCacheIds"] == []


def test_valid_promotion_dry_run_reports_without_mutation(tmp_path: Path) -> None:
    old_v2 = _cache(10, 2, "a" * 64, "2026-07-17T10:00:00Z")
    promoted_v3 = _cache(20, 3, "c" * 64, "2026-07-17T12:00:00Z")
    evidence_path, evidence_sha = _write_evidence(
        tmp_path,
        _evidence(promoted_v3, generation=3),
    )

    result, mutations = _run_prune(
        tmp_path,
        [old_v2, promoted_v3],
        extra_args=[
            "-TauriPromotionEvidencePath",
            str(evidence_path),
            "-ExpectedTauriPromotionEvidenceSha256",
            evidence_sha,
            "-ExpectedTauriAppKey",
            str(promoted_v3["key"]),
        ],
    )

    assert mutations == []
    assert result["mode"] == "dry-run"
    assert result["tauriPromotionAuthorized"] is True
    assert result["tauriPromotionProposedDeletionIds"] == [10]
    assert result["tauriPromotionDeletedIds"] == []


def test_tauri_cache_is_not_part_of_generic_rolling_retention() -> None:
    source = PRUNE_SCRIPT.read_text(encoding="utf-8")
    rolling_family_block = source.split("$rollingFamilies = @(", 1)[1].split(")\n\n$deletions", 1)[0]

    assert "Name = 'tauri-app'" not in rolling_family_block
    assert "GenerationPattern = '^scriber-tauri-app-binary-v(?<generation>[123])-Windows-'" in source


def test_current_generation_verification_requires_the_exact_sole_tauri_cache(
    tmp_path: Path,
) -> None:
    rust_key = f"scriber-rust-dependencies-v1-Windows-{'e' * 64}"
    rust_cache = {
        "id": 50,
        "key": rust_key,
        "ref": "refs/heads/main",
        "sizeInBytes": 50 * 1024,
        "createdAt": "2026-07-17T12:00:00Z",
        "lastAccessedAt": "2026-07-17T12:00:00Z",
    }
    promoted_v3 = _cache(20, 3, "c" * 64, "2026-07-17T12:00:00Z")
    args = [
        "-VerifyCurrentGeneration",
        "-ExpectedRustDependencyKey",
        rust_key,
        "-ExpectedTauriAppKey",
        str(promoted_v3["key"]),
    ]

    result, mutations = _run_prune(
        tmp_path / "valid",
        [rust_cache, promoted_v3],
        extra_args=args,
    )
    assert mutations == []
    assert result["verificationPassed"] is True
    assert result["expectedTauriAppMatches"] == 1
    assert result["expectedTauriRefGenerations"] == 1

    old_v2 = _cache(10, 2, "a" * 64, "2026-07-17T10:00:00Z")
    _run_prune(
        tmp_path / "old-generation-remains",
        [rust_cache, old_v2, promoted_v3],
        extra_args=args,
        expected_failure_contains="was not the only Tauri generation on its protected ref",
    )
