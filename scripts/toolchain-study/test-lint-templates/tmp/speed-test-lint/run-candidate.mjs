import { globSync } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { oxlintArgs } from "./lint-file-inventory.mjs";

// Experimental harness. Every candidate retains the complete existing file set.
const frontend = path.resolve(import.meta.dirname, "../../Frontend");
const toolRoot = process.env.SCRIBER_LINT_TOOLS ?? path.resolve(frontend, "../../lint-tools");
const bun = process.env.SCRIBER_BUN_EXE ?? "bun";
const candidate = process.argv[2];
const extraArgs = process.argv.slice(3);
const bin = (relative) => path.join(frontend, "node_modules", relative);
const logicFiles = globSync("client/src/**/*.test.ts", { cwd: frontend })
  .sort()
  .map((file) => `./${file.replaceAll(path.sep, "/")}`);
const vitest = (runtime, pool, workers) => [
  runtime,
  [bin("vitest/vitest.mjs"), "run", "--config", "vitest.components.config.ts", "--isolate", "--pool", pool,
    ...(workers ? ["--maxWorkers", workers] : []), ...extraArgs],
];

let command;
if (candidate === "lib-node") {
  command = [process.execPath, [bin("tsx/dist/cli.mjs"), "--test", ...extraArgs, ...logicFiles]];
} else if (candidate === "lib-bun-isolated") {
  command = [bun, ["test", "--isolate", ...extraArgs, ...logicFiles]];
} else if (/^lib-bun-parallel-\d+$/.test(candidate)) {
  command = [bun, ["test", "--isolate", `--parallel=${candidate.split("-").at(-1)}`, ...extraArgs, ...logicFiles]];
} else if (candidate === "components-original") {
  command = [process.execPath, [bin("vitest/vitest.mjs"), "run", "--config", "vitest.components.config.ts", ...extraArgs]];
} else if (/^components-(node|bun)-(forks|threads)-(default|\d+)$/.test(candidate)) {
  const [, runtime, pool, workers] = candidate.split("-");
  command = vitest(runtime === "bun" ? bun : process.execPath, pool, workers === "default" ? undefined : workers);
} else if (candidate === "build-node" || candidate === "build-bun") {
  command = [candidate === "build-bun" ? bun : process.execPath, [bin("vite/bin/vite.js"), "build", ...extraArgs]];
} else if (candidate === "lint-eslint" || candidate === "lint-eslint-cached") {
  command = [process.execPath, [bin("eslint/bin/eslint.js"), ".", "--max-warnings", "0", ...(candidate.endsWith("-cached") ? ["--cache", "--cache-strategy", "content", "--cache-location", "node_modules/.cache/eslint-speed"] : []), ...extraArgs]];
} else if (candidate === "lint-oxlint-native") {
  command = [process.execPath, [path.join(toolRoot, "node_modules/oxlint/bin/oxlint"), ...oxlintArgs(frontend, ".oxlintrc.json", extraArgs)]];
} else if (candidate === "lint-oxlint-retained-hooks") {
  command = [process.execPath, [path.join(toolRoot, "node_modules/oxlint/bin/oxlint"), ...oxlintArgs(frontend, ".oxlintrc.retained-hooks.json", extraArgs)]];
} else if (candidate === "lint-oxlint-hybrid" || candidate === "lint-oxlint-hybrid-cached") {
  const native = spawn(process.execPath, [path.join(toolRoot, "node_modules/oxlint/bin/oxlint"), ...oxlintArgs(frontend, ".oxlintrc.residual.json", extraArgs)], { cwd: frontend, stdio: "inherit", env: process.env });
  const code = await new Promise((resolve, reject) => { native.on("error", reject); native.on("close", (status, signal) => resolve(signal ? 1 : status ?? 1)); });
  if (code !== 0) process.exit(code);
  command = [process.execPath, [bin("eslint/bin/eslint.js"), ".", "--config", "eslint.residual.config.js", "--max-warnings", "0", ...(candidate.endsWith("-cached") ? ["--cache", "--cache-strategy", "content", "--cache-location", "node_modules/.cache/eslint-residual-speed"] : []), ...extraArgs]];
} else if (candidate === "format-prettier") {
  command = [process.execPath, [bin("prettier/bin/prettier.cjs"), "--check", ".", ...extraArgs]];
} else if (candidate === "format-oxfmt") {
  command = [process.execPath, [path.join(toolRoot, "node_modules/oxfmt/bin/oxfmt"), "--check", ".", ...extraArgs]];
} else if (candidate === "format-bun") {
  command = [bun, ["fmt", "--check", ".", ...extraArgs]];
} else {
  throw new Error(`Unknown experimental candidate: ${candidate}`);
}

let launchFailed = false;
const child = spawn(command[0], command[1], { cwd: frontend, stdio: "inherit", env: process.env });
child.on("error", (error) => { launchFailed = true; console.error(error); process.exitCode = 1; });
child.on("close", (code, signal) => { process.exitCode = launchFailed || signal ? 1 : code ?? 1; });
