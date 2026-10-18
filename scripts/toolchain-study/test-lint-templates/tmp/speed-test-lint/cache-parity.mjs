import { readFileSync, writeFileSync, rmSync, statSync, utimesSync, mkdirSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const fixture = path.join(frontend, "client/src/lib/__speed_cache_probe.ts");
const config = path.join(frontend, "eslint.config.js");
const originalConfig = readFileSync(config);
const harness = path.join(import.meta.dirname, "run-candidate.mjs");
const records = [];
const clean = "console.log('cache probe'); export {};".padEnd(160, " ") + "\n";
// This rule is retained in ESLint by the hybrid configuration. A native
// Oxlint diagnostic must not mask a stale residual ESLint cache entry.
const invalid = "declare const probe: { run: () => void } | undefined; (probe?.run)(); console.log('cache probe'); export {};".padEnd(160, " ") + "\n";
mkdirSync(path.join(import.meta.dirname, "cache-probes"), { recursive: true });

const run = (candidate, stage, expectedCode, diagnostic) => {
  const result = spawnSync(process.execPath, [harness, candidate], { cwd: root, env: process.env, encoding: "utf8", timeout: 60000 });
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  writeFileSync(path.join(import.meta.dirname, "cache-probes", `${candidate}-${stage}.log`), output);
  const passed = !result.error && !result.signal && result.status === expectedCode && (!diagnostic || diagnostic.test(output));
  records.push({ candidate, stage, exitCode: result.status, signal: result.signal, error: result.error?.message, pass: passed });
};

try {
  for (const candidate of ["lint-eslint-cached", "lint-oxlint-hybrid-cached"]) {
    rmSync(path.join(frontend, "node_modules/.cache", candidate.startsWith("lint-eslint") ? "eslint-speed" : "eslint-residual-speed"), { force: true });
    writeFileSync(fixture, clean);
    // Whole-second timestamp makes preservation exact even across filesystems
    // with different timestamp resolution; Date round-tripping cannot weaken it.
    const fixedTime = new Date("2026-01-01T00:00:00.000Z");
    utimesSync(fixture, fixedTime, fixedTime);
    run(candidate, "cold-clean", 0);
    run(candidate, "warm-clean", 0);
    const saved = statSync(fixture);
    writeFileSync(fixture, invalid);
    utimesSync(fixture, saved.atime, saved.mtime);
    const changed = statSync(fixture);
    if (changed.size !== saved.size || changed.mtimeMs !== saved.mtimeMs) {
      records.push({
        candidate, stage: "changed-content-same-size-and-mtime", pass: false,
        preconditionError: "Cache invalidation probe must preserve both size and mtime exactly",
        before: { size: saved.size, mtimeMs: saved.mtimeMs },
        after: { size: changed.size, mtimeMs: changed.mtimeMs },
      });
    } else {
      run(candidate, "changed-content-same-size-and-mtime", 1, /no-unsafe-optional-chaining/);
    }
    writeFileSync(fixture, clean);
    run(candidate, "fixed-content", 0);
    const changedConfig = originalConfig.toString("utf8").replace(
      /\n\);\s*$/,
      '\n  { files: ["client/src/lib/__speed_cache_probe.ts"], rules: { "no-console": "error" } },\n);\n',
    );
    if (changedConfig === originalConfig.toString("utf8")) throw new Error("Could not add the scoped configuration invalidation probe");
    writeFileSync(config, changedConfig);
    run(candidate, "changed-config", 1, /no-console/);
    writeFileSync(config, originalConfig);
    run(candidate, "restored-config", 0);
  }
} finally {
  writeFileSync(config, originalConfig);
  rmSync(fixture, { force: true });
}
writeFileSync(path.join(import.meta.dirname, "cache-parity-results.json"), JSON.stringify(records, null, 2) + "\n");
console.log(JSON.stringify({ cases: records.length, passed: records.filter((record) => record.pass).length, records }, null, 2));
if (records.some((record) => !record.pass)) process.exitCode = 1;
