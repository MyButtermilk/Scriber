import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import MicrophoneBlueFlame from "./MicrophoneBlueFlame";

function drawingHarness(reducedMotion = false) {
  const frames: FrameRequestCallback[] = [];
  const gradient = { addColorStop: vi.fn() } as unknown as CanvasGradient;
  const context = {
    beginPath: vi.fn(),
    clearRect: vi.fn(),
    clip: vi.fn(),
    createLinearGradient: vi.fn(() => gradient),
    fillRect: vi.fn(),
    lineTo: vi.fn(),
    moveTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    restore: vi.fn(),
    roundRect: vi.fn(),
    save: vi.fn(),
    setTransform: vi.fn(),
    stroke: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
  const getContext = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context);
  vi.spyOn(performance, "now").mockReturnValue(0);
  vi.stubGlobal("devicePixelRatio", 4);
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: reducedMotion, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    }),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const frame = (time: number) => act(() => frames.shift()?.(time));
  return { context, getContext, frame };
}

describe("MicrophoneBlueFlame", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("caps high-DPI rendering at 3 and high-refresh drawing at 60 fps, with rounded alpha composition", () => {
    const { context, getContext, frame } = drawingHarness();
    const { getByTestId, unmount } = render(
      <MicrophoneBlueFlame active rmsRef={{ current: 0.01 }} width={203} height={41} />,
    );
    const canvas = getByTestId("native-recording-blue-flame");
    expect(canvas).toHaveAttribute("width", "609");
    expect(canvas).toHaveAttribute("height", "123");
    expect(getContext).toHaveBeenCalledWith("2d", { alpha: true });
    frame(0);
    frame(8);
    frame(16);
    frame(24);
    frame(32);
    frame(40);
    frame(48);
    expect(context.clearRect).toHaveBeenCalledTimes(3);
    expect(context.roundRect).toHaveBeenCalledWith(0, 0, 203, 41, 20.5);
    expect(context.createLinearGradient).toHaveBeenCalledTimes(3);
    expect(context.moveTo).toHaveBeenCalledWith(-4, expect.any(Number));
    expect(canvas).toHaveStyle({ clipPath: "inset(0 round 20.5px)", pointerEvents: "none" });
    unmount();
    expect(cancelAnimationFrame).toHaveBeenCalled();
  });

  it("leaves a quiet pill at silence and reduces motion to a settled four-Hz refresh", () => {
    const { context, frame } = drawingHarness(true);
    render(<MicrophoneBlueFlame active rmsRef={{ current: 0 }} width={203} height={41} />);
    frame(0);
    frame(16);
    frame(100);
    frame(249);
    frame(250);
    expect(context.fillRect).toHaveBeenCalledTimes(2);
    expect(context.stroke).not.toHaveBeenCalled();
  });

  it("does not acquire a canvas or schedule work while inactive", () => {
    const { getContext } = drawingHarness();
    render(<MicrophoneBlueFlame active={false} rmsRef={{ current: 1 }} width={203} height={41} />);
    expect(getContext).not.toHaveBeenCalled();
    expect(requestAnimationFrame).not.toHaveBeenCalled();
  });
});
