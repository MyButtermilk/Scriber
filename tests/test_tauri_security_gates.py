from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = REPO_ROOT / "Frontend" / "src-tauri"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_tauri_default_capability_is_minimal() -> None:
    capability = read_json(TAURI_DIR / "capabilities" / "default.json")

    assert capability["windows"] == ["main"]
    assert capability["permissions"] == [
        "core:app:allow-version",
        "process:allow-restart",
        "updater:allow-check",
        "updater:allow-download-and-install",
    ]

    denied_permissions = {
        "*",
        "core:default",
        "opener:default",
        "process:default",
        "process:allow-exit",
        "shell:default",
        "shell:allow-execute",
        "shell:allow-spawn",
        "updater:default",
    }
    assert not denied_permissions.intersection(capability["permissions"])
    assert all(not permission.startswith("shell:") for permission in capability["permissions"])


def test_tauri_does_not_expose_general_shell_or_opener_plugins() -> None:
    cargo = tomllib.loads((TAURI_DIR / "Cargo.toml").read_text(encoding="utf-8"))
    dependencies = cargo["dependencies"]
    lib_rs = (TAURI_DIR / "src" / "lib.rs").read_text(encoding="utf-8")

    assert "tauri-plugin-shell" not in dependencies
    assert "tauri-plugin-opener" not in dependencies
    assert "tauri_plugin_shell" not in lib_rs
    assert "tauri_plugin_opener" not in lib_rs


def test_tauri_bundle_only_carries_backend_resource_directory() -> None:
    config = read_json(TAURI_DIR / "tauri.conf.json")

    assert "externalBin" not in config.get("bundle", {})
    assert config["bundle"]["resources"] == {
        "target/release/backend/": "backend/",
    }


def test_backend_supervisor_executable_allowlist_is_narrow() -> None:
    lib_rs = (TAURI_DIR / "src" / "lib.rs").read_text(encoding="utf-8")
    allowlist_function = re.search(
        r"fn\s+backend_executable_names\s*\(\)\s*->\s*&'static\s*\[&'static\s*str\]\s*\{(?P<body>.*?)\n\}",
        lib_rs,
        flags=re.DOTALL,
    )

    assert "SCRIBER_BACKEND_EXE" in lib_rs
    assert "is_allowed_backend_executable_name(&path)" in lib_rs
    assert re.search(r"fn\s+is_allowed_backend_executable_name\s*\(", lib_rs)
    assert allowlist_function
    allowlist_body = allowlist_function.group("body")
    assert '"scriber-backend.exe"' in lib_rs
    assert '"scriber-backend-x86_64-pc-windows-msvc.exe"' in lib_rs
    assert '"scriber-backend"' in lib_rs
    assert '"python.exe"' not in allowlist_body
    assert '"cmd.exe"' not in allowlist_body
    assert '"powershell.exe"' not in allowlist_body
