import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LiveMicAutoStopSettings } from "./LiveMicAutoStopSettings";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";

function mount(enabled = false, saving = false) {
  const onChange = vi.fn();
  render(
    <LocaleProvider>
      <LiveMicAutoStopSettings enabled={enabled} silenceSeconds={5} saving={saving} onChange={onChange} />
    </LocaleProvider>,
  );
  return onChange;
}

describe("Live Mic automatic stop settings", () => {
  beforeEach(() => window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en"));

  it("keeps automatic stop disabled until explicitly enabled by keyboard", async () => {
    const onChange = mount();
    const toggle = screen.getByRole("switch", { name: "Stop after silence" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/Applies to new recordings/);
    expect(screen.getByRole("combobox", { name: "Silence duration" })).toBeDisabled();
    expect(screen.getByRole("combobox")).toHaveValue("5");
    toggle.focus();
    await userEvent.keyboard(" ");
    expect(onChange).toHaveBeenCalledExactlyOnceWith({ micAutoStopEnabled: true });
  });

  it("offers exactly one to ten seconds and saves only the chosen threshold", async () => {
    const onChange = mount(true);
    const select = screen.getByRole("combobox", { name: "Silence duration" });
    expect(screen.getAllByRole("option").map((option) => (option as HTMLOptionElement).value)).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
      "9",
      "10",
    ]);
    await userEvent.selectOptions(select, "10");
    expect(onChange).toHaveBeenCalledExactlyOnceWith({ micAutoStopSilenceSeconds: 10 });
  });

  it("prevents a second setting change while the first save is pending", () => {
    const onChange = mount(true, true);
    expect(screen.getByRole("switch")).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("provides German labels and duration units", async () => {
    mount(true);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: LANGUAGE_STORAGE_KEY, newValue: "de" })));
    expect(await screen.findByRole("switch", { name: "Bei Stille stoppen" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Dauer der Stille" })).toHaveValue("5");
    expect(screen.getByRole("option", { name: "1 Sekunde" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "10 Sekunden" })).toBeInTheDocument();
  });
});
