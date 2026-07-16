from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _runtime_identity_tree_sha256(entries: list[dict[str, object]]) -> str:
    canonical = "".join(
        f"{entry['path']}\0{entry['length']}\0{entry['sha256']}\0"
        for entry in sorted(entries, key=lambda item: str(item["path"]))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_release_workflow_uses_adaptive_parallel_cold_producers_and_safe_warm_fallback() -> None:
    workflow = _read(".github/workflows/release-windows.yml")

    assert "release-plan:" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "actions: read\n      contents: read" in workflow
    assert "prepare-tauri-cold:" in workflow
    assert "prepare-backend-cold:" in workflow
    assert "needs: release-plan" in workflow
    assert workflow.count("if: needs.release-plan.outputs.use-cold-path == 'true'") == 2
    assert "Build and attest exact Tauri binary" in workflow
    assert "Build and attest backend product" in workflow
    assert "always() &&" in workflow
    assert "Cold products were not requested or did not both validate; using the established single-runner path." in workflow
    assert "pattern: scriber-cold-*-product" in workflow
    assert "merge-multiple: true" in workflow


def test_runtime_cache_binding_occurs_only_after_fresh_build_output() -> None:
    workflow = _read(".github/workflows/release-windows.yml")

    cold_restore = workflow.split("- name: Validate frozen backend runtime\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    cold_fresh = workflow.split("- name: Build and attest backend product\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    main_restore = workflow.split("- name: Validate frozen backend runtime cache\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    main_fresh = workflow.split("- name: Validate produced frozen backend runtime\n", 1)[1].split(
        "\n      - name:", 1
    )[0]

    assert "-BindIfMissing" not in cold_restore
    assert "-BindIfMissing" in cold_fresh
    assert "-BindIfMissing" not in main_restore
    assert "-BindIfMissing" in main_fresh


def test_tag_cache_publication_is_detached_and_passive() -> None:
    release = _read(".github/workflows/release-windows.yml")
    maintenance = _read(".github/workflows/release-cache-maintenance.yml")

    assert "Stage passive release-cache maintenance handoff" in release
    assert "retention-days: 1" in release
    assert "compression-level: 0" in release
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in release
    assert "workflow_run:" in maintenance
    assert "actions: write\n  contents: write" in maintenance
    assert maintenance.index("Download and prevalidate passive maintenance handoff") < maintenance.index(
        "Checkout exact completed release source"
    )
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in maintenance
    assert "persist-credentials: false" in maintenance
    assert "sync_cold_backend_product.ps1" in maintenance
    assert "-Mode Import" in maintenance
    assert "sync_release_cache_handoff.ps1" in maintenance
    assert "Validate imported cache payload without executing it" in maintenance
    assert "validate_backend_sidecar_cache.ps1 -FailIfUnusable" in maintenance
    assert "Recheck stale-release guard immediately before mutation" in maintenance
    assert "steps.mutation-guard.outputs.current == 'true'" in maintenance
    assert "Require every requested cache publication" in maintenance
    assert "steps.publish-finished-caches.outcome == 'skipped'" in maintenance
    assert "steps.publish-finished-caches.outputs.failed-count == '0'" in maintenance
    assert "Invoke-Expression" not in maintenance
    assert "-EncodedCommand" not in maintenance


def test_release_cache_generation_includes_backend_runtime() -> None:
    publisher = _read("scripts/ci/publish_finished_component_caches_parallel.ps1")
    generic = _read("scripts/ci/publish_release_cache_artifact.ps1")
    prune = _read("scripts/ci/prune_obsolete_release_caches.ps1")

    assert "PublishBackendRuntime" in publisher
    assert 'SourcePath = "build\\tauri-sidecar-runtime-cache"' in publisher
    assert "backend-runtime" in generic
    assert "release-cache-backend-runtime-v1" in prune
    assert "scriber-backend-runtime-v1-Windows-python-" in prune


def test_release_cache_key_files_are_lf_utf8_without_bom(tmp_path: Path) -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")
    output = REPO_ROOT / "build" / f"test-cache-keys-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(REPO_ROOT / "scripts/ci/write_release_cache_keys.ps1"),
                "-OutputDir",
                str(output.relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        for path in output.glob("*.txt"):
            content = path.read_bytes()
            assert not content.startswith(b"\xef\xbb\xbf")
            assert b"\r" not in content
            assert content.endswith(b"\n")
    finally:
        shutil.rmtree(output, ignore_errors=True)

    writer = _read("scripts/ci/write_release_cache_keys.ps1")
    assert '$normalized = $Value -replace "\\r\\n", "`n" -replace "\\r", "`n"' in writer
    assert "[System.Text.UTF8Encoding]::new($false)" in writer
    assert "[System.StringComparer]::Ordinal" in writer
    assert "[System.IO.Path]::GetRelativePath" in writer
    assert "MakeRelativeUri" not in writer


def test_runtime_cache_validator_roundtrip_and_tamper_rejection() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    cache_root = REPO_ROOT / "build" / f"test-runtime-cache-{uuid.uuid4().hex}"
    runtime_root = cache_root / "scriber-backend"
    internal = runtime_root / "_internal"
    media_root = cache_root / "media-tools"
    internal.mkdir(parents=True)
    media_root.mkdir(parents=True)
    executable = runtime_root / "scriber-backend.exe"
    runtime_data = internal / "runtime.dat"
    deno = media_root / "deno.exe"
    executable.write_bytes(b"frozen-python-launcher")
    runtime_data.write_bytes(b"stable-runtime-data")
    deno.write_bytes(b"stable-deno-runtime")

    input_manifest = {
        "runtimeContract": {"name": "scriber-frozen-python-runtime", "revision": 1},
        "python": {"version": "3.13.14", "cacheTag": "cpython-313"},
    }
    inner_key = hashlib.sha256(_compact(input_manifest).encode()).hexdigest()
    runtime_files = [
        {"path": "_internal/runtime.dat", "length": runtime_data.stat().st_size, "sha256": _sha256(runtime_data)},
        {"path": "scriber-backend.exe", "length": executable.stat().st_size, "sha256": _sha256(executable)},
    ]
    tree_sha = _runtime_identity_tree_sha256(runtime_files)
    layer_manifest = {
        "schemaVersion": 1,
        "name": "scriber-backend-runtime-layer",
        "cacheKey": inner_key,
        "runtimeContract": {"name": "scriber-frozen-python-runtime", "revision": 1},
        "python": {"version": "3.13.14", "cacheTag": "cpython-313"},
        "executable": {"sha256": _sha256(executable), "length": executable.stat().st_size},
        "content": {"fileCount": 2, "treeSha256": tree_sha, "files": runtime_files},
    }
    cache_manifest = {
        "apiVersion": 1,
        "generatedAt": "2026-07-16T00:00:00Z",
        "cacheKey": inner_key,
        "sidecarSha256": _sha256(executable),
        "sidecarLength": executable.stat().st_size,
        "inputManifest": input_manifest,
        "runtimeFiles": runtime_files,
        "stableMediaFiles": [
            {"path": "media-tools/deno.exe", "length": deno.stat().st_size, "sha256": _sha256(deno)}
        ],
    }
    (runtime_root / "runtime-layer-manifest.json").write_text(
        json.dumps(layer_manifest), encoding="utf-8"
    )
    manifest_path = cache_root / "runtime-cache-manifest.json"
    manifest_path.write_text(json.dumps(cache_manifest), encoding="utf-8")
    workflow_fingerprint = "f" * 64
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(REPO_ROOT / "scripts/ci/validate_backend_runtime_cache.ps1"),
        "-ExpectedWorkflowFingerprint",
        workflow_fingerprint,
        "-CacheRoot",
        str(cache_root.relative_to(REPO_ROOT)),
        "-FailIfUnusable",
    ]
    try:
        missing_envelope = subprocess.run(
            command[:-1], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        missing_payload = json.loads(missing_envelope.stdout.strip().splitlines()[-1])
        assert missing_payload["usable"] is False
        assert missing_payload["reason"] == "invalid"

        subprocess.run(command + ["-BindIfMissing"], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        envelope = json.loads((cache_root / "workflow-cache-envelope.json").read_text(encoding="utf-8"))
        assert envelope["workflowFingerprint"] == workflow_fingerprint
        assert envelope["innerCacheKey"] == inner_key

        cache_manifest["inputManifest"]["python"]["version"] = "3.13.15"
        manifest_path.write_text(json.dumps(cache_manifest), encoding="utf-8")
        tampered = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        assert tampered.returncode != 0
        assert "internal key" in (tampered.stdout + tampered.stderr)
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def test_passive_handoff_rejects_duplicate_manifest_paths() -> None:
    handoff = _read("scripts/ci/sync_release_cache_handoff.ps1")
    cold = _read("scripts/ci/sync_cold_backend_product.ps1")

    assert "HashSet[string]" in handoff
    assert "-not $seen.Add($relative)" in handoff
    assert "$actualByPath.Count -ne $Entries.Count" in handoff
    assert "-not $attestationPaths.Add([string]$entry.path)" in cold
    assert "passive" in handoff
    assert "never executes" in handoff
