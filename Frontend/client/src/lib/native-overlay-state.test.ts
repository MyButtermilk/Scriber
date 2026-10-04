import assert from "node:assert/strict";
import test from "node:test";
import { createNativeOverlayStateGate } from "./native-overlay-state";

test("a newer renderer handshake supersedes an earlier hidden native event", () => {
  const accept = createNativeOverlayStateGate();
  assert.equal(accept({ revision: 2 }), true); // queued hide received during listener setup
  assert.equal(accept({ revision: 3 }), true); // ready snapshot: now recording
  assert.equal(accept({ revision: 2 }), false); // delayed hide cannot blank that recording
});

test("a late handshake cannot overwrite a newer recording event", () => {
  const accept = createNativeOverlayStateGate();
  assert.equal(accept({ revision: 5 }), true);
  assert.equal(accept({ revision: 4 }), false);
  assert.equal(accept({ revision: 5 }), true); // RMS with current lifecycle
  assert.equal(accept({ revision: 6 }), true); // authoritative hide
});

test("invalid or unversioned state cannot erase an observed native revision", () => {
  const accept = createNativeOverlayStateGate();
  assert.equal(accept({}), true);
  for (const revision of [-1, Number.NaN, Number.POSITIVE_INFINITY, 1.5]) {
    assert.equal(accept({ revision }), false);
  }
  assert.equal(accept({ revision: 0 }), true);
  assert.equal(accept({}), false);
});
