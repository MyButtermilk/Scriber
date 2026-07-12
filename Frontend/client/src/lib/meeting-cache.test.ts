import assert from "node:assert/strict";
import test from "node:test";

import { QueryClient, type InfiniteData } from "@tanstack/react-query";

import { REST_API_VERSION, type MeetingActionItem, type MeetingState, type MeetingSummary, type MeetingsResponse } from "./api-types";
import {
  applyMeetingActionItem,
  applyMeetingSummaryEvent,
  MEETING_HISTORY_QUERY_KEY,
  MEETING_LIST_QUERY_KEY,
} from "./meeting-cache";

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
