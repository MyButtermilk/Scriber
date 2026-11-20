export function alphabeticalLabel(index: number): string {
  let value = Math.max(0, Math.floor(index));
  let result = "";
  do {
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return result;
}

const TECHNICAL_SPEAKER_LABEL = /^(?:speaker|sprecher|remote)(?:[\s_-]*(?:\d+|[a-z]|[a-f0-9]{4,}))?$/i;

export function genericMeetingSpeakerLabel(rawLabel: string, index: number): string | null {
  return TECHNICAL_SPEAKER_LABEL.test(rawLabel.trim()) ? `Speaker ${alphabeticalLabel(index)}` : null;
}

export function genericMeetingSpeakerLabels(
  speakers: ReadonlyArray<{
    id: string;
    label: string;
    displayName: string;
    displayNameSource?: string;
  }>,
): ReadonlyMap<string, string> {
  const labels = new Map<string, string>();
  let technicalIndex = 0;
  speakers.forEach((speaker) => {
    const generic = genericMeetingSpeakerLabel(speaker.label, technicalIndex);
    if (!generic) return;
    // Every durable technical speaker reserves its ordinal, even after it is
    // named. Non-speaker rows such as You or Meeting audio consume no letter.
    technicalIndex += 1;
    if (speaker.displayNameSource && speaker.displayNameSource !== "anonymous") return;
    labels.set(speaker.id, generic);
  });
  return labels;
}

// The meeting store names an untitled meeting `Meeting YYYY-MM-DD`
// (src/data/meeting_store.py, `f"Meeting {now[:10]}"`). Only that exact
// generated form counts; any title with extra user text is kept as written.
const AUTO_MEETING_TITLE = /^Meeting \d{4}-\d{2}-\d{2}$/;

export function isAutoMeetingTitle(title: string): boolean {
  return AUTO_MEETING_TITLE.test(title);
}
