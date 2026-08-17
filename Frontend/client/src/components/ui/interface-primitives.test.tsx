import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageIntro } from "@/components/page-intro";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CardDescription, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Toggle } from "@/components/ui/toggle";

describe("interface primitives", () => {
  it("shares visible focus and target-size contracts across pressable controls", () => {
    render(
      <>
        <Button aria-label="Start recording" size="icon">
          <span aria-hidden="true">R</span>
        </Button>
        <Toggle aria-label="Grid view">Grid</Toggle>
      </>,
    );

    for (const control of screen.getAllByRole("button")) {
      expect(control).toHaveClass("ui-pressable", "ui-hit-target");
      expect(control.className).toContain("focus-visible:ring-2");
      expect(control.className).toContain("focus-visible:ring-offset-2");
    }
  });

  it("keeps invalid fields explicit without replacing the native aria state", () => {
    render(
      <>
        <Input aria-label="API key" aria-invalid="true" />
        <Textarea aria-label="Prompt" aria-invalid="true" />
      </>,
    );

    for (const field of [
      screen.getByRole("textbox", { name: "API key" }),
      screen.getByRole("textbox", { name: "Prompt" }),
    ]) {
      expect(field).toHaveAttribute("aria-invalid", "true");
      expect(field).toHaveClass("ui-field", "ui-hover-field");
      expect(field.className).toContain("aria-invalid:border-destructive");
    }
  });

  it("keeps page titles and actions in a non-overlapping responsive grid", () => {
    render(
      <PageIntro
        eyebrow="Workspace"
        title="A long title that still needs room for actions"
        description="The description should remain readable while actions occupy their own grid column."
        sticky={false}
        actions={<Button>Refresh</Button>}
      />,
    );

    const header = document.querySelector('[data-page-intro="true"]');
    const layout = header?.firstElementChild;

    expect(layout).toHaveClass("grid");
    expect(layout?.className).toContain("md:grid-cols-[minmax(0,1fr)_auto]");
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass("text-balance", "break-words");
    expect(screen.getByText(/The description/)).toHaveClass(
      "break-words",
      "text-pretty",
      "text-[13px]",
      "md:text-[14px]",
    );
  });

  it("wraps card copy safely and keeps passive badges visually quiet", () => {
    render(
      <>
        <CardTitle>Very long provider model identifier that must wrap safely</CardTitle>
        <CardDescription>Descriptive copy should wrap naturally without breaking the card layout.</CardDescription>
        <Badge>12345</Badge>
      </>,
    );

    expect(screen.getByText(/Very long provider/)).toHaveClass("text-balance", "break-words");
    expect(screen.getByText(/Descriptive copy/)).toHaveClass("text-pretty", "break-words");

    const badge = screen.getByText("12345");
    expect(badge).toHaveClass("tabular-nums");
    expect(badge).not.toHaveClass("hover-elevate");
  });
});
