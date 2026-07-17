import { Check, ChevronRight, Copy } from "lucide-react";
import type { RuntimeLogContext, RuntimeLogContextValue } from "@/lib/api-types";
import { useI18n } from "@/i18n";

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

function formatDuration(
  value: number,
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string,
) {
  if (value >= 10_000) {
    return `${formatNumber(value / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} s`;
  }
  if (value >= 1000) {
    return `${formatNumber(value / 1000, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} s`;
  }
  if (value >= 100) return `${formatNumber(Math.round(value))} ms`;
  return `${formatNumber(value, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} ms`;
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

function hotPathMetrics(
  context: RuntimeLogContext | null | undefined,
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string,
): DisplayMetric[] {
  const meta = context?.meta;
  if (!meta) return [];
  return HOT_PATH_METRICS.flatMap(([key, label, group]) => {
    const value = meta[key];
    return isFiniteNumber(value) ? [{ key, label, value: formatDuration(value, formatNumber), group }] : [];
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

function technicalContextRows(
  context: RuntimeLogContext,
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string,
  t: (source: string) => string,
): Array<[string, string]> {
  const rows = CONTEXT_LABELS.flatMap(([key, label]) => {
    const value = context[key];
    return typeof value === "string" && value.trim() ? [[label, value] as [string, string]] : [];
  });
  if (typeof context.durationMs === "number" && Number.isFinite(context.durationMs)) {
    rows.push(["Duration", formatDuration(context.durationMs, formatNumber)]);
  }
  if (typeof context.milestone === "boolean") {
    rows.push(["Milestone", context.milestone ? t("Yes") : t("No")]);
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
  const { t } = useI18n();
  return (
    <button
      type="button"
      className="debug-log-copy-detail"
      onClick={onClick}
      aria-label={label}
      title={label}
    >
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span>{copied ? t("Copied") : t("Copy")}</span>
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
  const { formatNumber, t } = useI18n();
  const isLongMessage = message.length > LONG_MESSAGE_PREVIEW_CHARS || message.split("\n").length > 6;
  const timingMetrics = hotPathMetrics(context, formatNumber);
  const startupMetrics = timingMetrics.filter((metric) => metric.group === "startup");
  const finalizationMetrics = timingMetrics.filter((metric) => metric.group === "finalization");
  const configMetrics = configurationMetrics(context);
  const summaryMetrics = timingMetrics.length ? timingMetrics : configMetrics;
  const rawContext = context ? JSON.stringify(context, null, 2) : "";
  const contextRows = context ? technicalContextRows(context, formatNumber, t) : [];
  const contextFieldCount = context ? countPrimitiveFields(context) : 0;
  const fullMessageCopyKey = `${copyKey}:message`;
  const contextCopyKey = `${copyKey}:context`;

  return (
    <div className="debug-log-message">
      <div className="debug-log-message-summary">
        {compactMessagePreview(message)}
      </div>

      {summaryMetrics.length > 0 && (
        <dl className="debug-log-key-metrics" aria-label={t("Key diagnostic values")}>
          {summaryMetrics.map((metric) => (
            <div key={metric.key}>
              <dt>{t(metric.label)}</dt>
              <dd title={metric.value}>{metric.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {context && (
        <details className="debug-log-details">
          <summary>
            <ChevronRight className="debug-log-details-chevron" aria-hidden="true" />
            <span>{t("Technical details")}</span>
            <small>{contextFieldCount === 1
              ? t("{{count}} field", { count: formatNumber(contextFieldCount) })
              : t("{{count}} fields", { count: formatNumber(contextFieldCount) })}</small>
          </summary>
          <div className="debug-log-details-body">
            {contextRows.length > 0 && (
              <section className="debug-log-detail-section" aria-label={t("Event context")}>
                <h3>{t("Event context")}</h3>
                <dl className="debug-log-detail-grid">
                  {contextRows.map(([label, value]) => (
                    <div key={label}>
                      <dt>{t(label)}</dt>
                      <dd title={value}>{value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {startupMetrics.length > 0 && (
              <section className="debug-log-detail-section" aria-label={t("Startup timings")}>
                <h3>{t("Startup phase")}</h3>
                <dl className="debug-log-detail-grid">
                  {startupMetrics.map((metric) => (
                    <div key={metric.key}>
                      <dt>{t(metric.label)}</dt>
                      <dd>{metric.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {finalizationMetrics.length > 0 && (
              <section className="debug-log-detail-section" aria-label={t("Finalization timings")}>
                <h3>{t("After stop")}</h3>
                <dl className="debug-log-detail-grid">
                  {finalizationMetrics.map((metric) => (
                    <div key={metric.key}>
                      <dt>{t(metric.label)}</dt>
                      <dd>{metric.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            <details className="debug-log-raw-details">
              <summary>
                <ChevronRight className="debug-log-details-chevron" aria-hidden="true" />
                <span>{t("Raw structured data")}</span>
              </summary>
              <div className="debug-log-raw-body">
                <div className="debug-log-raw-header">
                  <span>{t("Redacted JSON")}</span>
                  <CopyTechnicalButton
                    copied={copiedKey === contextCopyKey}
                    label={t("Copy raw structured log data")}
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
            <span>{t("Full message")}</span>
            <small>{message.length === 1
              ? t("{{count}} character", { count: formatNumber(message.length) })
              : t("{{count}} characters", { count: formatNumber(message.length) })}</small>
          </summary>
          <div className="debug-log-raw-body">
            <div className="debug-log-raw-header">
              <span>{t("Original redacted message")}</span>
              <CopyTechnicalButton
                copied={copiedKey === fullMessageCopyKey}
                label={t("Copy full log message")}
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
