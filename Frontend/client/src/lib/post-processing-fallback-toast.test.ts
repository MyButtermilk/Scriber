import assert from "node:assert/strict";
import test from "node:test";

import { showPostProcessingFallbackToast } from "@/lib/post-processing-fallback-toast";

test("shows each successful post-processing fallback event exactly once", () => {
  const toasts: Array<{ title: string; description: string; duration: number }> = [];
  const seen = new Set<string>();
  const t = (key: string, values?: Record<string, string | number>) =>
    key.replace(/\{\{(\w+)\}\}/g, (_match, name: string) => String(values?.[name] ?? ""));
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
