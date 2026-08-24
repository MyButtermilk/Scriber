(function initializeScriberYoutubeButton() {
  "use strict";

  const bridge = globalThis.ScriberYoutubeBridge;
  const ROOT_ID = "scriber-youtube-action";
  const LABELS = {
    idle: "Mit Scriber transkribieren",
    opening: "Öffne Scriber …",
    handedOff: "An Scriber übergeben",
    unavailable: "Kein Video erkannt",
  };
  let scheduled = false;
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

  function ensureButton() {
    scheduled = false;
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
