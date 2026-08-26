import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const settingsSource = readFileSync(path.resolve(import.meta.dirname, "../pages/Settings.tsx"), "utf8");
const compactSettingsSource = settingsSource.replace(/\s+/g, " ");
const translationsSource = readFileSync(
  path.resolve(import.meta.dirname, "../i18n/translations/de/settings.ts"),
  "utf8",
);
const compactTranslationsSource = translationsSource.replace(/\s+/g, " ");

test("offers Gemini 3.5 Transcribe as separate live and async provider choices", () => {
  assert.match(compactSettingsSource, /value: "gemini-realtime", label: "Gemini 3\.5 Transcribe Live"/);
  assert.match(compactSettingsSource, /value: "gemini-stt", label: "Gemini 3\.5 Transcribe"/);
  assert.match(
    compactSettingsSource,
    /benchmarkOption\( "gemini-realtime", "Gemini 3\.5 Transcribe Live", 9\.0, 4\.0, "cloud_streaming", "gemini"/,
  );
  assert.match(
    compactSettingsSource,
    /benchmarkOption\( "gemini-stt", "Gemini 3\.5 Transcribe", 5\.0, 2\.6, "cloud_async", "gemini"/,
  );
  assert.match(compactSettingsSource, /model: providerModels\[value\] \|\| ""/);
});

test("round-trips Gemini provider choices through the settings API contract", () => {
  assert.match(compactSettingsSource, /if \(service === "gemini_realtime"\) \{ return "gemini-realtime"; \}/);
  assert.match(compactSettingsSource, /if \(service === "gemini_stt"\) \{ return "gemini-stt"; \}/);
  assert.match(
    compactSettingsSource,
    /value === "gemini-realtime"\) \{ await updateSettings\(\{ defaultSttService: "gemini_realtime" \}\)/,
  );
  assert.match(
    compactSettingsSource,
    /value === "gemini-stt"\) \{ await updateSettings\(\{ defaultSttService: "gemini_stt" \}\)/,
  );
  assert.match(
    compactSettingsSource,
    /case "gemini-realtime": case "gemini-stt": return \{ provider: "Gemini", label: "Gemini API key", helpKey: "gemini" \}/,
  );
});

test("shows the dedicated async model in Meeting settings and localizes Gemini guidance", () => {
  assert.match(
    compactSettingsSource,
    /value: "gemini_stt", label: "Gemini 3\.5 Transcribe", model: "gemini-3\.5-transcribe", credentialModel: "gemini-stt", recommended: false, nativeDiarization: true/,
  );
  assert.match(
    compactTranslationsSource,
    /"Interim and final text · smart transcription": "Zwischen- und Endergebnisse · intelligente Transkription"/,
  );
  assert.match(
    compactTranslationsSource,
    /"Speaker diarization and word timestamps · final text": "Sprechertrennung und Wortzeitstempel · finaler Text"/,
  );
});
