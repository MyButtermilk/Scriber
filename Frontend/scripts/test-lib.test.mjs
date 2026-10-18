import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { copyFile, link, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";

const frontend = path.resolve(import.meta.dirname, "..");
const wrapper = await readFile(path.join(frontend, "scripts/test-lib.mjs"));
const bun = path.join(frontend, "node_modules/bun/bin/bun.exe");
const windows = process.platform === "win32";
const signals = ["SIGINT", "SIGTERM"];

// This temporary observer delegates to native spawn without changing its
// arguments or result. IPC is unreferenced, so it cannot keep the wrapper alive.
// Windows cannot deliver POSIX signals to a parent: those cases explicitly
// inject Node's signal event while still terminating the real Bun child.
const observer = `
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
const send = (message) => {
  if (process.connected) process.send(message, () => {});
};
const nativeSpawn = childProcess.spawn;
childProcess.spawn = (...args) => {
  const child = nativeSpawn(...args);
  send({ kind: "spawn", pid: child.pid, argv: args[1] });
  child.once("exit", (code, signal) => send({ kind: "exit", pid: child.pid, code, signal }));
  child.once("close", () => queueMicrotask(() => send({
    kind: "closed",
    listeners: [process.listenerCount("SIGINT"), process.listenerCount("SIGTERM")],
  })));
  return child;
};
syncBuiltinESMExports();
if (process.platform === "win32") {
  process.on("message", ({ signal }) => {
    if (["SIGINT", "SIGTERM"].includes(signal)) {
      send({ kind: "injected", signal, handled: process.emit(signal, signal) });
    }
  });
}
process.channel.unref();
`;

const heldTest = `
import { test } from "node:test";
import { renameSync, writeFileSync } from "node:fs";
test("owned cancellation fixture", { timeout: 30000 }, async () => {
  if (process.env.SCRIBER_TEST_LIB_GRACEFUL === "1") {
    for (const signal of ["SIGINT", "SIGTERM"]) {
      process.on(signal, () => {
        writeFileSync("stopped.txt", signal);
        process.exit(0);
      });
    }
  }
  writeFileSync("ready.tmp", JSON.stringify({ pid: process.pid, bun: process.versions.bun }));
  renameSync("ready.tmp", "ready.json");
  await new Promise(resolve => setTimeout(resolve, 20000));
});
`;

async function within(promise, description) {
  let timeout;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error(`Timed out: ${description}`)), 5000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

function killOwned(pid) {
  if (!pid) return;
  try {
    process.kill(pid, "SIGKILL");
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}

async function inProject(t, files, check, { installBun = true } = {}) {
  const cache = path.join(frontend, "node_modules/.cache");
  await mkdir(cache, { recursive: true });
  const root = await mkdtemp(path.join(cache, "test-lib-"));
  const runs = [];
  t.after(async () => {
    try {
      for (const run of runs) {
        if (!run.messages.some((message) => message.kind === "exit")) {
          killOwned(run.messages.find((message) => message.kind === "spawn")?.pid);
        }
        if (run.child.exitCode === null && run.child.signalCode === null) run.child.kill("SIGKILL");
        await within(run.closed, `cleanup of owned wrapper ${run.child.pid}`);
      }
    } finally {
      await rm(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
    }
  });
  await mkdir(path.join(root, "scripts"));
  await mkdir(path.join(root, "client/src"), { recursive: true });
  await writeFile(path.join(root, "scripts/test-lib.mjs"), wrapper);
  await writeFile(path.join(root, "observer.mjs"), observer);
  for (const [relative, source] of Object.entries(files)) {
    const filename = path.join(root, relative);
    await mkdir(path.dirname(filename), { recursive: true });
    await writeFile(filename, source);
  }
  if (installBun) {
    const executable = path.join(root, "node_modules/bun/bin/bun.exe");
    await mkdir(path.dirname(executable), { recursive: true });
    // The cache normally shares Bun's filesystem. Copy only when hard links
    // are unavailable; no Windows symlink privilege is required.
    await link(bun, executable).catch(async (error) => {
      if (!["EXDEV", "EPERM", "ENOTSUP"].includes(error.code)) throw error;
      await copyFile(bun, executable);
    });
  }
  const start = (args = [], env = {}) => {
    const child = spawn(
      process.execPath,
      [
        "--import",
        pathToFileURL(path.join(root, "observer.mjs")).href,
        path.join(root, "scripts/test-lib.mjs"),
        ...args,
      ],
      {
        cwd: root,
        env: { ...process.env, ...env },
        stdio: ["ignore", "pipe", "pipe", "ipc"],
      },
    );
    const run = { child, messages: [], output: "" };
    for (const stream of [child.stdout, child.stderr]) {
      stream.setEncoding("utf8");
      stream.on("data", (chunk) => {
        run.output += chunk;
      });
    }
    run.spawned = new Promise((resolve) => {
      child.on("message", (message) => {
        run.messages.push(message);
        if (message.kind === "spawn") resolve(message);
      });
    });
    run.exited = new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => resolve({ code, signal }));
    });
    run.closed = new Promise((resolve) => child.once("close", resolve));
    runs.push(run);
    return run;
  };
  try {
    await check({ root, start });
  } catch (error) {
    for (const run of runs) {
      t.diagnostic(JSON.stringify({ messages: run.messages, output: run.output }));
    }
    throw error;
  }
}

