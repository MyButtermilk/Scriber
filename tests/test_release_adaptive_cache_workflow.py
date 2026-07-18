from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from backend_runtime.contract import RUNTIME_CONTRACT_REVISION


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


def test_release_path_planner_probes_hashfiles_digest_for_current_cache_generation() -> None:
    planner = _read("scripts/ci/plan_release_windows_path.ps1")
    workflow = _read(".github/workflows/release-windows.yml")

    assert "Convert-ManifestFingerprintToHashFilesFingerprint" in planner
    assert '$tauriActionsKey = "scriber-tauri-app-binary-v2-$RunnerOs-$tauriActionsHash"' in planner
    assert workflow.count(
        "key: scriber-tauri-app-binary-v2-${{ runner.os }}-"
        "${{ hashFiles('build/cache-keys/tauri-app-binary.txt') }}"
    ) == 2

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    manifest_fingerprint = "874af53b8de03b013f5d8af466492c181b8b9365fa7c515c2eeb81ae6137a86c"
    hashfiles_fingerprint = "ab399919060c2d1c346de94bbfd5abb22d3cd1b6e69a4c1c7af62be139bb2d81"
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(REPO_ROOT / "scripts/ci/plan_release_windows_path.ps1"),
            "-Repo",
            "MyButtermilk/Scriber",
            "-GitRef",
            "refs/tags/v0.5.37",
            "-PythonVersion",
            "3.13",
            "-BackendSidecarHash",
            manifest_fingerprint,
            "-TauriAppBinaryHash",
            manifest_fingerprint,
            "-EmitDerivedCacheKeysOnly",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    derived = json.loads(result.stdout)
    assert derived["backendActionsKey"] == (
        "scriber-backend-sidecar-v2-Windows-python-3.13-" + hashfiles_fingerprint
    )
    assert derived["tauriActionsKey"] == "scriber-tauri-app-binary-v2-Windows-" + hashfiles_fingerprint
    assert derived["backendAssetName"] == (
        "scriber-backend-sidecar-Windows-python-3.13-" + hashfiles_fingerprint + ".zip"
    )


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
    assert "$relative.Contains(\"..\")" not in cold_product
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
    handoff_upload = release[
        release.index("Upload passive release-cache maintenance handoff") :
        release.index("Publish bounded finished component caches in parallel")
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


def test_backend_cache_keys_ignore_spec_checkout_line_endings() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    spec_path = REPO_ROOT / "packaging/scriber-backend.spec"
    original = spec_path.read_bytes()
    normalized = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
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
        spec_path.write_bytes(normalized.encode("utf-8"))
        write_keys(output_lf)
        spec_path.write_bytes(normalized.replace("\n", "\r\n").encode("utf-8"))
        write_keys(output_crlf)

        lf_manifests = {path.name: path.read_bytes() for path in output_lf.glob("*.txt")}
        crlf_manifests = {path.name: path.read_bytes() for path in output_crlf.glob("*.txt")}
        assert lf_manifests == crlf_manifests
        assert b"packaging/scriber-backend.spec" in lf_manifests["backend-runtime.txt"]
        assert lf_manifests["backend-runtime.txt"] == crlf_manifests["backend-runtime.txt"]
        assert lf_manifests["backend-sidecar.txt"] == crlf_manifests["backend-sidecar.txt"]
    finally:
        spec_path.write_bytes(original)
        shutil.rmtree(output_lf, ignore_errors=True)
        shutil.rmtree(output_crlf, ignore_errors=True)


def test_release_cache_keys_detect_utf8_text_by_content_and_preserve_nul_binary() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    gitkeep = REPO_ROOT / "src/assets/.gitkeep"
    node_version = REPO_ROOT / ".node-version"
    extensionless = REPO_ROOT / "Frontend/client/cache-key-text-fixture"
    nul_binary = REPO_ROOT / "Frontend/src-tauri/icons/cache-key-binary-fixture"
    original_gitkeep = gitkeep.read_bytes()
    original_node_version = node_version.read_bytes()
    output_lf = REPO_ROOT / "build" / f"test-cache-content-lf-{uuid.uuid4().hex}"
    output_crlf = REPO_ROOT / "build" / f"test-cache-content-crlf-{uuid.uuid4().hex}"
    output_binary_changed = REPO_ROOT / "build" / f"test-cache-content-binary-{uuid.uuid4().hex}"

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

    def manifests(output: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in output.glob("*.txt")}

    def manifest_hash(output: Path, manifest: str, relative_path: str) -> str:
        for line in (output / manifest).read_text(encoding="utf-8").splitlines():
            kind, path, digest = line.split("\t")
            if kind == "file" and path == relative_path:
                return digest
        raise AssertionError(f"missing {relative_path} in {manifest}")

    try:
        gitkeep.write_bytes(b"cache key fixture\n")
        node_version.write_bytes(b"26.3.1\n")
        extensionless.write_bytes("Grüße aus Scriber\n".encode("utf-8"))
        nul_binary.write_bytes(b"binary\x00payload\r\nend")
        write_keys(output_lf)

        gitkeep.write_bytes(b"cache key fixture\r\n")
        node_version.write_bytes(b"26.3.1\r\n")
        extensionless.write_bytes("Grüße aus Scriber\r\n".encode("utf-8"))
        write_keys(output_crlf)

        assert manifests(output_lf) == manifests(output_crlf)
        assert b"src/assets/.gitkeep" in manifests(output_lf)["backend-sidecar.txt"]
        assert b"Frontend/client/cache-key-text-fixture" in manifests(output_lf)["tauri-app-binary.txt"]

        binary_relative = "Frontend/src-tauri/icons/cache-key-binary-fixture"
        crlf_binary = nul_binary.read_bytes()
        assert manifest_hash(output_crlf, "tauri-app-binary.txt", binary_relative) == hashlib.sha256(
            crlf_binary
        ).hexdigest()

        nul_binary.write_bytes(crlf_binary.replace(b"\r\n", b"\n"))
        write_keys(output_binary_changed)
        assert manifest_hash(
            output_binary_changed, "tauri-app-binary.txt", binary_relative
        ) == hashlib.sha256(nul_binary.read_bytes()).hexdigest()
        assert manifest_hash(
            output_binary_changed, "tauri-app-binary.txt", binary_relative
        ) != manifest_hash(output_crlf, "tauri-app-binary.txt", binary_relative)
    finally:
        gitkeep.write_bytes(original_gitkeep)
        node_version.write_bytes(original_node_version)
        extensionless.unlink(missing_ok=True)
        nul_binary.unlink(missing_ok=True)
        shutil.rmtree(output_lf, ignore_errors=True)
        shutil.rmtree(output_crlf, ignore_errors=True)
        shutil.rmtree(output_binary_changed, ignore_errors=True)

    writer = _read("scripts/ci/write_release_cache_keys.ps1")
    assert "$textExtensions" not in writer
    assert "DecoderFallbackException" in writer
    assert "IndexOf($bytes, [byte]0)" in writer


def test_tauri_app_cache_key_is_commit_stable_and_binary_input_sensitive() -> None:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required for release-script validation")

    fixture_root = REPO_ROOT / "build" / f"test-tauri-key-{uuid.uuid4().hex}"
    workflow_path = REPO_ROOT / ".github/workflows/release-windows.yml"
    touched_paths = [
        REPO_ROOT / ".node-version",
        REPO_ROOT / "packaging/tauri-app-binary-output-contract.json",
        REPO_ROOT / "Frontend/client/src/lib/api-types.ts",
        REPO_ROOT / "Frontend/src-tauri/src/audio_frame_pipe.rs",
        REPO_ROOT / "Frontend/src-tauri/tauri.conf.json",
        REPO_ROOT / "scripts/build_windows.ps1",
        REPO_ROOT / "scripts/create_release_metadata.py",
        REPO_ROOT / "scripts/prepare_tauri_updater_config.py",
        REPO_ROOT / "scripts/sync_version.py",
        REPO_ROOT / "scripts/ci/write_release_cache_keys.ps1",
        REPO_ROOT / "scripts/ci/finalize_release_cache_keys.ps1",
        REPO_ROOT / "scripts/ci/prepare_tauri_app.ps1",
        REPO_ROOT / "scripts/ci/prepare_cold_tauri_product.ps1",
        REPO_ROOT / "scripts/ci/sync_tauri_app_binary_cache.ps1",
    ]
    original_bytes = {path: path.read_bytes() for path in [workflow_path, *touched_paths]}
    outputs: list[Path] = []

    def generate(
        name: str,
        *,
        commit: str = "a" * 40,
        updater_key: str = "test-updater-key-a",
        endpoint: str = "https://updates.example.invalid/a/latest.json",
        outlook_id: str = "11111111-1111-4111-8111-111111111111",
    ) -> dict[str, bytes]:
        output = fixture_root / name
        outputs.append(output)
        relative_output = str(output.relative_to(REPO_ROOT))
        subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(REPO_ROOT / "scripts/ci/write_release_cache_keys.ps1"),
                "-OutputDir",
                relative_output,
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(REPO_ROOT / "scripts/ci/finalize_release_cache_keys.ps1"),
                "-CacheKeyDir",
                relative_output,
                "-SourceCommit",
                commit,
                "-UpdaterPublicKey",
                updater_key,
                "-UpdaterEndpoint",
                endpoint,
                "-OutlookClientId",
                outlook_id,
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return {path.name: path.read_bytes() for path in output.glob("*.txt")}

    try:
        baseline = generate("baseline")
        commit_only = generate("commit-only", commit="b" * 40)
        assert baseline == commit_only
        tauri_manifest = baseline["tauri-app-binary.txt"]
        assert b"release-runtime\tsource-commit\t" not in tauri_manifest
        for relative_path in (
            b".node-version",
            b"packaging/tauri-app-binary-output-contract.json",
            b"Frontend/client/src/lib/api-types.ts",
            b"Frontend/src-tauri/src/audio_frame_pipe.rs",
            b"Frontend/src-tauri/tauri.conf.json",
            b"scripts/build_windows.ps1",
            b"scripts/create_release_metadata.py",
            b"scripts/prepare_tauri_updater_config.py",
            b"scripts/sync_version.py",
            b"scripts/ci/write_release_cache_keys.ps1",
            b"scripts/ci/finalize_release_cache_keys.ps1",
            b"scripts/ci/prepare_tauri_app.ps1",
            b"scripts/ci/prepare_cold_tauri_product.ps1",
            b"scripts/ci/sync_tauri_app_binary_cache.ps1",
        ):
            assert relative_path in tauri_manifest
        assert b".github/workflows/release-windows.yml" not in tauri_manifest

        runtime_variants = [
            generate("updater-key", updater_key="test-updater-key-b"),
            generate("updater-endpoint", endpoint="https://updates.example.invalid/b/latest.json"),
            generate("outlook", outlook_id="22222222-2222-4222-8222-222222222222"),
        ]
        assert all(item["tauri-app-binary.txt"] != tauri_manifest for item in runtime_variants)

        workflow_path.write_bytes(
            original_bytes[workflow_path] + b"\n# orchestration-only cache-key fixture\n"
        )
        workflow_only = generate("workflow-only")
        assert workflow_only["tauri-app-binary.txt"] == tauri_manifest
        workflow_path.write_bytes(original_bytes[workflow_path])

        for index, path in enumerate(touched_paths):
            if path.suffix == ".json":
                suffix = b"\n "
            elif path.suffix in {".ts", ".rs"}:
                suffix = b"\n// cache-key sensitivity fixture\n"
            else:
                suffix = b"\n# cache-key sensitivity fixture\n"
            path.write_bytes(original_bytes[path] + suffix)
            changed = generate(f"binary-input-{index}")
            assert changed["tauri-app-binary.txt"] != tauri_manifest
            path.write_bytes(original_bytes[path])
    finally:
        for path, content in original_bytes.items():
            path.write_bytes(content)
        shutil.rmtree(fixture_root, ignore_errors=True)

    finalizer = _read("scripts/ci/finalize_release_cache_keys.ps1")
    workflow = _read(".github/workflows/release-windows.yml")
    assert 'Add-DynamicRow -Path $tauriAppBinaryPath -Name "source-commit"' not in finalizer
    assert "scriber-tauri-app-binary-v2-" in workflow
    assert "SCRIBER_SAVE_REF_LOCAL_TAURI_CACHE" in workflow
    assert "github.ref != 'refs/heads/main'" in workflow
    assert 'schemaVersion = 2' in workflow
    assert 'runIdentity = [ordered]@{' in workflow
    assert 'runId = [int64]"${{ github.run_id }}"' in workflow
    assert 'headSha = "${{ github.sha }}"' in workflow
    assert 'cacheKeyParity = [ordered]@{' in workflow
    assert '$allFingerprintsMatch = $componentMatches.backendSidecar' in workflow
    assert 'Release cache summary parity disagrees with the parity gate output' in workflow
    assert '$tauriAppBinaryImportUsable = Normalize-CacheOutput' in workflow
    assert 'actions-cache-exact-validated' in workflow
    assert 'actions-cache-exact-rejected' in workflow
    assert 'importUsable = $tauriAppBinaryImportUsable -eq "true"' in workflow


def test_tauri_app_binary_cache_reuses_across_commits_and_rejects_tampering() -> None:
    pwsh = shutil.which("pwsh")
    gh = shutil.which("gh")
    if pwsh is None or gh is None:
        pytest.skip("PowerShell 7 and GitHub CLI are required for exact Tauri cache validation")

    version_output = subprocess.run(
        [gh, "--version"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    version_match = re.search(r"\b(\d+\.\d+\.\d+)\b", version_output)
    assert version_match is not None
    version = version_match.group(1)

    fixture_root = REPO_ROOT / "build" / f"test-tauri-product-{uuid.uuid4().hex}"
    cache_root = fixture_root / "cache"
    binary_path = fixture_root / "target/scriber-desktop.exe"
    binary_path.parent.mkdir(parents=True)
    shutil.copy2(gh, binary_path)
    cache_key = "a" * 64
    script = REPO_ROOT / "scripts/ci/sync_tauri_app_binary_cache.ps1"

    def invoke(mode: str, *, commit: str, expected_version: str = version) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        output_path = fixture_root / f"output-{uuid.uuid4().hex}.txt"
        env = os.environ.copy()
        env["GITHUB_SHA"] = commit
        env["GITHUB_OUTPUT"] = str(output_path)
        result = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-File",
                str(script),
                "-Mode",
                mode,
                "-CacheKey",
                cache_key,
                "-Version",
                expected_version,
                "-CacheRoot",
                str(cache_root.relative_to(REPO_ROOT)),
                "-BinaryPath",
                str(binary_path.relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        values: dict[str, str] = {}
        if output_path.exists():
            for line in output_path.read_text(encoding="utf-8-sig").splitlines():
                name, value = line.split("=", 1)
                values[name] = value
        return result, values

    try:
        exported, export_outputs = invoke("Export", commit="1" * 40)
        assert exported.returncode == 0, exported.stderr
        assert export_outputs["usable"] == "true"
        manifest_path = cache_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        cached_binary_path = cache_root / "scriber-desktop.exe"
        cached_binary = cached_binary_path.read_bytes()
        assert manifest["apiVersion"] == "2"
        assert manifest["sourceCommit"] == "1" * 40

        binary_path.unlink()
        imported, import_outputs = invoke("Import", commit="2" * 40)
        assert imported.returncode == 0, imported.stderr
        assert import_outputs["usable"] == "true"
        assert binary_path.exists()
        expected_sha256 = hashlib.sha256(Path(gh).read_bytes()).hexdigest()
        assert hashlib.sha256(binary_path.read_bytes()).hexdigest() == expected_sha256

        def assert_rejected(
            name: str,
            *,
            manifest_changes: dict[str, object] | None = None,
            executable_changes: dict[str, object] | None = None,
            commit: str = "2" * 40,
            expected_version: str = version,
            tamper_binary: bool = False,
        ) -> None:
            candidate = json.loads(json.dumps(manifest))
            candidate.update(manifest_changes or {})
            candidate["executable"].update(executable_changes or {})
            manifest_path.write_text(json.dumps(candidate), encoding="utf-8")
            cached_binary_path.write_bytes(cached_binary + (b"tamper" if tamper_binary else b""))
            binary_path.unlink(missing_ok=True)
            rejected, outputs = invoke(
                "Import", commit=commit, expected_version=expected_version
            )
            assert rejected.returncode == 0, f"{name}: {rejected.stderr}"
            assert outputs["usable"] == "false", name
            assert not binary_path.exists(), name

        assert_rejected("cache key", manifest_changes={"cacheKey": "f" * 64})
        assert_rejected("API version", manifest_changes={"apiVersion": "1"})
        assert_rejected("app version", manifest_changes={"appVersion": "999.999.999"})
        assert_rejected("binary version", manifest_changes={"binaryVersion": "999.999.999"})
        assert_rejected("target", manifest_changes={"target": "aarch64-pc-windows-msvc"})
        assert_rejected("profile", manifest_changes={"profile": "debug"})
        assert_rejected("source commit format", manifest_changes={"sourceCommit": "not-a-commit"})
        assert_rejected("missing source commit", manifest_changes={"sourceCommit": ""})
        assert_rejected("caller commit format", commit="not-a-commit")
        assert_rejected("length", executable_changes={"length": len(cached_binary) + 1})
        assert_rejected("SHA-256", executable_changes={"sha256": "0" * 64})
        assert_rejected("tampered executable", tamper_binary=True)
        assert_rejected("expected version", expected_version="999.999.999")
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


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
        "runtimeContract": {
            "name": "scriber-frozen-python-runtime",
            "revision": RUNTIME_CONTRACT_REVISION,
        },
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
        "runtimeContract": {
            "name": "scriber-frozen-python-runtime",
            "revision": RUNTIME_CONTRACT_REVISION,
        },
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
