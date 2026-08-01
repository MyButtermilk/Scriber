import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocalPolishingSettings } from "@/components/settings/LocalPolishingSettings";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { LocalPolishingModelsResponse } from "@/lib/api-types";

const readyCatalog: LocalPolishingModelsResponse = {
  available: true,
  currentVariant: "q8_0",
  models: [
    {
      variant: "q8_0",
      status: "ready",
      installed: true,
      active: true,
      runtimeReady: true,
      sizeBytes: 291_545_440,
    },
    { variant: "bf16", status: "not_installed", installed: false },
  ],
};

function renderSettings(overrides: Partial<ComponentProps<typeof LocalPolishingSettings>> = {}) {
  const props: ComponentProps<typeof LocalPolishingSettings> = {
    enabled: true,
    engine: "cloud",
    selectedVariant: "q8_0",
    catalog: readyCatalog,
    loading: false,
    error: "",
    onEngineChange: vi.fn(),
    onInstall: vi.fn(),
    onCancel: vi.fn(),
    onUse: vi.fn(),
    onRemove: vi.fn(),
    onRetry: vi.fn(),
    ...overrides,
  };
  render(
    <LocaleProvider>
      <LocalPolishingSettings {...props} />
    </LocaleProvider>,
  );
  return props;
}

describe("LocalPolishingSettings", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it("downloads a model without activating it", () => {
    const props = renderSettings();
    const bf16Card = screen.getByTestId("local-polishing-model-bf16");

    fireEvent.click(within(bf16Card).getByRole("button", { name: "Download" }));

    expect(props.onInstall).toHaveBeenCalledWith("bf16");
    expect(props.onUse).not.toHaveBeenCalled();
    expect(props.onEngineChange).not.toHaveBeenCalled();
  });

  it("describes anonymous downloads and the exact local-processing privacy boundary", () => {
    renderSettings();

    expect(
      screen.getByText(
        "Polishing runs on this device, so transcript text is not sent to a polishing provider. Cloud speech recognition may still upload audio. The public model download needs no Hugging Face account.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Audio stays local/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in|log in|token|credential/i })).not.toBeInTheDocument();
  });

  it("shows the Gemma use-policy notice with safe official links but no acceptance checkbox", () => {
    renderSettings();

    expect(screen.getByText(/By clicking Download, you accept Google's/)).toBeInTheDocument();
    const termsLink = screen.getByRole("link", { name: "Gemma Terms of Use" });
    const prohibitedUseLink = screen.getByRole("link", { name: "Gemma Prohibited Use Policy" });
    expect(termsLink).toHaveAttribute("href", "https://ai.google.dev/gemma/terms");
    expect(prohibitedUseLink).toHaveAttribute("href", "https://ai.google.dev/gemma/prohibited_use_policy");
    for (const link of [termsLink, prohibitedUseLink]) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("marks Q8 as recommended and BF16 as optional", () => {
    renderSettings();

    const q8Card = screen.getByTestId("local-polishing-model-q8_0");
    const bf16Card = screen.getByTestId("local-polishing-model-bf16");
    expect(within(q8Card).getByText("Recommended")).toBeInTheDocument();
    expect(within(bf16Card).getByText("Optional")).toBeInTheDocument();
  });

  it("requires an explicit use action and protects the active model from removal", () => {
    const props = renderSettings({ engine: "local" });
    const q8Card = screen.getByTestId("local-polishing-model-q8_0");

    expect(within(q8Card).getByRole("button", { name: "In use" })).toBeDisabled();
    expect(within(q8Card).getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(props.onUse).not.toHaveBeenCalled();
  });

  it("shows cancellable progress", () => {
    const onCancel = vi.fn();
    renderSettings({
      onCancel,
      catalog: {
        available: true,
        models: [
          {
            variant: "q8_0",
            status: "downloading",
            operationId: "operation-17",
            progress: 38,
          },
          { variant: "bf16", status: "not_installed" },
        ],
      },
    });

    const q8Card = screen.getByTestId("local-polishing-model-q8_0");
    expect(within(q8Card).getByRole("progressbar", { name: "Q8_0 download progress" })).toHaveAttribute(
      "aria-valuenow",
      "38",
    );
    fireEvent.click(within(q8Card).getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledWith("operation-17");
  });
});
