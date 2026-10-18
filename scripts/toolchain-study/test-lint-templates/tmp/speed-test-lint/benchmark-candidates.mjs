import { closeSync, cpSync, existsSync, globSync, mkdirSync, openSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { performance } from "node:perf_hooks";

// Run groups serially on one otherwise idle machine. All samples include the
// same launcher overhead. Correctness/report validation is outside each timer.
const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const harness = path.join(import.meta.dirname, "run-candidate.mjs");
const options = Object.fromEntries(process.argv.slice(2).map((argument) => {
  const [key, ...value] = argument.replace(/^--/, "").split("=");
  return [key, value.join("=")];
}));
const rounds = Number(options.rounds ?? 5);
if (!Number.isInteger(rounds) || rounds < 5) throw new Error("At least five timed rounds are required");
const groups = (options.groups ?? "logic,components,lint,build").split(",");
const output = path.resolve(options.output ?? path.join(import.meta.dirname, `results-${process.platform}`));
mkdirSync(output, { recursive: true });
const bun = process.env.SCRIBER_BUN_EXE ?? "bun";
const git = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" });
if (git.error || git.signal || git.status !== 0) throw new Error("Unable to identify the source revision");
const bunVersion = spawnSync(bun, ["--version"], { encoding: "utf8" });
const expectedFiles = {
  logic: globSync("client/src/**/*.test.ts", { cwd: frontend }).map((file) => file.replaceAll("\\", "/")).sort(),
  components: globSync("client/src/**/*.test.tsx", { cwd: frontend }).map((file) => file.replaceAll("\\", "/")).sort(),
};
const counts = { logic: 142, components: 253 };
if (expectedFiles.logic.length !== 35 || expectedFiles.components.length !== 49) {
  throw new Error("The frozen study requires all 35 logic and 49 component test files");
}
const report = {
  metadata: {
    revision: git.stdout.trim(), sourceRevision: process.env.SCRIBER_SOURCE_REVISION ?? git.stdout.trim(),
    node: process.version, bun: bunVersion.error ? null : bunVersion.stdout.trim(),
    platform: process.platform, arch: process.arch, availableParallelism: os.availableParallelism(),
    startedAt: new Date().toISOString(), rounds,
    rssMetric: process.env.SCRIBER_TIME_EXE ? "Linux wait4 per-command maximum RSS (KiB), not simultaneous process-tree RSS" : null,
    cacheSemantics: "cold = empty ESLint cache, not cold OS pages; warm = populated unchanged content; edited = one valid changed file after priming the original content",
    selection: "One full screening run, then at least five rounds for baseline, best Node, best Bun and every passing candidate within 15% of fastest; each round rotates the candidate order by one position; lint retains all four controls across separate cache lanes",
    runOrder: "Cyclic rotation by one position per round; positions are completely balanced only across a multiple of the candidate count",
  },
  expectedFiles, baselineInventories: {}, screening: [], selections: {}, samples: [], summaries: [], disqualifications: [],
};
const checkpoint = () => writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2) + "\n");
const readParity = (name) => {
  const filename = path.join(import.meta.dirname, `${name}-parity-results.json`);
  try {
    const records = JSON.parse(readFileSync(filename, "utf8"));
    if (!Array.isArray(records)) throw new Error("Expected an array of parity cases");
    return records;
  } catch (error) {
    report.disqualifications.push({ scope: name, reason: `Parity evidence unavailable: ${error.message}` });
    return [];
  }
};
const runnerParity = readParity("runner");
const lintParity = readParity("lint");
const cacheParity = readParity("cache");
const parallelFailureParity = readParity("parallel-failure");
report.parity = {
  runner: { cases: runnerParity.length, passed: runnerParity.filter((entry) => entry.pass).length },
  lint: { cases: lintParity.length, passed: lintParity.filter((entry) => entry.pass).length },
  cache: { cases: cacheParity.length, passed: cacheParity.filter((entry) => entry.pass).length },
  parallelFailure: { cases: parallelFailureParity.length, passed: parallelFailureParity.filter((entry) => entry.pass).length },
};
const runnerPassed = (...ids) => ids.every((id) => runnerParity.filter((entry) => entry.id === id).length === 1 && runnerParity.find((entry) => entry.id === id).pass);
const cachePassed = (candidate) => {
  const stages = ["cold-clean", "warm-clean", "changed-content-same-size-and-mtime", "fixed-content", "changed-config", "restored-config"];
  return stages.every((stage) => cacheParity.filter((entry) => entry.candidate === candidate && entry.stage === stage).length === 1 && cacheParity.find((entry) => entry.candidate === candidate && entry.stage === stage).pass);
};
function parityRejection(candidate) {
  if (candidate === "lib-node" && !runnerPassed("node-isolation", "node-complete-suite-failure")) return "Node isolation/failure-propagation parity did not pass";
  if (candidate.startsWith("lib-bun")) {
    if (!runnerPassed("bun-isolation", "bun-complete-suite-failure")) return "Bun isolation/failure-propagation parity did not pass";
    if (candidate.includes("parallel") && !runnerPassed("bun-parallel-isolation")) return "Bun parallel isolation parity did not pass";
    if (candidate.includes("parallel") && !(parallelFailureParity.filter((entry) => entry.candidate === candidate).length === 1 && parallelFailureParity.find((entry) => entry.candidate === candidate).pass)) return "The complete-suite asynchronous failure probe did not pass at this Bun parallel worker count";
  }
  if (candidate.startsWith("components-")) {
    const family = candidate === "components-original" ? "components-node-forks" : candidate.split("-").slice(0, 3).join("-");
    if (!runnerPassed(`${family}-2-worker-runtime`, `${family}-2-assertion-failure`)) return "Actual worker runtime/jsdom/failure-propagation parity did not pass";
  }
  if (candidate.startsWith("lint-oxlint") && !(lintParity.length === 29 && new Set(lintParity.map((entry) => entry.id)).size === 29 && lintParity.every((entry) => entry.pass))) return "The complete lint rule-parity corpus did not pass";
  if (candidate.startsWith("lint-") && candidate.endsWith("-cached") && !cachePassed(candidate)) return "Content/configuration cache invalidation parity did not pass";
  return null;
}
function excludedBeforeTiming(group, candidate) {
  const exclusionReason = parityRejection(candidate);
  if (!exclusionReason) return null;
  const entry = { group, candidate, stage: "excluded-before-timing", round: 0, valid: false, exclusionReason };
  report.disqualifications.push(entry);
  console.log(`${group} ${candidate}: EXCLUDED (${exclusionReason})`);
  checkpoint();
  return entry;
}
const digest = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const normalizeFile = (file) => {
  if (!file) throw new Error("A test case has no source file in its runner report");
  const value = file.startsWith("file:") ? fileURLToPath(file) : file;
  return (path.isAbsolute(value) ? path.relative(frontend, value) : value).replaceAll("\\", "/");
};
const unescapeXml = (value) => value.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const attributes = (value) => Object.fromEntries([...value.matchAll(/([\w-]+)="([^"]*)"/g)].map((match) => [match[1], unescapeXml(match[2])]));
const normalizeTests = (tests) => tests.map((test) => `${normalizeFile(test.file)}::${test.name}`).sort();

function validateTests(group, candidate, rawPath, logPath) {
  let tests;
  if (group === "components") {
    const data = JSON.parse(readFileSync(rawPath, "utf8"));
    if (!data.success || data.numFailedTests || data.numPendingTests || data.numTodoTests || data.numTotalTests !== counts.components) throw new Error("Component counts/status do not match the baseline");
    tests = data.testResults.flatMap((file) => {
      if (file.status !== "passed") throw new Error(`Failed component test file: ${file.name}`);
      return file.assertionResults.map((test) => ({ file: file.name, name: test.fullName, status: test.status, skipped: false }));
    });
  } else if (candidate === "lib-node") {
    const data = JSON.parse(readFileSync(rawPath, "utf8"));
    tests = data.tests;
  } else {
    const xml = readFileSync(rawPath, "utf8");
    if (/<(?:failure|error|skipped)\b/.test(xml)) throw new Error("Bun reported a failed, errored or skipped test");
    tests = [...xml.matchAll(/<testcase\b([^>]*)>/g)].map((match) => {
      const entry = attributes(match[1]);
      return { name: entry.name, file: entry.file, status: "passed", skipped: false };
    });
  }
  if (tests.length !== counts[group] || tests.some((test) => test.status !== "passed" || test.skipped)) throw new Error(`Incomplete ${group} test inventory`);
  const files = [...new Set(tests.map((test) => normalizeFile(test.file)))].sort();
  if (JSON.stringify(files) !== JSON.stringify(expectedFiles[group])) throw new Error(`Changed ${group} file coverage`);
  const inventory = normalizeTests(tests);
  const baseline = report.baselineInventories[group];
  if (baseline && JSON.stringify(inventory) !== JSON.stringify(baseline)) throw new Error(`Changed ${group} test names`);
  if (!baseline) report.baselineInventories[group] = inventory;
  return { files: files.length, tests: tests.length, inventorySha256: digest(inventory) };
}

function fingerprint(directory) {
  const files = globSync("**/*", { cwd: directory }).filter((file) => statSync(path.join(directory, file)).isFile()).sort();
  if (!files.includes("index.html")) throw new Error("Production output has no index.html");
  const manifest = files.map((file) => {
    const bytes = readFileSync(path.join(directory, file));
    return { file: file.replaceAll("\\", "/"), size: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  return { files: manifest.length, bytes: manifest.reduce((total, file) => total + file.size, 0), sha256: digest(manifest), manifest };
}

let counter = 0;
async function run(group, candidate, stage, round, { extra = [], timeoutMs = group === "components" ? 360000 : 120000 } = {}) {
  const id = `${String(++counter).padStart(4, "0")}-${group}-${stage}-${round}-${candidate}`;
  const logPath = path.join(output, `${id}.log`);
  const rawPath = path.join(output, `${id}.report.${candidate.startsWith("lib-bun") ? "xml" : "json"}`);
  const rssPath = path.join(output, `${id}.rss`);
  const buildDirectory = path.join(output, "build-output");
  const args = [harness, candidate, ...extra];
  if (group === "components") args.push("--reporter=json", "--outputFile", rawPath);
  if (group === "logic" && candidate === "lib-node") args.push(`--test-reporter=${pathToFileURL(path.join(import.meta.dirname, "node-inventory-reporter.mjs")).href}`);
  if (group === "logic" && candidate.startsWith("lib-bun")) args.push("--reporter=junit", "--reporter-outfile", rawPath);
  if (group === "build") args.push("--outDir", buildDirectory);
  rmSync(rawPath, { force: true });
  rmSync(rssPath, { force: true });
  const log = openSync(logPath, "w");
  let binary = process.execPath;
  let commandArgs = args;
  if (process.env.SCRIBER_TIME_EXE) {
    binary = process.env.SCRIBER_TIME_EXE;
    commandArgs = ["-f", "%M", "-o", rssPath, process.execPath, ...args];
  }
  const started = performance.now();
  let timedOut = false;
  let spawnError;
  let status;
  let signal;
  try {
    const child = spawn(binary, commandArgs, { cwd: root, env: { ...process.env, SCRIBER_NODE_TEST_REPORT: rawPath }, stdio: ["ignore", log, log], detached: process.platform !== "win32" });
    let hardKill;
    const timer = setTimeout(() => {
      timedOut = true;
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
      } else {
        try { process.kill(-child.pid, "SIGTERM"); } catch {}
        hardKill = setTimeout(() => { try { process.kill(-child.pid, "SIGKILL"); } catch {} }, 5000);
      }
    }, timeoutMs);
    await new Promise((resolve) => {
      child.on("error", (error) => { spawnError = error; });
      child.on("close", (code, termination) => { status = code; signal = termination; resolve(); });
    });
    clearTimeout(timer);
    if (hardKill) clearTimeout(hardKill);
  } finally {
    closeSync(log);
  }
  const entry = {
    group, candidate, stage, round, wallSeconds: (performance.now() - started) / 1000,
    exitCode: status ?? null, signal: signal ?? null, timedOut, spawnError: spawnError?.message ?? null,
    valid: !spawnError && !signal && !timedOut && status === 0,
    log: path.basename(logPath), report: existsSync(rawPath) ? path.basename(rawPath) : null,
    maxRssKiB: existsSync(rssPath) ? Number(readFileSync(rssPath, "utf8").trim()) : null,
  };
  if (!Number.isFinite(entry.maxRssKiB)) entry.maxRssKiB = null;
  try {
    if (entry.valid && (group === "logic" || group === "components")) entry.evidence = validateTests(group, candidate, rawPath, logPath);
    if (entry.valid && group === "build") {
      const built = fingerprint(buildDirectory);
      entry.evidence = { files: built.files, bytes: built.bytes, sha256: built.sha256 };
      const manifestName = `${id}.build-manifest.json`;
      writeFileSync(path.join(output, manifestName), JSON.stringify(built.manifest, null, 2) + "\n");
      entry.report = manifestName;
      const baseline = report.baselineInventories.build;
      if (baseline && baseline.sha256 !== built.sha256) {
        cpSync(buildDirectory, path.join(output, `${id}.different-output`), { recursive: true, verbatimSymlinks: true });
        throw new Error("Production output is not byte-identical to the Node baseline");
      }
      if (!baseline) {
        report.baselineInventories.build = { files: built.files, bytes: built.bytes, sha256: built.sha256 };
        writeFileSync(path.join(output, "production-manifest.json"), JSON.stringify(built.manifest, null, 2) + "\n");
      }
    }
  } catch (error) {
    entry.valid = false;
    entry.validationError = error.message;
  }
  report.samples.push(entry);
  checkpoint();
  console.log(`${group} ${stage} ${round} ${candidate}: ${entry.valid ? "PASS" : "FAIL"} ${entry.wallSeconds.toFixed(3)}s${entry.validationError ? ` (${entry.validationError})` : ""}`);
  return entry;
}

function choose(screening, baseline) {
  const passed = screening.filter((entry) => entry.valid).sort((left, right) => left.wallSeconds - right.wallSeconds);
  if (!passed.some((entry) => entry.candidate === baseline)) throw new Error(`Invalid baseline: ${baseline}`);
  const selected = new Set([baseline]);
  const best = passed[0].wallSeconds;
  for (const entry of passed) if (entry.wallSeconds <= best * 1.15) selected.add(entry.candidate);
  for (const runtime of ["node", "bun"]) {
    const first = passed.find((entry) => entry.candidate.includes(`-${runtime}`));
    if (first) selected.add(first.candidate);
  }
  return [...selected];
}
const rotatedOrder = (list, index) => [...list.slice(index % list.length), ...list.slice(0, index % list.length)];

async function testGroup(group, candidates, baseline) {
  const screen = [];
  for (const candidate of candidates) screen.push(excludedBeforeTiming(group, candidate) ?? await run(group, candidate, "screen", 0));
  report.screening.push(...screen);
  const selected = choose(screen, baseline);
  report.selections[group] = selected;
  checkpoint();
  for (let round = 1; round <= rounds; round++) {
    for (const candidate of rotatedOrder(selected, round - 1)) {
      const entry = await run(group, candidate, "measured", round);
      if (!entry.valid && candidate === baseline) throw new Error(`The baseline failed during measured round ${round}`);
    }
  }
}

async function lintGroup() {
  const allCandidates = ["lint-eslint", "lint-eslint-cached", "lint-oxlint-hybrid", "lint-oxlint-hybrid-cached"];
  const candidates = [];
  const disqualified = new Set();
  const cache = (candidate) => path.join(frontend, "node_modules/.cache", candidate === "lint-eslint-cached" ? "eslint-speed" : "eslint-residual-speed");
  const requireBaselineOrExclude = (entry) => {
    if (entry.valid) return true;
    if (entry.candidate === "lint-eslint") throw new Error(`The original ESLint baseline failed: ${entry.stage}`);
    disqualified.add(entry.candidate);
    report.disqualifications.push({ group: "lint", candidate: entry.candidate, stage: entry.stage, round: entry.round, reason: entry.exclusionReason ?? entry.validationError ?? entry.spawnError ?? `Exit ${entry.exitCode}` });
    checkpoint();
    return false;
  };
  for (const candidate of allCandidates) {
    const entry = excludedBeforeTiming("lint", candidate) ?? await run("lint", candidate, "screen", 0);
    report.screening.push(entry);
    if (requireBaselineOrExclude(entry)) candidates.push(candidate);
  }
  report.selections.lint = candidates;
  for (const scenario of ["cold", "warm", "edited"]) {
    const fixture = path.join(frontend, "client/src/lib/__speed_lint_timing_probe.ts");
    try {
      if (scenario === "edited") writeFileSync(fixture, "export const speedTimingProbe = 1;\n");
      if (scenario === "warm") {
        for (const candidate of candidates) {
          if (disqualified.has(candidate)) continue;
          const entry = await run("lint", candidate, "warmup-warm", 0);
          requireBaselineOrExclude(entry);
        }
      }
      for (let round = 1; round <= rounds; round++) {
        for (const candidate of rotatedOrder(candidates, round - 1)) {
          if (disqualified.has(candidate)) continue;
          if (scenario === "cold" && candidate.endsWith("-cached")) rmSync(cache(candidate), { force: true });
          if (scenario === "edited") {
            writeFileSync(fixture, "export const speedTimingProbe = 1;\n");
            const warm = await run("lint", candidate, "warmup-edited", round);
            if (!requireBaselineOrExclude(warm)) continue;
            writeFileSync(fixture, "export const speedTimingProbe = 2;\n");
          }
          const entry = await run("lint", candidate, `measured-${scenario}`, round);
          requireBaselineOrExclude(entry);
        }
      }
    } finally { rmSync(fixture, { force: true }); }
  }
}

function summarize() {
  const samples = report.samples.filter((entry) => entry.stage.startsWith("measured"));
  const buckets = new Map();
  for (const entry of samples) {
    const key = [entry.group, entry.stage, entry.candidate].join("|");
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(entry);
  }
  report.summaries = [...buckets.values()].map((entries) => {
    const values = entries.map((entry) => entry.wallSeconds).sort((a, b) => a - b);
    const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
    return {
      group: entries[0].group, scenario: entries[0].stage, candidate: entries[0].candidate,
      eligible: entries.length >= rounds && entries.every((entry) => entry.valid) && !report.disqualifications.some((entry) => entry.candidate === entries[0].candidate),
      samples: values.length, medianSeconds: values.length % 2 ? values[(values.length - 1) / 2] : (values[values.length / 2 - 1] + values[values.length / 2]) / 2,
      minSeconds: values[0], maxSeconds: values.at(-1), standardDeviationSeconds: Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length),
      maxRssKiB: entries.every((entry) => entry.maxRssKiB === null) ? null : Math.max(...entries.map((entry) => entry.maxRssKiB ?? 0)),
    };
  });
}

try {
  for (const group of groups) {
    if (group === "logic") {
      const workers = [...new Set([2, 4, os.availableParallelism()])].filter((value) => value <= 8);
      await testGroup(group, ["lib-node", "lib-bun-isolated", ...workers.map((value) => `lib-bun-parallel-${value}`)], "lib-node");
    } else if (group === "components") {
      const candidates = ["components-original"];
      for (const runtime of ["node", "bun"]) for (const pool of ["forks", "threads"]) for (const workers of [1, 2, 4, "default"]) candidates.push(`components-${runtime}-${pool}-${workers}`);
      await testGroup(group, candidates, "components-original");
    } else if (group === "lint") {
      await lintGroup();
    } else if (group === "build") {
      await testGroup(group, ["build-node", "build-bun"], "build-node");
    } else {
      throw new Error(`Unknown benchmark group: ${group}`);
    }
  }
} catch (error) {
  report.error = error.stack ?? error.message;
  process.exitCode = 1;
} finally {
  summarize();
  report.metadata.finishedAt = new Date().toISOString();
  checkpoint();
  console.log(JSON.stringify({ error: report.error, summaries: report.summaries }, null, 2));
}
