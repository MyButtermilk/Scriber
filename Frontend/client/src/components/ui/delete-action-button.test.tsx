import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/i18n";
import { DELETE_CONFIRM_TIMEOUT_MS, DeleteActionButton } from "./delete-action-button";

function renderButton(props: Partial<Parameters<typeof DeleteActionButton>[0]> = {}) {
  const onClick = vi.fn();
  const onParentClick = vi.fn();
  const view = render(
    <div onClick={onParentClick}>
      <DeleteActionButton onClick={onClick} ariaLabel="Delete transcript Weekly sync" {...props} />
    </div>,
    { wrapper: LocaleProvider },
  );
  const button = () => view.container.querySelector("button.delete-pill") as HTMLButtonElement;
  return { ...view, onClick, onParentClick, button };
}

function expectArmed(button: HTMLButtonElement) {
  expect(button).toHaveClass("is-confirming");
  expect(button).toHaveAttribute("data-confirming", "true");
  expect(button).toHaveAttribute("data-label", "Confirm?");
  expect(button.getAttribute("aria-label")).toContain("Confirm deletion");
}

function expectDisarmed(button: HTMLButtonElement) {
  expect(button).not.toHaveClass("is-confirming");
  expect(button).not.toHaveAttribute("data-confirming");
  expect(button).toHaveAttribute("data-label", "Delete");
  expect(button).toHaveAttribute("aria-label", "Delete transcript Weekly sync");
}

describe("DeleteActionButton", () => {
  afterEach(() => vi.useRealTimers());

  it("arms on the first click without deleting or reaching row handlers, then deletes on the second", () => {
    const { button, onClick, onParentClick } = renderButton();
    expectDisarmed(button());

    fireEvent.click(button());
    expect(onClick).not.toHaveBeenCalled();
    expect(onParentClick).not.toHaveBeenCalled();
    expectArmed(button());
    expect(screen.getByRole("button", { name: /Confirm deletion/ })).toBe(button());

    fireEvent.click(button());
    expect(onClick).toHaveBeenCalledOnce();
    expectDisarmed(button());
  });

  it("disarms on Escape without deleting", () => {
    const { button, onClick } = renderButton();
    fireEvent.click(button());
    expectArmed(button());

    fireEvent.keyDown(button(), { key: "Escape" });
    expectDisarmed(button());

    fireEvent.click(button());
    expect(onClick).not.toHaveBeenCalled();
    expectArmed(button());
  });

  it("disarms on blur", () => {
    const { button, onClick } = renderButton();
    fireEvent.click(button());
    expectArmed(button());

    fireEvent.blur(button());
    expectDisarmed(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("disarms automatically after the confirmation window", () => {
    vi.useFakeTimers();
    const { button, onClick } = renderButton();
    fireEvent.click(button());
    expectArmed(button());

    act(() => vi.advanceTimersByTime(DELETE_CONFIRM_TIMEOUT_MS - 1));
    expectArmed(button());
    act(() => vi.advanceTimersByTime(1));
    expectDisarmed(button());

    fireEvent.click(button());
    expect(onClick).not.toHaveBeenCalled();
  });

  it("disarms when it becomes disabled or starts loading", () => {
    const { button, onClick, rerender } = renderButton();
    fireEvent.click(button());
    expectArmed(button());

    rerender(
      <div>
        <DeleteActionButton onClick={onClick} ariaLabel="Delete transcript Weekly sync" loading />
      </div>,
    );
    expect(button()).not.toHaveClass("is-confirming");
    rerender(
      <div>
        <DeleteActionButton onClick={onClick} ariaLabel="Delete transcript Weekly sync" />
      </div>,
    );
    expectDisarmed(button());

    fireEvent.click(button());
    expectArmed(button());
    rerender(
      <div>
        <DeleteActionButton onClick={onClick} ariaLabel="Delete transcript Weekly sync" disabled />
      </div>,
    );
    expect(button()).not.toHaveClass("is-confirming");
    expect(onClick).not.toHaveBeenCalled();
  });

  it("clears its pending timer on unmount", () => {
    vi.useFakeTimers();
    const { button, unmount } = renderButton();
    fireEvent.click(button());
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("deletes immediately when confirmation is not required", () => {
    const { button, onClick } = renderButton({ requireConfirm: false });
    fireEvent.click(button());
    expect(onClick).toHaveBeenCalledOnce();
    expectDisarmed(button());
  });
});
