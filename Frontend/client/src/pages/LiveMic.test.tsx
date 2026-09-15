import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import LiveMic from "./LiveMic";

const { start, stop, toast, refresh, socket } = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  toast: vi.fn(),
  refresh: vi.fn(),
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
  captureBenchmarkButtonActivationMarker: async () => null,
  presentLiveMicControlFailure: vi.fn(),
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/hooks/use-backend-status", () => ({ useBackendStatus: () => ({ checkNow: vi.fn() }) }));
vi.mock("@/hooks/use-transcript-auto-refresh", () => ({ useTranscriptAutoRefresh: () => ({ refreshNow: refresh }) }));
vi.mock("@/hooks/use-transcript-history-query", () => ({
  transcriptHistoryQueryKey: () => ["/api/transcripts", "mic"],
  useTranscriptHistoryQuery: () => ({ items: [], total: 0 }),
}));
vi.mock("@/lib/visualizer-settings", () => ({
  DEFAULT_VISUALIZER_BAR_COUNT: 32,
  loadVisualizerBarCount: async () => 32,
  normalizeVisualizerBarCount: (value: number) => value,
}));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));

type WsPayload<T> = T extends unknown ? Omit<T, "apiVersion"> : never;

function send(message: WsPayload<ScriberWebSocketMessage>) {
  act(() => socket.listener?.({ apiVersion: "1", ...message }));
}

function state(overrides: Partial<Extract<ScriberWebSocketMessage, { type: "state" }>> = {}) {
  send({
    type: "state",
    sessionId: "old-session",
    listening: false,
    voiceEnrollmentActive: false,
    status: "Transcribing...",
    backgroundProcessing: true,
    recordingState: "finalizing",
    transcribing: true,
    micStartPending: false,
    ...overrides,
  });
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <LiveMic />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("Live Mic consecutive recordings", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    start.mockResolvedValue(new Response("{}"));
    stop.mockResolvedValue(new Response("{}"));
    vi.spyOn(window, "requestAnimationFrame").mockReturnValue(1);
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  });

  it("starts another recording while the previous transcript is being processed", async () => {
    mount();
    state();
    const button = screen.getByRole("button", { name: "Start next recording" });
    expect(button).toBeEnabled();
    expect(screen.getByText("You can start your next recording now")).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(stop).not.toHaveBeenCalled();
  });

  it("announces a queued start and allows it to be canceled", async () => {
    mount();
    state({ micStartPending: true });
    expect(screen.getByRole("status")).toHaveTextContent("Next recording queued.");
    const button = screen.getByRole("button", { name: "Cancel next recording" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    expect(start).not.toHaveBeenCalled();
    state({ micStartPending: false });
    await screen.findByRole("button", { name: "Start next recording" });
  });

  it("keeps a new capture and its text when the previous session emits late events", async () => {
    mount();
    state({ micStartPending: true });
    send({ type: "session_started", sessionId: "new-session", session: {} });
    send({
      type: "status",
      sessionId: "new-session",
      listening: true,
      recordingState: "recording",
      status: "Listening",
    });
    send({ type: "transcript", sessionId: "new-session", text: "New dictation", isFinal: true });
    send({ type: "transcript", sessionId: "old-session", text: "Old dictation", isFinal: true });
    send({ type: "status", sessionId: "old-session", listening: false, recordingState: "idle", status: "Stopped" });
    send({ type: "session_finished", sessionId: "old-session", session: { content: "Old dictation" } });
    expect(screen.getByRole("button", { name: "Stop recording" })).toBeEnabled();
    expect(screen.getByTestId("live-mic-transcript-output")).toHaveTextContent("New dictation");
    expect(screen.getByTestId("live-mic-transcript-output")).not.toHaveTextContent("Old dictation");
    expect(screen.getByRole("status")).toHaveTextContent("Recording started.");
    fireEvent.click(screen.getByRole("button", { name: "Stop recording" }));
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
  });
});
