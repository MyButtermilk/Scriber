import { useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { Radio } from "lucide-react";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { ToastAction } from "@/components/ui/toast";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/i18n";
import { invalidatePodcastLibrary } from "@/lib/podcast-progress";

const SEEN_EVENT_LIMIT = 200;

function PodcastToastTitle({ children }: { children: string }) {
  return (
    <span className="flex items-center gap-2">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <Radio className="h-4 w-4" aria-hidden="true" />
      </span>
      <span>{children}</span>
    </span>
  );
}

/** Announces new and newly readable podcast episodes anywhere in the app. */
export function PodcastNotificationBridge() {
  const queryClient = useQueryClient();
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const { t, formatNumber } = useI18n();
  const seenRef = useRef<Set<string>>(new Set());

  const handleWsMessage = useCallback(
    (message: ScriberWebSocketMessage) => {
      if (message.type !== "podcast_episodes_discovered" && message.type !== "podcast_episode_ready") return;
      invalidatePodcastLibrary(queryClient);
      if (seenRef.current.has(message.eventId)) return;
      if (seenRef.current.size >= SEEN_EVENT_LIMIT) seenRef.current.clear();
      seenRef.current.add(message.eventId);

      if (message.type === "podcast_episode_ready") {
        toast({
          title: <PodcastToastTitle>{t("Episode ready to read")}</PodcastToastTitle>,
          description: message.podcastTitle ? `${message.podcastTitle}: ${message.title}` : message.title,
          duration: 10_000,
          action: (
            <ToastAction altText={t("Read episode")} onClick={() => setLocation(`/transcript/${message.transcriptId}`)}>
              {t("Read now")}
            </ToastAction>
          ),
        });
        return;
      }

      const single = message.podcasts.length === 1 ? message.podcasts[0] : null;
      const names = message.podcasts.map((podcast) => podcast.title).join(", ");
      toast({
        title: (
          <PodcastToastTitle>
            {message.count === 1
              ? t("New podcast episode")
              : t("{{count}} new podcast episodes", { count: formatNumber(message.count) })}
          </PodcastToastTitle>
        ),
        description: message.queuedCount ? `${names} · ${t("Transcripts and summaries are being prepared.")}` : names,
        duration: 10_000,
        action: (
          <ToastAction
            altText={t("Show new podcast episodes")}
            onClick={() => setLocation(single ? `/podcasts?subscription=${single.subscriptionId}` : "/podcasts")}
          >
            {t("Show")}
          </ToastAction>
        ),
      });
    },
    [formatNumber, queryClient, setLocation, t, toast],
  );

  useSharedWebSocket(handleWsMessage);
  return null;
}
