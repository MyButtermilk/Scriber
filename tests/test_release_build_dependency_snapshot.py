from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.ci.release_build_dependency_snapshot import (
    MANIFEST,
    _identity,
    export_snapshot,
    import_snapshot,
)

IDENTITY = _identity("MyButtermilk/Scriber", 123, 1, "a" * 40, "refs/tags/v0.5.115")
KEYS = {
    "rust": "scriber-rust-dependencies-v1-Windows-" + "b" * 64,
    "frontend": "scriber-frontend-node-modules-Windows-node-" + "c" * 64 + "-" + "d" * 64,
}
DEPENDENCY = "Frontend/src-tauri/target/release/deps/libserde-123.rlib"


def write(root: Path, name: str, value: bytes = b"dependency") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


@pytest.fixture
def snapshot(tmp_path: Path) -> tuple[Path, Path, dict]:
    source = tmp_path / "source"
    write(source, DEPENDENCY)
    write(source, ".cargo/registry/cache/index/test.crate")
    write(source, "Frontend/src-tauri/target/release/.fingerprint/serde-123/lib-serde")
    write(source, "Frontend/src-tauri/target/release/build/serde-123/output")
    write(source, "Frontend/src-tauri/target/release/incremental/serde-123/result.bin")
    write(source, "Frontend/node_modules/.package-lock.json", b"{}")
    write(source, "Frontend/node_modules/@test/pkg/index.js", b"module.exports={};")
    for path in (
        "Frontend/src-tauri/target/release/deps/scriber_desktop_lib-123.rlib",
        "Frontend/src-tauri/target/release/deps/scriber_audio_sidecar-123.exe",
        "Frontend/src-tauri/target/release/.fingerprint/scriber-desktop-123/lib-scriber-desktop",
        "Frontend/src-tauri/target/release/build/scriber-desktop-123/output",
        "Frontend/src-tauri/target/release/incremental/scriber_desktop-123/object.bin",
        "Frontend/src-tauri/target/release/scriber-desktop.exe",
        ".cargo/credentials.toml",
    ):
        write(source, path, b"must not export")
    output = tmp_path / "snapshot"
    manifest = export_snapshot(repo_root=source, output=output, identity=IDENTITY, keys=KEYS)
    return source, output, manifest


def test_roundtrip_retains_dependencies_and_mtimes_without_shipping_app_or_credentials(
    snapshot, tmp_path: Path
) -> None:
    source, output, _manifest = snapshot
    target = tmp_path / "target"
    target.mkdir()
    result = import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert set(result["components"]) == {"rust", "frontend"}
    assert (target / DEPENDENCY).read_bytes() == (source / DEPENDENCY).read_bytes()
    assert (target / DEPENDENCY).stat().st_mtime == pytest.approx((source / DEPENDENCY).stat().st_mtime, abs=0.001)
    assert (target / "Frontend/node_modules/.package-lock.json").read_bytes() == b"{}"
    assert not any("scriber_desktop" in path.name or "scriber-desktop" in path.name for path in target.rglob("*"))
    assert not (target / ".cargo/credentials.toml").exists()


def test_only_missed_components_are_exported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write(source, "Frontend/node_modules/.package-lock.json", b"{}")
    result = export_snapshot(
        repo_root=source, output=tmp_path / "snapshot", identity=IDENTITY, keys={"frontend": KEYS["frontend"]}
    )
    assert set(result["components"]) == {"frontend"}
    assert not (tmp_path / "snapshot/rust-dependencies.tar.zst").exists()


