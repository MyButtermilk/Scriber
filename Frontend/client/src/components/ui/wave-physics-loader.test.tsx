import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WavePhysicsLoader } from "./wave-physics-loader";

const reducedMotion = vi.hoisted(() => ({ enabled: false }));
vi.mock("motion/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("motion/react")>()),
  useReducedMotion: () => reducedMotion.enabled,
}));

describe("WavePhysicsLoader", () => {
  beforeEach(() => {
    reducedMotion.enabled = false;
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

  it("settles every animated bar and the ball when reduced motion is requested", async () => {
    reducedMotion.enabled = true;
    render(<WavePhysicsLoader size="panel" label="Loading" />);

    const loader = screen.getByRole("status", { name: "Loading" });
    const bars = loader.querySelectorAll<HTMLElement>(".origin-bottom");
    expect(bars.length).toBeGreaterThan(0);
    await waitFor(() => {
      const transforms = Array.from(bars, (bar) => bar.style.transform);
      expect(transforms[0]).toMatch(/^scaleY\([\d.]+\)$/);
      expect(new Set(transforms).size).toBe(1);
    });
    const ball = loader.querySelector<HTMLElement>(".absolute")!;
    expect(ball.style.transform).toMatch(/translateX\([\d.]+px\) translateY\(-[\d.]+px\)/);
    expect(ball.style.transform).not.toMatch(/scale/);
  });
});
