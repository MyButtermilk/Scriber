import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import type { MeetingDetail, MeetingSegment } from "@/lib/api-types";
import { MEETING_HISTORY_QUERY_KEY } from "@/lib/meeting-cache";
import { createReviewPlaybackLookup } from "@/lib/meeting-review-timeline";
import Meetings from "./Meetings";

const observed = vi.hoisted(() => ({
  transcriptRenders: 0,
  itemKeys: [] as Array<(index: number) => string | number>,
  scrollToIndex: vi.fn(),
  apiRequest: vi.fn(),
}));
vi.mock("@/contexts/WebSocketContext", () => ({
  useWebSocketContext: () => ({ isConnected: true }),
  useSharedWebSocket: () => ({ isConnected: true }),
}));
vi.mock("@/lib/queryClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/queryClient")>()),
  apiRequest: observed.apiRequest,
}));
vi.mock("@/components/meeting/SpeakerAttendeeAssignments", () => ({ SpeakerAttendeeAssignments: () => null }));
vi.mock("@/lib/meeting-review-timeline", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/meeting-review-timeline")>();
  return { ...actual, createReviewPlaybackLookup: vi.fn(actual.createReviewPlaybackLookup) };
});
// JSDOM has no viewport layout. Keep the real page, memo boundary, row markup,
// effects and interactions; substitute only the virtualizer's geometry.
vi.mock("@tanstack/react-virtual", () => {
  const virtualizer = {
    getTotalSize: () => 264,
    getVirtualItems: () => [0, 1, 2].map((index) => ({ index, start: index * 88 })),
    measureElement: () => undefined,
    scrollToIndex: observed.scrollToIndex,
  };
  return {
    useVirtualizer: (options: { getItemKey: (index: number) => string | number }) => {
      observed.transcriptRenders++;
      observed.itemKeys.push(options.getItemKey);
      return virtualizer;
    },
  };
});

function meeting(id = "meeting-1"): MeetingDetail {
  const createdAt = "2026-10-01T12:00:00Z";
  const segments: MeetingSegment[] = Array.from({ length: 3 }, (_, i) => ({
    id: `segment-${i}`,
    meetingId: id,
    revision: "canonical",
    source: "system",
    speakerId: null,
    speakerLabel: "Speaker A",
    startMs: i * 10_000,
    endMs: i * 10_000 + 9_000,
    durationMs: 9_000,
    text: `Review passage ${i}`,
    confidence: null,
    alignmentQuality: "exact_word",
    isFinal: true,
    sequence: i,
    createdAt,
    editVersion: 0,
    editedAt: null,
  }));
  return {
    apiVersion: "1",
    id,
    title: `Review ${id}`,
    state: "ready",
    language: "en",
    transcriptionMode: "final_only",
    liveProvider: "",
    finalProvider: "test",
    analysisModel: "",
    aecEnabled: false,
    voiceLibraryEnabled: false,
    consentConfirmed: true,
    origin: "captured",
    startedAt: createdAt,
    endedAt: createdAt,
    createdAt,
    updatedAt: createdAt,
    errorCode: "",
    errorMessage: "",
    captureMetadata: {},
    audioRetentionDays: 30,
    smartTurnEnabled: false,
    autoAnalyze: false,
    transcriptEditVersion: 0,
    processingProgress: null,
    segments,
    speakers: [],
    notes: [],
    actionItems: [],
    outputs: [],
    outputVersions: [],
    audioGaps: [],
    transcriptCheckpoints: [],
    audioAssets: [
      {
        id: "mix",
        meetingId: id,
        kind: "playback_mix",
        relativePath: "mix.wav",
        codec: "pcm",
        sampleRate: 16_000,
        channels: 1,
        durationMs: 30_000,
        byteSize: 1,
        sha256: "",
        createdAt,
      },
    ],
  };
}

let client: QueryClient;
beforeEach(() => {
  window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  observed.transcriptRenders = 0;
  observed.itemKeys = [];
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  client = new QueryClient({
    defaultOptions: { queries: { enabled: false, retry: false }, mutations: { retry: false } },
  });
});
afterEach(() => {
  client.clear();
  vi.unstubAllGlobals();
});

function renderMeeting(detail = meeting()) {
  client.setQueryData(["/api/meetings", detail.id], detail);
  client.setQueryData(["/api/meetings", detail.id, "deliveries"], { items: [] });
  const view = render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Meetings params={{ id: detail.id }} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
  return { ...view, audio: view.container.querySelector("audio")! };
}

