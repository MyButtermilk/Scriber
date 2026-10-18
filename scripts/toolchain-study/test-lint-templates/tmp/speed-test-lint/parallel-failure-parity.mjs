import { existsSync, globSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

// Failed async tests must propagate through every tested Bun parallel count
// while all original tests still run. A missing report excludes its candidate.
const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const fixture = path.join(frontend, "client/src/lib/__speed_parallel_async_failure.test.ts");
if (existsSync(fixture)) throw new Error("The deliberate parallel failure fixture already exists");
const originalFiles = globSync("client/src/**/*.test.ts", { cwd: frontend }).map((file) => file.replaceAll("\\", "/")).sort();
if (originalFiles.length !== 35) throw new Error("The failure probe requires all 35 original logic test files");
const expectedFiles = [...originalFiles, path.relative(frontend, fixture).replaceAll("\\", "/")].sort();
const output = path.join(import.meta.dirname, "parallel-failure-probes");
mkdirSync(output, { recursive: true });
const records = [];
try {
  writeFileSync(fixture, 'import test from "node:test"; import assert from "node:assert/strict"; test("scriber deliberate parallel asynchronous assertion failure", async () => { await Promise.resolve(); assert.equal(1, 2); });\n');
  for (const workers of [2, 4, 8]) {
    const candidate = `lib-bun-parallel-${workers}`;
    const xmlPath = path.join(output, `${candidate}.xml`);
    rmSync(xmlPath, { force: true });
    const result = spawnSync(process.execPath, [path.join(import.meta.dirname, "run-candidate.mjs"), candidate, "--reporter=junit", "--reporter-outfile", xmlPath], { cwd: root, env: process.env, encoding: "utf8", timeout: 60000 });
    const log = `${result.stdout ?? ""}${result.stderr ?? ""}`;
    writeFileSync(path.join(output, `${candidate}.log`), log);
    let xml = "";
    let reportError = null;
    try { xml = readFileSync(xmlPath, "utf8"); } catch (error) { reportError = error.message; }
    const cases = [...xml.matchAll(/<testcase\b([^>]*?)(?:\/?>)/g)];
    const failures = [...xml.matchAll(/<failure\b/g)].length;
    const errors = [...xml.matchAll(/<error\b/g)].length;
    const skipped = [...xml.matchAll(/<skipped\b/g)].length;
    const files = [...new Set(cases.map((entry) => {
      const file = /\bfile="([^"]+)"/.exec(entry[1])?.[1] ?? "";
      return (path.isAbsolute(file) ? path.relative(frontend, file) : file).replaceAll("\\", "/").replace(/^\.\//, "");
    }))].sort();
    const completeFileInventory = JSON.stringify(files) === JSON.stringify(expectedFiles);
    const pass = !result.error && !result.signal && !reportError && result.status === 1 && cases.length === 143 && failures === 1 && errors === 0 && skipped === 0 && completeFileInventory && /142 pass/.test(log) && /1 fail/.test(log) && /scriber deliberate parallel asynchronous assertion failure/.test(xml);
    records.push({ candidate, exitCode: result.status, signal: result.signal, error: result.error?.message, reportError, tests: cases.length, failures, errors, skipped, completeFileInventory, pass });
  }
} finally {
  rmSync(fixture, { force: true });
}
writeFileSync(path.join(import.meta.dirname, "parallel-failure-parity-results.json"), JSON.stringify(records, null, 2) + "\n");
console.log(JSON.stringify(records, null, 2));
if (records.some((record) => !record.pass)) process.exitCode = 1;
