import type { QueryClient } from "@tanstack/react-query";
import { apiRequest } from "@/lib/queryClient";
import { PODCAST_EPISODES_KEY, PODCAST_LIBRARY_KEY, isPodcastIdentifier } from "@/lib/podcast-types";

const pendingTranscriptReadMarks = new Set<string>();

export function invalidatePodcastLibrary(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: PODCAST_LIBRARY_KEY });
  void queryClient.invalidateQueries({ queryKey: [PODCAST_EPISODES_KEY] });
}

/**
 * Opening a podcast episode's transcript counts as reading it, from wherever it
 * is opened. Transcripts of other origins are a server-side no-op.
 */
export async function markPodcastTranscriptRead(queryClient: QueryClient, transcriptId: string): Promise<void> {
  if (!isPodcastIdentifier(transcriptId) || pendingTranscriptReadMarks.has(transcriptId)) return;
  pendingTranscriptReadMarks.add(transcriptId);
  try {
    const response = await apiRequest("POST", `/api/podcasts/transcripts/${transcriptId}/read`);
    const result = (await response.json()) as { updated?: boolean };
    if (result.updated) invalidatePodcastLibrary(queryClient);
  } catch {
    // Reading never fails because the podcast library is unavailable.
  } finally {
    // A later visit must mark it again if the user has since marked it unread.
    pendingTranscriptReadMarks.delete(transcriptId);
  }
}

export function resetPodcastTranscriptReadMarksForTests(): void {
  pendingTranscriptReadMarks.clear();
}
