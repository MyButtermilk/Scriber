import { Check, ChevronRight, Copy } from "lucide-react";
import type { RuntimeLogContext, RuntimeLogContextValue } from "@/lib/api-types";

type RuntimeLogMessageProps = {
  message: string;
  context?: RuntimeLogContext | null;
  copyKey: string;
  copiedKey: string;
  onCopy: (text: string, key: string) => void;
};

type DisplayMetric = {
  key: string;
  label: string;
  value: string;
  group?: "startup" | "finalization";
};

const HOT_PATH_METRICS: Array<[string, string, "startup" | "finalization"]> = [
  ["hotkey_received_to_mic_ready_ms", "Microphone ready", "startup"],
  ["hotkey_received_to_first_audible_audio_frame_ms", "Audio ready", "startup"],
  ["stop_requested_to_provider_final_received_ms", "Final transcript", "finalization"],
  ["stop_requested_to_first_paste_ms", "Text inserted", "finalization"],
];

const CONTEXT_LABELS: Array<[keyof RuntimeLogContext, string]> = [
  ["event", "Event"],
  ["workflow", "Workflow"],
  ["stage", "Stage"],
  ["provider", "Provider"],
  ["outcome", "Outcome"],
  ["errorCategory", "Error category"],
];

const LONG_MESSAGE_PREVIEW_CHARS = 320;