it("timeupdates within one segment do not rerender transcript rows; seeks still update and follow", () => {
  const { audio } = renderMeeting();
  expect(audio).not.toBeNull();
  const initialRenders = observed.transcriptRenders;
  const itemKey = observed.itemKeys.at(-1);
  observed.scrollToIndex.mockClear();
  for (let i = 1; i <= 20; i++) {
    audio.currentTime = i / 10;
    fireEvent.timeUpdate(audio);
  }
  expect(observed.transcriptRenders - initialRenders).toBe(0);
  expect(createReviewPlaybackLookup).toHaveBeenCalledTimes(1);
  expect(observed.scrollToIndex).not.toHaveBeenCalled();
  expect(screen.getByTestId("meeting-transcript-segment-segment-0")).toHaveAttribute("data-playback-active", "true");

  audio.currentTime = 12;
  fireEvent.timeUpdate(audio);
  expect(observed.transcriptRenders - initialRenders).toBe(1);
  expect(observed.itemKeys.at(-1)).toBe(itemKey);
  expect(screen.getByTestId("meeting-transcript-segment-segment-1")).toHaveAttribute("data-playback-active", "true");
  expect(observed.scrollToIndex).toHaveBeenCalledWith(1, { align: "center" });

  fireEvent.click(screen.getByRole("checkbox", { name: "Follow playback" }));
  observed.scrollToIndex.mockClear();
  audio.currentTime = 1;
  fireEvent.timeUpdate(audio);
  expect(screen.getByTestId("meeting-transcript-segment-segment-0")).toHaveAttribute("data-playback-active", "true");
  expect(observed.scrollToIndex).not.toHaveBeenCalled();
});

it("does not construct a playback index when no saved audio is available", () => {
  renderMeeting({ ...meeting(), audioAssets: [] });
  expect(createReviewPlaybackLookup).not.toHaveBeenCalled();
  expect(screen.getByText("Review passage 0")).toBeInTheDocument();
});

it("stable edit callbacks save and undo using the latest transcript version after playback", async () => {
  const detail = meeting();
  const { audio } = renderMeeting(detail);
  audio.currentTime = 1;
  fireEvent.timeUpdate(audio);
  act(() => {
    client.setQueryData<MeetingDetail>(["/api/meetings", detail.id], { ...detail, transcriptEditVersion: 7 });
  });
  await waitFor(() =>
    expect(client.getQueryData<MeetingDetail>(["/api/meetings", detail.id])?.transcriptEditVersion).toBe(7),
  );
  observed.apiRequest.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        meetingId: detail.id,
        segment: { ...detail.segments[0], text: "Corrected passage", editVersion: 1 },
        transcriptEditVersion: 8,
        outputsStale: false,
      }),
    ),
  );
  fireEvent.click(screen.getByTestId("meeting-segment-edit-segment-0"));
  fireEvent.change(screen.getByTestId("meeting-segment-edit-input-segment-0"), {
    target: { value: "Corrected passage" },
  });
  fireEvent.click(screen.getByTestId("meeting-segment-edit-save-segment-0"));
  await waitFor(() =>
    expect(observed.apiRequest).toHaveBeenCalledWith("PATCH", "/api/meetings/meeting-1/segments/segment-0", {
      expectedEditVersion: 7,
      text: "Corrected passage",
    }),
  );
  expect(await screen.findByText("Corrected passage")).toBeInTheDocument();
  observed.apiRequest.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        meetingId: detail.id,
        segment: detail.segments[0],
        transcriptEditVersion: 9,
        outputsStale: false,
      }),
    ),
  );
  fireEvent.click(screen.getByTestId("meeting-segment-undo-segment-0"));
  await waitFor(() =>
    expect(observed.apiRequest).toHaveBeenCalledWith("POST", "/api/meetings/meeting-1/segments/segment-0/undo", {
      expectedEditVersion: 8,
    }),
  );
  expect(await screen.findByText("Review passage 0")).toBeInTheDocument();
});

it("Meeting intent prefetch deduplicates pointer/focus and reuses fresh detail data", async () => {
  const other = meeting("meeting-2");
  client.setQueryData(MEETING_HISTORY_QUERY_KEY, {
    pages: [{ items: [other], total: 1, offset: 0, activeMeeting: null }],
    pageParams: [0],
  });
  let finish!: (response: Response) => void;
  const fetch = vi.fn(
    (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  renderMeeting();
  const row = screen.getByText(other.title).closest("button")!;
  fireEvent.pointerEnter(row);
  fireEvent.focus(row);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(String(fetch.mock.calls[0]?.[0])).toContain("/api/meetings/meeting-2");
  await act(async () => {
    finish(new Response(JSON.stringify(other)));
  });
  fireEvent.pointerEnter(row);
  fireEvent.focus(row);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(client.getQueryData(["/api/meetings", other.id])).toEqual(other);
});
