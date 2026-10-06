import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { InfoTooltip } from "@/components/ui/info-tooltip";
import { invalidateSettingsBootstrap } from "@/lib/settings-bootstrap";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { getAutostartStatus } from "@/lib/backend";
import { defaultPostProcessingPrompt } from "@/lib/settings-presentation";
import Settings from "./Settings";

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/contexts/WebSocketContext", () => ({ useSharedWebSocket: () => ({ isConnected: true }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/lib/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/backend")>()),
  getAutostartStatus: vi.fn(),
}));
// Tooltips appear throughout every Settings section; their render count shows
// whether a keystroke reconciled the page rather than only the edited field.
vi.mock("@/components/ui/info-tooltip", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/info-tooltip")>();
  return { ...actual, InfoTooltip: vi.fn(actual.InfoTooltip) };
});

let client: QueryClient;
let finishMicrophones: (response: Response) => void;
let finishAutostart: (status: { enabled: boolean; available: boolean }) => void;

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  invalidateSettingsBootstrap();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const microphones = new Promise<Response>((resolve) => {
    finishMicrophones = resolve;
  });
  vi.mocked(getAutostartStatus).mockReturnValue(
    new Promise((resolve) => {
      finishAutostart = resolve;
    }),
  );
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) => {
    const path = String(url);
    if (path.endsWith("/api/settings"))
      return Response.json({ hotkey: "Ctrl + Alt + X", apiKeys: { openai: "saved-key" } });
    if (path.endsWith("/api/microphones")) return microphones;
    return Response.json({ items: [], models: [], available: false, profiles: [] });
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  client.clear();
  invalidateSettingsBootstrap();
});

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Settings />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

it("shows persisted settings before slow devices and autostart, and keeps them after device failure", async () => {
  const view = mount();
  const shell = view.container.querySelector('[data-page-shell="settings"]')!;
  await waitFor(() => expect(shell).toHaveClass("opacity-100"));
  expect(view.container.textContent).toContain("Ctrl + Alt + X");
  expect(toast).not.toHaveBeenCalled();
  await act(async () => {
    finishMicrophones(new Response("device failed", { status: 500 }));
    finishAutostart({ enabled: true, available: true });
  });
  expect(shell).toHaveClass("opacity-100");
  expect(view.container.textContent).toContain("Ctrl + Alt + X");
  expect(toast).toHaveBeenCalledTimes(1);
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Failed to load microphones" }));
});

it("reports only the primary settings error when settings and microphones both fail", async () => {
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    String(url).endsWith("/api/settings")
      ? new Response("settings failed", { status: 500 })
      : String(url).endsWith("/api/microphones")
        ? new Response("devices failed", { status: 500 })
        : Response.json({ items: [], models: [], available: false, profiles: [] }),
  );
  mount();
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Failed to load settings" }));
});

function settingsPuts(): Array<Record<string, unknown>> {
  return vi
    .mocked(fetchWithTimeout)
    .mock.calls.filter(([url, init]) => String(url).endsWith("/api/settings") && init?.method === "PUT")
    .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);
}

async function mountLoaded(settings: Record<string, unknown> = {}) {
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) => {
    const path = String(url);
    if (path.endsWith("/api/settings")) {
      return Response.json({ hotkey: "Ctrl + Alt + X", apiKeys: { openai: "saved-key" }, ...settings });
    }
    if (path.endsWith("/api/microphones")) return Response.json({ devices: [] });
    return Response.json({ items: [], models: [], available: false, profiles: [] });
  });
  const view = mount();
  await waitFor(() => expect(view.container.querySelector('[data-page-shell="settings"]')).toHaveClass("opacity-100"));
  await act(async () => {
    finishAutostart({ enabled: false, available: true });
  });
  return view;
}

function cleanupPromptField(locale: "de" | "en"): HTMLTextAreaElement {
  // The multi-line default prompt is the field's placeholder; compare it exactly.
  const field = Array.from(document.querySelectorAll("textarea")).find(
    (candidate) => candidate.placeholder === defaultPostProcessingPrompt(locale),
  );
  if (!field) throw new Error("Live cleanup prompt field not found");
  return field;
}

function typeInto(field: HTMLTextAreaElement | HTMLInputElement, text: string): string {
  let value = field.value;
  for (const character of text) {
    value += character;
    fireEvent.change(field, { target: { value } });
  }
  return value;
}

const typedPhrase = "Scriber, Pipecat, MAI-2";

