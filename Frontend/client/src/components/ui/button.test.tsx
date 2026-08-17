import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "./button";

describe("Button interaction polish", () => {
  it("uses the shared press and focus contract", () => {
    render(<Button>Save</Button>);

    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveClass("ui-pressable", "ui-hit-target");
    expect(button).toHaveClass("transition-[color,background-color,border-color,box-shadow,transform]");
    expect(button).toHaveClass("focus-visible:ring-2", "focus-visible:ring-offset-2");
    expect(button).toHaveClass("motion-reduce:transform-none");
    expect(button).toHaveClass("motion-reduce:transition-none");
  });

  it("keeps disabled controls visually stationary", () => {
    render(<Button disabled>Save</Button>);

    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    expect(button).toHaveClass("disabled:pointer-events-none", "disabled:opacity-50");
  });
});
