import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { SidebarSearch } from "./sidebar-search";

describe("SidebarSearch", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it("exposes the keyboard shortcut and opens the command palette", () => {
    const onOpenCommandPalette = vi.fn();

    render(
      <LocaleProvider>
        <SidebarSearch onOpenCommandPalette={onOpenCommandPalette} />
      </LocaleProvider>,
    );

    const button = screen.getByRole("button", { name: "Open command palette" });
    expect(button).toHaveAttribute("aria-keyshortcuts", "Control+K Meta+K");

    fireEvent.click(button);
    expect(onOpenCommandPalette).toHaveBeenCalledTimes(1);
  });
});
