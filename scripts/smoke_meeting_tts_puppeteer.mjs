import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let activePhase = "bootstrap";
const diagnostics = {
  consoleErrorCount: 0,
  pageErrorCount: 0,
  requestFailureCount: 0,
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
    "validate-page-errors": "webview_page_error",
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
      if (message.type() === "error") diagnostics.consoleErrorCount += 1;
    });
    page.on("pageerror", (error) => {
      diagnostics.pageErrorCount += 1;
      if (!firstPageErrorHint) {
        firstPageErrorHint = sanitizeMessage(error).slice(0, 160);
      }
    });
    page.on("requestfailed", () => {
      diagnostics.requestFailureCount += 1;
    });

    activePhase = "navigate-meetings";
    const currentUrl = new URL(page.url());
    currentUrl.pathname = "/meetings";
    currentUrl.search = "";
    currentUrl.hash = "";
    if (page.url() !== currentUrl.toString()) {
      await page.goto(currentUrl.toString(), { waitUntil: "domcontentloaded" });
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

    activePhase = "validate-transcript-content";
    const segments = Array.isArray(finalDetail?.segments)
      ? finalDetail.segments
      : [];
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
    if (diagnostics.pageErrorCount > 0) {
      activePhase = "validate-page-errors";
      throw new Error(
        firstPageErrorHint
          ? "WebView page error gate failed after Meeting validation (sanitized hint retained internally)"
          : "WebView page error gate failed after Meeting validation",
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
