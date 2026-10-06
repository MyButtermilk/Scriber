import type { MeetingSegment } from "./api-types";

export type DisplayMeetingSegment = MeetingSegment & { label: string };

/** Preserve display identity for unchanged immutable query-cache segments. */
export function createMeetingSegmentProjector() {
  // Weak keys release removed segments with their query snapshots. Keep this
  // cache local to the mounted page rather than retaining transcript text globally.
  const displays = new WeakMap<MeetingSegment, DisplayMeetingSegment>();
  let previousSegments: readonly MeetingSegment[] = [];
  let previousDisplays: DisplayMeetingSegment[] = [];
  let previousLabelFor: ((segment: MeetingSegment) => string) | undefined;
  return (segments: readonly MeetingSegment[], labelFor: (segment: MeetingSegment) => string) => {
    const labelsUnchanged = labelFor === previousLabelFor;
    const result = segments.map((segment, index) => {
      // Appends and partial-text updates preserve almost every array position.
      // Avoid a WeakMap lookup and label resolution for that common case.
      if (labelsUnchanged && previousSegments[index] === segment) return previousDisplays[index];
      const label = labelFor(segment);
      const cached = displays.get(segment);
      if (cached?.label === label) return cached;
      const display = { ...segment, label };
      displays.set(segment, display);
      return display;
    });
    previousSegments = segments;
    previousDisplays = result;
    previousLabelFor = labelFor;
    return result;
  };
}
