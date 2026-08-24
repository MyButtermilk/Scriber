const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const bridge = require("../shared.js");
const manifest = require("../manifest.json");

describe("Scriber YouTube Chrome extension", () => {
  it("extracts only supported HTTPS YouTube video routes", () => {
    assert.equal(
      bridge.extractVideoId("https://www.youtube.com/watch?v=0wEjbSYNUM8&t=5"),
      "0wEjbSYNUM8",
    );
    assert.equal(
      bridge.extractVideoId("https://www.youtube.com/shorts/0wEjbSYNUM8"),
      "0wEjbSYNUM8",
    );
    assert.equal(
      bridge.extractVideoId(
        "https://www.youtube.com/live/0wEjbSYNUM8?feature=share",
      ),
      "0wEjbSYNUM8",
    );
    assert.equal(
      bridge.extractVideoId("https://youtu.be/0wEjbSYNUM8"),
      "0wEjbSYNUM8",
    );
    assert.equal(
      bridge.extractVideoId("http://www.youtube.com/watch?v=0wEjbSYNUM8"),
      null,
    );
    assert.equal(
      bridge.extractVideoId("https://notyoutube.com/watch?v=0wEjbSYNUM8"),
      null,
    );
    assert.equal(
      bridge.extractVideoId("https://www.youtube.com/watch?v=short"),
      null,
    );
  });

  it("builds the narrow versioned Scriber protocol without secrets", () => {
    const link = bridge.buildDeepLink({
      videoId: "0wEjbSYNUM8",
      title: "Ein Titel über KI",
      channel: "Beispiel",
    });
    const parsed = new URL(link);
    assert.equal(parsed.protocol, "scriber:");
    assert.equal(parsed.hostname, "youtube");
    assert.equal(parsed.pathname, "/transcribe");
    assert.equal(parsed.searchParams.get("v"), "1");
    assert.equal(parsed.searchParams.get("video"), "0wEjbSYNUM8");
    assert.equal(parsed.searchParams.get("title"), "Ein Titel über KI");
    assert.equal(parsed.searchParams.get("channel"), "Beispiel");
    assert.equal(parsed.searchParams.has("token"), false);
    assert.equal(parsed.searchParams.has("port"), false);
  });

  it("keeps the manifest on MV3 with no broad web or local-network permission", () => {
    assert.equal(manifest.manifest_version, 3);
    assert.deepEqual(manifest.permissions, ["activeTab"]);
    assert.equal(manifest.host_permissions, undefined);
    const matches = manifest.content_scripts.flatMap((entry) => entry.matches);
    assert(
      matches.every(
        (match) =>
          match.startsWith("https://") && match.includes("youtube.com/"),
      ),
    );
    assert(
      matches.every(
        (match) =>
          !match.includes("<all_urls>") && !match.includes("127.0.0.1"),
      ),
    );
  });
});
