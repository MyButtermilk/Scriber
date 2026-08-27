import assert from "node:assert/strict";
import test from "node:test";

import { showPostProcessingFallbackToast, type CredentialSettingsRequest } from "@/lib/post-processing-fallback-toast";

type CapturedToast = {
  title: string;
  description: string;
  duration: number;
  variant?: string;
  action?: unknown;
};

const t = (key: string, values?: Record<string, string | number>) =>
  key.replace(/\{\{(\w+)\}\}/g, (_match, name: string) => String(values?.[name] ?? ""));

test("shows each successful post-processing fallback event exactly once", () => {
  const toasts: CapturedToast[] = [];
  const seen = new Set<string>();
  const message = {
    apiVersion: "1",
    type: "post_processing_fallback_used" as const,
    eventId: "post-processing-fallback:session-1",
    sessionId: "session-1",
    primaryModel: "cerebras/gemma-4-31b",
    fallbackModel: "minimax/minimax-m3",
    desktopNotificationAccepted: false,
  };

  assert.equal(
    showPostProcessingFallbackToast((toast) => toasts.push(toast), t, message, seen),
    true,
  );
  assert.equal(
    showPostProcessingFallbackToast((toast) => toasts.push(toast), t, message, seen),
    false,
  );
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0]?.duration, 6000);
  assert.match(toasts[0]?.description ?? "", /MiniMax M3.*Gemma 4 31B/);

  assert.equal(
    showPostProcessingFallbackToast(
      (toast) => toasts.push(toast),
      t,
      { ...message, eventId: "post-processing-fallback:session-2", sessionId: "session-2" },
      seen,
    ),
    true,
  );
  assert.equal(toasts.length, 2);

  assert.equal(
    showPostProcessingFallbackToast(
      (toast) => toasts.push(toast),
      t,
      {
        ...message,
        eventId: "post-processing-fallback:session-3",
        sessionId: "session-3",
        desktopNotificationAccepted: true,
      },
      seen,
    ),
    true,
  );
  assert.equal(toasts.length, 2);
});

test("explains Cerebras authentication rejection and exposes the credential action seam", () => {
  const toasts: CapturedToast[] = [];
  const actions: CredentialSettingsRequest[] = [];
  const message = {
    apiVersion: "1",
    type: "post_processing_fallback_used" as const,
    eventId: "post-processing-fallback:auth",
    primaryModel: "cerebras/gemma-4-31b",
    fallbackModel: "google/gemini-2.5-flash-lite:nitro",
    desktopNotificationAccepted: true,
    reason: "invalid_request_error",
    reasonCategory: "authentication" as const,
    primaryFailureStatus: 401,
  };

  assert.equal(
    showPostProcessingFallbackToast(
      (toast) => toasts.push(toast),
      t,
      message,
      new Set(),
      (request) => {
        actions.push(request);
        return {} as never;
      },
    ),
    true,
  );

  assert.equal(toasts.length, 1, "the actionable in-app toast remains available after a native notice");
  assert.equal(toasts[0]?.variant, "destructive");
  assert.match(toasts[0]?.title ?? "", /Cerebras API key was rejected/);
  assert.match(toasts[0]?.description ?? "", /Gemini 2\.5 Flash Lite/);
  assert.match(toasts[0]?.description ?? "", /Check or replace the key in Settings/);
  assert.doesNotMatch(`${toasts[0]?.title} ${toasts[0]?.description}`, /invalid_request_error/);
  assert.ok(toasts[0]?.action);
  assert.deepEqual(actions, [{ provider: "Cerebras", actionLabel: "Check Cerebras key" }]);
});

test("maps fallback categories to distinct honest explanations", () => {
  const cases = [
    ["quota_or_payment", 402, "Provider quota or payment issue"],
    ["rate_limit", 429, "Provider rate limit reached"],
    ["provider_unavailable", 503, "Provider temporarily unavailable"],
    ["timeout", undefined, "Post-processing timed out"],
    ["output_limit", undefined, "Provider output limit reached"],
    ["request_rejected", 400, "Provider rejected the request"],
  ] as const;
  const toasts: CapturedToast[] = [];

  for (const [reasonCategory, primaryFailureStatus, expectedTitle] of cases) {
    showPostProcessingFallbackToast(
      (toast) => toasts.push(toast),
      t,
      {
        apiVersion: "1",
        type: "post_processing_fallback_used",
        eventId: `post-processing-fallback:${reasonCategory}`,
        primaryModel: "cerebras/gemma-4-31b",
        fallbackModel: "minimax/minimax-m3:nitro",
        desktopNotificationAccepted: false,
        reasonCategory,
        primaryFailureStatus,
      },
      new Set(),
    );
    assert.equal(toasts.at(-1)?.title, expectedTitle);
    assert.match(toasts.at(-1)?.description ?? "", /MiniMax M3/);
  }

  assert.equal(new Set(toasts.map((toast) => toast.title)).size, cases.length);
});

test("infers authentication from legacy status and preserves old and new GLM labels", () => {
  const toasts: CapturedToast[] = [];
  const actions: CredentialSettingsRequest[] = [];
  const base = {
    apiVersion: "1",
    type: "post_processing_fallback_used" as const,
    primaryModel: "cerebras/gemma-4-31b",
    desktopNotificationAccepted: false,
  };

  showPostProcessingFallbackToast(
    (toast) => toasts.push(toast),
    t,
    {
      ...base,
      eventId: "legacy-auth",
      fallbackModel: "z-ai/glm-5.2:nitro",
      reason: "invalid_request_error",
      primaryFailureStatus: 401,
    },
    new Set(),
    (request) => {
      actions.push(request);
      return {} as never;
    },
  );
  showPostProcessingFallbackToast(
    (toast) => toasts.push(toast),
    t,
    {
      ...base,
      eventId: "new-glm",
      fallbackModel: "z-ai/glm-5.3-flash:nitro",
    },
    new Set(),
  );

  assert.match(toasts[0]?.description ?? "", /GLM 5\.2/);
  assert.match(toasts[1]?.description ?? "", /GLM 5\.3 Flash/);
  assert.deepEqual(actions, [{ provider: "Cerebras", actionLabel: "Check Cerebras key" }]);
});

test("rejects malformed fallback events without showing a toast", () => {
  const toasts: unknown[] = [];
  const shown = showPostProcessingFallbackToast(
    (toast) => toasts.push(toast),
    (key) => key,
    {
      apiVersion: "1",
      type: "post_processing_fallback_used",
      eventId: "",
      primaryModel: "primary/model",
      fallbackModel: "fallback/model",
      desktopNotificationAccepted: false,
    },
    new Set(),
  );

  assert.equal(shown, false);
  assert.deepEqual(toasts, []);
});
