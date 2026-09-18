import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// Dev/quality gates compile the same standalone worker as release packaging.
// Stage Tauri's target-qualified externalBin without changing the installed name.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
if (args.length !== 2 || args[0] !== "--profile" || !["debug", "release"].includes(args[1])) {
  throw new Error("Usage: node scripts/stage_audio_worker.mjs --profile debug|release");
}
const compiler = spawnSync("rustc", ["--version", "--verbose"], { encoding: "utf8" });
if (compiler.status !== 0) throw new Error("Could not resolve the Rust host target");
const host = compiler.stdout.match(/^host: ([a-z0-9_-]+)$/m)?.[1];
if (!host) throw new Error("Rust did not report a valid host target");
const manifest = readFileSync(join(root, "native/scriber-audio-sidecar/Cargo.toml"), "utf8");
const packageBlock = manifest.match(/^\[package\]\s*\n([\s\S]*?)(?=^\[|$(?![\s\S]))/m)?.[1];
const workerVersion = packageBlock?.match(/^version\s*=\s*"(\d+\.\d+\.\d+)"\s*$/m)?.[1];
if (!workerVersion) throw new Error("The audio worker requires its own numeric package version");
const extension = process.platform === "win32" ? ".exe" : "";
const name = "scriber-audio-sidecar";
const source = join(root, "Frontend/src-tauri/target", args[1], `${name}${extension}`);
const probe = spawnSync(source, ["--self-test"], { encoding: "utf8" });
if (probe.status !== 0) throw new Error("The compiled audio worker self-test failed");
const result = JSON.parse(probe.stdout);
if (!result.ok || result.sidecar !== name || result.workerVersion !== workerVersion || result.protocolVersion !== "1") {
  throw new Error("The compiled audio worker identity or protocol does not match its source");
}
const destination = join(root, "Frontend/src-tauri/resources/audio-sidecar", `${name}-${host}${extension}`);
mkdirSync(dirname(destination), { recursive: true });
copyFileSync(source, destination);