def test_no_full_tree_copy_or_source_mutation(snapshot) -> None:
    source, output, _manifest = snapshot
    assert (source / ".cargo/credentials.toml").read_bytes() == b"must not export"
    assert (source / "Frontend/src-tauri/target/release/scriber-desktop.exe").exists()
    assert {path.name for path in output.iterdir()} == {
        MANIFEST,
        "rust-dependencies.tar.zst",
        "frontend-dependencies.tar.zst",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "Other/Repo"),
        ("sourceRunId", 999),
        ("sourceRunAttempt", 0),
        ("sourceSha", "b" * 40),
        ("sourceRef", "refs/tags/v0.5.114"),
    ],
)
def test_wrong_source_binding_fails_without_import(snapshot, tmp_path: Path, field: str, value) -> None:
    _source, output, _manifest = snapshot
    target = tmp_path / "target"
    target.mkdir()
    identity = {**IDENTITY, field: value}
    with pytest.raises(ValueError, match="snapshot (?:source identity|attempt)"):
        import_snapshot(repo_root=target, snapshot=output, identity=identity, expected_keys=KEYS)
    assert not list(target.iterdir())


def test_rerun_may_retain_successful_same_source_producer_snapshot(snapshot, tmp_path: Path) -> None:
    _source, output, _manifest = snapshot
    target = tmp_path / "target"
    target.mkdir()
    import_snapshot(repo_root=target, snapshot=output, identity={**IDENTITY, "sourceRunAttempt": 2}, expected_keys=KEYS)
    assert (target / DEPENDENCY).exists()


def test_dependency_key_mismatch_fails_before_any_import(snapshot, tmp_path: Path) -> None:
    _source, output, _manifest = snapshot
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="cache identity"):
        import_snapshot(
            repo_root=target,
            snapshot=output,
            identity=IDENTITY,
            expected_keys={**KEYS, "rust": "scriber-rust-dependencies-v1-Windows-" + "f" * 64},
        )
    assert not list(target.iterdir())


def test_corrupt_archive_is_rejected_before_unpacking_any_component(snapshot, tmp_path: Path) -> None:
    _source, output, _manifest = snapshot
    with (output / "frontend-dependencies.tar.zst").open("ab") as stream:
        stream.write(b"corrupt")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="checksum"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert not list(target.iterdir())


def rewrite_rust_archive(output: Path, manifest: dict, members: list[tarfile.TarInfo]) -> None:
    archive = output / "rust-dependencies.tar.zst"
    with tarfile.open(archive, "w:zst", level=1) as stream:
        for member in members:
            stream.addfile(member, io.BytesIO(b"x" * member.size) if member.isfile() else None)
    updated = deepcopy(manifest)
    updated["components"]["rust"].update(
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        length=archive.stat().st_size,
        fileCount=len(members),
        unpackedBytes=sum(member.size for member in members),
    )
    (output / MANIFEST).write_text(json.dumps(updated), encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "../../outside",
        "/absolute",
        "C:/absolute",
        "Frontend/src-tauri/target/release/deps/../../outside",
        "Frontend/src-tauri/target/release/deps/a:stream",
        "Frontend/src-tauri/target/release/deps/NUL.txt",
        "Frontend/src-tauri/target/release/deps/end.",
        "Frontend/src-tauri/target/release/deps\\backslash",
        "Frontend/src-tauri/target/release/deps/scriber_desktop-123.exe",
        "Frontend/src-tauri/target/release/deps/SCRIBER_DESKTOP-123.exe",
        "src/config.py",
        ".cargo/credentials.toml",
    ],
)
def test_rejects_unsafe_or_non_dependency_archive_paths(snapshot, tmp_path: Path, name: str) -> None:
    _source, output, manifest = snapshot
    member = tarfile.TarInfo(name)
    member.size = 1
    rewrite_rust_archive(output, manifest, [member])
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="Unsafe"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert not list(target.iterdir())


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE])
def test_rejects_links_and_special_files(snapshot, tmp_path: Path, kind: bytes) -> None:
    _source, output, manifest = snapshot
    member = tarfile.TarInfo(DEPENDENCY)
    member.type = kind
    member.linkname = "../../outside"
    rewrite_rust_archive(output, manifest, [member])
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="Unsafe"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert not list(target.iterdir())


