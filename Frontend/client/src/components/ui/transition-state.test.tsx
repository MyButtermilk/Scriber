import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/i18n";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { SuccessCheckIcon, TransitionText } from "@/components/ui/transition-state";

describe("transitions.dev state primitives", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps both copy icons mounted and changes only the documented state hook", () => {
    const { container, rerender } = render(
      <LocaleProvider>
        <CopyActionButton ariaLabel="Copy transcript" copied={false} onClick={vi.fn()} />
      </LocaleProvider>,
    );

    expect(container.querySelectorAll(".t-icon")).toHaveLength(2);
    expect(container.querySelector(".t-icon-swap")).toHaveAttribute("data-state", "a");

    rerender(
      <LocaleProvider>
        <CopyActionButton ariaLabel="Copy transcript" copied onClick={vi.fn()} />
      </LocaleProvider>,
    );

    expect(container.querySelectorAll(".t-icon")).toHaveLength(2);
    expect(container.querySelector(".t-icon-swap")).toHaveAttribute("data-state", "b");
  });

  it("runs the exit and entry phases while exposing the next accessible label immediately", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(<TransitionText value="Save" />);
    const text = container.querySelector(".t-text-swap");

    rerender(<TransitionText value="Saved" />);
    expect(text).toHaveClass("is-exit");
    expect(text).toHaveAttribute("aria-label", "Saved");
    expect(text).toHaveTextContent("Save");

    act(() => vi.advanceTimersByTime(150));
    expect(text).not.toHaveClass("is-exit");
    expect(text).toHaveTextContent("Saved");
  });

  it("cancels an interrupted text swap without leaving the current value hidden", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(<TransitionText value="Save" />);
    const text = container.querySelector(".t-text-swap");

    rerender(<TransitionText value="Saved" />);
    expect(text).toHaveClass("is-exit");

    rerender(<TransitionText value="Save" />);
    expect(text).not.toHaveClass("is-exit");
    expect(text).not.toHaveClass("is-enter-start");
    expect(text).toHaveTextContent("Save");

    act(() => vi.runAllTimers());
    expect(text).toHaveTextContent("Save");
  });

  it("keeps an already-visible check steady on remount and animates a later success", () => {
    const mounted = render(<SuccessCheckIcon visible animateOnMount={false} />);
    expect(mounted.container.querySelector(".t-success-check")).toHaveAttribute("data-state", "steady");
    mounted.unmount();

    const { container, rerender } = render(<SuccessCheckIcon visible={false} animateOnMount={false} />);
    expect(container.querySelector(".t-success-check")).toHaveAttribute("data-state", "out");
    rerender(<SuccessCheckIcon visible animateOnMount={false} />);
    expect(container.querySelector(".t-success-check")).toHaveAttribute("data-state", "in");
  });
});
