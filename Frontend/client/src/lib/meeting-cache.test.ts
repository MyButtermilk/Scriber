import assert from "node:assert/strict";
import test from "node:test";

import { QueryClient, QueryObserver, type InfiniteData } from "@tanstack/react-query";

import { REST_API_VERSION, type MeetingActionItem, type MeetingCapabilities, type MeetingDetail, type MeetingNote, type MeetingSpeakerAssignmentsResponse, type MeetingState, type MeetingSummary, type MeetingsResponse } from "./api-types";
import {
  ACTIVE_MEETING_QUERY_PATH,
  applyMeetingActionItem,
  applyMeetingNoteEvent,
  applyMeetingSpeakerName,
  applyMeetingSummaryEvent,
  isMeetingWebSocketReconnect,
  isNewMeetingSetupEnabled,
  meetingDetailRefetchInterval,
  MEETING_HISTORY_QUERY_KEY,
  MEETING_LIST_QUERY_KEY,
  refreshAllMeetingSpeakerIdentityCaches,
  refreshMeetingCollections,
  refreshMeetingDetail,
} from "./meeting-cache";

test("global active Meeting bootstrap never downloads the full Meeting library", () => {
  assert.equal(ACTIVE_MEETING_QUERY_PATH, "/api/meetings?limit=1");
});

function meeting(id: string, state: MeetingState = "ready"): MeetingSummary {
  return {
    id,
    title: `Meeting ${id}`,
    state,
    language: "auto",
    liveProvider: "soniox",
    finalProvider: "soniox_async",
    analysisModel: "test-model",
    aecEnabled: true,
    voiceLibraryEnabled: false,
    consentConfirmed: false,
    origin: "captured",
    startedAt: "2026-07-12T10:00:00.000Z",
    endedAt: "2026-07-12T10:10:00.000Z",
    createdAt: "2026-07-12T10:00:00.000Z",
    updatedAt: "2026-07-12T10:10:00.000Z",
    errorCode: "",
    errorMessage: "",
    captureMetadata: {},
    audioRetentionDays: 30,
    smartTurnEnabled: true,
    autoAnalyze: true,
    transcriptEditVersion: 0,
  };
}

function page(items: MeetingSummary[], offset: number, total: number): MeetingsResponse {
  return {
    apiVersion: REST_API_VERSION,
    items,
    total,
    limit: 2,
    offset,
    activeMeeting: null,
  };
}

function actionItem(meetingId: string, text: string): MeetingActionItem {
  return {
    id: "shared-item-id",
    meetingId,
    text,
    owner: null,
    dueDate: null,
    status: "open",
    segmentIds: [],
    userModified: false,
    provenance: "automatic",
    createdAt: "2026-07-12T10:00:00.000Z",
    updatedAt: "2026-07-12T10:00:00.000Z",
  };
}

test("meeting websocket updates preserve paginated history and reflow a new first item", () => {
  const client = new QueryClient();
  const initial: InfiniteData<MeetingsResponse, number> = {
    pages: [
      page([meeting("a"), meeting("b")], 0, 4),
      page([meeting("c"), meeting("d")], 2, 4),
    ],
    pageParams: [0, 2],
  };
  client.setQueryData(MEETING_HISTORY_QUERY_KEY, initial);

  applyMeetingSummaryEvent(client, { ...meeting("c"), title: "Updated C" });
  let cached = client.getQueryData<InfiniteData<MeetingsResponse, number>>(MEETING_HISTORY_QUERY_KEY);
  assert.equal(cached?.pages[1].items[0].title, "Updated C");
  assert.equal(cached?.pages[0].total, 4);

  applyMeetingSummaryEvent(client, meeting("new", "recording"));
  cached = client.getQueryData<InfiniteData<MeetingsResponse, number>>(MEETING_HISTORY_QUERY_KEY);
  assert.deepEqual(cached?.pages.flatMap((value) => value.items.map((item) => item.id)), [
    "new", "a", "b", "c",
  ]);
  assert.equal(cached?.pages[0].total, 5);
  assert.equal(cached?.pages[0].activeMeeting?.id, "new");
  assert.deepEqual(cached?.pages.map((value) => value.offset), [0, 2]);
});