async function ready(root) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try {
      return JSON.parse(await readFile(path.join(root, "ready.json"), "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    await delay(10);
  }
  throw new Error("Timed out: Bun fixture readiness");
}

async function stopParent(run, signal) {
  if (windows) {
    await new Promise((resolve, reject) => {
      run.child.send({ signal }, (error) => (error ? reject(error) : resolve()));
    });
  } else {
    assert.equal(run.child.kill(signal), true);
  }
}

test("logic wrapper preserves file scope, spaced paths, and forwarded test-name arguments", async (t) => {
  await inProject(
    t,
    {
      "client/src/nested/with space.test.ts": `import { test } from "node:test";
      import { writeFileSync } from "node:fs";
      test("selected first", () => writeFileSync("first.txt", "passed"));
      test("excluded by name", () => { throw new Error("name filter lost"); });`,
      "client/src/second.test.ts": `import { test } from "node:test";
      import { writeFileSync } from "node:fs";
      test("selected second", () => writeFileSync("second.txt", "passed"));`,
      "client/src/decoy.test.tsx": `throw new Error("component scope leaked");`,
      "client/src/decoy.spec.ts": `throw new Error("spec scope leaked");`,
    },
    async ({ root, start }) => {
      const run = start(["--test-name-pattern", "selected"]);
      assert.deepEqual(await within(run.exited, "successful wrapper exit"), { code: 0, signal: null });
      await within(run.closed, "successful wrapper closure");
      assert.equal(await readFile(path.join(root, "first.txt"), "utf8"), "passed");
      assert.equal(await readFile(path.join(root, "second.txt"), "utf8"), "passed");
      assert.deepEqual(run.messages.find((message) => message.kind === "closed")?.listeners, [0, 0]);
    },
  );
});

test("logic wrapper preserves a real Bun test failure", async (t) => {
  await inProject(
    t,
    {
      "client/src/failure.test.ts": `import { test } from "node:test";
      test("expected failure", () => { throw new Error("owned logic failure"); });`,
    },
    async ({ start }) => {
      const run = start();
      assert.deepEqual(await within(run.exited, "failing wrapper exit"), { code: 1, signal: null });
      await within(run.closed, "failing wrapper closure");
      assert.match(run.output, /owned logic failure/);
    },
  );
});

for (const empty of [true, false]) {
  test(`logic wrapper rejects ${empty ? "empty discovery" : "a missing Bun executable"}`, async (t) => {
    await inProject(
      t,
      empty ? {} : { "client/src/exists.test.ts": "" },
      async ({ start }) => {
        const run = start();
        assert.deepEqual(await within(run.exited, "discovery/launch failure"), { code: 1, signal: null });
        await within(run.closed, "discovery/launch failure closure");
        assert.match(run.output, empty ? /No logic test files matched/ : /Unable to start the local Bun test runner/);
        if (empty)
          assert.equal(
            run.messages.some((message) => message.kind === "spawn"),
            false,
          );
        else assert.deepEqual(run.messages.find((message) => message.kind === "closed")?.listeners, [0, 0]);
      },
      { installBun: false },
    );
  });
}

for (const signal of signals) {
  for (const graceful of [false, true]) {
    const mode = windows ? "Windows signal-event injection" : "native POSIX signal";
    test(
      `logic wrapper joins Bun on ${signal} (${mode}${graceful ? ", graceful child exit 0" : ""})`,
      {
        skip: graceful && windows ? "Windows child.kill force-terminates; no graceful POSIX handler claim" : false,
      },
      async (t) => {
        await inProject(t, { "client/src/hold.test.ts": heldTest }, async ({ root, start }) => {
          const run = start(["--timeout", "30000"], { SCRIBER_TEST_LIB_GRACEFUL: graceful ? "1" : "0" });
          const launched = await within(run.spawned, "Bun launch observation");
          const fixture = await ready(root);
          assert.equal(fixture.pid, launched.pid);
          assert.equal(fixture.bun, "1.4.3");
          await stopParent(run, signal);
          assert.deepEqual(await within(run.exited, "cancelled wrapper exit"), { code: 1, signal: null });
          await within(run.closed, "cancelled wrapper closure");
          const childExit = run.messages.find((message) => message.kind === "exit");
          assert.ok(childExit, "the real Bun child must exit before its wrapper");
          assert.equal(childExit.pid, fixture.pid);
          assert.deepEqual(run.messages.find((message) => message.kind === "closed")?.listeners, [0, 0]);
          if (graceful) {
            assert.deepEqual({ code: childExit.code, signal: childExit.signal }, { code: 0, signal: null });
            assert.equal(await readFile(path.join(root, "stopped.txt"), "utf8"), signal);
          } else {
            assert.equal(childExit.signal, signal);
          }
          if (windows) {
            assert.deepEqual(
              run.messages.find((message) => message.kind === "injected"),
              {
                kind: "injected",
                signal,
                handled: true,
              },
            );
          }
        });
      },
    );
  }
}
