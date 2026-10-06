import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import { useBackendActions } from "@/hooks/use-backend-status";
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
// The live recording stage is the only consumer of backend actions, so calls
// count its renders; toolbar calls count history-section renders.
vi.mock("@/hooks/use-backend-status", () => {
  const actions = { checkNow: () => Promise.resolve(true) };
  return { useBackendActions: vi.fn(() => actions) };
});
vi.mock("@/components/transcription-history-toolbar", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/transcription-history-toolbar")>();
  return { ...actual, TranscriptionHistoryToolbar: vi.fn(actual.TranscriptionHistoryToolbar) };
});
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

describe("Live Mic render isolation", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    vi.spyOn(window, "requestAnimationFrame").mockReturnValue(1);
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("streams interim text and elapsed time without re-rendering recording history", () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    mount();
    send({ type: "session_started", sessionId: "live", session: {} });
    send({ type: "status", sessionId: "live", listening: true, recordingState: "recording", status: "Listening" });
    vi.mocked(TranscriptionHistoryToolbar).mockClear();
    vi.mocked(useBackendActions).mockClear();

    let interim = "";
    for (let index = 0; index < 20; index += 1) {
      interim = `${interim} word${index}`.trim();
      send({ type: "transcript", sessionId: "live", text: interim, isFinal: false });
    }
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    send({ type: "transcript", sessionId: "live", text: "Final dictation", isFinal: true });

    const output = screen.getByTestId("live-mic-transcript-output");
    expect(output).toHaveTextContent("Final dictation");
    expect(screen.getByText("00:03")).toBeInTheDocument();
    // Each interim event, elapsed second and final previously re-rendered the
    // whole page, including the history toolbar and virtualizer.
    expect(vi.mocked(TranscriptionHistoryToolbar).mock.calls.length).toBe(0);
    // The stage itself still renders every interim event, the batched clock tick and the final.
    expect(vi.mocked(useBackendActions).mock.calls.length).toBeGreaterThanOrEqual(22);
  });

  it("typing a history search does not re-render the recording stage", () => {
    mount();
    vi.mocked(useBackendActions).mockClear();
    vi.mocked(TranscriptionHistoryToolbar).mockClear();

    const search = screen.getByRole("searchbox", { name: "Search recording history" });
    let query = "";
    for (const character of "meeting notes") {
      query += character;
      fireEvent.change(search, { target: { value: query } });
    }

    expect(search).toHaveValue("meeting notes");
    expect(vi.mocked(TranscriptionHistoryToolbar).mock.calls.length).toBeGreaterThanOrEqual(13);
    expect(vi.mocked(useBackendActions).mock.calls.length).toBe(0);
  });

  it("refreshes the visible history after the active session finishes", () => {
    mount();
    send({ type: "session_started", sessionId: "live", session: {} });
    send({ type: "session_finished", sessionId: "other", session: { content: "Stale" } });
    expect(refresh).not.toHaveBeenCalled();
    send({ type: "session_finished", sessionId: "live", session: { content: "Saved dictation" } });
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByTestId("live-mic-transcript-output")).toHaveTextContent("Saved dictation");
  });
});
