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
const inventory = (extension = "ts") => fs.globSync(`client/src/**/*.test.${extension}`, { cwd: frontend }).map(canonical).sort();
const normalizeFile = filename => {
  if (!filename) throw new Error("Runner report omitted a test file");
  const value = filename.startsWith("file:") ? fileURLToPath(filename) : filename;
  return canonical(path.isAbsolute(value) ? path.relative(frontend, value) : value.replace(/^\.\//, ""));
};
const hashInputs = () => Object.fromEntries(["package.json", "package-lock.json", "tsconfig.json", "tsconfig.tests.json", "vitest.components.config.ts", "vitest.setup.ts", "scripts/test-lib.mjs"].map(name => [name, digest(fs.readFileSync(path.join(frontend, name)))]));
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
const componentSentinel = "scriber production component deliberate asynchronous failure";
const componentRuntime = "scriber production component worker runtime and jsdom";
const probes = {
  app: { relative: "client/src/__production_type_gate_app_probe.ts", source: 'export const productionAppTypeGateProbe: number = "deliberate app type-gate failure";\n' },
  tests: { relative: "client/src/__production_type_gate_test_probe.test.ts", source: 'export const productionTestTypeGateProbe: number = "deliberate test type-gate failure";\n' },
  logic: { relative: "client/src/lib/__integration_wrapper_async_failure.test.ts", source: `import test from "node:test";\nimport assert from "node:assert/strict";\ntest("${sentinel}", async () => { await Promise.resolve(); assert.equal(1, 2); });\n` },
  components: { relative: "client/src/lib/__production_component_gate_probe.test.tsx", source: `import { expect, test } from "vitest";\ntest("${componentRuntime}", () => { expect(process.versions.bun).toBe("1.4.3"); expect(navigator.userAgent).toContain("jsdom"); expect(document.createElement("div")).toBeInstanceOf(HTMLElement); });\ntest("${componentSentinel}", async () => { await Promise.resolve(); expect(1).toBe(2); });\n` },
};
const owned = new Set();
const report = {
  startedAt: new Date().toISOString(), platform: process.platform, architecture: process.arch,
  node: process.version, nodeExecutable: process.execPath, npmExecutable: npm, frontend,
  harnessSha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))),
  inputHashesBefore: hashInputs(), sourceBefore: sourceSnapshot(), logicFilesBefore: inventory(), componentFilesBefore: inventory("tsx"),
  environment: { GITHUB_ACTIONS: env.GITHUB_ACTIONS ?? null, GITHUB_WORKSPACE: env.GITHUB_WORKSPACE ?? null },
  processes: {}, phases: {}, failures: [], passed: false,
};
const save = () => fs.writeFileSync(path.join(output, "results.json"), `${JSON.stringify(report, null, 2)}\n`);
const requireResult = (condition, message) => { if (!condition) throw new Error(message); };
function createProbe(name) {
  fs.writeFileSync(path.join(frontend, probes[name].relative), probes[name].source, { flag: "wx" });
  owned.add(name);
}
function removeProbe(name, releaseOwnership = false) {
  if (!owned.has(name)) return;
  const probe = probes[name], filename = path.join(frontend, probe.relative);
  const wasPresent = fs.existsSync(filename);
  if (wasPresent) {
    requireResult(fs.readFileSync(filename, "utf8") === probe.source, `Refusing to remove concurrently modified fixture ${filename}`);
    fs.unlinkSync(filename);
  }
  requireResult(!fs.existsSync(filename), `Fixture remained after removal: ${filename}`);
  (report.fixtureCleanup ??= []).push({ name, final: releaseOwnership, wasPresent, absentAfter: true });
  // Retain ownership until finally, so every created path is rechecked there
  // even if a removed fixture reappears with its original bytes.
  if (releaseOwnership) owned.delete(name);
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
function typeDiagnostics(text, context = { frontend, workspace: env.GITHUB_WORKSPACE ?? frontend, platform: process.platform, githubActions: env.GITHUB_ACTIONS === "true" }) {
  const pathApi = context.platform === "win32" ? path.win32 : path.posix;
  const normalize = (filename, base) => {
    if (!filename) return "";
    const relative = pathApi.relative(context.frontend, pathApi.resolve(base, filename)).replaceAll("\\", "/");
    return context.platform === "win32" ? relative.toLowerCase() : relative;
  };
  const decode = value => value.replace(/%(25|0D|0A|2C|3A)/gi, (_, code) => ({ "25": "%", "0D": "\r", "0A": "\n", "2C": ",", "3A": ":" })[code.toUpperCase()]);
  const diagnostics = [], annotations = [], unparsed = [], summaries = [];
  for (const line of text.split(/\r?\n/)) {
    const annotation = line.match(/^::(error|warning)(?:\s+(.*?))?::(.*)$/);
    if (annotation) {
      const properties = Object.fromEntries((annotation[2] ?? "").split(",").filter(Boolean).map(property => {
        const separator = property.indexOf("=");
        return separator < 0 ? [property, ""] : [property.slice(0, separator), decode(property.slice(separator + 1))];
      }));
      const code = properties.title?.match(/^TS(\d+)$/);
      if (!code) { unparsed.push(line); continue; }
      annotations.push({ file: normalize(properties.file ?? "", context.workspace), line: Number(properties.line ?? 0), column: Number(properties.col ?? 0), category: annotation[1], code: Number(code[1]), message: decode(annotation[3]).trim(), properties });
      continue;
    }
    const located = line.match(/^(.*?)\((\d+),(\d+)\): (error|warning) TS(\d+): (.*)$/);
    const global = line.match(/^(error|warning) TS(\d+): (.*)$/);
    if (located) diagnostics.push({ file: normalize(located[1], context.frontend), line: Number(located[2]), column: Number(located[3]), category: located[4], code: Number(located[5]), message: located[6].trim() });
    else if (global) diagnostics.push({ file: "", line: 0, column: 0, category: global[1], code: Number(global[2]), message: global[3].trim() });
    else if (/\bTS\d+\b/.test(line) || /^::(?:error|warning)\b/.test(line)) unparsed.push(line);
    const summary = line.match(/^Found (\d+) errors?\b/);
    if (summary) summaries.push(Number(summary[1]));
  }
  // Keep every source diagnostic. Annotations are a second representation,
  // matched one-for-one rather than dropped or deduplicated by error code.
  const keys = entries => entries.map(({ file, line, column, category, code, message }) => JSON.stringify({ file, line, column, category, code, message })).sort();
  const annotationParity = annotations.length || context.githubActions
    ? JSON.stringify(keys(annotations)) === JSON.stringify(keys(diagnostics)) : true;
  const summaryParity = summaries.length === 0 || summaries.reduce((sum, count) => sum + count, 0) === diagnostics.filter(item => item.category === "error").length;
  return { diagnostics, annotations, unparsed, summaries, annotationParity, summaryParity, valid: unparsed.length === 0 && annotationParity && summaryParity };
}
function verifyDiagnosticParser() {
  const checks = [];
  for (const platform of ["linux", "win32"]) {
    const workspace = platform === "win32" ? "D:\\a\\Scriber\\Scriber" : "/home/runner/work/Scriber/Scriber";
    const pathApi = platform === "win32" ? path.win32 : path.posix;
    const context = { platform, workspace, frontend: pathApi.join(workspace, "candidate/Frontend"), githubActions: true };
    const message = "Type 'string' is not assignable to type 'number'.";
    const source = `${probes.app.relative}(1,14): error TS2322: ${message}`;
    const annotation = `::error file=candidate/Frontend/${probes.app.relative},line=1,col=14,endLine=1,endColumn=40,title=TS2322::${message}`;
    const scenarios = [
      { name: "matching-native-annotation", text: `${source}\n${annotation}\nFound 1 error in 1 file`, valid: true, count: 1 },
      { name: "additional-source-error", text: `${source}\n${source.replace("(1,14)", "(2,14)")}\n${annotation}\nFound 2 errors`, valid: false, count: 2 },
      { name: "additional-complete-error-pair", text: `${source}\n${source.replace("(1,14)", "(2,14)")}\n${annotation}\n${annotation.replace("line=1,", "line=2,")}\nFound 2 errors`, valid: true, count: 2 },
      { name: "additional-annotation", text: `${source}\n${annotation}\n${annotation.replace("title=TS2322", "title=TS2304")}`, valid: false, count: 1 },
      { name: "wrong-annotation-file", text: `${source}\n${annotation.replace(probes.app.relative, "client/src/another.ts")}`, valid: false, count: 1 },
      { name: "unparsed-source-error", text: `${source}\n${annotation}\nUnexpected error TS9999: additional failure`, valid: false, count: 1 },
    ];
    for (const scenario of scenarios) {
      const parsed = typeDiagnostics(scenario.text, context);
      checks.push({ platform, name: scenario.name, valid: parsed.valid, diagnosticCount: parsed.diagnostics.length, annotationCount: parsed.annotations.length, passed: parsed.valid === scenario.valid && parsed.diagnostics.length === scenario.count });
    }
  }
  return checks;
}
async function checkPhase(label, probeName = null) {
  const { entry, text } = await run(process.execPath, [npm, "run", "check"], label);
  const appPosition = text.indexOf("> bun --check --tsconfig-override tsconfig.json");
  const testsPosition = text.indexOf("> bun --check --tsconfig-override tsconfig.tests.json");
  entry.appCommandRan = appPosition >= 0;
  entry.testsCommandRan = testsPosition >= 0;
  entry.dispatchOrdered = appPosition >= 0 && testsPosition > appPosition;
  Object.assign(entry, typeDiagnostics(text));
  entry.inputHashesAfter = hashInputs();
  if (probeName) {
    entry.probe = probes[probeName];
    entry.passed = completed(entry) && entry.valid && entry.exitCode === 1 && entry.appCommandRan
      && (probeName === "tests" ? entry.dispatchOrdered : !entry.testsCommandRan)
      && entry.diagnostics.length === 1 && entry.diagnostics[0].file === canonical(probes[probeName].relative)
      && entry.diagnostics[0].category === "error" && entry.diagnostics[0].code === 2322;
  } else entry.passed = completed(entry) && entry.valid && entry.exitCode === 0 && entry.dispatchOrdered && entry.diagnostics.length === 0;
  report.phases[label] = entry;
  save();
  console.log(`${label}: exit=${entry.exitCode}, sourceDiagnostics=${entry.diagnostics.length}, annotations=${entry.annotations.length}, passed=${entry.passed}`);
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

async function componentFailure() {
  const json = path.join(output, "component-async-error.json");
  const errorsJson = path.join(output, "component-runtime-errors.json");
  const reporter = path.join(output, "component-error-reporter.mjs");
  // Vitest 4.1's built-in JSON reporter omits unhandled errors. This reporting-
  // only adapter records the public onTestRunEnd error arguments and collection
  // errors; the actual npm script, worker pool and test configuration stay intact.
  fs.writeFileSync(reporter, `import { writeFileSync } from "node:fs";
export default class {
  onTestRunEnd(modules, unhandledErrors, reason) {
    const errors = values => values.map(error => ({ name: error.name ?? null, message: String(error.message ?? error), stack: error.stack ?? null }));
    const collectionErrors = modules.flatMap(module => [
      { file: module.moduleId, suite: null, errors: errors(module.errors()) },
      ...[...module.children.allSuites()].map(suite => ({ file: module.moduleId, suite: suite.fullName, errors: errors(suite.errors()) })),
    ]).filter(entry => entry.errors.length);
    writeFileSync(${JSON.stringify(errorsJson)}, JSON.stringify({ reason, bun: process.versions.bun ?? null, modules: modules.map(module => module.moduleId), unhandledErrors: errors(unhandledErrors), collectionErrors }, null, 2) + "\\n", { flag: "wx" });
  }
}
`, { flag: "wx" });
  const { entry, text } = await run(process.execPath, [npm, "run", "test:components", "--", "--reporter=json", "--reporter", reporter, "--outputFile", json], "component-async-error", 120000);
  report.phases["component-async-error"] = entry;
  Object.assign(entry, { probe: probes.components, json: path.basename(json), runtimeErrorsJson: path.basename(errorsJson), reporterSha256: digest(fs.readFileSync(reporter)), passed: false });
  save();
  requireResult(completed(entry) && entry.exitCode === 1 && fs.existsSync(json) && fs.existsSync(errorsJson), "The component failure must return exit 1 with complete JSON and runtime-error reports");
  const data = JSON.parse(fs.readFileSync(json, "utf8"));
  const runtimeErrors = JSON.parse(fs.readFileSync(errorsJson, "utf8"));
  requireResult(Array.isArray(data.testResults) && data.testResults.every(file => Array.isArray(file.assertionResults)), "Malformed component JSON report");
  const cases = data.testResults.flatMap(file => file.assertionResults.map(test => ({ ...test, file: normalizeFile(file.name) })));
  const files = data.testResults.map(file => normalizeFile(file.name)).sort();
  const expectedFiles = [...report.componentFilesBefore, canonical(probes.components.relative)].sort();
  const failed = cases.filter(test => test.status === "failed");
  const runtimeCases = cases.filter(test => test.file === canonical(probes.components.relative) && test.fullName === componentRuntime);
  entry.jsonSha256 = digest(fs.readFileSync(json));
  entry.files = files;
  entry.tests = cases.length;
  entry.passes = cases.filter(test => test.status === "passed").length;
  entry.failures = failed.length;
  entry.skippedOrOtherStatuses = cases.filter(test => !["passed", "failed"].includes(test.status)).length;
  entry.summary = Object.fromEntries(["success", "numTotalTests", "numPassedTests", "numFailedTests", "numPendingTests", "numTodoTests", "numFailedTestSuites", "numPendingTestSuites"].map(key => [key, data[key]]));
  entry.completeInventory = JSON.stringify(files) === JSON.stringify(expectedFiles);
  entry.onlyIntentionalFailure = failed.length === 1 && failed[0].file === canonical(probes.components.relative) && failed[0].fullName === componentSentinel && failed[0].failureMessages?.length === 1;
  entry.runtimeProofPassed = runtimeCases.length === 1 && runtimeCases[0].status === "passed" && (runtimeCases[0].failureMessages?.length ?? 0) === 0;
  entry.noAdditionalAssertionErrors = cases.filter(test => test.status === "passed").every(test => (test.failureMessages?.length ?? 0) === 0);
  entry.fileStatusesMatch = data.testResults.every(file => file.status === (normalizeFile(file.name) === canonical(probes.components.relative) ? "failed" : "passed") && file.message === "");
  entry.runtimeErrors = runtimeErrors;
  entry.unexpectedRuntimeLogLines = text.split(/\r?\n/).filter(line => /Vitest caught \d+ unhandled errors?|Unhandled Errors|Unhandled Rejection|Uncaught Exception|^\s*Errors\s+[1-9]\d* errors?\b/i.test(line));
  entry.noAdditionalRuntimeErrors = runtimeErrors.reason === "failed" && runtimeErrors.bun === "1.4.3"
    && Array.isArray(runtimeErrors.unhandledErrors) && runtimeErrors.unhandledErrors.length === 0
    && Array.isArray(runtimeErrors.collectionErrors) && runtimeErrors.collectionErrors.length === 0
    && Array.isArray(runtimeErrors.modules) && JSON.stringify(runtimeErrors.modules.map(normalizeFile).sort()) === JSON.stringify(expectedFiles)
    && entry.unexpectedRuntimeLogLines.length === 0;
  entry.componentCommandRan = text.includes(`> ${report.scripts["test:components"]}`);
  entry.passed = data.success === false && data.numTotalTests === 255 && data.numPassedTests === 254 && data.numFailedTests === 1
    && data.numPendingTests === 0 && data.numTodoTests === 0 && data.numFailedTestSuites === 1 && data.numPendingTestSuites === 0 && data.snapshot?.failure === false
    && entry.tests === 255 && files.length === 50 && entry.passes === 254 && entry.failures === 1 && entry.skippedOrOtherStatuses === 0
    && entry.completeInventory && entry.onlyIntentionalFailure && entry.runtimeProofPassed && entry.noAdditionalAssertionErrors
    && entry.fileStatusesMatch && entry.noAdditionalRuntimeErrors && entry.componentCommandRan;
  save();
  console.log(`component-async-error: tests=${entry.tests}, files=${files.length}, passes=${entry.passes}, failures=${entry.failures}, runtimeProof=${entry.runtimeProofPassed}, passed=${entry.passed}`);
  requireResult(entry.passed, "The component control did not retain all 253 original passes, the Bun/jsdom runtime proof and only the deliberate async failure");
}

let prerequisitesPassed = false;
try {
  requireResult(process.version === "v26.5.0", `Expected Node v26.5.0, got ${process.version}`);
  for (const probe of Object.values(probes)) requireResult(!fs.existsSync(path.join(frontend, probe.relative)), `Preexisting fixture path: ${probe.relative}`);
  requireResult(report.logicFilesBefore.length === 35, "Expected the pristine 35-file logic inventory");
  requireResult(report.componentFilesBefore.length === 49, "Expected the pristine 49-file component inventory");
  report.diagnosticParserChecks = verifyDiagnosticParser();
  requireResult(report.diagnosticParserChecks.every(check => check.passed), "Source/annotation parser regression control failed");
  const manifest = JSON.parse(fs.readFileSync(path.join(frontend, "package.json"), "utf8"));
  report.scripts = manifest.scripts;
  const expectedScripts = { check: "npm run check:app && npm run check:tests", "check:app": "bun --check --tsconfig-override tsconfig.json", "check:tests": "bun --check --tsconfig-override tsconfig.tests.json", "test:lib": "node scripts/test-lib.mjs", "test:components": "bun ./node_modules/vitest/vitest.mjs run --config vitest.components.config.ts" };
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
  removeProbe("logic");
  createProbe("components");
  await componentFailure();
} catch (failure) {
  report.failures.push(failure.stack ?? failure.message);
} finally {
  for (const name of [...owned]) try { removeProbe(name, true); } catch (failure) { report.failures.push(failure.message); }
  report.fixturesAbsent = Object.values(probes).every(probe => !fs.existsSync(path.join(frontend, probe.relative)));
  if (prerequisitesPassed && owned.size === 0 && report.fixturesAbsent) {
    try { await checkPhase("restored-clean"); } catch (failure) { report.failures.push(failure.message); }
  }
  report.inputHashesAfter = hashInputs();
  report.sourceAfter = sourceSnapshot();
  report.logicFilesAfter = inventory();
  report.componentFilesAfter = inventory("tsx");
  report.inputsUnchanged = JSON.stringify(report.inputHashesBefore) === JSON.stringify(report.inputHashesAfter);
  report.sourceRestored = JSON.stringify(report.sourceBefore) === JSON.stringify(report.sourceAfter);
  report.inventoryRestored = JSON.stringify(report.logicFilesBefore) === JSON.stringify(report.logicFilesAfter) && JSON.stringify(report.componentFilesBefore) === JSON.stringify(report.componentFilesAfter);
  report.restored = owned.size === 0 && report.fixturesAbsent && report.sourceRestored && report.inventoryRestored;
  if (!report.inputsUnchanged || !report.restored) report.failures.push("Manifest, configuration, wrapper or source restoration did not match its original fingerprint");
  report.finishedAt = new Date().toISOString();
  report.passed = report.failures.length === 0 && report.inputsUnchanged && report.restored && ["app-only-error", "test-only-error", "logic-async-error", "component-async-error", "restored-clean"].every(name => report.phases[name]?.passed);
  save();
  console.log(JSON.stringify({ passed: report.passed, restored: report.restored, inputsUnchanged: report.inputsUnchanged, failures: report.failures, results: path.join(output, "results.json") }, null, 2));
  if (!report.passed) process.exitCode = 1;
}
