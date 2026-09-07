import { fireEvent, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useHistoryNavigation } from "./use-history-navigation";

afterEach(() => vi.restoreAllMocks());

it("traverses once per side-button click and releases the listener on unmount", () => {
  const go = vi.spyOn(window.history, "go").mockImplementation(() => {});
  const { unmount } = renderHook(useHistoryNavigation);
  for (const button of [3, 4]) {
    expect(fireEvent.mouseDown(document.body, { button })).toBe(false);
    expect(fireEvent.mouseUp(document.body, { button })).toBe(false);
    expect(fireEvent(document.body, new MouseEvent("auxclick", { button, bubbles: true, cancelable: true }))).toBe(
      false,
    );
  }
  expect(go.mock.calls).toEqual([[-1], [1]]);
  for (const button of [0, 1, 2]) fireEvent.mouseUp(document.body, { button });
  expect(go).toHaveBeenCalledTimes(2);
  unmount();
  fireEvent.mouseUp(document.body, { button: 3 });
  expect(go).toHaveBeenCalledTimes(2);
});

it("supports browser keys and Alt+arrows without taking ordinary editor keys", () => {
  const go = vi.spyOn(window.history, "go").mockImplementation(() => {});
  renderHook(useHistoryNavigation);
  fireEvent.keyDown(document.body, { key: "BrowserBack" });
  fireEvent.keyDown(document.body, { key: "ArrowRight", altKey: true });
  fireEvent.keyDown(document.body, { key: "ArrowLeft" });
  fireEvent.keyDown(document.body, { key: "ArrowLeft", altKey: true, ctrlKey: true });
  fireEvent.keyDown(document.body, { key: "Backspace" });
  expect(go.mock.calls).toEqual([[-1], [1]]);
});
