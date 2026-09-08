import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InfoTooltip } from "./info-tooltip";

describe("InfoTooltip", () => {
  afterEach(() => vi.useRealTimers());

  it("exposes the complete side note on focus and dismisses with Escape without activating the setting", () => {
    render(
      <InfoTooltip label="Contributor data use">
        <p>Prompts and responses may be used to improve models.</p>
        <p>Choose Standard for confidential work.</p>
      </InfoTooltip>,
    );
    const trigger = screen.getByRole("button", { name: "Contributor data use" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.focus(trigger);
    const tooltip = screen.getByRole("tooltip");
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("Choose Standard for confidential work.");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(trigger).not.toHaveAttribute("aria-describedby");
  });

  it("supports tap, outside dismissal, and small-screen placement", () => {
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(320);
    render(
      <InfoTooltip label="Data region" compact>
        Region and API key must match.
      </InfoTooltip>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Data region" }));
    expect(screen.getByRole("tooltip")).toHaveStyle({ width: "296px", left: "12px" });
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("keeps the note open when the pointer moves from the trigger onto the note", () => {
    vi.useFakeTimers();
    render(<InfoTooltip label="Data use">A longer note can be read at the user's pace.</InfoTooltip>);
    const trigger = screen.getByRole("button");
    fireEvent.mouseEnter(trigger);
    fireEvent.mouseLeave(trigger);
    fireEvent.mouseEnter(screen.getByRole("tooltip"));
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByRole("tooltip"));
    act(() => vi.advanceTimersByTime(200));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("allows scrolling a long note but dismisses when the surrounding page moves", () => {
    render(<InfoTooltip label="Long guidance">Detailed account and privacy information.</InfoTooltip>);
    fireEvent.focus(screen.getByRole("button"));
    fireEvent.scroll(screen.getByRole("tooltip"));
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.scroll(window);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
