/** Compare the actual, complete npm scripts in two prepared Frontend copies.
 * Installation, reporting-only test preflights, cache preparation, validation
 * and source restoration are outside the measured check/lint/test/build clock.
 */
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { host, options } from "./common.mjs";

const input = options({ baseline: "", candidate: "", npm: "", output: "", rounds: "5", scenarios: "cold,warm,changed" });
for (const key of ["baseline", "candidate", "npm", "output"]) {
  if (!path.isAbsolute(input[key])) throw new Error(`--${key} must be an absolute path`);
}
const projects = Object.fromEntries(["baseline", "candidate"].map(name => [name, fs.realpathSync(input[name])]));
const npm = fs.realpathSync(input.npm);
const output = path.resolve(input.output);
const rounds = Number(input.rounds);
const scenarios = input.scenarios.split(",");
if (process.version !== "v26.5.0") throw new Error(`Expected Node v26.5.0, got ${process.version}`);
if (!Number.isInteger(rounds) || rounds < 5 || rounds > 15) throw new Error("Use 5 to 15 rounds");
if (!scenarios.length || new Set(scenarios).size !== scenarios.length || scenarios.some(value => !["cold", "warm", "changed"].includes(value))) throw new Error("Invalid scenarios");
if (projects.baseline === projects.candidate) throw new Error("Use two independent Frontend copies");
for (const directory of Object.values(projects)) {
  const relative = path.relative(directory, output);
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))) throw new Error("Results must be outside both Frontend copies");
}
if (fs.existsSync(path.join(output, "results.json"))) throw new Error("Use a fresh output directory; combined runs cannot be resumed");
const npmVersion = JSON.parse(fs.readFileSync(path.resolve(path.dirname(npm), "../package.json"), "utf8")).version;
if (npmVersion !== "11.17.0") throw new Error(`Expected the Node 26.5.0 baseline npm 11.17.0, got ${npmVersion}`);
fs.mkdirSync(path.join(output, "logs"), { recursive: true });
fs.mkdirSync(path.join(output, "evidence"), { recursive: true });

