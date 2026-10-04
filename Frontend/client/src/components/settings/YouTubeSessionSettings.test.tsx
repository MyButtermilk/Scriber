import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { YouTubeSessionSettings } from "./YouTubeSessionSettings";

vi.mock("@/i18n", () => ({ useI18n: () => ({ t: (value: string) => value }) }));
const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/queryClient", () => ({ apiRequest: request }));
vi.mock("@/lib/backend", () => ({ isTauriRuntime: () => false }));

const loginKey = ["/api/youtube/session/login"];
const idle = { state: "idle", error: "", available: true, attempt: 0 };

function setup(onConnected = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        queryFn: async ({ queryKey }) => (queryKey[0] === loginKey[0] ? idle : { connected: false }),
        retry: false,
      },
    },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <YouTubeSessionSettings onConnected={onConnected} />
    </QueryClientProvider>,
  );
  return { client, onConnected, ...view };
}

describe("YouTube session recovery", () => {
  beforeEach(() => request.mockReset());

  it("offers the official store link and starts sign-in without an add-on only on request", async () => {
    const { client, onConnected } = setup();
    await screen.findByText("No YouTube sign-in loaded");
    expect(request).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Open add-on in Chrome Web Store" }).getAttribute("href")).toBe(
      "https://chromewebstore.google.com/detail/scriber-f%C3%BCr-youtube/ilbdnbhdihacgkaedacmndeabbiondob",
    );
    request.mockResolvedValueOnce({ json: async () => ({ ...idle, state: "opening", attempt: 1 }) });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to YouTube" }));
    await screen.findByText("Complete sign-in in the browser. This video will then restart automatically.");
    expect(request).toHaveBeenCalledWith("POST", loginKey[0]);
    expect(onConnected).not.toHaveBeenCalled();
    act(() => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 1 });
    });
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1));
    act(() => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 1 });
    });
    expect(onConnected).toHaveBeenCalledTimes(1);
    client.clear();
  });

  it("ignores old completion and cancels the active sign-in without retrying the video", async () => {
    const { client, onConnected } = setup();
    act(() => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 1 });
    });
    expect(onConnected).not.toHaveBeenCalled();
    request.mockResolvedValueOnce({ json: async () => ({ ...idle, state: "waiting", attempt: 2 }) });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to YouTube" }));
    const cancel = await screen.findByRole("button", { name: "Cancel sign-in" });
    request.mockResolvedValueOnce({ json: async () => ({ ...idle, attempt: 2 }) });
    fireEvent.click(cancel);
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("DELETE", loginKey[0]));
    act(() => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 2 });
    });
    expect(onConnected).not.toHaveBeenCalled();
    client.clear();
  });

  it("keeps the store and import options available if no browser can open", async () => {
    const { client, onConnected } = setup();
    request.mockResolvedValueOnce({
      json: async () => ({ ...idle, state: "failed", available: false, error: "browser_missing" }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to YouTube" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Chrome or Edge is needed");
    expect(screen.getByRole("link", { name: "Open add-on in Chrome Web Store" })).toBeInTheDocument();
    expect(screen.getByText("Advanced: import a sign-in file")).toBeInTheDocument();
    expect(onConnected).not.toHaveBeenCalled();
    client.clear();
  });

  it("does not resume a video after its view has unmounted", async () => {
    const { client, onConnected, unmount } = setup();
    request.mockResolvedValueOnce({ json: async () => ({ ...idle, state: "waiting", attempt: 3 }) });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to YouTube" }));
    await screen.findByRole("button", { name: "Cancel sign-in" });
    unmount();
    act(() => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 3 });
    });
    expect(onConnected).not.toHaveBeenCalled();
    client.clear();
  });

  it("does not give an earlier video's sign-in attempt to the next video's callback", async () => {
    const { client, onConnected: first, rerender } = setup();
    const second = vi.fn();
    request.mockResolvedValueOnce({ json: async () => ({ ...idle, state: "waiting", attempt: 4 }) });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to YouTube" }));
    await screen.findByRole("button", { name: "Cancel sign-in" });
    rerender(
      <QueryClientProvider client={client}>
        <YouTubeSessionSettings recoveryKey="next-video" onConnected={second} />
      </QueryClientProvider>,
    );
    await act(async () => {
      client.setQueryData(loginKey, { ...idle, state: "connected", attempt: 4 });
    });
    expect(first).not.toHaveBeenCalled();
    expect(second).not.toHaveBeenCalled();
    client.clear();
  });

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
