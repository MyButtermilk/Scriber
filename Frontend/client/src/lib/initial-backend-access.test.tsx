import { afterEach, beforeEach, expect, it, vi } from "vitest";

const native = vi.hoisted(() => ({ isTauri: true, load: vi.fn() }));
vi.mock("./backend", () => ({
  isTauriRuntime: () => native.isTauri,
  loadBackendBaseUrlFromTauri: native.load,
}));

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  native.isTauri = true;
  native.load.mockReset();
  vi.spyOn(console, "debug").mockImplementation(() => undefined);
});
afterEach(() => vi.useRealTimers());

it("overlaps access with module loading and reuses the settled result at mount", async () => {
  const { loadInitialBackendAccess, isInitialBackendAccessReady } = await import("./initial-backend-access");
  native.load.mockImplementation(() => new Promise((resolve) => setTimeout(() => resolve("http://localhost"), 120)));
  const early = loadInitialBackendAccess();
  expect(isInitialBackendAccessReady()).toBe(false);
  expect(loadInitialBackendAccess()).toBe(early);
  // Simulated module evaluation takes 200 ms. Previously access started only
  // afterwards, adding 120 ms to this deterministic startup fixture.
  await vi.advanceTimersByTimeAsync(200);
  expect(isInitialBackendAccessReady()).toBe(true);
  expect(loadInitialBackendAccess()).toBe(early);
  await loadInitialBackendAccess();
  expect(native.load).toHaveBeenCalledTimes(1);
  expect(vi.getTimerCount()).toBe(0);
});

it("keeps the access gate closed while the native response is pending", async () => {
  let complete!: (url: string) => void;
  native.load.mockReturnValue(
    new Promise<string>((resolve) => {
      complete = resolve;
    }),
  );
  const { loadInitialBackendAccess, isInitialBackendAccessReady } = await import("./initial-backend-access");
  const pending = loadInitialBackendAccess();
  await vi.advanceTimersByTimeAsync(1000);
  expect(isInitialBackendAccessReady()).toBe(false);
  complete("http://localhost");
  await pending;
  expect(isInitialBackendAccessReady()).toBe(true);
});

it("bounds hung IPC and permits the existing health fallback without repeated startup waits", async () => {
  native.load.mockReturnValue(new Promise(() => {}));
  const { loadInitialBackendAccess, isInitialBackendAccessReady } = await import("./initial-backend-access");
  const pending = loadInitialBackendAccess();
  await vi.advanceTimersByTimeAsync(4999);
  expect(isInitialBackendAccessReady()).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  await pending;
  expect(isInitialBackendAccessReady()).toBe(true);
  await loadInitialBackendAccess();
  expect(native.load).toHaveBeenCalledTimes(1);
  expect(console.debug).toHaveBeenCalledOnce();
});

it("observes an early failure even before a React consumer mounts", async () => {
  native.load.mockRejectedValue(new Error("IPC unavailable"));
  const { loadInitialBackendAccess, isInitialBackendAccessReady } = await import("./initial-backend-access");
  await expect(loadInitialBackendAccess()).resolves.toBeUndefined();
  expect(isInitialBackendAccessReady()).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});

it("keeps the browser path synchronous and performs no native lookup", async () => {
  native.isTauri = false;
  const { loadInitialBackendAccess, isInitialBackendAccessReady } = await import("./initial-backend-access");
  expect(isInitialBackendAccessReady()).toBe(true);
  await loadInitialBackendAccess();
  expect(native.load).not.toHaveBeenCalled();
});
