import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY, initializeLocaleCatalog } from "@/i18n";
import Podcasts from "./Podcasts";

const { toast } = vi.hoisted(() => {
  window.localStorage.setItem("scriber-ui-locale", "de");
  return { toast: vi.fn() };
});
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));

afterEach(() => vi.unstubAllGlobals());

it("shows the TLS cause and matching diagnostic reference after subscribing to a search result", async () => {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, "de");
  await initializeLocaleCatalog();
  const reference = "a1".repeat(16);
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).startsWith("/api/podcasts/search?q=")) {
      return Response.json({
        items: [{ id: "result", title: "Search result", author: "Publisher", feedUrl: "https://example.com/podcast" }],
      });
    }
    expect(input).toBe("/api/podcasts/subscriptions");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ feedUrl: "https://example.com/podcast", autoProcess: true });
    return Response.json(
      {
        message:
          "The podcast host's TLS certificate could not be verified. Check your network or proxy settings and try again.",
        reference,
      },
      { status: 503 },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: async () => ({ subscriptions: [], activeCount: 0, refreshing: false }),
      },
    },
  });
  try {
    render(
      <QueryClientProvider client={client}>
        <LocaleProvider>
          <Podcasts />
        </LocaleProvider>
      </QueryClientProvider>,
    );
    fireEvent.change(await screen.findByRole("textbox", { name: "Podcast finden" }), { target: { value: "tax" } });
    fireEvent.click(screen.getByRole("button", { name: "Suchen" }));
    await screen.findByText("Search result");
    fireEvent.click(screen.getByRole("button", { name: "Abonnieren" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith({
        title: "Podcast-Aktion fehlgeschlagen",
        description:
          "Das TLS-Zertifikat des Podcast-Anbieters konnte nicht geprüft werden. Prüfe die Netzwerk- oder Proxy-Einstellungen und versuche es erneut. Referenz: " +
          reference,
        variant: "destructive",
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  } finally {
    client.clear();
  }
});
