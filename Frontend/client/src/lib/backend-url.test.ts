import assert from "node:assert/strict";
import test from "node:test";

import { appendBackendSessionToken } from "@/lib/backend-url";

function tokenFor(candidate: string, backend: string): string | null {
  const result = appendBackendSessionToken(candidate, backend, "secret", "http://fallback.invalid");
  return new URL(result).searchParams.get("scriberToken");
}

test("normalizes default ports for matching HTTP and WebSocket transports", () => {
  assert.equal(tokenFor("http://scriber.local:80/api/health", "http://scriber.local"), "secret");
  assert.equal(tokenFor("ws://scriber.local/ws", "http://scriber.local:80"), "secret");
  assert.equal(tokenFor("https://scriber.local:443/api/health", "https://scriber.local"), "secret");
  assert.equal(tokenFor("wss://scriber.local/ws", "https://scriber.local:443"), "secret");
});

test("does not attach a token across secure and insecure transport families", () => {
  assert.equal(tokenFor("http://scriber.local:443/api/health", "https://scriber.local"), null);
  assert.equal(tokenFor("https://scriber.local:80/api/health", "http://scriber.local"), null);
  assert.equal(tokenFor("ws://scriber.local:443/ws", "https://scriber.local"), null);
  assert.equal(tokenFor("wss://scriber.local:80/ws", "http://scriber.local"), null);
});

test("limits the token to the configured backend host, port, and authenticated paths", () => {
  assert.equal(tokenFor("http://other.local/api/health", "http://scriber.local"), null);
  assert.equal(tokenFor("http://scriber.local:8766/api/health", "http://scriber.local:8765"), null);
  assert.equal(tokenFor("http://scriber.local/public", "http://scriber.local"), null);
});

test("preserves existing query parameters and replaces a stale token", () => {
  const result = appendBackendSessionToken(
    "http://scriber.local/api/health?mode=full&scriberToken=stale",
    "http://scriber.local:80",
    "fresh",
    "http://fallback.invalid",
  );
  const parsed = new URL(result);
  assert.equal(parsed.searchParams.get("mode"), "full");
  assert.equal(parsed.searchParams.get("scriberToken"), "fresh");
});
