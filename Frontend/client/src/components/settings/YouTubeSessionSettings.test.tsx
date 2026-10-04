import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { YouTubeSessionSettings } from "./YouTubeSessionSettings";

vi.mock("@/i18n", () => ({ useI18n: () => ({ t: (value: string) => value }) }));
const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/queryClient", () => ({ apiRequest: request }));

describe("YouTube session recovery", () => {
  it("waits for an explicit import, filters other sites, then allows clearing", async () => {
    request.mockReset();
    const client = new QueryClient({
      defaultOptions: { queries: { queryFn: async () => ({ connected: false }), retry: false } },
    });
    request.mockResolvedValueOnce({ json: async () => ({ connected: true }) });
    render(
      <QueryClientProvider client={client}>
        <YouTubeSessionSettings />
      </QueryClientProvider>,
    );
    await screen.findByText("No YouTube sign-in loaded");
    expect(request).not.toHaveBeenCalled();
    const file = new File(["fixture"], "cookies.txt", { type: "text/plain" });
    const cookies =
      "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyt-value\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tforeign-secret";
    Object.defineProperty(file, "text", { value: async () => cookies });
    fireEvent.change(screen.getByLabelText("YouTube sign-in file"), { target: { files: [file] } });
    await screen.findByText("YouTube sign-in loaded");
    expect(request).toHaveBeenCalledWith("POST", "/api/youtube/session", {
      cookies: cookies.split("\n").slice(0, 2).join("\n"),
    });
    request.mockResolvedValueOnce({ json: async () => ({ connected: false }) });
    fireEvent.click(screen.getByRole("button", { name: "Clear YouTube sign-in" }));
    await waitFor(() => expect(screen.queryByText("YouTube sign-in loaded")).toBeNull());
    expect(request).toHaveBeenLastCalledWith("DELETE", "/api/youtube/session");
    client.clear();
  });
});
