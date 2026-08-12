import { render, within } from "@testing-library/react";
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

describe("AppLayout navigation", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("labels every main navigation and exposes the current page", () => {
    const location = memoryLocation({ path: "/meetings/42" });
    const { container, getAllByRole } = render(
      <Router hook={location.hook}>
        <LocaleProvider>
          <AppLayout>
            <p>Meeting</p>
          </AppLayout>
        </LocaleProvider>
      </Router>,
    );

    const navigations = getAllByRole("navigation", { name: "Main navigation" });
    expect(navigations).toHaveLength(2);
    const windowFrame = container.querySelector(".app-window-frame");
    expect(windowFrame).not.toBeNull();
    expect(windowFrame?.querySelector(".app-scroll-viewport")).not.toBeNull();
    expect(windowFrame?.querySelector("[data-app-overlay-scrollbar]")).not.toBeNull();
    expect(container.querySelector(".scriber-app-shell")).toHaveAttribute("data-app-shell", "true");
    expect(container.querySelector(".scriber-nav-rail")).toHaveAttribute("data-navigation-rail", "true");
    expect(container.querySelector(".scriber-workspace-frame")).toHaveAttribute("data-workspace-frame", "true");
    expect(container.querySelector(".scriber-workspace-scroll")).toHaveAttribute("data-workspace-scroll", "true");

    for (const navigation of navigations) {
      const meetingsLink = within(navigation).getByRole("link", { name: "Meetings" });
      const youtubeLink = within(navigation).getByRole("link", { name: "YouTube" });

      expect(meetingsLink).toHaveAttribute("aria-current", "page");
      expect(meetingsLink).toHaveAttribute("data-active", "true");
      expect(meetingsLink).toHaveClass("scriber-nav-link", "min-h-11");
      expect(youtubeLink).not.toHaveAttribute("aria-current");
      expect(youtubeLink).toHaveAttribute("data-active", "false");
    }
  });
});
