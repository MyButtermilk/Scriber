import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { OpenRouterRegionPicker } from "./OpenRouterRegionPicker";

describe("OpenRouter region", () => {
  beforeEach(() => window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en"));

  it("shows EU and requires an explicit selection to use global", async () => {
    const change = vi.fn();
    render(
      <LocaleProvider>
        <OpenRouterRegionPicker value="eu" onValueChange={change} />
      </LocaleProvider>,
    );
    const select = screen.getByRole("combobox", { name: "OpenRouter data processing region" });
    expect(select).toHaveValue("eu");
    expect(select).toHaveAccessibleDescription(/Business or Enterprise/);
    expect(select).toHaveAccessibleDescription(/never switches to Global automatically/);
    expect(screen.getAllByRole("option").map((option) => (option as HTMLOptionElement).value)).toEqual([
      "eu",
      "us",
      "global",
    ]);
    expect(change).not.toHaveBeenCalled();
    await userEvent.selectOptions(select, "global");
    expect(change).toHaveBeenCalledExactlyOnceWith("global");
  });

  it("shows the persisted US choice and German scope guidance", async () => {
    render(
      <LocaleProvider>
        <OpenRouterRegionPicker value="us" onValueChange={vi.fn()} />
      </LocaleProvider>,
    );
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: LANGUAGE_STORAGE_KEY, newValue: "de" })));
    const select = await screen.findByRole("combobox", { name: "OpenRouter-Region für die Datenverarbeitung" });
    expect(select).toHaveValue("us");
    expect(select).toHaveAccessibleDescription(/andere Anbieter/);
    expect(screen.getByRole("option", { name: "Europäische Union (Standard)" })).toBeInTheDocument();
  });
});