test("flat active-meeting cache and paginated history never share a data shape", () => {
  const client = new QueryClient();
  const flat = page([meeting("existing")], 0, 1);
  client.setQueryData(MEETING_LIST_QUERY_KEY, flat);

  applyMeetingSummaryEvent(client, meeting("new", "recording"));

  const cachedFlat = client.getQueryData<MeetingsResponse>(MEETING_LIST_QUERY_KEY);
  assert.deepEqual(cachedFlat?.items.map((item) => item.id), ["new", "existing"]);
  assert.equal(cachedFlat?.activeMeeting?.id, "new");
  assert.equal(client.getQueryData(MEETING_HISTORY_QUERY_KEY), undefined);
});

test("older meeting summaries cannot regress any cache surface", () => {
  const client = new QueryClient();
  const newer = {
    ...meeting("a", "paused"),
    updatedAt: "2026-07-12T10:02:00.000Z",
  };
  const older = {
    ...meeting("a", "recording"),
    updatedAt: "2026-07-12T10:01:00.000Z",
  };
  const list = {
    ...page([newer], 0, 1),
    activeMeeting: newer,
  };
  const history: InfiniteData<MeetingsResponse, number> = {
    pages: [list],
    pageParams: [0],
  };
  const capabilities = {
    apiVersion: REST_API_VERSION,
    platform: "windows",
    shellIpcAvailable: true,
    nativeMeetingCapture: true,
    liveMicBusy: false,
    activeMeeting: newer,
    sources: ["microphone", "system"],
    requiresPermissionConfirmation: true,
    longSession: {
      targetDurationSeconds: 18_000,
      checkpointIntervalSeconds: 30,
      requiredFreeBytes: 6 * 1024 ** 3,
      availableFreeBytes: 7 * 1024 ** 3,
      estimatedCaptureSeconds: 18_000,
      storageReady: true,
    },
  } satisfies MeetingCapabilities;
  const detail = {
    ...newer,
    apiVersion: REST_API_VERSION,
    segments: [],
    speakers: [],
    notes: [],
    actionItems: [],
    outputs: [],
    outputVersions: [],
    audioGaps: [],
    audioAssets: [],
    transcriptCheckpoints: [],
  } satisfies MeetingDetail;
  client.setQueryData(MEETING_LIST_QUERY_KEY, list);
  client.setQueryData(MEETING_HISTORY_QUERY_KEY, history);
  client.setQueryData(["/api/meetings/capabilities"], capabilities);
  client.setQueryData(["/api/meetings", "a"], detail);

  applyMeetingSummaryEvent(client, older);

  assert.equal(
    client.getQueryData<MeetingsResponse>(MEETING_LIST_QUERY_KEY)?.items[0].state,
    "paused",
  );
  assert.equal(
    client.getQueryData<InfiniteData<MeetingsResponse, number>>(MEETING_HISTORY_QUERY_KEY)
      ?.pages[0].items[0].state,
    "paused",
  );
  assert.equal(
    client.getQueryData<MeetingCapabilities>(["/api/meetings/capabilities"])?.activeMeeting?.state,
    "paused",
  );
  assert.equal(
    client.getQueryData<MeetingDetail>(["/api/meetings", "a"])?.state,
    "paused",
  );
});

test("action-item responses update only their target Meeting cache", () => {
  const client = new QueryClient();
  client.setQueryData(["/api/meetings", "a"], { actionItems: [actionItem("a", "Original A")] });
  client.setQueryData(["/api/meetings", "b"], { actionItems: [actionItem("b", "Original B")] });

  applyMeetingActionItem(client, "a", {
    ...actionItem("a", "Updated A"),
    updatedAt: "2026-07-12T10:01:00.000Z",
  });

  const cachedA = client.getQueryData<{ actionItems: MeetingActionItem[] }>(["/api/meetings", "a"]);
  const cachedB = client.getQueryData<{ actionItems: MeetingActionItem[] }>(["/api/meetings", "b"]);
  assert.equal(cachedA?.actionItems[0].text, "Updated A");
  assert.equal(cachedB?.actionItems[0].text, "Original B");

  applyMeetingActionItem(client, "a", actionItem("b", "Wrong Meeting"));
  assert.equal(
    client.getQueryData<{ actionItems: MeetingActionItem[] }>(["/api/meetings", "a"])?.actionItems[0].text,
    "Updated A",
  );
});

