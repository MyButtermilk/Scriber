import assert from "node:assert/strict";
import test from "node:test";
import { youtubeOnlyCookieFile } from "./youtube-session";

test("cookie import removes unrelated account credentials before transmission", () => {
  const content =
    "\uFEFF# Netscape HTTP Cookie File\r\n" +
    ".google.com\tTRUE\t/\tTRUE\t0\tSID\tgoogle-secret\r\n" +
    ".youtube.com.evil.test\tTRUE\t/\tTRUE\t0\tSID\tevil-secret\r\n" +
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-secret\r\n";
  const filtered = youtubeOnlyCookieFile(content);
  assert.equal(filtered, "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-secret");
});
