(function initializeScriberYoutubeBridge(globalScope) {
  "use strict";

  const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;
  const YOUTUBE_HOSTS = new Set([
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
  ]);

  function validVideoId(value) {
    const normalized = String(value || "").trim();
    return VIDEO_ID_PATTERN.test(normalized) ? normalized : null;
  }

  function extractVideoId(rawUrl) {
    let parsed;
    try {
      parsed = new URL(String(rawUrl || ""));
    } catch {
      return null;
    }
    const hostname = parsed.hostname.toLowerCase();
    if (parsed.protocol !== "https:" || !YOUTUBE_HOSTS.has(hostname)) {
      return null;
    }
    if (hostname === "youtu.be" || hostname === "www.youtu.be") {
      return validVideoId(parsed.pathname.split("/").filter(Boolean)[0]);
    }
    if (parsed.pathname === "/watch") {
      return validVideoId(parsed.searchParams.get("v"));
    }
    const segments = parsed.pathname.split("/").filter(Boolean);
    if (["shorts", "live", "embed", "v"].includes(segments[0])) {
      return validVideoId(segments[1]);
    }
    return null;
  }

  function normalizedMetadata(value, maxCharacters) {
    const normalized = String(value || "").trim();
    const characters = Array.from(normalized);
    const hasControlCharacter = characters.some((character) => {
      const codePoint = character.codePointAt(0) || 0;
      return codePoint <= 0x1f || codePoint === 0x7f;
    });
    if (!normalized || hasControlCharacter) {
      return "";
    }
    return characters.slice(0, maxCharacters).join("");
  }

  function buildDeepLink({ videoId, title = "", channel = "" }) {
    const normalizedVideoId = validVideoId(videoId);
    if (!normalizedVideoId) {
      return null;
    }
    const params = new URLSearchParams({ v: "1", video: normalizedVideoId });
    const normalizedTitle = normalizedMetadata(title, 500);
    const normalizedChannel = normalizedMetadata(channel, 300);
    if (normalizedTitle) {
      params.set("title", normalizedTitle);
    }
    if (normalizedChannel) {
      params.set("channel", normalizedChannel);
    }
    return `scriber://youtube/transcribe?${params.toString()}`;
  }

  const bridge = Object.freeze({
    buildDeepLink,
    extractVideoId,
    normalizedMetadata,
  });
  globalScope.ScriberYoutubeBridge = bridge;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = bridge;
  }
})(globalThis);
