const assert = require("node:assert/strict");
const { test } = require("node:test");
const session = require("../session.js");

test("exports only HTTPS YouTube cookies and preserves HttpOnly/session semantics", () => {
  const cookie = {
    domain: ".youtube.com",
    path: "/",
    httpOnly: true,
    name: "SID",
    value: "yt-secret",
  };
  const result = session.cookieFile([
    cookie,
    { ...cookie, domain: ".google.com", value: "foreign" },
    { ...cookie, domain: ".youtube.com.evil.test", value: "foreign" },
  ]);
  assert.equal(
    result,
    "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyt-secret",
  );
  assert.equal(result.includes("foreign"), false);
});

test("optional permission stays limited to YouTube and the local Scriber endpoint", () => {
  const manifest = require("../manifest.json");
  assert.deepEqual(
    manifest.optional_permissions,
    session.permissions.permissions,
  );
  assert.deepEqual(
    manifest.optional_host_permissions,
    session.permissions.origins,
  );
  assert.deepEqual(manifest.permissions, ["activeTab"]);
});

test("visitor-only, empty and expired account cookies cannot replace an authenticated session", () => {
  const cookie = {
    domain: ".youtube.com",
    path: "/",
    name: "VISITOR_INFO1_LIVE",
    value: "visitor",
  };
  assert.equal(session.cookieFile([cookie]), null);
  assert.equal(
    session.cookieFile([cookie, { ...cookie, name: "SID", value: "" }]),
    null,
  );
  assert.equal(
    session.cookieFile([cookie, { ...cookie, name: "SID", expirationDate: 1 }]),
    null,
  );
  assert.equal(
    session.cookieFile([
      cookie,
      { ...cookie, name: "SID", domain: ".google.com" },
    ]),
    null,
  );
});
