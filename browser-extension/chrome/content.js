(function initializeScriberYoutubeButton() {
  "use strict";

  const bridge = globalThis.ScriberYoutubeBridge;
  const ROOT_ID = "scriber-youtube-action";
  const CONNECT_TOAST_ID = "scriber-youtube-connect-toast";
  const CONNECT_HASH_PATTERN = /^#scriber-connect=([a-f0-9]{32})$/;
  const LABELS = {
    idle: "Mit Scriber transkribieren",
    opening: "Öffne Scriber …",
    handedOff: "An Scriber übergeben",
    unavailable: "Kein Video erkannt",
  };
  let scheduled = false;
  let connectToastTimer = null;
  const handledConnectIds = new Set();
  const stateTimers = new Set();

  function clearStateTimers() {
    for (const timer of stateTimers) {
      window.clearTimeout(timer);
    }
    stateTimers.clear();
  }

  function scheduleState(button, state, delayMs) {
    const timer = window.setTimeout(() => {
      stateTimers.delete(timer);
      setButtonState(button, state);
    }, delayMs);
    stateTimers.add(timer);
  }

  function visibleMetadata() {
    const metaTitle = document
      .querySelector('meta[name="title"]')
      ?.getAttribute("content");
    const headingTitle = document.querySelector(
      "ytd-watch-metadata h1 yt-formatted-string",
    )?.textContent;
    const documentTitle = document.title.replace(/\s+-\s+YouTube\s*$/i, "");
    const channel =
      document.querySelector("ytd-watch-metadata #owner #channel-name a")
        ?.textContent ||
      document.querySelector("#upload-info #channel-name a")?.textContent ||
      "";
    return {
      title: bridge.normalizedMetadata(
        metaTitle || headingTitle || documentTitle,
        500,
      ),
      channel: bridge.normalizedMetadata(channel, 300),
    };
  }

  function setButtonState(button, state) {
    button.dataset.state = state;
    button.disabled = state === "opening" || state === "handedOff";
    button.setAttribute("aria-label", LABELS[state] || LABELS.idle);
    const label = button.querySelector(".scriber-youtube-label");
    if (label) {
      label.textContent = LABELS[state] || LABELS.idle;
    }
  }

  function launchCurrentVideo(button) {
    const videoId = bridge.extractVideoId(window.location.href);
    if (!videoId) {
      setButtonState(button, "unavailable");
      clearStateTimers();
      scheduleState(button, "idle", 2200);
      return;
    }
    const deepLink = bridge.buildDeepLink({ videoId, ...visibleMetadata() });
    setButtonState(button, "opening");
    // A prior explicit permission in the toolbar popup enables later handoffs.
    // Session transfer is optional and must not consume the click's activation.
    try {
      void chrome.runtime
        .sendMessage({ type: "youtube-session", videoId })
        .catch(() => {});
    } catch {
      // An extension update can invalidate this page's old content script.
    }
    if (!deepLink) {
      return;
    }
    setButtonState(button, "opening");
    const launcher = document.createElement("a");
    launcher.href = deepLink;
    launcher.hidden = true;
    launcher.setAttribute("aria-hidden", "true");
    document.documentElement.appendChild(launcher);
    launcher.click();
    launcher.remove();

    clearStateTimers();
    scheduleState(button, "handedOff", 700);
    scheduleState(button, "idle", 3200);
  }

  function createButtonRoot() {
    const root = document.createElement("div");
    root.id = ROOT_ID;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "scriber-youtube-button";
    button.setAttribute("aria-label", LABELS.idle);
    button.innerHTML =
      '<span class="scriber-youtube-mark" aria-hidden="true">S</span>' +
      '<span class="scriber-youtube-label">Mit Scriber transkribieren</span>';
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      launchCurrentVideo(button);
    });
    root.appendChild(button);
    return root;
  }

  function preferredMountTarget() {
    for (const selector of [
      "ytd-watch-metadata #actions-inner #top-level-buttons-computed",
      "ytd-watch-metadata #actions-inner",
      "#above-the-fold #actions-inner",
    ]) {
      const target = document.querySelector(selector);
      if (target) {
        return target;
      }
    }
    return null;
  }

  function extractConnectId() {
    let hash = String(window.location.hash || "");
    if (!hash) {
      try {
        hash = new URL(String(window.location.href || "")).hash;
      } catch {
        hash = "";
      }
    }
    const match = CONNECT_HASH_PATTERN.exec(hash);
    return match ? match[1] : null;
  }

  function showConnectToast(message, tone, dismissMs) {
    if (connectToastTimer !== null) {
      window.clearTimeout(connectToastTimer);
      connectToastTimer = null;
    }
    let toast = document.getElementById(CONNECT_TOAST_ID);
    if (!toast) {
      toast = document.createElement("div");
      toast.id = CONNECT_TOAST_ID;
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      (document.body || document.documentElement).appendChild(toast);
    }
    toast.dataset.tone = tone;
    toast.textContent = message;
    if (dismissMs > 0) {
      connectToastTimer = window.setTimeout(() => {
        connectToastTimer = null;
        toast?.remove();
      }, dismissMs);
    }
  }

  function checkScriberConnect() {
    const connectId = extractConnectId();
    if (!connectId || handledConnectIds.has(connectId)) {
      return;
    }
    handledConnectIds.add(connectId);
    try {
      const parsed = new URL(String(window.location.href || ""));
      window.history?.replaceState?.(
        window.history.state,
        "",
        `${parsed.pathname}${parsed.search}`,
      );
    } catch {
      // Best-effort cleanup of the transient fragment.
    }
    showConnectToast("Verbinde YouTube-Anmeldung mit Scriber …", "info", 0);
    let responsePromise;
    try {
      responsePromise = chrome.runtime.sendMessage({
        type: "youtube-session-connect",
        connectId,
      });
    } catch {
      showConnectToast(
        "Bitte lade die Seite neu oder klicke auf das Scriber-Symbol.",
        "warning",
        8000,
      );
      return;
    }
    Promise.resolve(responsePromise)
      .then((result) => {
        if (result?.connected) {
          showConnectToast(
            "YouTube-Anmeldung an Scriber übergeben. Du kannst diesen Tab schließen.",
            "success",
            6000,
          );
          return;
        }
        if (result?.reason === "permission_required") {
          showConnectToast(
            "Klicke einmal auf das Scriber-Symbol oben rechts, um deine YouTube-Anmeldung für Scriber freizugeben.",
            "warning",
            12000,
          );
          return;
        }
        if (result?.reason === "not_signed_in") {
          showConnectToast(
            "Bitte melde dich bei YouTube an und klicke danach auf „Mit Scriber transkribieren“.",
            "warning",
            12000,
          );
          return;
        }
        showConnectToast(
          "Keine offene Verbindungsanfrage in Scriber gefunden. Starte die Verbindung erneut in Scriber.",
          "warning",
          8000,
        );
      })
      .catch(() => {
        showConnectToast(
          "Scriber konnte nicht erreicht werden. Stelle sicher, dass Scriber läuft.",
          "warning",
          8000,
        );
      });
  }

  function ensureButton() {
    scheduled = false;
    checkScriberConnect();
    const videoId = bridge.extractVideoId(window.location.href);
    let root = document.getElementById(ROOT_ID);
    if (!videoId) {
      clearStateTimers();
      root?.remove();
      return;
    }
    root ||= createButtonRoot();
    if (root.dataset.videoId && root.dataset.videoId !== videoId) {
      clearStateTimers();
      const button = root.querySelector(".scriber-youtube-button");
      if (button) {
        setButtonState(button, "idle");
      }
    }
    root.dataset.videoId = videoId;
    const target = preferredMountTarget();
    if (target) {
      root.className = "scriber-youtube-inline";
      if (root.parentElement !== target) {
        target.prepend(root);
      }
      return;
    }
    root.className = "scriber-youtube-floating";
    if (root.parentElement !== document.body) {
      document.body.appendChild(root);
    }
  }

  function scheduleEnsureButton() {
    if (scheduled) {
      return;
    }
    scheduled = true;
    window.setTimeout(ensureButton, 120);
  }

  const observer = new MutationObserver(scheduleEnsureButton);
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  window.addEventListener("popstate", scheduleEnsureButton);
  window.addEventListener("yt-navigate-finish", scheduleEnsureButton);
  scheduleEnsureButton();
})();
