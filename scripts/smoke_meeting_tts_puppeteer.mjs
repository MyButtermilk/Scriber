import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import {
  expectedFetchAbortAllowed,
  expectedMeetingDiscardConsoleAllowed,
  expectedMeetingDiscardNotFoundAllowed,
} from "./lib/browser_diagnostics_gate.mjs";
import { meetingDeviceTestPassed } from "./lib/meeting_device_test_gate.mjs";

const require = createRequire(import.meta.url);
let activePhase = "bootstrap";
const diagnostics = {
  consoleErrorCount: 0,
  expectedFetchAbortCount: 0,
  expectedMeetingDiscardConsoleCount: 0,
  expectedMeetingDiscardNotFoundCount: 0,
  pageErrorCount: 0,
  requestFailureCount: 0,
  requestFailureKinds: {},
};
const observedStates = [];
const meetingStates = new Set([
  "starting",
  "recording",
  "paused",
  "stopping",
  "finalizing",
  "analyzing",
  "ready",
  "capture_failed",
  "finalization_failed",
  "analysis_failed",
  "interrupted",
  "discarded",
]);
const meetingDebug = {
  providerPhase: "not_started",
  meetingIdHash: null,
  captureIdHash: null,
  meetingState: null,
  finalProvider: null,
  segmentCount: null,
  errorCode: "",
};

function isExpectedFetchAbort(request, phase, observedCount) {
  return expectedFetchAbortAllowed(
    {
      resourceType: request.resourceType(),
      method: request.method(),
      errorText: request.failure()?.errorText,
    },
    phase,
    observedCount,
  );
}

function requestFailureKind(request, phase) {
  const rawPhase = String(phase ?? "unknown").toLowerCase();
  const phaseName = /^[a-z0-9-]{1,64}$/.test(rawPhase) ? rawPhase : "other";
  const rawResourceType = String(request.resourceType() ?? "other").toLowerCase();
  const resourceType = /^[a-z-]{1,24}$/.test(rawResourceType)
    ? rawResourceType
    : "other";
  const rawReason = String(request.failure()?.errorText ?? "other");
  const reason = /^net::[A-Z0-9_]{1,64}$/.test(rawReason) ? rawReason : "other";
  return `${phaseName}:${resourceType}:${reason}`;
}

