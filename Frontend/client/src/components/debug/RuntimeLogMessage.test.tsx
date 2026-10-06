import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { RuntimeLogContext } from "@/lib/api-types";
import { RuntimeLogMessage } from "./RuntimeLogMessage";

beforeEach(() => localStorage.setItem(LANGUAGE_STORAGE_KEY, "en"));

it("does not serialize closed JSON, serializes once when opened, and copies exact redacted context", async () => {
  const context: RuntimeLogContext = { event: "completed", meta: { count: 12, model: "test-model" } };
  const stringify = vi.spyOn(JSON, "stringify");
  const onCopy = vi.fn();
  const content = (copiedKey = "") => (
    <LocaleProvider>
      <RuntimeLogMessage message="Finished" context={context} copyKey="row" copiedKey={copiedKey} onCopy={onCopy} />
    </LocaleProvider>
  );
  const view = render(content());
  const serializations = () => stringify.mock.calls.filter(([value]) => value === context).length;
  expect(serializations()).toBe(0);
  expect(view.container.querySelector("pre")).toBeNull();
  const details = view.container.querySelector<HTMLDetailsElement>(".debug-log-raw-details")!;
  await act(async () => {
    details.open = true;
    fireEvent(details, new Event("toggle"));
  });
  expect(serializations()).toBe(1);
  const raw = view.container.querySelector("pre")!.textContent;
  fireEvent.click(screen.getByRole("button", { name: "Copy raw structured log data" }));
  expect(onCopy).toHaveBeenCalledWith(raw, "row:context");
  expect(JSON.parse(raw!)).toEqual(context);
  view.rerender(content("row:context"));
  expect(serializations()).toBe(1);
  expect(screen.getByText("Copied")).toBeInTheDocument();
});

it("skips repeated context traversal on unrelated parent renders but accepts changed records", () => {
  const read = vi.fn(() => "completed");
  const context: RuntimeLogContext = {
    get event() {
      return read();
    },
    meta: { count: 12 },
  };
  const onCopy = vi.fn();
  const content = (message = "Finished") => (
    <LocaleProvider>
      <RuntimeLogMessage message={message} context={context} copyKey="row" copiedKey="" onCopy={onCopy} />
    </LocaleProvider>
  );
  const view = render(content());
  expect(read).toHaveBeenCalled();
  read.mockClear();
  for (let index = 0; index < 20; index++) view.rerender(content());
  expect(read).not.toHaveBeenCalled();
  view.rerender(content("Updated"));
  expect(screen.getByText("Updated")).toBeInTheDocument();
  expect(read).toHaveBeenCalled();
});
