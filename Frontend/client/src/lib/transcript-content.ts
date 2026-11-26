/**
 * Pure parsing and deterministic, zero-AI structuring of stored transcript text
 * for display, search, and export.
 *
 * Three stored shapes exist:
 * - Canonical artifacts rendered by the backend's `render_legacy_content`: one
 *   segment per line, `[m:ss] Speaker 0: text`, `[m:ss] Alice: text` or the
 *   timestamp-only `[m:ss] text`. Minutes are not wrapped at 60 (`[125:04]`);
 *   `[h:mm:ss]` is accepted as well.
 * - Legacy diarized paragraphs, `[Speaker N]: text`, separated by blank lines.
 * - Plain text, whose explicit line breaks are meaningful.
 *
 * For canonical timed turns, consecutive segments of the same speaker are
 * reflowed into complete sentences (so mid-sentence STT chunk boundaries never
 * split a sentence across blocks) and partitioned into readable logical
 * paragraphs using four zero-cost signals:
 *   1. Sentence-boundary preservation (DE/EN abbreviations, dates, mid-sentence ellipses)
 *   2. Multilingual DE/EN discourse & topic-transition cues
 *   3. Lightweight TextTiling lexical-cohesion valleys (stopword-filtered stems)
 *   4. Acoustic/timestamp pause gaps, Q&A beats, and comfortable reading rhythm
 */

export interface TranscriptBlock {
  /** Display timestamp of the block's first sentence, such as `0:12` or `1:02:03`. */
  startLabel?: string;
  /** Start of the block's first sentence in seconds, when it carries a timestamp. */
  startSeconds?: number;
  /** Speaker label exactly as stored, such as `Speaker 0` or `Alice`. */
  speaker?: string;
  /** True when the previous block belongs to the same speaker turn. */
  continued?: boolean;
  /** True when strong semantic/temporal cues indicate a new thematic section. */
  sectionBreak?: boolean;
  text: string;
}

export interface TranscriptMatch {
  blockIndex: number;
  /** UTF-16 offsets into `blocks[blockIndex].text`; `end` is exclusive. */
  start: number;
  end: number;
}

const TIMESTAMP_LINE = /^\[(\d{1,4}):(\d{2})(?::(\d{2}))?\]\s?(.*)$/;
const LEGACY_SPEAKER_LINE = /^\[Speaker (\d+)\]:\s*(.*)$/;
const GENERIC_SPEAKER = /^Speaker (\d+)$/;
const SPEAKER_PREFIX = /^([^:\n]{1,48}?):\s+(\S[\s\S]*)$/;

/** Comfortable editorial paragraph envelope (characters). */
const MIN_PARAGRAPH_CHARS = 150;
const SOFT_PARAGRAPH_CHARS = 260;
const MAX_PARAGRAPH_CHARS = 500;
const ORPHAN_TAIL_MAX_CHARS = 95;
const ORPHAN_MERGE_CEILING_CHARS = 580;
const UNPUNCTUATED_RUNON_LIMIT = 520;
const UNPUNCTUATED_TARGET_CHARS = 320;
const MIN_CHARS_BETWEEN_SECTIONS = 450;

/** Common German and English abbreviations that end with a period without ending a sentence. */
const ABBREVIATIONS = new Set([
  "dr",
  "mr",
  "mrs",
  "ms",
  "prof",
  "st",
  "vs",
  "etc",
  "eg",
  "ie",
  "no",
  "nr",
  "ca",
  "bzw",
  "usw",
  "evtl",
  "inkl",
  "exkl",
  "zzgl",
  "ggf",
  "abs",
  "art",
  "tel",
  "str",
  "mio",
  "mrd",
  "jan",
  "feb",
  "mar",
  "mär",
  "apr",
  "jun",
  "jul",
  "aug",
  "sep",
  "sept",
  "okt",
  "oct",
  "nov",
  "dez",
  "dec",
  "mon",
  "tue",
  "wed",
  "thu",
  "fri",
  "sat",
  "sun",
  "inc",
  "ltd",
  "corp",
  "jr",
  "sr",
  "fig",
  "vol",
  "approx",
  "min",
  "max",
  "sec",
  "hrs",
  "est",
  "dept",
  "gov",
  "sen",
  "rep",
  "gen",
  "col",
  "lt",
  "sgt",
  "capt",
  "co",
  "p",
  "pp",
  "s",
  "z",
  "b",
  "d",
  "h",
  "u",
  "a",
  "v",
  "o",
  "ä",
]);

