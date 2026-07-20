from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_quickjs_youtube_runtime as helper


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "packaging" / "quickjs-youtube-runtime-lock-v1.json"


def test_quickjs_wrapper_lock_binds_sources_engine_and_canonical_manifest() -> None:
    lock = helper._load_lock(LOCK_PATH, REPO_ROOT)

    assert lock["contract"] == helper.LOCK_CONTRACT
    assert lock["engine"]["installedFileName"] == "qjs-engine.exe"
    assert lock["license"]["installedFileName"] == "LICENSE.quickjs-ng.txt"
    assert lock["wrapper"]["output"]["installedFileName"] == "qjs.exe"
    assert lock["wrapper"]["artifact"]["length"] == lock["wrapper"]["output"]["length"]
    assert lock["wrapper"]["artifact"]["sha256"] == lock["wrapper"]["output"]["sha256"]
    assert "release-cache-quickjs-wrapper-v3" in lock["wrapper"]["artifact"]["url"]
    source_tree = (
        json.dumps(
            lock["wrapper"]["files"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    assert hashlib.sha256(source_tree).hexdigest() == lock["wrapper"]["artifact"][
        "sourceTreeSha256"
    ]
    assert lock["manifest"]["runtime"]["implementation"] == helper.IMPLEMENTATION
    assert lock["manifest"]["runtime"]["protocol"] == helper.PROTOCOL
    assert lock["manifest"]["runtime"]["wrapperVersion"] == "3"
    assert (
        lock["manifest"]["policy"][
            "processStateFailureCleanupBeforeReaderJoin"
        ]
        is True
    )
    assert hashlib.sha256(
        helper._canonical_manifest_bytes(lock["manifest"])
    ).hexdigest() == lock["manifestCanonicalSha256"]


def test_quickjs_upstream_lock_binding_normalizes_mixed_line_endings(
    tmp_path: Path,
) -> None:
    lock = helper._load_json_object(
        LOCK_PATH, label="QuickJS wrapper runtime lock"
    )
    source = helper._normalized_text_bytes(
        REPO_ROOT / lock["upstreamLock"]["relativePath"]
    )
    lines = source.splitlines(keepends=True)
    mixed = b"".join(
        line[:-1] + (b"\r\n" if index % 2 else b"\n")
        if line.endswith(b"\n")
        else line
        for index, line in enumerate(lines)
    )
    upstream_path = tmp_path / lock["upstreamLock"]["relativePath"]
    upstream_path.parent.mkdir(parents=True)
    upstream_path.write_bytes(mixed)

    entry = helper._validate_upstream_lock(tmp_path, lock)

    assert entry["id"] == lock["upstreamLock"]["entry"]
    upstream_path.write_bytes(mixed.replace(b"quickjs-ng", b"quickjs-nh", 1))
    with pytest.raises(
        helper.BuildError, match="upstream QuickJS lock differs from its wrapper binding"
    ):
        helper._validate_upstream_lock(tmp_path, lock)

    upstream_path.write_bytes(source.replace(b"\n", b"\r", 1))
    with pytest.raises(helper.BuildError, match="lone carriage return"):
        helper._validate_upstream_lock(tmp_path, lock)


def test_quickjs_input_cache_reuses_verified_bytes_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = b"locked quickjs input"
    identity = {
        "url": "https://example.test/locked-input",
        "fileName": "locked-input.bin",
        "length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    downloads: list[str] = []

    def fake_download(*, url: str, destination: Path, **_kwargs: object) -> None:
        downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    monkeypatch.setattr(helper, "_download_locked", fake_download)
    cache_root = tmp_path / "cache"

    first = helper._provision_input(
        cache_root=cache_root, identity=identity, offline=False
    )
    second = helper._provision_input(
        cache_root=cache_root, identity=identity, offline=True
    )

    assert first == second
    assert first.read_bytes() == content
    assert downloads == [identity["url"]]


def test_quickjs_offline_cache_rejects_tampered_bytes(tmp_path: Path) -> None:
    content = b"locked quickjs input"
    identity = {
        "url": "https://example.test/locked-input",
        "fileName": "locked-input.bin",
        "length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / identity["fileName"]).write_bytes(b"tampered")

    with pytest.raises(helper.BuildError, match="offline QuickJS build cache"):
        helper._provision_input(cache_root=cache_root, identity=identity, offline=True)


def test_quickjs_cargo_target_is_short_stable_and_wrapper_bound(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    lock = helper._load_lock(LOCK_PATH, REPO_ROOT)
    long_work_dir = tmp_path.joinpath(*(["long-candidate-work-root"] * 12))

    target = helper._cargo_target_dir(repo_root, lock)

    assert target == helper._cargo_target_dir(repo_root, lock)
    assert target.relative_to(repo_root.resolve()).parts[:2] == ("build", "qjs-target")
    assert target.name == lock["wrapper"]["output"]["sha256"][:16]
    assert len(str(target)) < len(str(long_work_dir / "cargo-target"))


def test_normal_quickjs_build_provisions_locked_wrapper_instead_of_linking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock = helper._load_lock(LOCK_PATH, REPO_ROOT)
    output_root = tmp_path / "runtime"
    work_root = tmp_path / "work"
    wrapper = tmp_path / "downloaded-wrapper.exe"
    engine = tmp_path / "engine.exe"
    license_path = tmp_path / "LICENSE"
    wrapper.write_bytes(b"wrapper")
    engine.write_bytes(b"engine")
    license_path.write_bytes(b"license")
    provisioned: list[dict[str, object]] = []

    def fake_provision(*, identity: dict[str, object], **_kwargs: object) -> Path:
        provisioned.append(identity)
        if identity is lock["wrapper"]["artifact"]:
            return wrapper
        if identity is lock["engine"]["source"]:
            return engine
        if identity is lock["license"]:
            return license_path
        raise AssertionError("unexpected QuickJS input")

    monkeypatch.setattr(helper, "_load_lock", lambda *_args: lock)
    monkeypatch.setattr(helper, "_provision_input", fake_provision)
    monkeypatch.setattr(helper, "_harden_engine", lambda **_kwargs: engine)
    monkeypatch.setattr(
        helper,
        "_build_wrapper",
        lambda **_kwargs: pytest.fail("normal build linked the wrapper"),
    )
    monkeypatch.setattr(helper, "_copy_exact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "_verify_runtime", lambda **_kwargs: None)

    result = helper.build_or_verify(
        SimpleNamespace(
            repo_root=REPO_ROOT,
            lock=LOCK_PATH,
            output=output_root / "qjs.exe",
            engine_output=output_root / "qjs-engine.exe",
            license_output=output_root / "LICENSE.quickjs-ng.txt",
            manifest=output_root / "js-runtime-manifest.json",
            work_dir=work_root,
            rustup=None,
            quickjs_wrapper=None,
            quickjs_engine=None,
            quickjs_license=None,
            rebuild_wrapper=False,
            offline=False,
            verify_only=False,
        )
    )

    assert result["ok"] is True
    assert provisioned[-1] is lock["wrapper"]["artifact"]


def test_verify_only_never_builds_or_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock = helper._load_lock(LOCK_PATH, REPO_ROOT)
    output_root = tmp_path / "runtime"
    outputs = [
        output_root / "qjs.exe",
        output_root / "qjs-engine.exe",
        output_root / "LICENSE.quickjs-ng.txt",
        output_root / "js-runtime-manifest.json",
    ]
    verified: list[tuple[Path, Path, Path, Path]] = []
    monkeypatch.setattr(helper, "_load_lock", lambda *_args: lock)
    monkeypatch.setattr(
        helper,
        "_build_wrapper",
        lambda **_kwargs: pytest.fail("verify-only built the wrapper"),
    )
    monkeypatch.setattr(
        helper,
        "_provision_input",
        lambda **_kwargs: pytest.fail("verify-only provisioned an input"),
    )
    monkeypatch.setattr(
        helper,
        "_verify_runtime",
        lambda *, wrapper, engine, license_path, manifest_path, lock: verified.append(
            (wrapper, engine, license_path, manifest_path)
        ),
    )

    result = helper.build_or_verify(
        SimpleNamespace(
            repo_root=REPO_ROOT,
            lock=LOCK_PATH,
            output=outputs[0],
            engine_output=outputs[1],
            license_output=outputs[2],
            manifest=outputs[3],
            work_dir=None,
            rustup=None,
            quickjs_engine=None,
            quickjs_license=None,
            offline=False,
            verify_only=True,
        )
    )

    assert result["ok"] is True
    assert result["mode"] == "verify"
    assert verified == [tuple(path.resolve(strict=False) for path in outputs)]


def test_sidecar_and_release_cache_keys_track_every_quickjs_build_input() -> None:
    sidecar = (REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1").read_text(
        encoding="utf-8"
    )
    release_keys = (
        REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1"
    ).read_text(encoding="utf-8")
    required = (
        "packaging/quickjs-youtube-runtime-lock-v1.json",
        "scripts/build_quickjs_youtube_runtime.py",
        "scripts/perf/profiles/installer-size/quickjs-runtime-lock-v1.json",
        "native/scriber-quickjs-wrapper/Cargo.toml",
        "native/scriber-quickjs-wrapper/Cargo.lock",
        "native/scriber-quickjs-wrapper/src/lib.rs",
        "native/scriber-quickjs-wrapper/src/main.rs",
    )

    normalized_sidecar = sidecar.replace("\\", "/")
    for relative in required:
        assert f'"{relative}"' in normalized_sidecar
        assert f'"{relative}"' in release_keys
    assert "Invoke-QuickJsYoutubeRuntimeBuild" in sidecar
    assert "Invoke-DenortYoutubeRuntimeBuild" not in sidecar


def test_ci_downloads_and_hashes_quickjs_wrapper_before_release_builds() -> None:
    verifier = (
        REPO_ROOT / "scripts" / "ci" / "verify_quickjs_wrapper_cache_asset.ps1"
    ).read_text(encoding="utf-8")
    release_workflow = (
        REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
    ).read_text(encoding="utf-8")
    pr_workflow = (
        REPO_ROOT / ".github" / "workflows" / "hybrid-pr-checks.yml"
    ).read_text(encoding="utf-8")
    publisher = (
        REPO_ROOT / "scripts" / "ci" / "publish_quickjs_wrapper_cache_asset.ps1"
    ).read_text(encoding="utf-8")
    pruner = (
        REPO_ROOT / "scripts" / "ci" / "prune_obsolete_release_caches.ps1"
    ).read_text(encoding="utf-8")

    assert "gh release download" in verifier
    assert "Get-FileHash" in verifier
    assert "release-cache-quickjs-wrapper-v3" in verifier
    assert "verify_quickjs_wrapper_cache_asset.ps1" in release_workflow
    assert "verify_quickjs_wrapper_cache_asset.ps1" in pr_workflow
    assert "tests\\test_build_quickjs_youtube_runtime.py" in pr_workflow
    assert "Get-FileHash" in publisher
    assert "gh release upload" in publisher
    assert "verify_quickjs_wrapper_cache_asset.ps1" in publisher
    assert "release-cache-quickjs-wrapper-v3" in pruner
    assert "release-cache-quickjs-wrapper-v\\d+" in pruner
