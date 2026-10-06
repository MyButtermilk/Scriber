import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { preloadPrimaryTabData } from "./tab-data-preload";
import { invalidateSettingsBootstrap } from "./settings-bootstrap";
import { fetchTranscriptHistoryPage } from "@/hooks/use-transcript-history-query";
import { fetchWithTimeout } from "./fetch-with-timeout";

vi.mock("./fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/hooks/use-transcript-history-query", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/use-transcript-history-query")>()),
  fetchTranscriptHistoryPage: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
let client: QueryClient;
let startIdle: () => void;
let settings: ReturnType<typeof deferred<Response>>;
let microphones: ReturnType<typeof deferred<Response>>;
const frames: FrameRequestCallback[] = [];
beforeEach(() => {
  invalidateSettingsBootstrap();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  settings = deferred<Response>();
  microphones = deferred<Response>();
  vi.mocked(fetchWithTimeout).mockImplementation((url) => {
    if (String(url).endsWith("/api/settings")) return settings.promise;
    if (String(url).endsWith("/api/microphones")) return microphones.promise;
    if (String(url).endsWith("/api/autostart"))
      return Promise.resolve(Response.json({ enabled: false, available: false }));
    return new Promise(() => {});
  });
  frames.length = 0;
  vi.stubGlobal("requestIdleCallback", (callback: () => void) => {
    startIdle = callback;
    return 1;
  });
  vi.stubGlobal("cancelIdleCallback", vi.fn());
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    frames.push(callback);
    return frames.length;
  });
  vi.mocked(fetchTranscriptHistoryPage).mockResolvedValue({
    items: [],
    total: 0,
    offset: 0,
    limit: 100,
    hasMore: false,
  });
});
afterEach(() => {
  client.clear();
  invalidateSettingsBootstrap();
  vi.unstubAllGlobals();
});

it("history warms before slow settings and Meeting reads finish, one page at a time", async () => {
  const stop = preloadPrimaryTabData(client);
  startIdle();
  await vi.waitFor(() => expect(frames.length).toBe(1));
  expect(vi.mocked(fetchTranscriptHistoryPage).mock.calls.map(([args]) => args.type)).toEqual(["mic"]);
  frames.shift()!(0);
  await vi.waitFor(() => expect(frames.length).toBe(1));
  expect(vi.mocked(fetchTranscriptHistoryPage).mock.calls.map(([args]) => args.type)).toEqual(["mic", "youtube"]);
  stop();
  frames.shift()!(0);
  await Promise.resolve();
  expect(fetchTranscriptHistoryPage).toHaveBeenCalledTimes(2);
  expect(fetchTranscriptHistoryPage).toHaveBeenCalledWith(expect.objectContaining({ signal: expect.any(AbortSignal) }));
});

it("canceling idle preload prevents work and suppresses late settings publication", async () => {
  const canceled = preloadPrimaryTabData(client);
  canceled();
  startIdle();
  expect(fetchWithTimeout).not.toHaveBeenCalled();
  const stop = preloadPrimaryTabData(client);
  startIdle();
  stop();
  client.setQueryData(["/api/settings"], { language: "newer" });
  settings.resolve(Response.json({ language: "stale" }));
  microphones.resolve(Response.json({ devices: [] }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(client.getQueryData(["/api/settings"])).toEqual({ language: "newer" });
});

it("publishes settings before microphones and never republishes the old snapshot after a save", async () => {
  const stop = preloadPrimaryTabData(client);
  startIdle();
  settings.resolve(Response.json({ language: "en" }));
  await vi.waitFor(() => expect(client.getQueryData(["/api/settings"])).toEqual({ language: "en" }));
  client.setQueryData(["/api/settings"], { language: "de" });
  invalidateSettingsBootstrap();
  microphones.resolve(Response.json({ devices: [] }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(client.getQueryData(["/api/settings"])).toEqual({ language: "de" });
  stop();
});

it("does not publish an invalidated settings request even without a replacement query value", async () => {
  const stop = preloadPrimaryTabData(client);
  startIdle();
  invalidateSettingsBootstrap();
  settings.resolve(Response.json({ language: "stale" }));
  microphones.resolve(Response.json({ devices: [] }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(client.getQueryData(["/api/settings"])).toBeUndefined();
  stop();
});

it("preserves independent query writes even when their timestamps are identical", async () => {
  vi.spyOn(Date, "now").mockReturnValue(1000);
  client.setQueryData(["/api/settings"], { language: "initial" });
  const stop = preloadPrimaryTabData(client);
  startIdle();
  client.setQueryData(["/api/settings"], { language: "newer" });
  settings.resolve(Response.json({ language: "stale" }));
  microphones.resolve(Response.json({ devices: [] }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(client.getQueryData(["/api/settings"])).toEqual({ language: "newer" });
  stop();
});
