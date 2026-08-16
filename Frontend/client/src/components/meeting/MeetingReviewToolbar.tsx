import { BookmarkPlus, ChevronDown, ChevronUp, ScanLine, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/i18n";
import { reviewTimelinePositionPercent, type ReviewTimeRange } from "@/lib/meeting-review-timeline";

interface ReviewSpeaker {
  id: string;
  label: string;
}

export interface MeetingReviewTimelineMarker {
  id: string;
  atMs: number;
  kind: "match" | "bookmark" | "citation";
  label: string;
}

function formatTimelineOffset(atMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(atMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

interface MeetingReviewToolbarProps {
  searchValue: string;
  onSearchChange: (value: string) => void;
  matchIndex: number;
  matchCount: number;
  onPreviousMatch: () => void;
  onNextMatch: () => void;
  onPlayCurrentMatch: () => void;
  speakers: readonly ReviewSpeaker[];
  speakerId: string;
  onSpeakerChange: (speakerId: string) => void;
  timeRange: ReviewTimeRange;
  onTimeRangeChange: (range: ReviewTimeRange) => void;
  followPlayback: boolean;
  onFollowPlaybackChange: (follow: boolean) => void;
  currentTimeLabel: string;
  canBookmark: boolean;
  onBookmark: () => void;
  timelineDurationMs: number;
  timelineCurrentMs: number;
  timelineMarkers: readonly MeetingReviewTimelineMarker[];
  onSeek: (atMs: number) => void;
}

export function MeetingReviewToolbar({
  searchValue,
  onSearchChange,
  matchIndex,
  matchCount,
  onPreviousMatch,
  onNextMatch,
  onPlayCurrentMatch,
  speakers,
  speakerId,
  onSpeakerChange,
  timeRange,
  onTimeRangeChange,
  followPlayback,
  onFollowPlaybackChange,
  currentTimeLabel,
  canBookmark,
  onBookmark,
  timelineDurationMs,
  timelineCurrentMs,
  timelineMarkers,
  onSeek,
}: MeetingReviewToolbarProps) {
  const { t, formatNumber } = useI18n();
  const hasMatches = matchCount > 0;
  const matchLabel = hasMatches
    ? t("{{current}} of {{total}}", {
        current: formatNumber(Math.min(matchIndex + 1, matchCount)),
        total: formatNumber(matchCount),
      })
    : t("No matches");

  return (
    <section
      className="meeting-review-toolbar rounded-[18px] bg-muted/35 p-2.5 shadow-[inset_0_1px_0_hsl(var(--background)/0.65)]"
      aria-label={t("Review timeline")}
    >
      <div className="flex min-w-0 flex-col gap-2">
        <label className="relative block min-w-0 flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            data-testid="meeting-review-search"
            type="search"
            value={searchValue}
            onChange={(event) => onSearchChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && hasMatches) {
                event.preventDefault();
                onPlayCurrentMatch();
              }
            }}
            placeholder={t("Search transcript, speaker, or phrase")}
            className="h-10 rounded-[12px] border-transparent bg-background/80 pl-9 pr-3 text-sm shadow-sm focus-visible:border-primary/40"
            aria-label={t("Search this meeting transcript")}
          />
        </label>

        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <div
            className="flex min-w-0 flex-wrap items-center gap-1.5"
            role="group"
            aria-label={t("Transcript filters")}
          >
            <div className="inline-flex h-10 items-center rounded-[12px] bg-background/75 p-1 shadow-sm">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 rounded-[9px] active:scale-[0.96]"
                disabled={!hasMatches}
                onClick={onPreviousMatch}
                aria-label={t("Previous match")}
              >
                <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
              <span
                data-testid="meeting-review-match-count"
                className="min-w-[58px] px-1 text-center font-mono text-ui-micro font-medium tabular-nums text-muted-foreground"
              >
                {matchLabel}
              </span>
              <Button
                data-testid="meeting-review-next"
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 rounded-[9px] active:scale-[0.96]"
                disabled={!hasMatches}
                onClick={onNextMatch}
                aria-label={t("Next match")}
              >
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
            </div>

            <label className="relative">
              <select
                value={speakerId}
                onChange={(event) => onSpeakerChange(event.target.value)}
                aria-label={t("Filter transcript by speaker")}
                className="h-10 max-w-48 appearance-none rounded-[12px] border-0 bg-background/75 py-0 pl-3 pr-8 text-xs font-medium text-foreground shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">{t("All speakers")}</option>
                {speakers.map((speaker) => (
                  <option key={speaker.id} value={speaker.id}>
                    {speaker.label}
                  </option>
                ))}
              </select>
              <ChevronDown
                className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
            </label>

            <label className="relative">
              <select
                value={timeRange}
                onChange={(event) => onTimeRangeChange(event.target.value as ReviewTimeRange)}
                aria-label={t("Filter transcript by time")}
                className="h-10 appearance-none rounded-[12px] border-0 bg-background/75 py-0 pl-3 pr-8 text-xs font-medium text-foreground shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="all">{t("Entire meeting")}</option>
                <option value="first-15">{t("First 15 minutes")}</option>
                <option value="15-30">{t("Minutes 15 to 30")}</option>
                <option value="30-60">{t("Minutes 30 to 60")}</option>
                <option value="after-60">{t("After 60 minutes")}</option>
              </select>
              <ChevronDown
                className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
            </label>
          </div>

          <div className="flex shrink-0 items-center gap-1.5" role="group" aria-label={t("Playback review actions")}>
            <label className="inline-flex h-10 cursor-pointer items-center gap-2 rounded-[12px] bg-background/75 px-3 text-xs font-medium shadow-sm">
              <input
                data-testid="meeting-review-follow"
                type="checkbox"
                checked={followPlayback}
                onChange={(event) => onFollowPlaybackChange(event.target.checked)}
                className="peer sr-only"
              />
              <span className="grid h-5 w-5 place-items-center rounded-[7px] bg-muted text-muted-foreground peer-checked:bg-primary peer-checked:text-primary-foreground peer-focus-visible:ring-2 peer-focus-visible:ring-ring">
                <ScanLine className="h-3.5 w-3.5" aria-hidden="true" />
              </span>
              {t("Follow playback")}
            </label>

            <Button
              data-testid="meeting-review-bookmark"
              type="button"
              variant="outline"
              className="h-10 rounded-[12px] border-transparent bg-background/75 px-3 text-xs shadow-sm active:scale-[0.97]"
              disabled={!canBookmark}
              onClick={onBookmark}
              aria-label={t("Bookmark {{time}}", { time: currentTimeLabel })}
            >
              <BookmarkPlus className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
              <span className="hidden 2xl:inline">{t("Bookmark")}</span>
              <span className="ml-0 font-mono tabular-nums 2xl:ml-2">{currentTimeLabel}</span>
            </Button>
          </div>
        </div>
      </div>
      {timelineDurationMs > 0 && (
        <div
          data-testid="meeting-review-timeline"
          className="mt-2 px-1"
          role="group"
          aria-label={t("Meeting review markers")}
        >
          <div className="relative h-3">
            <div className="absolute inset-x-0 top-1 h-1 overflow-hidden rounded-full bg-background/85 shadow-inner">
              <span
                className="block h-full origin-left rounded-full bg-primary/55"
                style={{
                  transform: `scaleX(${reviewTimelinePositionPercent(timelineCurrentMs, timelineDurationMs) / 100})`,
                }}
              />
            </div>
            {timelineMarkers.map((marker) => {
              const time = formatTimelineOffset(marker.atMs);
              return (
                <button
                  key={marker.id}
                  type="button"
                  className={`absolute top-0 h-3 w-1.5 -translate-x-1/2 rounded-full outline-none transition-transform duration-[var(--duration-quick)] hover:scale-y-125 focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none ${marker.kind === "bookmark" ? "bg-amber-500" : marker.kind === "citation" ? "bg-emerald-500" : "bg-primary"}`}
                  style={{ left: `${reviewTimelinePositionPercent(marker.atMs, timelineDurationMs)}%` }}
                  onClick={() => onSeek(marker.atMs)}
                  aria-label={t("Jump to {{label}} at {{time}}", { label: marker.label, time })}
                />
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
