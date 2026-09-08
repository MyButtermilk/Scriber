import type { RuntimeLogEntry } from "./api-types";

export const DIAGNOSTIC_WORKFLOWS = {
  all: "All activity",
  live_mic: "Live Mic",
  meetings: "Meetings",
  meeting_import: "Meeting imports",
  youtube: "YouTube",
  file: "Files",
  podcasts: "Podcasts",
  voice: "Voice Library",
  local_polishing: "Local polishing",
  models: "Models",
  calendar: "Calendar",
  settings: "Settings",
  devices: "Audio devices",
  transcripts: "Transcripts",
  runtime: "App runtime",
  other: "Other activity",
} as const;

export type DiagnosticWorkflow = keyof typeof DIAGNOSTIC_WORKFLOWS;

/** Structured ownership wins; older component names keep legacy logs useful. */
export function diagnosticWorkflow(entry: RuntimeLogEntry): DiagnosticWorkflow {
  const workflow = entry.context?.workflow;
  if (workflow && workflow !== "all" && Object.prototype.hasOwnProperty.call(DIAGNOSTIC_WORKFLOWS, workflow)) {
    return workflow as DiagnosticWorkflow;
  }
  const marker = `${entry.component || ""} ${entry.context?.event || ""} ${entry.context?.stage || ""}`.toLowerCase();
  const patterns: Array<[RegExp, DiagnosticWorkflow]> = [
    [/podcast/, "podcasts"],
    [/local.polish/, "local_polishing"],
    [/voice|diariz|speaker/, "voice"],
    [/meeting.import/, "meeting_import"],
    [/meeting/, "meetings"],
    [/youtube/, "youtube"],
    [/file.transcri/, "file"],
    [/calendar|outlook/, "calendar"],
    [/inject|clipboard|live.mic|microphone|prewarm|hot.path/, "live_mic"],
    [/onnx|model.install|model.download/, "models"],
    [/device/, "devices"],
    [/settings/, "settings"],
    [/shell|updater|runtime|startup/, "runtime"],
  ];
  return (
    patterns.find(([pattern]) => pattern.test(marker))?.[1] ||
    (entry.source.includes("shell") || entry.source.includes("crash") ? "runtime" : "other")
  );
}

export function indexRuntimeLogs(logs: readonly RuntimeLogEntry[]) {
  return logs.map((entry) => ({
    entry,
    workflow: diagnosticWorkflow(entry),
    search: [
      entry.message,
      entry.source,
      entry.component,
      entry.level,
      entry.timestamp,
      entry.timestampMs,
      entry.context ? JSON.stringify(entry.context) : "",
    ]
      .join(" ")
      .toLowerCase(),
  }));
}

export function workflowCounts(logs: readonly RuntimeLogEntry[]): Partial<Record<DiagnosticWorkflow, number>> {
  const counts: Partial<Record<DiagnosticWorkflow, number>> = { all: logs.length };
  for (const entry of logs) {
    const workflow = diagnosticWorkflow(entry);
    counts[workflow] = (counts[workflow] || 0) + 1;
  }
  return counts;
}

export interface RuntimeLatencySample {
  key: "microphone" | "insertion" | "clipboard";
  label: string;
  durationMs: number | null;
  detail: string;
}

/** Never combine unlike stages or present HTTP admission as completed STT. */
export function runtimeLatencySamples(logs: readonly RuntimeLogEntry[]): RuntimeLatencySample[] {
  const samples: RuntimeLatencySample[] = [
    {
      key: "microphone",
      label: "Microphone ready",
      durationMs: null,
      detail: "From activation to microphone readiness",
    },
    {
      key: "insertion",
      label: "Text insertion",
      durationMs: null,
      detail: "From stop request to the first paste command",
    },
    {
      key: "clipboard",
      label: "Clipboard restored",
      durationMs: null,
      detail: "From clipboard replacement to restoration",
    },
  ];
  // The HTTP log contract is chronological, with the most recent event last.
  for (let index = logs.length - 1; index >= 0; index -= 1) {
    const context = logs[index].context;
    const values = [
      context?.meta?.hotkey_received_to_mic_ready_ms,
      context?.meta?.stop_requested_to_first_paste_ms,
      context?.event === "injector.clipboard.restore" ? (context.durationMs ?? context.meta?.elapsed_ms) : undefined,
    ];
    values.forEach((value, sampleIndex) => {
      if (
        samples[sampleIndex].durationMs === null &&
        typeof value === "number" &&
        Number.isFinite(value) &&
        value >= 0
      ) {
        samples[sampleIndex].durationMs = value;
      }
    });
  }
  return samples;
}
