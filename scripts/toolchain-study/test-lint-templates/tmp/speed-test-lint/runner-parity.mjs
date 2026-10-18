import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const bun = process.env.SCRIBER_BUN_EXE ?? "bun";
const probeDir = path.join(import.meta.dirname, "runner-probes");
const logicFailure = path.join(frontend, "client/src/lib/__speed_async_failure.test.ts");
const componentFailure = path.join(frontend, "client/src/lib/__speed_async_failure.test.tsx");
const records = [];
mkdirSync(probeDir, { recursive: true });

const run = (id, binary, args, expectPass, expectedText) => {
  const result = spawnSync(binary, args, { cwd: frontend, env: process.env, encoding: "utf8", timeout: 60000 });
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  writeFileSync(path.join(probeDir, `${id}.log`), output);
  const completed = !result.error && !result.signal && result.status !== null;
  const pass = completed && (expectPass ? result.status === 0 : result.status !== 0) && expectedText.test(output);
  records.push({ id, exitCode: result.status, signal: result.signal, error: result.error?.message, pass });
};

try {
  writeFileSync(path.join(probeDir, "state.ts"), "export const state = { value: 0 };\n");
  writeFileSync(path.join(probeDir, "a.test.ts"), 'import test from "node:test"; import assert from "node:assert/strict"; import { state } from "./state.ts"; test("isolated first file", () => { assert.equal(state.value, 0); state.value = 1; globalThis.__scriberProbe = 1; });\n');
  writeFileSync(path.join(probeDir, "b.test.ts"), 'import test from "node:test"; import assert from "node:assert/strict"; import { state } from "./state.ts"; test("isolated second file", () => { assert.equal(state.value, 0); assert.equal(globalThis.__scriberProbe, undefined); });\n');
  const files = [path.join(probeDir, "a.test.ts"), path.join(probeDir, "b.test.ts")];
  run("node-isolation", process.execPath, [path.join(frontend, "node_modules/tsx/dist/cli.mjs"), "--test", "--test-reporter=tap", ...files], true, /# pass 2/);
  run("bun-isolation", bun, ["test", "--isolate", ...files], true, /2 pass/);
  run("bun-parallel-isolation", bun, ["test", "--isolate", "--parallel=2", ...files], true, /2 pass/);
  run("bun-unisolated-negative-control", bun, ["test", ...files], false, /1 fail/);

  writeFileSync(logicFailure, 'import test from "node:test"; import assert from "node:assert/strict"; test("scriber deliberate asynchronous assertion failure", async () => { await Promise.resolve(); assert.equal(1, 2); });\n');
  const harness = path.join(import.meta.dirname, "run-candidate.mjs");
  run("node-complete-suite-failure", process.execPath, [harness, "lib-node", "--test-reporter=tap"], false, /# fail 1/);
  run("bun-complete-suite-failure", process.execPath, [harness, "lib-bun-isolated"], false, /1 fail/);
  rmSync(logicFailure);

  for (const candidate of ["components-node-threads-2", "components-bun-threads-2", "components-node-forks-2", "components-bun-forks-2"]) {
    const expectBun = candidate.includes("-bun-");
    writeFileSync(componentFailure, `import { expect, test } from "vitest"; test("scriber actual worker runtime and jsdom", () => { expect(Boolean(process.versions.bun)).toBe(${expectBun}); expect(navigator.userAgent).toContain("jsdom"); expect(document.createElement("div")).toBeInstanceOf(HTMLElement); });\n`);
    run(`${candidate}-worker-runtime`, process.execPath, [harness, candidate, "client/src/lib/__speed_async_failure.test.tsx"], true, /1 passed/);
    writeFileSync(componentFailure, 'import { expect, test } from "vitest"; test("scriber deliberate asynchronous assertion failure", async () => { await Promise.resolve(); expect(1).toBe(2); });\n');
    run(`${candidate}-assertion-failure`, process.execPath, [harness, candidate, "client/src/lib/__speed_async_failure.test.tsx"], false, /scriber deliberate asynchronous assertion failure/);
  }
} finally {
  rmSync(logicFailure, { force: true });
  rmSync(componentFailure, { force: true });
  for (const name of ["state.ts", "a.test.ts", "b.test.ts"]) rmSync(path.join(probeDir, name), { force: true });
}
writeFileSync(path.join(import.meta.dirname, "runner-parity-results.json"), JSON.stringify(records, null, 2) + "\n");
console.log(JSON.stringify({ cases: records.length, passed: records.filter((record) => record.pass).length, records }, null, 2));
if (records.some((record) => !record.pass)) process.exitCode = 1;
