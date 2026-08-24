import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { parseBrowserYoutubeImport, stripBrowserYoutubeImportParams } from "./browser-youtube-import";

describe("browser YouTube import", () => {
  it("builds the exact existing YouTube job input from a validated desktop navigation", () => {
    const parsed = parseBrowserYoutubeImport(
      "?browserVideo=0wEjbSYNUM8&browserRequest=0123456789abcdef0123456789abcdef" +
        "&browserTitle=Ein+Titel+%C3%BCber+KI&browserChannel=Beispiel",
    );

    assert.deepEqual(parsed, {
      requestId: "0123456789abcdef0123456789abcdef",
      item: {
        videoId: "0wEjbSYNUM8",
        url: "https://www.youtube.com/watch?v=0wEjbSYNUM8",
        title: "Ein Titel über KI",
        description: "",
        channelTitle: "Beispiel",
        publishedAt: "",
        thumbnailUrl: "https://i.ytimg.com/vi/0wEjbSYNUM8/hqdefault.jpg",
        duration: "00:00",
        durationSeconds: 0,
      },
    });
  });

  it("rejects malformed, duplicated, or control-character-bearing handoffs", () => {
    for (const search of [
      "?browserVideo=short&browserRequest=0123456789abcdef0123456789abcdef",
      "?browserVideo=0wEjbSYNUM8&browserRequest=not-a-request",
      "?browserVideo=0wEjbSYNUM8&browserVideo=abcdefghijk&browserRequest=0123456789abcdef0123456789abcdef",
      "?browserVideo=0wEjbSYNUM8&browserRequest=0123456789abcdef0123456789abcdef&browserTitle=%00bad",
    ]) {
      assert.equal(parseBrowserYoutubeImport(search), null, search);
    }
  });

  it("consumes only the one-shot browser parameters", () => {
    assert.equal(
      stripBrowserYoutubeImportParams(
        "?search=existing&browserVideo=0wEjbSYNUM8&browserRequest=0123456789abcdef0123456789abcdef" +
          "&browserTitle=Title&browserChannel=Channel&sort=views",
      ),
      "search=existing&sort=views",
    );
  });
});
