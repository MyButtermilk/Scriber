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
  let child = null;
  let launchFailed = false;
  let cancelled = false;
  const forwardSignal = (signal) => {
    cancelled = true;
    // Keep supervising Bun until it closes, even when only this wrapper was
    // signalled. A child that handles cancellation and exits 0 still failed
    // to complete the requested test run.
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill(signal);
    }
  };
  process.on("SIGINT", forwardSignal);
  process.on("SIGTERM", forwardSignal);

  child = spawn(bun, ["test", "--isolate", "--parallel=4", ...process.argv.slice(2), ...testFiles], {
    cwd: frontend,
    stdio: "inherit",
  });
  child.once("error", (error) => {
    launchFailed = true;
    console.error(`Unable to start the local Bun test runner: ${error.message}`);
    process.exitCode = 1;
  });
  child.once("close", (code, signal) => {
    process.off("SIGINT", forwardSignal);
    process.off("SIGTERM", forwardSignal);
    if (signal) {
      console.error(`The Bun test runner terminated with ${signal}.`);
    }
    process.exitCode = launchFailed || cancelled || signal ? 1 : (code ?? 1);
  });
}
