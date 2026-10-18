/**
 * Untimed production failure controls for the prepared candidate Frontend.
 * Usage: node verify-production-gates.mjs --frontend ABS --npm ABS --output ABS
 * --npm is npm/bin/npm-cli.js (a Unix npm symlink is also accepted).
 * Runs the actual npm scripts, then restores every owned source fixture.
 */
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { parseArgs, stripVTControlCharacters } from "node:util";
import { fileURLToPath } from "node:url";

const { values } = parseArgs({ options: Object.fromEntries(["frontend", "npm", "output"].map(name => [name, { type: "string" }])) });
for (const name of ["frontend", "npm", "output"]) {
  if (!values[name] || !path.isAbsolute(values[name])) throw new Error(`--${name} must be an absolute path`);
}
const frontend = fs.realpathSync(values.frontend);
const npm = fs.realpathSync(values.npm);
const output = path.resolve(values.output);
const relativeOutput = path.relative(frontend, output);
if (!relativeOutput || (!relativeOutput.startsWith(`..${path.sep}`) && relativeOutput !== ".." && !path.isAbsolute(relativeOutput))) throw new Error("Use an output directory outside Frontend");
if (fs.existsSync(output) && fs.readdirSync(output).length) throw new Error("Use an empty output directory");
fs.mkdirSync(output, { recursive: true });
const bun = path.join(frontend, "node_modules/bun/bin/bun.exe");
const env = {
  ...process.env,
  PATH: [path.dirname(process.execPath), path.resolve(path.dirname(npm), "../../.bin"), process.env.PATH ?? ""].join(path.delimiter),
  CI: "true", NO_COLOR: "1", FORCE_COLOR: "0", npm_config_update_notifier: "false",
};
const digest = value => createHash("sha256").update(value).digest("hex");
const canonical = value => process.platform === "win32" ? value.replaceAll("\\", "/").toLowerCase() : value.replaceAll("\\", "/");
const inventory = () => fs.globSync("client/src/**/*.test.ts", { cwd: frontend }).map(canonical).sort();
const hashInputs = () => Object.fromEntries(["package.json", "package-lock.json", "tsconfig.json", "tsconfig.tests.json", "scripts/test-lib.mjs"].map(name => [name, digest(fs.readFileSync(path.join(frontend, name)))]));
function sourceSnapshot() {
  const entries = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) entries.push([canonical(path.relative(frontend, filename)), digest(fs.readFileSync(filename))]);
      else throw new Error(`Unexpected special source file: ${filename}`);
    }
  }
  visit(path.join(frontend, "client/src"));
  entries.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  return { files: entries.length, sha256: digest(JSON.stringify(entries)) };
}
const sentinel = "scriber production wrapper deliberate asynchronous failure";
const probes = {
  app: { relative: "client/src/__production_type_gate_app_probe.ts", source: 'export const productionAppTypeGateProbe: number = "deliberate app type-gate failure";\n' },
  tests: { relative: "client/src/__production_type_gate_test_probe.test.ts", source: 'export const productionTestTypeGateProbe: number = "deliberate test type-gate failure";\n' },
  logic: { relative: "client/src/lib/__integration_wrapper_async_failure.test.ts", source: `import test from "node:test";\nimport assert from "node:assert/strict";\ntest("${sentinel}", async () => { await Promise.resolve(); assert.equal(1, 2); });\n` },
};
const owned = new Set();
const report = {
  startedAt: new Date().toISOString(), platform: process.platform, architecture: process.arch,
  node: process.version, nodeExecutable: process.execPath, npmExecutable: npm, frontend,
  harnessSha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))),
  inputHashesBefore: hashInputs(), sourceBefore: sourceSnapshot(), logicFilesBefore: inventory(),
  processes: {}, phases: {}, failures: [], passed: false,
};
const save = () => fs.writeFileSync(path.join(output, "results.json"), `${JSON.stringify(report, null, 2)}\n`);
const requireResult = (condition, message) => { if (!condition) throw new Error(message); };
function createProbe(name) {
  fs.writeFileSync(path.join(frontend, probes[name].relative), probes[name].source, { flag: "wx" });
  owned.add(name);
}
function removeProbe(name) {
  if (!owned.has(name)) return;
  const probe = probes[name], filename = path.join(frontend, probe.relative);
  if (fs.existsSync(filename)) {
    requireResult(fs.readFileSync(filename, "utf8") === probe.source, `Refusing to remove concurrently modified fixture ${filename}`);
    fs.unlinkSync(filename);
  }
  owned.delete(name);
}

