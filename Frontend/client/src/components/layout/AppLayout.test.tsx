import { render, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { AppLayout } from "./AppLayout";

vi.mock("@/components/DesktopTitleBar", () => ({
  DesktopTitleBar: () => null,
}));
vi.mock("@/components/BrandMark", () => ({
  BrandMark: () => <span aria-hidden="true" />,
}));
vi.mock("@/components/meeting/ActiveMeetingPill", () => ({
  ActiveMeetingPill: () => null,
}));
vi.mock("@/components/ui/sidebar-search", () => ({
  SidebarSearch: () => <div />,
}));
vi.mock("@/components/theme-toggle", () => ({
  ThemeToggle: () => null,
}));
vi.mock("@/components/language-toggle", () => ({
  LanguageToggle: () => null,
}));
vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SheetContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SheetTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  SheetTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

function renderLayout(path: string, attentionCount = 0) {
  const location = memoryLocation({ path });
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: async ({ queryKey }) => {
          if (queryKey[0] !== "/api/podcasts") throw new Error(`Unexpected query ${String(queryKey[0])}`);
          return { subscriptions: [], attentionCount };
        },
      },
    },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <Router hook={location.hook}>
        <LocaleProvider>
          <AppLayout>
            <p>Page</p>
          </AppLayout>
        </LocaleProvider>
      </Router>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

describe("AppLayout navigation", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("restores the raised active state, keeps settings in the main tabs, and exposes the current page", () => {
    const { container, getAllByRole } = renderLayout("/meetings/42");

    const navigations = getAllByRole("navigation", { name: "Main navigation" });
    expect(navigations).toHaveLength(2);
    const windowFrame = container.querySelector(".app-window-frame");
    expect(windowFrame).not.toBeNull();
    expect(windowFrame?.querySelector(".app-scroll-viewport")).not.toBeNull();
    expect(windowFrame?.querySelector("[data-app-overlay-scrollbar]")).not.toBeNull();

    for (const navigation of navigations) {
      const meetingLink = within(navigation).getByRole("link", { name: "Meetings" });
      const youtubeLink = within(navigation).getByRole("link", { name: "YouTube" });
      const settingsLink = within(navigation).getByRole("link", { name: "Settings" });
      const consoleLink = within(navigation).getByRole("link", { name: "Console" });

      expect(meetingLink).toHaveAttribute("aria-current", "page");
      expect(meetingLink).toHaveAttribute("data-active", "true");
      expect(meetingLink).toHaveClass("neu-nav-item", "neu-nav-active");
      expect(youtubeLink).not.toHaveAttribute("aria-current");
      expect(youtubeLink).toHaveAttribute("data-active", "false");
      expect(youtubeLink).not.toHaveClass("neu-nav-active");
      expect(settingsLink.closest("ul")).toBe(meetingLink.closest("ul"));
      expect(settingsLink.closest("ul")).not.toBe(consoleLink.closest("ul"));
    }
  });

  it("counts new or unread podcast episodes on the Podcasts tab", async () => {
    const { findAllByLabelText, getAllByRole } = renderLayout("/file", 3);
    const badges = await findAllByLabelText("3 new or unread podcast episodes");
    expect(badges).toHaveLength(2);
    for (const navigation of getAllByRole("navigation", { name: "Main navigation" })) {
      const podcastsLink = within(navigation).getByRole("link", { name: /^Podcasts/ });
      expect(within(podcastsLink).getByText("3")).toBeVisible();
    }
  });

  it("keeps the Podcasts tab quiet when nothing needs attention", async () => {
    const { client, getAllByRole, queryByLabelText } = renderLayout("/file", 0);
    await waitFor(() => expect(client.getQueryData(["/api/podcasts"])).toBeDefined());
    expect(getAllByRole("link", { name: "Podcasts" })).toHaveLength(2);
    expect(queryByLabelText(/new or unread podcast/)).toBeNull();
  });
});
