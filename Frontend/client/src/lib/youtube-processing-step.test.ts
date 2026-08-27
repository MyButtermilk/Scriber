import assert from "node:assert/strict";
import test from "node:test";
import { localizedYoutubeProcessingStep } from "@/lib/youtube-processing-step";

type Values = Record<string, string | number>;

function interpolate(template: string, values: Values = {}): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_match, key: string) => String(values[key] ?? ""));
}

const t = (source: string, values?: Values): string => {
  const translated: Record<string, string> = {
    "Retrying audio download in {{seconds}}s ({{attempt}}/{{total}})":
      "Audiodownload wird in {{seconds}} s erneut versucht ({{attempt}}/{{total}})",
    "Retrying in {{seconds}}s ({{attempt}}/{{total}})": "Neuer Versuch in {{seconds}} s ({{attempt}}/{{total}})",
    "Downloading… {{percent}}": "Download läuft … {{percent}}",
    ETA: "Restzeit",
  };
  return interpolate(translated[source] || source, values);
};

const formatNumber = (value: number, options?: Intl.NumberFormatOptions): string =>
  new Intl.NumberFormat("de-DE", options).format(value);

test("localizes YouTube audio retry state separately from background job retries", () => {
  assert.equal(
    localizedYoutubeProcessingStep("Retrying audio download in 2s (1/3)", "Processing", t, formatNumber),
    "Audiodownload wird in 2 s erneut versucht (1/3)",
  );
  assert.equal(
    localizedYoutubeProcessingStep("Retrying in 5s (2/3)", "Processing", t, formatNumber),
    "Neuer Versuch in 5 s (2/3)",
  );
});

test("keeps download progress evidence and ETA localized", () => {
  assert.equal(
    localizedYoutubeProcessingStep("Downloading... 12.5% • 1.0MiB/s • ETA 00:08", "Processing", t, formatNumber),
    "Download läuft … 12,5 % • 1.0MiB/s • Restzeit 00:08",
  );
});
