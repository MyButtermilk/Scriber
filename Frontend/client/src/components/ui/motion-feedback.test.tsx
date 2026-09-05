import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnimatedCheckbox } from "@/components/ui/animated-checkbox";
import { ErrorShake } from "@/components/ui/error-shake";
import { TransitionTooltip } from "@/components/ui/transition-tooltip";

describe("transitions.dev feedback primitives", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("connects a persistent tooltip to its trigger", () => {
    render(
      <TransitionTooltip content="Refresh logs">
        <button type="button">Refresh</button>
      </TransitionTooltip>,
    );

    const trigger = screen.getByRole("button", { name: "Refresh" });
    const tooltip = screen.getByRole("tooltip");
    expect(trigger).toHaveClass("t-tt-trigger");
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("Refresh logs");
  });

  it("does not repeat an identical accessible name as the tooltip description", () => {
    const { container } = render(
      <TransitionTooltip content="Refresh logs">
        <button type="button" aria-label="Refresh logs">
          Refresh
        </button>
      </TransitionTooltip>,
    );

    expect(screen.getByRole("button", { name: "Refresh logs" })).not.toHaveAttribute("aria-describedby");
    expect(container.querySelector('[role="tooltip"]')).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("keeps the checkbox path mounted while its semantic state changes", () => {
    const onClick = vi.fn();
    const { container, rerender } = render(
      <AnimatedCheckbox ariaLabel="Complete action item" checked={false} onClick={onClick} />,
    );
    const checkbox = screen.getByRole("checkbox", { name: "Complete action item" });
    const path = container.querySelector("path");

    expect(checkbox).toHaveAttribute("aria-checked", "false");
    fireEvent.click(checkbox);
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(<AnimatedCheckbox ariaLabel="Reopen action item" checked onClick={onClick} />);
    expect(screen.getByRole("checkbox", { name: "Reopen action item" })).toHaveAttribute("aria-checked", "true");
    expect(container.querySelector("path")).toBe(path);
  });

  it("replays the shake independently from the persistent error state", () => {
    vi.useFakeTimers();
    const reflow = vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(120);
    const { container, rerender } = render(
      <ErrorShake active trigger={{ attempt: 1 }}>
        <div role="alert">Unsupported file</div>
      </ErrorShake>,
    );
    const input = container.querySelector(".t-input");

    expect(input).toHaveClass("is-error", "is-shaking");
    rerender(
      <ErrorShake active trigger={{ attempt: 2 }}>
        <div role="alert">Unsupported file</div>
      </ErrorShake>,
    );
    expect(reflow).toHaveBeenCalledTimes(2);
    expect(input).toHaveClass("is-error", "is-shaking");

    act(() => vi.advanceTimersByTime(300));
    expect(input).toHaveClass("is-error");
    expect(input).not.toHaveClass("is-shaking");
  });

  it("removes the transient shake immediately when the error becomes inactive", () => {
    vi.useFakeTimers();
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(120);
    const { container, rerender } = render(
      <ErrorShake active trigger="rejected">
        <div role="alert">Unsupported file</div>
      </ErrorShake>,
    );
    const input = container.querySelector(".t-input");

    expect(input).toHaveClass("is-shaking");
    rerender(
      <ErrorShake active={false} trigger="cleared">
        <div>Ready</div>
      </ErrorShake>,
    );

    expect(input).not.toHaveClass("is-error", "is-shaking");
  });
});
