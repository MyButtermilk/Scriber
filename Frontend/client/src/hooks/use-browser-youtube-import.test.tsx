import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { YouTubeSearchItem } from "@/lib/api-types";
import { useBrowserYoutubeImport } from "./use-browser-youtube-import";
import { apiRequest } from "@/lib/queryClient";

vi.mock("@/lib/queryClient", () => ({ apiRequest: vi.fn() }));

interface HarnessProps {
  busy?: boolean;
  onImport: (item: YouTubeSearchItem) => void;
}

function Harness({ busy = false, onImport }: HarnessProps) {
  useBrowserYoutubeImport({ busy, onImport });
  return null;
}

const requestSearch =
  "?browserVideo=0wEjbSYNUM8&browserRequest=0123456789abcdef0123456789abcdef" +
  "&browserTitle=Browser%20handoff&browserChannel=Scriber";

describe("useBrowserYoutubeImport", () => {
  beforeEach(() => {
    vi.mocked(apiRequest).mockReset().mockResolvedValue(new Response("{}"));
    window.history.replaceState(null, "", "/youtube");
  });

  it("consumes a query-only handoff while the YouTube route is already open", async () => {
    const onImport = vi.fn();
    render(<Harness onImport={onImport} />);

    act(() => {
      window.history.pushState(null, "", `/youtube${requestSearch}`);
    });

    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
    expect(onImport).toHaveBeenCalledWith(
      expect.objectContaining({
        videoId: "0wEjbSYNUM8",
        url: "https://www.youtube.com/watch?v=0wEjbSYNUM8",
        title: "Browser handoff",
        channelTitle: "Scriber",
      }),
    );
    expect(window.location.pathname).toBe("/youtube");
    expect(window.location.search).toBe("");
    expect(apiRequest).toHaveBeenCalledWith("POST", "/api/youtube/session/browser-accept", { videoId: "0wEjbSYNUM8" });
  });

  it("keeps a pending handoff until the current start request settles", async () => {
    const onImport = vi.fn();
    const view = render(<Harness busy onImport={onImport} />);

    act(() => {
      window.history.pushState(null, "", `/youtube${requestSearch}`);
    });
    expect(onImport).not.toHaveBeenCalled();
    expect(window.location.search).toBe(requestSearch);

    view.rerender(<Harness onImport={onImport} />);

    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
    expect(window.location.search).toBe("");
  });

  it("waits for the session handoff before extraction and ignores a later unmount", async () => {
    let finish!: (response: Response) => void;
    vi.mocked(apiRequest).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const onImport = vi.fn();
    const view = render(<Harness onImport={onImport} />);
    act(() => {
      window.history.pushState(null, "", `/youtube${requestSearch}`);
    });
    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(1));
    expect(onImport).not.toHaveBeenCalled();
    view.unmount();
    await act(async () => {
      finish(new Response("{}"));
    });
    expect(onImport).not.toHaveBeenCalled();
  });

  it("keeps older extensions usable when optional session transfer fails", async () => {
    vi.mocked(apiRequest).mockRejectedValue(new Error("unavailable"));
    const onImport = vi.fn();
    render(<Harness onImport={onImport} />);
    act(() => {
      window.history.pushState(null, "", `/youtube${requestSearch}`);
    });
    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
  });
});
