import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "../..");
const probes = path.join(repo, "tmp/toolchain-study/test-lint-project/tmp/speed-test-lint");
const env = {
  ...process.env,
  SCRIBER_BUN_EXE: path.join(import.meta.dirname, "runtime-tools/node_modules/bun/bin/bun.exe"),
  SCRIBER_LINT_TOOLS: path.join(import.meta.dirname, "lint-tools"),
  SCRIBER_SOURCE_REVISION: "d061ad75a0b39ca1b299eb8174ac828b122ef136",
  ...(process.platform === "linux" ? { SCRIBER_TIME_EXE: process.env.SCRIBER_TIME_EXE ?? "/usr/bin/time" } : {}),
};
for (const name of ["lint-parity", "runner-parity", "parallel-failure-parity", "cache-parity", "formatter-parity", "benchmark-candidates"]) {
  const diagnosticResult = name.endsWith("-parity") ? path.join(probes, `${name}-results.json`) : null;
  if (diagnosticResult) rmSync(diagnosticResult, { force: true });
  const args = [path.join(probes, `${name}.mjs`), ...(diagnosticResult ? [] : ["--rounds=5", "--groups=logic,components,lint,build", `--output=${path.join(repo, "tmp/toolchain-study/results/test-lint")}`])];
  const result = spawnSync(process.execPath, args, { cwd: repo, env, stdio: "inherit" });
  if (result.error || result.signal || (result.status !== 0 && (!diagnosticResult || !existsSync(diagnosticResult)))) throw new Error(`Study command failed: ${args[0]} (${result.error?.message ?? result.signal ?? result.status})`);
  if (result.status !== 0) console.log(`${name}: recorded failed alternatives; the benchmark will apply candidate-specific correctness gates.`);
}
