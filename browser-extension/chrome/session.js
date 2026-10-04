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
    const authNames = new Set([
      "SID",
      "SAPISID",
      "__Secure-1PSID",
      "__Secure-3PSID",
      "__Secure-1PAPISID",
      "__Secure-3PAPISID",
    ]);
    const selected = cookies.filter(
      (cookie) =>
        hosts.has(cookie.domain.toLowerCase().replace(/^\./, "")) &&
        (!cookie.expirationDate || cookie.expirationDate * 1000 > Date.now()),
    );
    if (!selected.some((cookie) => authNames.has(cookie.name) && cookie.value))
      return null;
    return (
      "# Netscape HTTP Cookie File\n" +
      selected
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
