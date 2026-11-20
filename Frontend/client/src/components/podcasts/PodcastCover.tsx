import { useLayoutEffect, useRef, useState } from "react";
import { Radio } from "lucide-react";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";

export type PodcastCoverSize = "rail" | "result" | "header" | "hero";

// Remember covers that already decoded in this session so switching back to
// Podcasts renders cached artwork immediately without a fallback-icon flash.
const MAX_CACHED_COVER_URLS = 256;
const loadedCoverUrls = new Set<string>();

function rememberLoadedCover(url: string) {
  if (!url) return;
  if (loadedCoverUrls.size >= MAX_CACHED_COVER_URLS && !loadedCoverUrls.has(url)) {
    const oldest = loadedCoverUrls.values().next().value;
    if (oldest) loadedCoverUrls.delete(oldest);
  }
  loadedCoverUrls.add(url);
}

interface PodcastCoverProps {
  /** Subscription or search-result ID; both resolve through the same artwork route. */
  id?: string;
  /** False when the backend knows there is no cover, so no request is made. */
  hasArtwork?: boolean;
  /** Cover version from the backend; versioned URLs may be cached by the browser. */
  version?: string;
  size?: PodcastCoverSize;
  className?: string;
}

/**
 * Podcast cover artwork served by the local backend, with the radio mark as
 * fallback while loading, when no cover is known, or when it fails to load.
 * Decorative: the podcast title is always rendered next to it.
 */
export function PodcastCover({ id, hasArtwork = true, version, size = "rail", className }: PodcastCoverProps) {
  const query = version ? `?v=${encodeURIComponent(version)}` : "";
  const src = id && hasArtwork ? apiUrl(`/api/podcasts/artwork/${id}${query}`) : "";
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [failedSrc, setFailedSrc] = useState("");
  const [loadedSrc, setLoadedSrc] = useState(() => (src && loadedCoverUrls.has(src) ? src : ""));
  const [instantSrc, setInstantSrc] = useState(() => (src && loadedCoverUrls.has(src) ? src : ""));

  useLayoutEffect(() => {
    if (!src) return;
    if (loadedCoverUrls.has(src)) {
      setLoadedSrc(src);
      setInstantSrc(src);
      return;
    }
    const img = imgRef.current;
    if (img && img.complete && img.naturalWidth > 0) {
      rememberLoadedCover(src);
      setLoadedSrc(src);
      setInstantSrc(src);
    }
  }, [src]);

  const showImage = Boolean(src) && failedSrc !== src;
  const isLoaded = showImage && (loadedSrc === src || loadedCoverUrls.has(src));
  const isInstant = isLoaded && (instantSrc === src || loadedCoverUrls.has(src));

  return (
    <span
      className={cn("podcast-cover", `podcast-cover--${size}`, className)}
      data-cover-state={showImage ? (isLoaded ? "image" : "loading") : "fallback"}
      data-cover-instant={isInstant ? "true" : "false"}
      aria-hidden="true"
    >
      <Radio className="podcast-cover__mark" aria-hidden="true" />
      {showImage ? (
        <img
          ref={imgRef}
          src={src}
          alt=""
          loading={size === "result" ? "lazy" : "eager"}
          decoding="async"
          draggable={false}
          className="podcast-cover__image"
          onLoad={() => {
            rememberLoadedCover(src);
            setLoadedSrc(src);
          }}
          onError={() => setFailedSrc(src)}
        />
      ) : null}
    </span>
  );
}
