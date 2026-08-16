import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import { MeetingReviewToolbar } from "./MeetingReviewToolbar";

describe("MeetingReviewToolbar", () => {
  beforeEach(() => window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en"));

  it("keeps search, match navigation, speaker focus, playback follow, and bookmarks in one review surface", () => {
    const onSearchChange = vi.fn();
    const onNextMatch = vi.fn();
    const onPlayCurrentMatch = vi.fn();
    const onSpeakerChange = vi.fn();
    const onTimeRangeChange = vi.fn();
    const onFollowPlaybackChange = vi.fn();
    const onBookmark = vi.fn();
    const onSeek = vi.fn();

    render(
      <LocaleProvider>
        <MeetingReviewToolbar
          searchValue="launch"
          onSearchChange={onSearchChange}
          matchIndex={1}
          matchCount={3}
          onPreviousMatch={vi.fn()}
          onNextMatch={onNextMatch}
          onPlayCurrentMatch={onPlayCurrentMatch}
          speakers={[{ id: "speaker-a", label: "Alex" }]}
          speakerId=""
          onSpeakerChange={onSpeakerChange}
          timeRange="all"
          onTimeRangeChange={onTimeRangeChange}
          followPlayback={true}
          onFollowPlaybackChange={onFollowPlaybackChange}
          currentTimeLabel="01:24"
          canBookmark={true}
          onBookmark={onBookmark}
          timelineDurationMs={120_000}
          timelineCurrentMs={30_000}
          timelineMarkers={[{ id: "bookmark-1", atMs: 60_000, kind: "bookmark", label: "Bookmark" }]}
          onSeek={onSeek}
        />
      </LocaleProvider>,
    );

    expect(screen.getByRole("group", { name: "Transcript filters" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Playback review actions" })).toBeInTheDocument();
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search this meeting transcript" }), {
      target: { value: "budget" },
    });
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "Search this meeting transcript" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Filter transcript by speaker" }), {
      target: { value: "speaker-a" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Filter transcript by time" }), {
      target: { value: "15-30" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Follow playback" }));
    fireEvent.click(screen.getByRole("button", { name: "Bookmark 01:24" }));
    fireEvent.click(screen.getByRole("button", { name: "Jump to Bookmark at 01:00" }));

    expect(onSearchChange).toHaveBeenCalledWith("budget");
    expect(onNextMatch).toHaveBeenCalledOnce();
    expect(onPlayCurrentMatch).toHaveBeenCalledOnce();
    expect(onSpeakerChange).toHaveBeenCalledWith("speaker-a");
    expect(onTimeRangeChange).toHaveBeenCalledWith("15-30");
    expect(onFollowPlaybackChange).toHaveBeenCalledWith(false);
    expect(onBookmark).toHaveBeenCalledOnce();
    expect(onSeek).toHaveBeenCalledWith(60_000);
  });
});
