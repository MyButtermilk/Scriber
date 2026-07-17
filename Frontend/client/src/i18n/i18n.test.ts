import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import * as ts from "typescript";
import {
  LANGUAGE_STORAGE_KEY,
  getLocaleTag,
  localeFromLanguages,
  localizeLegacyDateLabel,
  translate,
} from "@/i18n";
import { componentTranslations } from "@/i18n/translations/de/components";
import { coreTranslations } from "@/i18n/translations/de/core";
import { germanTranslations } from "@/i18n/translations/de";
import { meetingsTranslations } from "@/i18n/translations/de/meetings";
import { settingsTranslations } from "@/i18n/translations/de/settings";
import { transcriptionTranslations } from "@/i18n/translations/de/transcription";

function placeholders(value: string): string[] {
  return Array.from(value.matchAll(/\{\{([a-zA-Z0-9_]+)\}\}/g), (match) => match[1]).sort();
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(absolutePath);
    }
    if (!entry.name.endsWith(".ts") && !entry.name.endsWith(".tsx")) {
      return [];
    }
    if (entry.name.endsWith(".test.ts") || entry.name.endsWith(".test.tsx")) {
      return [];
    }
    return [absolutePath];
  });
}

function staticStringKeys(expression: ts.Expression): string[] {
  if (ts.isStringLiteralLike(expression)) {
    return [expression.text];
  }
  if (ts.isParenthesizedExpression(expression)) {
    return staticStringKeys(expression.expression);
  }
  if (ts.isConditionalExpression(expression)) {
    return [
      ...staticStringKeys(expression.whenTrue),
      ...staticStringKeys(expression.whenFalse),
    ];
  }
  if (
    ts.isBinaryExpression(expression)
    && (
      expression.operatorToken.kind === ts.SyntaxKind.BarBarToken
      || expression.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
    )
  ) {
    return [
      ...staticStringKeys(expression.left),
      ...staticStringKeys(expression.right),
    ];
  }
  if (ts.isAsExpression(expression) || ts.isTypeAssertionExpression(expression)) {
    return staticStringKeys(expression.expression);
  }
  return [];
}

function literalTranslationKeys(source: string, fileName: string): string[] {
  const keys: string[] = [];
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "t"
      && node.arguments.length > 0
    ) {
      keys.push(...staticStringKeys(node.arguments[0]));
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return keys;
}

test("translates known text and preserves the English source", () => {
  assert.equal(translate("de", "Save"), "Speichern");
  assert.equal(translate("en", "Save"), "Save");
  assert.equal(translate("de", "Unknown catalog entry"), "Unknown catalog entry");
});

test("interpolates values after selecting the locale", () => {
  assert.equal(
    translate("de", "Transcript #{{id}}", { id: 42 }),
    "Transkript #42",
  );
  assert.equal(
    translate("en", "Transcript #{{id}}", { id: 42 }),
    "Transcript #42",
  );
});

test("catalog lookup ignores inherited object properties", () => {
  assert.equal(translate("de", "toString"), "toString");
  assert.equal(translate("de", "constructor", { value: 1 }), "constructor");
  assert.equal(translate("de", "__proto__"), "__proto__");
});

test("localizes legacy relative date labels", () => {
  assert.equal(localizeLegacyDateLabel("de", "Today, 14:30"), "Heute um 14:30");
  assert.equal(localizeLegacyDateLabel("en", "Today, 2:30 PM"), "Today at 2:30 PM");
  assert.equal(localizeLegacyDateLabel("de", "Yesterday"), "Gestern");
  assert.equal(localizeLegacyDateLabel("de", "2026-07-16"), "2026-07-16");
});

test("German translations are non-empty and preserve placeholders", () => {
  for (const [source, translated] of Object.entries(germanTranslations)) {
    assert.notEqual(translated.trim(), "", `Empty German translation for ${source}`);
    assert.deepEqual(
      placeholders(translated),
      placeholders(source),
      `Placeholder mismatch for ${source}`,
    );
  }
});