const digest = value => createHash("sha256").update(value).digest("hex");
const jsonHash = value => digest(JSON.stringify(value));
const slash = value => value.replaceAll("\\", "/");
const canonical = value => process.platform === "win32" ? slash(value).toLowerCase() : slash(value);
const ignored = new Set(["node_modules", "dist", "output", "src-tauri", ".git", ".cache", ".eslintcache"]);
function fingerprint(directory, source = false) {
  const files = [];
  function visit(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      if (source && (ignored.has(entry.name) || entry.name.endsWith(".tsbuildinfo"))) continue;
      const filename = path.join(current, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) {
        const bytes = fs.readFileSync(filename);
        files.push({ file: slash(path.relative(directory, filename)), size: bytes.length, sha256: digest(bytes) });
      } else throw new Error(`Unexpected symbolic link or special file in fingerprint: ${filename}`);
    }
  }
  visit(directory);
  files.sort((left, right) => left.file < right.file ? -1 : left.file > right.file ? 1 : 0);
  return { files: files.length, bytes: files.reduce((sum, entry) => sum + entry.size, 0), sha256: jsonHash(files), manifest: files };
}
const scripts = {};
const originalUtils = {};
const sourceFingerprints = {};
const expectedFiles = {};
const env = {
  ...process.env,
  PATH: [path.dirname(process.execPath), path.resolve(path.dirname(npm), "../../.bin"), process.env.PATH ?? ""].join(path.delimiter),
  CI: "true", NO_COLOR: "1", FORCE_COLOR: "0", npm_config_update_notifier: "false",
};
const report = {
  metadata: {
    ...host(), rssScope: "not measured by the combined comparator", startedAt: new Date().toISOString(), npm: npmVersion, nodeExecutable: process.execPath,
    npmExecutable: npm, harnessSha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))),
    rounds, scenarios,
    policy: "Actual npm run check, lint, test and build execute sequentially under the same Node/npm. Total and stage wall clocks are monotonic. Setup/install, reporting-only full test preflights, cache preparation, log validation, manifest hashing and source restoration are excluded. Cold clears compiler/ESLint caches only; OS pages, test transforms and network caches are not flushed. Warm/changed start with empty compiler/ESLint caches, then prime check+lint on original source outside timing. Changed adds the same valid public export to real client/src/lib/utils.ts. Every build starts without dist/public. Any failure invalidates the comparison.",
    runOrder: "Each scenario alternates baseline,candidate and candidate,baseline by round (pure cyclic rotation; five rounds give first position three versus two times).",
    testEvidence: "Untimed reporting-only executions establish complete test names/files. Every timed actual npm run test must retain the full Node/Bun logic and Vitest component counts; immutable source/config fingerprints prevent scope changes between preflight and timing.",
    environment: { CI: env.CI, NO_COLOR: env.NO_COLOR, FORCE_COLOR: env.FORCE_COLOR, pathSha256: digest(env.PATH), nodeOptionsSha256: digest(env.NODE_OPTIONS ?? ""), TZ: env.TZ ?? null, LANG: env.LANG ?? null },
  },
  projects: {}, expectedFiles, preflight: [], preparation: [], samples: [], summary: {}, comparisons: {}, passed: false,
};
const save = () => fs.writeFileSync(path.join(output, "results.json"), `${JSON.stringify(report, null, 2)}\n`);
const utilsPath = name => path.join(projects[name], "client/src/lib/utils.ts");
for (const [name, directory] of Object.entries(projects)) {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, "package.json"), "utf8"));
  scripts[name] = manifest.scripts;
  for (const script of ["check", "lint", "test", "build", "test:lib", "test:components"]) if (!scripts[name]?.[script]) throw new Error(`Missing ${name} script ${script}`);
  if (scripts[name].test !== "npm run test:lib && npm run test:components") throw new Error(`Unexpected ${name} complete-test orchestration`);
  originalUtils[name] = fs.readFileSync(utilsPath(name));
  if (originalUtils[name].includes("scriberCombinedPipelineProbe")) throw new Error("The source already contains the changed-file probe");
  sourceFingerprints[name] = fingerprint(directory, true);
  const testFiles = Object.fromEntries([["logic", "ts"], ["components", "tsx"]].map(([group, extension]) => [group, fs.globSync(`client/src/**/*.test.${extension}`, { cwd: directory }).map(canonical).sort()]));
  if (testFiles.logic.length !== 35 || testFiles.components.length !== 49) throw new Error(`Incomplete ${name} test-file scope`);
  if (name === "baseline") Object.assign(expectedFiles, testFiles);
  else if (JSON.stringify(testFiles) !== JSON.stringify(expectedFiles)) throw new Error("Different test-file scope between copies");
  const git = spawnSync("git", ["rev-parse", "HEAD"], { cwd: directory, encoding: "utf8" });
  report.projects[name] = {
    directory, scripts: scripts[name], gitHead: git.status === 0 ? git.stdout.trim() : null,
    source: sourceFingerprints[name], client: fingerprint(path.join(directory, "client")),
    packageLockSha256: digest(fs.readFileSync(path.join(directory, "package-lock.json"))),
    versions: Object.fromEntries(["typescript", "bun", "tsx", "vitest", "eslint", "vite"].map(packageName => {
      const filename = path.join(directory, "node_modules", packageName, "package.json");
      return [packageName, fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, "utf8")).version : null];
    })),
  };
}
if (report.projects.baseline.client.sha256 !== report.projects.candidate.client.sha256) throw new Error("Application/test source bytes differ before the comparison");
if (scripts.baseline.build !== scripts.candidate.build || scripts.baseline["build:webview"] !== scripts.candidate["build:webview"]) throw new Error("The byte-identical Node build comparison requires the unchanged build scripts");
save();

