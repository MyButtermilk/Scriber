import { act, cleanup, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendStatusProvider, useBackendActions, useBackendStatus } from "./use-backend-status";

const native = vi.hoisted(() => ({
  invoke: vi.fn(),
  listen: vi.fn(),
  unlisten: vi.fn(),
  handler: undefined as ((event: { payload: unknown }) => void) | undefined,
  isTauriRuntime: vi.fn(() => true),
  loadAccess: vi.fn(),
  reportReady: vi.fn(),
  setBaseUrl: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: native.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: native.listen }));
vi.mock("@/lib/backend", () => ({
  apiUrl: (path: string) => `http://127.0.0.1:8765${path}`,
  backendSessionToken: "test-token",
  isTauriRuntime: native.isTauriRuntime,
  loadBackendBaseUrlFromTauri: native.loadAccess,
  reportFrontendReady: native.reportReady,
  setBackendBaseUrl: native.setBaseUrl,
  setBackendSessionTokenRequired: vi.fn(),
}));

function status(ready: boolean) {
  return {
    baseUrl: "http://127.0.0.1:8765",
    running: true,
    ready,
    starting: !ready,
    managed: true,
    message: ready ? "Ready" : "Backend is restarting",
    runtimeMode: "tauri-supervised",
    launchKind: "sidecar",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function settle() {
  await act(async () => {
    await vi.dynamicImportSettled();
  });
}

async function emit(payload: unknown = null) {
  await act(async () => {
    expect(native.handler).toBeDefined();
    native.handler?.({ payload });
    await vi.dynamicImportSettled();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  native.handler = undefined;
  native.isTauriRuntime.mockReset().mockReturnValue(true);
  native.loadAccess.mockReset().mockResolvedValue(undefined);
  native.reportReady.mockReset().mockResolvedValue(undefined);
  native.invoke.mockReset().mockResolvedValue(status(true));
  native.unlisten.mockReset();
  native.listen.mockReset().mockImplementation((_event: string, handler: typeof native.handler) => {
    native.handler = handler;
    return Promise.resolve(native.unlisten);
  });
  native.setBaseUrl.mockReset();
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Unexpected HTTP fallback")));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("backend status invalidation", () => {
  it("subscribes before the initial authoritative snapshot", async () => {
    const registration = deferred<() => void>();
    native.listen.mockReturnValueOnce(registration.promise);
    const { result } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();

    expect(native.listen).toHaveBeenCalledWith("backend-status-changed", expect.any(Function));
    expect(native.invoke).not.toHaveBeenCalled();
    await act(async () => registration.resolve(native.unlisten));
    await settle();

    expect(native.invoke).toHaveBeenCalledExactlyOnceWith("ensure_backend_running");
    expect(result.current.hasConnected).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("coalesces concurrent checks and queues one fresh snapshot for in-flight invalidations", async () => {
    const staleSnapshot = deferred<ReturnType<typeof status>>();
    native.invoke.mockReturnValueOnce(staleSnapshot.promise);
    const { result } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();

    let firstCheck!: Promise<boolean>;
    let secondCheck!: Promise<boolean>;
    act(() => {
      firstCheck = result.current.checkNow();
      secondCheck = result.current.checkNow();
      window.dispatchEvent(new Event("focus"));
    });
    expect(firstCheck).toBe(secondCheck);
    await emit();
    await emit();
    await emit();
    expect(native.invoke).toHaveBeenCalledTimes(1);

    await act(async () => staleSnapshot.resolve(status(false)));
    await settle();
    expect(await firstCheck).toBe(false);
    expect(native.invoke).toHaveBeenCalledTimes(2);
    expect(result.current.isOnline).toBe(true);
    expect(result.current.checkCount).toBe(2);
    expect(result.current.backendStarting).toBe(false);
  });

  it("refreshes restarting and ready states immediately without trusting event payloads", async () => {
    const { result } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();
    expect(result.current.isOnline).toBe(true);

    native.invoke.mockResolvedValueOnce(status(false));
    await emit({ ready: true, baseUrl: "http://untrusted.invalid" });
    expect(result.current.isOnline).toBe(false);
    expect(result.current.backendStarting).toBe(true);
    expect(result.current.hasConnected).toBe(true);
    expect(native.setBaseUrl).not.toHaveBeenCalledWith("http://untrusted.invalid");

    await emit({ ready: false });
    expect(result.current.isOnline).toBe(true);
    expect(result.current.backendStarting).toBe(false);
    expect(result.current.error).toBeNull();
    expect(native.invoke).toHaveBeenCalledTimes(3);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retains offline/online timers and focus recovery when listener registration fails", async () => {
    vi.spyOn(console, "debug").mockImplementation(() => {});
    native.listen.mockRejectedValueOnce(new Error("Listener unavailable"));
    native.invoke.mockResolvedValueOnce(status(false));
    const { result } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();
    expect(result.current.isOnline).toBe(false);

    await act(async () => vi.advanceTimersByTimeAsync(4999));
    expect(native.invoke).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await settle();
    expect(result.current.isOnline).toBe(true);
    expect(native.invoke).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(29999));
    expect(native.invoke).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await settle();
    expect(native.invoke).toHaveBeenCalledTimes(3);
    act(() => window.dispatchEvent(new Event("focus")));
    await settle();
    expect(native.invoke).toHaveBeenCalledTimes(4);
  });

  it("releases a listener that finishes registering after unmount without starting a check", async () => {
    const registration = deferred<() => void>();
    native.listen.mockReturnValueOnce(registration.promise);
    const { unmount } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();
    unmount();
    await act(async () => registration.resolve(native.unlisten));
    await settle();
    expect(native.unlisten).toHaveBeenCalledOnce();
    expect(native.invoke).not.toHaveBeenCalled();
  });

  it("bounds a stalled registration and still cleans up its eventual listener", async () => {
    vi.spyOn(console, "debug").mockImplementation(() => {});
    const registration = deferred<() => void>();
    native.listen.mockReturnValueOnce(registration.promise);
    const { result, unmount } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();
    await act(async () => vi.advanceTimersByTimeAsync(2999));
    expect(native.invoke).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    await settle();
    expect(result.current.hasConnected).toBe(true);
    expect(native.invoke).toHaveBeenCalledTimes(1);

    await act(async () => registration.resolve(native.unlisten));
    await settle();
    expect(native.invoke).toHaveBeenCalledTimes(1);
    unmount();
    expect(native.unlisten).toHaveBeenCalledOnce();
  });

  it("cleans up events, polling and queued work on unmount", async () => {
    const pendingSnapshot = deferred<ReturnType<typeof status>>();
    native.invoke.mockReturnValueOnce(pendingSnapshot.promise);
    const { unmount } = renderHook(useBackendStatus, { wrapper: BackendStatusProvider });
    await settle();
    await emit();
    unmount();
    expect(native.unlisten).toHaveBeenCalledOnce();
    await act(async () => pendingSnapshot.resolve(status(false)));
    await emit();
    act(() => window.dispatchEvent(new Event("focus")));
    await act(async () => vi.advanceTimersByTimeAsync(30000));
    expect(native.invoke).toHaveBeenCalledTimes(1);
  });
});

it("keeps action-only consumers stable while compatibility consumers observe health checks", async () => {
  let actionRenderCount = 0;
  let statusRenderCount = 0;
  let checkNow!: () => Promise<boolean>;
  function ActionsConsumer() {
    checkNow = useBackendActions().checkNow;
    actionRenderCount += 1;
    return null;
  }
  function StatusConsumer() {
    const backend = useBackendStatus();
    statusRenderCount += 1;
    return <output>{backend.isChecking ? "checking" : String(backend.checkCount)}</output>;
  }
  const { getByText } = render(
    <BackendStatusProvider>
      <ActionsConsumer />
      <StatusConsumer />
    </BackendStatusProvider>,
  );
  await settle();
  const initialAction = checkNow;
  const settledStatusRenders = statusRenderCount;
  const pendingSnapshot = deferred<ReturnType<typeof status>>();
  native.invoke.mockReturnValueOnce(pendingSnapshot.promise);
  act(() => {
    void checkNow();
  });
  await settle();
  expect(getByText("checking")).toBeInTheDocument();
  expect(statusRenderCount).toBeGreaterThan(settledStatusRenders);
  await act(async () => pendingSnapshot.resolve(status(false)));
  await settle();
  expect(getByText("2")).toBeInTheDocument();
  expect(actionRenderCount).toBe(1);
  expect(checkNow).toBe(initialAction);
});
