import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { YouTubeSearchItem } from "@/lib/api-types";
import { useBrowserYoutubeImport } from "./use-browser-youtube-import";

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
});
