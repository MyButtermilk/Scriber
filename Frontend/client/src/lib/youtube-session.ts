/** Discard other sites' cookies before any data reaches the backend. */
export function youtubeOnlyCookieFile(content: string): string {
  const lines = content.replace(/^\uFEFF/, "").split(/\r?\n/);
  const allowedDomains = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"]);
  return lines
    .filter(
      (line, index) =>
        index === 0 ||
        allowedDomains.has(
          line
            .replace(/^#HttpOnly_/, "")
            .split("\t")[0]
            .toLowerCase()
            .replace(/^\./, ""),
        ),
    )
    .join("\n");
}
