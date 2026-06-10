from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = REPO_ROOT / "Frontend" / "src-tauri"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_csp(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for raw_directive in csp.split(";"):
        parts = raw_directive.strip().split()
        if not parts:
            continue
        directives[parts[0]] = parts[1:]
    return directives


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


def test_tauri_csp_restricts_webview_to_local_backend_and_assets() -> None:
    config = read_json(TAURI_DIR / "tauri.conf.json")
    csp = config["app"]["security"]["csp"]

    assert isinstance(csp, str)
    assert csp.strip()
    assert " *" not in csp
    assert "'unsafe-eval'" not in csp
    assert "https:" not in csp
    assert "http://*" not in csp
    assert "ws://*" not in csp

    directives = parse_csp(csp)
    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert "'unsafe-inline'" in directives["style-src"]
    assert "data:" in directives["img-src"]
    assert "blob:" in directives["img-src"]
    assert "http://127.0.0.1:*" in directives["img-src"]
    assert "http://localhost:*" in directives["img-src"]
    assert "data:" in directives["font-src"]
    assert "data:" in directives["media-src"]
    assert "blob:" in directives["media-src"]
    assert "ipc:" in directives["connect-src"]
    assert "http://ipc.localhost" in directives["connect-src"]
    assert "http://127.0.0.1:*" in directives["connect-src"]
    assert "ws://127.0.0.1:*" in directives["connect-src"]
    assert "http://localhost:*" in directives["connect-src"]
    assert "ws://localhost:*" in directives["connect-src"]


def test_tauri_main_window_allows_html_file_drag_and_drop() -> None:
    config = read_json(TAURI_DIR / "tauri.conf.json")
    windows = config["app"]["windows"]

    assert windows
    assert windows[0]["dragDropEnabled"] is False


def test_frontend_entrypoint_is_compatible_with_tauri_csp() -> None:
    index_html = (REPO_ROOT / "Frontend" / "client" / "index.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "Frontend" / "client" / "src" / "index.css").read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in index_html
    assert "fonts.gstatic.com" not in index_html
    assert "<script type=\"module\" src=\"/src/main.tsx\"></script>" in index_html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index_html)
    assert "ui-sans-serif" in css
    assert "--font-sans:" in css
    assert "--font-heading:" in css


def test_tauri_does_not_expose_general_shell_or_opener_plugins() -> None:
    cargo = tomllib.loads((TAURI_DIR / "Cargo.toml").read_text(encoding="utf-8"))
    dependencies = cargo["dependencies"]
    lib_rs = (TAURI_DIR / "src" / "lib.rs").read_text(encoding="utf-8")

    assert "tauri-plugin-shell" not in dependencies
    assert "tauri-plugin-opener" not in dependencies
    assert "tauri_plugin_shell" not in lib_rs
    assert "tauri_plugin_opener" not in lib_rs


def test_rust_audio_sidecar_is_separate_cargo_binary_not_tauri_external_bin() -> None:
    cargo = tomllib.loads((TAURI_DIR / "Cargo.toml").read_text(encoding="utf-8"))
    bins = {item["name"]: item["path"] for item in cargo.get("bin", [])}
    config = read_json(TAURI_DIR / "tauri.conf.json")

    assert bins["scriber-audio-sidecar"] == "src/audio_sidecar.rs"
    assert (TAURI_DIR / "src" / "audio_sidecar.rs").is_file()
    assert "externalBin" not in config.get("bundle", {})


def test_tauri_window_menu_is_not_installed() -> None:
    lib_rs = (TAURI_DIR / "src" / "lib.rs").read_text(encoding="utf-8")

    assert "fn install_application_menu" not in lib_rs
    assert "install_application_menu(app)" not in lib_rs
    assert "app.set_menu" not in lib_rs
    assert "install_tray(app)?" in lib_rs


def test_tauri_bundle_only_carries_approved_resource_directories() -> None:
    config = read_json(TAURI_DIR / "tauri.conf.json")

    assert "externalBin" not in config.get("bundle", {})
    assert config["bundle"]["resources"] == {
        "target/release/backend/": "backend/",
        "resources/audio-sidecar/": "audio-sidecar/",
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
