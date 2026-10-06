import type { QueryClient } from "@tanstack/react-query";
import type { MeetingDetail } from "@/lib/api-types";

const DWELL_MS = 150;
type Intent = "pointer" | "focus";

// One speculative request at a time. Never cancel a query borrowed from another
// consumer, or one adopted by navigation while the pointer leaves the row.
export function createMeetingDetailPrefetch(
  client: QueryClient,
  fetchDetail: (id: string, signal: AbortSignal) => Promise<MeetingDetail>,
  staleTime: number,
) {
  const intents: Record<Intent, string | null> = { pointer: null, focus: null };
  let candidate: { id: string; readyAt: number } | null = null;
  let active: { id: string; owned: boolean; adopted: boolean } | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function cancelUnusedRequest() {
    if (!active || active.id === candidate?.id || !active.owned || active.adopted) return;
    const queryKey = ["/api/meetings", active.id];
    const query = client.getQueryCache().find({ queryKey, exact: true });
    if (query?.getObserversCount() === 0) void client.cancelQueries({ queryKey, exact: true });
  }

  function schedule() {
    clearTimeout(timer);
    if (candidate && !active) timer = setTimeout(start, Math.max(0, candidate.readyAt - Date.now()));
  }

  function start() {
    if (!candidate || active) return;
    const id = candidate.id;
    const queryKey = ["/api/meetings", id];
    // An existing consumer already has this request in flight. It owns cleanup.
    if (client.getQueryState(queryKey)?.fetchStatus === "fetching") return;
    const request = { id, owned: false, adopted: false };
    active = request;
    void client
      .prefetchQuery({
        queryKey,
        queryFn: async ({ signal }) => {
          request.owned = true;
          const before = client.getQueryData<MeetingDetail>(queryKey);
          const detail = await fetchDetail(id, signal);
          // Live events and edits may update this cache while the HTTP snapshot
          // is in flight. Never replace those newer changes with speculation.
          const current = client.getQueryData<MeetingDetail>(queryKey);
          return current && current !== before ? current : detail;
        },
        staleTime,
      })
      .finally(() => {
        if (active !== request) return;
        active = null;
        if (candidate?.id !== id) schedule();
      });
  }

  function reconcile() {
    const id = intents.pointer ?? intents.focus;
    if (candidate?.id === id) return;
    clearTimeout(timer);
    candidate = id ? { id, readyAt: Date.now() + DWELL_MS } : null;
    cancelUnusedRequest();
    schedule();
  }

  return {
    enter(id: string, source: Intent) {
      intents[source] = id;
      reconcile();
    },
    leave(id: string, source: Intent) {
      if (intents[source] === id) intents[source] = null;
      reconcile();
    },
    forget(id: string, preserveRequest = false) {
      if (preserveRequest && active?.id === id) active.adopted = true;
      if (intents.pointer === id) intents.pointer = null;
      if (intents.focus === id) intents.focus = null;
      reconcile();
    },
    dispose() {
      intents.pointer = null;
      intents.focus = null;
      reconcile();
    },
  };
}
