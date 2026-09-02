import { Check, Cloud, Cpu, Download, HardDrive, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";
import { useI18n } from "@/i18n";
import type {
  LocalPolishingModelInfo,
  LocalPolishingModelsResponse,
  LocalPolishingVariant,
  PostProcessingEngine,
} from "@/lib/api-types";
import { LOCAL_POLISHING_MODEL_LABEL, LOCAL_POLISHING_VARIANTS } from "@/lib/local-polishing";
import { cn } from "@/lib/utils";

interface LocalPolishingSettingsProps {
  enabled: boolean;
  engine: PostProcessingEngine;
  selectedVariant: LocalPolishingVariant;
  catalog: LocalPolishingModelsResponse | null;
  loading: boolean;
  error: string;
  onEngineChange: (engine: PostProcessingEngine) => void;
  onInstall: (variant: LocalPolishingVariant) => void;
  onCancel: (operationId: string) => void;
  onUse: (variant: LocalPolishingVariant) => void;
  onRemove: (variant: LocalPolishingVariant) => void;
  onRetry: () => void;
}

function modelForVariant(
  catalog: LocalPolishingModelsResponse | null,
  variant: LocalPolishingVariant,
): LocalPolishingModelInfo {
  return (
    catalog?.models.find((model) => model.variant === variant) ?? {
      variant,
      status: "not_installed",
      installed: false,
    }
  );
}

function progressValue(model: LocalPolishingModelInfo): number {
  if (typeof model.progress === "number") {
    return Math.min(100, Math.max(0, model.progress));
  }
  if (model.bytesReceived !== undefined && model.bytesTotal && model.bytesTotal > 0) {
    return Math.min(100, Math.max(0, (model.bytesReceived / model.bytesTotal) * 100));
  }
  return 0;
}

function modelStatusLabel(model: LocalPolishingModelInfo, active: boolean, t: ReturnType<typeof useI18n>["t"]): string {
  if (active) return t("In use");
  if (model.installed && model.runtimeError) return t("Runtime unavailable");
  if (model.status === "ready") return model.updateAvailable ? t("Update available") : t("Ready");
  if (model.status === "downloading") return t("Downloading");
  if (model.status === "verifying") return t("Verifying");
  if (model.status === "cancelling") return t("Canceling");
  if (model.status === "cancelled") return t("Canceled");
  if (model.status === "unavailable") return t("Unavailable");
  if (model.status === "error") return t("Error");
  return t("Not installed");
}

function modelStatusVariant(
  model: LocalPolishingModelInfo,
  active: boolean,
): "default" | "secondary" | "destructive" | "outline" {
  if (model.status === "error" || (model.installed && model.runtimeError)) return "destructive";
  if (active || model.status === "ready") return "default";
  if (model.status === "downloading" || model.status === "verifying" || model.status === "cancelling") {
    return "secondary";
  }
  return "outline";
}

export function LocalPolishingSettings({
  enabled,
  engine,
  selectedVariant,
  catalog,
  loading,
  error,
  onEngineChange,
  onInstall,
  onCancel,
  onUse,
  onRemove,
  onRetry,
}: LocalPolishingSettingsProps) {
  const { t, formatNumber } = useI18n();
  const selectedModel = modelForVariant(catalog, selectedVariant);
  const readySelectedModel = selectedModel.status === "ready" && selectedModel.runtimeReady === true;

  const formatBytes = (bytes: number | undefined) => {
    if (!bytes || bytes < 0) return "";
    const megabytes = bytes / (1024 * 1024);
    if (megabytes >= 1024) {
      return t("{{size}} GB", {
        size: formatNumber(megabytes / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
      });
    }
    return t("{{size}} MB", { size: formatNumber(Math.max(1, Math.round(megabytes))) });
  };

  return (
    <div className="space-y-3" data-testid="local-polishing-settings">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(250px,0.75fr)] sm:items-center">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100">
            {t("Processing engine")}
          </p>
          <p className="mt-1 text-[12px] leading-4 text-slate-600 dark:text-slate-400">
            {engine === "local"
              ? t("Cleanup runs only on this device. A failure returns the original transcript.")
              : t("Cleanup uses the selected cloud model and custom prompt.")}
          </p>
        </div>
        <div
          className="grid grid-cols-2 rounded-xl border border-slate-200/90 bg-white/70 p-1 dark:border-[var(--workspace-border)] dark:bg-[var(--live-well)]"
          role="group"
          aria-label={t("Processing engine")}
        >
          <button
            type="button"
            aria-pressed={engine === "cloud"}
            disabled={!enabled}
            onClick={() => onEngineChange("cloud")}
            className={cn(
              "flex min-h-9 items-center justify-center gap-2 rounded-lg px-3 text-[12px] font-semibold outline-none transition-[background-color,color,box-shadow,transform] active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-blue-500/60 disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none",
              engine === "cloud"
                ? "bg-slate-950 text-white shadow-sm dark:bg-slate-100 dark:text-slate-950"
                : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
            )}
          >
            <Cloud className="h-4 w-4" aria-hidden="true" />
            {t("Cloud")}
          </button>
          <button
            type="button"
            aria-pressed={engine === "local"}
            disabled={!enabled || (!readySelectedModel && engine !== "local")}
            aria-describedby={!readySelectedModel && engine !== "local" ? "local-polishing-engine-hint" : undefined}
            onClick={() => onEngineChange("local")}
            className={cn(
              "flex min-h-9 items-center justify-center gap-2 rounded-lg px-3 text-[12px] font-semibold outline-none transition-[background-color,color,box-shadow,transform] active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-blue-500/60 disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none",
              engine === "local"
                ? "bg-slate-950 text-white shadow-sm dark:bg-slate-100 dark:text-slate-950"
                : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
            )}
          >
            <Cpu className="h-4 w-4" aria-hidden="true" />
            {t("Local")}
          </button>
        </div>
      </div>
      {!readySelectedModel && engine !== "local" ? (
        <p id="local-polishing-engine-hint" className="text-[11px] leading-4 text-slate-500 dark:text-slate-400">
          {t("Install a local model, then choose Use locally.")}
        </p>
      ) : null}

      <div className="rounded-xl border border-amber-200/80 bg-amber-50/70 px-3 py-2.5 dark:border-amber-900/60 dark:bg-amber-950/25">
        <div className="flex items-start gap-2">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" aria-hidden="true" />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-[12px] font-semibold text-amber-950 dark:text-amber-100">
                {t("Local polishing model")}
              </p>
              <Badge
                variant="outline"
                className="border-amber-300 bg-amber-100/80 text-amber-800 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200"
              >
                {t("Experimental")}
              </Badge>
            </div>
            <p className="mt-1 text-[11px] leading-4 text-amber-900/80 dark:text-amber-200/80">
              {t(
                "When local polishing is available, transcript text stays on this device. Cloud speech recognition may still upload audio.",
              )}
            </p>
            <p className="mt-2 border-t border-amber-200/80 pt-2 text-[11px] font-medium leading-4 text-amber-950 dark:border-amber-900/70 dark:text-amber-100">
              {t(
                "Scriber supports only one local model: LFM2.5 350M quantized with QAD to Q4_0. Praxist by Sapient Intelligence.",
              )}
            </p>
          </div>
        </div>
      </div>

      {loading && !catalog ? (
        <div className="grid gap-2" aria-label={t("Loading local polishing model...")}>
          {LOCAL_POLISHING_VARIANTS.map((variant) => (
            <div
              key={variant}
              className="h-40 animate-pulse rounded-xl border border-slate-200/80 bg-slate-100/75 motion-reduce:animate-none dark:border-[var(--workspace-border)] dark:bg-[var(--live-well)]"
            />
          ))}
        </div>
      ) : error ? (
        <div
          className="flex flex-col gap-3 rounded-xl border border-red-200 bg-red-50/70 p-3 dark:border-red-900/60 dark:bg-red-950/25 sm:flex-row sm:items-center sm:justify-between"
          role="alert"
        >
          <p className="text-[12px] leading-4 text-red-800 dark:text-red-200">{error}</p>
          <Button size="sm" variant="outline" onClick={onRetry}>
            <RefreshCw className="h-4 w-4" />
            {t("Try again")}
          </Button>
        </div>
      ) : catalog?.available === false ? (
        <p className="rounded-xl border border-slate-200 bg-white/70 p-3 text-[12px] leading-4 text-slate-600 dark:border-[var(--workspace-border)] dark:bg-[var(--live-well)] dark:text-slate-300">
          {catalog.message === "catalog_not_materialized"
            ? t("The local polishing model is unavailable in this build.")
            : catalog.message || t("Local polishing is not available in this Scriber build.")}
        </p>
      ) : (
        <div className="grid gap-2">
          {LOCAL_POLISHING_VARIANTS.map((variant) => {
            const model = modelForVariant(catalog, variant);
            const selected = engine === "local" && selectedVariant === variant;
            const active = selected && model.status === "ready" && model.runtimeReady === true && model.active === true;
            const busy =
              model.status === "downloading" || model.status === "verifying" || model.status === "cancelling";
            const progress = progressValue(model);
            const size = formatBytes(model.sizeBytes ?? model.bytesTotal);
            return (
              <article
                key={variant}
                data-testid={`local-polishing-model-${variant}`}
                className={cn(
                  "flex min-h-[176px] flex-col rounded-xl border bg-white/70 p-3.5 transition-[border-color,background-color,box-shadow,transform] active:translate-y-px motion-reduce:transition-none dark:bg-[var(--live-well)]",
                  active
                    ? "border-blue-400 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.12)] dark:border-blue-700"
                    : "border-slate-200/90 dark:border-[var(--workspace-border)]",
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h4 className="text-[13px] font-bold text-slate-950 dark:text-slate-100">
                        {model.name ? t(model.name) : LOCAL_POLISHING_MODEL_LABEL}
                      </h4>
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
                      {model.description
                        ? t(model.description)
                        : t("Optimized for fast, high-quality German dictation cleanup directly on this device.")}
                    </p>
                  </div>
                  <Badge variant={modelStatusVariant(model, active)} className="shrink-0">
                    {modelStatusLabel(model, active, t)}
                  </Badge>
                </div>

                {size ? (
                  <p className="mt-2 flex items-center gap-1.5 text-ui-micro font-medium text-slate-500 dark:text-slate-400">
                    <HardDrive className="h-3.5 w-3.5" aria-hidden="true" />
                    {size}
                  </p>
                ) : null}

                {busy ? (
                  <div className="mt-3 space-y-1.5" aria-live="polite">
                    <Progress
                      value={progress}
                      aria-label={t("{{model}} download progress", { model: LOCAL_POLISHING_MODEL_LABEL })}
                    />
                    <div className="flex min-w-0 items-center gap-1.5 text-ui-micro text-slate-500 dark:text-slate-400">
                      <WavePhysicsLoader size="micro" />
                      <span className="min-w-0 truncate">
                        {model.message ||
                          (model.status === "cancelling"
                            ? t("Canceling download...")
                            : model.status === "verifying"
                              ? t("Verifying model files...")
                              : t("Downloading model files..."))}
                      </span>
                      <span className="ml-auto shrink-0 font-mono">{formatNumber(Math.round(progress))}%</span>
                    </div>
                    {model.etaSeconds !== undefined ? (
                      <p className="text-right text-ui-micro text-slate-500 dark:text-slate-400">
                        {t("About {{minutes}} min remaining", {
                          minutes: formatNumber(Math.max(1, Math.ceil(model.etaSeconds / 60))),
                        })}
                      </p>
                    ) : null}
                  </div>
                ) : model.installed && model.runtimeError ? (
                  <p className="mt-2 text-[11px] leading-4 text-red-700 dark:text-red-300" role="alert">
                    {t("The verified local runtime could not start. Reinstall Scriber or use cloud cleanup.")}
                  </p>
                ) : model.status === "error" && (model.message || model.errorCode) ? (
                  <p className="mt-2 text-[11px] leading-4 text-red-700 dark:text-red-300" role="alert">
                    {model.message || model.errorCode}
                  </p>
                ) : null}

                <div className="mt-auto flex flex-wrap gap-2 pt-3">
                  {busy ? (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!model.operationId || model.status === "cancelling"}
                      onClick={() => model.operationId && onCancel(model.operationId)}
                    >
                      <X className="h-4 w-4" />
                      {model.status === "cancelling" ? t("Canceling") : t("Cancel")}
                    </Button>
                  ) : model.status === "ready" ? (
                    <>
                      <Button size="sm" disabled={active || !enabled} onClick={() => onUse(variant)}>
                        {active ? <Check className="h-4 w-4" /> : <Cpu className="h-4 w-4" />}
                        {active ? t("In use") : t("Use locally")}
                      </Button>
                      {model.updateAvailable ? (
                        <Button size="sm" variant="outline" onClick={() => onInstall(variant)}>
                          <RefreshCw className="h-4 w-4" />
                          {t("Update")}
                        </Button>
                      ) : null}
                      <Button size="sm" variant="outline" disabled={selected} onClick={() => onRemove(variant)}>
                        <Trash2 className="h-4 w-4" />
                        {t("Remove")}
                      </Button>
                    </>
                  ) : (
                    <Button size="sm" disabled={model.status === "unavailable"} onClick={() => onInstall(variant)}>
                      <Download className="h-4 w-4" />
                      {model.status === "unavailable"
                        ? t("Unavailable")
                        : model.status === "error"
                          ? t("Try download again")
                          : t("Download")}
                    </Button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
