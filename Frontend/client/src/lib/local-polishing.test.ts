import assert from "node:assert/strict";
import test from "node:test";

import { mergeLocalPolishingProgress, normalizeLocalPolishingModelsResponse } from "@/lib/local-polishing";

test("normalizes the authoritative local polishing catalog", () => {
  const catalog = normalizeLocalPolishingModelsResponse({
    available: true,
    currentVariant: "qad_q4_0",
    models: [
      { variant: "qad_q4_0", status: "ready", sizeBytes: 228_000_000 },
      { variant: "unknown", status: "ready" },
      { variant: "q8_0", status: "downloading", progress: 125 },
      { variant: "bf16", status: "ready" },
    ],
  });

  assert.equal(catalog.models.length, 1);
  assert.equal(catalog.models[0]?.installed, true);
  assert.equal(catalog.currentVariant, "qad_q4_0");
});

test("merges websocket progress without changing the selected variant", () => {
  const catalog = normalizeLocalPolishingModelsResponse({
    available: true,
    currentVariant: "qad_q4_0",
    models: [{ variant: "qad_q4_0", status: "not_installed" }],
  });
  const updated = mergeLocalPolishingProgress(catalog, {
    variant: "qad_q4_0",
    status: "downloading",
    operationId: "download-qad",
    progress: 42.7,
    bytesReceived: 10,
    bytesTotal: 20,
  });

  assert.equal(updated?.currentVariant, "qad_q4_0");
  assert.deepEqual(updated?.models[0], {
    variant: "qad_q4_0",
    status: "downloading",
    installed: false,
    active: undefined,
    runtimeReady: undefined,
    runtimeError: undefined,
    updateAvailable: undefined,
    operationId: "download-qad",
    progress: 42.7,
    bytesReceived: 10,
    bytesTotal: 20,
    etaSeconds: undefined,
    message: undefined,
    errorCode: undefined,
    sizeBytes: undefined,
    name: undefined,
    description: undefined,
  });
});

test("preserves explicit cancellation and unavailability states", () => {
  const cancelling = normalizeLocalPolishingModelsResponse({
    available: false,
    models: [{ variant: "qad_q4_0", status: "cancelling", errorCode: "cancel_requested" }],
  });
  const unavailable = normalizeLocalPolishingModelsResponse({
    available: false,
    models: [{ variant: "qad_q4_0", status: "unavailable" }],
  });

  assert.equal(cancelling.models[0]?.status, "cancelling");
  assert.equal(cancelling.models[0]?.errorCode, "cancel_requested");
  assert.equal(unavailable.models[0]?.status, "unavailable");
});

test("rejects retired local model variants from settings and catalog payloads", () => {
  const catalog = normalizeLocalPolishingModelsResponse({
    available: true,
    currentVariant: "bf16",
    models: [
      { variant: "q8_0", status: "ready" },
      { variant: "bf16", status: "ready" },
    ],
  });

  assert.equal(catalog.currentVariant, undefined);
  assert.deepEqual(catalog.models, []);
});
