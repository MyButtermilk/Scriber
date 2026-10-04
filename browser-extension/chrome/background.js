"use strict";
importScripts("session.js");

const endpoint = "http://127.0.0.1:8765/api/youtube/browser-session/";
const delay = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));
let transferring = false;

async function transfer(videoId) {
  if (transferring || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) return false;
  if (!(await chrome.permissions.contains(ScriberYouTubeSession.permissions)))
    return false;
  transferring = true;
  let content = "";
  const secret = Array.from(
    crypto.getRandomValues(new Uint8Array(32)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  const deadline = Date.now() + 35_000;
  async function post(path, body) {
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
  try {
    let offered = false;
    while (Date.now() < deadline) {
      if (!offered) {
        const response = await post("offer", { secret, videoId });
        if (response?.ok) {
          offered = true;
          content = ScriberYouTubeSession.cookieFile(
            await chrome.cookies.getAll({ url: "https://www.youtube.com/" }),
          );
        }
      } else {
        const response = await post("upload", { secret, cookies: content });
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

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || message?.type !== "youtube-session")
    return false;
  const source = sender.url || "";
  if (!(
    source === chrome.runtime.getURL("popup.html") ||
    /^https:\/\/(www\.|m\.)?youtube\.com\//.test(source)
  ))
    return false;
  transfer(message.videoId).then(
    (connected) => respond({ connected }),
    () => respond({ connected: false }),
  );
  return true;
});
