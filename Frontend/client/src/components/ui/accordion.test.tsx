import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";

describe("Accordion transitions", () => {
  it("keeps the grid panel mounted but removes closed content from interaction and accessibility", () => {
    const { container } = render(
      <Accordion type="single" collapsible>
        <AccordionItem value="details">
          <AccordionTrigger>Details</AccordionTrigger>
          <AccordionContent>
            <button type="button">Inside action</button>
          </AccordionContent>
        </AccordionItem>
      </Accordion>,
    );
    const trigger = screen.getByRole("button", { name: "Details" });
    const panel = container.querySelector(".t-acc-panel");
    const presence = container.querySelector(".t-acc-presence");

    expect(container.querySelector(".t-acc")).not.toBeNull();
    expect(container.querySelector(".t-acc-chevron")).not.toBeNull();
    expect(presence).not.toBe(panel);
    expect(presence).toContainElement(panel as HTMLElement);
    expect(presence).toHaveAttribute("aria-hidden", "true");
    expect(presence).toHaveAttribute("inert");
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(panel).toHaveAttribute("inert");
    expect(screen.queryByRole("button", { name: "Inside action" })).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(panel).not.toHaveAttribute("aria-hidden");
    expect(panel).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Inside action" })).toBeInTheDocument();

    fireEvent.click(trigger);
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("button", { name: "Inside action" })).not.toBeInTheDocument();
  });
});
