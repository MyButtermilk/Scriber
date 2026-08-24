(async function initializePopup() {
  "use strict";

  const bridge = globalThis.ScriberYoutubeBridge;
  const titleElement = document.getElementById("video-title");
  const statusElement = document.getElementById("status");
  const startButton = document.getElementById("start");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const videoId = bridge.extractVideoId(tab?.url || "");
  if (!videoId) {
    titleElement.textContent = "Kein unterstütztes YouTube-Video erkannt.";
    statusElement.textContent =
      "Unterstützt werden normale Videos, Shorts und Livestream-Seiten.";
    return;
  }

  const title = bridge.normalizedMetadata(
    String(tab?.title || "").replace(/\s+-\s+YouTube\s*$/i, ""),
    500,
  );
  titleElement.textContent = title || "YouTube-Video";
  statusElement.textContent =
    "Scriber muss installiert sein. Beim ersten Start fragt Chrome nach Bestätigung.";
  startButton.disabled = false;
  startButton.addEventListener("click", () => {
    const deepLink = bridge.buildDeepLink({ videoId, title });
    if (!deepLink) {
      return;
    }
    startButton.disabled = true;
    startButton.textContent = "Öffne Scriber …";
    statusElement.textContent =
      "Die Transkription wird an die lokale Scriber-App übergeben.";
    const launcher = document.createElement("a");
    launcher.href = deepLink;
    launcher.hidden = true;
    launcher.setAttribute("aria-hidden", "true");
    document.documentElement.appendChild(launcher);
    launcher.click();
    launcher.remove();
  });
})().catch((error) => {
  const statusElement = document.getElementById("status");
  if (statusElement) {
    statusElement.textContent = `Die aktive Seite konnte nicht gelesen werden: ${String(error?.message || error)}`;
  }
});
