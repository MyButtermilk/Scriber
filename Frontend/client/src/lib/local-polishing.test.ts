import assert from "node:assert/strict";
import test from "node:test";

import { mergeLocalPolishingProgress, normalizeLocalPolishingModelsResponse } from "@/lib/local-polishing";

test("normalizes the authoritative local polishing catalog", () => {
  const catalog = normalizeLocalPolishingModelsResponse({
    available: true,
    currentVariant: "q8_0",
    models: [
      { variant: "q8_0", status: "ready", sizeBytes: 291_545_440 },
      { variant: "unknown", status: "ready" },
      { variant: "bf16", status: "downloading", progress: 125 },
    ],
  });

  assert.equal(catalog.models.length, 2);
  assert.equal(catalog.models[0]?.installed, true);
  assert.equal(catalog.models[1]?.progress, 100);
  assert.equal(catalog.currentVariant, "q8_0");
});

test("merges websocket progress without changing the selected variant", () => {
  const catalog = normalizeLocalPolishingModelsResponse({
    available: true,
    currentVariant: "q8_0",
    models: [{ variant: "bf16", status: "not_installed" }],
  });
  const updated = mergeLocalPolishingProgress(catalog, {
    variant: "bf16",
    status: "downloading",
    operationId: "download-bf16",
    progress: 42.7,
    bytesReceived: 10,
    bytesTotal: 20,
  });

  assert.equal(updated?.currentVariant, "q8_0");
  assert.deepEqual(updated?.models[0], {
    variant: "bf16",
    status: "downloading",
    installed: false,
    active: undefined,
    runtimeReady: undefined,
    runtimeError: undefined,
    updateAvailable: undefined,
    operationId: "download-bf16",
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
  const catalog = normalizeLocalPolishingModelsResponse({
    available: false,
    models: [
      { variant: "q8_0", status: "cancelling", errorCode: "cancel_requested" },
      { variant: "bf16", status: "unavailable" },
    ],
  });

  assert.equal(catalog.models[0]?.status, "cancelling");
  assert.equal(catalog.models[0]?.errorCode, "cancel_requested");
  assert.equal(catalog.models[1]?.status, "unavailable");
});