// No shell is used to start npm. Its actual package scripts still use npm's
// normal native shell, with the pinned Node and npm first in PATH.
async function execute(name, script, fd, extra = [], environment = env) {
  const args = [npm, "run", script, ...(extra.length ? ["--", ...extra] : [])];
  const started = performance.now();
  let child, timer, error = null, timedOut = false;
  let status = null, signal = null;
  const terminateTree = () => {
    if (!child?.pid) return;
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", timeout: 10000 });
    else { try { process.kill(-child.pid, "SIGKILL"); } catch {} }
  };
  try {
    child = spawn(process.execPath, args, { cwd: projects[name], env: environment, stdio: ["ignore", fd, fd], detached: process.platform !== "win32", windowsHide: true });
    timer = setTimeout(() => { timedOut = true; terminateTree(); }, 600000);
    await new Promise(resolve => {
      child.on("error", failure => { error = failure.message; });
      child.on("close", (code, termination) => { status = code; signal = termination; resolve(); });
    });
  } catch (failure) { error = failure.message; terminateTree(); }
  finally { clearTimeout(timer); }
  const seconds = (performance.now() - started) / 1000;
  return { script, command: process.execPath, args, seconds, status, signal, error, timedOut, passed: status === 0 && !signal && !error && !timedOut };
}
async function standalone(name, script, label, extra = [], environment = env) {
  const log = `logs/${label}.log`;
  const fd = fs.openSync(path.join(output, log), "w");
  try { return { ...await execute(name, script, fd, extra, environment), name, log }; }
  finally { fs.closeSync(fd); }
}
const normalizeFile = (name, filename) => {
  if (!filename) throw new Error("Runner report omitted a test file");
  const value = filename.startsWith("file:") ? fileURLToPath(filename) : filename;
  return canonical(path.isAbsolute(value) ? path.relative(projects[name], value) : value.replace(/^\.\//, ""));
};
const xmlDecode = value => value.replace(/&(quot|apos|lt|gt|amp|#\d+|#x[\da-f]+);/gi, (_, entity) => entity[0] === "#" ? String.fromCodePoint(entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : Number(entity.slice(1))) : ({ quot: '"', apos: "'", lt: "<", gt: ">", amp: "&" })[entity]);
function inventory(name, group, tests) {
  const count = group === "logic" ? 142 : 253;
  if (tests.length !== count || tests.some(test => test.status !== "passed" || test.skipped || typeof test.name !== "string")) throw new Error(`Incomplete ${name}/${group} test names/status`);
  const entries = tests.map(test => ({ file: normalizeFile(name, test.file), name: test.name })).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  const files = [...new Set(entries.map(test => test.file))].sort();
  if (JSON.stringify(files) !== JSON.stringify(expectedFiles[group])) throw new Error(`Incomplete ${name}/${group} test-file coverage`);
  return { tests: entries.length, files: files.length, sha256: jsonHash(entries), entries };
}
const nodeReporter = path.join(output, "evidence/node-inventory-reporter.mjs");
fs.writeFileSync(nodeReporter, `import { writeFileSync } from "node:fs";
export default async function* reporter(events) {
  const tests = [];
  for await (const event of events) {
    if ((event.type === "test:pass" || event.type === "test:fail") && event.data.details?.type !== "suite") {
      tests.push({ name: event.data.name, file: event.data.file, status: event.type === "test:pass" ? "passed" : "failed", skipped: Boolean(event.data.skip || event.data.todo) });
    }
  }
  const text = JSON.stringify({ tests });
  writeFileSync(process.env.SCRIBER_COMBINED_TEST_REPORT, text);
  yield text + "\\n";
}
`);
const inventories = {};
async function preflight(name) {
  const logicScript = scripts[name]["test:lib"];
  const bunWrapper = logicScript === "node scripts/test-lib.mjs";
  if (!bunWrapper && !/^tsx\s+--test\s/.test(logicScript)) throw new Error(`Unrecognized ${name} logic-runner reporting route: ${logicScript}`);
  const logicReport = path.join(output, `evidence/${name}-logic.${bunWrapper ? "xml" : "json"}`);
  let extra = [], environment = env;
  if (bunWrapper) extra = ["--reporter=junit", "--reporter-outfile", logicReport];
  else {
    if (!process.allowedNodeEnvironmentFlags.has("--test-reporter")) throw new Error("Node disallows the reporting-only NODE_OPTIONS flag");
    environment = { ...env, NODE_OPTIONS: `${env.NODE_OPTIONS ?? ""} --test-reporter=${pathToFileURL(nodeReporter).href}`.trim(), SCRIBER_COMBINED_TEST_REPORT: logicReport };
  }
  const logic = await standalone(name, "test:lib", `${name}-preflight-logic`, extra, environment);
  logic.reporting = bunWrapper ? "Existing Bun wrapper forwards reporting-only arguments before explicit files" : "Actual tsx npm script; reporting-only custom reporter supplied through allowed NODE_OPTIONS, avoiding flags after the glob";
  report.preflight.push(logic); save();
  if (!logic.passed || !fs.existsSync(logicReport)) throw new Error(`Failed ${name} complete logic preflight/report`);
  let logicTests;
  if (bunWrapper) {
    const xml = fs.readFileSync(logicReport, "utf8");
    if (/<(?:failure|error|skipped)\b/.test(xml)) throw new Error(`Failed/skipped ${name} Bun test`);
    logicTests = [...xml.matchAll(/<testcase\b([^>]*?)(?:\/>|>)/g)].map(match => {
      const attributes = Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(item => [item[1], xmlDecode(item[2])]));
      return { file: attributes.file, name: attributes.name, status: "passed", skipped: false };
    });
  } else logicTests = JSON.parse(fs.readFileSync(logicReport, "utf8")).tests;
  const componentReport = path.join(output, `evidence/${name}-components.json`);
  const components = await standalone(name, "test:components", `${name}-preflight-components`, ["--reporter=json", "--outputFile", componentReport]);
  report.preflight.push(components); save();
  if (!components.passed || !fs.existsSync(componentReport)) throw new Error(`Failed ${name} complete component preflight/report`);
  const data = JSON.parse(fs.readFileSync(componentReport, "utf8"));
  if (!data.success || data.numFailedTests || data.numPendingTests || data.numTodoTests || data.numTotalTests !== 253 || data.testResults.some(file => file.status !== "passed")) throw new Error(`Failed/incomplete ${name} Vitest report`);
  inventories[name] = {
    logic: inventory(name, "logic", logicTests),
    components: inventory(name, "components", data.testResults.flatMap(file => file.assertionResults.map(test => ({ file: file.name, name: test.fullName, status: test.status, skipped: false })))),
  };
  report.projects[name].testInventories = inventories[name]; save();
}
function validateTimedTests(name, text) {
  text = text.replaceAll(/\x1b\[[0-9;]*m/g, "").replaceAll("\r\n", "\n");
  const marker = text.match(/^> \S+@\S+ test:components(?:\s[^\n]*)?$/m);
  if (!marker) throw new Error("Timed npm test did not invoke test:components");
  const logic = text.slice(0, marker.index), components = text.slice(marker.index);
  if (scripts[name]["test:lib"] === "node scripts/test-lib.mjs") {
    const runs = [...logic.matchAll(/Ran\s+(\d+)\s+tests?\s+across\s+(\d+)\s+files?\./g)];
    if (runs.length !== 1 || Number(runs[0][1]) !== 142 || Number(runs[0][2]) !== 35 || !/^\s*142 pass\s*$/m.test(logic) || !/^\s*0 fail\s*$/m.test(logic) || /^\s*[1-9]\d* (?:skip|todo|cancelled)\b/m.test(logic)) throw new Error("Incomplete timed Bun logic counts");
  } else {
    for (const [key, expected] of Object.entries({ tests: 142, pass: 142, fail: 0, cancelled: 0, skipped: 0, todo: 0 })) {
      const matches = [...logic.matchAll(new RegExp(`^(?:#|ℹ)\\s*${key}\\s+(\\d+)\\s*$`, "gm"))];
      if (matches.length !== 1 || Number(matches[0][1]) !== expected) throw new Error(`Incomplete timed Node logic ${key}`);
    }
  }
  if (!/^\s*Test Files\s+49 passed\s+\(49\)\s*$/m.test(components) || !/^\s*Tests\s+253 passed\s+\(253\)\s*$/m.test(components)) throw new Error("Incomplete timed Vitest counts");
  return { logic: { files: 35, tests: 142 }, components: { files: 49, tests: 253 }, fullNamesPreflightSha256: jsonHash(inventories[name]) };
}
const cachePaths = [
  "node_modules/typescript/tsbuildinfo", "node_modules/typescript/tsbuildinfo.tests",
  "node_modules/.cache/typescript6", "node_modules/.cache/eslint",
  ".cache/typescript6", ".cache/eslint", ".eslintcache",
];
async function prepare(name, scenario, round) {
  if (fingerprint(projects[name], true).sha256 !== sourceFingerprints[name].sha256) throw new Error(`Changed ${name} source/config after preflight`);
  const removed = cachePaths.filter(relative => fs.existsSync(path.join(projects[name], relative)));
  for (const relative of cachePaths) fs.rmSync(path.join(projects[name], relative), { recursive: true, force: true });
  fs.rmSync(path.join(projects[name], "dist/public"), { recursive: true, force: true });
  const entry = { name, scenario, round, clearedCaches: removed, buildOutputRemoved: true, commands: [] };
  report.preparation.push(entry);
  if (scenario !== "cold") for (const stage of ["check", "lint"]) {
    const primed = await standalone(name, stage, `${name}-${scenario}-${round}-prime-${stage}`);
    entry.commands.push(primed); save();
    if (!primed.passed) throw new Error(`Failed ${name}/${scenario} ${stage} cache prime`);
  }
  if (scenario === "changed") {
    const eol = originalUtils[name].includes("\r\n") ? "\r\n" : "\n";
    const addition = `${eol}export const scriberCombinedPipelineProbe = { revision: 1 } as const;${eol}`;
    fs.writeFileSync(utilsPath(name), Buffer.concat([originalUtils[name], Buffer.from(addition)]));
    entry.changedFile = "client/src/lib/utils.ts";
    entry.changedSha256 = digest(fs.readFileSync(utilsPath(name)));
  }
  save();
}
async function pipeline(name, scenario, round, position) {
  const label = `${scenario}-${round}-${name}`;
  const destinations = ["check", "lint", "test", "build"].map(script => {
    const log = `logs/${label}-${script}.log`;
    return { script, log, fd: fs.openSync(path.join(output, log), "w") };
  });
  const sample = { name, scenario, round, position, stages: [], passed: false };
  const started = performance.now();
  try {
    for (const destination of destinations) {
      const record = await execute(name, destination.script, destination.fd);
      sample.stages.push({ ...record, log: destination.log });
      if (!record.passed) break;
    }
  } finally {
    sample.seconds = (performance.now() - started) / 1000;
    for (const destination of destinations) fs.closeSync(destination.fd);
  }
  const failed = sample.stages.find(stage => !stage.passed);
  Object.assign(sample, { status: failed ? failed.status : sample.stages.length === 4 ? 0 : null, signal: failed?.signal ?? null, error: failed?.error ?? null, timedOut: failed?.timedOut ?? false });
  try {
    if (failed || sample.stages.length !== 4) throw new Error(`Failed actual ${name} pipeline at ${failed?.script ?? "missing stage"}`);
    sample.testEvidence = validateTimedTests(name, fs.readFileSync(path.join(output, sample.stages.find(stage => stage.script === "test").log), "utf8"));
    const built = fingerprint(path.join(projects[name], "dist/public"));
    if (!built.manifest.some(file => file.file === "index.html")) throw new Error("Node build omitted index.html");
    const manifest = `evidence/${label}-build-manifest.json`;
    fs.writeFileSync(path.join(output, manifest), `${JSON.stringify(built.manifest, null, 2)}\n`);
    sample.build = { files: built.files, bytes: built.bytes, sha256: built.sha256, manifest };
    sample.passed = true;
  } catch (failure) { sample.validationError = failure.message; }
  report.samples.push(sample); save();
  console.log(`${label}: ${sample.passed ? "PASS" : "FAIL"} ${sample.seconds.toFixed(3)}s`);
  if (!sample.passed) throw new Error(sample.validationError);
  return sample;
}
function summarize(samples, stage = null) {
  const values = samples.map(sample => stage ? sample.stages.find(value => value.script === stage) : sample);
  const valid = report.passed && values.length === rounds && values.every(value => value?.passed);
  const sorted = values.filter(value => value?.passed).map(value => value.seconds).sort((a, b) => a - b);
  return { attempts: values.length, passed: values.filter(value => value?.passed).length, eligible: valid, medianSeconds: valid ? (sorted[Math.floor((rounds - 1) / 2)] + sorted[Math.ceil((rounds - 1) / 2)]) / 2 : null, minSeconds: valid ? sorted[0] : null, maxSeconds: valid ? sorted.at(-1) : null };
}
try {
  for (const name of ["baseline", "candidate"]) await preflight(name);
  for (const group of ["logic", "components"]) if (JSON.stringify(inventories.baseline[group].entries) !== JSON.stringify(inventories.candidate[group].entries)) throw new Error(`Different complete ${group} test names`);
  for (let round = 0; round < rounds; round++) for (const scenario of scenarios) {
    const names = round % 2 ? ["candidate", "baseline"] : ["baseline", "candidate"];
    const pair = [];
    for (const [position, name] of names.entries()) {
      try {
        await prepare(name, scenario, round);
        pair.push(await pipeline(name, scenario, round, position));
      } finally { fs.writeFileSync(utilsPath(name), originalUtils[name]); }
      if (fingerprint(projects[name], true).sha256 !== sourceFingerprints[name].sha256) throw new Error(`Pipeline altered ${name} source/config`);
    }
    if (pair[0].build.sha256 !== pair[1].build.sha256) {
      for (const sample of pair) { sample.passed = false; sample.validationError = "Baseline and candidate Node build manifests are not byte-identical"; }
      throw new Error(`Different Node build artifacts for ${scenario}/${round}`);
    }
    save();
  }
  report.passed = report.samples.length === rounds * scenarios.length * 2 && report.samples.every(sample => sample.passed);
} catch (failure) {
  report.fatalError = failure.stack ?? failure.message;
  process.exitCode = 1;
} finally {
  report.restoration = {};
  for (const name of ["baseline", "candidate"]) {
    try {
      fs.writeFileSync(utilsPath(name), originalUtils[name]);
      report.restoration[name] = fingerprint(projects[name], true).sha256 === sourceFingerprints[name].sha256;
      if (!report.restoration[name]) throw new Error(`Failed to restore ${name} source/config`);
    } catch (failure) { report.restoration[name] = false; report.restorationError = failure.message; report.passed = false; process.exitCode = 1; }
  }
  for (const scenario of scenarios) {
    for (const name of ["baseline", "candidate"]) {
      const samples = report.samples.filter(sample => sample.name === name && sample.scenario === scenario);
      report.summary[`${name}/${scenario}`] = { total: summarize(samples), stages: Object.fromEntries(["check", "lint", "test", "build"].map(stage => [stage, summarize(samples, stage)])) };
    }
    const baseline = report.summary[`baseline/${scenario}`].total.medianSeconds;
    const candidate = report.summary[`candidate/${scenario}`].total.medianSeconds;
    report.comparisons[scenario] = { baselineSeconds: baseline, candidateSeconds: candidate, candidateOverBaseline: baseline && candidate ? candidate / baseline : null, savedPercent: baseline && candidate ? 100 * (1 - candidate / baseline) : null };
  }
  report.metadata.finishedAt = new Date().toISOString();
  save();
  console.log(JSON.stringify({ passed: report.passed, comparisons: report.comparisons, fatalError: report.fatalError ?? null }));
}
