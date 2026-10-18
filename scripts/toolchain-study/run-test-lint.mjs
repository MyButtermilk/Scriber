import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "../..");
const probes = path.join(repo, "tmp/toolchain-study/test-lint-project/tmp/speed-test-lint");
const arguments_ = process.argv.slice(2);
if (arguments_.some((argument) => !argument.startsWith("--groups=")) || arguments_.length > 1) throw new Error("Usage: node run-test-lint.mjs [--groups=logic,components,lint,build]");
const groups = (arguments_[0]?.slice("--groups=".length) ?? "logic,components,lint,build").split(",");
if (groups.length === 0 || new Set(groups).size !== groups.length || groups.some((group) => !["logic", "components", "lint", "build"].includes(group))) throw new Error("Unknown or duplicate benchmark group");
const env = {
  ...process.env,
  SCRIBER_BUN_EXE: path.join(import.meta.dirname, "runtime-tools/node_modules/bun/bin/bun.exe"),
  SCRIBER_LINT_TOOLS: path.join(import.meta.dirname, "lint-tools"),
  SCRIBER_SOURCE_REVISION: "d061ad75a0b39ca1b299eb8174ac828b122ef136",
  ...(process.platform === "linux" ? { SCRIBER_TIME_EXE: process.env.SCRIBER_TIME_EXE ?? "/usr/bin/time" } : {}),
};
const commands = [];
if (groups.includes("lint")) commands.push("lint-parity", "lint-scope-parity", "cache-parity");
if (groups.some((group) => group === "logic" || group === "components")) commands.push("runner-parity");
if (groups.includes("logic")) commands.push("parallel-failure-parity");
if (groups.includes("build")) commands.push("formatter-parity");
commands.push("benchmark-candidates");
for (const name of commands) {
  const diagnosticResult = name.endsWith("-parity") ? path.join(probes, `${name}-results.json`) : null;
  if (diagnosticResult) rmSync(diagnosticResult, { force: true });
  const args = [path.join(probes, `${name}.mjs`), ...(diagnosticResult ? [] : ["--rounds=5", `--groups=${groups.join(",")}`, `--output=${path.join(repo, "tmp/toolchain-study/results/test-lint")}`])];
  const result = spawnSync(process.execPath, args, { cwd: repo, env, stdio: "inherit" });
  if (result.error || result.signal || (result.status !== 0 && (!diagnosticResult || !existsSync(diagnosticResult)))) throw new Error(`Study command failed: ${args[0]} (${result.error?.message ?? result.signal ?? result.status})`);
  if (result.status !== 0) console.log(`${name}: recorded failed alternatives; the benchmark will apply candidate-specific correctness gates.`);
}
