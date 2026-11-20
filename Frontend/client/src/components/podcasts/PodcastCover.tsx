import { useState } from "react";
import { Radio } from "lucide-react";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";

export type PodcastCoverSize = "rail" | "result" | "header" | "hero";

interface PodcastCoverProps {
  /** Subscription or search-result ID; both resolve through the same artwork route. */
  id?: string;
  /** False when the backend knows there is no cover, so no request is made. */
  hasArtwork?: boolean;
  size?: PodcastCoverSize;
  className?: string;
}

/**
 * Podcast cover artwork served by the local backend, with the radio mark as
 * fallback while loading, when no cover is known, or when it fails to load.
 * Decorative: the podcast title is always rendered next to it.
 */
export function PodcastCover({ id, hasArtwork = true, size = "rail", className }: PodcastCoverProps) {
  const src = id && hasArtwork ? apiUrl(`/api/podcasts/artwork/${id}`) : "";
  const [failedSrc, setFailedSrc] = useState("");
  const [loadedSrc, setLoadedSrc] = useState("");
  const showImage = Boolean(src) && failedSrc !== src;
  return (
    <span
      className={cn("podcast-cover", `podcast-cover--${size}`, className)}
      data-cover-state={showImage ? (loadedSrc === src ? "image" : "loading") : "fallback"}
      aria-hidden="true"
    >
      <Radio className="podcast-cover__mark" aria-hidden="true" />
      {showImage ? (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          className="podcast-cover__image"
          onLoad={() => setLoadedSrc(src)}
          onError={() => setFailedSrc(src)}
        />
      ) : null}
    </span>
  );
}
