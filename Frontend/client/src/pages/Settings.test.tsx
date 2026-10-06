import { act, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { invalidateSettingsBootstrap } from "@/lib/settings-bootstrap";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { getAutostartStatus } from "@/lib/backend";
import Settings from "./Settings";

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/contexts/WebSocketContext", () => ({ useSharedWebSocket: () => ({ isConnected: true }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/lib/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/backend")>()),
  getAutostartStatus: vi.fn(),
}));

let client: QueryClient;
let finishMicrophones: (response: Response) => void;
let finishAutostart: (status: { enabled: boolean; available: boolean }) => void;

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  invalidateSettingsBootstrap();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const microphones = new Promise<Response>((resolve) => {
    finishMicrophones = resolve;
  });
  vi.mocked(getAutostartStatus).mockReturnValue(
    new Promise((resolve) => {
      finishAutostart = resolve;
    }),
  );
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) => {
    const path = String(url);
    if (path.endsWith("/api/settings"))
      return Response.json({ hotkey: "Ctrl + Alt + X", apiKeys: { openai: "saved-key" } });
    if (path.endsWith("/api/microphones")) return microphones;
    return Response.json({ items: [], models: [], available: false, profiles: [] });
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  client.clear();
  invalidateSettingsBootstrap();
});

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Settings />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

it("shows persisted settings before slow devices and autostart, and keeps them after device failure", async () => {
  const view = mount();
  const shell = view.container.querySelector('[data-page-shell="settings"]')!;
  await waitFor(() => expect(shell).toHaveClass("opacity-100"));
  expect(view.container.textContent).toContain("Ctrl + Alt + X");
  expect(toast).not.toHaveBeenCalled();
  await act(async () => {
    finishMicrophones(new Response("device failed", { status: 500 }));
    finishAutostart({ enabled: true, available: true });
  });
  expect(shell).toHaveClass("opacity-100");
  expect(view.container.textContent).toContain("Ctrl + Alt + X");
  expect(toast).toHaveBeenCalledTimes(1);
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Failed to load microphones" }));
});

it("reports only the primary settings error when settings and microphones both fail", async () => {
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    String(url).endsWith("/api/settings")
      ? new Response("settings failed", { status: 500 })
      : String(url).endsWith("/api/microphones")
        ? new Response("devices failed", { status: 500 })
        : Response.json({ items: [], models: [], available: false, profiles: [] }),
  );
  mount();
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Failed to load settings" }));
});

it("ignores a late microphone error after leaving settings", async () => {
  const view = mount();
  await waitFor(() => expect(view.container.querySelector('[data-page-shell="settings"]')).toHaveClass("opacity-100"));
  view.unmount();
  await act(async () => {
    finishMicrophones(new Response("device failed", { status: 500 }));
    finishAutostart({ enabled: false, available: true });
  });
  expect(toast).not.toHaveBeenCalled();
});
