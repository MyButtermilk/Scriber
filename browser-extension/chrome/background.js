"use strict";
importScripts("session.js");

const endpoint = "http://127.0.0.1:8765/api/youtube/browser-session/";
const delay = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));
let transferring = false;

async function postLocal(path, body) {
  try {
    return await fetch(endpoint + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.timeout(2_000),
    });
  } catch {
    return null;
  }
}

function randomSecret() {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function transfer(videoId) {
  if (transferring || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) return false;
  if (!(await chrome.permissions.contains(ScriberYouTubeSession.permissions)))
    return false;
  transferring = true;
  let content = "";
  const secret = randomSecret();
  const deadline = Date.now() + 35_000;
  try {
    content = ScriberYouTubeSession.cookieFile(
      await chrome.cookies.getAll({ url: "https://www.youtube.com/" }),
    );
    if (!content) return false;
    let offered = false;
    while (Date.now() < deadline) {
      if (!offered) {
        const response = await postLocal("offer", { secret, videoId });
        if (response?.ok) {
          offered = true;
        }
      } else {
        const response = await postLocal("upload", { secret, cookies: content });
        if (response?.ok) return true;
        if (response?.status === 400 || response?.status === 403) return false;
      }
      await delay(300);
    }
    return false;
  } finally {
    content = "";
    transferring = false;
  }
}

async function transferConnect(connectId) {
  if (!/^[a-f0-9]{32}$/.test(connectId))
    return { connected: false, reason: "invalid" };
  if (transferring) return { connected: false, reason: "busy" };
  if (!(await chrome.permissions.contains(ScriberYouTubeSession.permissions)))
    return { connected: false, reason: "permission_required" };
  transferring = true;
  let content = "";
  const secret = randomSecret();
  try {
    content = ScriberYouTubeSession.cookieFile(
      await chrome.cookies.getAll({ url: "https://www.youtube.com/" }),
    );
    if (!content) return { connected: false, reason: "not_signed_in" };
    let offerResponse = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      offerResponse = await postLocal("offer", { secret, connectId });
      if (offerResponse !== null) break;
      await delay(400);
    }
    if (!offerResponse)
      return { connected: false, reason: "app_unreachable" };
    if (!offerResponse.ok) return { connected: false, reason: "rejected" };
    const uploadResponse = await postLocal("upload", {
      secret,
      cookies: content,
    });
    if (uploadResponse?.ok)
      return { connected: true, reason: "connected" };
    return {
      connected: false,
      reason: uploadResponse ? "rejected" : "app_unreachable",
    };
  } finally {
    content = "";
    transferring = false;
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return false;
  const source = sender.url || "";
  if (message?.type === "youtube-session") {
    if (
      !(
        source === chrome.runtime.getURL("popup.html") ||
        /^https:\/\/(www\.|m\.)?youtube\.com\//.test(source)
      )
    )
      return false;
    transfer(message.videoId).then(
      (connected) => respond({ connected }),
      () => respond({ connected: false }),
    );
    return true;
  }
  if (message?.type === "youtube-session-connect") {
    if (!/^https:\/\/(www\.|m\.)?youtube\.com\//.test(source)) return false;
    transferConnect(message.connectId).then(
      (result) => respond(result),
      () => respond({ connected: false, reason: "app_unreachable" }),
    );
    return true;
  }
  return false;
});
