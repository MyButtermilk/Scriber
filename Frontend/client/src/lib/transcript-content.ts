/**
 * Pure parsing of stored transcript text for display, search and export.
 *
 * Three stored shapes exist:
 * - Canonical artifacts rendered by the backend's `render_legacy_content`: one
 *   segment per line, `[m:ss] Speaker 0: text`, `[m:ss] Alice: text` or the
 *   timestamp-only `[m:ss] text`. Minutes are not wrapped at 60 (`[125:04]`);
 *   `[h:mm:ss]` is accepted as well.
 * - Legacy diarized paragraphs, `[Speaker N]: text`, separated by blank lines.
 * - Plain text, whose explicit line breaks are meaningful.
 */

export interface TranscriptBlock {
  /** Display timestamp of the block's first segment, such as `0:12` or `1:02:03`. */
  startLabel?: string;
  /** Start of the block's first segment in seconds, when it carries a timestamp. */
  startSeconds?: number;
  /** Speaker label exactly as stored, such as `Speaker 0` or `Alice`. */
  speaker?: string;
  /** True when the previous block belongs to the same speaker turn. */
  continued?: boolean;
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
/** Consecutive same-speaker segments are merged into paragraphs of about this size. */
const BLOCK_SOFT_CHARACTERS = 700;

interface TimedLine {
  startSeconds: number;
  startLabel: string;
  candidateSpeaker: string | null;
  candidateText: string;
  rest: string;
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
  let current: TranscriptBlock | null = null;
  // A blank line ends merging, so explicit paragraphs survive. Untimed lines
  // continue the current block on a new line; timed segments of one speaker
  // join into readable paragraphs that keep the turn's start time.
  let mergeable = false;

  lines.forEach((line, index) => {
    const entry = timed[index];
    if (!entry) {
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
    const sameTurn = current !== null && mergeable && current.startLabel !== undefined && current.speaker === speaker;
    if (current && sameTurn && current.text.length + text.length < BLOCK_SOFT_CHARACTERS) {
      if (text) current.text = current.text ? `${current.text} ${text}` : text;
    } else {
      const block: TranscriptBlock = { startLabel: entry.startLabel, startSeconds: entry.startSeconds, text };
      if (speaker !== undefined) block.speaker = speaker;
      if (sameTurn) block.continued = true;
      current = block;
      blocks.push(block);
    }
    mergeable = true;
  });
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
