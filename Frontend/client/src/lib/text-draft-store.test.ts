import assert from "node:assert/strict";
import test from "node:test";
import { createTextDraftStore } from "./text-draft-store";

test("draft store notifies only for changed values and returns the latest text", () => {
  const store = createTextDraftStore("saved");
  const seen: string[] = [];
  const unsubscribe = store.subscribe(() => seen.push(store.get()));

  store.set("saved");
  store.set("saved!");
  store.set("saved!");
  store.set("");

  assert.deepEqual(seen, ["saved!", ""]);
  assert.equal(store.get(), "");
  unsubscribe();
  store.set("after");
  assert.deepEqual(seen, ["saved!", ""]);
  assert.equal(store.get(), "after");
});

test("nested writes from a listener leave every subscriber with the latest value", () => {
  const store = createTextDraftStore("en default");
  const replacements: string[] = [];
  const observed: string[] = [];
  store.subscribe(() => {
    if (store.get() === "en default") {
      replacements.push("de default");
      store.set("de default");
    }
  });
  store.subscribe(() => observed.push(store.get()));

  store.set("typed");
  store.set("en default");

  assert.deepEqual(replacements, ["de default"]);
  assert.equal(store.get(), "de default");
  assert.deepEqual(observed, ["typed", "de default", "de default"]);
});

test("a listener removed during notification is skipped", () => {
  const store = createTextDraftStore();
  const calls: string[] = [];
  let unsubscribeSecond = () => {};
  store.subscribe(() => {
    calls.push("first");
    unsubscribeSecond();
  });
  unsubscribeSecond = store.subscribe(() => calls.push("second"));

  store.set("a");
  store.set("b");

  assert.deepEqual(calls, ["first", "first"]);
});
