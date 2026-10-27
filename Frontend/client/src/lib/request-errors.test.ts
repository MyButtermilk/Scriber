import assert from "node:assert/strict";
import test from "node:test";
import {
  ApiRequestError,
  isFailureOnlyContent,
  requiresYouTubeSignIn,
  responseErrorMessage,
  responseRequestError,
} from "./request-errors";

test("YouTube recovery covers legacy bot and reload errors without capturing transient 403s", () => {
  assert.equal(requiresYouTubeSignIn("ERROR: [youtube] BFKcC0VyuZA: Sign in to confirm you’re not a bot."), true);
  assert.equal(requiresYouTubeSignIn("ERROR: The page needs to be reloaded."), true);
  assert.equal(
    requiresYouTubeSignIn("YouTube requires sign-in. Import your YouTube sign-in in Settings, then retry this video."),
    true,
  );
  assert.equal(requiresYouTubeSignIn("HTTP Error 403: Forbidden"), false);
  assert.equal(requiresYouTubeSignIn("Requested format is not available"), false);
});

test("only failed records whose content is a stored failure message are failure-only", () => {
  assert.equal(isFailureOnlyContent("failed", "  [Error] Soniox rejected the API key."), true);
  assert.equal(isFailureOnlyContent("failed", "[Timeout] File transcription timed out."), true);
  assert.equal(isFailureOnlyContent("failed", "Recognized speech before the failure"), false);
  assert.equal(isFailureOnlyContent("completed", "[Error] quoted inside a real transcript"), false);
  assert.equal(isFailureOnlyContent("failed", undefined), false);
});

test("request failures preserve a valid content-free reference beside the existing message", async () => {
  const reference = "a1".repeat(16);
  const error = await responseRequestError(
    Response.json({ message: "Podcast request failed.", reference }, { status: 503 }),
  );
  assert.ok(error instanceof ApiRequestError);
  assert.equal(error.message, "503: Podcast request failed.");
  assert.equal(error.reference, reference);
  assert.equal(
    await responseErrorMessage(Response.json({ error: "Legacy failure" }, { status: 400 })),
    "400: Legacy failure",
  );
});

test("request references cannot smuggle response content into diagnostic UI", async () => {
  for (const reference of [
    "https://private.example/feed?token=secret",
    "a".repeat(33),
    "A".repeat(32),
    { secret: "value" },
    5,
    null,
  ]) {
    const error = await responseRequestError(Response.json({ message: "Request failed.", reference }, { status: 503 }));
    assert.equal(error.reference, undefined);
  }
  assert.equal((await responseRequestError(new Response("Unavailable", { status: 502 }))).message, "502: Unavailable");
});