function parseArguments(argv) {
  const options = {
    browserUrl: "",
    puppeteerRoot: "",
    output: "",
    title: "Puppeteer Piper TTS meeting smoke",
    expectedTokens: [],
    fixtureDurationMs: 0,
    prePauseMs: 3_000,
    pausedMs: 1_200,
    finalizationTimeoutMs: 420_000,
    navigationTimeoutMs: 60_000,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--expected-token") {
      options.expectedTokens.push(argv[++index] ?? "");
      continue;
    }
    if (!key.startsWith("--")) {
      throw new Error("unexpected positional argument");
    }
    const value = argv[++index];
    if (value == null) {
      throw new Error(`missing value for ${key}`);
    }
    switch (key) {
      case "--browser-url":
        options.browserUrl = value;
        break;
      case "--puppeteer-root":
        options.puppeteerRoot = value;
        break;
      case "--output":
        options.output = value;
        break;
      case "--title":
        options.title = value;
        break;
      case "--fixture-duration-ms":
        options.fixtureDurationMs = Number.parseInt(value, 10);
        break;
      case "--pre-pause-ms":
        options.prePauseMs = Number.parseInt(value, 10);
        break;
      case "--paused-ms":
        options.pausedMs = Number.parseInt(value, 10);
        break;
      case "--finalization-timeout-ms":
        options.finalizationTimeoutMs = Number.parseInt(value, 10);
        break;
      case "--navigation-timeout-ms":
        options.navigationTimeoutMs = Number.parseInt(value, 10);
        break;
      default:
        throw new Error(`unknown argument ${key}`);
    }
  }
  if (!options.browserUrl || !options.puppeteerRoot || !options.output) {
    throw new Error(
      "--browser-url, --puppeteer-root, and --output are required",
    );
  }
  for (const [name, value] of [
    ["fixture duration", options.fixtureDurationMs],
    ["pre-pause duration", options.prePauseMs],
    ["paused duration", options.pausedMs],
    ["finalization timeout", options.finalizationTimeoutMs],
    ["navigation timeout", options.navigationTimeoutMs],
  ]) {
    if (!Number.isFinite(value) || value < 0) {
      throw new Error(`${name} must be a non-negative integer`);
    }
  }
  options.expectedTokens = options.expectedTokens
    .map((token) => normalizeText(token))
    .filter(Boolean);
  return options;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function normalizeText(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("de-DE")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function hashHint(value) {
  return crypto
    .createHash("sha256")
    .update(String(value ?? ""), "utf8")
    .digest("hex")
    .slice(0, 16);
}

function safeToken(value) {
  const token = String(value ?? "").trim();
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(token) ? token : null;
}

function providerPhaseForState(state, previousPhase) {
  if (
    ["starting", "recording", "paused", "stopping", "capture_failed"].includes(
      state,
    )
  ) {
    return "capture";
  }
  if (["finalizing", "finalization_failed"].includes(state)) {
    return "final_transcription";
  }
  if (["analyzing", "analysis_failed"].includes(state)) return "analysis";
  if (state === "ready") return "complete";
  if (
    state === "interrupted" &&
    ["capture", "final_transcription", "analysis"].includes(previousPhase)
  ) {
    return previousPhase;
  }
  return "unknown";
}

function updateMeetingDebug(payload, meetingIdHint = "") {
  const nestedMeeting =
    payload?.meeting && typeof payload.meeting === "object"
      ? payload.meeting
      : null;
  const meeting =
    nestedMeeting ??
    (payload && typeof payload === "object" ? payload : null);
  if (!meeting) return;

  const meetingId =
    String(meeting.id ?? "").trim() || String(meetingIdHint ?? "").trim();
  if (meetingId) meetingDebug.meetingIdHash = hashHint(meetingId);

  const stateCandidate = String(meeting.state ?? "").trim();
  if (meetingStates.has(stateCandidate)) {
    meetingDebug.meetingState = stateCandidate;
    meetingDebug.providerPhase = providerPhaseForState(
      stateCandidate,
      meetingDebug.providerPhase,
    );
  }

  if (Object.prototype.hasOwnProperty.call(meeting, "finalProvider")) {
    meetingDebug.finalProvider = safeToken(meeting.finalProvider);
  }
  const captureMetadata =
    meeting.captureMetadata && typeof meeting.captureMetadata === "object"
      ? meeting.captureMetadata
      : null;
  const captureId = String(captureMetadata?.captureId ?? "").trim();
  if (captureId) meetingDebug.captureIdHash = hashHint(captureId);

  if (Array.isArray(meeting.segments)) {
    meetingDebug.segmentCount = meeting.segments.length;
  }
  if (Object.prototype.hasOwnProperty.call(meeting, "errorCode")) {
    const rawErrorCode = String(meeting.errorCode ?? "").trim();
    meetingDebug.errorCode = rawErrorCode
      ? (safeToken(rawErrorCode) ?? "meeting_error_code_redacted")
      : "";
  }
}

function harnessErrorCodeForPhase(phase) {
  const codes = {
    bootstrap: "harness_configuration_invalid",
    "connect-webview2": "webview_connection_failed",
    "select-main-webview": "webview_target_missing",
    "navigate-meetings": "meeting_page_navigation_failed",
    "resolve-backend-access": "backend_access_unavailable",
    "verify-managed-backend": "backend_not_ready",
    "wait-frontend-websocket": "webview_connection_failed",
    "prepare-meeting-form": "meeting_start_control_unavailable",
    "wait-meeting-start-enabled": "meeting_start_control_unavailable",
    "start-meeting": "meeting_start_failed",
    "wait-recording": "meeting_recording_state_failed",
    "pause-meeting": "meeting_pause_control_failed",
    "wait-paused": "meeting_pause_state_failed",
    "resume-meeting": "meeting_resume_control_failed",
    "wait-resumed-recording": "meeting_resume_state_failed",
    "stop-meeting": "meeting_stop_control_failed",
    "wait-finalization": "meeting_finalization_failed",
    "validate-transcript-content": "meeting_transcript_empty",
    "validate-transcript-marker": "meeting_marker_missing",
    "validate-audio-gap": "meeting_audio_gap_missing",
    "validate-meeting-readiness": "meeting_readiness_failed",
    "rename-meeting-workspace": "meeting_workspace_failed",
    "edit-meeting-workspace-segment": "meeting_workspace_failed",
    "undo-meeting-workspace-segment": "meeting_workspace_failed",
    "save-meeting-workspace-note": "meeting_workspace_failed",
    "search-meeting-workspace-segment": "meeting_workspace_failed",
    "reprocess-meeting-transcript": "meeting_reprocess_failed",
    "validate-meeting-artifacts": "meeting_artifact_failed",
    "validate-meeting-catalog": "meeting_catalog_failed",
    "discard-meeting-through-ui": "meeting_catalog_failed",
    "navigate-live-mic": "live_mic_navigation_failed",
    "start-live-mic-through-ui": "live_mic_start_failed",
    "stop-live-mic-through-ui": "live_mic_stop_failed",
    "validate-live-mic-transcript": "live_mic_transcript_failed",
    "validate-browser-diagnostics": "webview_page_error",
  };
  return codes[phase] ?? "harness_unexpected_error";
}

function meetingDebugSnapshot(failureCode = "") {
  const fallback = safeToken(failureCode) ?? "harness_unexpected_error";
  return {
    providerPhase: meetingDebug.providerPhase,
    meetingIdHash: meetingDebug.meetingIdHash,
    captureIdHash: meetingDebug.captureIdHash,
    meetingState: meetingDebug.meetingState,
    finalProvider: meetingDebug.finalProvider,
    segmentCount: meetingDebug.segmentCount,
    errorCode: meetingDebug.errorCode || (failureCode ? fallback : ""),
  };
}

function sanitizeMessage(error) {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/[A-Za-z]:\\[^\r\n"']+/g, "<path>")
    .replace(/https?:\/\/[^\s"']+/gi, "<url>")
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi, "<id>")
    .replace(/[0-9a-f]{32,}/gi, "<opaque>")
    .slice(0, 320);
}

async function writeResult(outputPath, result) {
  const resolved = path.resolve(outputPath);
  await fs.mkdir(path.dirname(resolved), { recursive: true });
  const temporary = `${resolved}.${process.pid}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  await fs.rename(temporary, resolved);
}

async function selectMainPage(browser, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const pages = await browser.pages();
    const candidates = pages.filter((page) => {
      try {
        const url = new URL(page.url());
        return (
          ["http:", "https:"].includes(url.protocol) &&
          !url.pathname.toLocaleLowerCase("en-US").includes("overlay") &&
          !url.pathname.toLocaleLowerCase("en-US").includes("tray-panel") &&
          !url.searchParams.has("overlay") &&
          !url.searchParams.has("tray")
        );
      } catch {
        return false;
      }
    });
    const meetingPage = candidates.find((page) => {
      try {
        return new URL(page.url()).pathname.startsWith("/meetings");
      } catch {
        return false;
      }
    });
    if (meetingPage) return meetingPage;
    if (candidates.length > 0) return candidates[0];
    await delay(250);
  }
  throw new Error(
    "WebView2 main page target did not appear before the deadline",
  );
}

async function fetchJson(access, pathname, init = {}) {
  const url = new URL(pathname, access.baseUrl);
  const headers = new Headers(init.headers ?? {});
  headers.set("X-Scriber-Token", access.sessionToken);
  const response = await fetch(url, {
    ...init,
    headers,
    signal: AbortSignal.timeout(15_000),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(`backend request failed with HTTP ${response.status}`);
  }
  return payload;
}

async function waitForManagedBackend(access, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastFailure = "not ready";
  while (Date.now() < deadline) {
    try {
      const health = await fetchJson(access, "/api/health");
      const runtime = await fetchJson(access, "/api/runtime");
      if (
        health?.ready === true &&
        runtime?.runtimeMode === "tauri-supervised"
      ) {
        return { health, runtime };
      }
      lastFailure = "health contract was not ready";
    } catch (error) {
      lastFailure = error?.constructor?.name ?? "Error";
    }
    await delay(500);
  }
  throw new Error(`managed backend did not become ready (${lastFailure})`);
}

async function waitForMeetingState(
  access,
  meetingId,
  acceptedStates,
  timeoutMs,
  observedStates,
) {
  const accepted = new Set(acceptedStates);
  const deadline = Date.now() + timeoutMs;
  let lastState = "unknown";
  while (Date.now() < deadline) {
    const detail = await fetchJson(
      access,
      `/api/meetings/${encodeURIComponent(meetingId)}`,
    );
    updateMeetingDebug(detail, meetingId);
    lastState = String(detail?.state ?? "unknown");
    if (observedStates.at(-1) !== lastState) observedStates.push(lastState);
    if (accepted.has(lastState)) return detail;
    if (
      [
        "capture_failed",
        "finalization_failed",
        "analysis_failed",
        "interrupted",
        "discarded",
      ].includes(lastState)
    ) {
      throw new Error(`meeting entered terminal failure state ${lastState}`);
    }
    await delay(750);
  }
  throw new Error(`meeting state deadline expired after state ${lastState}`);
}

async function waitForLiveMicState(page, sessionId, acceptedStates, timeoutMs) {
  const accepted = new Set(acceptedStates);
  const deadline = Date.now() + timeoutMs;
  let lastState = "unknown";
  while (Date.now() < deadline) {
    const state = await browserJson(page, "GET", "/api/state");
    lastState = String(state?.recordingState ?? "unknown");
    if (
      String(state?.sessionId ?? "") === sessionId &&
      accepted.has(lastState)
    ) {
      return state;
    }
    await delay(250);
  }
  throw new Error(`Live Mic state deadline expired after state ${lastState}`);
}

async function waitForLiveMicTranscript(page, sessionId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastStatus = "missing";
  while (Date.now() < deadline) {
    const history = await browserJson(
      page,
      "GET",
      "/api/transcripts?type=mic&limit=10",
    );
    const item = Array.isArray(history?.items)
      ? history.items.find((candidate) => String(candidate?.id ?? "") === sessionId)
      : null;
    if (item) {
      lastStatus = String(item.status ?? "unknown");
      if (lastStatus === "completed") {
        const detail = await browserJson(
          page,
          "GET",
          `/api/transcripts/${encodeURIComponent(sessionId)}`,
        );
        if (String(detail?.content ?? "").trim()) return detail;
      }
      if (["failed", "stopped"].includes(lastStatus)) {
        throw new Error(`Live Mic transcript entered terminal state ${lastStatus}`);
      }
    }
    await delay(500);
  }
  throw new Error(`Live Mic transcript deadline expired after state ${lastStatus}`);
}

async function clickControl(page, meetingId, action, timeoutMs) {
  const selector = `[data-testid="active-meeting-${action}"]`;
  await page.waitForSelector(selector, { visible: true, timeout: timeoutMs });
  const responsePromise = page.waitForResponse(
    (response) => {
      const request = response.request();
      try {
        const url = new URL(response.url());
        return (
          request.method() === "POST" &&
          url.pathname === `/api/meetings/${meetingId}/${action}`
        );
      } catch {
        return false;
      }
    },
    { timeout: timeoutMs },
  );
  await page.$eval(selector, (button) => {
    if (!(button instanceof HTMLButtonElement) || button.disabled) {
      throw new Error("Meeting control is not actionable");
    }
    button.click();
  });
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`${action} control failed with HTTP ${response.status()}`);
  }
}

async function browserJson(page, method, pathname, body = null) {
  const result = await page.evaluate(
    async ({ requestBody, requestMethod, requestPath }) => {
      const baseUrl = window.__SCRIBER_BACKEND_URL__;
      const token = window.__SCRIBER_SESSION_TOKEN__;
      if (!baseUrl || !token) {
        throw new Error("authenticated backend access is unavailable");
      }
      const response = await fetch(new URL(requestPath, baseUrl), {
        method: requestMethod,
        headers: {
          "Content-Type": "application/json",
          "X-Scriber-Token": token,
        },
        body: requestBody == null ? undefined : JSON.stringify(requestBody),
        cache: "no-store",
        credentials: "include",
      });
      const payload = await response.json().catch(() => null);
      return { ok: response.ok, payload, status: response.status };
    },
    { requestBody: body, requestMethod: method, requestPath: pathname },
  );
  if (!result.ok) {
    throw new Error(`browser backend request failed with HTTP ${result.status}`);
  }
  return result.payload;
}

async function browserArtifact(page, pathname, headers = {}) {
  const result = await page.evaluate(
    async ({ requestHeaders, requestPath }) => {
      const baseUrl = window.__SCRIBER_BACKEND_URL__;
      const token = window.__SCRIBER_SESSION_TOKEN__;
      if (!baseUrl || !token) {
        throw new Error("authenticated backend access is unavailable");
      }
      const response = await fetch(new URL(requestPath, baseUrl), {
        method: "GET",
        headers: {
          ...requestHeaders,
          "X-Scriber-Token": token,
        },
        cache: "no-store",
        credentials: "include",
      });
      const body = await response.arrayBuffer();
      return {
        byteLength: body.byteLength,
        cacheControl: response.headers.get("cache-control") ?? "",
        contentType: response.headers.get("content-type") ?? "",
        ok: response.ok,
        status: response.status,
      };
    },
    { requestHeaders: headers, requestPath: pathname },
  );
  if (!result.ok) {
    throw new Error(`browser artifact request failed with HTTP ${result.status}`);
  }
  return result;
}

async function run(options) {
  activePhase = "connect-webview2";
  const puppeteerModule = path.join(
    path.resolve(options.puppeteerRoot),
    "node_modules",
    "puppeteer-core",
  );
  const puppeteer = require(puppeteerModule);
  let browser;
  const startedAt = Date.now();
  let firstPageErrorHint = "";
  let browserDiagnosticsArmed = false;
  const expectedFetchAbortCountsByPhase = new Map();
  const expectedMeetingDiscardNotFoundPaths = new Set();
  try {
    browser = await puppeteer.connect({
      browserURL: options.browserUrl,
      defaultViewport: null,
      protocolTimeout: Math.max(options.navigationTimeoutMs, 30_000),
    });
    activePhase = "select-main-webview";
    const page = await selectMainPage(browser, options.navigationTimeoutMs);
    page.setDefaultTimeout(options.navigationTimeoutMs);
    page.setDefaultNavigationTimeout(options.navigationTimeoutMs);
    page.on("console", (message) => {
      if (browserDiagnosticsArmed && message.type() === "error") {
        if (
          expectedMeetingDiscardConsoleAllowed(
            { type: message.type(), text: message.text() },
            activePhase,
            diagnostics.expectedMeetingDiscardConsoleCount,
          )
        ) {
          diagnostics.expectedMeetingDiscardConsoleCount += 1;
          return;
        }
        diagnostics.consoleErrorCount += 1;
      }
    });
    page.on("pageerror", (error) => {
      diagnostics.pageErrorCount += 1;
      if (!firstPageErrorHint) {
        firstPageErrorHint = sanitizeMessage(error).slice(0, 160);
      }
    });
    page.on("requestfailed", (request) => {
      if (!browserDiagnosticsArmed) return;
      const expectedAbortCount = Number(expectedFetchAbortCountsByPhase.get(activePhase) ?? 0);
      if (isExpectedFetchAbort(request, activePhase, expectedAbortCount)) {
        expectedFetchAbortCountsByPhase.set(activePhase, expectedAbortCount + 1);
        diagnostics.expectedFetchAbortCount += 1;
        return;
      }
      diagnostics.requestFailureCount += 1;
      const kind = requestFailureKind(request, activePhase);
      diagnostics.requestFailureKinds[kind] =
        Number(diagnostics.requestFailureKinds[kind] ?? 0) + 1;
    });
    page.on("response", (response) => {
      if (!browserDiagnosticsArmed || response.status() < 400) return;
      const path = new URL(response.url()).pathname;
      if (
        expectedMeetingDiscardNotFoundAllowed(
          { status: response.status(), method: response.request().method(), path },
          activePhase,
          expectedMeetingDiscardNotFoundPaths,
        )
      ) {
        expectedMeetingDiscardNotFoundPaths.add(path);
        diagnostics.expectedMeetingDiscardNotFoundCount =
          expectedMeetingDiscardNotFoundPaths.size;
      }
    });
    activePhase = "navigate-meetings";
    const currentPathname = await page.evaluate(() => window.location.pathname);
    if (currentPathname !== "/meetings") {
      await page.waitForSelector('a[href="/meetings"]', {
        visible: true,
        timeout: options.navigationTimeoutMs,
      });
      await page.$eval('a[href="/meetings"]', (link) => {
        if (!(link instanceof HTMLAnchorElement)) {
          throw new Error("Meetings navigation link is unavailable");
        }
        link.click();
      });
      await page.waitForFunction(
        () => window.location.pathname === "/meetings",
        { timeout: options.navigationTimeoutMs },
      );
    }
    await page.waitForSelector('[data-page-shell="meetings"]', {
      visible: true,
    });
    activePhase = "resolve-backend-access";
    await page.waitForFunction(
      () =>
        Boolean(
          window.__SCRIBER_BACKEND_URL__ &&
            window.__SCRIBER_SESSION_TOKEN__,
        ),
      { timeout: options.navigationTimeoutMs },
    );
    const access = await page.evaluate(() => ({
      baseUrl: window.__SCRIBER_BACKEND_URL__,
      sessionToken: window.__SCRIBER_SESSION_TOKEN__,
    }));
    if (!access?.baseUrl || !access?.sessionToken) {
      throw new Error(
        "Tauri WebView did not expose authenticated backend access",
      );
    }
    activePhase = "verify-managed-backend";
    const { health, runtime } = await waitForManagedBackend(
      access,
      options.navigationTimeoutMs,
    );
    activePhase = "wait-frontend-websocket";
    await page.waitForSelector(
      '[data-page-shell="meetings"][data-websocket-connected="true"]',
      {
        visible: true,
        timeout: options.navigationTimeoutMs,
      },
    );
    browserDiagnosticsArmed = true;

    activePhase = "validate-meeting-readiness";
    const meetingCapabilities = await browserJson(
      page,
      "GET",
      "/api/meetings/capabilities",
    );
    if (
      meetingCapabilities?.nativeMeetingCapture !== true ||
      meetingCapabilities?.shellIpcAvailable !== true
    ) {
      throw new Error("Meeting readiness did not expose native capture");
    }
    const meetingAudioDevices = await browserJson(
      page,
      "GET",
      "/api/meetings/audio-devices",
    );
    if (
      meetingAudioDevices?.available !== true ||
      !Array.isArray(meetingAudioDevices?.capture) ||
      !Array.isArray(meetingAudioDevices?.render)
    ) {
      throw new Error("Meeting readiness returned no usable audio inventory");
    }
    const selectedCapture = meetingAudioDevices.capture.find(
      (endpoint) => endpoint?.isDefault === true,
    ) ?? meetingAudioDevices.capture[0];
    const selectedRender = meetingAudioDevices.render.find(
      (endpoint) => endpoint?.isDefault === true,
    ) ?? meetingAudioDevices.render[0];
    const meetingDeviceTest = await browserJson(
      page,
      "POST",
      "/api/meetings/device-test",
      {
        durationMs: 500,
        microphoneNativeEndpointIdHash: String(
          selectedCapture?.endpointIdHash ?? "",
        ),
        renderNativeEndpointIdHash: String(
          selectedRender?.endpointIdHash ?? "",
        ),
        aecEnabled: true,
        playTestTone: false,
      },
    );
    if (!meetingDeviceTestPassed(meetingDeviceTest)) {
      throw new Error("Meeting device test did not settle its privacy-safe probe");
    }

    activePhase = "prepare-meeting-form";
    await page.waitForSelector("#meeting-title", { visible: true });
    await page.$eval("#meeting-title", (input) => {
      input.focus();
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.type("#meeting-title", options.title);
    const startSelector = 'button[aria-describedby="meeting-start-status"]';
    activePhase = "wait-meeting-start-enabled";
    await page.waitForFunction(
      (selector) => {
        const button = document.querySelector(selector);
        return button instanceof HTMLButtonElement && !button.disabled;
      },
      { timeout: options.navigationTimeoutMs },
      startSelector,
    );
    activePhase = "start-meeting";
    const startResponsePromise = page.waitForResponse(
      (response) => {
        try {
          const request = response.request();
          const url = new URL(response.url());
          return (
            request.method() === "POST" && url.pathname === "/api/meetings"
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(startSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting start control is not actionable");
      }
      button.click();
    });
    const startResponse = await startResponsePromise;
    const startPayload = await startResponse.json().catch(() => null);
    updateMeetingDebug(startPayload);
    if (!startResponse.ok()) {
      throw new Error(
        `meeting start failed with HTTP ${startResponse.status()}`,
      );
    }
    const startedMeeting =
      startPayload?.meeting && typeof startPayload.meeting === "object"
        ? startPayload.meeting
        : startPayload;
    const meetingId = String(startedMeeting?.id ?? "");
    if (!meetingId)
      throw new Error("meeting start response omitted its identifier");

    activePhase = "wait-recording";
    await waitForMeetingState(
      access,
      meetingId,
      ["recording"],
      options.navigationTimeoutMs,
      observedStates,
    );
    await delay(options.prePauseMs);
    activePhase = "pause-meeting";
    await clickControl(
      page,
      meetingId,
      "pause",
      options.navigationTimeoutMs,
    );
    activePhase = "wait-paused";
    await waitForMeetingState(
      access,
      meetingId,
      ["paused"],
      options.navigationTimeoutMs,
      observedStates,
    );
    await delay(options.pausedMs);
    activePhase = "resume-meeting";
    await clickControl(
      page,
      meetingId,
      "resume",
      options.navigationTimeoutMs,
    );
    activePhase = "wait-resumed-recording";
    await waitForMeetingState(
      access,
      meetingId,
      ["recording"],
      options.navigationTimeoutMs,
      observedStates,
    );

    const postResumeMs = Math.max(4_000, options.fixtureDurationMs + 1_500);
    await delay(postResumeMs);
    activePhase = "stop-meeting";
    await clickControl(
      page,
      meetingId,
      "stop",
      options.navigationTimeoutMs,
    );
    activePhase = "wait-finalization";
    const finalDetail = await waitForMeetingState(
      access,
      meetingId,
      ["ready"],
      options.finalizationTimeoutMs,
      observedStates,
    );

    const workspaceTitle = `${options.title} workspace verified`;
    activePhase = "rename-meeting-workspace";
    const detailTitleSelector = '[data-testid="meeting-detail-title"]';
    const titleEditSelector = '[data-testid="meeting-title-edit"]';
    const titleInputSelector = '[data-testid="meeting-title-input"]';
    const titleSaveSelector = '[data-testid="meeting-title-save"]';
    await page.waitForSelector(detailTitleSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    await page.$eval(titleEditSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting title edit control is not actionable");
      }
      button.click();
    });
    await page.waitForSelector(titleInputSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    await page.focus(titleInputSelector);
    await page.keyboard.down("Control");
    await page.keyboard.press("A");
    await page.keyboard.up("Control");
    await page.keyboard.type(workspaceTitle);
    await page.waitForFunction(
      (selector) => {
        const button = document.querySelector(selector);
        return button instanceof HTMLButtonElement && !button.disabled;
      },
      { timeout: options.navigationTimeoutMs },
      titleSaveSelector,
    );
    const renameResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "PATCH" &&
            new URL(response.url()).pathname ===
              `/api/meetings/${encodeURIComponent(meetingId)}`
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(titleSaveSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting title save control is not actionable");
      }
      button.click();
    });
    const renameResponse = await renameResponsePromise;
    const renamed = await renameResponse.json().catch(() => null);
    if (!renameResponse.ok() || String(renamed?.title ?? "") !== workspaceTitle) {
      throw new Error("Meeting workspace title mutation was not durable");
    }
    await page.waitForFunction(
      (expected) =>
        document
          .querySelector('[data-testid="meeting-detail-title"]')
          ?.textContent?.trim() === expected,
      { timeout: options.navigationTimeoutMs },
      workspaceTitle,
    );

    const segments = Array.isArray(finalDetail?.segments)
      ? finalDetail.segments
      : [];
    const firstSegment = segments[0];
    const segmentId = String(firstSegment?.id ?? "");
    if (!segmentId) {
      throw new Error("Meeting workspace E2E needs one transcript segment");
    }
    const originalSegmentText = String(firstSegment?.text ?? "").trim();
    const workspaceMarker = "Workspace route E2E verified.";
    const editedSegmentText = `${originalSegmentText} ${workspaceMarker}`.trim();
    const segmentEditSelector = `[data-testid="meeting-segment-edit-${segmentId}"]`;
    const segmentEditInputSelector = `[data-testid="meeting-segment-edit-input-${segmentId}"]`;
    const segmentEditSaveSelector = `[data-testid="meeting-segment-edit-save-${segmentId}"]`;
    const segmentUndoSelector = `[data-testid="meeting-segment-undo-${segmentId}"]`;
    activePhase = "edit-meeting-workspace-segment";
    await page.$eval(segmentEditSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting segment edit control is not actionable");
      }
      button.click();
    });
    await page.waitForSelector(segmentEditInputSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    await page.focus(segmentEditInputSelector);
    await page.keyboard.down("Control");
    await page.keyboard.press("A");
    await page.keyboard.up("Control");
    await page.keyboard.type(editedSegmentText);
    await page.waitForFunction(
      (selector) => {
        const button = document.querySelector(selector);
        return button instanceof HTMLButtonElement && !button.disabled;
      },
      { timeout: options.navigationTimeoutMs },
      segmentEditSaveSelector,
    );
    const segmentEditResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "PATCH" &&
            new URL(response.url()).pathname ===
              `/api/meetings/${encodeURIComponent(meetingId)}/segments/${encodeURIComponent(segmentId)}`
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(segmentEditSaveSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting segment save control is not actionable");
      }
      button.click();
    });
    const segmentEditResponse = await segmentEditResponsePromise;
    const edited = await segmentEditResponse.json().catch(() => null);
    if (!segmentEditResponse.ok() || String(edited?.segment?.text ?? "") !== editedSegmentText) {
      throw new Error("Meeting workspace segment mutation was not durable");
    }
    await page.waitForFunction(
      ({ expected, selector }) =>
        document.querySelector(selector)?.textContent?.includes(expected) === true,
      { timeout: options.navigationTimeoutMs },
      {
        expected: workspaceMarker,
        selector: `[data-testid="meeting-transcript-segment-${segmentId}"]`,
      },
    );
    activePhase = "search-meeting-workspace-segment";
    const search = await browserJson(
      page,
      "GET",
      `/api/meetings/${encodeURIComponent(meetingId)}/search?q=${encodeURIComponent(workspaceMarker)}`,
    );
    if (!Array.isArray(search?.items) || !search.items.some((item) => String(item?.id ?? "") === segmentId)) {
      throw new Error("Meeting workspace search did not observe the durable correction");
    }
    const history = await browserJson(
      page,
      "GET",
      `/api/meetings/${encodeURIComponent(meetingId)}/segments/${encodeURIComponent(segmentId)}/edits`,
    );
    if (!Array.isArray(history?.items) || history.items.length === 0) {
      throw new Error("Meeting workspace edit history omitted the durable correction");
    }
    activePhase = "undo-meeting-workspace-segment";
    await page.waitForSelector(segmentUndoSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    const segmentUndoResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "POST" &&
            new URL(response.url()).pathname ===
              `/api/meetings/${encodeURIComponent(meetingId)}/segments/${encodeURIComponent(segmentId)}/undo`
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(segmentUndoSelector, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Meeting segment undo control is not actionable");
      }
      button.click();
    });
    const segmentUndoResponse = await segmentUndoResponsePromise;
    if (!segmentUndoResponse.ok()) {
      throw new Error("Meeting workspace undo was not durable");
    }
    await page.waitForFunction(
      ({ forbidden, selector }) =>
        document.querySelector(selector)?.textContent?.includes(forbidden) === false,
      { timeout: options.navigationTimeoutMs },
      {
        forbidden: workspaceMarker,
        selector: `[data-testid="meeting-transcript-segment-${segmentId}"]`,
      },
    );

    const workspaceNote = "Meeting workspace E2E note verified.";
    activePhase = "save-meeting-workspace-note";
    await page.$eval('[data-testid="meeting-workspace-tab-notes"]', (button) => {
      if (!(button instanceof HTMLButtonElement)) {
        throw new Error("Meeting notes workspace tab is unavailable");
      }
      button.click();
    });
    const workspaceNoteSelector = '[data-testid="meeting-workspace-note"]';
    await page.waitForSelector(workspaceNoteSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    const noteResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "PUT" &&
            new URL(response.url()).pathname ===
              `/api/meetings/${encodeURIComponent(meetingId)}/notes`
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.focus(workspaceNoteSelector);
    await page.keyboard.down("Control");
    await page.keyboard.press("A");
    await page.keyboard.up("Control");
    await page.keyboard.type(workspaceNote);
    const noteResponse = await noteResponsePromise;
    const note = await noteResponse.json().catch(() => null);
    if (
      !noteResponse.ok() ||
      String(note?.body ?? "") !== workspaceNote ||
      note?.writeApplied === false
    ) {
      throw new Error("Meeting workspace note mutation was not durable");
    }
    await page.waitForFunction(
      (expected) =>
        document.querySelector('[data-testid="meeting-workspace-note"]')?.value === expected,
      { timeout: options.navigationTimeoutMs },
      workspaceNote,
    );

    activePhase = "validate-transcript-content";
    const transcript = segments
      .map((segment) => String(segment?.text ?? ""))
      .join(" ")
      .trim();
    if (segments.length === 0 || transcript.length < 12) {
      throw new Error(
        "final Meeting detail contains no meaningful transcript segments",
      );
    }
    const normalizedTranscript = normalizeText(transcript);
    const matchedExpectedTokens = options.expectedTokens.filter((token) =>
      normalizedTranscript.includes(token),
    );
    activePhase = "validate-transcript-marker";
    if (
      options.expectedTokens.length > 0 &&
      matchedExpectedTokens.length === 0
    ) {
      throw new Error(
        "final transcript did not contain any configured synthetic marker",
      );
    }
    const audioGaps = Array.isArray(finalDetail?.audioGaps)
      ? finalDetail.audioGaps
      : [];
    activePhase = "validate-audio-gap";
    if (audioGaps.length === 0) {
      throw new Error("pause/resume flow did not persist an audio gap");
    }
    activePhase = "reprocess-meeting-transcript";
    const reprocess = await browserJson(
      page,
      "POST",
      `/api/meetings/${encodeURIComponent(meetingId)}/reprocess`,
      { mode: "full_transcript" },
    );
    if (
      String(reprocess?.mode ?? "") !== "full_transcript" ||
      String(reprocess?.meeting?.state ?? "") !== "finalizing"
    ) {
      throw new Error("Meeting reprocess admission did not reserve finalization");
    }
    const reprocessedDetail = await waitForMeetingState(
      access,
      meetingId,
      ["ready"],
      options.finalizationTimeoutMs,
      observedStates,
    );
    const reprocessedTranscript = Array.isArray(reprocessedDetail?.segments)
      ? reprocessedDetail.segments
          .map((segment) => String(segment?.text ?? ""))
          .join(" ")
          .trim()
      : "";
    if (reprocessedTranscript.length < 12) {
      throw new Error("Meeting reprocess produced no meaningful transcript");
    }
    if (
      options.expectedTokens.length > 0 &&
      !options.expectedTokens.some((token) =>
        normalizeText(reprocessedTranscript).includes(token),
      )
    ) {
      throw new Error("Meeting reprocess lost every configured synthetic marker");
    }
    activePhase = "validate-meeting-artifacts";
    const artifactBase = `/api/meetings/${encodeURIComponent(meetingId)}`;
    const artifactPaths = {
      emailDraft: "/export-email",
      emailPreview: "/email-preview",
      exportJson: "/export/json",
      exportPdf: "/export/pdf",
    };
    const artifactJson = await browserJson(
      page,
      "GET",
      `${artifactBase}${artifactPaths.exportJson}`,
    );
    if (
      String(artifactJson?.id ?? "") !== meetingId ||
      !Array.isArray(artifactJson?.segments) ||
      artifactJson.segments.length === 0
    ) {
      throw new Error("Meeting JSON export omitted durable transcript content");
    }
    const artifactPdf = await browserArtifact(
      page,
      `${artifactBase}${artifactPaths.exportPdf}`,
    );
    if (
      !artifactPdf.contentType.startsWith("application/pdf") ||
      artifactPdf.byteLength < 100
    ) {
      throw new Error("Meeting document renderer produced no valid PDF artifact");
    }
    const emailPreview = await browserJson(
      page,
      "GET",
      `${artifactBase}${artifactPaths.emailPreview}`,
    );
    if (
      String(emailPreview?.apiVersion ?? "") === "" ||
      String(emailPreview?.subject ?? "") === ""
    ) {
      throw new Error("Meeting email preview omitted its public contract");
    }
    const emailDraft = await browserArtifact(
      page,
      `${artifactBase}${artifactPaths.emailDraft}`,
    );
    if (
      !emailDraft.contentType.startsWith("message/rfc822") ||
      emailDraft.byteLength < 100
    ) {
      throw new Error("Meeting email draft was not a bounded RFC 822 artifact");
    }
    for (const audioPath of [
      `${artifactBase}/audio`,
      `${artifactBase}/audio/microphone`,
      `${artifactBase}/audio/system`,
    ]) {
      const audio = await browserArtifact(page, audioPath, {
        "Range": "bytes=0-3",
      });
      if (
        audio.status !== 206 ||
        audio.byteLength !== 4 ||
        audio.cacheControl !== "private, no-store"
      ) {
        throw new Error("Meeting playback did not honor the private byte-range contract");
      }
    }
    activePhase = "validate-meeting-catalog";
    const catalog = await browserJson(page, "GET", "/api/meetings?limit=7&offset=0");
    if (
      !Array.isArray(catalog?.items) ||
      !catalog.items.some((meeting) => String(meeting?.id ?? "") === meetingId)
    ) {
      throw new Error("Meeting catalogue omitted the durable Meeting");
    }
    const catalogDetail = await browserJson(page, "GET", artifactBase);
    if (
      String(catalogDetail?.id ?? "") !== meetingId ||
      String(catalogDetail?.title ?? "") !== workspaceTitle
    ) {
      throw new Error("Meeting catalogue detail omitted the durable projection");
    }

    activePhase = "discard-meeting-through-ui";
    const catalogItemSelector = `[data-testid="meeting-catalog-item-${meetingId}"]`;
    const discardSelector = `[data-testid="meeting-catalog-discard-${meetingId}"]`;
    await page.waitForSelector(discardSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    await page.hover(catalogItemSelector);
    await page.click(discardSelector);
    const confirmSelector = '[data-testid="meeting-catalog-discard-confirm"]';
    await page.waitForSelector(confirmSelector, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    const discardResponse = page.waitForResponse(
      (response) => {
        const request = response.request();
        try {
          const url = new URL(response.url());
          return request.method() === "DELETE" && url.pathname === artifactBase;
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.click(confirmSelector);
    const discardedResponse = await discardResponse;
    if (!discardedResponse.ok()) {
      throw new Error(`Meeting catalogue discard failed with HTTP ${discardedResponse.status()}`);
    }
    await page.waitForFunction(
      (itemSelector) =>
        window.location.pathname.endsWith("/meetings") &&
        document.querySelector(itemSelector) == null,
      { timeout: options.navigationTimeoutMs },
      catalogItemSelector,
    );
    const catalogAfterDiscard = await browserJson(page, "GET", "/api/meetings?limit=7&offset=0");
    if (
      !Array.isArray(catalogAfterDiscard?.items) ||
      catalogAfterDiscard.items.some((meeting) => String(meeting?.id ?? "") === meetingId)
    ) {
      throw new Error("Meeting catalogue retained a discarded Meeting");
    }

    activePhase = "navigate-live-mic";
    await page.$eval('a[href="/"]', (link) => {
      if (!(link instanceof HTMLAnchorElement)) {
        throw new Error("Live Mic navigation link is unavailable");
      }
      link.click();
    });
    await page.waitForFunction(
      () =>
        window.location.pathname === "/" &&
        document.querySelector('[data-page-shell="live-mic"]') != null,
      { timeout: options.navigationTimeoutMs },
    );

    const liveMicButton = "#live-mic-toggle-button";
    activePhase = "start-live-mic-through-ui";
    await page.waitForSelector(liveMicButton, {
      visible: true,
      timeout: options.navigationTimeoutMs,
    });
    const liveMicStartResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "POST" &&
            new URL(response.url()).pathname === "/api/live-mic/start"
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(liveMicButton, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Live Mic start control is not actionable");
      }
      button.click();
    });
    const liveMicStartResponse = await liveMicStartResponsePromise;
    const liveMicStartPayload = await liveMicStartResponse.json().catch(() => null);
    if (!liveMicStartResponse.ok()) {
      throw new Error(
        `Live Mic start failed with HTTP ${liveMicStartResponse.status()}`,
      );
    }
    const liveMicSessionId = String(liveMicStartPayload?.sessionId ?? "").trim();
    if (!liveMicSessionId) {
      throw new Error("Live Mic start returned no session identity");
    }
    await waitForLiveMicState(
      page,
      liveMicSessionId,
      ["recording"],
      options.navigationTimeoutMs,
    );
    await page.waitForSelector(".glossy-mic-wrapper.is-recording", {
      timeout: options.navigationTimeoutMs,
    });
    await delay(Math.max(2_000, options.fixtureDurationMs + 500));

    activePhase = "stop-live-mic-through-ui";
    const liveMicStopResponsePromise = page.waitForResponse(
      (response) => {
        try {
          return (
            response.request().method() === "POST" &&
            new URL(response.url()).pathname === "/api/live-mic/stop-request"
          );
        } catch {
          return false;
        }
      },
      { timeout: options.navigationTimeoutMs },
    );
    await page.$eval(liveMicButton, (button) => {
      if (!(button instanceof HTMLButtonElement) || button.disabled) {
        throw new Error("Live Mic stop control is not actionable");
      }
      button.click();
    });
    const liveMicStopResponse = await liveMicStopResponsePromise;
    if (!liveMicStopResponse.ok()) {
      throw new Error(
        `Live Mic stop failed with HTTP ${liveMicStopResponse.status()}`,
      );
    }

    activePhase = "validate-live-mic-transcript";
    const liveMicTranscript = await waitForLiveMicTranscript(
      page,
      liveMicSessionId,
      options.finalizationTimeoutMs,
    );
    const liveMicText = String(liveMicTranscript?.content ?? "");
    const normalizedLiveMicText = normalizeText(liveMicText);
    const liveMicMatchedTokens = options.expectedTokens.filter((token) =>
      normalizedLiveMicText.includes(token),
    );
    if (liveMicMatchedTokens.length === 0) {
      throw new Error("Live Mic transcript contained no configured synthetic marker");
    }
    await page.waitForFunction(
      (expectedTokens) => {
        const transcript = document.querySelector(
          '[data-testid="live-mic-transcript-output"]',
        );
        const normalized = String(transcript?.textContent ?? "")
          .normalize("NFKD")
          .replace(/[\u0300-\u036f]/g, "")
          .toLocaleLowerCase("de-DE")
          .replace(/[^a-z0-9]+/g, " ")
          .trim();
        return expectedTokens.some((token) => normalized.includes(token));
      },
      { timeout: options.navigationTimeoutMs },
      options.expectedTokens,
    );
    if (
      diagnostics.expectedMeetingDiscardConsoleCount !==
        diagnostics.expectedMeetingDiscardNotFoundCount ||
      diagnostics.consoleErrorCount > 0 ||
      diagnostics.pageErrorCount > 0 ||
      diagnostics.requestFailureCount > 0
    ) {
      activePhase = "validate-browser-diagnostics";
      throw new Error(
        firstPageErrorHint
          ? "WebView browser diagnostics gate failed after Meeting validation (sanitized hint retained internally)"
          : "WebView browser diagnostics gate failed after Meeting validation",
      );
    }
    activePhase = "complete";

    return {
      schemaVersion: 1,
      ok: true,
      automation: "puppeteer-core",
      browserTransport: "webview2-remote-debugging",
      apiVersion: String(health?.apiVersion ?? runtime?.apiVersion ?? ""),
      runtimeMode: String(runtime?.runtimeMode ?? ""),
      meetingIdHash: hashHint(meetingId),
      observedStates,
      segmentCount: segments.length,
      transcriptCharacterCount: transcript.length,
      expectedTokenCount: options.expectedTokens.length,
      matchedExpectedTokenCount: matchedExpectedTokens.length,
      audioGapCount: audioGaps.length,
      meetingCapabilitiesVerified: true,
      meetingAudioDevicesVerified: true,
      meetingDeviceTestVerified: true,
      workspaceTitleVerified: true,
      workspaceSegmentVerified: true,
      workspaceNoteVerified: true,
      meetingReprocessVerified: true,
      meetingArtifactJsonVerified: true,
      meetingArtifactDocumentVerified: true,
      meetingArtifactEmailVerified: true,
      meetingArtifactPlaybackVerified: true,
      meetingCatalogListVerified: true,
      meetingCatalogDetailVerified: true,
      meetingCatalogDiscardVerified: true,
      liveMicStartVerified: true,
      liveMicStopVerified: true,
      liveMicTranscriptVerified: true,
      liveMicSessionIdHash: hashHint(liveMicSessionId),
      liveMicTranscriptCharacterCount: liveMicText.length,
      liveMicMatchedExpectedTokenCount: liveMicMatchedTokens.length,
      fixtureDurationMs: options.fixtureDurationMs,
      elapsedMs: Date.now() - startedAt,
      diagnostics: { ...diagnostics },
      meetingDebug: meetingDebugSnapshot(),
    };
  } finally {
    if (browser) await browser.disconnect();
  }
}

let options;
try {
  options = parseArguments(process.argv.slice(2));
  const result = await run(options);
  await writeResult(options.output, result);
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  const result = {
    schemaVersion: 1,
    ok: false,
    automation: "puppeteer-core",
    browserTransport: "webview2-remote-debugging",
    phase: activePhase,
    errorType: error?.constructor?.name ?? "Error",
    message: sanitizeMessage(error),
    observedStates: [...observedStates],
    diagnostics: { ...diagnostics },
    meetingDebug: meetingDebugSnapshot(harnessErrorCodeForPhase(activePhase)),
  };
  if (options?.output) {
    await writeResult(options.output, result).catch(() => {});
  }
  process.stderr.write(`${JSON.stringify(result)}\n`);
  process.exitCode = 1;
}
