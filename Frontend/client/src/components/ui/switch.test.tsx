import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Switch } from "@/components/ui/switch";

describe("Switch", () => {
  it("keeps the unchecked state neutral and exposes the checked state semantically", () => {
    const { container } = render(<Switch aria-label="Automatic checks" />);
    const control = screen.getByRole("switch", { name: "Automatic checks" });

    expect(control).toHaveAttribute("data-state", "unchecked");
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(container.querySelector(".impact-echo-switch__icon--x")).toBeNull();
    expect(container.querySelector(".impact-echo-switch__ambient-glow")).toBeNull();

    fireEvent.click(control);

    expect(control).toHaveAttribute("data-state", "checked");
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(container.querySelector(".impact-echo-switch__icon--check")).not.toBeNull();
  });
});
