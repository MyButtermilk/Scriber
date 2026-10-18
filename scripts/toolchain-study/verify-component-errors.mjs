/**
 * Untimed, deliberately narrow unhandled-error control for the production VM pool.
 * node verify-component-errors.mjs --frontend ABS --npm ABS --output ABS
 * --npm names npm/bin/npm-cli.js (or its Unix symlink). No retries or pool overrides.
 */
import fs from "node:fs";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { parseArgs, stripVTControlCharacters } from "node:util";
import { fileURLToPath } from "node:url";

const { values } = parseArgs({ options: Object.fromEntries(["frontend", "npm", "output"].map(key => [key, { type: "string" }])) });
for (const key of ["frontend", "npm", "output"]) {
  if (!values[key] || !path.isAbsolute(values[key])) throw new Error(`--${key} must be an absolute path`);
}
const frontend = fs.realpathSync(values.frontend);
const npm = fs.realpathSync(values.npm);
function outsideFrontend(directory) {
  const relative = path.relative(frontend, directory);
  return relative && (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative));
}
if (!outsideFrontend(path.resolve(values.output))) throw new Error("Output must be outside Frontend");
if (fs.existsSync(values.output) && fs.readdirSync(values.output).length) throw new Error("Use an empty output directory");
fs.mkdirSync(values.output, { recursive: true });
const output = fs.realpathSync(values.output);
if (!outsideFrontend(output)) throw new Error("Resolved output must be outside Frontend");
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const slash = value => value.replaceAll("\\", "/");
const canonical = value => process.platform === "win32" ? slash(value).toLowerCase() : slash(value);
const normalizeFile = value => {
  if (typeof value !== "string" || !value) throw new Error("Reporter omitted a filename");
  const filename = value.startsWith("file:") ? fileURLToPath(value) : value;
  return canonical(path.isAbsolute(filename) ? path.relative(frontend, filename) : filename.replace(/^\.\//, ""));
};
const excludedRoots = ["node_modules", "dist", "output", ".git", ".cache", ".vite", "src-tauri/target"];
function snapshot() {
  const manifest = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      const relative = slash(path.relative(frontend, filename));
      if (excludedRoots.some(root => relative === root || relative.startsWith(`${root}/`))
        || relative === ".eslintcache" || relative.endsWith(".tsbuildinfo")) continue;
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) {
        const bytes = fs.readFileSync(filename);
        manifest.push({ file: relative, bytes: bytes.length, sha256: digest(bytes) });
      } else throw new Error(`Unexpected special source/config entry: ${filename}`);
    }
  }
  visit(frontend);
  manifest.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : 0);
  return { files: manifest.length, sha256: digest(JSON.stringify(manifest)), manifest };
}
const inventory = () => fs.globSync("client/src/**/*.test.tsx", { cwd: frontend }).map(canonical).sort();
const inputHashes = () => Object.fromEntries(["package.json", "package-lock.json", "vitest.components.config.ts", "vitest.setup.ts", "tsconfig.json", "tsconfig.tests.json"].map(file => [file, digest(fs.readFileSync(path.join(frontend, file)))]));
const id = randomUUID().replaceAll("-", "");
const relativeFixture = `client/src/lib/__scriber_component_errors_${id}.test.tsx`;
const fixture = path.join(frontend, relativeFixture);
const titles = ["scriber component error control: runtime and DOM", "scriber component error control: active rejection", "scriber component error control: active timer throw", "scriber component error control: active microtask throw"];
const markers = ["REJECTION", "TIMER", "MICROTASK"].map(kind => `SCRIBER_COMPONENT_ERROR_${id}_${kind}`);
const fixtureSource = Buffer.from(`import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
test(${JSON.stringify(titles[0])}, () => {
  expect(process.versions.node).toBe("26.5.0");
  expect(process.versions.bun ?? null).toBeNull();
  expect(globalThis).toBe(window);
  expect(Reflect.get(globalThis, Symbol.for("scriber.components.fileContext"))).toBe(true);
  expect(navigator.userAgent).toContain("jsdom");
  expect(document.createElement("div")).toBeInstanceOf(HTMLElement);
  render(createElement("button", {}, "scriber error-control DOM"));
  expect(screen.getByRole("button", { name: "scriber error-control DOM" })).toBeInTheDocument();
});
test(${JSON.stringify(titles[1])}, async () => {
  expect(screen.queryByRole("button", { name: "scriber error-control DOM" })).toBeNull();
  void Promise.reject(new Error(${JSON.stringify(markers[0])}));
  await new Promise(resolve => setTimeout(resolve, 80));
  expect(true).toBe(true);
});
test(${JSON.stringify(titles[2])}, async () => {
  setTimeout(() => { throw new Error(${JSON.stringify(markers[1])}); }, 5);
  await new Promise(resolve => setTimeout(resolve, 80));
  expect(true).toBe(true);
});
test(${JSON.stringify(titles[3])}, async () => {
  queueMicrotask(() => { throw new Error(${JSON.stringify(markers[2])}); });
  await new Promise(resolve => setTimeout(resolve, 80));
  expect(true).toBe(true);
});
`);
const json = path.join(output, "vitest.json");
const errorsJson = path.join(output, "runtime-errors.json");
const reporter = path.join(output, "error-reporter.mjs");
const expectedCommand = "vitest run --config vitest.components.config.ts";
const env = {
  ...process.env,
  PATH: [path.dirname(process.execPath), path.resolve(path.dirname(npm), "../../.bin"), process.env.PATH ?? ""].join(path.delimiter),
  CI: "true", NO_COLOR: "1", FORCE_COLOR: "0", npm_config_update_notifier: "false",
};
const report = {
  startedAt: new Date().toISOString(), platform: process.platform, architecture: process.arch,
  node: process.version, nodeExecutable: process.execPath, npmExecutable: npm, frontend,
  harnessSha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))),
  scope: { policy: "One uniquely named temporary component file, selected through the actual npm script. This proves unhandled-error exit propagation only; it is not a full-suite inventory or performance measurement.", expectedFiles: [canonical(relativeFixture)], expectedTests: titles, excludedSnapshotRoots: excludedRoots, excludedSnapshotFiles: [".eslintcache", "*.tsbuildinfo"] },
  fixture: { relative: relativeFixture, sha256: digest(fixtureSource), expectedErrorMessages: markers },
  environment: { GITHUB_ACTIONS: env.GITHUB_ACTIONS ?? null, GITHUB_WORKSPACE: env.GITHUB_WORKSPACE ?? null },
  processes: {}, failures: [], assertions: {}, passed: false,
};
const save = () => fs.writeFileSync(path.join(output, "results.json"), `${JSON.stringify(report, null, 2)}\n`);
const requireResult = (condition, message) => { if (!condition) throw new Error(message); };
let ownsFixture = false;
async function run(label, args, timeoutMs) {
  const log = path.join(output, `${label}.log`);
  const fd = fs.openSync(log, "wx");
  let child, timer, status = null, signal = null, error = null, timedOut = false, termination = null;
  const terminate = () => {
    if (!child?.pid) return;
    if (process.platform === "win32") {
      const result = spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { encoding: "utf8", timeout: 10000, windowsHide: true });
      termination = { status: result.status, signal: result.signal, error: result.error?.message ?? null, output: `${result.stdout ?? ""}${result.stderr ?? ""}` };
    } else {
      try { process.kill(-child.pid, "SIGKILL"); termination = { status: 0 }; }
      catch (failure) { termination = { error: failure.message }; }
    }
  };
  try {
    child = spawn(process.execPath, args, { cwd: frontend, env, stdio: ["ignore", fd, fd], detached: process.platform !== "win32", windowsHide: true });
    timer = setTimeout(() => { timedOut = true; terminate(); }, timeoutMs);
    await new Promise(resolve => {
      child.once("error", failure => { error = failure.message; });
      child.once("close", (code, reason) => { status = code; signal = reason; resolve(); });
    });
  } catch (failure) { error = failure.message; terminate(); }
  finally { clearTimeout(timer); fs.closeSync(fd); }
  const entry = { command: process.execPath, args, status, signal, error, timedOut, timeoutMs, termination, log: path.basename(log) };
  report.processes[label] = entry;
  save();
  requireResult(error === null && signal === null && !timedOut && Number.isInteger(status), `${label}: failed process invocation`);
  return { entry, text: stripVTControlCharacters(fs.readFileSync(log, "utf8")) };
}

