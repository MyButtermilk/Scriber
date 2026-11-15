/**
 * Text normalization for the embedded PDF fonts.
 *
 * The documents embed Inter's Latin subset and fall back to the engine's
 * built-in Noto Sans for other scripts. Neither carries arrow glyphs, and the
 * fallback has no italic face, so both cases are normalized here instead of
 * printing replacement marks.
 */

const GLYPH_SUBSTITUTIONS: ReadonlyArray<readonly [RegExp, string]> = [
  [/[\u2192\u21d2\u27f6\u2794\u279c\u279e]/g, "\u203a"],
  [/[\u2190\u21d0\u27f5]/g, "\u2039"],
  [/[\u2194\u21d4]/g, "\u2039\u203a"],
  [/\u25aa|\u25cf|\u25e6|\u2043/g, "\u2022"],
];

// C0/C1 controls except tab and line feed, plus the Unicode line separators
// that some transcripts carry and PDF text operators cannot.
// eslint-disable-next-line no-control-regex -- stripping control characters is the purpose
const CONTROL_CHARACTERS = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b\u2028\u2029\ufeff]/g;

// Latin letters, punctuation and symbols covered by the embedded Inter subset.
// eslint-disable-next-line no-control-regex -- the range starts at U+0000 on purpose
const INTER_LATIN_TEXT = /^[\u0000-\u024f\u2000-\u206f\u20ac\u2122\u2212]*$/;

export function normalizePdfText(value: string): string {
  let text = String(value ?? "")
    .replace(/\r\n?/g, "\n")
    .replace(CONTROL_CHARACTERS, "");
  for (const [pattern, replacement] of GLYPH_SUBSTITUTIONS) {
    text = text.replace(pattern, replacement);
  }
  return text;
}

/** True when the italic face can set this text without missing glyphs. */
export function supportsItalic(value: string): boolean {
  return INTER_LATIN_TEXT.test(value);
}

/** Collapse a value into one bounded line for running headers and metadata. */
export function singleLinePdfText(value: string, maxLength: number): string {
  const line = normalizePdfText(value).replace(/\s+/g, " ").trim();
  if (line.length <= maxLength) return line;
  return `${line.slice(0, Math.max(1, maxLength - 1)).trimEnd()}\u2026`;
}
