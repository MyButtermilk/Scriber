import { createContext, Script } from "node:vm";

const WRAPPER_PROTOCOL = "ScriberYtDlpDenoStdinV1";
const RUNTIME = Deno;
const MAX_STDIN_BYTES = 32 * 1024 * 1024;
const REQUIRED_OPTIONS = Object.freeze([
  "--ext=js",
  "--no-code-cache",
  "--no-prompt",
  "--no-remote",
  "--no-lock",
  "--node-modules-dir=none",
  "--no-config",
  "--cached-only",
]);
const NORMAL_ONLY_OPTION = "--no-npm";

function fail(message: string, code = 64): never {
  console.error(`${WRAPPER_PROTOCOL}: ${message}`);
  RUNTIME.exit(code);
}

async function readBoundedStdin(): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let total = 0;
  for await (const chunk of RUNTIME.stdin.readable) {
    total += chunk.byteLength;
    if (total > MAX_STDIN_BYTES) {
      fail("stdin limit exceeded", 65);
    }
    chunks.push(chunk.slice());
  }
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

function hideAmbientStorageAndWorkers(): void {
  for (const name of [
    "Deno",
    "localStorage",
    "sessionStorage",
    "caches",
    "Worker",
    "SharedWorker",
  ]) {
    Reflect.deleteProperty(globalThis, name);
    Object.defineProperty(globalThis, name, {
      value: undefined,
      writable: false,
      enumerable: false,
      configurable: false,
    });
  }
}

if (RUNTIME.args.length === 1 && RUNTIME.args[0] === "--version") {
  console.log("deno 2.9.2 (stable, release, x86_64-pc-windows-msvc)");
  console.log("v8 14.9.207.2-rusty");
  console.log("typescript 6.0.3");
  RUNTIME.exit(0);
}

if (RUNTIME.args.length < 3 || RUNTIME.args[0] !== "run" || RUNTIME.args.at(-1) !== "-") {
  fail("unsupported invocation");
}

const options = RUNTIME.args.slice(1, -1);
const allowed = new Set([...REQUIRED_OPTIONS, NORMAL_ONLY_OPTION]);
const seen = new Set<string>();
for (const option of options) {
  if (!allowed.has(option) || seen.has(option)) {
    fail("unsupported or duplicate option");
  }
  seen.add(option);
}
for (const option of REQUIRED_OPTIONS) {
  if (!seen.has(option)) {
    fail("required option missing");
  }
}

if (!seen.has(NORMAL_ONLY_OPTION)) {
  fail("npm cache unavailable", 1);
}

const inputBytes = await readBoundedStdin();
let input: string;
try {
  input = new TextDecoder("utf-8", { fatal: true }).decode(inputBytes);
} catch {
  fail("stdin is not valid UTF-8", 65);
}
hideAmbientStorageAndWorkers();
const context = createContext(
  {},
  {
    name: "scriber-youtube-ejs",
    codeGeneration: { strings: true, wasm: false },
  },
);
new Script(`
  (() => {
    const disabledConstructor = function () {
      throw new TypeError("constructor access is disabled");
    };
    const functionPrototypes = [
      Function.prototype,
      Object.getPrototypeOf(async function () {}),
      Object.getPrototypeOf(function* () {}),
      Object.getPrototypeOf(async function* () {}),
    ];
    for (const prototype of functionPrototypes) {
      Object.defineProperty(prototype, "constructor", {
        value: disabledConstructor,
        writable: false,
        enumerable: false,
        configurable: false,
      });
    }
    Object.setPrototypeOf(globalThis, null);
    Object.defineProperty(globalThis, "constructor", {
      value: null,
      writable: false,
      enumerable: false,
      configurable: false,
    });
    for (const name of ["Deno", "process", "require", "module", "exports"]) {
      Object.defineProperty(globalThis, name, {
        value: undefined,
        writable: false,
        enumerable: false,
        configurable: false,
      });
    }
    if (Function("return typeof Deno + '|' + typeof process")() !== "undefined|undefined") {
      throw new TypeError("context isolation failed");
    }
    const lines = [];
    const safeConsole = Object.freeze({
      log(value) {
        if (typeof value !== "string" || lines.length !== 0) {
          throw new TypeError("invalid solver output");
        }
        lines.push(value);
      },
    });
    Object.defineProperty(globalThis, "console", {
      value: safeConsole,
      writable: false,
      enumerable: false,
      configurable: false,
    });
    Object.defineProperty(globalThis, "__scriberTakeOutput", {
      value: () => lines.slice(),
      writable: false,
      enumerable: false,
      configurable: false,
    });
  })();
`, { filename: "scriber-youtube-bootstrap.js" }).runInContext(context);
const script = new Script(`(async () => { "use strict";\n${input}\n})()`, {
  filename: "scriber-youtube-ejs.js",
  importModuleDynamically: () => {
    throw new TypeError("dynamic imports are disabled");
  },
});
await script.runInContext(context);
const output = new Script("__scriberTakeOutput()", {
  filename: "scriber-youtube-output.js",
}).runInContext(context);
if (!Array.isArray(output) || output.length !== 1 || typeof output[0] !== "string") {
  fail("solver output is invalid", 66);
}
console.log(output[0]);