save();
try {
  requireResult(process.version === "v26.5.0", "Expected Node v26.5.0");
  report.sourceBefore = snapshot();
  report.inputsBefore = inputHashes();
  report.componentFilesBefore = inventory();
  const manifest = JSON.parse(fs.readFileSync(path.join(frontend, "package.json"), "utf8"));
  report.componentScript = manifest.scripts?.["test:components"];
  requireResult(report.componentScript === expectedCommand, "Unexpected production component script");
  report.vitest = JSON.parse(fs.readFileSync(path.join(frontend, "node_modules/vitest/package.json"), "utf8")).version;
  requireResult(report.vitest === "4.1.10", "Expected Vitest 4.1.10 reporter semantics");
  const version = await run("npm-version", [npm, "--version"], 10000);
  requireResult(version.entry.status === 0 && version.text.trim() === "11.17.0", "Expected npm 11.17.0");
  fs.writeFileSync(path.join(output, "fixture-source.tsx"), fixtureSource, { flag: "wx" });
  fs.writeFileSync(reporter, `import { writeFileSync } from "node:fs";
export default class {
  onTestRunEnd(modules, unhandledErrors, reason) {
    const errors = values => values.map(error => ({ name: error.name ?? null, message: String(error.message ?? error), type: error.type ?? null, stack: error.stack ?? null }));
    const collectionErrors = modules.flatMap(module => [
      { file: module.moduleId, suite: null, errors: errors(module.errors()) },
      ...[...module.children.allSuites()].map(suite => ({ file: module.moduleId, suite: suite.fullName, errors: errors(suite.errors()) })),
    ]).filter(entry => entry.errors.length);
    writeFileSync(${JSON.stringify(errorsJson)}, JSON.stringify({ reason, node: process.versions.node, bun: process.versions.bun ?? null, pools: [...new Set(modules.map(module => module.project.config.pool))], modules: modules.map(module => module.moduleId), unhandledErrors: errors(unhandledErrors), collectionErrors }, null, 2) + "\\n", { flag: "wx" });
  }
}
`, { flag: "wx" });
  report.reporterSha256 = digest(fs.readFileSync(reporter));
  fs.writeFileSync(fixture, fixtureSource, { flag: "wx" });
  ownsFixture = true;
  save();
  const result = await run("component-errors", [npm, "run", "test:components", "--", relativeFixture, "--reporter=json", "--reporter", reporter, "--outputFile", json], 60000);
  requireResult(result.entry.status === 1, "Unhandled errors must cause exactly exit 1");
  requireResult(fs.existsSync(json) && fs.existsSync(errorsJson), "Missing fresh Vitest/error report");
  const data = JSON.parse(fs.readFileSync(json, "utf8"));
  const errors = JSON.parse(fs.readFileSync(errorsJson, "utf8"));
  report.jsonSha256 = digest(fs.readFileSync(json));
  report.errorsSha256 = digest(fs.readFileSync(errorsJson));
  report.runtimeErrors = errors;
  requireResult(Array.isArray(data.testResults) && data.testResults.length === 1 && Array.isArray(data.testResults[0].assertionResults), "Expected exactly one reported test file");
  const file = data.testResults[0];
  const cases = file.assertionResults;
  report.summary = Object.fromEntries(["success", "numTotalTests", "numPassedTests", "numFailedTests", "numPendingTests", "numTodoTests", "numTotalTestSuites", "numPassedTestSuites", "numFailedTestSuites", "numPendingTestSuites"].map(key => [key, data[key]]));
  report.tests = cases.map(test => ({ name: test.fullName, status: test.status, failureMessages: test.failureMessages }));
  report.assertions = {
    exactInventory: normalizeFile(file.name) === canonical(relativeFixture) && cases.length === titles.length && JSON.stringify(cases.map(test => test.fullName).sort()) === JSON.stringify([...titles].sort()),
    allAssertionsPassed: cases.every(test => test.status === "passed" && Array.isArray(test.failureMessages) && test.failureMessages.length === 0) && file.status === "passed" && file.message === "",
    exactSummary: data.success === true && data.numTotalTests === 4 && data.numPassedTests === 4 && data.numFailedTests === 0 && data.numPendingTests === 0 && data.numTodoTests === 0 && data.numTotalTestSuites === 1 && data.numPassedTestSuites === 1 && data.numFailedTestSuites === 0 && data.numPendingTestSuites === 0 && data.snapshot?.failure === false,
    // Vitest 4.1.10 reports passed assertions here even when unhandled errors
    // make the CLI fail. The separately checked exit 1 is the point of this control.
    runtimeAndPool: errors.reason === "passed" && errors.node === "26.5.0" && errors.bun === null && JSON.stringify(errors.pools) === JSON.stringify(["vmThreads"]),
    reporterInventory: Array.isArray(errors.modules) && JSON.stringify(errors.modules.map(normalizeFile)) === JSON.stringify([canonical(relativeFixture)]),
    exactUnhandledErrors: Array.isArray(errors.unhandledErrors) && errors.unhandledErrors.length === 3 && errors.unhandledErrors.every(error => error.name === "Error") && JSON.stringify(errors.unhandledErrors.map(error => error.message).sort()) === JSON.stringify([...markers].sort()) && errors.unhandledErrors.every(error => error.type === (error.message === markers[0] ? "Unhandled Rejection" : "Uncaught Exception")),
    noCollectionErrors: Array.isArray(errors.collectionErrors) && errors.collectionErrors.length === 0,
    actualScriptRan: result.text.includes(`> ${expectedCommand}`),
  };
  save();
  requireResult(Object.values(report.assertions).every(value => value === true), "An error-control assertion failed; inspect results.json and raw reports");
  report.passed = true;
} catch (failure) {
  report.failures.push({ phase: "verification", message: failure.message, stack: failure.stack ?? null });
} finally {
  try {
    if (ownsFixture) {
      requireResult(fs.existsSync(fixture) && fs.lstatSync(fixture).isFile(), "Owned fixture disappeared or became a special file");
      requireResult(fs.readFileSync(fixture).equals(fixtureSource), "Refusing to remove a concurrently modified fixture");
      fs.unlinkSync(fixture);
      requireResult(!fs.existsSync(fixture), "Owned fixture survived removal");
      report.fixtureCleanup = { byteIdenticalBeforeRemoval: true, absentAfterRemoval: true };
    } else report.fixtureCleanup = { created: false };
  } catch (failure) { report.failures.push({ phase: "fixture-cleanup", message: failure.message }); }
  try {
    report.sourceAfter = snapshot();
    report.inputsAfter = inputHashes();
    report.componentFilesAfter = inventory();
    report.restored = Boolean(report.sourceBefore) && report.sourceAfter.sha256 === report.sourceBefore.sha256
      && JSON.stringify(report.inputsAfter) === JSON.stringify(report.inputsBefore)
      && JSON.stringify(report.componentFilesAfter) === JSON.stringify(report.componentFilesBefore);
    requireResult(report.restored, "Source/config bytes or original component inventory changed");
  } catch (failure) { report.failures.push({ phase: "restoration", message: failure.message }); }
  report.passed = report.passed && report.failures.length === 0 && report.restored === true;
  report.finishedAt = new Date().toISOString();
  save();
  console.log(JSON.stringify({ passed: report.passed, assertions: report.assertions, restored: report.restored, failures: report.failures.map(failure => failure.message), results: path.join(output, "results.json") }));
  if (!report.passed) process.exitCode = 1;
}
