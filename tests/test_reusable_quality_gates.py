from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality-gates.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
HYBRID_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "hybrid-pr-checks.yml"


def test_release_and_pr_use_the_same_reusable_quality_workflow() -> None:
    quality = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding="utf-8"))
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    hybrid = yaml.safe_load(HYBRID_WORKFLOW.read_text(encoding="utf-8"))

    assert release["jobs"]["quality-gates"]["uses"] == ("./.github/workflows/quality-gates.yml")
    assert hybrid["jobs"]["quality-gates"]["uses"] == ("./.github/workflows/quality-gates.yml")
    assert set(quality["jobs"]) == {
        "python-ruff",
        "python-full-suite",
        "frontend",
        "rust",
    }


def test_quality_workflow_runs_every_requested_exact_revision_gate() -> None:
    raw = QUALITY_WORKFLOW.read_text(encoding="utf-8")

    assert raw.count("ref: ${{ github.sha }}") == 3
    assert "python -m ruff check src tests scripts" in raw
    assert "python -m ruff format --check src tests scripts" in raw
    assert "uses: ./.github/workflows/python-full-suite.yml" in raw
    for command in ("npm run check", "npm run lint", "npm test", "npm run build"):
        assert command in raw
    assert "cargo fmt --all -- --check" in raw
    assert "cargo clippy --locked --all-targets -- -D warnings" in raw
    assert "cargo test --locked --all-targets" in raw


def test_rust_quality_cache_cannot_publish_from_a_pr_or_replace_tests() -> None:
    workflow = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding="utf-8"))
    rust = workflow["jobs"]["rust"]
    steps = {step["name"]: step for step in rust["steps"]}
    restore = steps["Restore Rust quality compilation cache"]
    save = steps["Save trusted Rust quality compilation cache"]

    assert "needs" not in rust  # Do not serialize Rust behind the frontend gate.
    assert restore["uses"] == "actions/cache/restore@v6"
    assert "if" not in restore
    assert save["uses"] == "actions/cache/save@v6"
    assert " ".join(save["if"].split()) == (
        "success() && github.repository == 'MyButtermilk/Scriber' && "
        "github.event_name == 'push' && github.ref == 'refs/heads/main' && "
        "steps.rust-quality-cache.outputs.cache-hit != 'true'"
    )
    assert save["with"]["key"] == "${{ steps.rust-quality-cache.outputs.cache-primary-key }}"
    names = list(steps)
    for name in ("Check Rust formatting", "Run Rust clippy", "Run Rust tests"):
        assert not {"if", "continue-on-error"} & steps[name].keys()
        assert names.index(restore["name"]) < names.index(name) < names.index(save["name"])
    for name in (
        "Check standalone audio formatting",
        "Build and stage the standalone audio worker",
        "Run standalone audio clippy",
        "Run standalone audio tests",
    ):
        assert not {"if", "continue-on-error"} & steps[name].keys()
        assert names.index(restore["name"]) < names.index(name) < names.index(save["name"])
    assert names.index("Build and stage the standalone audio worker") < names.index("Run Rust clippy")
    assert (
        "scripts/stage_audio_worker.mjs --profile debug" in steps["Build and stage the standalone audio worker"]["run"]
    )


def test_rust_quality_cache_binds_build_environment_and_excludes_release_or_credentials() -> None:
    workflow = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding="utf-8"))
    rust = workflow["jobs"]["rust"]
    steps = {step["name"]: step for step in rust["steps"]}
    environment = steps["Identify Rust quality compilation environment"]["run"]
    restore = steps["Restore Rust quality compilation cache"]["with"]
    save = steps["Save trusted Rust quality compilation cache"]["with"]

    assert rust["env"]["CARGO_INCREMENTAL"] == "1"
    for identity in (
        "rustc --version --verbose",
        "node --version",
        "$env:RUNNER_OS",
        "$env:RUNNER_ARCH",
        "$env:ImageOS",
        "$env:ImageVersion",
        "$env:CARGO_INCREMENTAL",
        "$env:RUSTFLAGS",
        "$env:CARGO_ENCODED_RUSTFLAGS",
        "$env:RUSTDOCFLAGS",
    ):
        assert identity in environment
    assert "IsNullOrWhiteSpace" in environment
    assert "throw" in environment
    key = restore["key"]
    assert "steps.rust-quality-environment.outputs.identity" in key
    for path in (
        ".node-version",
        "Frontend/src-tauri/Cargo.toml",
        "Frontend/src-tauri/Cargo.lock",
        "Frontend/src-tauri/build.rs",
        "Frontend/src-tauri/tauri.conf.json",
        "Frontend/src-tauri/capabilities/**",
        ".cargo/config",
        ".cargo/config.toml",
        "Frontend/src-tauri/.cargo/config",
        "Frontend/src-tauri/.cargo/config.toml",
        "native/scriber-audio-sidecar/Cargo.toml",
        "native/scriber-audio-sidecar/Cargo.lock",
        "native/scriber-audio-sidecar/build.rs",
        "native/scriber-audio-sidecar/windows-app-manifest.xml",
        "scripts/stage_audio_worker.mjs",
        ".github/workflows/quality-gates.yml",
    ):
        assert f"'{path}'" in key
    assert "github.sha" not in key
    assert "restore-keys" not in restore
    assert restore["path"] == save["path"]
    assert set(restore["path"].splitlines()) == {
        "~/.cargo/registry/index",
        "~/.cargo/registry/cache",
        "~/.cargo/registry/src",
        "~/.cargo/git/db",
        "Frontend/src-tauri/target/debug/.fingerprint",
        "Frontend/src-tauri/target/debug/build",
        "Frontend/src-tauri/target/debug/deps",
        "Frontend/src-tauri/target/debug/incremental",
    }
