import { spawn } from "node:child_process";
import { globSync } from "node:fs";
import path from "node:path";

const frontend = path.resolve(import.meta.dirname, "..");
const testFiles = globSync("client/src/**/*.test.ts", { cwd: frontend })
  .map((file) => `./${file.replaceAll("\\", "/")}`)
  .sort();

if (testFiles.length === 0) {
  console.error("No logic test files matched client/src/**/*.test.ts.");
  process.exitCode = 1;
} else {
  // The local Bun package uses this native executable path on every platform.
  const bun = path.join(frontend, "node_modules", "bun", "bin", "bun.exe");
  const child = spawn(bun, ["test", "--isolate", "--parallel=4", ...process.argv.slice(2), ...testFiles], {
    cwd: frontend,
    stdio: "inherit",
  });

  let launchFailed = false;
  child.once("error", (error) => {
    launchFailed = true;
    console.error(`Unable to start the local Bun test runner: ${error.message}`);
    process.exitCode = 1;
  });
  child.once("close", (code, signal) => {
    if (signal) {
      console.error(`The Bun test runner terminated with ${signal}.`);
    }
    process.exitCode = launchFailed || signal ? 1 : (code ?? 1);
  });
}
