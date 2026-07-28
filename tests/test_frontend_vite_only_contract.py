from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "Frontend"

REMOVED_SCAFFOLD_PATHS = (
    "drizzle.config.ts",
    "script/build.ts",
    "server/index.ts",
    "server/routes.ts",
    "server/static.ts",
    "server/storage.ts",
    "server/vite.ts",
    "shared/schema.ts",
)

REMOVED_DEPENDENCIES = {
    "@jridgewell/trace-mapping",
    "connect-pg-simple",
    "drizzle-orm",
    "drizzle-zod",
    "express",
    "express-session",
    "memorystore",
    "passport",
    "passport-local",
    "pg",
    "ws",
    "zod",
    "zod-validation-error",
}

REMOVED_DEV_DEPENDENCIES = {
    "@replit/vite-plugin-cartographer",
    "@replit/vite-plugin-dev-banner",
    "@replit/vite-plugin-runtime-error-modal",
    "@types/connect-pg-simple",
    "@types/express",
    "@types/express-session",
    "@types/passport",
    "@types/passport-local",
    "@types/ws",
    "drizzle-kit",
    "esbuild",
}


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_frontend_scaffold_paths_are_removed() -> None:
    assert all(not (FRONTEND_ROOT / path).exists() for path in REMOVED_SCAFFOLD_PATHS)


def test_frontend_package_is_vite_only() -> None:
    package = _read_json(FRONTEND_ROOT / "package.json")
    scripts = package["scripts"]
    dependencies = package["dependencies"]
    dev_dependencies = package["devDependencies"]

    assert package["name"] == "scriber"
    assert (REPO_ROOT / ".node-version").read_text(encoding="utf-8").strip() == "26.5.0"
    assert package["engines"] == {"node": ">=26.5.0 <27"}
    assert scripts["dev:client"] == "vite dev --port 5000"
    assert scripts["dev:tauri"] == (
        "cargo build --manifest-path src-tauri/Cargo.toml --bin scriber-audio-sidecar && vite dev --port 5000"
    )
    assert scripts["dev"] == "vite dev --port 5000"
    assert scripts["build"] == "npm run build:webview"
    assert scripts["build:webview"] == "vite build"
    assert scripts["check"] == "npm run check:app && npm run check:tests"
    assert scripts["lint"] == "eslint . --max-warnings 0"
    assert scripts["test"] == "npm run test:lib && npm run test:components"
    assert scripts["test:lib"] == 'tsx --test "client/src/**/*.test.ts"'
    assert scripts["test:components"] == "vitest run --config vitest.components.config.ts"
    assert scripts["tauri:dev"] == "tauri dev"
    assert scripts["tauri:build"] == "tauri build"
    assert scripts["tauri:bundle"] == "tauri bundle"
    assert {"start", "db:push"}.isdisjoint(scripts)

    assert len(dependencies) == 35
    assert len(dev_dependencies) == 25
    assert {
        "@eslint/js",
        "@testing-library/react",
        "eslint",
        "eslint-plugin-react-hooks",
        "jsdom",
        "typescript-eslint",
        "vitest",
    } <= set(dev_dependencies)
    assert REMOVED_DEPENDENCIES.isdisjoint(dependencies)
    assert REMOVED_DEV_DEPENDENCIES.isdisjoint(dev_dependencies)
    assert not package.get("optionalDependencies")


def test_frontend_lockfile_root_matches_clean_package_contract() -> None:
    package = _read_json(FRONTEND_ROOT / "package.json")
    lockfile = _read_json(FRONTEND_ROOT / "package-lock.json")
    root_package = lockfile["packages"][""]

    assert lockfile["name"] == "scriber"
    assert lockfile["lockfileVersion"] == 3
    assert root_package["name"] == "scriber"
    assert root_package["engines"] == {"node": ">=26.5.0 <27"}
    assert root_package["dependencies"] == package["dependencies"]
    assert root_package["devDependencies"] == package["devDependencies"]
    assert not root_package.get("optionalDependencies")


def test_frontend_typescript_and_vite_scope_only_the_webview() -> None:
    tsconfig = _read_json(FRONTEND_ROOT / "tsconfig.json")
    vite = (FRONTEND_ROOT / "vite.config.ts").read_text(encoding="utf-8")

    assert tsconfig["include"] == ["client/src/**/*"]
    assert tsconfig["compilerOptions"]["paths"] == {
        "@/*": ["./client/src/*"],
    }
    assert "server" not in json.dumps(tsconfig)
    assert "shared" not in json.dumps(tsconfig)
    assert "@shared" not in vite
    assert "REPL_ID" not in vite
    assert "@replit/vite-plugin-" not in vite
    assert "plugins: [react(), tailwindcss()]" in vite
    assert "metaImagesPlugin" not in vite
    assert 'readFileSync(path.resolve(import.meta.dirname, "package.json"), "utf-8")' in vite
    assert '"@assets": path.resolve(import.meta.dirname, "attached_assets")' in vite
    assert 'root: path.resolve(import.meta.dirname, "client")' in vite
    assert 'outDir: path.resolve(import.meta.dirname, "dist/public")' in vite


def test_tauri_webview_and_release_cache_contracts_remain_intact() -> None:
    tauri = _read_json(FRONTEND_ROOT / "src-tauri" / "tauri.conf.json")
    build = tauri["build"]
    cache_writer = (REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1").read_text(encoding="utf-8")

    assert build["beforeDevCommand"] == "npm run dev:tauri"
    assert build["beforeBuildCommand"] == "npm run build:webview"
    assert build["devUrl"] == "http://localhost:5000"
    assert build["frontendDist"] == "../dist/public"
    assert "build_tauri_backend_sidecar.ps1" in build["beforeBundleCommand"]
    assert 'Path "Frontend/package.json"' in cache_writer
    assert 'Path "Frontend/package-lock.json"' in cache_writer
    assert "Frontend/shared" not in cache_writer
