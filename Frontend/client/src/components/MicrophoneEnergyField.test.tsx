import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import MicrophoneEnergyField from "./MicrophoneEnergyField";

function createCanvasContext() {
  const gradient = {
    addColorStop: vi.fn(),
  } as unknown as CanvasGradient;
  const context = {
    arc: vi.fn(),
    beginPath: vi.fn(),
    clearRect: vi.fn(),
    createLinearGradient: vi.fn(() => gradient),
    createRadialGradient: vi.fn(() => gradient),
    fill: vi.fn(),
    fillStyle: "",
    globalAlpha: 1,
    globalCompositeOperation: "source-over",
    lineCap: "butt",
    lineJoin: "miter",
    lineTo: vi.fn(),
    lineWidth: 1,
    moveTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    setTransform: vi.fn(),
    stroke: vi.fn(),
    strokeStyle: "",
  } as unknown as CanvasRenderingContext2D;
  return { context, gradient };
}

describe("MicrophoneEnergyField", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders from the pill's far-left edge and reuses HiDPI drawing resources", () => {
    const animationFrames: FrameRequestCallback[] = [];
    const { context } = createCanvasContext();
    let now = 0;

    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context);
    vi.stubGlobal("devicePixelRatio", 3);
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      animationFrames.push(callback);
      return animationFrames.length;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    const { getByTestId } = render(
      <MicrophoneEnergyField
        active
        rmsRef={{ current: 0.01 }}
        width={203}
        height={41}
        plotX={0}
        plotY={6}
        plotWidth={203}
        plotHeight={29}
      />,
    );

    const canvas = getByTestId("native-recording-waveform") as HTMLCanvasElement;
    expect(canvas.width).toBe(609);
    expect(canvas.height).toBe(123);
    expect(canvas).toHaveStyle({
      inset: "0",
      width: "203px",
      height: "41px",
      pointerEvents: "none",
    });
    expect(context.createLinearGradient).toHaveBeenCalledTimes(1);
    expect(context.createRadialGradient).toHaveBeenCalledTimes(1);

    for (const frameTime of [17, 34, 51]) {
      const callback = animationFrames.shift();
      expect(callback).toBeTypeOf("function");
      now = frameTime;
      act(() => callback?.(frameTime));
    }

    expect(context.moveTo).toHaveBeenCalledWith(-8, expect.any(Number));
    expect(context.lineTo).toHaveBeenCalledWith(0, expect.any(Number));
    expect(context.createLinearGradient).toHaveBeenCalledTimes(1);
    expect(context.createRadialGradient).toHaveBeenCalledTimes(1);
  });
});