function isFiniteNumber(value: RuntimeLogContextValue | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function formatDuration(value: number) {
  if (value >= 10_000) return `${(value / 1000).toFixed(1)} s`;
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  if (value >= 100) return `${Math.round(value)} ms`;
  return `${value.toFixed(1)} ms`;
}

function compactMessagePreview(message: string) {
  const compact = message.replace(/\s+/g, " ").trim();
  if (compact.length <= LONG_MESSAGE_PREVIEW_CHARS) return compact;
  return `${compact.slice(0, LONG_MESSAGE_PREVIEW_CHARS).trimEnd()}…`;
}

function countPrimitiveFields(value: RuntimeLogContextValue | RuntimeLogContext): number {
  if (value === null || typeof value !== "object") return 1;
  if (Array.isArray(value)) {
    return value.reduce<number>((total, item) => total + countPrimitiveFields(item), 0);
  }
  return Object.values(value).reduce<number>(
    (total, item) => total + countPrimitiveFields(item as RuntimeLogContextValue),
    0,
  );
}

function hotPathMetrics(context?: RuntimeLogContext | null): DisplayMetric[] {
  const meta = context?.meta;
  if (!meta) return [];
  return HOT_PATH_METRICS.flatMap(([key, label, group]) => {
    const value = meta[key];
    return isFiniteNumber(value) ? [{ key, label, value: formatDuration(value), group }] : [];
  });
}

function configurationMetrics(context?: RuntimeLogContext | null): DisplayMetric[] {
  const meta = context?.meta;
  if (!meta) return [];
  const fields: Array<[string, string]> = [
    ["model", "Model"],
    ["mode", "Mode"],
    ["region", "Region"],
    ["language", "Language"],
  ];
  return fields.flatMap(([key, label]) => {
    const value = meta[key];
    return typeof value === "string" && value.trim()
      ? [{ key, label, value }]
      : [];
  });
}

function technicalContextRows(context: RuntimeLogContext): Array<[string, string]> {
  const rows = CONTEXT_LABELS.flatMap(([key, label]) => {
    const value = context[key];
    return typeof value === "string" && value.trim() ? [[label, value] as [string, string]] : [];
  });
  if (typeof context.durationMs === "number" && Number.isFinite(context.durationMs)) {
    rows.push(["Duration", formatDuration(context.durationMs)]);
  }
  if (typeof context.milestone === "boolean") {
    rows.push(["Milestone", context.milestone ? "Yes" : "No"]);
  }
  return rows;
}

function CopyTechnicalButton({
  copied,
  label,
  onClick,
}: {
  copied: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="debug-log-copy-detail"
      onClick={onClick}
      aria-label={label}
      title={label}
    >
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span>{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

export function RuntimeLogMessage({
  message,
  context,
  copyKey,
  copiedKey,
  onCopy,
}: RuntimeLogMessageProps) {
  const isLongMessage = message.length > LONG_MESSAGE_PREVIEW_CHARS || message.split("\n").length > 6;
  const timingMetrics = hotPathMetrics(context);
  const startupMetrics = timingMetrics.filter((metric) => metric.group === "startup");
  const finalizationMetrics = timingMetrics.filter((metric) => metric.group === "finalization");
  const configMetrics = configurationMetrics(context);
  const summaryMetrics = timingMetrics.length ? timingMetrics : configMetrics;
  const rawContext = context ? JSON.stringify(context, null, 2) : "";
  const contextRows = context ? technicalContextRows(context) : [];
  const contextFieldCount = context ? countPrimitiveFields(context) : 0;
  const fullMessageCopyKey = `${copyKey}:message`;
  const contextCopyKey = `${copyKey}:context`;

  return (
    <div className="debug-log-message">
      <div className="debug-log-message-summary">
        {compactMessagePreview(message)}
      </div>

      {summaryMetrics.length > 0 && (
        <dl className="debug-log-key-metrics" aria-label="Key diagnostic values">
          {summaryMetrics.map((metric) => (
            <div key={metric.key}>
              <dt>{metric.label}</dt>
              <dd title={metric.value}>{metric.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {context && (
        <details className="debug-log-details">
          <summary>
            <ChevronRight className="debug-log-details-chevron" aria-hidden="true" />
            <span>Technical details</span>
            <small>{contextFieldCount} fields</small>
          </summary>
          <div className="debug-log-details-body">
            {contextRows.length > 0 && (
              <section className="debug-log-detail-section" aria-label="Event context">
                <h3>Event context</h3>
                <dl className="debug-log-detail-grid">
                  {contextRows.map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd title={value}>{value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {startupMetrics.length > 0 && (
              <section className="debug-log-detail-section" aria-label="Startup timings">
                <h3>Startup</h3>
                <dl className="debug-log-detail-grid">
                  {startupMetrics.map((metric) => (
                    <div key={metric.key}>
                      <dt>{metric.label}</dt>
                      <dd>{metric.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {finalizationMetrics.length > 0 && (
              <section className="debug-log-detail-section" aria-label="Finalization timings">
                <h3>After stop</h3>
                <dl className="debug-log-detail-grid">
                  {finalizationMetrics.map((metric) => (
                    <div key={metric.key}>
                      <dt>{metric.label}</dt>
                      <dd>{metric.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            <details className="debug-log-raw-details">
              <summary>
                <ChevronRight className="debug-log-details-chevron" aria-hidden="true" />
                <span>Raw structured data</span>
              </summary>
              <div className="debug-log-raw-body">
                <div className="debug-log-raw-header">
                  <span>Redacted JSON</span>
                  <CopyTechnicalButton
                    copied={copiedKey === contextCopyKey}
                    label="Copy raw structured log data"
                    onClick={() => onCopy(rawContext, contextCopyKey)}
                  />
                </div>
                <pre>{rawContext}</pre>
              </div>
            </details>
          </div>
        </details>
      )}

      {isLongMessage && (
        <details className="debug-log-details debug-log-full-message">
          <summary>
            <ChevronRight className="debug-log-details-chevron" aria-hidden="true" />
            <span>Full message</span>
            <small>{message.length.toLocaleString()} characters</small>
          </summary>
          <div className="debug-log-raw-body">
            <div className="debug-log-raw-header">
              <span>Original redacted message</span>
              <CopyTechnicalButton
                copied={copiedKey === fullMessageCopyKey}
                label="Copy full log message"
                onClick={() => onCopy(message, fullMessageCopyKey)}
              />
            </div>
            <pre>{message}</pre>
          </div>
        </details>
      )}
    </div>
  );
}
