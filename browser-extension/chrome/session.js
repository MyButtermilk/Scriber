(function initializeSessionBridge(scope) {
  "use strict";
  const permissions = {
    permissions: ["cookies"],
    origins: ["https://*.youtube.com/*", "http://127.0.0.1:8765/*"],
  };
  function cookieFile(cookies) {
    const hosts = new Set([
      "youtube.com",
      "www.youtube.com",
      "m.youtube.com",
      "music.youtube.com",
    ]);
    return (
      "# Netscape HTTP Cookie File\n" +
      cookies
        .filter((cookie) =>
          hosts.has(cookie.domain.toLowerCase().replace(/^\./, "")),
        )
        .map((cookie) =>
          [
            cookie.httpOnly ? `#HttpOnly_${cookie.domain}` : cookie.domain,
            cookie.domain.startsWith(".") ? "TRUE" : "FALSE",
            cookie.path,
            "TRUE",
            cookie.expirationDate > 0 ? Math.floor(cookie.expirationDate) : 0,
            cookie.name,
            cookie.value,
          ].join("\t"),
        )
        .join("\n")
    );
  }
  scope.ScriberYouTubeSession = Object.freeze({ permissions, cookieFile });
  if (typeof module !== "undefined" && module.exports)
    module.exports = scope.ScriberYouTubeSession;
})(globalThis);
