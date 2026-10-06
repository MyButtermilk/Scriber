import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeetingDetail } from "./api-types";
import { createMeetingDetailPrefetch } from "./meeting-detail-prefetch";

let client: QueryClient;
let prefetch: ReturnType<typeof createMeetingDetailPrefetch>;
const requests: Array<{ id: string; signal: AbortSignal; finish: () => void }> = [];
const fetchDetail = vi.fn(
  (id: string, signal: AbortSignal) =>
    new Promise<MeetingDetail>((resolve) => {
      requests.push({ id, signal, finish: () => resolve({ id } as MeetingDetail) });
    }),
);
beforeEach(() => {
  vi.useFakeTimers();
  requests.length = 0;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prefetch = createMeetingDetailPrefetch(client, fetchDetail, 30_000);
});
afterEach(() => {
  prefetch.dispose();
  client.clear();
  vi.useRealTimers();
});

it("skips ten transient rows and fetches only the row with sustained intent", async () => {
  for (let i = 0; i < 10; i++) {
    prefetch.enter(String(i), "pointer");
    await vi.advanceTimersByTimeAsync(100);
    prefetch.leave(String(i), "pointer");
  }
  expect(fetchDetail).not.toHaveBeenCalled();
  prefetch.enter("wanted", "pointer");
  await vi.advanceTimersByTimeAsync(100);
  prefetch.enter("wanted", "focus");
  await vi.advanceTimersByTimeAsync(50);
  expect(requests.map((r) => r.id)).toEqual(["wanted"]);
  prefetch.leave("wanted", "pointer");
  expect(requests[0].signal.aborted).toBe(false);
  prefetch.leave("wanted", "focus");
  expect(requests[0].signal.aborted).toBe(true);
});

it("cancels obsolete speculative requests and retains only the latest candidate", async () => {
  prefetch.enter("a", "pointer");
  await vi.advanceTimersByTimeAsync(150);
  prefetch.enter("b", "pointer");
  expect(requests[0].signal.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(100);
  prefetch.enter("c", "pointer");
  await vi.advanceTimersByTimeAsync(150);
  expect(requests.map((r) => r.id)).toEqual(["a", "c"]);
  requests[0].finish();
  await vi.advanceTimersByTimeAsync(0);
  expect(client.getQueryData(["/api/meetings", "a"])).toBeUndefined();
});

it("keeps an adopted navigation request alive across leave and cleanup", async () => {
  prefetch.enter("a", "pointer");
  await vi.advanceTimersByTimeAsync(150);
  prefetch.forget("a", true);
  prefetch.dispose();
  expect(requests[0].signal.aborted).toBe(false);
  requests[0].finish();
  await vi.advanceTimersByTimeAsync(0);
  expect(client.getQueryData(["/api/meetings", "a"])).toEqual({ id: "a" });
});

it("never cancels a request started by another query consumer", async () => {
  const existing = client.fetchQuery({
    queryKey: ["/api/meetings", "a"],
    queryFn: ({ signal }) => fetchDetail("a", signal),
  });
  prefetch.enter("a", "focus");
  await vi.advanceTimersByTimeAsync(150);
  prefetch.leave("a", "focus");
  prefetch.dispose();
  expect(fetchDetail).toHaveBeenCalledTimes(1);
  expect(requests[0].signal.aborted).toBe(false);
  requests[0].finish();
  await existing;
});

it("preserves observed queries and bounds speculative concurrency", async () => {
  prefetch.enter("a", "pointer");
  await vi.advanceTimersByTimeAsync(150);
  const observer = new QueryObserver(client, { queryKey: ["/api/meetings", "a"], enabled: false });
  const unsubscribe = observer.subscribe(() => {});
  prefetch.enter("b", "pointer");
  await vi.advanceTimersByTimeAsync(150);
  expect(requests[0].signal.aborted).toBe(false);
  expect(fetchDetail).toHaveBeenCalledTimes(1);
  requests[0].finish();
  await vi.advanceTimersByTimeAsync(1);
  expect(requests.map((r) => r.id)).toEqual(["a", "b"]);
  unsubscribe();
});

it("reuses fresh data and cancels pending dwell on cleanup", async () => {
  client.setQueryData(["/api/meetings", "a"], { id: "a" });
  prefetch.enter("a", "focus");
  await vi.advanceTimersByTimeAsync(150);
  expect(fetchDetail).not.toHaveBeenCalled();
  prefetch.enter("b", "focus");
  prefetch.dispose();
  await vi.advanceTimersByTimeAsync(150);
  expect(fetchDetail).not.toHaveBeenCalled();
});