async function run(executable, args, label, timeout = 60000) {
  const stdoutLog = `${label}.stdout.log`, stderrLog = `${label}.stderr.log`, log = `${label}.log`;
  const stdoutFd = fs.openSync(path.join(output, stdoutLog), "w"), stderrFd = fs.openSync(path.join(output, stderrLog), "w");
  let child, timer, error = null, status = null, signal = null, timedOut = false, termination = null;
  const terminateTree = () => {
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
    child = spawn(executable, args, { cwd: frontend, env, stdio: ["ignore", stdoutFd, stderrFd], detached: process.platform !== "win32", windowsHide: true });
    timer = setTimeout(() => { timedOut = true; terminateTree(); }, timeout);
    await new Promise(resolve => {
      child.once("error", failure => { error = failure.message; });
      child.once("close", (code, reason) => { status = code; signal = reason; resolve(); });
    });
  } catch (failure) { error = failure.message; terminateTree(); }
  finally { clearTimeout(timer); fs.closeSync(stdoutFd); fs.closeSync(stderrFd); }
  const stdout = fs.readFileSync(path.join(output, stdoutLog), "utf8"), stderr = fs.readFileSync(path.join(output, stderrLog), "utf8");
  fs.writeFileSync(path.join(output, log), stdout + stderr);
  const entry = { command: [executable, ...args], status, exitCode: error || signal || timedOut ? null : status, error, signal, timedOut, termination, stdoutLog, stderrLog, log };
  report.processes[label] = entry;
  save();
  return { entry, stdout: stripVTControlCharacters(stdout), text: stripVTControlCharacters(stdout + stderr) };
}
const completed = entry => !entry.error && !entry.signal && !entry.timedOut && Number.isInteger(entry.exitCode);
async function checkPhase(label, probeName = null) {
  const { entry, text } = await run(process.execPath, [npm, "run", "check"], label);
  const appPosition = text.indexOf("> bun --check --tsconfig-override tsconfig.json");
  const testsPosition = text.indexOf("> bun --check --tsconfig-override tsconfig.tests.json");
  entry.appCommandRan = appPosition >= 0;
  entry.testsCommandRan = testsPosition >= 0;
  entry.dispatchOrdered = appPosition >= 0 && testsPosition > appPosition;
  entry.diagnosticLines = text.split(/\r?\n/).filter(line => /TS\d+/.test(line));
  entry.inputHashesAfter = hashInputs();
  if (probeName) {
    entry.probe = probes[probeName];
    entry.passed = completed(entry) && entry.exitCode === 1 && entry.appCommandRan
      && (probeName === "tests" ? entry.dispatchOrdered : !entry.testsCommandRan)
      && entry.diagnosticLines.length === 1 && entry.diagnosticLines[0].includes(path.basename(probes[probeName].relative)) && /\bTS2322\b/.test(entry.diagnosticLines[0]);
  } else entry.passed = completed(entry) && entry.exitCode === 0 && entry.dispatchOrdered && entry.diagnosticLines.length === 0;
  report.phases[label] = entry;
  save();
  console.log(`${label}: exit=${entry.exitCode}, diagnostics=${entry.diagnosticLines.length}, passed=${entry.passed}`);
  requireResult(entry.passed, `Unexpected production type-check result: ${label}`);
}