test("note events patch only the detail and preserve ephemeral speaker suggestions", () => {
  const client = new QueryClient();
  const note: MeetingNote = {
    id: "workspace",
    meetingId: "a",
    body: "Updated note",
    atMs: null,
    createdAt: "2026-07-15T08:00:00Z",
    updatedAt: "2026-07-15T08:01:00Z",
  };
  client.setQueryData(["/api/meetings", "a"], {
    ...meeting("a"),
    apiVersion: REST_API_VERSION,
    notes: [],
    segments: [],
    speakers: [],
    actionItems: [],
    outputs: [],
    outputVersions: [],
    audioGaps: [],
    audioAssets: [],
    transcriptCheckpoints: [],
  } satisfies MeetingDetail);
  const assignments = {
    apiVersion: REST_API_VERSION,
    calendarEvent: null,
    requiresConfirmation: true,
    llmSuggestionAvailable: false,
    llmRequested: true,
    items: [{
      speakerId: "speaker-1",
      speakerLabel: "Speaker 1",
      currentDisplayName: "Speaker 1",
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [{
        attendee: { participantId: "participant-1", name: "Alex", address: "alex@example.com" },
        source: "llm" as const,
        confidence: 0.8,
        reason: "Transcript context",
      }],
    }],
  } satisfies MeetingSpeakerAssignmentsResponse;
  client.setQueryData(["/api/meetings", "a", "speaker-assignments"], assignments);

  applyMeetingNoteEvent(client, "a", note);

  assert.equal(client.getQueryData<MeetingDetail>(["/api/meetings", "a"])?.notes[0].body, "Updated note");
  assert.deepEqual(
    client.getQueryData<MeetingSpeakerAssignmentsResponse>(["/api/meetings", "a", "speaker-assignments"]),
    assignments,
  );
});

test("manual speaker rename clears only the target's stale participant identity", () => {
  const client = new QueryClient();
  const attendee = {
    participantId: "participant-1",
    name: "Alex",
    address: "alex@example.com",
  };
  const target = {
    speakerId: "speaker-1",
    speakerLabel: "Speaker 1",
    currentDisplayName: "Alex",
    profileMatch: { profileId: "profile-1", displayName: "Alex", confidence: 0.9 },
    confirmedAttendee: attendee,
    participantLinkSource: "voice_profile",
    suggestions: [{
      attendee,
      source: "llm" as const,
      confidence: 0.8,
      reason: "Old identity context",
    }],
  };
  const untouched = {
    speakerId: "speaker-2",
    speakerLabel: "Speaker 2",
    currentDisplayName: "Taylor",
    profileMatch: null,
    confirmedAttendee: null,
    participantLinkSource: "",
    suggestions: [],
  };
  const assignments = {
    apiVersion: REST_API_VERSION,
    calendarEvent: null,
    requiresConfirmation: true,
    llmSuggestionAvailable: false,
    items: [target, untouched],
  } satisfies MeetingSpeakerAssignmentsResponse;
  client.setQueryData(["/api/meetings", "a", "speaker-assignments"], assignments);

  applyMeetingSpeakerName(client, "a", "speaker-1", "Alexander");

  const updated = client.getQueryData<MeetingSpeakerAssignmentsResponse>(
    ["/api/meetings", "a", "speaker-assignments"],
  );
  assert.equal(updated?.items[0].currentDisplayName, "Alexander");
  assert.equal(updated?.items[0].confirmedAttendee, null);
  assert.equal(updated?.items[0].participantLinkSource, "");
  assert.equal(updated?.items[0].profileMatch?.displayName, "Alexander");
  assert.deepEqual(updated?.items[0].suggestions, []);
  assert.deepEqual(updated?.items[1], untouched);
});