/** German & English high-frequency stopwords ignored by TextTiling lexical cohesion. */
const STOPWORDS = new Set([
  // English
  "that",
  "this",
  "with",
  "have",
  "from",
  "they",
  "them",
  "their",
  "there",
  "about",
  "which",
  "would",
  "could",
  "should",
  "what",
  "when",
  "where",
  "will",
  "just",
  "like",
  "been",
  "being",
  "were",
  "your",
  "more",
  "some",
  "very",
  "into",
  "than",
  "then",
  "also",
  "only",
  "even",
  "most",
  "much",
  "many",
  "such",
  "here",
  "going",
  "really",
  "actually",
  "basically",
  "kind",
  "sort",
  "yeah",
  "okay",
  "right",
  "well",
  "know",
  "think",
  "want",
  "make",
  "made",
  "doing",
  "done",
  "does",
  "didnt",
  "dont",
  "cant",
  "wont",
  "isnt",
  "arent",
  "wasnt",
  "thats",
  "theres",
  "heres",
  "theyre",
  "youre",
  "weve",
  "ive",
  "thing",
  "things",
  "something",
  "everything",
  "nothing",
  "anything",
  "other",
  "another",
  "because",
  "before",
  "after",
  "while",
  "over",
  "under",
  "again",
  "still",
  "always",
  "never",
  "every",
  "each",
  "through",
  "around",
  // German
  "dass",
  "eine",
  "einen",
  "einem",
  "einer",
  "eines",
  "nicht",
  "auch",
  "noch",
  "schon",
  "aber",
  "oder",
  "wenn",
  "dann",
  "denn",
  "doch",
  "hier",
  "dort",
  "wird",
  "sind",
  "sein",
  "seine",
  "seinen",
  "seiner",
  "haben",
  "hatte",
  "hatten",
  "kann",
  "konnte",
  "muss",
  "mussen",
  "musste",
  "soll",
  "sollte",
  "wird",
  "werden",
  "wurde",
  "wurden",
  "ware",
  "waren",
  "weil",
  "damit",
  "uber",
  "unter",
  "durch",
  "nach",
  "beim",
  "beide",
  "dies",
  "diese",
  "dieser",
  "dieses",
  "diesen",
  "diesem",
  "mein",
  "meine",
  "meinen",
  "dein",
  "deine",
  "ihre",
  "ihren",
  "unser",
  "unsere",
  "euch",
  "sich",
  "mich",
  "dich",
  "alles",
  "alle",
  "allen",
  "etwas",
  "nichts",
  "immer",
  "wieder",
  "sehr",
  "mehr",
  "viel",
  "viele",
  "ganz",
  "halt",
  "eben",
  "also",
  "einfach",
  "wirklich",
  "eigentlich",
  "genau",
  "sagen",
  "gesagt",
  "machen",
  "gemacht",
  "geht",
  "gibt",
  "jetzt",
  "heute",
  "dazu",
  "dabei",
  "davon",
  "dafür",
  "dafur",
  "darauf",
  "darin",
  "daran",
  "damit",
  "naturlich",
  "vielleicht",
  "irgendwie",
]);

