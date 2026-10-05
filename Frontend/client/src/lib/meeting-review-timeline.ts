export interface ReviewTimelineSegment {
  id: string;
  revision: "live" | "canonical";
  speakerId: string | null;
  label: string;
  startMs: number;
  endMs: number;
  alignmentQuality: "exact_word" | "provider_segment" | "estimated";
  text: string;
}

export interface ReviewSearch {
  query: string;
  speakerId?: string | null;
  fromMs?: number | null;
  toMs?: number | null;
}

export type ReviewTimeRange = "all" | "first-15" | "15-30" | "30-60" | "after-60";

const alignmentPriority: Record<ReviewTimelineSegment["alignmentQuality"], number> = {
  exact_word: 3,
  provider_segment: 2,
  estimated: 1,
};

export function activeReviewSegmentId(segments: readonly ReviewTimelineSegment[], playbackMs: number): string | null {
  if (!Number.isFinite(playbackMs) || playbackMs < 0) return null;

  let best: ReviewTimelineSegment | null = null;
  for (const segment of segments) {
    if (playbackMs < segment.startMs || playbackMs >= segment.endMs) continue;

    if (
      best === null ||
      Number(segment.revision === "canonical") > Number(best.revision === "canonical") ||
      (segment.revision === best.revision &&
        alignmentPriority[segment.alignmentQuality] > alignmentPriority[best.alignmentQuality]) ||
      (segment.revision === best.revision &&
        segment.alignmentQuality === best.alignmentQuality &&
        segment.startMs > best.startMs)
    ) {
      best = segment;
    }
  }

  return best?.id ?? null;
}

type PlaybackSegment = Pick<ReviewTimelineSegment, "id" | "revision" | "startMs" | "endMs" | "alignmentQuality">;

interface PlaybackInterval {
  segment: PlaybackSegment;
  order: number;
  maxEndMs: number;
  left: PlaybackInterval | null;
  right: PlaybackInterval | null;
}

/** Build once per transcript revision, then reuse for timeupdates and arbitrary seeks.
 * The balanced interval tree skips subtrees that cannot contain the playhead.
 * Disjoint timelines take logarithmic work; overlaps visit additional candidates.
 * Original order breaks exact ties, just like the one-shot linear lookup.
 */
export function createReviewPlaybackLookup(segments: readonly PlaybackSegment[]): (atMs: number) => string | null {
  const ordered = segments
    .map((segment, order) => ({ segment, order }))
    .filter(
      ({ segment }) =>
        Number.isFinite(segment.startMs) && Number.isFinite(segment.endMs) && segment.endMs > segment.startMs,
    )
    .sort((a, b) => a.segment.startMs - b.segment.startMs || a.order - b.order);

  function build(low: number, high: number): PlaybackInterval | null {
    if (low >= high) return null;
    const middle = (low + high) >>> 1;
    const left = build(low, middle);
    const right = build(middle + 1, high);
    const entry = ordered[middle];
    return {
      ...entry,
      left,
      right,
      maxEndMs: Math.max(entry.segment.endMs, left?.maxEndMs ?? -Infinity, right?.maxEndMs ?? -Infinity),
    };
  }
  const root = build(0, ordered.length);

  return (atMs) => {
    if (!Number.isFinite(atMs) || atMs < 0) return null;
    let best: PlaybackInterval | null = null;
    function visit(node: PlaybackInterval | null): void {
      if (!node || node.maxEndMs <= atMs) return;
      visit(node.left);
      const segment = node.segment;
      if (segment.startMs > atMs) return;
      if (atMs < segment.endMs) {
        const priority =
          best === null
            ? 1
            : Number(segment.revision === "canonical") - Number(best.segment.revision === "canonical") ||
              alignmentPriority[segment.alignmentQuality] - alignmentPriority[best.segment.alignmentQuality] ||
              segment.startMs - best.segment.startMs ||
              best.order - node.order;
        if (priority > 0) best = node;
      }
      visit(node.right);
    }
    visit(root);
    return (best as PlaybackInterval | null)?.segment.id ?? null;
  };
}

export function matchingReviewSegmentIds(segments: readonly ReviewTimelineSegment[], search: ReviewSearch): string[] {
  const query = search.query.trim().toLocaleLowerCase();
  return segments
    .filter((segment) => !search.speakerId || segment.speakerId === search.speakerId)
    .filter((segment) => search.fromMs == null || segment.endMs > search.fromMs)
    .filter((segment) => search.toMs == null || segment.startMs < search.toMs)
    .filter(
      (segment) =>
        !query || segment.text.toLocaleLowerCase().includes(query) || segment.label.toLocaleLowerCase().includes(query),
    )
    .map((segment) => segment.id);
}

export function nextReviewMatchId(
  matchIds: readonly string[],
  currentId: string | null,
  direction: 1 | -1,
): string | null {
  if (matchIds.length === 0) return null;
  const currentIndex = currentId ? matchIds.indexOf(currentId) : -1;
  if (currentIndex === -1) return direction === 1 ? matchIds[0] : matchIds[matchIds.length - 1];
  return matchIds[(currentIndex + direction + matchIds.length) % matchIds.length] ?? null;
}

export function reviewTimeRangeBounds(range: ReviewTimeRange): { fromMs: number | null; toMs: number | null } {
  switch (range) {
    case "first-15":
      return { fromMs: 0, toMs: 900_000 };
    case "15-30":
      return { fromMs: 900_000, toMs: 1_800_000 };
    case "30-60":
      return { fromMs: 1_800_000, toMs: 3_600_000 };
    case "after-60":
      return { fromMs: 3_600_000, toMs: null };
    default:
      return { fromMs: null, toMs: null };
  }
}

export function reviewTimelinePositionPercent(atMs: number, durationMs: number): number {
  if (!Number.isFinite(atMs) || !Number.isFinite(durationMs) || durationMs <= 0) return 0;
  return Math.min(100, Math.max(0, (atMs / durationMs) * 100));
}
