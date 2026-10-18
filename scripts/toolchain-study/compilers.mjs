/**
 * Isolated Scriber compiler equivalence and benchmark harness.
 * All generated configs, sentinels and incremental caches live in --frontend,
 * which MUST be a disposable copy of Frontend, never the working checkout.
 * Usage: node run.mjs --frontend /tmp/copy/Frontend --tools /tmp/compiler-tools
 *   --bun /path/to/bun --output /tmp/results --mode correctness|benchmark|report
 *   [--rounds 5] [--candidates baseline,baseline-normalized,ts7,...]
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(
  process.argv.slice(2).reduce((items, value, index, all) => {
    if (value.startsWith("--")) items.push([value.slice(2), all[index + 1]]);
    return items;
  }, []),
);
const frontend = path.resolve(args.frontend ?? path.join(HERE, "project/Frontend"));
const tools = path.resolve(args.tools ?? path.join(HERE, "tools"));
const output = path.resolve(args.output ?? path.join(HERE, "results"));
const bun = path.resolve(args.bun ?? path.join(HERE, "../../toolchain/node_modules/.bin/bun"));
const mode = args.mode ?? "correctness";
const rounds = Number(args.rounds ?? "5");
const timeout = Number(args.timeout ?? "120000");
const timeExe = path.resolve(args["time-exe"] ?? process.env.SCRIBER_TIME_EXE ?? "/usr/bin/time");
const rssLabel = args["rss-label"] ?? process.env.SCRIBER_RSS_METHOD ?? (path.basename(timeExe) === "wait-rss" ? "Linux wait4" : "GNU time");
const cacheRoot = path.join(frontend, ".compiler-cache");
fs.mkdirSync(output, { recursive: true });
fs.mkdirSync(cacheRoot, { recursive: true });

const candidates = [
  { id: "baseline", package: "typescript", bin: "bin/tsc", normalized: false },
  { id: "baseline-normalized", package: "typescript", bin: "bin/tsc", normalized: true },
  { id: "ts7", package: "ts7", bin: "bin/tsc", normalized: true, oracle: "baseline-normalized" },
  { id: "tsrs", package: "tsrs", bin: "bin/tsrs", normalized: true, oracle: "tsrs-oracle" },
  { id: "tsc-rs", package: "tsc-rs", bin: "bin/tsc-rs", normalized: true, oracle: "tsc-rs-oracle" },
  { id: "tsc-rs-fast", package: "tsc-rs", bin: "bin/tsc-rs", flags: ["--fast"], normalized: true, oracle: "tsc-rs-oracle" },
  { id: "tsc-ts", package: "tsc-ts", bin: "bin/tsc-ts.js", normalized: true, oracle: "ts7" },
  { id: "bun-check", bun: true, normalized: true, oracle: "ts7" },
  { id: "tsrs-oracle", package: "tsrs-oracle", bin: "bin/tsc", normalized: true, referenceOnly: true },
  { id: "tsc-rs-oracle", package: "tsc-rs-oracle", bin: "bin/tsc", normalized: true, referenceOnly: true },
];
const selectedIds = args.candidates?.split(",");
const selected = candidates.filter((candidate) => !selectedIds || selectedIds.includes(candidate.id));
const originalApp = JSON.parse(fs.readFileSync(path.join(frontend, "tsconfig.json"), "utf8"));
const originalTests = JSON.parse(fs.readFileSync(path.join(frontend, "tsconfig.tests.json"), "utf8"));

function writeJson(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, `${JSON.stringify(value, null, 2)}\n`);
}

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

function portable(filename) {
  return filename.replaceAll("\\", "/");
}

function configFile(candidate, project) {
  return path.join(frontend, `tsconfig.compiler-${candidate.id}.${project}.json`);
}

function prepare(candidate) {
  const app = structuredClone(originalApp);
  const tests = structuredClone(originalTests);
  app.compilerOptions.tsBuildInfoFile = `./.compiler-cache/${candidate.id}.app.tsbuildinfo`;
  tests.compilerOptions.tsBuildInfoFile = `./.compiler-cache/${candidate.id}.tests.tsbuildinfo`;
  tests.extends = `./${path.basename(configFile(candidate, "app"))}`;
  if (candidate.normalized) {
    // TS7 removed ES5. Compare separately against the untouched TS5 baseline.
    // Explicitly freeze target-dependent defaults instead of accepting ES2025.
    delete app.compilerOptions.baseUrl;
    Object.assign(app.compilerOptions, {
      target: "ES2015",
      rootDir: ".",
      noUncheckedSideEffectImports: false,
      useDefineForClassFields: false,
    });
    // TypeScript <6 lacks this option; the dependency closure contains no lib
    // replacement packages, so setting true here preserves the older behavior.
    if (candidate.package !== "typescript") app.compilerOptions.libReplacement = true;
  }
  writeJson(configFile(candidate, "app"), app);
  writeJson(configFile(candidate, "tests"), tests);
}

function command(candidate, project, flags = []) {
  const config = path.basename(configFile(candidate, project));
  if (candidate.bun) {
    // Bun 1.4.3 still selects the package check script even with --no-script.
    // Explicitly select a package without that script and an absolute project.
    // tsconfig-relative source/module resolution remains unchanged.
    const toolsManifest = JSON.parse(fs.readFileSync(path.join(tools, "package.json"), "utf8"));
    if (toolsManifest.scripts?.check) throw new Error("Bun compiler tools directory must not contain a check script.");
    return { exe: bun, cwd: tools, args: ["check", "--no-pretty", "--all", "-p", path.join(frontend, config), ...flags] };
  }
  return {
    exe: process.execPath,
    args: [path.join(tools, "node_modules", candidate.package, candidate.bin), ...(candidate.flags ?? []), "-p", config, "--pretty", "false", "--noErrorTruncation", ...flags],
  };
}

function cleanCache(candidate) {
  for (const project of ["app", "tests"]) {
    fs.rmSync(path.join(cacheRoot, `${candidate.id}.${project}.tsbuildinfo`), { force: true });
  }
}

function execute(invocation, logName, timed = false) {
  const logPath = path.join(output, `${logName}.log`);
  let exe = invocation.exe;
  let argv = invocation.args;
  let rssPath;
  if (timed && process.platform === "linux" && fs.existsSync(timeExe)) {
    rssPath = path.join(output, `${logName}.rss`);
    exe = timeExe;
    argv = ["-f", "%M", "-o", rssPath, invocation.exe, ...invocation.args];
  }
  const start = performance.now();
  const result = spawnSync(exe, argv, {
    cwd: invocation.cwd ?? frontend,
    encoding: "utf8",
    timeout,
    maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  const elapsedMs = performance.now() - start;
  const text = `${result.stdout ?? ""}${result.stderr ?? ""}`.replaceAll("\r\n", "\n");
  fs.writeFileSync(logPath, text);
  if (result.error?.code === "ETIMEDOUT") {
    // A timed-out wrapper can leave a native grandchild alive. Kill the owned
    // process group on POSIX and abort this lane on every platform; never time
    // another candidate under uncertain residual CPU load.
    if (process.platform !== "win32" && result.pid) {
      try { process.kill(-result.pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    throw new Error(`Compiler command timed out; lane aborted to prevent contaminated timings. See ${logPath}`);
  }
  let maxRssKiB = null;
  if (rssPath && fs.existsSync(rssPath)) {
    const lines = fs.readFileSync(rssPath, "utf8").trim().split("\n");
    const value = Number(lines.at(-1));
    if (Number.isFinite(value)) maxRssKiB = value;
  }
  return {
    exitCode: result.error || result.signal ? null : result.status,
    signal: result.signal,
    error: result.error?.message ?? null,
    cwd: invocation.cwd ?? frontend,
    elapsedMs,
    maxRssKiB,
    log: path.relative(output, logPath),
    stdout: result.stdout ?? "",
    text,
  };
}

function cleanText(text) {
  const replacements = [
    [frontend, "<frontend>"],
    [fs.realpathSync(path.join(frontend, "node_modules")), "<node_modules>"],
    [tools, "<compiler-tools>"],
  ];
  let result = text.replaceAll(/\x1b\[[0-9;]*m/g, "");
  for (const [from, to] of replacements) {
    result = result.replaceAll(from, to).replaceAll(portable(from), to);
  }
  return result;
}

function diagnostics(text, cwd = frontend) {
  // Resolve diagnostic paths before anonymizing roots. A relative path from
  // tools to a /tmp project can contain the absolute project path as a suffix;
  // replacing that substring first would make the relative path unresolvable.
  const lines = text.replaceAll(/\x1b\[[0-9;]*m/g, "").replaceAll("\r\n", "\n").split("\n");
  const parsed = [];
  let inDiagnostic = false;
  for (const line of lines) {
    const match = line.match(/^(.*?)(?:\((\d+),(\d+)\))?: (error|warning) TS(\d+): (.*)$/);
    const plain = line.match(/^(error|warning) TS(\d+): (.*)$/);
    if (match) {
      let filename = portable(match[1]);
      if (filename) {
        const resolved = path.resolve(cwd, filename);
        if (pathKey(resolved).startsWith(`${pathKey(frontend)}/`)) filename = pathKey(path.relative(frontend, resolved));
        else filename = cleanText(filename);
      }
      parsed.push({ file: filename, line: Number(match[2] ?? 0), column: Number(match[3] ?? 0), category: match[4], code: Number(match[5]), message: cleanText(match[6]) });
      inDiagnostic = true;
    } else if (plain) {
      parsed.push({ file: "", line: 0, column: 0, category: plain[1], code: Number(plain[2]), message: cleanText(plain[3]) });
      inDiagnostic = true;
    } else if (/^\s+\S/.test(line) && inDiagnostic) {
      parsed.at(-1).message += `\n${cleanText(line.replace(/\s+$/, ""))}`;
    } else {
      // Bun adds a separate count-per-file footer after "Found N errors".
      // It is not part of the final TypeScript diagnostic message.
      inDiagnostic = false;
    }
  }
  return parsed.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function summarizeRun(run) {
  return {
    exitCode: run.exitCode,
    signal: run.signal,
    error: run.error,
    elapsedMs: run.elapsedMs,
    maxRssKiB: run.maxRssKiB,
    log: run.log,
    diagnostics: diagnostics(run.text, run.cwd),
    outputSha256: hash(cleanText(run.text)),
  };
}

function pathKey(filename) {
  const value = portable(filename);
  return process.platform === "win32" ? value.toLowerCase() : value;
}

function filesFrom(text, cwd = frontend) {
  const files = text.replaceAll("\r\n", "\n").split("\n").map((line) => line.trim()).filter((line) => /\.[cm]?[jt]sx?$/.test(line)).map((filename) => filename.startsWith("bundled:///") ? filename : path.resolve(cwd, filename)).filter((filename) => filename.startsWith("bundled:///") || fs.existsSync(filename));
  const absoluteFrontend = pathKey(frontend);
  const projectFiles = files.filter((filename) => pathKey(filename).startsWith(`${absoluteFrontend}/`) && !pathKey(filename).includes("/node_modules/")).map((filename) => pathKey(path.relative(frontend, filename))).sort();
  const dependencyRoot = fs.realpathSync(path.join(frontend, "node_modules"));
  const dependencyFiles = files.filter((filename) => !filename.startsWith("bundled:///") && pathKey(fs.realpathSync(filename)).startsWith(`${pathKey(dependencyRoot)}/`)).map((filename) => pathKey(path.relative(dependencyRoot, fs.realpathSync(filename)))).sort();
  const libraries = files.filter((filename) => /(?:^|[\\/])lib\.[\w.]+\.d\.ts$/.test(filename)).map((filename) => ({ name: path.basename(filename), sha256: filename.startsWith("bundled:///") ? null : hash(fs.readFileSync(filename)) })).sort((a, b) => a.name.localeCompare(b.name));
  return { total: files.length, projectFiles, projectFilesSha256: hash(JSON.stringify(projectFiles)), dependencyFiles, dependencyFilesSha256: hash(JSON.stringify(dependencyFiles)), libraries, librariesSha256: libraries.some((library) => library.sha256 === null) ? null : hash(JSON.stringify(libraries)), libraryNamesSha256: hash(JSON.stringify(libraries.map((library) => library.name))) };
}

const packageFileInfoCache = new Map();
function packageFileInfo(relativeFile) {
  if (packageFileInfoCache.has(relativeFile)) return packageFileInfoCache.get(relativeFile);
  const absolute = path.join(frontend, "node_modules", relativeFile);
  let directory = path.dirname(absolute);
  while (directory !== path.dirname(directory)) {
    const packageFile = path.join(directory, "package.json");
    if (fs.existsSync(packageFile)) {
      const manifest = JSON.parse(fs.readFileSync(packageFile, "utf8"));
      if (manifest.name && manifest.version) {
        const info = { identity: `${manifest.name}@${manifest.version}:${pathKey(path.relative(directory, absolute))}`, sha256: hash(fs.readFileSync(absolute)) };
        packageFileInfoCache.set(relativeFile, info);
        return info;
      }
    }
    directory = path.dirname(directory);
  }
  return null;
}

function explainDependencyMigration(baseline, candidate) {
  const removed = baseline.filter((filename) => !candidate.includes(filename));
  const added = candidate.filter((filename) => !baseline.includes(filename));
  const nodeManifest = JSON.parse(fs.readFileSync(path.join(frontend, "node_modules/@types/node/package.json"), "utf8"));
  const hasNodeMapping = JSON.stringify(nodeManifest.typesVersions?.["<=5.6"]?.["*"]) === JSON.stringify(["ts5.6/*"]);
  const evidence = [];
  const unexplained = [];
  for (const filename of removed) {
    const mapped = filename.replace(/^@types\/node\/ts5\.6\//, "@types/node/");
    if (hasNodeMapping && filename !== mapped && added.includes(mapped)) {
      evidence.push({ kind: "declared-typesVersions", package: `@types/node@${nodeManifest.version}`, from: filename, to: mapped });
      continue;
    }
    const info = packageFileInfo(filename);
    const replacement = candidate.find((other) => {
      const otherInfo = packageFileInfo(other);
      return info && otherInfo && info.identity === otherInfo.identity && info.sha256 === otherInfo.sha256;
    });
    if (replacement) evidence.push({ kind: "identical-package-deduplication", from: filename, to: replacement, ...info });
    else unexplained.push({ removed: filename });
  }
  for (const filename of added) {
    if (!evidence.some((item) => item.to === filename)) unexplained.push({ added: filename });
  }
  return { removed, added, evidence, unexplained, accountedFor: unexplained.length === 0 };
}

function versions(candidate) {
  if (candidate.bun) {
    return execute({ exe: bun, args: ["-p", "JSON.stringify({bun:process.versions.bun,typescript:process.versions.typescript})"] }, `${candidate.id}.version`).text.trim();
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(tools, "node_modules", candidate.package, "package.json"), "utf8"));
  const invocation = { exe: process.execPath, args: [path.join(tools, "node_modules", candidate.package, candidate.bin), "--version"] };
  const run = execute(invocation, `${candidate.id}.version`);
  return { packageVersion: manifest.version, compilerVersion: run.text.trim(), available: run.exitCode === 0, optionalDependencies: manifest.optionalDependencies ?? {} };
}

const appSentinel = path.join(frontend, "client/src/compiler-equivalence-probe.tsx");
const testsSentinel = path.join(frontend, "client/src/compiler-equivalence-probe.test.tsx");
const targetSentinel = path.join(frontend, "client/src/compiler-target-probe.ts");
const consumerSentinel = path.join(frontend, "client/src/compiler-incremental-consumer.ts");
const validApp = `import { cn } from "@/lib/utils";
import type { ReactElement } from "react";
import "./compiler-missing-side-effect-probe";
type Payload = { kind: "ready"; count: number } | { kind: "failed"; error: Error };
export function probeCount(payload: Payload): number {
  return payload.kind === "ready" ? payload.count : 0;
}
export const probePromise: Promise<number> = Promise.resolve(1);
export const probeElement: ReactElement = <div className={cn("probe")}>{probeCount({ kind: "ready", count: 1 })}</div>;
export function probeIdentity<T extends { count: number }>(value: T): T { return value; }
// @ts-expect-error Intentional negative type assertion, expected to be consumed.
export const consumedTypeError: number = "text";
export const propagationProbe = { value: 1 };
`;
const invalidApp = `${validApp}
export const incorrectAssignment: number = "text";
export const incorrectNull: string = null;
export const incorrectCallback: (value: string) => void = (_value: number) => {};
export function incorrectCatch(): void { try { throw new Error("probe"); } catch (error) { error.message; } }
export const incorrectGeneric = probeIdentity({ count: "wrong" });
export const incorrectDom: HTMLButtonElement = document.createElement("div");
const ProbeRequiredProps = ({ count }: { count: number }) => <span>{count}</span>;
export const incorrectJsx = <ProbeRequiredProps />;
// @ts-expect-error This directive must be reported as unused.
export const unusedDirective = 1;
`;
const validTests = `import { expectTypeOf } from "vitest";
import { probeCount, propagationProbe } from "./compiler-equivalence-probe";
expectTypeOf(probeCount({ kind: "ready", count: 1 })).toEqualTypeOf<number>();
expect(document.body).toBeInTheDocument();
export const testProbe: number = 1;
export const importedTypeProbe: number = propagationProbe.value;
`;
const invalidTests = `${validTests}
export const incorrectTestAssignment: number = "text";
expect(document.body).notARealMatcher();
expectTypeOf<number>().toEqualTypeOf<string>();
`;

function writeSentinels(phase) {
  if (phase === "valid" || phase === "invalid" || phase === "propagated") {
    const appText = phase === "propagated" ? validApp.replace("export const propagationProbe = { value: 1 };", 'export const propagationProbe = { value: "changed" };') : phase === "valid" ? validApp : invalidApp;
    fs.writeFileSync(appSentinel, appText);
    if (phase !== "propagated") {
      fs.writeFileSync(testsSentinel, phase === "invalid" ? invalidTests : validTests);
      fs.writeFileSync(consumerSentinel, 'import { propagationProbe } from "./compiler-equivalence-probe";\nexport const unchangedConsumer: number = propagationProbe.value;\n');
    }
  } else if (phase === "target") {
    fs.writeFileSync(targetSentinel, "export class TargetProbe { #value = 1; read() { return this.#value; } }\nexport function targetProbe() { for (const value of new Set<number>([1])) { return value; } }\n");
  }
}

function removeSentinels() {
  for (const filename of [appSentinel, testsSentinel, targetSentinel, consumerSentinel]) fs.rmSync(filename, { force: true });
}

function assertDisposable() {
  for (const filename of [appSentinel, testsSentinel, targetSentinel, consumerSentinel]) {
    if (fs.existsSync(filename)) throw new Error(`Refusing to overwrite existing sentinel file: ${filename}`);
  }
  if (frontend === path.resolve(HERE, "../../Scriber/Frontend")) {
    throw new Error("Refusing to use the primary Scriber working tree. Pass a disposable Frontend copy.");
  }
}

function correctness() {
  const results = args.resume === "true" && fs.existsSync(path.join(output, "correctness.json")) ? JSON.parse(fs.readFileSync(path.join(output, "correctness.json"), "utf8")) : {
    metadata: metadata(),
    targetMigration: "Untouched TS5.6 defaults to ES5. All normalized configurations explicitly use ES2015 (the minimum accepted by TS7), retain lib/strict/types/noEmit, preserve noUncheckedSideEffectImports=false and useDefineForClassFields=false, and remove baseUrl while retaining relative path aliases.",
    candidates: {},
  };
  results.execution = { incrementalState: "See each candidate cachePolicy. The current harness resets only clean; subsequent phases reuse prior state to verify changed-file error detection and recovery.", bunInvocation: "Built-in check runs from the isolated tools package (no check script) with an absolute --project path; --no-script is ineffective in Bun 1.4.3." };
  for (const entry of Object.values(results.candidates)) {
    entry.cachePolicy ??= "reset-every-phase (initial local run)";
    for (const coverage of Object.values(entry.coverage ?? {})) {
      Object.assign(coverage, filesFrom(fs.readFileSync(path.join(output, coverage.log), "utf8")));
    }
  }
  const available = [];
  for (const candidate of selected) {
    prepare(candidate);
    const version = versions(candidate);
    const entry = { version, oracle: candidate.oracle ?? null, cachePolicy: "reset-clean-only", phases: {} };
    results.candidates[candidate.id] = entry;
    if (typeof version === "object" && !version.available) {
      entry.status = "unavailable";
      continue;
    }
    available.push(candidate);
    entry.coverage = {};
    for (const project of ["app", "tests"]) {
      const run = execute(command(candidate, project, ["--listFilesOnly"]), `${candidate.id}.${project}.files`);
      entry.coverage[project] = { exitCode: run.exitCode, ...filesFrom(run.stdout), log: run.log };
    }
  }
  for (const phase of ["clean", "valid", "propagated", "invalid", "target", "restored"]) {
    if (phase !== "propagated") removeSentinels();
    writeSentinels(phase);
    for (const candidate of available) {
      if (phase === "clean") cleanCache(candidate);
      const entry = results.candidates[candidate.id];
      entry.phases[phase] = {};
      for (const project of ["app", "tests"]) {
        const run = execute(command(candidate, project), `${candidate.id}.${project}.${phase}`);
        entry.phases[phase][project] = summarizeRun(run);
      }
      process.stdout.write(`${phase}: ${candidate.id} app=${entry.phases[phase].app.exitCode}, tests=${entry.phases[phase].tests.exitCode}\n`);
      writeJson(path.join(output, "correctness.json"), results);
    }
  }
  removeSentinels();
  return summarizeCorrectness(results);
}

function summarizeCorrectness(results) {
  for (const [id, entry] of Object.entries(results.candidates)) {
    if (!entry.phases?.restored) continue;
    for (const coverage of Object.values(entry.coverage)) {
      Object.assign(coverage, filesFrom(fs.readFileSync(path.join(output, coverage.log), "utf8"), id === "bun-check" ? tools : frontend));
    }
    for (const phase of Object.values(entry.phases)) {
      for (const run of Object.values(phase)) {
        run.diagnostics = diagnostics(fs.readFileSync(path.join(output, run.log), "utf8"), id === "bun-check" ? tools : frontend);
      }
    }
  }
  for (const [id, entry] of Object.entries(results.candidates)) {
    if (!entry.phases?.restored) continue;
    entry.sentinelSummary = {};
    for (const project of ["app", "tests"]) {
      const probeDiagnostics = entry.phases.invalid[project].diagnostics.filter((diagnostic) => diagnostic.file.includes("compiler-equivalence-probe"));
      entry.sentinelSummary[project] = {
        diagnosticCount: probeDiagnostics.length,
        codes: probeDiagnostics.map((diagnostic) => diagnostic.code).sort((a, b) => a - b),
        appProbeDetected: probeDiagnostics.some((diagnostic) => diagnostic.file.endsWith("compiler-equivalence-probe.tsx")),
        testProbeDetected: probeDiagnostics.some((diagnostic) => diagnostic.file.endsWith("compiler-equivalence-probe.test.tsx")),
      };
    }
    const oracle = results.candidates[entry.oracle];
    if (oracle?.phases?.clean) {
      entry.oracleComparison = {};
      for (const phase of Object.keys(entry.phases)) {
        if (!oracle.phases[phase]) continue;
        entry.oracleComparison[phase] = {};
        for (const project of ["app", "tests"]) {
          entry.oracleComparison[phase][project] = JSON.stringify(entry.phases[phase][project].diagnostics) === JSON.stringify(oracle.phases[phase][project].diagnostics);
        }
      }
      entry.oracleCoverageMatches = Object.fromEntries(["app", "tests"].map((project) => [project, entry.coverage[project].projectFilesSha256 === oracle.coverage[project].projectFilesSha256]));
      entry.oracleDependencyCoverageMatches = Object.fromEntries(["app", "tests"].map((project) => [project, entry.coverage[project].dependencyFilesSha256 === oracle.coverage[project].dependencyFilesSha256]));
      entry.oracleLibrariesMatch = Object.fromEntries(["app", "tests"].map((project) => [project, entry.coverage[project].librariesSha256 === null || oracle.coverage[project].librariesSha256 === null ? null : entry.coverage[project].librariesSha256 === oracle.coverage[project].librariesSha256]));
      entry.oracleLibraryNamesMatch = Object.fromEntries(["app", "tests"].map((project) => [project, entry.coverage[project].libraryNamesSha256 === oracle.coverage[project].libraryNamesSha256]));
    }
    const baseline = results.candidates.baseline;
    entry.sourceCoverageMatchesBaseline = Object.fromEntries(["app", "tests"].map((project) => [project, baseline ? entry.coverage[project].projectFilesSha256 === baseline.coverage[project].projectFilesSha256 : null]));
    entry.dependencyCoverageMatchesBaseline = Object.fromEntries(["app", "tests"].map((project) => [project, baseline ? entry.coverage[project].dependencyFilesSha256 === baseline.coverage[project].dependencyFilesSha256 : null]));
    entry.dependencyMigration = Object.fromEntries(["app", "tests"].map((project) => [project, baseline ? explainDependencyMigration(baseline.coverage[project].dependencyFiles, entry.coverage[project].dependencyFiles) : null]));
    entry.cleanValidRestoredPass = ["clean", "valid", "restored"].every((phase) => ["app", "tests"].every((project) => entry.phases[phase][project].exitCode === 0 && entry.phases[phase][project].diagnostics.length === 0));
    entry.requiredSentinelsDetected = entry.sentinelSummary.app.diagnosticCount === 8 && !entry.sentinelSummary.app.testProbeDetected && entry.sentinelSummary.tests.diagnosticCount === 11 && entry.sentinelSummary.tests.testProbeDetected && ["app", "tests"].every((project) => entry.phases.invalid[project].exitCode !== 0 && entry.phases.invalid[project].exitCode !== null);
    entry.disqualificationReasons = [];
    if (!entry.cleanValidRestoredPass) entry.disqualificationReasons.push("Clean source, valid probes, or restored source failed.");
    if (!entry.requiredSentinelsDetected) entry.disqualificationReasons.push("Required app/test sentinel diagnostics were not all detected.");
    if (!["app", "tests"].every((project) => entry.coverage[project].exitCode === 0 && entry.coverage[project].projectFiles.length > 0 && entry.sourceCoverageMatchesBaseline[project] === true)) entry.disqualificationReasons.push("Project source-file coverage differs from the untouched baseline or could not be read.");
    if (!["app", "tests"].every((project) => entry.dependencyMigration[project]?.accountedFor === true)) entry.disqualificationReasons.push("Dependency declaration/source-file coverage differs from the untouched baseline without an explained version mapping or byte-identical package deduplication.");
    if (Object.values(entry.phases).some((phase) => Object.values(phase).some((run) => run.error || run.signal))) entry.disqualificationReasons.push("A compiler process failed or was interrupted.");
    if (entry.oracle && entry.oracle !== "baseline-normalized") {
      if (!entry.oracleComparison || !Object.values(entry.oracleComparison).every((phase) => Object.values(phase).every(Boolean))) entry.disqualificationReasons.push("Full diagnostic sets differ from the matching-version official oracle.");
      if (!["app", "tests"].every((project) => entry.oracleDependencyCoverageMatches?.[project] === true)) entry.disqualificationReasons.push("Dependency declaration/source-file coverage differs from the matching-version official oracle.");
      if (!oracle?.cleanValidRestoredPass && !["clean", "valid", "restored"].every((phase) => ["app", "tests"].every((project) => oracle?.phases?.[phase]?.[project]?.exitCode === 0))) entry.disqualificationReasons.push("Matching-version oracle did not pass reference phases.");
    }
    entry.targetMigrationVerified = ["app", "tests"].every((project) => {
      const run = entry.phases.target[project];
      return id === "baseline" && project === "app"
        ? run.exitCode !== 0 && run.exitCode !== null && run.diagnostics.length === 2 && run.diagnostics.some((diagnostic) => diagnostic.code === 18028) && run.diagnostics.some((diagnostic) => diagnostic.code === 2802)
        : run.exitCode === 0 && run.diagnostics.length === 0;
    });
    if (!entry.targetMigrationVerified) entry.disqualificationReasons.push("Expected ES5-to-ES2015 target boundary was not observed.");
    entry.importedTypeChangeDetected = entry.phases.propagated ? ["app", "tests"].every((project) => {
      const run = entry.phases.propagated[project];
      return run.exitCode !== null && run.exitCode !== 0 && run.diagnostics.length === 1 && run.diagnostics[0].code === 2322 && run.diagnostics[0].file.endsWith(project === "app" ? "compiler-incremental-consumer.ts" : "compiler-equivalence-probe.test.tsx");
    }) : null;
    if (entry.importedTypeChangeDetected !== true) entry.disqualificationReasons.push("Changed imported type did not produce the expected error in an unchanged consumer, or the check has not run.");
    entry.eligibleForMeasurement = entry.disqualificationReasons.length === 0;
  }
  writeJson(path.join(output, "correctness.json"), results);
  if (!results.candidates.baseline?.eligibleForMeasurement) process.exitCode = 1;
  return results;
}

function metadata() {
  return {
    node: process.version,
    platform: process.platform,
    arch: process.arch,
    cpuCount: os.cpus().length,
    availableParallelism: os.availableParallelism(),
    memoryBytes: os.totalmem(),
    frontend,
    tools,
    timestamp: new Date().toISOString(),
    sourceInputsSha256: sourceInputsHash(),
    compilerManifestSha256: hash(fs.readFileSync(path.join(tools, "package.json"))),
    elapsedClock: "performance.now monotonic wall clock; child process startup included",
    rssMethod: process.platform === "linux" && fs.existsSync(timeExe) ? `${rssLabel} (${timeExe}) maximum resident set size per app/test invocation (KiB); paired result uses maximum, not sum` : "Not sampled (RSS wrapper unavailable or non-Linux platform)",
  };
}

function sourceInputsHash() {
  const files = ["package.json", "package-lock.json", "tsconfig.json", "tsconfig.tests.json", "vitest.setup.ts"];
  function visit(relative) {
    for (const entry of fs.readdirSync(path.join(frontend, relative), { withFileTypes: true })) {
      const child = `${relative}/${entry.name}`;
      if (entry.isDirectory()) visit(child);
      else if (entry.isFile()) files.push(child);
    }
  }
  visit("client/src");
  const digest = createHash("sha256");
  for (const filename of files.sort()) digest.update(filename).update("\0").update(fs.readFileSync(path.join(frontend, filename))).update("\0");
  return digest.digest("hex");
}

function describe(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return { median: sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2, min: sorted[0], max: sorted.at(-1), samples: values };
}

function benchmark() {
  if (rounds < 5 || !Number.isInteger(rounds)) throw new Error("Use at least five integer measurement rounds.");
  const correctnessFile = path.resolve(args.correctness ?? path.join(output, "correctness.json"));
  if (!fs.existsSync(correctnessFile)) throw new Error("Run correctness first, or pass --correctness pointing to its captured JSON.");
  const correctnessResults = JSON.parse(fs.readFileSync(correctnessFile, "utf8"));
  if (correctnessResults.metadata.sourceInputsSha256 !== sourceInputsHash()) throw new Error("Compiler correctness proof belongs to different source/config/dependency inputs. Run correctness again.");
  if (correctnessResults.metadata.compilerManifestSha256 !== hash(fs.readFileSync(path.join(tools, "package.json")))) throw new Error("Compiler candidate pins changed after correctness. Run correctness again.");
  const benchmarkCandidates = selected.filter((candidate) => !candidate.referenceOnly);
  const results = { metadata: metadata(), rounds, order: [], candidates: {} };
  for (const candidate of benchmarkCandidates) {
    prepare(candidate);
    const version = versions(candidate);
    if (typeof version === "object" && !version.available) {
      results.candidates[candidate.id] = { version, status: "unavailable" };
      continue;
    }
    const proof = correctnessResults.candidates[candidate.id];
    if (!proof?.eligibleForMeasurement) {
      results.candidates[candidate.id] = { version, status: "disqualified", reasons: proof?.disqualificationReasons ?? ["No passed correctness record."] };
      continue;
    }
    results.candidates[candidate.id] = { version, measurements: { cold: [], warm: [], changed: [] } };
    cleanCache(candidate);
    for (const project of ["app", "tests"]) {
      const warmup = execute(command(candidate, project), `${candidate.id}.${project}.warmup`);
      if (warmup.exitCode !== 0) {
        Object.assign(results.candidates[candidate.id], { status: "disqualified", reasons: [`Warmup failed; see ${warmup.log}`], failure: summarizeRun(warmup) });
        break;
      }
    }
  }
  // Add a small public export in a real imported module so cached dependency
  // consumers must observe a changed public shape. The production source is
  // restored before the next candidate and in finally.
  const changedFile = path.join(frontend, "client/src/lib/utils.ts");
  const originalText = fs.readFileSync(changedFile, "utf8");
  const runnable = benchmarkCandidates.filter((candidate) => !results.candidates[candidate.id].status);
  try {
    for (let round = 0; round < rounds; round++) {
      const offset = round % runnable.length;
      let order = [...runnable.slice(offset), ...runnable.slice(0, offset)];
      if (round % 2) order = order.reverse();
      results.order.push(order.map((candidate) => candidate.id));
      for (const candidate of order) {
        if (results.candidates[candidate.id].status) continue;
        fs.writeFileSync(changedFile, originalText);
        for (const phase of ["cold", "warm", "changed"]) {
          if (phase === "cold") cleanCache(candidate);
          if (phase === "changed") fs.writeFileSync(changedFile, `${originalText}\nexport const compilerBenchmarkProbe = ${round + 1};\n`);
          const pair = [];
          for (const project of ["app", "tests"]) {
            const run = execute(command(candidate, project), `${candidate.id}.${project}.${phase}.${round}`, true);
            pair.push(summarizeRun(run));
            if (run.exitCode !== 0) {
              Object.assign(results.candidates[candidate.id], { status: "disqualified", reasons: [`${phase} round ${round + 1} failed; see ${run.log}`], failure: summarizeRun(run) });
              break;
            }
          }
          if (results.candidates[candidate.id].status) break;
          results.candidates[candidate.id].measurements[phase].push({
            round,
            elapsedMs: pair.reduce((total, run) => total + run.elapsedMs, 0),
            maxRssKiB: pair.some((run) => run.maxRssKiB === null) ? null : Math.max(...pair.map((run) => run.maxRssKiB)),
            app: pair[0],
            tests: pair[1],
          });
          process.stdout.write(`round ${round + 1} ${phase}: ${candidate.id}\n`);
          writeJson(path.join(output, "benchmark.json"), results);
        }
      }
    }
  } finally {
    fs.writeFileSync(changedFile, originalText);
  }
  for (const candidate of runnable) {
    const entry = results.candidates[candidate.id];
    entry.summary = Object.fromEntries(Object.entries(entry.measurements).map(([phase, observations]) => [phase, {
      elapsedMs: observations.length ? describe(observations.map((observation) => observation.elapsedMs)) : null,
      maxRssKiB: !observations.length || observations.some((observation) => observation.maxRssKiB === null) ? null : describe(observations.map((observation) => observation.maxRssKiB)),
    }]));
  }
  writeJson(path.join(output, "benchmark.json"), results);
  if (["cold", "warm", "changed"].some((phase) => results.candidates.baseline?.measurements?.[phase]?.length !== rounds)) process.exitCode = 1;
  return results;
}

assertDisposable();
try {
  if (mode === "correctness") correctness();
  else if (mode === "benchmark") benchmark();
  else if (mode === "report") summarizeCorrectness(JSON.parse(fs.readFileSync(path.join(output, "correctness.json"), "utf8")));
  else if (mode === "prepare") for (const candidate of selected) prepare(candidate);
  else throw new Error(`Unknown mode ${mode}`);
} finally {
  removeSentinels();
}
