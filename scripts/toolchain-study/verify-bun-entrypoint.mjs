/**
 * Verify the Bun 1.4.3 production entrypoint against the exhaustive checker
 * reference captured by compilers.mjs. Always use its disposable Frontend copy.
 *
 * Usage: node verify-bun-entrypoint.mjs DISPOSABLE_FRONTEND RESULTS BUN_EXECUTABLE
 *
 * The no-entrypoint form bypasses package scripts.check. Bun 1.4.3 dispatches
 * it to CheckCommand::exec_without_arguments and takes its project from the
 * tsconfig override:
 * https://github.com/oven-sh/bun/blob/bun-v1.4.3/src/runtime/cli/mod.rs#L1487
 * https://github.com/oven-sh/bun/blob/bun-v1.4.3/src/runtime/cli/check_command.rs#L588
 * That route uses default diagnostic grouping; --all and --no-pretty are not
 * forwarded. Piped output is plain. The benchmark retains the exhaustive
 * `bun check --all --no-pretty -p CONFIG` route from a package without check.
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const [frontendArg, outputArg, bunArg] = process.argv.slice(2);
if (!frontendArg || !outputArg || !bunArg) {
  throw new Error("Usage: node verify-bun-entrypoint.mjs DISPOSABLE_FRONTEND RESULTS BUN_EXECUTABLE");
}
const frontend = path.resolve(frontendArg);
const output = path.resolve(outputArg);
const bun = path.resolve(bunArg);
const referenceText = fs.readFileSync(path.join(output, "correctness.json"), "utf8");
const reference = JSON.parse(referenceText).candidates["bun-check"];
if (!reference?.eligibleForMeasurement) {
  throw new Error("A passed exhaustive Bun check reference is required.");
}
const manifest = JSON.parse(fs.readFileSync(path.join(frontend, "package.json"), "utf8"));
if (!manifest.scripts?.check) {
  throw new Error("The disposable project must contain scripts.check to exercise command dispatch.");
}
const sourceText = fs.readFileSync(path.join(here, "compilers.mjs"), "utf8");
const source = sourceText.replaceAll("\r\n", "\n");
// Read the existing fixture literals as data, without evaluating script text.
// Their only substitutions are the two explicit valid-corpus insertions below.
// Normalize checkout line endings as JavaScript does for template literals.
function fixture(name) {
  const match = source.match(new RegExp("const " + name + " = `([\\s\\S]*?)`;\\n"));
  if (!match) throw new Error(`Missing compiler fixture: ${name}`);
  return match[1];
}
const validApp = fixture("validApp");
const validTests = fixture("validTests");
const invalidApp = fixture("invalidApp").replace("${validApp}", validApp);
const invalidTests = fixture("invalidTests").replace("${validTests}", validTests);
const probePaths = [
  "compiler-equivalence-probe.tsx",
  "compiler-equivalence-probe.test.tsx",
  "compiler-incremental-consumer.ts",
].map((name) => path.join(frontend, "client/src", name));
for (const filename of probePaths) {
  if (fs.existsSync(filename)) throw new Error(`Refusing to overwrite ${filename}`);
}
const consumer = 'import { propagationProbe } from "./compiler-equivalence-probe";\nexport const unchangedConsumer: number = propagationProbe.value;\n';
const phases = ["clean", "valid", "propagated", "invalid", "restored"];
const projects = ["app", "tests"];
for (const phase of phases) {
  for (const project of projects) {
    if (!reference.phases?.[phase]?.[project]) throw new Error(`Missing reference phase: ${phase}/${project}`);
  }
}

function diagnostics(text) {
  const parsed = [];
  let active = false;
  const lines = text.replaceAll(/\x1b\[[0-9;]*m/g, "").replaceAll("\r\n", "\n").split("\n");
  for (const line of lines) {
    const match = line.match(/^(.*?)(?:\((\d+),(\d+)\))?: (error|warning) TS(\d+): (.*)$/);
    const plain = line.match(/^(error|warning) TS(\d+): (.*)$/);
    if (match) {
      let filename = path.relative(frontend, path.resolve(frontend, match[1])).replaceAll("\\", "/");
      if (process.platform === "win32") filename = filename.toLowerCase();
      parsed.push({ file: filename, line: Number(match[2] ?? 0), column: Number(match[3] ?? 0), category: match[4], code: Number(match[5]), message: match[6] });
      active = true;
    } else if (plain) {
      parsed.push({ file: "", line: 0, column: 0, category: plain[1], code: Number(plain[2]), message: plain[3] });
      active = true;
    } else if (active && /^\s+\S/.test(line)) {
      parsed.at(-1).message += `\n${line.trimEnd()}`;
    } else {
      // The count-per-file footer is separate from the final diagnostic.
      active = false;
    }
  }
  return parsed.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

const sha256 = (text) => createHash("sha256").update(text).digest("hex");
const results = {
  node: process.version,
  platform: process.platform,
  frontend,
  invocation: "bun --check --tsconfig-override CONFIG (no entrypoint; project cwd includes scripts.check)",
  compilerHarnessSha256: sha256(sourceText),
  correctnessReferenceSha256: sha256(referenceText),
  referenceVersion: reference.version,
  phases: {},
  allEquivalent: false,
};
let completed = 0;
try {
  for (const phase of phases) {
    if (phase === "restored") {
      for (const filename of probePaths) fs.rmSync(filename, { force: true });
    }
    if (["valid", "propagated", "invalid"].includes(phase)) {
      const appText = phase === "propagated"
        ? validApp.replace("export const propagationProbe = { value: 1 };", 'export const propagationProbe = { value: "changed" };')
        : phase === "invalid" ? invalidApp : validApp;
      fs.writeFileSync(probePaths[0], appText);
      if (phase !== "propagated") {
        fs.writeFileSync(probePaths[1], phase === "invalid" ? invalidTests : validTests);
        fs.writeFileSync(probePaths[2], consumer);
      }
    }
    results.phases[phase] = {};
    for (const project of projects) {
      const args = ["--check", "--tsconfig-override", path.join(frontend, `tsconfig.compiler-bun-check.${project}.json`)];
      const run = spawnSync(bun, args, {
        cwd: frontend,
        encoding: "utf8",
        timeout: 30000,
        killSignal: "SIGKILL",
        maxBuffer: 32 * 1024 * 1024,
        windowsHide: true,
        env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
      });
      const text = `${run.stdout ?? ""}${run.stderr ?? ""}`;
      const parsed = diagnostics(text);
      const expected = reference.phases[phase][project];
      const scriptDispatchDetected = /\b(?:bun|npm|pnpm)(?:\.exe)?\s+(?:run\s+)?check:(?:app|tests)\b/.test(text);
      const equivalent = !run.error && !run.signal && run.status === expected.exitCode
        && JSON.stringify(parsed) === JSON.stringify(expected.diagnostics) && !scriptDispatchDetected;
      const log = `bun-global.${project}.${phase}.log`;
      fs.writeFileSync(path.join(output, log), text);
      results.phases[phase][project] = {
        command: [bun, ...args],
        exitCode: run.error || run.signal ? null : run.status,
        error: run.error?.message ?? null,
        signal: run.signal,
        diagnostics: parsed,
        equivalent,
        scriptDispatchDetected,
        log,
      };
      completed++;
      process.stdout.write(`${phase} ${project}: exit=${run.status}, diagnostics=${parsed.length}, equivalent=${equivalent}\n`);
      if (run.error?.code === "ETIMEDOUT") throw new Error("Bun global command timed out; lane aborted.");
    }
  }
  results.allEquivalent = completed === phases.length * projects.length
    && Object.values(results.phases).every((phase) => Object.values(phase).every((run) => run.equivalent));
} catch (error) {
  results.fatalError = error.message;
  throw error;
} finally {
  for (const filename of probePaths) fs.rmSync(filename, { force: true });
  fs.writeFileSync(path.join(output, "bun-entrypoint.json"), `${JSON.stringify(results, null, 2)}\n`);
}
if (!results.allEquivalent) process.exitCode = 1;
