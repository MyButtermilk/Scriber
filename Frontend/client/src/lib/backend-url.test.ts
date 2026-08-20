import assert from "node:assert/strict";
import test from "node:test";

import { targetsSameBackend } from "@/lib/backend-url";

function matches(candidate: string, backend: string): boolean {
  return targetsSameBackend(new URL(candidate), new URL(backend));
}

test("normalizes default ports for matching HTTP and WebSocket transports", () => {
  assert.equal(matches("http://scriber.local:80/api/health", "http://scriber.local"), true);
  assert.equal(matches("ws://scriber.local/ws", "http://scriber.local:80"), true);
  assert.equal(matches("https://scriber.local:443/api/health", "https://scriber.local"), true);
  assert.equal(matches("wss://scriber.local/ws", "https://scriber.local:443"), true);
});

test("rejects matches across secure and insecure transport families", () => {
  assert.equal(matches("http://scriber.local:443/api/health", "https://scriber.local"), false);
  assert.equal(matches("https://scriber.local:80/api/health", "http://scriber.local"), false);
  assert.equal(matches("ws://scriber.local:443/ws", "https://scriber.local"), false);
  assert.equal(matches("wss://scriber.local:80/ws", "http://scriber.local"), false);
});

test("requires the configured backend hostname and effective port", () => {
  assert.equal(matches("http://other.local/api/health", "http://scriber.local"), false);
  assert.equal(matches("http://scriber.local:8766/api/health", "http://scriber.local:8765"), false);
  assert.equal(matches("http://scriber.local:8765/api/health", "http://scriber.local:8765"), true);
});

test("rejects unsupported transport protocols", () => {
  assert.equal(matches("ftp://scriber.local/api/health", "http://scriber.local"), false);
  assert.equal(matches("http://scriber.local/api/health", "ftp://scriber.local"), false);
});
