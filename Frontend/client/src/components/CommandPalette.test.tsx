import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import type { TranscriptHistoryItem } from "@/lib/api-types";
import { CommandPalette } from "./CommandPalette";

const { start, stop, socket } = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  socket: { listener: null as ((message: ScriberWebSocketMessage) => void) | null },
}));
vi.mock("@/contexts/WebSocketContext", () => ({
  useSharedWebSocket: (listener: (message: ScriberWebSocketMessage) => void) => {
    socket.listener = listener;
    return { isConnected: true };
  },
}));
vi.mock("@/lib/live-mic-control", () => ({
  requestLiveMicStart: start,
  requestLiveMicStop: stop,
  presentLiveMicControlFailure: vi.fn(),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/hooks/use-backend-status", () => ({
  useBackendActions: () => ({ checkNow: () => Promise.resolve(true) }),
}));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));

beforeEach(() => {
  window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  start.mockResolvedValue(new Response("{}"));
  stop.mockResolvedValue(new Response("{}"));
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

function mount(recent: TranscriptHistoryItem[] = []) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["/api/settings"], {});
  client.setQueryData(["/api/transcripts", { limit: 8 }], { items: recent });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <CommandPalette open onOpenChange={vi.fn()} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

function item(id: string, title: string, preview = "", type: TranscriptHistoryItem["type"] = "file") {
  return {
    id,
    title,
    preview,
    type,
    date: "",
    duration: "",
    status: "completed",
    createdAt: "2026-11-02T10:00:00Z",
  } satisfies TranscriptHistoryItem;
}

type Deferred = { url: string; resolve: (response: Response) => void };

/** Route /api/state to an idle snapshot and keep every search request pending until resolved. */
function routeFetch() {
  const searches: Deferred[] = [];
  vi.mocked(fetch).mockImplementation((input) => {
    const url = String(input);
    if (url.startsWith("/api/state")) {
      return Promise.resolve(Response.json({ sessionId: null, listening: false, recordingState: "idle" }));
    }
    return new Promise<Response>((resolve) => searches.push({ url, resolve }));
  });
  return searches;
}

function optionNames(): string[] {
  return screen.getAllByRole("option").map((option) => option.textContent || "");
}

it("lists Podcasts in navigation and shows the preview of untitled recent transcripts", async () => {
  routeFetch();
  mount([item("a".repeat(32), "", "  Guten   Morgen, heute sprechen wir über Gezeiten.  ", "mic")]);
  expect(await screen.findByRole("option", { name: "Podcasts" })).toBeInTheDocument();
  expect(screen.getByText("Recent Transcripts")).toBeInTheDocument();
  expect(screen.getByText("Guten Morgen, heute sprechen wir über Gezeiten.")).toBeInTheDocument();
  expect(screen.queryByText(/Transcript #/)).toBeNull();
});

it("debounces server full-text search and keeps content-only matches in server order", async () => {
  const searches = routeFetch();
  mount([item("r".repeat(32), "Recent only")]);
  const input = screen.getByPlaceholderText("Type a command or search transcripts...");

  fireEvent.change(input, { target: { value: "ti" } });
  fireEvent.change(input, { target: { value: "tide" } });
  fireEvent.change(input, { target: { value: "tides" } });
  // Nothing is requested until typing pauses.
  expect(searches).toHaveLength(0);
  await waitFor(() => expect(searches).toHaveLength(1), { timeout: 2_000 });
  expect(searches[0].url).toBe("/api/transcripts?q=tides&limit=20");

  await act(async () => {
    searches[0].resolve(
      Response.json({
        items: [
          item("1".repeat(32), "Weekly sync", "the tides were mentioned only in the content"),
          item("2".repeat(32), "", "Tides and moon phases"),
          item("3".repeat(32), "Ocean notes"),
        ],
      }),
    );
  });

  const options = await screen.findAllByRole("option");
  const transcriptNames = options.map((option) => option.textContent || "").filter((name) => !/Podcasts/.test(name));
  expect(transcriptNames[0]).toContain("Weekly sync");
  expect(transcriptNames[0]).toContain("the tides were mentioned only in the content");
  expect(transcriptNames[1]).toContain("Tides and moon phases");
  expect(transcriptNames[2]).toContain("Ocean notes");
  // Local commands are filtered by the same text; none match "tides".
  expect(screen.queryByRole("option", { name: "Settings" })).toBeNull();
  expect(screen.queryByText("Recent only")).toBeNull();
  expect(screen.getByText("Transcripts")).toBeInTheDocument();
});

it("encodes the query and filters local commands while server results load", async () => {
  const searches = routeFetch();
  mount();
  const input = screen.getByPlaceholderText("Type a command or search transcripts...");
  fireEvent.change(input, { target: { value: "pod & cast" } });
  await waitFor(() => expect(searches).toHaveLength(1), { timeout: 2_000 });
  expect(searches[0].url).toBe(`/api/transcripts?q=${encodeURIComponent("pod & cast")}&limit=20`);
  expect(screen.queryByText("No results found.")).toBeNull();
  expect(screen.getByText("Searching transcripts…")).toBeInTheDocument();

  fireEvent.change(input, { target: { value: "podc" } });
  expect(optionNames()).toEqual(["Podcasts"]);
});

it("never shows a slower response for an older query", async () => {
  const searches = routeFetch();
  mount();
  const input = screen.getByPlaceholderText("Type a command or search transcripts...");

  fireEvent.change(input, { target: { value: "alpha" } });
  await waitFor(() => expect(searches).toHaveLength(1), { timeout: 2_000 });
  fireEvent.change(input, { target: { value: "beta" } });
  await waitFor(() => expect(searches).toHaveLength(2), { timeout: 2_000 });
  expect(searches[1].url).toContain("q=beta");

  await act(async () => {
    searches[1].resolve(Response.json({ items: [item("b".repeat(32), "Beta result")] }));
  });
  expect(await screen.findByText("Beta result")).toBeInTheDocument();

  await act(async () => {
    searches[0].resolve(Response.json({ items: [item("a".repeat(32), "Alpha result")] }));
  });
  expect(screen.queryByText("Alpha result")).toBeNull();
  expect(screen.getByText("Beta result")).toBeInTheDocument();
});

it("offers Stop when first opened after an unconfirmed native cleanup", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(
    Response.json({ sessionId: null, listening: false, recordingState: "failed", nativeCleanupPending: true }),
  );
  mount();
  fireEvent.click(await screen.findByRole("option", { name: "Stop recording" }));
  await waitFor(() => expect(stop).toHaveBeenCalledOnce());
  expect(start).not.toHaveBeenCalled();
});

it("keeps a newly started capture when an older mount snapshot arrives", async () => {
  let resolveSnapshot!: (response: Response) => void;
  const snapshot = new Promise<Response>((resolve) => (resolveSnapshot = resolve));
  vi.mocked(fetch).mockReturnValueOnce(snapshot);
  mount();
  act(() => socket.listener?.({ apiVersion: "1", type: "session_started", sessionId: "new", session: {} }));
  await act(async () => {
    resolveSnapshot(Response.json({ sessionId: null, listening: false, recordingState: "idle" }));
  });
  fireEvent.click(screen.getByRole("option", { name: "Stop recording" }));
  await waitFor(() => expect(stop).toHaveBeenCalledOnce());
  expect(start).not.toHaveBeenCalled();
});
