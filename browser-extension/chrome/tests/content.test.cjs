const assert = require("node:assert/strict");
const { test } = require("node:test");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

// Minimal inert DOM: run the actual content script; never navigate a browser.
function harness(sendMessage) {
  const elements = [],
    timers = new Map(),
    launches = [];
  let timerId = 0;
  function element(tag) {
    const node = {
      tag,
      dataset: {},
      children: [],
      listeners: {},
      className: "",
      parentElement: null,
      setAttribute() {},
      remove() {},
      appendChild(child) {
        this.children.push(child);
        child.parentElement = this;
      },
      addEventListener(name, callback) {
        this.listeners[name] = callback;
      },
      querySelector(selector) {
        return (
          this.children.find((child) => selector === "." + child.className) ||
          null
        );
      },
      click() {
        if (this.disabled) return;
        if (tag === "a") launches.push(this.href);
        else
          this.listeners.click?.({ preventDefault() {}, stopPropagation() {} });
      },
    };
    elements.push(node);
    return node;
  }
  const document = {
    title: "Test video - YouTube",
    documentElement: element("html"),
    body: element("body"),
    createElement: element,
    querySelector: () => null,
    getElementById: (id) => elements.find((node) => node.id === id) || null,
  };
  const context = vm.createContext({
    URL,
    URLSearchParams,
    document,
    chrome: { runtime: { sendMessage } },
    MutationObserver: class {
      observe() {}
    },
    window: {
      location: { href: "https://www.youtube.com/watch?v=BFKcC0VyuZA" },
      addEventListener() {},
      clearTimeout: (id) => timers.delete(id),
      setTimeout: (callback) => {
        timers.set(++timerId, callback);
        return timerId;
      },
    },
  });
  for (const file of ["shared.js", "content.js"]) {
    vm.runInContext(
      fs.readFileSync(path.join(__dirname, "..", file), "utf8"),
      context,
    );
  }
  function flushTimers() {
    for (const [id, callback] of [...timers]) {
      timers.delete(id);
      callback();
    }
  }
  flushTimers();
  return {
    button: elements.find((node) => node.tag === "button"),
    launches,
    flushTimers,
  };
}

for (const [name, sendMessage] of [
  ["denied optional permission", async () => ({ connected: false })],
  ["pending optional transfer", () => new Promise(() => {})],
  ["unavailable worker", () => Promise.reject(new Error("unavailable"))],
  [
    "invalidated extension context",
    () => {
      throw new Error("context invalidated");
    },
  ],
])
  test(`in-page launch stays synchronous and usable with ${name}`, () => {
    const h = harness(sendMessage);
    h.button.click();
    assert.equal(h.launches.length, 1);
    assert.match(h.launches[0], /^scriber:\/\/youtube\/transcribe\?/);
    h.flushTimers();
    h.button.click();
    assert.equal(h.launches.length, 2);
  });
