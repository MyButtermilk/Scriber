import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useSearch } from "wouter";
import {
  ArrowDownToLine,
  ArrowRight,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Headphones,
  Link2,
  Loader2,
  MoreHorizontal,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Rss,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { InfoTooltip } from "@/components/ui/info-tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useI18n } from "@/i18n";
import { useToast } from "@/hooks/use-toast";
import { apiRequest } from "@/lib/queryClient";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";
import {
  PODCAST_EPISODES_KEY,
  PODCAST_LIBRARY_KEY,
  isPodcastIdentifier,
  podcastEpisodeIsBusy,
  podcastStatusLabels,
  type PodcastEpisode,
  type PodcastEpisodePage,
  type PodcastEpisodeView,
  type PodcastLibrary,
  type PodcastSearchResult,
} from "@/lib/podcast-types";

const LIBRARY_KEY = PODCAST_LIBRARY_KEY;

/** Only a page the user can see may mark episodes as seen. */
function usePageVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden",
  );
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

type EpisodeProgress = { read?: boolean; played?: boolean };

/** Applies a confirmed read/played change to a locally retained episode. */
function withProgress(episode: PodcastEpisode, body: EpisodeProgress): PodcastEpisode {
  const stamp = new Date().toISOString();
  const readAt = body.read === undefined ? episode.readAt : body.read ? episode.readAt || stamp : "";
  const playedAt = body.played === undefined ? episode.playedAt : body.played ? episode.playedAt || stamp : "";
  return { ...episode, readAt, playedAt, isUnread: episode.status === "completed" && !readAt && !playedAt };
}

function PodcastMark({ small = false }: { small?: boolean }) {
  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center rounded-2xl border border-border/70 bg-muted/50 text-foreground",
        small ? "h-11 w-11" : "h-16 w-16",
      )}
    >
      <Radio className={cn("stroke-[1.4]", small ? "h-5 w-5" : "h-8 w-8")} aria-hidden="true" />
    </div>
  );
}

function PodcastInfo() {
  const { t } = useI18n();
  return (
    <InfoTooltip compact label={t("How podcast subscriptions work")}>
      <p>
        {t(
          "New episodes are checked every 30 minutes while Scriber is running. Up to three episodes are transcribed and summarized at the same time.",
        )}
      </p>
      <p>
        {t(
          "A new subscription starts with the latest episode. Older episodes are available on demand. Your configured transcription and summary providers apply; their normal usage charges may apply.",
        )}
      </p>
      <p>
        {t(
          "Search uses Apple's public podcast directory without an API key. Audio comes directly from the publisher's public RSS feed. Downloads are limited to 256 MB per episode and 2 GB in total.",
        )}
      </p>
    </InfoTooltip>
  );
}

