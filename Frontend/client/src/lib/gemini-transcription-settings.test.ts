import assert from "node:assert/strict";
import test from "node:test";

import { settingsTranslations } from "@/i18n/translations/de/settings";
import {
  GEMINI_ASYNC_TRANSCRIPTION_OPTION,
  GEMINI_CREDENTIAL_REQUIREMENT,
  GEMINI_MEETING_FINAL_STT_OPTION,
  GEMINI_REALTIME_TRANSCRIPTION_OPTION,
  geminiFrontendModelForService,
  geminiSettingsPatchForModel,
} from "@/lib/gemini-transcription-settings";

test("offers Gemini 3.5 Transcribe as separate live and async provider choices", () => {
  assert.deepEqual(GEMINI_REALTIME_TRANSCRIPTION_OPTION, {
    value: "gemini-realtime",
    service: "gemini_realtime",
    label: "Gemini 3.5 Transcribe Live",
    usdPerThousandMinutes: 9.0,
    wordErrorRatePercent: 4.0,
    group: "cloud_streaming",
    icon: "gemini",
    routeNote: "Interim and final text · smart transcription",
  });
  assert.deepEqual(GEMINI_ASYNC_TRANSCRIPTION_OPTION, {
    value: "gemini-stt",
    service: "gemini_stt",
    label: "Gemini 3.5 Transcribe",
    usdPerThousandMinutes: 5.0,
    wordErrorRatePercent: 2.6,
    group: "cloud_async",
    icon: "gemini",
    routeNote: "Speaker diarization and word timestamps · final text",
  });
});

test("round-trips Gemini provider choices through the settings API contract", () => {
  assert.equal(geminiFrontendModelForService("gemini_realtime"), "gemini-realtime");
  assert.equal(geminiFrontendModelForService("gemini_stt"), "gemini-stt");
  assert.equal(geminiFrontendModelForService("soniox"), null);
  assert.deepEqual(geminiSettingsPatchForModel("gemini-realtime"), {
    defaultSttService: "gemini_realtime",
  });
  assert.deepEqual(geminiSettingsPatchForModel("gemini-stt"), {
    defaultSttService: "gemini_stt",
  });
  assert.equal(geminiSettingsPatchForModel("soniox-realtime"), null);
  assert.deepEqual(GEMINI_CREDENTIAL_REQUIREMENT, {
    provider: "Gemini",
    label: "Gemini API key",
    helpKey: "gemini",
  });
});

test("shows the dedicated async model in Meeting settings and localizes Gemini guidance", () => {
  assert.deepEqual(GEMINI_MEETING_FINAL_STT_OPTION, {
    value: "gemini_stt",
    label: "Gemini 3.5 Transcribe",
    model: "gemini-3.5-transcribe",
    credentialModel: "gemini-stt",
    recommended: false,
    nativeDiarization: true,
    fiveHourSupported: false,
    detail: "Creates the final transcript with speaker names and word-level timing after the meeting.",
  });
  assert.equal(
    settingsTranslations[GEMINI_REALTIME_TRANSCRIPTION_OPTION.routeNote],
    "Zwischen- und Endergebnisse · intelligente Transkription",
  );
  assert.equal(
    settingsTranslations[GEMINI_ASYNC_TRANSCRIPTION_OPTION.routeNote],
    "Sprechertrennung und Wortzeitstempel · finaler Text",
  );
});
