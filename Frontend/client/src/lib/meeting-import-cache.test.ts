import assert from "node:assert/strict";
import test from "node:test";

import { QueryClient, QueryObserver } from "@tanstack/react-query";

import { REST_API_VERSION, type MeetingImportJob, type MeetingImportsResponse } from "./api-types";
import {
  applyMeetingImportProgressEvent,
  MEETING_IMPORTS_QUERY_KEY,
  mergeMeetingImportProgress,
  upsertMeetingImportJob,
} from "./meeting-import-cache";

function job(overrides: Partial<MeetingImportJob> = {}): MeetingImportJob {
  return {
    apiVersion: REST_API_VERSION,
    id: "import-1",
    state: "receiving",
    sourceFilename: "meeting.webm",
    title: "Meeting",
    language: "auto",
    profileId: "balanced",
    expectedBytes: 100,
    receivedBytes: 20,
    progress: 0.2,
    status: "Uploading recording",
    meetingId: null,
    cancelRequested: false,
    canCancel: true,
    canRetry: false,
    errorCode: null,
    errorMessage: null,
    createdAt: "2026-07-15T08:00:00Z",
    updatedAt: "2026-07-15T08:00:01Z",
    finishedAt: null,
    ...overrides,
  };
}

test("import progress is monotone within a phase and rejects a late older phase", () => {
  const atSixty = mergeMeetingImportProgress(
    { importId: "import-1", phase: "receiving", stage: "Uploading", percentage: 20 },
    { importId: "import-1", phase: "receiving", stage: "Uploading", percentage: 60 },
  );
  const lateForty = mergeMeetingImportProgress(
    atSixty,
    { importId: "import-1", phase: "receiving", stage: "Uploading", percentage: 40 },
  );
  const safelyStored = mergeMeetingImportProgress(
    lateForty,
    { importId: "import-1", phase: "received", stage: "Stored", percentage: 86 },
  );
  const lateUpload = mergeMeetingImportProgress(
    safelyStored,
    { importId: "import-1", phase: "receiving", stage: "Uploading", percentage: 84 },
  );

  assert.equal(lateForty.percentage, 60);
  assert.equal(safelyStored.percentage, 86);
  assert.deepEqual(lateUpload, safelyStored);
});

test("terminal import outcomes are sticky while cancel-requested may still complete", () => {
  const completed = {
    importId: "import-1",
    phase: "completed",
    stage: "Meeting created",
    percentage: 100,
  };
  assert.deepEqual(
    mergeMeetingImportProgress(completed, {
      importId: "import-1",
      phase: "cancel_requested",
      stage: "Cancel requested",
      percentage: 0,
    }),
    completed,
  );
  assert.deepEqual(
    mergeMeetingImportProgress(completed, {
      importId: "import-1",
      phase: "canceled",
      stage: "Late cancellation",
      percentage: 100,
    }),
    completed,
  );
  assert.deepEqual(
    mergeMeetingImportProgress(completed, {
      importId: "import-1",
      phase: "future_unknown_phase",
      stage: "Late unknown event",
      percentage: 0,
    }),
    completed,
  );

  const canceled = {
    importId: "import-1",
    phase: "canceled",
    stage: "Import canceled",
    percentage: 86,
  };
  assert.deepEqual(
    mergeMeetingImportProgress(canceled, {
      importId: "import-1",
      phase: "failed",
      stage: "Late failure",
      percentage: 100,
    }),
    canceled,
  );

  const cancelRequested = {
    importId: "import-1",
    phase: "cancel_requested",
    stage: "Cancel requested",
    percentage: 86,
  };
  assert.equal(
    mergeMeetingImportProgress(cancelRequested, completed).phase,
    "completed",
  );
});

test("websocket progress patches the inbox without triggering query fetches", () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const initial: MeetingImportsResponse = {
    apiVersion: REST_API_VERSION,
    items: [job()],
    total: 1,
    limit: 24,
  };
  client.setQueryData(MEETING_IMPORTS_QUERY_KEY, initial);
  let fetches = 0;
  const observer = new QueryObserver(client, {
    queryKey: MEETING_IMPORTS_QUERY_KEY,
    queryFn: async () => {
      fetches += 1;
      return initial;
    },
    staleTime: Infinity,
  });
  observer.subscribe(() => {});

  for (let index = 1; index <= 100; index += 1) {
    applyMeetingImportProgressEvent(client, {
      importId: "import-1",
      phase: "receiving",
      progress: index / 100,
      status: "Uploading recording",
      receivedBytes: index,
      expectedBytes: 100,
    });
  }

  const cached = client.getQueryData<MeetingImportsResponse>(MEETING_IMPORTS_QUERY_KEY);
  assert.equal(fetches, 0);
  assert.equal(cached?.items[0].progress, 1);
  assert.equal(cached?.items[0].receivedBytes, 100);
  observer.destroy();
});

test("created and returned import jobs upsert without an inbox refetch", () => {
  const client = new QueryClient();
  upsertMeetingImportJob(client, job({ state: "created", progress: 0 }));
  upsertMeetingImportJob(client, job({ state: "received", progress: 0.86, receivedBytes: 100 }));

  const cached = client.getQueryData<MeetingImportsResponse>(MEETING_IMPORTS_QUERY_KEY);
  assert.equal(cached?.items.length, 1);
  assert.equal(cached?.total, 1);
  assert.equal(cached?.items[0].state, "received");
});

test("late cache upserts and websocket events cannot rewrite a terminal import", () => {
  const client = new QueryClient();
  const completed = job({
    state: "completed",
    progress: 1,
    status: "Meeting created",
    meetingId: "meeting-1",
    receivedBytes: 100,
  });
  upsertMeetingImportJob(client, completed);
  upsertMeetingImportJob(client, job({
    state: "cancel_requested",
    progress: 0.86,
    status: "Cancel requested",
    receivedBytes: 100,
  }));
  applyMeetingImportProgressEvent(client, {
    importId: completed.id,
    phase: "failed",
    progress: 1,
    status: "Late failure",
    receivedBytes: 100,
  });

  const cached = client.getQueryData<MeetingImportsResponse>(MEETING_IMPORTS_QUERY_KEY);
  assert.equal(cached?.items[0].state, "completed");
  assert.equal(cached?.items[0].status, "Meeting created");
  assert.equal(cached?.items[0].meetingId, "meeting-1");
});
