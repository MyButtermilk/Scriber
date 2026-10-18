import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// The VM pool reuses workers and creates a new context for each test file only
// while files are scheduled individually. A single-worker run batches files into
// one shared context; fail instead of letting state leak between test files.
const fileContextMarker = Symbol.for("scriber.components.fileContext");
const markedGlobal = globalThis as typeof globalThis & { [fileContextMarker]?: true };
if (markedGlobal[fileContextMarker]) {
  throw new Error(
    "Component test files are sharing one global context. Run them with at least two workers (do not use --maxWorkers=1 or --no-file-parallelism).",
  );
}
markedGlobal[fileContextMarker] = true;

afterEach(() => {
  cleanup();
});