export default function Podcasts() {
  const { t, formatNumber, formatDate } = useI18n();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState("");
  const [searchText, setSearchText] = useState("");
  const [submittedSearch, setSubmittedSearch] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [showFeed, setShowFeed] = useState(false);
  const [offset, setOffset] = useState(0);
  const [view, setView] = useState<PodcastEpisodeView>("all");
  // The episode being listened to stays listed while its player is open, even
  // after playing it moved it out of the unread view.
  const [player, setPlayer] = useState<{ episode: PodcastEpisode; index: number } | null>(null);
  const playingId = player?.episode.id ?? null;
  const [removeId, setRemoveId] = useState<string | null>(null);
  // Episodes stay marked "New" for this visit after the server records them as seen.
  const [newThisVisit, setNewThisVisit] = useState<ReadonlySet<string>>(() => new Set());
  const seenRequestedRef = useRef<Set<string>>(new Set());
  const pageVisible = usePageVisible();
  const requestedSubscription = new URLSearchParams(useSearch()).get("subscription") ?? "";
  const [, navigate] = useLocation();
  const library = useQuery<PodcastLibrary>({
    queryKey: LIBRARY_KEY,
    staleTime: 10_000,
    refetchInterval: (query) => (query.state.data?.refreshing ? 2000 : query.state.data?.activeCount ? 5000 : 30_000),
    refetchIntervalInBackground: false,
  });
  const subscriptions = library.data?.subscriptions ?? [];
  const selected = subscriptions.find((item) => item.id === selectedId) ?? subscriptions[0];
  const episodeQueryKey = [PODCAST_EPISODES_KEY, selected?.id, offset, view] as const;
  const episodes = useQuery<PodcastEpisodePage>({
    queryKey: episodeQueryKey,
    queryFn: async () =>
      (
        await apiRequest(
          "GET",
          `/api/podcasts/subscriptions/${selected?.id}/episodes?offset=${offset}${view === "unread" ? "&view=unread" : ""}`,
        )
      ).json(),
    enabled: Boolean(selected),
    staleTime: 10_000,
    refetchInterval: library.data?.activeCount ? 5000 : 30_000,
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    // A notification links to its podcast with ?subscription=<id>. The request
    // is consumed so that the same notification link can select it again.
    if (!requestedSubscription) return;
    if (isPodcastIdentifier(requestedSubscription)) {
      setSelectedId(requestedSubscription);
      setOffset(0);
      setView("all");
      setPlayer(null);
    }
    navigate("/podcasts", { replace: true });
  }, [navigate, requestedSubscription]);

  useEffect(() => {
    setNewThisVisit(new Set());
    seenRequestedRef.current = new Set();
  }, [selected?.id]);

  useEffect(() => {
    if (!pageVisible) return;
    const unseen = (episodes.data?.items ?? [])
      .filter((episode) => episode.isNew && !seenRequestedRef.current.has(episode.id))
      .map((episode) => episode.id);
    if (!unseen.length) return;
    for (const id of unseen) seenRequestedRef.current.add(id);
    setNewThisVisit((previous) => new Set([...previous, ...unseen]));
    void apiRequest("POST", "/api/podcasts/episodes/seen", { ids: unseen })
      .then(() => queryClient.invalidateQueries({ queryKey: LIBRARY_KEY }))
      .catch(() => {
        // The episodes stay new on the server and are marked again next time.
        for (const id of unseen) seenRequestedRef.current.delete(id);
      });
  }, [episodes.data, pageVisible, queryClient]);
  const search = useQuery<{ items: PodcastSearchResult[] }>({
    queryKey: ["/api/podcasts/search", submittedSearch],
    queryFn: async () =>
      (await apiRequest("GET", `/api/podcasts/search?q=${encodeURIComponent(submittedSearch)}`)).json(),
    enabled: submittedSearch.length >= 2,
    staleTime: 15 * 60_000,
  });

  const refreshData = () => {
    void queryClient.invalidateQueries({ queryKey: LIBRARY_KEY });
    void queryClient.invalidateQueries({ queryKey: [PODCAST_EPISODES_KEY] });
  };
  const progress = useMutation({
    mutationFn: async ({ id, body }: { id: string; body: EpisodeProgress }) =>
      (await apiRequest("PATCH", `/api/podcasts/episodes/${id}`, body)).json(),
    onSuccess: (_result, { id, body }) => {
      setPlayer((current) =>
        current?.episode.id === id ? { ...current, episode: withProgress(current.episode, body) } : current,
      );
      refreshData();
    },
    onError: (error) =>
      toast({ title: t("Podcast action failed"), description: t(error.message), variant: "destructive" }),
  });
  const listedEpisodes = episodes.data?.items ?? [];
  const shownEpisodes =
    player && !listedEpisodes.some((item) => item.id === player.episode.id)
      ? [...listedEpisodes.slice(0, player.index), player.episode, ...listedEpisodes.slice(player.index)]
      : listedEpisodes;
  const markPlayed = (episode: PodcastEpisode) => {
    if (!episode.playedAt && !progress.isPending) progress.mutate({ id: episode.id, body: { played: true } });
  };
  const mutation = useMutation({
    mutationFn: async ({ method, path, body }: { method: string; path: string; body?: unknown }) => {
      return (await apiRequest(method, path, body, { timeoutMs: 60_000 })).json() as Promise<{ id?: string }>;
    },
    onSuccess: (result, variables) => {
      if (result.id) {
        setSelectedId(result.id);
        setOffset(0);
        setShowFeed(false);
        setFeedUrl("");
      }
      if (variables.method === "DELETE" && variables.path.includes("/subscriptions/")) {
        setSelectedId("");
        setOffset(0);
        setPlayer(null);
      }
      refreshData();
    },
    onError: (error) =>
      toast({ title: t("Podcast action failed"), description: t(error.message), variant: "destructive" }),
  });

  const subscribe = (url: string) =>
    mutation.mutate({ method: "POST", path: "/api/podcasts/subscriptions", body: { feedUrl: url, autoProcess: true } });
  const searchSubmit = (event: FormEvent) => {
    event.preventDefault();
    setSubmittedSearch(searchText.trim());
  };
  const selectedChange = (id: string) => {
    setSelectedId(id);
    setOffset(0);
    setView("all");
    setPlayer(null);
  };
  const viewChange = (next: PodcastEpisodeView) => {
    setView(next);
    setOffset(0);
    setPlayer(null);
  };

  const renderEpisode = (episode: PodcastEpisode, index: number) => {
    const busy = podcastEpisodeIsBusy(episode.status);
    const isNew = episode.isNew || newThisVisit.has(episode.id);
    const consumedLabel = episode.readAt ? "Read" : episode.playedAt ? "Listened" : "";
    const canMarkRead = episode.status === "completed" || Boolean(episode.readAt || episode.playedAt);
    return (
      <article
        key={episode.id}
        data-new={isNew ? "true" : undefined}
        data-unread={episode.isUnread ? "true" : undefined}
        className="group border-b border-border/60 py-5 last:border-b-0"
      >
        <div className="flex items-start gap-3 sm:gap-4">
          <div className="mt-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-border/60 bg-muted/30 text-muted-foreground">
            {busy ? (
              <Loader2 className="h-4 w-4 motion-safe:animate-spin" aria-hidden="true" />
            ) : episode.status === "completed" ? (
              <Check className="h-4 w-4" aria-hidden="true" />
            ) : (
              <Headphones className="h-4 w-4" aria-hidden="true" />
            )}
          </div>
          <div className="min-w-0 flex-1 space-y-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui-micro text-muted-foreground">
              {isNew && (
                <span className="rounded-full bg-primary px-2 py-0.5 font-semibold text-primary-foreground">
                  {t("New")}
                </span>
              )}
              {episode.publishedAt && (
                <time dateTime={episode.publishedAt}>
                  {formatDate(episode.publishedAt, { day: "numeric", month: "short", year: "numeric" })}
                </time>
              )}
              {episode.durationSeconds != null && (
                <span>{t("{{minutes}} min", { minutes: formatNumber(Math.ceil(episode.durationSeconds / 60)) })}</span>
              )}
              <span className="rounded-full border border-border/70 px-2 py-0.5">
                {t(podcastStatusLabels[episode.status])}
              </span>
              {episode.isUnread ? (
                <span className="flex items-center gap-1.5 font-medium text-primary">
                  <span className="h-1.5 w-1.5 rounded-full bg-primary" aria-hidden="true" />
                  {t("Unread")}
                </span>
              ) : (
                consumedLabel && (
                  <span className="flex items-center gap-1">
                    <CheckCheck className="h-3.5 w-3.5" aria-hidden="true" />
                    {t(consumedLabel)}
                  </span>
                )
              )}
            </div>
            <h3
              className={cn(
                "text-sm leading-relaxed text-foreground",
                isNew || episode.isUnread ? "font-semibold" : "font-medium text-foreground/85",
              )}
            >
              {episode.title}
            </h3>
            {episode.description && (
              <p className="line-clamp-2 max-w-3xl text-xs leading-relaxed text-muted-foreground">
                {episode.description}
              </p>
            )}
            {episode.error && (
              <p
                role="status"
                className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs leading-relaxed text-foreground"
              >
                {t(episode.error)}
                <span className="mt-1 block font-mono text-muted-foreground">
                  {t("Reference: {{id}}", { id: episode.transcriptId || episode.id })}
                </span>
              </p>
            )}
            <div className="flex flex-wrap items-center gap-1 pt-1">
              {episode.transcriptId && (
                <Button variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs" asChild>
                  <Link href={`/transcript/${episode.transcriptId}`}>
                    {t("Open transcript")}
                    <ArrowRight className="h-3.5 w-3.5" />
                  </Link>
                </Button>
              )}
              {(episode.status === "available" ||
                episode.status === "failed" ||
                (episode.status === "completed" && episode.downloadedBytes === 0)) && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 text-xs"
                  disabled={mutation.isPending}
                  onClick={() =>
                    mutation.mutate({ method: "POST", path: `/api/podcasts/episodes/${episode.id}/queue` })
                  }
                >
                  {episode.status === "failed" ? (
                    <RefreshCw className="h-3.5 w-3.5" />
                  ) : (
                    <ArrowDownToLine className="h-3.5 w-3.5" />
                  )}
                  {t(
                    episode.status === "failed"
                      ? "Retry episode"
                      : episode.status === "completed"
                        ? "Download audio again"
                        : "Download & transcribe",
                  )}
                </Button>
              )}
              {episode.downloadedBytes > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1.5 text-xs"
                  onClick={() => setPlayer(playingId === episode.id ? null : { episode, index })}
                >
                  {playingId === episode.id ? <X className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  {t(playingId === episode.id ? "Close player" : "Listen")}
                </Button>
              )}
              {!busy && (episode.downloadedBytes > 0 || canMarkRead) && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={t("Episode options")}>
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    {canMarkRead && (
                      <DropdownMenuItem
                        disabled={progress.isPending}
                        onClick={() =>
                          progress.mutate({
                            id: episode.id,
                            body: episode.isUnread ? { read: true } : { read: false, played: false },
                          })
                        }
                      >
                        <CheckCheck className="mr-2 h-4 w-4" />
                        {t(episode.isUnread ? "Mark as read" : "Mark as unread")}
                      </DropdownMenuItem>
                    )}
                    {canMarkRead && episode.downloadedBytes > 0 && <DropdownMenuSeparator />}
                    {episode.downloadedBytes > 0 && (
                      <DropdownMenuItem
                        disabled={mutation.isPending}
                        onClick={() => {
                          setPlayer(null);
                          mutation.mutate({ method: "DELETE", path: `/api/podcasts/episodes/${episode.id}/audio` });
                        }}
                      >
                        <Trash2 className="mr-2 h-4 w-4" />
                        {t("Remove download")}
                      </DropdownMenuItem>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
            {playingId === episode.id && (
              <audio
                controls
                preload="metadata"
                src={apiUrl(`/api/podcasts/episodes/${episode.id}/audio`)}
                className="mt-3 h-10 w-full max-w-lg"
                aria-label={episode.title}
                onPlay={() => markPlayed(episode)}
              />
            )}
          </div>
        </div>
      </article>
    );
  };

  return (
    <div className="app-page-shell space-y-7 py-7" data-page-shell="podcasts">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-2 text-ui-micro font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            {t("A listening library that reads itself")}
          </p>
          <h1 className="text-3xl font-semibold tracking-tight">{t("Podcasts")}</h1>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground">
            {t(
              "Follow the conversations you care about. New episodes become searchable transcripts and concise summaries.",
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <PodcastInfo />
          <Button
            variant="outline"
            size="sm"
            className="gap-2"
            disabled={mutation.isPending || library.data?.refreshing || !subscriptions.length}
            onClick={() => mutation.mutate({ method: "POST", path: "/api/podcasts/refresh" })}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", library.data?.refreshing && "motion-safe:animate-spin")} />
            {t("Check for episodes")}
          </Button>
        </div>
      </header>

      <section className="rounded-2xl border border-border/70 bg-card p-4 sm:p-5" aria-label={t("Find a podcast")}>
        <form onSubmit={searchSubmit} className="flex flex-wrap gap-2">
          <div className="relative min-w-52 flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              minLength={2}
              maxLength={160}
              placeholder={t("Find a podcast by title or creator…")}
              aria-label={t("Find a podcast")}
              className="h-10 rounded-xl pl-9"
            />
          </div>
          <Button
            type="submit"
            disabled={searchText.trim().length < 2 || search.isFetching}
            className="h-10 gap-2 rounded-xl px-5"
          >
            {search.isFetching && <Loader2 className="h-4 w-4 motion-safe:animate-spin" />}
            {t("Search")}
          </Button>
          <Button
            type="button"
            variant="ghost"
            className="h-10 gap-2 rounded-xl"
            onClick={() => setShowFeed(!showFeed)}
          >
            <Link2 className="h-4 w-4" />
            {t("Add RSS feed")}
          </Button>
        </form>
        {showFeed && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              subscribe(feedUrl);
            }}
            className="mt-3 flex gap-2 border-t border-border/60 pt-3"
          >
            <Input
              type="url"
              required
              maxLength={4096}
              value={feedUrl}
              onChange={(event) => setFeedUrl(event.target.value)}
              placeholder="https://example.com/podcast/feed.xml"
              aria-label={t("Podcast RSS feed URL")}
              className="rounded-xl"
            />
            <Button
              type="submit"
              variant="outline"
              disabled={mutation.isPending || !feedUrl}
              className="gap-2 rounded-xl"
            >
              <Plus className="h-4 w-4" />
              {t("Subscribe")}
            </Button>
          </form>
        )}
        {search.error && (
          <p role="alert" className="mt-4 text-sm text-muted-foreground">
            {t(search.error.message)}
          </p>
        )}
        {submittedSearch && search.data && (
          <div className="mt-4 border-t border-border/60 pt-4">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs text-muted-foreground">
                {t("Podcast directory · {{count}} results", { count: formatNumber(search.data.items.length) })}
              </p>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={t("Close search results")}
                onClick={() => setSubmittedSearch("")}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            {search.data.items.length === 0 ? (
              <p className="py-3 text-sm text-muted-foreground">
                {t("No podcasts found. Try another title or add a public RSS feed.")}
              </p>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {search.data.items.map((result) => {
                  const subscribed = subscriptions.some((item) => item.feedUrl === result.feedUrl);
                  return (
                    <div
                      key={result.id}
                      className="flex min-w-0 items-center gap-3 rounded-xl border border-border/60 p-3"
                    >
                      <PodcastMark small />
                      <div className="min-w-0 flex-1">
                        <h2 className="truncate text-sm font-medium">{result.title}</h2>
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">{result.author}</p>
                      </div>
                      <Button
                        variant={subscribed ? "ghost" : "outline"}
                        size="sm"
                        className="h-8 gap-1.5 text-xs"
                        disabled={subscribed || mutation.isPending}
                        onClick={() => subscribe(result.feedUrl)}
                      >
                        {subscribed ? <Check className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
                        {t(subscribed ? "Subscribed" : "Subscribe")}
                      </Button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </section>

      {library.error ? (
        <div role="alert" className="rounded-2xl border border-border p-6 text-sm text-muted-foreground">
          {t("Your podcast library could not be loaded.")}
          <Button variant="ghost" size="sm" className="ml-2" onClick={() => void library.refetch()}>
            {t("Retry")}
          </Button>
        </div>
      ) : library.isLoading ? (
        <div className="flex min-h-48 items-center justify-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 motion-safe:animate-spin" />
          {t("Loading podcasts…")}
        </div>
      ) : !selected ? (
        <section className="flex min-h-72 flex-col items-center justify-center rounded-2xl border border-dashed border-border/80 px-6 py-12 text-center">
          <PodcastMark />
          <h2 className="mt-5 text-lg font-medium">{t("Your next good listen starts here")}</h2>
          <p className="mt-2 max-w-md text-sm leading-relaxed text-muted-foreground">
            {t(
              "Search for a podcast or paste its RSS feed. Subscribe once and let new episodes arrive with a transcript and summary.",
            )}
          </p>
          <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
            <Rss className="h-3.5 w-3.5" />
            {t("No extra podcast account or API key needed")}
          </p>
        </section>
      ) : (
        <div className="grid items-start gap-6 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="space-y-3" aria-label={t("Your subscriptions")}>
            <div className="flex items-center justify-between px-1">
              <h2 className="text-xs font-medium text-muted-foreground">{t("Your subscriptions")}</h2>
              <span className="text-xs tabular-nums text-muted-foreground">{formatNumber(subscriptions.length)}</span>
            </div>
            <div className="flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible">
              {subscriptions.map((item) => (
                <button
                  key={item.id}
                  onClick={() => selectedChange(item.id)}
                  aria-current={selected.id === item.id ? "true" : undefined}
                  className={cn(
                    "flex min-w-56 items-center gap-3 rounded-xl border p-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-w-0",
                    selected.id === item.id
                      ? "border-border bg-card shadow-sm"
                      : "border-transparent hover:bg-muted/50",
                  )}
                >
                  <PodcastMark small />
                  <span className="min-w-0 flex-1">
                    <span
                      className={cn("block truncate text-sm", item.attentionCount ? "font-semibold" : "font-medium")}
                    >
                      {item.title}
                    </span>
                    <span className="mt-1 block text-ui-micro text-muted-foreground">
                      {item.unreadCount
                        ? t("{{count}} unread", { count: formatNumber(item.unreadCount) })
                        : t("{{count}} ready", { count: formatNumber(item.completedCount) })}
                      {!item.autoProcess && ` · ${t("Paused")}`}
                    </span>
                  </span>
                  {item.newCount > 0 && (
                    <span
                      className="shrink-0 rounded-full bg-primary px-2 py-0.5 text-ui-micro font-semibold tabular-nums text-primary-foreground"
                      aria-label={
                        item.newCount === 1
                          ? t("1 new episode")
                          : t("{{count}} new episodes", { count: formatNumber(item.newCount) })
                      }
                    >
                      {t("{{count}} new", { count: formatNumber(item.newCount) })}
                    </span>
                  )}
                </button>
              ))}
            </div>
            <p className="px-1 text-ui-micro leading-relaxed text-muted-foreground">
              {library.data?.activeCount
                ? library.data.activeCount === 1
                  ? t("1 episode in progress")
                  : t("{{count}} episodes in progress", { count: formatNumber(library.data.activeCount) })
                : t("Up to date. Checking while Scriber is running.")}
            </p>
          </aside>

          <section
            className="min-w-0 rounded-2xl border border-border/70 bg-card px-4 sm:px-6"
            aria-label={selected.title}
          >
            <header className="border-b border-border/70 py-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-ui-micro text-muted-foreground">{selected.author}</p>
                  <h2 className="mt-1 text-xl font-semibold tracking-tight">{selected.title}</h2>
                </div>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={t("Subscription options")}>
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => setRemoveId(selected.id)}>
                      <Trash2 className="mr-2 h-4 w-4" />
                      {t("Unsubscribe")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
              {selected.description && (
                <p className="mt-3 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                  {selected.description}
                </p>
              )}
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <label className="flex items-center gap-2.5 text-xs">
                  <Switch
                    className="compact-impact-switch"
                    checked={selected.autoProcess}
                    disabled={mutation.isPending}
                    onCheckedChange={(checked) =>
                      mutation.mutate({
                        method: "PATCH",
                        path: `/api/podcasts/subscriptions/${selected.id}`,
                        body: { autoProcess: checked },
                      })
                    }
                    aria-label={t("Automatically process new episodes")}
                  />
                  {t("Automatic downloads, transcripts & summaries")}
                </label>
                <PodcastInfo />
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <div
                  role="group"
                  aria-label={t("Episode filter")}
                  className="inline-flex rounded-lg border border-border/70 p-0.5"
                >
                  {(["all", "unread"] as const).map((option) => (
                    <button
                      key={option}
                      type="button"
                      aria-pressed={view === option}
                      onClick={() => viewChange(option)}
                      className={cn(
                        "rounded-md px-3 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        view === option ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {option === "all"
                        ? t("All episodes")
                        : selected.unreadCount
                          ? t("Unread ({{count}})", { count: formatNumber(selected.unreadCount) })
                          : t("Unread")}
                    </button>
                  ))}
                </div>
                {selected.attentionCount > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 gap-1.5 text-xs"
                    disabled={mutation.isPending}
                    onClick={() => {
                      setNewThisVisit(new Set());
                      mutation.mutate({ method: "POST", path: `/api/podcasts/subscriptions/${selected.id}/read` });
                    }}
                  >
                    <CheckCheck className="h-3.5 w-3.5" />
                    {t("Mark all as read")}
                  </Button>
                )}
              </div>
              {selected.error && (
                <p role="status" className="mt-3 rounded-lg bg-muted/60 p-3 text-xs text-muted-foreground">
                  {t(selected.error)}
                </p>
              )}
            </header>
            {episodes.isLoading ? (
              <div className="flex min-h-48 items-center justify-center">
                <Loader2
                  className="h-5 w-5 text-muted-foreground motion-safe:animate-spin"
                  aria-label={t("Loading episodes…")}
                />
              </div>
            ) : episodes.error ? (
              <p role="alert" className="py-6 text-sm text-muted-foreground">
                {t("Episodes could not be loaded. Try again.")}
              </p>
            ) : shownEpisodes.length ? (
              shownEpisodes.map(renderEpisode)
            ) : (
              <p className="py-8 text-sm text-muted-foreground">
                {view === "unread"
                  ? t("You're all caught up. Every ready episode has been read or played.")
                  : t("No audio episodes are available in this feed yet.")}
              </p>
            )}
            {(episodes.data?.total ?? 0) > 50 && (
              <div className="flex items-center justify-between border-t border-border py-4">
                <span className="text-xs text-muted-foreground">
                  {t("{{count}} episodes", { count: formatNumber(episodes.data?.total ?? 0) })}
                </span>
                <div className="flex gap-2">
                  <Button
                    size="icon"
                    variant="outline"
                    className="h-8 w-8"
                    disabled={offset === 0}
                    onClick={() => {
                      setOffset(Math.max(0, offset - 50));
                      setPlayer(null);
                    }}
                    aria-label={t("Previous page")}
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    className="h-8 w-8"
                    disabled={offset + 50 >= (episodes.data?.total ?? 0)}
                    onClick={() => {
                      setOffset(offset + 50);
                      setPlayer(null);
                    }}
                    aria-label={t("Next page")}
                  >
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            )}
          </section>
        </div>
      )}
      <AlertDialog
        open={removeId !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Unsubscribe from this podcast?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t(
                "Automatic processing stops and downloaded episodes are removed. Your transcripts and summaries remain in File history.",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (removeId) mutation.mutate({ method: "DELETE", path: `/api/podcasts/subscriptions/${removeId}` });
                setRemoveId(null);
              }}
            >
              {t("Unsubscribe")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
