import assert from "node:assert/strict";
import test from "node:test";

import { settingsTranslations } from "@/i18n/translations/de/settings";
import {
  META_CREDENTIAL_REQUIREMENT,
  META_MEETING_FINAL_STT_OPTION,
  META_TRANSCRIPTION_OPTIONS,
  metaFrontendModelForService,
  metaSettingsPatchForModel,
} from "@/lib/meta-transcription-settings";

test("Meta offers separate realtime and async routes for one published model", () => {
  assert.deepEqual(
    META_TRANSCRIPTION_OPTIONS.map((option) => option.group),
    ["cloud_streaming", "cloud_async"],
  );
  for (const option of META_TRANSCRIPTION_OPTIONS) {
    assert.equal(option.model, "muse-voice-transcribe-1.0");
    assert.equal(option.usdPerHour, 0.18);
    assert.equal(metaFrontendModelForService(option.service), option.value);
    assert.deepEqual(metaSettingsPatchForModel(option.value), { defaultSttService: option.service });
    assert.ok(settingsTranslations[option.routeNote]);
    assert.equal("wordErrorRatePercent" in option, false);
  }
  assert.equal(metaSettingsPatchForModel("meta-unknown"), null);
  assert.equal(metaFrontendModelForService("meta"), null);
});

test("both Meta STT routes reuse the existing Meta Model API credential", () => {
  assert.deepEqual(META_CREDENTIAL_REQUIREMENT, {
    provider: "Meta Model API",
    label: "Meta Model API key",
    helpKey: "meta",
  });
});

test("Meta Meeting option does not promise five-hour or word-level support", () => {
  assert.equal(META_MEETING_FINAL_STT_OPTION.value, "meta_stt_async");
  assert.equal(META_MEETING_FINAL_STT_OPTION.nativeDiarization, true);
  assert.equal(META_MEETING_FINAL_STT_OPTION.fiveHourSupported, false);
  assert.match(META_MEETING_FINAL_STT_OPTION.detail, /10 minutes/);
  assert.ok(settingsTranslations[META_MEETING_FINAL_STT_OPTION.detail]);
});
