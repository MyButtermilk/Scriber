import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { preloadPrimaryTabData } from "./tab-data-preload";
import { loadSettingsBootstrap } from "./settings-bootstrap";
import { fetchTranscriptHistoryPage } from "@/hooks/use-transcript-history-query";
import { fetchWithTimeout } from "./fetch-with-timeout";

vi.mock("./settings-bootstrap", () => ({ loadSettingsBootstrap: vi.fn() }));
vi.mock("./fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/hooks/use-transcript-history-query", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/use-transcript-history-query")>()),
  fetchTranscriptHistoryPage: vi.fn(),
}));

let client: QueryClient;
let startIdle: () => void;
const frames: FrameRequestCallback[] = [];
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
  vi.unstubAllGlobals();
});

it("history warms before slow settings and Meeting reads finish, one page at a time", async () => {
  vi.mocked(loadSettingsBootstrap).mockReturnValue(new Promise(() => {}));
  vi.mocked(fetchWithTimeout).mockReturnValue(new Promise(() => {}));
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
  let finish!: (value: Awaited<ReturnType<typeof loadSettingsBootstrap>>) => void;
  vi.mocked(loadSettingsBootstrap).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  vi.mocked(fetchWithTimeout).mockReturnValue(new Promise(() => {}));
  const canceled = preloadPrimaryTabData(client);
  canceled();
  startIdle();
  expect(loadSettingsBootstrap).not.toHaveBeenCalled();
  const stop = preloadPrimaryTabData(client);
  startIdle();
  stop();
  client.setQueryData(["/api/settings"], { language: "newer" });
  finish({
    settings: { language: "stale" },
    microphones: { devices: [] },
    autostart: { enabled: false, available: false },
  });
  await Promise.resolve();
  await Promise.resolve();
  expect(client.getQueryData(["/api/settings"])).toEqual({ language: "newer" });
});
