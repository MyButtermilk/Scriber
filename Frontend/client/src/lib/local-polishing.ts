import type {
  LocalPolishingModelInfo,
  LocalPolishingModelsResponse,
  LocalPolishingModelStatus,
  LocalPolishingVariant,
} from "@/lib/api-types";

export const LOCAL_POLISHING_VARIANTS = ["qad_q4_0"] as const satisfies readonly LocalPolishingVariant[];
export const LOCAL_POLISHING_MODEL_LABEL = "LFM2.5 350M · QAD Q4_0";

export interface LocalPolishingProgressUpdate {
  variant: LocalPolishingVariant;
  status: LocalPolishingModelStatus;
  operationId?: string;
  progress?: number;
  bytesReceived?: number;
  bytesTotal?: number;
  etaSeconds?: number;
  message?: string;
  errorCode?: string;
  installed?: boolean;
  active?: boolean;
  runtimeReady?: boolean;
  runtimeError?: string;
  code?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function isVariant(value: unknown): value is LocalPolishingVariant {
  return value === "qad_q4_0";
}

function normalizeStatus(value: unknown): LocalPolishingModelStatus {
  if (
    value === "downloading" ||
    value === "verifying" ||
    value === "cancelling" ||
    value === "cancelled" ||
    value === "ready" ||
    value === "unavailable" ||
    value === "error"
  ) {
    return value;
  }
  return "not_installed";
}

function normalizeProgress(value: unknown): number | undefined {
  const numeric = optionalNumber(value);
  return numeric === undefined ? undefined : Math.min(100, Math.max(0, numeric));
}

function normalizeModel(value: unknown): LocalPolishingModelInfo | null {
  if (!isRecord(value) || !isVariant(value.variant)) {
    return null;
  }
  const status = normalizeStatus(value.status);
  return {
    variant: value.variant,
    name: optionalString(value.name),
    description: optionalString(value.description),
    status,
    installed: optionalBoolean(value.installed) ?? status === "ready",
    active: optionalBoolean(value.active),
    runtimeReady: optionalBoolean(value.runtimeReady),
    runtimeError: optionalString(value.runtimeError),
    updateAvailable: optionalBoolean(value.updateAvailable),
    operationId: optionalString(value.operationId),
    progress: normalizeProgress(value.progress),
    bytesReceived: optionalNumber(value.bytesReceived),
    bytesTotal: optionalNumber(value.bytesTotal),
    etaSeconds: optionalNumber(value.etaSeconds),
    message: optionalString(value.message),
    errorCode: optionalString(value.errorCode) ?? optionalString(value.code),
    sizeBytes: optionalNumber(value.sizeBytes),
  };
}

export function normalizeLocalPolishingModelsResponse(value: unknown): LocalPolishingModelsResponse {
  if (!isRecord(value)) {
    throw new Error("Local polishing model status is invalid.");
  }
  const models = Array.isArray(value.models)
    ? value.models.map(normalizeModel).filter((model): model is LocalPolishingModelInfo => model !== null)
    : [];
  return {
    available: value.available !== false,
    message: optionalString(value.message),
    currentVariant: isVariant(value.currentVariant) ? value.currentVariant : undefined,
    models,
  };
}

export function mergeLocalPolishingProgress(
  current: LocalPolishingModelsResponse | null,
  update: LocalPolishingProgressUpdate,
): LocalPolishingModelsResponse | null {
  if (!current) {
    return current;
  }
  const existing = current.models.find((model) => model.variant === update.variant);
  const next: LocalPolishingModelInfo = {
    ...(existing ?? { variant: update.variant, status: "not_installed" as const }),
    status: update.status,
    operationId: update.operationId ?? existing?.operationId,
    progress: normalizeProgress(update.progress) ?? existing?.progress,
    bytesReceived: update.bytesReceived ?? existing?.bytesReceived,
    bytesTotal: update.bytesTotal ?? existing?.bytesTotal,
    etaSeconds: update.etaSeconds ?? existing?.etaSeconds,
    message: update.message ?? existing?.message,
    errorCode: update.errorCode ?? update.code ?? existing?.errorCode,
    installed: update.status === "ready" ? true : existing?.installed,
    active: update.active ?? existing?.active,
    runtimeReady: update.runtimeReady ?? existing?.runtimeReady,
    runtimeError: update.runtimeError ?? existing?.runtimeError,
  };
  return {
    ...current,
    models: existing
      ? current.models.map((model) => (model.variant === update.variant ? next : model))
      : [...current.models, next],
  };
}
