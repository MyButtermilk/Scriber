from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

from backend_runtime.contract import RUNTIME_CONTRACT_REVISION

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bytes_with_retry(path: Path, content: bytes) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            path.write_bytes(content)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.025 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _runtime_identity_tree_sha256(entries: list[dict[str, object]]) -> str:
    canonical = "".join(
        f"{entry['path']}\0{entry['length']}\0{entry['sha256']}\0"
        for entry in sorted(entries, key=lambda item: str(item["path"]))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_release_workflow_uses_adaptive_parallel_cold_producers_and_safe_warm_fallback() -> None:
    raw = _read(".github/workflows/release-windows.yml")
    workflow = yaml.safe_load(raw)
    backend_steps = workflow["jobs"]["prepare-backend-cold"]["steps"]
    backend_upload = next(step for step in backend_steps if step["name"] == "Upload attested backend product")

    assert "release-plan:" in raw
    assert "runs-on: ubuntu-latest" in raw
    assert "actions: read\n      contents: read" in raw
    assert "prepare-tauri-cold:" in raw
    assert "prepare-backend-cold:" in raw
    assert "Build and attest exact Tauri binary" in raw
    assert "Build and attest backend product" in raw
    assert "always() &&" in raw
    assert "Cold products were not requested or did not both validate; using the established single-runner path." in raw
    assert "pattern: scriber-cold-*-product" in raw
    assert "merge-multiple: true" in raw
    assert backend_upload["uses"] == "actions/upload-artifact@v7"
    assert backend_upload["with"]["include-hidden-files"] is True


def test_cold_backend_hidden_files_remain_inside_the_exact_attested_roundtrip() -> None:
    cold_product = _read("scripts/ci/sync_cold_backend_product.ps1")

    assert (REPO_ROOT / "src/assets/.gitkeep").is_file()
    assert "Get-ChildItem -LiteralPath $resolvedProductRoot -Recurse -File -Force" in cold_product
    assert "if ($actualFiles.Count -ne $attestedFiles.Count)" in cold_product
    assert "-not (Test-FileAttestation -Root $resolvedProductRoot -Entry $entry)" in cold_product


def test_release_cache_summary_carries_run_bound_cross_runner_fingerprints() -> None:
    raw = _read(".github/workflows/release-windows.yml")
    workflow = yaml.safe_load(raw)
    build = workflow["jobs"]["build-windows"]
    report = next(step for step in build["steps"] if step["name"] == "Report release cache hits")
    script = report["run"]

    assert "schemaVersion = 2" in script
    assert 'repository = "${{ github.repository }}"' in script
    assert 'runId = [int64]"${{ github.run_id }}"' in script
    assert "headSha = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()" in script
    assert 'eventName = "${{ github.event_name }}"' in script
    assert "ref = [string]$env:GITHUB_REF" in script
    assert 'apiVersion = "1"' in script
    assert "planner = $plannerFingerprints" in script
    assert "packager = $packagerFingerprints" in script
    assert "componentMatches = $componentMatches" in script
    assert "$componentMatches.backendSidecar -and" in script
    assert "$componentMatches.backendRuntime -and" in script
    assert "$componentMatches.tauriAppBinary" in script


def test_release_quality_suite_blocks_packaging_but_not_cold_preparation() -> None:
    raw = _read(".github/workflows/release-windows.yml")
    workflow = yaml.safe_load(raw)
    jobs = workflow["jobs"]

    assert jobs["quality-gates"] == {
        "name": "Exact-revision quality gates",
        "uses": "./.github/workflows/quality-gates.yml",
        "permissions": {"contents": "read"},
    }

    for producer_name in ("prepare-tauri-cold", "prepare-backend-cold"):
        producer = jobs[producer_name]
        condition = " ".join(producer["if"].split())
        assert set(producer["needs"]) == {"release-plan"}
        assert condition == (
            "needs.release-plan.result == 'success' && needs.release-plan.outputs.use-cold-path == 'true'"
        )
        assert "quality-gates" not in producer["needs"]
        assert "needs.quality-gates" not in producer["if"]

    build = jobs["build-windows"]
    build_condition = " ".join(build["if"].split())
    assert set(build["needs"]) == {
        "release-plan",
        "quality-gates",
        "prepare-backend-cold",
        "prepare-tauri-cold",
    }
    assert build_condition == (
        "always() && !cancelled() && needs.release-plan.result == 'success' && needs.quality-gates.result == 'success'"
    )
    assert "cancel-in-progress: ${{ !startsWith(github.ref, 'refs/tags/v') }}" in raw
    assert build["concurrency"] == {
        "group": (
            "${{ startsWith(github.ref, 'refs/tags/v') && "
            "'release-windows-tags' || "
            "format('release-windows-build-{0}', github.ref) }}"
        ),
        "queue": "max",
    }

    publish = next(
        step for step in build["steps"] if step["name"] == "Verify and publish exact GitHub release transaction"
    )
    assert publish["if"] == "needs.release-plan.outputs.official-release == 'true'"
    assert "python -m scripts.ci.publish_github_release" in publish["run"]
    assert '--release-id "${{ steps.uploaded-draft.outputs.release-id }}"' in publish["run"]
    assert "--expected-state-report build\\release-asset-api-roundtrip.json" in publish["run"]


@pytest.mark.parametrize(
    ("plan_result", "use_cold_path", "suite_result", "expected"),
    [
        ("success", True, "pending", True),
        ("success", True, "failure", True),
        ("success", True, "success", True),
        ("success", False, "success", False),
        ("failure", True, "success", False),
    ],
)
def test_release_cold_producer_gate_truth_table(
    plan_result: str,
    use_cold_path: bool,
    suite_result: str,
    expected: bool,
) -> None:
    # The quality suite intentionally does not participate in cold producer
    # scheduling. The packaging job remains the fail-closed quality boundary.
    assert suite_result in {"pending", "success", "failure"}
    actual = plan_result == "success" and use_cold_path
    assert actual is expected


def test_release_planner_probes_exact_restore_and_asset_hashfiles_identities() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/release-windows.yml"))
    jobs = workflow["jobs"]
    planner = next(step for step in jobs["release-plan"]["steps"] if step["name"] == "Select adaptive release path")[
        "run"
    ]
    build_steps = jobs["build-windows"]["steps"]

    backend_identity = "${{ hashFiles('build/cache-keys/backend-sidecar.txt') }}"
    tauri_identity = "${{ hashFiles('build/cache-keys/tauri-app-binary.txt') }}"

    backend_restore = next(step for step in build_steps if step["name"] == "Restore backend sidecar cache")
    backend_asset = next(step for step in build_steps if step["name"] == "Restore backend sidecar release artifact")
    tauri_restore = next(step for step in build_steps if step["name"] == "Restore exact Tauri app binary")

    assert backend_identity in backend_restore["with"]["key"]
    assert backend_identity in backend_asset["run"]
    assert tauri_identity in tauri_restore["with"]["key"]
    assert f'-BackendSidecarHash "{backend_identity}"' in planner
    assert f'-TauriAppBinaryHash "{tauri_identity}"' in planner
    assert "steps.cache-keys.outputs.backend-sidecar-hash" not in planner
    assert "steps.cache-keys.outputs.tauri-app-binary-hash" not in planner


def test_official_release_restores_exact_python_environment_for_installer_smoke() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/release-windows.yml"))
    steps = workflow["jobs"]["build-windows"]["steps"]
    by_name = {step["name"]: step for step in steps}
    official_release = "needs.release-plan.outputs.official-release == 'true'"

    for name in (
        "Restore Python dependency cache",
        "Restore Python venv release artifact",
        "Validate restored Python environment",
        "Restore Python wheelhouse cache",
        "Restore Python wheelhouse release artifact",
        "Restore pip package store",
        "Install Python dependencies",
    ):
        assert official_release in by_name[name]["if"]

    venv_restore = by_name["Restore Python dependency cache"]
    assert venv_restore["with"]["path"] == ".venv"
    assert venv_restore["with"]["key"] == (
        "scriber-python-venv-${{ runner.os }}-${{ steps.setup-python.outputs.python-version }}-"
        "${{ hashFiles('build/cache-keys/python-dependencies.txt') }}"
    )
    assert (
        "steps.python-venv-cache.outputs.cache-hit != 'true'" in by_name["Restore Python venv release artifact"]["if"]
    )
    for name in (
        "Restore Python wheelhouse cache",
        "Restore Python wheelhouse release artifact",
        "Restore pip package store",
    ):
        assert "steps.python-venv-validation.outputs.usable != 'true'" in by_name[name]["if"]

    venv_validation = by_name["Validate restored Python environment"]["run"]
    dependency_install = by_name["Install Python dependencies"]["run"]
    cache_report = by_name["Report release cache hits"]["run"]
    assert "& $venvPython -m pip check" in venv_validation
    assert '& $venvPython -c "import aiohttp"' in venv_validation
    assert "if ($venvUsable)" in dependency_install
    assert "Using restored Python venv; requirements key is current." in dependency_install
    assert (
        "steps.python-wheelhouse-cache.outputs.cache-hit" in dependency_install
        and "steps.python-wheelhouse-artifact.outputs.restored" in dependency_install
    )
    assert '$officialRelease = "${{ needs.release-plan.outputs.official-release }}" -eq "true"' in cache_report
    assert "$pythonEnvironmentRequired = $officialRelease -or $pythonCacheRefresh" in cache_report
    assert '$pythonVenvEffective = if ($pythonEnvironmentRequired -and $pythonVenvValidated -eq "true")' in cache_report
    assert "$pythonWheelhouseEffective = if ($pythonCacheRefresh)" in cache_report
    assert 'elseif ($officialRelease -and $pythonVenvValidated -eq "true")' in cache_report
    assert "$pythonPipStoreNotNeeded = if ($pythonCacheRefresh)" in cache_report
    assert "} elseif ($officialRelease) {" in cache_report

    smoke = by_name["Smoke downloaded installer candidate"]["run"]
    step_names = [step["name"] for step in steps]
    assert step_names.index("Install Python dependencies") < step_names.index("Smoke downloaded installer candidate")
    assert '$smokePython = (Resolve-Path -LiteralPath ".\\.venv\\Scripts\\python.exe"' in smoke
    assert '& $smokePython -c "import aiohttp"' in smoke
    assert "-PythonExecutable $smokePython" in smoke


def test_explicit_main_refresh_repairs_python_caches_despite_exact_backend_hit() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/release-windows.yml"))
    steps = workflow["jobs"]["build-windows"]["steps"]
    by_name = {step["name"]: step for step in steps}
    refresh = "env.SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS == 'true'"

    for name in (
        "Restore Python dependency cache",
        "Restore Python venv release artifact",
        "Validate restored Python environment",
        "Restore Python wheelhouse cache",
        "Restore Python wheelhouse release artifact",
        "Restore pip package store",
        "Install Python dependencies",
    ):
        assert refresh in by_name[name]["if"]

    for name in (
        "Restore Python wheelhouse cache",
        "Restore Python wheelhouse release artifact",
        "Restore pip package store",
    ):
        condition = by_name[name]["if"]
        assert condition.count(refresh) >= 2
        assert "steps.python-venv-validation.outputs.usable != 'true'" in condition

    dependency_install = by_name["Install Python dependencies"]["run"]
    cache_report = by_name["Report release cache hits"]["run"]
    assert '$refreshPythonCaches = $env:SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS -eq "true"' in dependency_install
    assert "$wheelhouseRequired = (-not $venvUsable) -or $refreshPythonCaches" in dependency_install
    assert "$wheelhouseRestored = (" in dependency_install
    assert "python -m venv --clear .venv" in dependency_install
    assert '$expectedPythonVersion = "${{ steps.setup-python.outputs.python-version }}"' in dependency_install
    assert "--dry-run --ignore-installed" in dependency_install
    assert "--no-index --find-links $resolvedWheelhouse" in dependency_install
    assert "\"venv-ready=$(if ($venvReady) { 'true' } else { 'false' })\"" in dependency_install
    assert "\"wheelhouse-ready=$(if ($wheelhouseReady) { 'true' } else { 'false' })\"" in dependency_install
    assert '$pythonCacheRefresh = $env:SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS -eq "true"' in cache_report
    assert "$pythonEnvironmentRequired = $officialRelease -or $pythonCacheRefresh" in cache_report
    assert "$pythonWheelhouseEffective = if ($pythonCacheRefresh)" in cache_report

    venv_publication = by_name["Publish Python venv release artifact"]
    wheelhouse_publication = by_name["Publish Python wheelhouse release artifact"]
    assert venv_publication["id"] == "python-venv-publication"
    assert wheelhouse_publication["id"] == "python-wheelhouse-publication"
    assert "steps.python-dependencies.outputs.venv-ready == 'true'" in venv_publication["if"]
    assert "steps.python-dependencies.outputs.wheelhouse-ready == 'true'" in wheelhouse_publication["if"]
    assert "steps.backend-sidecar-validation.outputs.usable" not in venv_publication["if"]
    assert "steps.backend-sidecar-validation.outputs.usable" not in wheelhouse_publication["if"]
    assert (
        "scriber-python-venv-${{ runner.os }}-${{ steps.setup-python.outputs.python-version }}-"
        "${{ hashFiles('build/cache-keys/python-dependencies.txt') }}.zip"
    ) in venv_publication["run"]
    assert (
        "scriber-python-wheelhouse-${{ runner.os }}-${{ steps.setup-python.outputs.python-version }}-"
        "${{ hashFiles('build/cache-keys/python-dependencies.txt') }}.zip"
    ) in wheelhouse_publication["run"]

    publication_gate = by_name["Require refreshed Python cache artifact publication"]
    assert publication_gate["if"] == "env.SCRIBER_PUBLISH_RELEASE_CACHE_ARTIFACTS == 'true'"
    assert "steps.python-venv-publication.outputs.published" in publication_gate["run"]
    assert "steps.python-wheelhouse-publication.outputs.published" in publication_gate["run"]
    assert "throw" in publication_gate["run"]

    venv_save = by_name["Save Python dependency cache"]["if"]
    wheelhouse_save = by_name["Save Python wheelhouse cache"]["if"]
    pip_store_save = by_name["Save pip package store"]["if"]
    assert "steps.backend-sidecar-validation.outputs.usable" not in venv_save
    assert "steps.backend-sidecar-validation.outputs.usable" not in wheelhouse_save
    assert "steps.backend-sidecar-validation.outputs.usable" not in pip_store_save
    assert "steps.python-dependencies.outputs.venv-ready == 'true'" in venv_save
    assert "steps.python-dependencies.outputs.wheelhouse-ready == 'true'" in wheelhouse_save
    assert "steps.python-dependencies.outputs.wheelhouse-built" not in wheelhouse_save
    assert "steps.python-wheelhouse-cache.outputs.cache-hit != 'true'" in wheelhouse_save

    step_names = [step["name"] for step in steps]
    assert step_names.index("Publish Python venv release artifact") < step_names.index(
        "Require refreshed Python cache artifact publication"
    )
    assert step_names.index("Publish Python wheelhouse release artifact") < step_names.index(
        "Require refreshed Python cache artifact publication"
    )
    assert step_names.index("Require refreshed Python cache artifact publication") < step_names.index(
        "Save Python dependency cache"
    )


def test_cold_backend_restores_and_prunes_shared_rust_dependency_cache() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/release-windows.yml"))
    steps = workflow["jobs"]["prepare-backend-cold"]["steps"]
    step_names = [step["name"] for step in steps]

    assert "Restore Rust dependency state" in step_names
    assert "Remove application outputs from Rust dependency state" in step_names
    restore = next(step for step in steps if step["name"] == "Restore Rust dependency state")
    prune = next(step for step in steps if step["name"] == "Remove application outputs from Rust dependency state")

    assert restore["uses"] == "actions/cache/restore@v6"
    assert set(restore["with"]["path"].splitlines()) == {
        ".cargo/registry/index",
        ".cargo/registry/cache",
        ".cargo/git/db",
        "Frontend/src-tauri/target/release/.fingerprint",
        "Frontend/src-tauri/target/release/build",
        "Frontend/src-tauri/target/release/deps",
        "Frontend/src-tauri/target/release/incremental",
    }
    assert restore["with"]["key"] == (
        "scriber-rust-dependencies-v1-${{ runner.os }}-${{ hashFiles('build/cache-keys/rust-dependencies.txt') }}"
    )
    assert restore["with"]["restore-keys"].strip() == ("scriber-rust-dependencies-v1-${{ runner.os }}-")
    assert "scripts\\ci\\prune_rust_dependency_cache.ps1" in prune["run"]
    assert step_names.index(restore["name"]) < step_names.index(prune["name"])
    assert step_names.index(prune["name"]) < step_names.index("Build and attest backend product")


def test_cold_backend_parallelizes_only_independent_diarization_build() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/release-windows.yml"))
    steps = workflow["jobs"]["prepare-backend-cold"]["steps"]
    build = next(step for step in steps if step["name"] == "Build and attest backend product")
    build_script = build["run"]

    assert "-ParallelizeRustDiarizationBuild" in build_script
    assert "-ParallelizeIndependentBuilds" not in build_script
    assert "-RustAudioIsolatedTarget" not in build_script


@pytest.mark.parametrize(
    (
        "plan_result",
        "suite_result",
        "backend_cold_result",
        "tauri_cold_result",
        "cancelled",
        "expected",
    ),
    [
        ("success", "success", "skipped", "skipped", False, True),
        ("success", "success", "failure", "success", False, True),
        ("success", "success", "success", "failure", False, True),
        ("success", "failure", "skipped", "skipped", False, False),
        ("failure", "success", "skipped", "skipped", False, False),
        ("success", "success", "success", "success", True, False),
    ],
)
def test_release_build_gate_truth_table(
    plan_result: str,
    suite_result: str,
    backend_cold_result: str,
    tauri_cold_result: str,
    cancelled: bool,
    expected: bool,
) -> None:
    # Cold producer results are intentionally absent: any failure or skip must
    # enter the established single-runner fallback instead of blocking build.
    assert backend_cold_result in {"success", "failure", "skipped"}
    assert tauri_cold_result in {"success", "failure", "skipped"}
    actual = not cancelled and plan_result == "success" and suite_result == "success"
    assert actual is expected


def test_distinct_release_tags_share_one_fifo_build_and_publish_queue() -> None:
    def build_queue(ref: str) -> str:
        if ref.startswith("refs/tags/v"):
            return "release-windows-tags"
        return f"release-windows-build-{ref}"

    assert build_queue("refs/tags/v0.5.44") == build_queue("refs/tags/v0.5.45")
    assert build_queue("refs/heads/main") != build_queue("refs/tags/v0.5.45")


def test_runtime_tree_identity_is_compatible_with_windows_powershell() -> None:
    scripts = [
        _read("scripts/build_tauri_backend_sidecar.ps1"),
        _read("scripts/ci/validate_backend_runtime_cache.ps1"),
        _read("scripts/ci/validate_backend_sidecar_cache.ps1"),
    ]
    for script in scripts:
        assert "Get-FileIdentityTreeSha256" in script
        assert ".IndexOf([char]0) -ge 0" in script
        assert ".Contains([char]0)" not in script
    assert "$Object -is [System.Collections.IDictionary]" in scripts[0]
    assert "$Object.Contains($Name)" in scripts[0]

    windows_powershell = shutil.which("powershell")
    if windows_powershell:
        probe = subprocess.run(
            [
                windows_powershell,
                "-NoProfile",
                "-Command",
                "$entry = [ordered]@{ path = 'runtime/file'; length = 1; sha256 = 'a' }; "
                "$path = if ($entry -is [System.Collections.IDictionary] -and $entry.Contains('path')) { [string]$entry['path'] } else { [string]$entry.PSObject.Properties['path'].Value }; "
                "$byPath = [System.Collections.Generic.SortedDictionary[string, object]]::new([System.StringComparer]::Ordinal); "
                "if ($path.IndexOf([char]0) -ge 0) { throw 'false NUL match' }; "
                "$byPath.Add($path, $entry); "
                "if ($byPath.Count -ne 1) { throw 'sorted dictionary failed' }; "
                "Write-Output 'OK'",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert "OK" in probe.stdout


def test_backend_cache_keeps_full_identity_but_uses_a_windows_safe_entry_name() -> None:
    builder = _read("scripts/build_tauri_backend_sidecar.ps1")
    validator = _read("scripts/ci/validate_backend_sidecar_cache.ps1")
    selector = _read("scripts/ci/select_backend_sidecar_cache_entry.ps1")
    cold_product = _read("scripts/ci/sync_cold_backend_product.ps1")

    assert "$cacheEntryName = $cacheKey.Substring(0, 24)" in builder
    assert "$existingIdentityValid" in builder
    assert "$existingCacheKey.StartsWith($cacheEntryName" in builder
    assert "(Get-StringSha256 -Value $existingInputJson) -eq $existingCacheKey" in builder
    assert "cache entry prefix collision" in builder
    assert "$cacheEntryName = $cacheKey.Substring(0, 24)" in selector
    assert "$entries[0].Name -notmatch '^[0-9a-f]{24}$'" in validator
    assert "$cacheEntryName -ne $cacheKey.Substring(0, 24)" in validator
    assert "$backendEntries[0].Name -notmatch '^[0-9a-f]{24}$'" in cold_product
    assert "$backendCacheKey -notmatch '^[0-9a-f]{64}$'" in cold_product
    assert "$backendEntries[0].Name -ne $backendCacheKey.Substring(0, 24)" in cold_product
    assert '$relative.Contains("..")' not in cold_product
    assert "$relative -match '(^|/)\\.\\.($|/)'" in cold_product

    relative = (
        "scriber-backend\\_internal\\pipecat\\cli\\templates\\client\\react-nextjs"
        "\\src\\app\\api\\sessions\\[sessionId]\\[...path]\\route.ts"
    )
    base_length = 240 - 24 - 1 - len(relative)
    base = "C:\\" + ("b" * (base_length - 3))
    bounded = base + ("f" * 24) + "\\" + relative
    legacy = base + ("f" * 64) + "\\" + relative

    assert len(bounded) == 240
    assert len(legacy) == 280
    assert len(legacy) > 260


def test_runtime_cache_binding_occurs_only_after_fresh_build_output() -> None:
    workflow = _read(".github/workflows/release-windows.yml")

    cold_restore = workflow.split("- name: Validate frozen backend runtime\n", 1)[1].split("\n      - name:", 1)[0]
    cold_fresh = workflow.split("- name: Build and attest backend product\n", 1)[1].split("\n      - name:", 1)[0]
    main_restore = workflow.split("- name: Validate frozen backend runtime cache\n", 1)[1].split("\n      - name:", 1)[
        0
    ]
    main_fresh = workflow.split("- name: Validate produced frozen backend runtime\n", 1)[1].split("\n      - name:", 1)[
        0
    ]

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
    handoff_upload = release[
        release.index("Upload passive release-cache maintenance handoff") : release.index(
            "Publish bounded finished component caches in parallel"
        )
    ]
    assert "include-hidden-files: true" in handoff_upload
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


def test_backend_cache_keys_ignore_text_checkout_line_endings() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    spec_path = REPO_ROOT / "packaging/scriber-backend.spec"
    gitkeep_path = REPO_ROOT / "src/assets/.gitkeep"
    original_spec = spec_path.read_bytes()
    original_gitkeep = gitkeep_path.read_bytes()
    normalized_spec = original_spec.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    normalized_gitkeep = original_gitkeep.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    output_lf = REPO_ROOT / "build" / f"test-cache-keys-lf-{uuid.uuid4().hex}"
    output_crlf = REPO_ROOT / "build" / f"test-cache-keys-crlf-{uuid.uuid4().hex}"

    def write_keys(output: Path) -> None:
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

    try:
        _write_bytes_with_retry(spec_path, normalized_spec.encode("utf-8"))
        _write_bytes_with_retry(gitkeep_path, normalized_gitkeep.encode("utf-8"))
        write_keys(output_lf)
        _write_bytes_with_retry(spec_path, normalized_spec.replace("\n", "\r\n").encode("utf-8"))
        _write_bytes_with_retry(gitkeep_path, normalized_gitkeep.replace("\n", "\r\n").encode("utf-8"))
        write_keys(output_crlf)

        lf_manifests = {path.name: path.read_bytes() for path in output_lf.glob("*.txt")}
        crlf_manifests = {path.name: path.read_bytes() for path in output_crlf.glob("*.txt")}
        assert lf_manifests == crlf_manifests
        assert b"packaging/scriber-backend.spec" in lf_manifests["backend-runtime.txt"]
        assert b"src/assets/.gitkeep" in lf_manifests["backend-sidecar.txt"]
        assert lf_manifests["backend-runtime.txt"] == crlf_manifests["backend-runtime.txt"]
        assert lf_manifests["backend-sidecar.txt"] == crlf_manifests["backend-sidecar.txt"]
    finally:
        _write_bytes_with_retry(spec_path, original_spec)
        _write_bytes_with_retry(gitkeep_path, original_gitkeep)
        shutil.rmtree(output_lf, ignore_errors=True)
        shutil.rmtree(output_crlf, ignore_errors=True)


def test_numpy_overlay_lock_invalidates_only_python_product_cache_keys() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    lock_path = REPO_ROOT / "packaging/wheels/numpy-noblas-wheel-lock-v1.json"
    original = lock_path.read_bytes()
    output_before = REPO_ROOT / "build" / f"test-cache-keys-numpy-before-{uuid.uuid4().hex}"
    output_after = REPO_ROOT / "build" / f"test-cache-keys-numpy-after-{uuid.uuid4().hex}"

    def write_keys(output: Path) -> dict[str, bytes]:
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
        return {path.name: path.read_bytes() for path in output.glob("*.txt")}

    try:
        before = write_keys(output_before)
        lock_path.write_bytes(original + b"\n ")
        after = write_keys(output_after)

        assert before.keys() == after.keys()
        changed = {name for name in before if before[name] != after[name]}
        assert changed == {
            "python-dependencies.txt",
            "backend-runtime.txt",
            "backend-sidecar.txt",
        }
        for name in changed:
            manifest = after[name]
            assert b"packaging/wheels/numpy-noblas-wheel-lock-v1.json" in manifest
            assert b"packaging/wheels/numpy-2.4.6+scriber.noblas.1-cp314-cp314-win_amd64.whl" in manifest
        for name in ("backend-runtime.txt", "backend-sidecar.txt"):
            assert b"scripts/validate_numpy_noblas_wheel.py" in after[name]
    finally:
        lock_path.write_bytes(original)
        shutil.rmtree(output_before, ignore_errors=True)
        shutil.rmtree(output_after, ignore_errors=True)


def test_runtime_flavor_and_jit_invalidate_only_python_product_cache_keys() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    output_official = REPO_ROOT / "build" / f"test-cache-keys-official-{uuid.uuid4().hex}"
    output_clang_jit = REPO_ROOT / "build" / f"test-cache-keys-clang-jit-{uuid.uuid4().hex}"

    def write_keys(output: Path, flavor: str, jit: str) -> dict[str, bytes]:
        subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(REPO_ROOT / "scripts/ci/write_release_cache_keys.ps1"),
                "-OutputDir",
                str(output.relative_to(REPO_ROOT)),
                "-PythonRuntimeFlavor",
                flavor,
                "-PythonJitMode",
                jit,
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return {path.name: path.read_bytes() for path in output.glob("*.txt")}

    try:
        official = write_keys(output_official, "Official", "Disabled")
        clang_jit = write_keys(output_clang_jit, "ClangPgo", "Enabled")

        assert official.keys() == clang_jit.keys()
        changed = {name for name in official if official[name] != clang_jit[name]}
        assert changed == {
            "python-dependencies.txt",
            "backend-runtime.txt",
            "backend-sidecar.txt",
        }
        assert b"parameter\tpython-runtime-flavor\tClangPgo" in clang_jit["python-dependencies.txt"]
        assert b"parameter\tpython-jit-mode\tEnabled" in clang_jit["python-dependencies.txt"]
    finally:
        shutil.rmtree(output_official, ignore_errors=True)
        shutil.rmtree(output_clang_jit, ignore_errors=True)


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
    quickjs_files = {
        "qjs.exe": b"stable-quickjs-wrapper",
        "qjs-engine.exe": b"stable-quickjs-engine",
        "LICENSE.quickjs-ng.txt": b"stable-quickjs-license",
        "js-runtime-manifest.json": b"stable-quickjs-manifest",
    }
    executable.write_bytes(b"frozen-python-launcher")
    runtime_data.write_bytes(b"stable-runtime-data")
    for name, content in quickjs_files.items():
        (media_root / name).write_bytes(content)

    input_manifest = {
        "runtimeContract": {
            "name": "scriber-frozen-python-runtime",
            "revision": RUNTIME_CONTRACT_REVISION,
        },
        "python": {"version": "3.14.6", "cacheTag": "cpython-314"},
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
        "runtimeContract": {
            "name": "scriber-frozen-python-runtime",
            "revision": RUNTIME_CONTRACT_REVISION,
        },
        "python": {"version": "3.14.6", "cacheTag": "cpython-314"},
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
            {
                "path": f"media-tools/{name}",
                "length": (media_root / name).stat().st_size,
                "sha256": _sha256(media_root / name),
            }
            for name in quickjs_files
        ],
    }
    (runtime_root / "runtime-layer-manifest.json").write_text(json.dumps(layer_manifest), encoding="utf-8")
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
        missing_envelope = subprocess.run(command[:-1], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        missing_payload = json.loads(missing_envelope.stdout.strip().splitlines()[-1])
        assert missing_payload["usable"] is False
        assert missing_payload["reason"] == "invalid"

        subprocess.run(command + ["-BindIfMissing"], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        envelope = json.loads((cache_root / "workflow-cache-envelope.json").read_text(encoding="utf-8"))
        assert envelope["workflowFingerprint"] == workflow_fingerprint
        assert envelope["innerCacheKey"] == inner_key

        cache_manifest["inputManifest"]["python"]["version"] = "3.14.7"
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
