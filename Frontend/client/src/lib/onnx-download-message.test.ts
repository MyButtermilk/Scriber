import assert from "node:assert/strict";
import test from "node:test";

import { translate, type TranslationValues } from "@/i18n";
import { localizeOnnxDownloadMessage } from "./onnx-download-message";

const german = (source: string, values?: TranslationValues) => translate("de", source, values);
const english = (source: string, values?: TranslationValues) => translate("en", source, values);
const germanNumber = (value: number, options?: Intl.NumberFormatOptions) =>
  new Intl.NumberFormat("de-DE", options).format(value);
const englishNumber = (value: number, options?: Intl.NumberFormatOptions) =>
  new Intl.NumberFormat("en-US", options).format(value);

test("localizes every structured ONNX download progress message", () => {
  assert.equal(
    localizeOnnxDownloadMessage(
      "Downloading model files (int8). This can take a while...",
      german,
      germanNumber,
    ),
    "Modelldateien werden heruntergeladen (int8). Dies kann eine Weile dauern ...",
  );
  assert.equal(
    localizeOnnxDownloadMessage("Downloading files 1/12...", german, germanNumber),
    "Dateien werden heruntergeladen: 1/12 ...",
  );
  assert.equal(
    localizeOnnxDownloadMessage(
      "Downloading encoder_model.onnx (12.5 MB/800.0 MB)",
      german,
      germanNumber,
    ),
    "encoder_model.onnx wird heruntergeladen (12,5 MB/800,0 MB)",
  );
  assert.equal(
    localizeOnnxDownloadMessage("Preparing local model package...", german, germanNumber),
    "Lokales Modellpaket wird vorbereitet ...",
  );
  assert.equal(
    localizeOnnxDownloadMessage("Download failed: connection reset", german, germanNumber),
    "Download fehlgeschlagen: connection reset",
  );
});

test("preserves the English source forms and safely falls back for unknown messages", () => {
  assert.equal(
    localizeOnnxDownloadMessage("Downloading files 2/10...", english, englishNumber),
    "Downloading files 2/10...",
  );
  assert.equal(
    localizeOnnxDownloadMessage("Waiting for cache lock", german, germanNumber),
    "Waiting for cache lock",
  );
  assert.equal(localizeOnnxDownloadMessage(undefined, german, germanNumber), "Wird heruntergeladen ...");
});
