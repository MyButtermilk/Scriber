import assert from "node:assert/strict";
import test from "node:test";
import type { RuntimeLogEntry } from "./api-types";
import { diagnosticWorkflow, indexRuntimeLogs, runtimeLatencySamples, workflowCounts } from "./runtime-diagnostics";

function entry(context: RuntimeLogEntry["context"] = {}, component = ""): RuntimeLogEntry {
  return { source: "latest.structured.jsonl", line: 1, level: "INFO", message: "Complete", context, component };
}

test("structured workflow ownership precedes legacy module inference", () => {
  assert.equal(diagnosticWorkflow(entry({ workflow: "podcasts" }, "file_transcription")), "podcasts");
  assert.equal(diagnosticWorkflow(entry({}, "local_polishing")), "local_polishing");
  assert.equal(diagnosticWorkflow(entry({}, "speaker_profiles")), "voice");
  assert.equal(diagnosticWorkflow(entry({ workflow: "unknown" })), "other");
  assert.equal(diagnosticWorkflow(entry({ workflow: "__proto__" })), "other");
});

test("search indexes structured error details and counts each event once", () => {
  const logs = [entry({ workflow: "podcasts", errorCategory: "NetworkError" }), entry({ workflow: "meetings" })];
  assert.equal(indexRuntimeLogs(logs)[0].search.includes("networkerror"), true);
  assert.deepEqual(workflowCounts(logs), { all: 2, podcasts: 1, meetings: 1 });
});

test("latencies keep the newest valid measurement for each exact stage", () => {
  const logs = [
    entry({ meta: { hotkey_received_to_mic_ready_ms: 42, stop_requested_to_first_paste_ms: 210 } }),
    entry({ event: "runtime.operation.completed", durationMs: 9999 }),
    entry({ event: "injector.clipboard.restore", durationMs: 18 }),
    entry({ meta: { hotkey_received_to_mic_ready_ms: -1, stop_requested_to_first_paste_ms: 175 } }),
  ];
  assert.deepEqual(
    runtimeLatencySamples(logs).map((sample) => sample.durationMs),
    [42, 175, 18],
  );
  assert.deepEqual(
    runtimeLatencySamples([]).map((sample) => sample.durationMs),
    [null, null, null],
  );
});
