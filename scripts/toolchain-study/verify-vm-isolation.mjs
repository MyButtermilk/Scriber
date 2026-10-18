/**
 * Bounded, untimed checks of the actual component config/setup and VM scheduling.
 * node verify-vm-isolation.mjs --frontend ABS --npm ABS --output ABS
 * --npm names npm/bin/npm-cli.js (or a symlink to it). No production files change.
 */
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { parseArgs, stripVTControlCharacters } from "node:util";
import { fileURLToPath } from "node:url";

const { values } = parseArgs({ options: Object.fromEntries(["frontend", "npm", "output"].map(name => [name, { type: "string" }])) });
for (const name of ["frontend", "npm", "output"]) {
  if (!values[name] || !path.isAbsolute(values[name])) throw new Error(`--${name} must be absolute`);
}
const frontend = fs.realpathSync(values.frontend), npm = fs.realpathSync(values.npm), output = path.resolve(values.output);
const relOutput = path.relative(frontend, output);
if (!relOutput || (!relOutput.startsWith(`..${path.sep}`) && relOutput !== ".." && !path.isAbsolute(relOutput))) throw new Error("Output must be outside Frontend");
if (fs.existsSync(output) && fs.readdirSync(output).length) throw new Error("Output directory must be empty");
fs.mkdirSync(output, { recursive: true });
const digest = value => createHash("sha256").update(value).digest("hex");
const normalize = value => {
  const filename = value.startsWith("file:") ? fileURLToPath(value) : value;
  const result = path.resolve(filename).replaceAll("\\", "/");
  return process.platform === "win32" ? result.toLowerCase() : result;
};
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const need = (condition, message) => { if (!condition) throw new Error(message); };
function snapshot() {
  const files = ["package.json", "package-lock.json", "tsconfig.json", "tsconfig.tests.json", "vitest.components.config.ts", "vitest.setup.ts", "scripts/test-lib.mjs"];
  function visit(directory) {
    for (const entry of fs.readdirSync(path.join(frontend, directory), { withFileTypes: true })) {
      const relative = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(relative);
      else if (entry.isFile()) files.push(relative);
      else throw new Error(`Unexpected special source entry: ${relative}`);
    }
  }
  visit("client/src");
  return Object.fromEntries(files.sort().map(filename => [filename.replaceAll("\\", "/"), digest(fs.readFileSync(path.join(frontend, filename)))]));
}
const guard = "Component test files are sharing one global context. Run them with at least two workers (do not use --maxWorkers=1 or --no-file-parallelism).";
const env = { ...process.env, PATH: [path.dirname(process.execPath), path.resolve(path.dirname(npm), "../../.bin"), process.env.PATH ?? ""].join(path.delimiter), CI: "true", NO_COLOR: "1", FORCE_COLOR: "0", npm_config_update_notifier: "false" };
// Otherwise a caller's environment could override even explicit worker options.
delete env.VITEST_MAX_WORKERS;
const report = { startedAt: new Date().toISOString(), frontend, platform: process.platform, node: process.version, nodeExecutable: process.execPath, npmExecutable: npm, harnessSha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))), inputHashesBefore: snapshot(), cases: [], failures: [], passed: false };
const save = () => fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2) + "\n");
let work;
async function command(args, label, extraEnv = {}, timeout = 60000) {
  const stdoutPath = path.join(output, `${label}.stdout.log`), stderrPath = path.join(output, `${label}.stderr.log`);
  const stdoutFd = fs.openSync(stdoutPath, "wx"), stderrFd = fs.openSync(stderrPath, "wx");
  let child, timer, status = null, signal = null, error = null, timedOut = false, termination = null;
  const kill = () => {
    if (!child?.pid) return;
    if (process.platform === "win32") {
      const killed = spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { encoding: "utf8", timeout: 10000, windowsHide: true });
      termination = { status: killed.status, signal: killed.signal, error: killed.error?.message ?? null, output: `${killed.stdout ?? ""}${killed.stderr ?? ""}` };
    } else {
      try { process.kill(-child.pid, "SIGKILL"); termination = { status: 0 }; }
      catch (failure) { termination = { error: failure.message }; }
    }
  };
  try {
    child = spawn(process.execPath, [npm, ...args], { cwd: frontend, env: { ...env, ...extraEnv }, stdio: ["ignore", stdoutFd, stderrFd], detached: process.platform !== "win32", windowsHide: true });
    timer = setTimeout(() => { timedOut = true; kill(); }, timeout);
    await new Promise(resolve => { child.once("error", failure => { error = failure.message; }); child.once("close", (code, reason) => { status = code; signal = reason; resolve(); }); });
  } catch (failure) { error = failure.message; kill(); }
  finally { clearTimeout(timer); fs.closeSync(stdoutFd); fs.closeSync(stderrFd); }
  const log = stripVTControlCharacters(fs.readFileSync(stdoutPath, "utf8") + fs.readFileSync(stderrPath, "utf8"));
  fs.writeFileSync(path.join(output, `${label}.log`), log);
  return { command: [process.execPath, npm, ...args], status, signal, error, timedOut, termination, log: `${label}.log` };
}
const completed = result => result.error === null && result.signal === null && !result.timedOut && Number.isInteger(result.status);
try {
  need(process.versions.node === "26.5.0", "Pinned Node 26.5.0 required");
  const npmResult = await command(["--version"], "npm-version");
  report.npm = fs.readFileSync(path.join(output, npmResult.log), "utf8").trim();
  need(completed(npmResult) && npmResult.status === 0 && report.npm === "11.17.0", "Pinned npm 11.17.0 required");
  report.vitest = JSON.parse(fs.readFileSync(path.join(frontend, "node_modules/vitest/package.json"))).version;
  need(report.vitest === "4.1.10", "Pinned Vitest 4.1.10 required");
  need(JSON.parse(fs.readFileSync(path.join(frontend, "package.json"))).scripts["test:components"] === "vitest run --config vitest.components.config.ts", "Expected actual Node Vitest npm script");
  // Windows hosted runners put the checkout on D: and os.tmpdir() on C:.
  // Vitest/Vite's entry resolution failed across those drives before collecting
  // any fixture. Keep the owned project beside Frontend, on its real drive.
  work = fs.realpathSync(fs.mkdtempSync(path.join(path.dirname(frontend), "scriber-vm-isolation-")));
  report.workDirectory = work;
  report.workPlacement = { kind: "frontend-sibling", frontendRoot: path.parse(frontend).root, workRoot: path.parse(work).root };
  need(normalize(path.parse(frontend).root) === normalize(path.parse(work).root), "Temporary project must share Frontend's filesystem root");
  fs.mkdirSync(path.join(work, "fixtures"));
  fs.mkdirSync(path.join(work, "node_modules"));
  fs.writeFileSync(path.join(work, "package.json"), '{"name":"scriber-vm-isolation-probe","private":true,"type":"module"}\n');
  // Individual directory links retain installed dependencies while keeping Vite's
  // generated .vite/.vite-temp files in the owned temporary node_modules root.
  for (const entry of fs.readdirSync(path.join(frontend, "node_modules"), { withFileTypes: true })) {
    if (entry.name.startsWith(".") || !entry.isDirectory()) continue;
    fs.symlinkSync(path.join(frontend, "node_modules", entry.name), path.join(work, "node_modules", entry.name), process.platform === "win32" ? "junction" : "dir");
  }
  for (const [name, pkg, source] of [
    ["scriber-vm-cjs-probe", { main: "index.cjs" }, "module.exports = { count: 0 };\n"],
    ["scriber-vm-esm-probe", { type: "module", main: "index.js" }, "export const state = { count: 0 };\n"],
    ["scriber-vm-mock-probe", { type: "module", main: "index.js" }, "export const value = 'actual';\n"],
  ]) {
    const directory = path.join(work, "node_modules", name);
    fs.mkdirSync(directory);
    fs.writeFileSync(path.join(directory, "package.json"), JSON.stringify({ name, version: "1.0.0", ...pkg }));
    fs.writeFileSync(path.join(directory, pkg.main), source);
  }
  fs.writeFileSync(path.join(work, "fixtures/state.ts"), "export const state = { count: 0 };\n");
  const fixtureFiles = [];
  for (let index = 0; index < 6; index++) {
    const filename = path.join(work, "fixtures", `isolation-${index}.test.tsx`);
    fixtureFiles.push(normalize(filename));
    fs.writeFileSync(filename, `import { expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { appendFileSync } from "node:fs";
import { threadId } from "node:worker_threads";
import { state } from "./state";
import cjs from "scriber-vm-cjs-probe";
import { state as esm } from "scriber-vm-esm-probe";
${index % 2 ? 'vi.mock("scriber-vm-mock-probe", () => ({ value: "mocked" }));' : ""}
import { value } from "scriber-vm-mock-probe";
test("fresh browser globals, source and external modules, and file-local mock ${index}", () => {
  expect(process.versions.node).toBe("26.5.0");
  expect(process.versions.bun).toBeUndefined();
  expect(navigator.userAgent).toContain("jsdom");
  expect(globalThis).toBe(window);
  expect(Reflect.get(globalThis, Symbol.for("scriber.components.fileContext"))).toBe(true);
  expect(Reflect.get(globalThis, Symbol.for("scriber.review.leakedGlobal"))).toBeUndefined();
  expect(localStorage.getItem("scriber-review")).toBeNull();
  expect(sessionStorage.getItem("scriber-review")).toBeNull();
  expect(document.querySelector("[data-review-leak]")).toBeNull();
  expect(document.cookie).toBe("");
  expect(document.createElement("div")).toBeInstanceOf(HTMLElement);
  expect(document.createElement("div")).toBeInstanceOf(window.HTMLElement);
  expect(state.count).toBe(0); expect(cjs.count).toBe(0); expect(esm.count).toBe(0);
  expect(value).toBe(${JSON.stringify(index % 2 ? "mocked" : "actual")});
  appendFileSync(process.env.SCRIBER_VM_TRACE!, JSON.stringify({ file: import.meta.url, threadId, poolId: process.env.VITEST_POOL_ID, workerId: process.env.VITEST_WORKER_ID, node: process.versions.node }) + "\\n");
  Reflect.set(globalThis, Symbol.for("scriber.review.leakedGlobal"), ${index});
  localStorage.setItem("scriber-review", "dirty"); sessionStorage.setItem("scriber-review", "dirty");
  document.cookie = "scriber-review=dirty";
  const element = document.createElement("div"); element.dataset.reviewLeak = "dirty"; document.body.append(element);
  state.count++; cjs.count++; esm.count++;
});
test("local module reset reloads state ${index}", async () => {
  vi.resetModules();
  const first = await import("./state"); expect(first.state.count).toBe(0); first.state.count++;
  expect((await import("./state")).state.count).toBe(1);
  vi.resetModules(); const second = await import("./state");
  expect(second.state.count).toBe(0); expect(second.state).not.toBe(first.state);
});
test("render and mock for setup cleanup ${index}", () => {
  render(createElement("div", { "data-testid": "review-render" }, "review"));
  expect(screen.getByTestId("review-render")).toBeInTheDocument();
  vi.spyOn(Math, "random").mockReturnValue(0.125); expect(Math.random()).toBe(0.125);
});
test("setup cleanup and restoreMocks run between tests ${index}", () => {
  expect(screen.queryByTestId("review-render")).toBeNull();
  expect(vi.isMockFunction(Math.random)).toBe(false);
});
`);
  }
  const config = path.join(work, "vitest.isolation.config.mjs");
  const configSource = `import sourceConfig from ${JSON.stringify(path.join(frontend, "vitest.components.config.ts").replaceAll("\\", "/"))};\nimport { mergeConfig } from "vitest/config";\nexport default mergeConfig(sourceConfig, { root: ${JSON.stringify(work)}, cacheDir: ${JSON.stringify(path.join(work, "node_modules/.vite"))}, test: { include: ["fixtures/*.test.tsx"] } });\n`;
  fs.writeFileSync(config, configSource);
  const reporter = path.join(work, "errors-reporter.mjs");
  fs.writeFileSync(reporter, `import { writeFileSync } from "node:fs";
export default class IsolationErrorsReporter {
  onTestRunEnd(modules, unhandledErrors, reason) {
    const errors = values => [...values].map(error => ({ name: error.name, message: error.message, stack: error.stack }));
    writeFileSync(process.env.SCRIBER_VM_ERRORS, JSON.stringify({ reason, node: process.versions.node, pools: [...new Set(modules.map(module => module.project.config.pool))], modules: modules.map(module => module.moduleId), moduleConfigs: modules.map(module => ({ file: module.moduleId, pool: module.project.config.pool, isolate: module.project.config.isolate, maxWorkers: module.project.config.maxWorkers })), unhandledErrors: errors(unhandledErrors), collectionErrors: modules.flatMap(module => [ { file: module.moduleId, suite: null, errors: errors(module.errors()) }, ...[...module.children.allSuites()].map(suite => ({ file: module.moduleId, suite: suite.fullName, errors: errors(suite.errors()) })) ]).filter(entry => entry.errors.length) }, null, 2) + "\\n", { flag: "wx" });
  }
}\n`);
  // Retain every generated source as reproducible evidence, independently of temp cleanup.
  fs.cpSync(path.join(work, "fixtures"), path.join(output, "fixtures"), { recursive: true });
  fs.writeFileSync(path.join(output, "generated-config.mjs"), configSource);
  fs.copyFileSync(reporter, path.join(output, "errors-reporter.mjs"));
  fs.copyFileSync(path.join(frontend, "vitest.components.config.ts"), path.join(output, "production-config.ts"));
  fs.copyFileSync(path.join(frontend, "vitest.setup.ts"), path.join(output, "production-setup.ts"));
  report.expectedFiles = fixtureFiles.sort();
  const cases = [
    { name: "two-workers", args: ["--maxWorkers=2"], workers: 2 },
    { name: "three-workers", args: ["--maxWorkers=3"], workers: 3 },
    { name: "one-worker-guard", args: ["--maxWorkers=1"], workers: 1, negative: true },
    { name: "serial-guard", args: ["--no-file-parallelism"], workers: 1, negative: true },
  ];
  for (const probe of cases) {
    const jsonPath = path.join(output, `${probe.name}.json`), errorsPath = path.join(output, `${probe.name}.errors.json`), tracePath = path.join(output, `${probe.name}.trace.jsonl`);
    // npm run already supplies --config, and Vitest rejects duplicate config
    // flags. npm exec --offline --no uses the same installed CLI without fetching;
    // the temporary config imports the real production config and its setup.
    const result = await command(["exec", "--offline", "--no", "--", "vitest", "run", "--config", config, ...probe.args, "--reporter=json", `--reporter=${reporter}`, "--outputFile", jsonPath], probe.name, { SCRIBER_VM_ERRORS: errorsPath, SCRIBER_VM_TRACE: tracePath });
    const entry = { name: probe.name, expected: probe.negative ? "guard rejection" : "isolated reused VM contexts", process: result, passed: false };
    report.cases.push(entry); save();
    need(completed(result), `${probe.name}: abnormal process termination`);
    const json = JSON.parse(fs.readFileSync(jsonPath, "utf8")), errors = JSON.parse(fs.readFileSync(errorsPath, "utf8"));
    const files = json.testResults.map(file => normalize(file.name)).sort();
    const modules = errors.modules.map(normalize).sort();
    const assertions = json.testResults.flatMap(file => file.assertionResults);
    entry.observed = { status: result.status, files: files.length, totalTests: json.numTotalTests, passedTests: json.numPassedTests, failedTests: json.numFailedTests, pendingTests: json.numPendingTests, todoTests: json.numTodoTests, tracePresent: fs.existsSync(tracePath), traceFiles: null, threadIds: null, collectionErrors: errors.collectionErrors.length, unhandledErrors: errors.unhandledErrors.length };
    entry.runnerErrors = { collection: errors.collectionErrors, unhandled: errors.unhandledErrors, failedFiles: json.testResults.filter(file => file.status !== "passed").map(file => ({ file: file.name, message: file.message, assertions: file.assertionResults.filter(test => test.status !== "passed") })) };
    // A collection/setup failure can prevent the first trace write. Record and
    // validate the original runner errors before touching that optional file.
    save();
    need(equal(files, fixtureFiles) && equal(modules, fixtureFiles), `${probe.name}: wrong file/module inventory`);
    need(errors.node === "26.5.0" && equal(errors.pools, ["vmThreads"]), `${probe.name}: wrong runtime/pool`);
    need(errors.unhandledErrors.length === 0 && json.numPendingTests === 0 && json.numTodoTests === 0 && json.numPendingTestSuites === 0, `${probe.name}: unexpected unhandled/pending/skipped results`);
    const collectionMessages = errors.collectionErrors.flatMap(entry => entry.errors.map(error => error.message)).join("; ");
    if (probe.negative) {
      need(errors.collectionErrors.length === 5 && errors.collectionErrors.every(entry => entry.suite === null && entry.errors.length === 1 && entry.errors[0].message === guard), `${probe.name}: expected only five exact guard errors; received: ${collectionMessages}`);
    } else {
      need(errors.collectionErrors.length === 0 && json.testResults.every(file => file.status === "passed" && !file.message), `${probe.name}: unexpected collection/file failure: ${collectionMessages}`);
    }
    need(entry.observed.tracePresent, `${probe.name}: no worker trace was produced; see recorded runnerErrors`);
    const traces = fs.readFileSync(tracePath, "utf8").trim().split(/\r?\n/).map(line => JSON.parse(line));
    const workers = [...new Set(traces.map(trace => trace.threadId))];
    entry.observed.traceFiles = traces.length; entry.observed.threadIds = workers; save();
    need(workers.length === probe.workers && workers.every(id => id > 0), `${probe.name}: worker count/reuse not observed`);
    need(traces.every(trace => trace.node === "26.5.0"), `${probe.name}: wrong worker runtime`);
    if (probe.negative) {
      need(result.status === 1 && json.success === false && json.numTotalTests === 4 && json.numPassedTests === 4 && json.numFailedTests === 0, `${probe.name}: expected exactly one clean file then guard-only collection failures`);
      need(assertions.length === 4 && assertions.every(assertion => assertion.status === "passed" && assertion.failureMessages.length === 0), `${probe.name}: unexpected assertion failure`);
      need(traces.length === 1 && errors.collectionErrors.length === 5, `${probe.name}: guard did not reject all remaining files`);
      const passedFiles = json.testResults.filter(file => file.status === "passed"), failedFiles = json.testResults.filter(file => file.status === "failed");
      need(passedFiles.length === 1 && failedFiles.length === 5 && failedFiles.every(file => file.message.includes(guard)), `${probe.name}: failed files do not identify exact guard`);
    } else {
      need(result.status === 0 && json.success === true && json.numTotalTests === 24 && json.numPassedTests === 24 && json.numFailedTests === 0, `${probe.name}: expected all 24 assertions to pass`);
      need(assertions.length === 24 && assertions.every(assertion => assertion.status === "passed" && assertion.failureMessages.length === 0), `${probe.name}: incomplete assertions`);
      need(traces.length === 6 && equal(traces.map(trace => normalize(trace.file)).sort(), fixtureFiles), `${probe.name}: wrong trace inventory`);
      need(workers.some(id => traces.filter(trace => trace.threadId === id).length >= 2), `${probe.name}: worker reuse not observed`);
    }
    need(equal(snapshot(), report.inputHashesBefore), `${probe.name}: production sources changed`);
    entry.sourcesUnchanged = true; entry.passed = true; save();
  }
  // These supported serial commands use two existing tests and the actual npm
  // script/config, with no temporary-config override. They are separate from the
  // four VM fixture controls above and assert identical complete test inventories.
  const serialFiles = ["client/src/lib/desktop-updates.test.tsx", "client/src/lib/initial-backend-access.test.tsx"];
  const expectedSerialFiles = serialFiles.map(file => normalize(path.join(frontend, file))).sort();
  let serialNames;
  for (const probe of [
    { name: "threads-one-worker", flag: "--maxWorkers=1" },
    { name: "threads-serial", flag: "--no-file-parallelism" },
  ]) {
    const jsonPath = path.join(output, `${probe.name}.json`), errorsPath = path.join(output, `${probe.name}.errors.json`);
    const result = await command(["run", "test:components", "--", "--pool=threads", probe.flag, ...serialFiles, "--reporter=json", `--reporter=${reporter}`, "--outputFile", jsonPath], probe.name, { SCRIBER_VM_ERRORS: errorsPath });
    const entry = { name: probe.name, expected: "supported serial threads debug command", process: result, passed: false };
    report.cases.push(entry); save();
    need(completed(result) && result.status === 0, `${probe.name}: serial debug command failed`);
    const json = JSON.parse(fs.readFileSync(jsonPath, "utf8")), errors = JSON.parse(fs.readFileSync(errorsPath, "utf8"));
    const files = json.testResults.map(file => normalize(file.name)).sort();
    const assertions = json.testResults.flatMap(file => file.assertionResults);
    const names = json.testResults.flatMap(file => file.assertionResults.map(test => `${normalize(file.name)}\0${test.fullName}`)).sort();
    entry.observed = { status: result.status, files: files.length, totalTests: json.numTotalTests, passedTests: json.numPassedTests, failedTests: json.numFailedTests, pendingTests: json.numPendingTests, todoTests: json.numTodoTests, moduleConfigs: errors.moduleConfigs, collectionErrors: errors.collectionErrors.length, unhandledErrors: errors.unhandledErrors.length };
    entry.testNames = names;
    need(equal(files, expectedSerialFiles) && equal(errors.modules.map(normalize).sort(), expectedSerialFiles), `${probe.name}: wrong serial file/module inventory`);
    need(errors.node === "26.5.0" && equal(errors.pools, ["threads"]) && errors.moduleConfigs.length === 2 && equal(errors.moduleConfigs.map(config => normalize(config.file)).sort(), expectedSerialFiles) && errors.moduleConfigs.every(config => config.pool === "threads" && config.isolate === true && config.maxWorkers === 1), `${probe.name}: wrong resolved serial pool/isolation/worker settings`);
    need(json.success === true && json.numTotalTests === 14 && json.numPassedTests === 14 && json.numFailedTests === 0 && json.numPendingTests === 0 && json.numTodoTests === 0 && json.numPendingTestSuites === 0, `${probe.name}: expected all 14 tests to pass`);
    need(assertions.length === 14 && assertions.every(test => test.status === "passed" && test.failureMessages.length === 0), `${probe.name}: incomplete serial assertions`);
    need(json.testResults.every(file => file.status === "passed" && !file.message) && errors.collectionErrors.length === 0 && errors.unhandledErrors.length === 0, `${probe.name}: unexpected serial collection/unhandled error`);
    need(new Set(names).size === 14 && (!serialNames || equal(names, serialNames)), `${probe.name}: the two commands selected different tests`);
    serialNames ??= names;
    need(fs.readFileSync(path.join(output, result.log), "utf8").includes("> vitest run --config vitest.components.config.ts"), `${probe.name}: production npm script dispatch absent`);
    need(equal(snapshot(), report.inputHashesBefore), `${probe.name}: production sources changed`);
    entry.sourcesUnchanged = true; entry.passed = true; save();
  }
  report.passed = true;
} catch (error) {
  report.failures.push({ message: error.message, stack: error.stack });
} finally {
  try {
    if (work) fs.rmSync(work, { recursive: true, force: true });
    report.temporaryDirectoryRemoved = !work || !fs.existsSync(work);
    need(report.temporaryDirectoryRemoved, "Temporary directory survived cleanup");
    report.inputHashesAfter = snapshot();
    report.sourcesUnchanged = equal(report.inputHashesBefore, report.inputHashesAfter);
    need(report.sourcesUnchanged, "Production inputs changed");
  } catch (error) { report.failures.push({ message: error.message, stack: error.stack }); }
  report.passed &&= report.failures.length === 0 && report.cases.length === 6 && report.cases.every(probe => probe.passed);
  report.finishedAt = new Date().toISOString(); save();
  process.stdout.write(JSON.stringify({ passed: report.passed, cases: report.cases.map(probe => ({ name: probe.name, passed: probe.passed, observed: probe.observed })), failures: report.failures.map(error => error.message), report: path.join(output, "results.json") }, null, 2) + "\n");
  if (!report.passed) process.exitCode = 1;
}