/** Strong DE/EN discourse transition cues that naturally open a new paragraph or section. */
const STRONG_TRANSITION_PATTERN =
  /^(?:["“„«'(\s]*)(?:now,\s+(?:here(?:'|’)s|it(?:'|’)s|what(?:'|’)s|the\b|let(?:'|’)s|if\b|when\b|we\b|i\b)|now\s+(?:what(?:'|’)s|here(?:'|’)s|let(?:'|’)s)\b|here(?:'|’)s\s+the\s+thing\b|the\s+thing\s+is\b|first\s+of\s+all\b|first,|second,|secondly\b|third,|thirdly\b|next,|finally,|lastly,|moving\s+on\b|speaking\s+of\b|by\s+the\s+way\b|on\s+the\s+other\s+hand\b|in\s+contrast\b|in\s+addition\b|furthermore\b|moreover\b|meanwhile\b|anyway,|anyways,|all\s+right,\s+so\b|okay,\s+so\b|ok,\s+so\b|so\s+here(?:'|’)s\b|let(?:'|’)s\s+(?:talk\s+about|look\s+at|move\s+on|jump\s+into|switch\s+to)\b|to\s+sum\s+up\b|to\s+summarize\b|in\s+summary\b|in\s+conclusion\b|the\s+bottom\s+line\b|another\s+thing\b|one\s+more\s+thing\b|number\s+(?:one|two|three|four|five)\b|step\s+(?:one|two|three|four|five)\b|point\s+(?:one|two|three)\b|erstens\b|zweitens\b|drittens\b|viertens\b|fünftens\b|als\s+nächstes\b|kommen\s+wir\s+zu\b|schauen\s+wir\s+uns\b|übrigens\b|andererseits\b|im\s+gegensatz\b|darüber\s+hinaus\b|außerdem\b|gleichzeitig\b|währenddessen\b|zusammenfassend\b|kurz\s+gesagt\b|unterm\s+strich\b|zum\s+schluss\b|abschließend\b|ein\s+weiterer\s+punkt\b|der\s+nächste\s+punkt\b|punkt\s+(?:eins|zwei|drei|vier|fünf)\b|(?:aber\s+)?das\s+wichtigste\b|was\s+noch\s+wichtig\s+ist\b|jetzt\s+kommt\b|nun,|okay,\s+also\b|gut,\s+dann\b|so,\s+jetzt\b|fangen\s+wir\s+an\b|lass(?:en\s+sie)?\s+uns\s+(?:über|schauen|anschauen)\b)/i;

/** Moderate DE/EN contrast/pivot cues that reinforce a paragraph break once length builds. */
const MODERATE_TRANSITION_PATTERN =
  /^(?:["“„«'(\s]*)(?:however,|still,|instead,|actually,|honestly,|frankly,|basically,|fortunately,|unfortunately,|importantly,|so,\s+(?:yeah|basically|anyway|what|the|we|i|if)\b|now,|allerdings\b|jedoch\b|trotzdem\b|dennoch\b|stattdessen\b|tatsächlich\b|ehrlich\s+gesagt\b|grundsätzlich\b|eigentlich\b|glücklicherweise\b|leider\b|wichtig\s+ist\b|also,\s+)/i;

interface TimedLine {
  startSeconds: number;
  startLabel: string;
  candidateSpeaker: string | null;
  candidateText: string;
  rest: string;
}

interface SegmentSpan {
  index: number;
  offset: number;
  startSeconds: number;
  startLabel: string;
  pauseBeforeSec: number;
}

interface SentenceUnit {
  text: string;
  segmentIndex: number;
  startSeconds: number;
  startLabel: string;
  contentStems: Set<string>;
}

function formatStartLabel(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const ss = String(seconds).padStart(2, "0");
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${ss}`;
  return `${minutes}:${ss}`;
}

/** A label that plausibly names a speaker rather than starting a sentence. */
function plausibleSpeakerLabel(label: string): boolean {
  const value = label.trim();
  if (!value || value !== label) return false;
  if (GENERIC_SPEAKER.test(value)) return true;
  if (/[.!?,;"“”„()[\]{}]/.test(value)) return false;
  if (/^\d+$/.test(value)) return false;
  return value.split(/\s+/).length <= 4;
}

function parseTimedLine(line: string): TimedLine | null {
  const match = TIMESTAMP_LINE.exec(line);
  if (!match) return null;
  const [, first, second, third, rest] = match;
  let startSeconds: number;
  if (third !== undefined) {
    if (Number(second) > 59 || Number(third) > 59) return null;
    startSeconds = Number(first) * 3600 + Number(second) * 60 + Number(third);
  } else {
    if (Number(second) > 59) return null;
    startSeconds = Number(first) * 60 + Number(second);
  }
  const speakerMatch = SPEAKER_PREFIX.exec(rest);
  const candidate = speakerMatch && plausibleSpeakerLabel(speakerMatch[1]) ? speakerMatch : null;
  return {
    startSeconds,
    startLabel: formatStartLabel(startSeconds),
    candidateSpeaker: candidate ? candidate[1] : null,
    candidateText: candidate ? candidate[2].trim() : "",
    rest: rest.trim(),
  };
}

function extractContentStems(text: string): Set<string> {
  const stems = new Set<string>();
  const folded = foldSearchText(text);
  const tokens = folded.match(/[a-z0-9]{4,}/g);
  if (!tokens) return stems;
  for (const token of tokens) {
    if (/^\d+$/.test(token) || STOPWORDS.has(token)) continue;
    stems.add(token.length >= 6 ? token.slice(0, 6) : token);
  }
  return stems;
}

function jaccardSimilarity(left: Set<string>, right: Set<string>): number {
  if (left.size === 0 || right.size === 0) return 0.5;
  let intersection = 0;
  for (const stem of left) {
    if (right.has(stem)) intersection += 1;
  }
  const union = left.size + right.size - intersection;
  return union > 0 ? intersection / union : 0.5;
}

function isSentenceStartChar(character: string): boolean {
  return /[A-ZÄÖÜ0-9"“„«'(—–-]/.test(character);
}

function isWordBeforePeriodAbbreviation(text: string, periodIndex: number): boolean {
  let start = periodIndex - 1;
  while (start >= 0 && /[A-Za-zÄÖÜäöüß0-9.]/.test(text[start])) {
    start -= 1;
  }
  const token = text.slice(start + 1, periodIndex);
  if (!token) return true;
  // Numbers like "1.", "26." (German ordinals/dates) or decimals ("3.5")
  if (/^\d+(?:\.\d+)*$/.test(token)) {
    // Only treat bare digits as non-sentence-ending when short (e.g. "26. November", "1. Punkt")
    // unless followed by a clear sentence-starting pronoun/word; keep dates/ordinals intact.
    return token.length <= 2 || token.includes(".");
  }
  // Multi-dot abbreviations such as "e.g." / "i.e." / "z.B." / "U.S."
  if (token.includes(".")) {
    const parts = token.split(".").filter(Boolean);
    if (parts.length > 0 && parts.every((part) => part.length <= 3)) return true;
  }
  const lower = token.toLowerCase();
  if (lower.length === 1) return true;
  return ABBREVIATIONS.has(lower);
}

/**
 * Split a long unpunctuated run-on span cleanly at clause/conjunction
 * boundaries without ever splitting mid-word.
 */
function splitLongRunOn(text: string, baseOffset: number): Array<{ text: string; offset: number }> {
  if (text.length <= UNPUNCTUATED_RUNON_LIMIT) {
    return [{ text, offset: baseOffset }];
  }
  const pieces: Array<{ text: string; offset: number }> = [];
  let cursor = 0;
  while (text.length - cursor > UNPUNCTUATED_RUNON_LIMIT) {
    const windowStart = cursor + 180;
    const windowEnd = Math.min(text.length - 120, cursor + UNPUNCTUATED_RUNON_LIMIT);
    const ideal = cursor + UNPUNCTUATED_TARGET_CHARS;
    let bestSplit = -1;
    let bestDistance = Number.POSITIVE_INFINITY;

    // Prefer comma/semicolon/dash boundaries first.
    for (let idx = windowStart; idx < windowEnd; idx += 1) {
      const ch = text[idx];
      if ((ch === "," || ch === ";" || ch === "—" || ch === "–") && text[idx + 1] === " ") {
        const splitAt = idx + 1;
        const dist = Math.abs(splitAt - ideal);
        if (dist < bestDistance) {
          bestDistance = dist;
          bestSplit = splitAt;
        }
      }
    }

    // Fallback: coordinating conjunction boundary (DE/EN) or plain space.
    if (bestSplit < 0) {
      const slice = text.slice(windowStart, windowEnd);
      const conjunction = /\s+(?:and|but|so|because|while|und|aber|oder|weil|also|dann)\s+/gi;
      let match: RegExpExecArray | null;
      while ((match = conjunction.exec(slice)) !== null) {
        const splitAt = windowStart + match.index;
        const dist = Math.abs(splitAt - ideal);
        if (dist < bestDistance) {
          bestDistance = dist;
          bestSplit = splitAt;
        }
      }
    }
    if (bestSplit < 0) {
      const space = text.lastIndexOf(" ", ideal + 60);
      bestSplit = space > windowStart ? space : text.indexOf(" ", ideal);
      if (bestSplit < 0 || bestSplit >= text.length - 1) break;
    }

    const head = text.slice(cursor, bestSplit).trim();
    if (head) pieces.push({ text: head, offset: baseOffset + cursor });
    cursor = bestSplit;
    while (cursor < text.length && text[cursor] === " ") cursor += 1;
  }
  const tail = text.slice(cursor).trim();
  if (tail) pieces.push({ text: tail, offset: baseOffset + cursor });
  return pieces;
}

/**
 * Split a continuous same-speaker text stream into complete sentence spans
 * while preserving exact character offsets back to timed segments.
 */
function splitIntoSentenceSpans(text: string): Array<{ text: string; offset: number }> {
  const rawSpans: Array<{ text: string; offset: number }> = [];
  let start = 0;
  let index = 0;

  while (index < text.length) {
    const ch = text[index];
    if (ch !== "." && ch !== "!" && ch !== "?" && ch !== "…") {
      index += 1;
      continue;
    }

    // Consume ellipsis ("..." or "…") or repeated punctuation ("?!", "!!").
    let punctEnd = index;
    const isEllipsis = ch === "…" || (ch === "." && text[index + 1] === "." && text[index + 2] === ".");
    if (ch === "." && isEllipsis) {
      while (punctEnd + 1 < text.length && text[punctEnd + 1] === ".") punctEnd += 1;
    } else if (ch === "!" || ch === "?") {
      while (punctEnd + 1 < text.length && (text[punctEnd + 1] === "!" || text[punctEnd + 1] === "?")) {
        punctEnd += 1;
      }
    }

    // Skip closing quotes/brackets attached to the sentence ending.
    let closeEnd = punctEnd;
    while (closeEnd + 1 < text.length && /["'”’»)\]]/.test(text[closeEnd + 1])) {
      closeEnd += 1;
    }

    const nextIndex = closeEnd + 1;
    if (nextIndex >= text.length) {
      break;
    }

    // Must have whitespace after the sentence boundary.
    if (!/\s/.test(text[nextIndex])) {
      index = nextIndex;
      continue;
    }

    let nextWordStart = nextIndex;
    while (nextWordStart < text.length && /\s/.test(text[nextWordStart])) {
      nextWordStart += 1;
    }
    if (nextWordStart >= text.length) {
      break;
    }

    const nextChar = text[nextWordStart];
    // Mid-sentence ellipsis followed by lowercase ("is... pretty insane") stays inside the sentence.
    if (isEllipsis && !/[A-ZÄÖÜ"“„«'(]/.test(nextChar)) {
      index = nextWordStart;
      continue;
    }
    if (!isSentenceStartChar(nextChar)) {
      index = nextWordStart;
      continue;
    }
    if (ch === "." && !isEllipsis && isWordBeforePeriodAbbreviation(text, index)) {
      index = nextWordStart;
      continue;
    }

    const sentence = text.slice(start, closeEnd + 1).trim();
    if (sentence) {
      rawSpans.push({ text: sentence, offset: start });
    }
    start = nextWordStart;
    index = nextWordStart;
  }

  const remainder = text.slice(start).trim();
  if (remainder) {
    rawSpans.push({ text: remainder, offset: start });
  }

  const refined: Array<{ text: string; offset: number }> = [];
  for (const span of rawSpans) {
    refined.push(...splitLongRunOn(span.text, span.offset));
  }
  return refined;
}

function lookupSegmentForOffset(spans: readonly SegmentSpan[], offset: number): SegmentSpan {
  let low = 0;
  let high = spans.length - 1;
  let best = 0;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (spans[mid].offset <= offset) {
      best = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return spans[best];
}

function unionStems(sentences: readonly SentenceUnit[], from: number, to: number): Set<string> {
  const combined = new Set<string>();
  const start = Math.max(0, from);
  const end = Math.min(sentences.length, to);
  for (let index = start; index < end; index += 1) {
    for (const stem of sentences[index].contentStems) {
      combined.add(stem);
    }
  }
  return combined;
}

function segmentRunIntoBlocks(
  segments: readonly { startSeconds: number; startLabel: string; text: string }[],
  speaker: string | undefined,
  continuedTurn: boolean,
): TranscriptBlock[] {
  const nonEmpty = segments.filter((segment) => segment.text.length > 0);
  if (nonEmpty.length === 0) {
    const first = segments[0];
    if (!first) return [];
    const block: TranscriptBlock = { startLabel: first.startLabel, startSeconds: first.startSeconds, text: "" };
    if (speaker !== undefined) block.speaker = speaker;
    if (continuedTurn) block.continued = true;
    return [block];
  }

  const segmentSpans: SegmentSpan[] = [];
  let joined = "";
  for (let segIdx = 0; segIdx < nonEmpty.length; segIdx += 1) {
    const segment = nonEmpty[segIdx];
    const prevSegment = segIdx > 0 ? nonEmpty[segIdx - 1] : null;
    const expectedPrevSec = prevSegment ? Math.max(2, prevSegment.text.length / 13) : 0;
    const pauseBeforeSec = prevSegment
      ? Math.max(0, segment.startSeconds - prevSegment.startSeconds - expectedPrevSec)
      : 0;
    if (joined) joined += " ";
    segmentSpans.push({
      index: segIdx,
      offset: joined.length,
      startSeconds: segment.startSeconds,
      startLabel: segment.startLabel,
      pauseBeforeSec,
    });
    joined += segment.text;
  }

  const rawSentences = splitIntoSentenceSpans(joined);
  const sentences: SentenceUnit[] = rawSentences.map((span) => {
    const timing = lookupSegmentForOffset(segmentSpans, span.offset);
    return {
      text: span.text,
      segmentIndex: timing.index,
      startSeconds: timing.startSeconds,
      startLabel: timing.startLabel,
      contentStems: extractContentStems(span.text),
    };
  });

  const pauseBetween = (prev: SentenceUnit, next: SentenceUnit): number => {
    if (next.segmentIndex <= prev.segmentIndex) return 0;
    let maxGap = 0;
    for (let segIdx = prev.segmentIndex + 1; segIdx <= next.segmentIndex; segIdx += 1) {
      maxGap = Math.max(maxGap, segmentSpans[segIdx].pauseBeforeSec);
    }
    return maxGap;
  };

  // Short turns stay as a single clean block unless there is a very large time jump.
  const maxPauseSec = segmentSpans.reduce((maxGap, span) => Math.max(maxGap, span.pauseBeforeSec), 0);

  if (
    sentences.length <= 1 ||
    (joined.length <= 320 && maxPauseSec < 35) ||
    (sentences.length <= 2 && joined.length <= 420 && maxPauseSec < 25)
  ) {
    const block: TranscriptBlock = {
      startLabel: sentences[0].startLabel,
      startSeconds: sentences[0].startSeconds,
      text: joined,
    };
    if (speaker !== undefined) block.speaker = speaker;
    if (continuedTurn) block.continued = true;
    return [block];
  }

  // Precompute TextTiling lexical similarity across adjacent 2-sentence windows.
  const similarities: number[] = new Array(sentences.length).fill(0.5);
  for (let i = 1; i < sentences.length; i += 1) {
    const left = unionStems(sentences, i - 2, i);
    const right = unionStems(sentences, i, i + 2);
    similarities[i] = jaccardSimilarity(left, right);
  }

  interface DraftParagraph {
    sentences: SentenceUnit[];
    chars: number;
    sectionBreak: boolean;
  }

  const paragraphs: DraftParagraph[] = [];
  let currentSentences: SentenceUnit[] = [sentences[0]];
  let currentChars = sentences[0].text.length;
  let pendingSectionBreak = false;
  let charsSinceSection = 0;

  for (let i = 1; i < sentences.length; i += 1) {
    const prev = sentences[i - 1];
    const next = sentences[i];

    // 1. Discourse transition signal (DE/EN).
    let semanticScore = 0;
    let hasStrongTransition = false;
    if (STRONG_TRANSITION_PATTERN.test(next.text)) {
      semanticScore += 3.4;
      hasStrongTransition = true;
    } else if (MODERATE_TRANSITION_PATTERN.test(next.text)) {
      semanticScore += 1.5;
    }

    // 2. Mini-TextTiling lexical cohesion valley.
    const sim = similarities[i];
    const leftSim = i > 1 ? similarities[i - 1] : sim;
    const rightSim = i + 1 < sentences.length ? similarities[i + 1] : sim;
    const valleyDepth = Math.max(0, leftSim - sim) + Math.max(0, rightSim - sim);
    const leftStems = unionStems(sentences, i - 2, i);
    const rightStems = unionStems(sentences, i, i + 2);
    if (leftStems.size >= 4 && rightStems.size >= 4) {
      if (sim === 0) {
        if (valleyDepth >= 0.12) {
          semanticScore += 3.2;
        } else if (leftStems.size >= 7 && rightStems.size >= 7 && currentChars >= 220) {
          semanticScore += 2.7;
        } else {
          semanticScore += 1.9;
        }
      } else if (sim <= 0.08 || valleyDepth >= 0.22) {
        semanticScore += 1.5;
      }
    }

    // 3. Question / rhetorical beat signal.
    const prevQuestion = prev.text.trimEnd().endsWith("?");
    const nextQuestion = next.text.trimEnd().endsWith("?");
    if (prevQuestion && !nextQuestion) {
      semanticScore += 1.8;
    } else if (!prevQuestion && nextQuestion && currentChars >= 180) {
      semanticScore += 1.2;
    }

    // 4. Temporal / acoustic pause signal beyond normal segment speaking cadence.
    const excessPause = pauseBetween(prev, next);
    if (excessPause >= 18) {
      semanticScore += 3.6;
    } else if (excessPause >= 7) {
      semanticScore += 1.8;
    } else if (excessPause >= 2.5) {
      semanticScore += 0.6;
    }

    // 5. Progressive length pressure so paragraphs stay airy (~240-460 chars).
    const lengthPressure = currentChars >= SOFT_PARAGRAPH_CHARS ? (currentChars - SOFT_PARAGRAPH_CHARS) / 85 : 0;

    const strongPauseBreak = excessPause >= 22 && currentChars >= 80;
    const minCharsForBreak = hasStrongTransition ? 110 : MIN_PARAGRAPH_CHARS;
    const logicalBreak = currentChars >= minCharsForBreak && semanticScore + lengthPressure >= 3.2;
    const ceilingBreak = currentChars >= MAX_PARAGRAPH_CHARS;

    if (strongPauseBreak || logicalBreak || ceilingBreak) {
      paragraphs.push({
        sentences: currentSentences,
        chars: currentChars,
        sectionBreak: pendingSectionBreak,
      });
      charsSinceSection += currentChars;
      const isMajorShift =
        charsSinceSection >= MIN_CHARS_BETWEEN_SECTIONS &&
        (semanticScore >= 4.2 || (hasStrongTransition && sim <= 0.08) || excessPause >= 18);
      pendingSectionBreak = isMajorShift;
      if (isMajorShift) charsSinceSection = 0;

      currentSentences = [next];
      currentChars = next.text.length;
    } else {
      currentSentences.push(next);
      currentChars += 1 + next.text.length;
    }
  }

  if (currentSentences.length > 0) {
    paragraphs.push({
      sentences: currentSentences,
      chars: currentChars,
      sectionBreak: pendingSectionBreak,
    });
  }

  // Fold a tiny trailing orphan sentence back into the previous paragraph if it fits comfortably.
  if (paragraphs.length >= 2) {
    const last = paragraphs[paragraphs.length - 1];
    const beforeLast = paragraphs[paragraphs.length - 2];
    if (
      last.chars <= ORPHAN_TAIL_MAX_CHARS &&
      !last.sectionBreak &&
      beforeLast.chars + 1 + last.chars <= ORPHAN_MERGE_CEILING_CHARS
    ) {
      beforeLast.sentences.push(...last.sentences);
      beforeLast.chars += 1 + last.chars;
      paragraphs.pop();
    }
  }

  return paragraphs.map((paragraph, index) => {
    const firstSentence = paragraph.sentences[0];
    const block: TranscriptBlock = {
      startLabel: firstSentence.startLabel,
      startSeconds: firstSentence.startSeconds,
      text: paragraph.sentences.map((sentence) => sentence.text).join(" "),
    };
    if (speaker !== undefined) block.speaker = speaker;
    if (continuedTurn || index > 0) block.continued = true;
    if (paragraph.sectionBreak && index > 0) block.sectionBreak = true;
    return block;
  });
}

function parseCanonicalLines(lines: string[], timed: Array<TimedLine | null>): TranscriptBlock[] {
  // Named labels are accepted when the whole transcript is diarized or the
  // name recurs. A lone "Here is the thing: ..." in an undiarized transcript
  // therefore stays ordinary text. Generic "Speaker N" labels always count.
  const timedLines = timed.filter((entry): entry is TimedLine => entry !== null);
  const everyLineLabelled = timedLines.every((entry) => entry.candidateSpeaker !== null);
  const labelCounts = new Map<string, number>();
  for (const entry of timedLines) {
    if (entry.candidateSpeaker) {
      labelCounts.set(entry.candidateSpeaker, (labelCounts.get(entry.candidateSpeaker) ?? 0) + 1);
    }
  }
  const acceptsLabel = (label: string) =>
    GENERIC_SPEAKER.test(label) || everyLineLabelled || (labelCounts.get(label) ?? 0) >= 2;

  const blocks: TranscriptBlock[] = [];
  let pendingRun: {
    speaker: string | undefined;
    continuedTurn: boolean;
    segments: Array<{ startSeconds: number; startLabel: string; text: string }>;
  } | null = null;
  let current: TranscriptBlock | null = null;
  // A blank line ends merging, so explicit paragraphs survive. Untimed lines
  // continue the current block on a new line; timed segments of one speaker
  // reflow into complete sentences and logical paragraphs.
  let mergeable = false;

  const flushPendingRun = () => {
    if (!pendingRun) return;
    const created = segmentRunIntoBlocks(pendingRun.segments, pendingRun.speaker, pendingRun.continuedTurn);
    for (const block of created) {
      blocks.push(block);
      current = block;
    }
    pendingRun = null;
  };

  lines.forEach((line, index) => {
    const entry = timed[index];
    if (!entry) {
      flushPendingRun();
      if (!line.trim()) {
        mergeable = false;
        return;
      }
      if (current && mergeable) {
        current.text += `\n${line.trimEnd()}`;
      } else {
        current = { text: line.trimEnd() };
        blocks.push(current);
      }
      mergeable = true;
      return;
    }

    const speaker = entry.candidateSpeaker && acceptsLabel(entry.candidateSpeaker) ? entry.candidateSpeaker : undefined;
    const text = speaker ? entry.candidateText : entry.rest;
    const samePendingRun = pendingRun !== null && mergeable && pendingRun.speaker === speaker;

    if (samePendingRun && pendingRun) {
      pendingRun.segments.push({
        startSeconds: entry.startSeconds,
        startLabel: entry.startLabel,
        text,
      });
    } else {
      flushPendingRun();
      const continuedTurn =
        current !== null && mergeable && current.startLabel !== undefined && current.speaker === speaker;
      pendingRun = {
        speaker,
        continuedTurn,
        segments: [
          {
            startSeconds: entry.startSeconds,
            startLabel: entry.startLabel,
            text,
          },
        ],
      };
    }
    mergeable = true;
  });

  flushPendingRun();
  return blocks.filter((block) => block.text || block.speaker || block.startLabel);
}

function parseParagraphs(lines: string[]): TranscriptBlock[] {
  const blocks: TranscriptBlock[] = [];
  let current: TranscriptBlock | null = null;
  let previousSpeaker: string | undefined;
  for (const line of lines) {
    if (!line.trim()) {
      current = null;
      continue;
    }
    const legacy = LEGACY_SPEAKER_LINE.exec(line);
    if (legacy) {
      const speaker = `Speaker ${Number(legacy[1])}`;
      current = { speaker, text: legacy[2].trim() };
      if (previousSpeaker === speaker) current.continued = true;
      previousSpeaker = speaker;
      blocks.push(current);
      continue;
    }
    if (current) {
      current.text = current.text ? `${current.text}\n${line.trimEnd()}` : line.trimEnd();
    } else {
      current = { text: line.trimEnd() };
      previousSpeaker = undefined;
      blocks.push(current);
    }
  }
  return blocks.filter((block) => block.text || block.speaker);
}

/** Parse stored transcript text into display blocks without losing words or line breaks. */
export function parseTranscriptContent(text: string): TranscriptBlock[] {
  const normalized = String(text ?? "").replace(/\r\n?/g, "\n");
  if (!normalized.trim()) return [];
  const lines = normalized.split("\n");
  const timed = lines.map(parseTimedLine);
  if (timed.some((entry) => entry !== null)) return parseCanonicalLines(lines, timed);
  return parseParagraphs(lines);
}

/** The number in a generic `Speaker N` label, or null for a named speaker. */
export function genericSpeakerNumber(speaker: string | undefined | null): number | null {
  const match = GENERIC_SPEAKER.exec(String(speaker ?? "").trim());
  return match ? Number(match[1]) : null;
}

function stableHash(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  return hash;
}

/**
 * Color slot for a speaker. Generic labels use their number, named speakers a
 * stable hash; both are reduced with a non-negative modulo so `Speaker 0` and
 * negative hashes always select a real palette entry.
 */
export function speakerColorIndex(speaker: string | number | undefined | null, count: number): number {
  if (!Number.isFinite(count) || count <= 0) return 0;
  const size = Math.floor(count);
  let key: number;
  if (typeof speaker === "number") {
    key = Number.isFinite(speaker) ? Math.trunc(speaker) : 0;
  } else {
    const generic = genericSpeakerNumber(speaker);
    key =
      generic ??
      stableHash(
        String(speaker ?? "")
          .trim()
          .toLocaleLowerCase(),
      );
  }
  return ((key % size) + size) % size;
}

/** Case- and accent-insensitive form of one character (`ü` → `u`, `ß` → `ss`). */
function foldCharacter(character: string): string {
  return character.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/ß/g, "ss");
}

export function foldSearchText(value: string): string {
  let result = "";
  for (const character of value) result += foldCharacter(character);
  return result;
}

export interface FoldedText {
  folded: string;
  /** Original start offset for each folded UTF-16 unit. */
  starts: number[];
  /** Original exclusive end offset for each folded UTF-16 unit. */
  ends: number[];
}

function foldWithOffsets(value: string): FoldedText {
  let folded = "";
  const starts: number[] = [];
  const ends: number[] = [];
  let offset = 0;
  for (const character of value) {
    const end = offset + character.length;
    const piece = foldCharacter(character);
    if (piece) {
      folded += piece;
      for (let index = 0; index < piece.length; index += 1) {
        starts.push(offset);
        ends.push(end);
      }
    } else if (ends.length) {
      // A combining mark belongs to the previous visible character.
      ends[ends.length - 1] = end;
    }
    offset = end;
  }
  return { folded, starts, ends };
}

/** Fold each block once so repeated query keystrokes only scan folded text. */
export function prepareTranscriptSearchIndex(blocks: readonly TranscriptBlock[]): readonly FoldedText[] {
  return blocks.map((block) => foldWithOffsets(block.text));
}

/** Non-overlapping matches of `query` in reading order against a prepared search index. */
export function findPreparedTranscriptMatches(index: readonly FoldedText[], query: string): TranscriptMatch[] {
  const needle = foldSearchText(String(query ?? "").trim());
  if (!needle) return [];
  const matches: TranscriptMatch[] = [];
  index.forEach(({ folded, starts, ends }, blockIndex) => {
    let from = 0;
    let lastEnd = 0;
    while (from <= folded.length - needle.length) {
      const position = folded.indexOf(needle, from);
      if (position < 0) break;
      const start = starts[position];
      const end = ends[position + needle.length - 1];
      // One original character can fold to several units (ß → ss).
      if (start >= lastEnd) {
        matches.push({ blockIndex, start, end });
        lastEnd = end;
      }
      from = position + needle.length;
    }
  });
  return matches;
}

/** Non-overlapping matches of `query` in reading order across all blocks. */
export function findTranscriptMatches(blocks: readonly TranscriptBlock[], query: string): TranscriptMatch[] {
  return findPreparedTranscriptMatches(prepareTranscriptSearchIndex(blocks), query);
}

/** Wrap-around step through `total` matches. */
export function cycleMatchIndex(current: number, total: number, direction: 1 | -1): number {
  if (total <= 0) return -1;
  if (current < 0 || current >= total) return direction === 1 ? 0 : total - 1;
  return (((current + direction) % total) + total) % total;
}