test("overlapping catalogs never disagree on a German translation", () => {
  const catalogs = [
    coreTranslations,
    componentTranslations,
    transcriptionTranslations,
    meetingsTranslations,
    settingsTranslations,
  ];
  const seen = new Map<string, string>();
  for (const catalog of catalogs) {
    for (const [source, translated] of Object.entries(catalog)) {
      const previous = seen.get(source);
      assert.ok(
        previous === undefined || previous === translated,
        `Conflicting German translations for ${source}: ${previous} / ${translated}`,
      );
      seen.set(source, translated);
    }
  }
});

test("all literal t calls have a German catalog entry", () => {
  const sourceRoot = path.resolve(import.meta.dirname, "..");
  const missing = new Set<string>();
  for (const file of sourceFiles(sourceRoot)) {
    const source = readFileSync(file, "utf8");
    for (const key of literalTranslationKeys(source, file)) {
      if (!Object.prototype.hasOwnProperty.call(germanTranslations, key)) {
        missing.add(key);
      }
    }
  }
  assert.deepEqual(Array.from(missing).sort(), []);
});

test("boot locale and runtime locale use the same storage key", () => {
  const bootScript = readFileSync(
    path.resolve(import.meta.dirname, "../../public/boot-locale.js"),
    "utf8",
  );
  const indexHtml = readFileSync(
    path.resolve(import.meta.dirname, "../../index.html"),
    "utf8",
  );
  assert.match(bootScript, new RegExp(`var key = ["']${LANGUAGE_STORAGE_KEY}["']`));
  assert.ok(indexHtml.indexOf("boot-shell") < indexHtml.indexOf("/boot-locale.js"));
  assert.ok(indexHtml.indexOf("/boot-locale.js") < indexHtml.indexOf("/src/main.tsx"));
  assert.doesNotMatch(bootScript, /DOMContentLoaded/);
  assert.equal(getLocaleTag("de"), "de-DE");
  assert.equal(getLocaleTag("en"), "en-US");
});

test("browser language preference respects the declared priority order", () => {
  assert.equal(localeFromLanguages(["en-US", "de-DE"]), "en");
  assert.equal(localeFromLanguages(["fr-FR", "de-DE", "en-US"]), "de");
  assert.equal(localeFromLanguages(["fr-FR"]), "en");
});

test("long-lived UI state keeps translation keys instead of rendered locale text", () => {
  const sourceRoot = path.resolve(import.meta.dirname, "..");
  const uploadStore = readFileSync(path.join(sourceRoot, "lib/file-upload-store.ts"), "utf8");
  const filePage = readFileSync(path.join(sourceRoot, "pages/FileTranscribe.tsx"), "utf8");
  const debugConsole = readFileSync(path.join(sourceRoot, "pages/DebugConsole.tsx"), "utf8");
  const trayPanel = readFileSync(path.join(sourceRoot, "components/TrayPanel.tsx"), "utf8");
  const desktopUpdates = readFileSync(path.join(sourceRoot, "lib/desktop-updates.ts"), "utf8");
  const settingsPage = readFileSync(path.join(sourceRoot, "pages/Settings.tsx"), "utf8");

  assert.doesNotMatch(uploadStore, /statusText:\s*translateNow\(/);
  assert.match(uploadStore, /statusValues/);
  assert.doesNotMatch(filePage, /setDropError\([^)]*t\(/);
  assert.match(filePage, /translateNow\("File uploaded"\)/);
  assert.match(filePage, /formatNumberNow\(result\.responses\.length\)/);
  assert.doesNotMatch(debugConsole, /setActionStatus\(t\(/);
  assert.doesNotMatch(debugConsole, /setError\(t\(/);
  assert.match(debugConsole, /renderLocalizedMessage\(actionStatus, t, formatNumber\)/);
  assert.match(debugConsole, /renderLocalizedMessage\(error, t, formatNumber\)/);
  assert.doesNotMatch(trayPanel, /setUpdateCheckMessage\(t\(/);
  assert.match(trayPanel, /t\(updateCheckMessage\.source, updateCheckMessage\.values\)/);
  assert.doesNotMatch(desktopUpdates, /translateNow\(/);
  assert.match(desktopUpdates, /message:\s*status\.message/);
  assert.match(settingsPage, /message:\s*"Update check failed\."/);
  assert.match(settingsPage, /message:\s*"Update installation failed\."/);
  assert.match(settingsPage, /description:\s*t\(status\.message, status\.messageValues\)/);
});
