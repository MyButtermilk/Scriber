import assert from "node:assert/strict";
import test from "node:test";
import { requiresYouTubeSignIn } from "./request-errors";

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
