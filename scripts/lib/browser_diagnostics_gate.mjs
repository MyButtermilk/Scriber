const EXPECTED_FETCH_ABORT_LIMIT = 6;
const EXPECTED_MEETING_DISCARD_NOT_FOUND_LIMIT = 2;
const MEETING_ID_PATTERN =
  "(?:[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})";
const EXPECTED_MEETING_DISCARD_PATH = new RegExp(
  `^/api/meetings/${MEETING_ID_PATTERN}(?:/speaker-assignments)?$`,
  "i",
);

const EXPECTED_FETCH_ABORT_PHASES = new Set([
  "start-meeting",
  "wait-recording",
  "pause-meeting",
  "wait-paused",
  "resume-meeting",
  "wait-resumed-recording",
  "stop-meeting",
  "wait-finalization",
  "rename-meeting-workspace",
  "edit-meeting-workspace-segment",
  "undo-meeting-workspace-segment",
  "save-meeting-workspace-note",
  "reprocess-meeting-transcript",
  "validate-meeting-artifacts",
  "discard-meeting-through-ui",
  "navigate-live-mic",
  "start-live-mic-through-ui",
  "stop-live-mic-through-ui",
  "validate-live-mic-transcript",
]);

export function expectedFetchAbortAllowed(request, phase, observedCount) {
  return (
    request?.resourceType === "fetch" &&
    request?.method === "GET" &&
    request?.errorText === "net::ERR_ABORTED" &&
    EXPECTED_FETCH_ABORT_PHASES.has(String(phase ?? "")) &&
    Number.isInteger(observedCount) &&
    observedCount >= 0 &&
    observedCount < EXPECTED_FETCH_ABORT_LIMIT
  );
}

export function expectedMeetingDiscardNotFoundAllowed(response, phase, observedPaths) {
  const path = String(response?.path ?? "");
  return (
    response?.status === 404 &&
    response?.method === "GET" &&
    phase === "discard-meeting-through-ui" &&
    observedPaths instanceof Set &&
    observedPaths.size < EXPECTED_MEETING_DISCARD_NOT_FOUND_LIMIT &&
    EXPECTED_MEETING_DISCARD_PATH.test(path) &&
    !observedPaths.has(path)
  );
}

export function expectedMeetingDiscardConsoleAllowed(message, phase, observedCount) {
  return (
    message?.type === "error" &&
    phase === "discard-meeting-through-ui" &&
    /^Failed to load resource: the server responded with a status of 404 \(Not Found\)$/.test(
      String(message?.text ?? ""),
    ) &&
    Number.isInteger(observedCount) &&
    observedCount >= 0 &&
    observedCount < EXPECTED_MEETING_DISCARD_NOT_FOUND_LIMIT
  );
}
