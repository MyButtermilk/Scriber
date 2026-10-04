const assert = require("node:assert/strict");
const { test } = require("node:test");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const session = require("../session.js");

function harness(permission = true) {
  let listener;
  const requests = [];
  const cookieReads = [];
  const id = "a".repeat(32);
  const context = vm.createContext({
    ScriberYouTubeSession: session,
    importScripts: () => {},
    crypto: webcrypto,
    AbortSignal,
    setTimeout: (callback) => setTimeout(callback, 0),
    chrome: {
      permissions: { contains: async () => permission },
      cookies: {
        getAll: async (query) => {
          cookieReads.push(query);
          return [
            {
              domain: ".youtube.com",
              path: "/",
              name: "SID",
              value: "private-session",
            },
          ];
        },
      },
      runtime: {
        id,
        getURL: (file) => `chrome-extension://${id}/${file}`,
        onMessage: {
          addListener: (fn) => {
            listener = fn;
          },
        },
      },
    },
    fetch: async (url, options) => {
      requests.push({ url, options, payload: JSON.parse(options.body) });
      return { ok: true, status: 200 };
    },
  });
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, "../background.js"), "utf8"),
    context,
  );
  const sender = { id, url: `chrome-extension://${id}/popup.html` };
  return {
    requests,
    cookieReads,
    sender,
    call: (senderOverride = sender) =>
      new Promise((resolve) => {
        if (
          !listener(
            { type: "youtube-session", videoId: "BFKcC0VyuZA" },
            senderOverride,
            resolve,
          )
        )
          resolve(false);
      }),
  };
}

test("hands off a YouTube-only session with a one-use capability outside URLs", async () => {
  const h = harness();
  assert.equal((await h.call()).connected, true);
  assert.equal(h.cookieReads.length, 1);
  assert.equal(h.cookieReads[0].url, "https://www.youtube.com/");
  assert.deepEqual(
    h.requests.map((r) => r.url),
    [
      "http://127.0.0.1:8765/api/youtube/browser-session/offer",
      "http://127.0.0.1:8765/api/youtube/browser-session/upload",
    ],
  );
  assert.match(h.requests[0].payload.secret, /^[a-f0-9]{64}$/);
  assert.equal(h.requests[0].payload.cookies, undefined);
  assert.equal(h.requests[0].payload.secret, h.requests[1].payload.secret);
  assert.ok(h.requests[1].payload.cookies.includes("private-session"));
  assert.equal(h.requests[1].options.redirect, "error");
  assert.equal(h.requests[1].options.credentials, "omit");
});

test("denied optional permission and foreign senders never read cookies or connect", async () => {
  const denied = harness(false);
  assert.equal((await denied.call()).connected, false);
  assert.equal(denied.cookieReads.length, 0);
  assert.equal(denied.requests.length, 0);
  const h = harness();
  assert.equal(await h.call({ ...h.sender, url: "https://evil.test/" }), false);
  assert.equal(await h.call({ ...h.sender, id: "b".repeat(32) }), false);
  assert.equal(h.cookieReads.length, 0);
});
