import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
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

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["/api/settings"], {});
  client.setQueryData(["/api/transcripts", { limit: 50 }], { items: [] });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <CommandPalette open onOpenChange={vi.fn()} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

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
