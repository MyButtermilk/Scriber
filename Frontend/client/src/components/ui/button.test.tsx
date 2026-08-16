import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "./button";

describe("Button interaction polish", () => {
  it("uses a restrained interruptible press transition", () => {
    render(<Button>Save</Button>);

    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveClass("active:scale-[var(--scale-large)]");
    expect(button).toHaveClass("transition-[color,background-color,border-color,box-shadow,opacity,scale]");
    expect(button).toHaveClass("motion-reduce:scale-100");
    expect(button).toHaveClass("motion-reduce:transition-none");
  });

  it("keeps disabled controls visually stationary", () => {
    render(<Button disabled>Save</Button>);

    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    expect(button).toHaveClass("disabled:scale-100");
  });
});
