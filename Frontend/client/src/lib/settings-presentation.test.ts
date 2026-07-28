import assert from "node:assert/strict";
import test from "node:test";
import {
  chooseActiveSettingsSection,
  defaultPostProcessingPrompt,
  fixedEstimateExchangeRateLabel,
  formatCurrency,
  formatEstimatedEuroFromUsd,
  isDefaultPostProcessingPrompt,
  type SettingsSectionKey,
} from "@/lib/settings-presentation";

test("provides complete locale-specific cleanup defaults", () => {
  const german = defaultPostProcessingPrompt("de");
  const english = defaultPostProcessingPrompt("en");

  assert.match(german, /^Glätte das folgende Speech-to-Text-Transkript/);
  assert.match(english, /^Polish the following speech-to-text transcript/);
  assert.match(german, /\$\{output\}$/);
  assert.match(english, /\$\{output\}$/);
  assert.notEqual(german, english);
  assert.equal(isDefaultPostProcessingPrompt(`\n${german}\n`), true);
  assert.equal(isDefaultPostProcessingPrompt(english), true);
  assert.equal(isDefaultPostProcessingPrompt("Keep this custom prompt."), false);
});

test("formats currencies with Intl conventions and discloses the fixed estimate rate", () => {
  assert.equal(formatCurrency(12.5, "USD", "en-US"), "$12.50");
  assert.equal(formatCurrency(12.5, "EUR", "en-US"), "€12.50");
  assert.match(formatCurrency(12.5, "EUR", "de-DE"), /12,50\s*€/);
  assert.equal(formatEstimatedEuroFromUsd(1, "en-US", 3), "€0.877");
  assert.equal(fixedEstimateExchangeRateLabel("en-US"), "$1 = €0.877");
  assert.match(fixedEstimateExchangeRateLabel("de-DE"), /1\s*\$\s*=\s*0,877\s*€/);
});

test("keeps the selected settings section while it remains visible", () => {
  const firstRow = new Set<SettingsSectionKey>(["transcription", "providers"]);
  assert.equal(chooseActiveSettingsSection(firstRow, "providers"), "providers");
  assert.equal(chooseActiveSettingsSection(firstRow, "meetings"), "transcription");
});