it("typing custom vocabulary renders only its field and autosaves the final text once", async () => {
  await mountLoaded({ customVocab: "Saved" });
  const field = screen.getByPlaceholderText<HTMLTextAreaElement>("Enter terms, one per line...");
  expect(field.value).toBe("Saved");
  vi.mocked(InfoTooltip).mockClear();

  const finalValue = typeInto(field, `\n${typedPhrase}`);

  expect(field.value).toBe(finalValue);
  // The original page re-rendered every Settings section for each character.
  expect(vi.mocked(InfoTooltip).mock.calls.length).toBe(0);
  expect(settingsPuts()).toEqual([]);
  await waitFor(() => expect(settingsPuts()).toEqual([{ customVocab: finalValue }]), { timeout: 3000 });

  fireEvent.blur(field);
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 50));
  });
  expect(settingsPuts()).toEqual([{ customVocab: finalValue }]);
});

it("custom vocabulary blur saves the newest text before the debounce without a duplicate later", async () => {
  await mountLoaded();
  const field = screen.getByPlaceholderText<HTMLTextAreaElement>("Enter terms, one per line...");

  const finalValue = typeInto(field, typedPhrase);
  fireEvent.blur(field);

  await waitFor(() => expect(settingsPuts()).toEqual([{ customVocab: finalValue }]));
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 900));
  });
  expect(settingsPuts()).toEqual([{ customVocab: finalValue }]);
});

it("prompt fields keep keystrokes local and persist the newest text on blur and reset", async () => {
  await mountLoaded({ summarizationPrompt: "Old summary prompt", postProcessingPrompt: "Old cleanup ${output}" });
  const summaryPrompt = screen.getByPlaceholderText<HTMLTextAreaElement>(
    "Summarize the key points, decisions, and action items. Keep it concise and structured.",
  );
  const cleanupPrompt = cleanupPromptField("en");
  expect(summaryPrompt.value).toBe("Old summary prompt");
  expect(cleanupPrompt.value).toBe("Old cleanup ${output}");
  vi.mocked(InfoTooltip).mockClear();

  const summaryValue = typeInto(summaryPrompt, ` ${typedPhrase}`);
  const cleanupValue = typeInto(cleanupPrompt, ` ${typedPhrase}`);

  expect(vi.mocked(InfoTooltip).mock.calls.length).toBe(0);
  fireEvent.blur(summaryPrompt);
  fireEvent.blur(cleanupPrompt);
  await waitFor(() =>
    expect(settingsPuts()).toEqual([{ summarizationPrompt: summaryValue }, { postProcessingPrompt: cleanupValue }]),
  );

  fireEvent.click(screen.getByRole("button", { name: "Reset prompt" }));
  expect(cleanupPrompt.value).toBe(defaultPostProcessingPrompt("en"));
  await waitFor(() =>
    expect(settingsPuts().at(-1)).toEqual({ postProcessingPrompt: defaultPostProcessingPrompt("en") }),
  );
});

it("replaces a stored default cleanup prompt from another interface language once loaded", async () => {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, "de");
  await mountLoaded({ postProcessingPrompt: defaultPostProcessingPrompt("en") });

  const germanDefault = defaultPostProcessingPrompt("de");
  await waitFor(() => expect(settingsPuts()).toEqual([{ postProcessingPrompt: germanDefault }]));
  await waitFor(() => expect(cleanupPromptField("de").value).toBe(germanDefault));
});

it("custom OpenRouter model input updates its own action and saves the canonical code", async () => {
  await mountLoaded({ apiKeys: { openai: "saved-key", openrouter: "saved-router-key" } });
  const field = document.querySelector<HTMLInputElement>("#summary-custom-openrouter-model")!;
  const form = field.closest("form")!;
  const useButton = () => form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  expect(useButton()).toBeDisabled();
  vi.mocked(InfoTooltip).mockClear();

  typeInto(field, "not a model");
  expect(useButton()).toBeEnabled();
  expect(vi.mocked(InfoTooltip).mock.calls.length).toBe(0);
  fireEvent.click(useButton());
  expect(await screen.findByText("Enter a canonical OpenRouter model code such as author/model.")).toBeInTheDocument();

  fireEvent.change(field, { target: { value: "" } });
  expect(screen.queryByText("Enter a canonical OpenRouter model code such as author/model.")).toBeNull();
  expect(useButton()).toBeDisabled();
  typeInto(field, "author/model:nitro");
  fireEvent.click(useButton());

  expect(field.value).toBe("author/model");
  await waitFor(() => expect(settingsPuts()).toEqual([{ summarizationModel: "author/model" }]));
});

it("ignores a late microphone error after leaving settings", async () => {
  const view = mount();
  await waitFor(() => expect(view.container.querySelector('[data-page-shell="settings"]')).toHaveClass("opacity-100"));
  view.unmount();
  await act(async () => {
    finishMicrophones(new Response("device failed", { status: 500 }));
    finishAutostart({ enabled: false, available: true });
  });
  expect(toast).not.toHaveBeenCalled();
});
