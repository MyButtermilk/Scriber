import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WavePhysicsLoader } from "./wave-physics-loader";

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      addEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: query === "(prefers-reduced-motion: reduce)" && reduce,
      media: query,
      onchange: null,
      removeEventListener: vi.fn(),
    })),
  );
}

describe("WavePhysicsLoader", () => {
  beforeEach(() => {
    mockReducedMotion(false);
  });

  it("renders a compact accessible loading status", () => {
    render(<WavePhysicsLoader label="Loading results" />);

    expect(screen.getByRole("status", { name: "Loading results" })).toHaveAttribute("data-size", "compact");
    expect(screen.getByTestId("wave-physics-loader").querySelectorAll("[class*='origin-bottom']")).toHaveLength(11);
  });

  it("stays decorative when the surrounding control provides the label", () => {
    render(<WavePhysicsLoader size="inline" />);

    expect(screen.getByTestId("wave-physics-loader")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("supports the static reduced-motion fallback", () => {
    mockReducedMotion(true);
    render(<WavePhysicsLoader size="panel" label="Loading" />);

    expect(screen.getByRole("status", { name: "Loading" })).toHaveAttribute("data-size", "panel");
  });
});