test("targeted refreshes never refetch Meeting child queries", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const keys = [
    MEETING_LIST_QUERY_KEY,
    MEETING_HISTORY_QUERY_KEY,
    ["/api/meetings", "a"] as const,
    ["/api/meetings", "a", "deliveries"] as const,
    ["/api/meetings", "a", "speaker-assignments"] as const,
    ["/api/meetings", "a", "email-preview"] as const,
  ];
  const calls = new Map(keys.map((key) => [JSON.stringify(key), 0]));
  const observers = keys.map((queryKey) => {
    client.setQueryData(queryKey, {});
    const observer = new QueryObserver(client, {
      queryKey,
      queryFn: async () => {
        const key = JSON.stringify(queryKey);
        calls.set(key, (calls.get(key) ?? 0) + 1);
        return {};
      },
      staleTime: Infinity,
    });
    observer.subscribe(() => {});
    return observer;
  });

  await refreshMeetingDetail(client, "a");
  assert.equal(calls.get(JSON.stringify(["/api/meetings", "a"])), 1);
  assert.equal(calls.get(JSON.stringify(["/api/meetings", "a", "deliveries"])), 0);
  assert.equal(calls.get(JSON.stringify(["/api/meetings", "a", "speaker-assignments"])), 0);
  assert.equal(calls.get(JSON.stringify(["/api/meetings", "a", "email-preview"])), 0);

  await refreshMeetingCollections(client);
  assert.equal(calls.get(JSON.stringify(MEETING_LIST_QUERY_KEY)), 1);
  assert.equal(calls.get(JSON.stringify(MEETING_HISTORY_QUERY_KEY)), 1);
  assert.equal(calls.get(JSON.stringify(["/api/meetings", "a"])), 1);
  observers.forEach((observer) => observer.destroy());
});

test("global speaker-profile merge invalidates every Meeting identity projection only", async () => {
  const client = new QueryClient();
  const invalidatedKeys = [
    ["/api/meetings", "a"],
    ["/api/meetings", "b"],
    ["/api/meetings", "a", "speaker-assignments"],
    ["/api/meetings", "b", "speaker-assignments"],
    ["/api/meetings/speaker-profiles"],
  ] as const;
  const untouchedKeys = [
    MEETING_LIST_QUERY_KEY,
    MEETING_HISTORY_QUERY_KEY,
    ["/api/meetings", "a", "email-preview"],
    ["/api/meetings", "b", "deliveries"],
    ["/api/transcripts", "history"],
  ] as const;
  [...invalidatedKeys, ...untouchedKeys].forEach((queryKey) => {
    client.setQueryData(queryKey, {});
  });

  await refreshAllMeetingSpeakerIdentityCaches(client);

  invalidatedKeys.forEach((queryKey) => {
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, true, JSON.stringify(queryKey));
  });
  untouchedKeys.forEach((queryKey) => {
    assert.equal(client.getQueryState(queryKey)?.isInvalidated, false, JSON.stringify(queryKey));
  });
});

test("reconnect and workspace policies distinguish first connect from recovery", () => {
  assert.equal(isMeetingWebSocketReconnect(false, false, true), false);
  assert.equal(isMeetingWebSocketReconnect(true, true, false), false);
  assert.equal(isMeetingWebSocketReconnect(true, false, true), true);
  assert.equal(isMeetingWebSocketReconnect(true, true, true), false);
  assert.equal(isNewMeetingSetupEnabled(""), true);
  assert.equal(isNewMeetingSetupEnabled("meeting-1"), false);
});

test("Meeting detail polling survives missed terminal websocket events", () => {
  assert.equal(meetingDetailRefetchInterval("recording", true), false);
  assert.equal(meetingDetailRefetchInterval("recording", false), 2_000);
  assert.equal(meetingDetailRefetchInterval("paused", false), 2_000);
  assert.equal(meetingDetailRefetchInterval("finalizing", true), 2_000);
  assert.equal(meetingDetailRefetchInterval("analyzing", true), 2_000);
  assert.equal(meetingDetailRefetchInterval("ready", false), false);
  assert.equal(meetingDetailRefetchInterval("capture_failed", false), false);
});
