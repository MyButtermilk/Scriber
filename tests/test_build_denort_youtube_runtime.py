from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "scripts" / "build_denort_youtube_runtime.py"
LOCK_PATH = (
    REPO_ROOT
    / "scripts"
    / "perf"
    / "profiles"
    / "installer-size"
    / "denort-runtime-lock-v1.json"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location("build_denort_youtube_runtime", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_denort_lock_manifest_and_crlf_wrapper_are_canonical() -> None:
    helper = _load_helper()
    entry = helper._load_lock(LOCK_PATH)
    wrapper = REPO_ROOT / entry["wrapper"]["relativePath"]
    normalized = helper._normalized_wrapper_bytes(wrapper)

    assert len(normalized) == entry["wrapper"]["length"] == 5_002
    assert hashlib.sha256(normalized).hexdigest() == entry["wrapper"]["sha256"]
    manifest_bytes = helper._canonical_manifest_bytes(entry["manifest"])
    assert manifest_bytes.endswith(b"\n")
    assert hashlib.sha256(manifest_bytes).hexdigest() == entry["manifestCanonicalSha256"]


def test_denort_wrapper_normalization_rejects_lone_carriage_return(tmp_path: Path) -> None:
    helper = _load_helper()
    source = tmp_path / "wrapper.ts"
    source.write_bytes(b"first\r\nsecond\rthird\n")

    with pytest.raises(helper.BuildError, match="lone carriage return"):
        helper._normalized_wrapper_bytes(source)


def test_denort_lock_rejects_noncanonical_manifest_hash(tmp_path: Path) -> None:
    helper = _load_helper()
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    payload["entry"]["manifest"]["policy"]["firstRunDownloads"] = True
    tampered = tmp_path / "lock.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(helper.BuildError, match="canonical hash"):
        helper._load_lock(tampered)


def test_sidecar_builder_stages_only_locked_quickjs_wrapper_runtime() -> None:
    source = (REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1").read_text(
        encoding="utf-8"
    )
    initializer = source.split(
        "function Initialize-BackendRuntimeStableMediaTools", 1
    )[1].split("function Test-BackendMediaFiles", 1)[0]
    copier = source.split("function Copy-MediaTools", 1)[1].split(
        "$RepoRoot = (Resolve-Path $RepoRoot).Path", 1
    )[0]

    assert "Invoke-QuickJsYoutubeRuntimeBuild" in initializer
    assert 'Join-Path $stableRoot "qjs.exe"' in initializer
    assert 'Join-Path $stableRoot "qjs-engine.exe"' in initializer
    assert 'Join-Path $stableRoot "LICENSE.quickjs-ng.txt"' in initializer
    assert 'Join-Path $stableRoot "js-runtime-manifest.json"' in initializer
    assert 'Resolve-BackendStableMediaTool -Names @("qjs.exe")' in copier
    assert 'Resolve-BackendStableMediaTool -Names @("qjs-engine.exe")' in copier
    assert 'Resolve-BackendStableMediaTool -Names @("js-runtime-manifest.json")' in copier
    assert "-VerifyOnly" in copier
    assert '"packaging\\quickjs-youtube-runtime-lock-v1.json"' in source
    assert '"scripts\\build_quickjs_youtube_runtime.py"' in source
    assert '"native\\scriber-quickjs-wrapper\\src\\lib.rs"' in source


def _fake_denort_entry(archive: Path, executable_bytes: bytes) -> dict:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("release/denort.exe", executable_bytes)
    archive_bytes = archive.read_bytes()
    return {
        "denortAsset": {
            "url": archive.as_uri(),
            "fileName": "denort-test.zip",
            "length": len(archive_bytes),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "executableLength": len(executable_bytes),
            "executableSha256": hashlib.sha256(executable_bytes).hexdigest(),
        }
    }


def test_cold_build_provisions_locked_denort_and_reuses_offline_cache(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    source_archive = tmp_path / "source-denort.zip"
    executable_bytes = b"locked-denort-executable"
    entry = _fake_denort_entry(source_archive, executable_bytes)
    work_dir = tmp_path / "repo-work"

    first = helper._provision_denort(work_dir, entry)

    assert first.read_bytes() == executable_bytes
    cached_archive = work_dir / "denort-input-cache" / "denort-test.zip"
    assert cached_archive.is_file()
    assert hashlib.sha256(cached_archive.read_bytes()).hexdigest() == (
        entry["denortAsset"]["sha256"]
    )

    source_archive.unlink()
    first.unlink()
    second = helper._provision_denort(work_dir, entry)

    assert second == first
    assert second.read_bytes() == executable_bytes


def test_denort_override_must_match_the_locked_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _load_helper()
    source_archive = tmp_path / "source-denort.zip"
    executable_bytes = b"locked-denort-executable"
    entry = _fake_denort_entry(source_archive, executable_bytes)
    override = tmp_path / "denort.exe"
    override.write_bytes(executable_bytes)
    monkeypatch.setenv("DENORT_BIN", str(override))
    args = SimpleNamespace(denort=None)

    assert helper._resolve_denort_for_build(args, tmp_path / "work", entry) == override

    override.write_bytes(b"different")
    with pytest.raises(helper.BuildError, match="protected provenance lock"):
        helper._resolve_denort_for_build(args, tmp_path / "work", entry)


def test_verify_only_needs_neither_compiler_nor_raw_denort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _load_helper()
    output = tmp_path / "deno.exe"
    manifest = tmp_path / "js-runtime-manifest.json"
    output.write_bytes(b"cached-runtime")
    manifest.write_bytes(b"cached-manifest")
    verified: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        helper,
        "_verify_output",
        lambda runtime, runtime_manifest, _entry: verified.append(
            (runtime, runtime_manifest)
        ),
    )
    monkeypatch.setattr(
        helper,
        "_verify_compiler",
        lambda *_args: pytest.fail("verify-only resolved the compiler"),
    )
    monkeypatch.setattr(
        helper,
        "_resolve_denort_for_build",
        lambda *_args: pytest.fail("verify-only resolved raw denort"),
    )

    result = helper.build_or_verify(
        SimpleNamespace(
            repo_root=REPO_ROOT,
            lock=LOCK_PATH,
            compiler=None,
            denort=None,
            output=output,
            manifest=manifest,
            work_dir=None,
            verify_only=True,
        )
    )

    assert result["ok"] is True
    assert result["mode"] == "verify"
    assert verified == [(output, manifest)]


def test_release_runtime_key_tracks_every_quickjs_wrapper_build_input() -> None:
    source = (REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1").read_text(
        encoding="utf-8"
    )
    runtime_key = source.split("$backendRuntimeEntries = New-EntryList", 1)[1].split(
        'Write-KeyFile -Name "backend-runtime.txt"', 1
    )[0]

    assert '"packaging/quickjs-youtube-runtime-lock-v1.json"' in runtime_key
    assert '"scripts/build_quickjs_youtube_runtime.py"' in runtime_key
    assert '"scripts/perf/profiles/installer-size/quickjs-runtime-lock-v1.json"' in runtime_key
    assert '"native/scriber-quickjs-wrapper/Cargo.lock"' in runtime_key
    assert '"native/scriber-quickjs-wrapper/src/lib.rs"' in runtime_key


def test_verify_only_power_shell_path_does_not_require_quickjs_overrides() -> None:
    source = (REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1").read_text(
        encoding="utf-8"
    )
    invocation = source.split("function Invoke-QuickJsYoutubeRuntimeBuild", 1)[1].split(
        "function Test-MediaToolExecutable", 1
    )[0]

    assert invocation.index("if ($VerifyOnly)") < invocation.index(
        "if ($env:SCRIBER_QUICKJS_ENGINE_BIN)"
    )
    assert '"--verify-only"' in invocation
    assert '"--quickjs-engine", $env:SCRIBER_QUICKJS_ENGINE_BIN' in invocation
    assert '"--quickjs-license", $env:SCRIBER_QUICKJS_LICENSE_FILE' in invocation