const xmlDecode = value => value.replace(/&(quot|apos|lt|gt|amp|#\d+|#x[\da-f]+);/gi, (_, entity) => entity[0] === "#" ? String.fromCodePoint(entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : Number(entity.slice(1))) : ({ quot: '"', apos: "'", lt: "<", gt: ">", amp: "&" })[entity.toLowerCase()]);
async function logicFailure() {
  const junit = path.join(output, "logic-async-error.xml");
  const { entry, text } = await run(process.execPath, [npm, "run", "test:lib", "--", "--reporter=junit", "--reporter-outfile", junit], "logic-async-error");
  report.phases["logic-async-error"] = entry;
  entry.probe = probes.logic;
  entry.junit = path.basename(junit);
  entry.passed = false;
  save();
  requireResult(completed(entry) && entry.exitCode === 1 && fs.existsSync(junit), "The async failure must return exit 1 with a JUnit report");
  const xml = fs.readFileSync(junit, "utf8");
  const cases = [...xml.matchAll(/<testcase\b([^>]*?)(?:\/>|>([\s\S]*?)<\/testcase>)/g)].map(match => ({
    attributes: Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(attribute => [attribute[1], xmlDecode(attribute[2])])),
    content: match[2] ?? "",
  }));
  const normalizeFile = filename => {
    requireResult(Boolean(filename), "JUnit omitted a test file");
    const value = filename.startsWith("file:") ? fileURLToPath(filename) : filename;
    return canonical(path.isAbsolute(value) ? path.relative(frontend, value) : value.replace(/^\.\//, ""));
  };
  const files = [...new Set(cases.map(test => normalizeFile(test.attributes.file)))].sort();
  const failed = cases.filter(test => /<failure\b/.test(test.content));
  const expectedFiles = [...report.logicFilesBefore, canonical(probes.logic.relative)].sort();
  entry.junitSha256 = digest(xml);
  entry.files = files;
  entry.tests = cases.length;
  entry.failures = [...xml.matchAll(/<failure\b/g)].length;
  entry.errors = [...xml.matchAll(/<error\b/g)].length;
  entry.skipped = [...xml.matchAll(/<skipped\b/g)].length;
  entry.passes = cases.filter(test => !/<(?:failure|error|skipped)\b/.test(test.content)).length;
  entry.completeInventory = JSON.stringify(files) === JSON.stringify(expectedFiles);
  entry.onlyIntentionalFailure = failed.length === 1 && failed[0].attributes.name === sentinel && normalizeFile(failed[0].attributes.file) === canonical(probes.logic.relative);
  entry.fullSummary = /Ran 143 tests across 36 files\./.test(text) && /\b142 pass\b/.test(text) && /\b1 fail\b/.test(text);
  entry.wrapperCommandRan = text.includes("> node scripts/test-lib.mjs");
  entry.passed = entry.tests === 143 && files.length === 36 && entry.passes === 142 && entry.failures === 1 && entry.errors === 0 && entry.skipped === 0
    && entry.completeInventory && entry.onlyIntentionalFailure && entry.fullSummary && entry.wrapperCommandRan;
  save();
  console.log(`logic-async-error: tests=${entry.tests}, files=${files.length}, failures=${entry.failures}, passed=${entry.passed}`);
  requireResult(entry.passed, "The async control did not retain all 142 original passes and only the deliberate failure");
}

let prerequisitesPassed = false;
try {
  requireResult(process.version === "v26.5.0", `Expected Node v26.5.0, got ${process.version}`);
  for (const probe of Object.values(probes)) requireResult(!fs.existsSync(path.join(frontend, probe.relative)), `Preexisting fixture path: ${probe.relative}`);
  requireResult(report.logicFilesBefore.length === 35, "Expected the pristine 35-file logic inventory");
  const manifest = JSON.parse(fs.readFileSync(path.join(frontend, "package.json"), "utf8"));
  report.scripts = manifest.scripts;
  const expectedScripts = { check: "npm run check:app && npm run check:tests", "check:app": "bun --check --tsconfig-override tsconfig.json", "check:tests": "bun --check --tsconfig-override tsconfig.tests.json", "test:lib": "node scripts/test-lib.mjs" };
  for (const [name, expected] of Object.entries(expectedScripts)) requireResult(manifest.scripts?.[name] === expected, `Unexpected production script ${name}`);
  const npmVersion = await run(process.execPath, [npm, "--version"], "npm-version", 10000);
  const bunVersion = await run(bun, ["--version"], "bun-version", 10000);
  report.npm = npmVersion.stdout.trim();
  report.bun = { declared: manifest.devDependencies?.bun, installed: JSON.parse(fs.readFileSync(path.join(frontend, "node_modules/bun/package.json"), "utf8")).version, observed: bunVersion.stdout.trim(), executable: bun };
  requireResult(completed(npmVersion.entry) && npmVersion.entry.exitCode === 0 && report.npm === "11.17.0", "Expected npm 11.17.0");
  requireResult(completed(bunVersion.entry) && bunVersion.entry.exitCode === 0 && report.bun.declared === "1.4.3" && report.bun.installed === "1.4.3" && report.bun.observed === "1.4.3", "Expected declared, installed and executing Bun 1.4.3");
  prerequisitesPassed = true;
  createProbe("app");
  await checkPhase("app-only-error", "app");
  removeProbe("app");
  createProbe("tests");
  await checkPhase("test-only-error", "tests");
  removeProbe("tests");
  createProbe("logic");
  await logicFailure();
} catch (failure) {
  report.failures.push(failure.stack ?? failure.message);
} finally {
  for (const name of [...owned]) try { removeProbe(name); } catch (failure) { report.failures.push(failure.message); }
  report.fixturesAbsent = Object.values(probes).every(probe => !fs.existsSync(path.join(frontend, probe.relative)));
  if (prerequisitesPassed && owned.size === 0 && report.fixturesAbsent) {
    try { await checkPhase("restored-clean"); } catch (failure) { report.failures.push(failure.message); }
  }
  report.inputHashesAfter = hashInputs();
  report.sourceAfter = sourceSnapshot();
  report.logicFilesAfter = inventory();
  report.inputsUnchanged = JSON.stringify(report.inputHashesBefore) === JSON.stringify(report.inputHashesAfter);
  report.sourceRestored = JSON.stringify(report.sourceBefore) === JSON.stringify(report.sourceAfter);
  report.inventoryRestored = JSON.stringify(report.logicFilesBefore) === JSON.stringify(report.logicFilesAfter);
  report.restored = owned.size === 0 && report.fixturesAbsent && report.sourceRestored && report.inventoryRestored;
  if (!report.inputsUnchanged || !report.restored) report.failures.push("Manifest, configuration, wrapper or source restoration did not match its original fingerprint");
  report.finishedAt = new Date().toISOString();
  report.passed = report.failures.length === 0 && report.inputsUnchanged && report.restored && ["app-only-error", "test-only-error", "logic-async-error", "restored-clean"].every(name => report.phases[name]?.passed);
  save();
  console.log(JSON.stringify({ passed: report.passed, restored: report.restored, inputsUnchanged: report.inputsUnchanged, failures: report.failures, results: path.join(output, "results.json") }, null, 2));
  if (!report.passed) process.exitCode = 1;
}