def test_duplicate_windows_case_insensitive_paths_are_rejected(snapshot, tmp_path: Path) -> None:
    _source, output, manifest = snapshot
    members = [tarfile.TarInfo(DEPENDENCY), tarfile.TarInfo(DEPENDENCY.replace("libserde", "LIBSERDE"))]
    for member in members:
        member.size = 1
    rewrite_rust_archive(output, manifest, members)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="Unsafe"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert not list(target.iterdir())


def test_existing_destination_is_never_overwritten(snapshot, tmp_path: Path) -> None:
    _source, output, _manifest = snapshot
    target = tmp_path / "target"
    existing = write(target, DEPENDENCY, b"keep")
    with pytest.raises(ValueError, match="already exists"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert existing.read_bytes() == b"keep"
    assert not (target / "Frontend/node_modules").exists()


def test_export_refuses_existing_snapshot_directory(snapshot) -> None:
    source, output, _manifest = snapshot
    with pytest.raises(FileExistsError):
        export_snapshot(repo_root=source, output=output, identity=IDENTITY, keys=KEYS)


def test_export_rejects_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    real = write(source, DEPENDENCY)
    link = real.with_name("linked.rlib")
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    with pytest.raises(ValueError, match="links or reparse"):
        export_snapshot(repo_root=source, output=tmp_path / "snapshot", identity=IDENTITY, keys={"rust": KEYS["rust"]})


def test_snapshot_inventory_mismatch_never_commits_the_other_component(snapshot, tmp_path: Path) -> None:
    _source, output, manifest = snapshot
    manifest["components"]["frontend"]["fileCount"] += 1
    (output / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="inventory does not match"):
        import_snapshot(repo_root=target, snapshot=output, identity=IDENTITY, expected_keys=KEYS)
    assert not list(target.iterdir())


def test_maintenance_publishes_only_source_bound_passive_dependencies_on_default_ref() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release-cache-maintenance.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["publish-build-dependencies"]
    assert "needs" not in job
    assert "github.event.workflow_run.conclusion == 'success'" in job["if"]
    steps = job["steps"]
    guard = steps[0]
    assert "source.head_repository.full_name" in guard["run"]
    assert "source.path -ne '.github/workflows/release-windows.yml'" in guard["run"]
    assert "source.head_sha -ne $env:SOURCE_SHA" in guard["run"]
    assert "source.conclusion -ne 'success'" in guard["run"]
    assert "commits/refs/tags/$($env:SOURCE_TAG)" in guard["run"]
    assert "tagCommit.sha -ne $env:SOURCE_SHA" in guard["run"]
    assert "commits/main" in guard["run"]
    assert "compare/$($env:SOURCE_SHA)...$($mainCommit.sha)" in guard["run"]
    assert "comparison.status -notin @('ahead', 'identical')" in guard["run"]
    assert "comparison.base_commit.sha -ne $env:SOURCE_SHA" in guard["run"]
    assert "comparison.merge_base_commit.sha -ne $env:SOURCE_SHA" in guard["run"]
    assert "releases/tags/$($env:SOURCE_TAG)" in guard["run"]
    assert "release.tag_name -ne $env:SOURCE_TAG" in guard["run"]
    assert "release.draft -ne $false" in guard["run"]
    assert "release.prerelease -ne $false" in guard["run"]
    assert "release.published_at" in guard["run"]
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v7")
    assert steps.index(guard) < steps.index(checkout)
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    download = next(step for step in steps if step.get("uses") == "actions/download-artifact@v8")
    assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
    reader = next(step for step in steps if step.get("id") == "dependency-import")
    assert "release_build_dependency_snapshot.py import" in reader["run"]
    assert "--source-run-attempt" in reader["run"]
    caches = [step for step in steps if step.get("uses") == "actions/cache/save@v6"]
    assert len(caches) == 2
    for step in caches:
        assert steps.index(step) > steps.index(reader)
        assert "steps.dependency-import.outputs." in step["if"]
        assert "github.event.workflow_run.head_sha" not in step["with"]["key"]
        assert "src/version.py" not in step["with"]["key"]
        assert "release-version" not in step["with"]["key"]
