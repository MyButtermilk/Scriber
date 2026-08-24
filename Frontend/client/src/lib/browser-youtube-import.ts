import type { YouTubeSearchItem } from "@/lib/api-types";

const YOUTUBE_VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;
const BROWSER_REQUEST_ID_PATTERN = /^[a-f0-9]{32}$/;
const BROWSER_IMPORT_KEYS = ["browserVideo", "browserRequest", "browserTitle", "browserChannel"] as const;

export interface BrowserYoutubeImport {
  requestId: string;
  item: YouTubeSearchItem;
}

function oneQueryValue(params: URLSearchParams, key: string, required: boolean): string | null {
  const values = params.getAll(key);
  if (values.length > 1 || (required && values.length !== 1)) {
    return null;
  }
  return values[0] ?? "";
}

function normalizedMetadata(value: string, maxCharacters: number): string | null {
  const normalized = value.trim();
  const characters = Array.from(normalized);
  const hasControlCharacter = characters.some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint <= 0x1f || codePoint === 0x7f;
  });
  if (characters.length > maxCharacters || hasControlCharacter) {
    return null;
  }
  return normalized;
}

export function parseBrowserYoutubeImport(search: string): BrowserYoutubeImport | null {
  const params = new URLSearchParams(search);
  const videoId = oneQueryValue(params, "browserVideo", true);
  const requestId = oneQueryValue(params, "browserRequest", true);
  const rawTitle = oneQueryValue(params, "browserTitle", false);
  const rawChannel = oneQueryValue(params, "browserChannel", false);
  if (
    !videoId ||
    !requestId ||
    !YOUTUBE_VIDEO_ID_PATTERN.test(videoId) ||
    !BROWSER_REQUEST_ID_PATTERN.test(requestId) ||
    rawTitle === null ||
    rawChannel === null
  ) {
    return null;
  }
  const title = normalizedMetadata(rawTitle, 500);
  const channelTitle = normalizedMetadata(rawChannel, 300);
  if (title === null || channelTitle === null) {
    return null;
  }

  return {
    requestId,
    item: {
      videoId,
      url: `https://www.youtube.com/watch?v=${videoId}`,
      title: title || "YouTube",
      description: "",
      channelTitle,
      publishedAt: "",
      thumbnailUrl: `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`,
      duration: "00:00",
      durationSeconds: 0,
    },
  };
}

export function stripBrowserYoutubeImportParams(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of BROWSER_IMPORT_KEYS) {
    params.delete(key);
  }
  return params.toString();
}
