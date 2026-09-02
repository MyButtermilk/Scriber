import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocalPolishingSettings } from "@/components/settings/LocalPolishingSettings";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { LocalPolishingModelsResponse } from "@/lib/api-types";

const readyCatalog: LocalPolishingModelsResponse = {
  available: true,
  currentVariant: "qad_q4_0",
  models: [
    {
      variant: "qad_q4_0",
      status: "ready",
      installed: true,
      active: true,
      runtimeReady: true,
      sizeBytes: 228_000_000,
    },
  ],
};

function renderSettings(overrides: Partial<ComponentProps<typeof LocalPolishingSettings>> = {}) {
  const props: ComponentProps<typeof LocalPolishingSettings> = {
    enabled: true,
    engine: "cloud",
    selectedVariant: "qad_q4_0",
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
    const props = renderSettings({
      catalog: {
        available: true,
        models: [{ variant: "qad_q4_0", status: "not_installed", installed: false }],
      },
    });
    const qadCard = screen.getByTestId("local-polishing-model-qad_q4_0");

    fireEvent.click(within(qadCard).getByRole("button", { name: "Download" }));

    expect(props.onInstall).toHaveBeenCalledWith("qad_q4_0");
    expect(props.onUse).not.toHaveBeenCalled();
    expect(props.onEngineChange).not.toHaveBeenCalled();
  });

  it("describes the conditional local-processing privacy boundary without claiming a public download", () => {
    renderSettings();

    expect(
      screen.getByText(
        "When local polishing is available, transcript text stays on this device. Cloud speech recognition may still upload audio.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Audio stays local/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in|log in|token|credential/i })).not.toBeInTheDocument();
  });

  it("identifies the single QAD model without retired model choices or Gemma terms", () => {
    renderSettings();

    expect(screen.getByText("LFM2.5 350M · QAD Q4_0")).toBeInTheDocument();
    expect(screen.getByText(/only one local model: LFM2.5 350M quantized with QAD to Q4_0/i)).toBeInTheDocument();
    expect(screen.getByText(/Praxist by Sapient Intelligence/i)).toBeInTheDocument();
    expect(screen.queryByTestId("local-polishing-model-q8_0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("local-polishing-model-bf16")).not.toBeInTheDocument();
    expect(screen.queryByText(/Gemma/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("shows exactly one installable model card", () => {
    renderSettings();

    expect(screen.getAllByTestId(/^local-polishing-model-/)).toHaveLength(1);
    expect(screen.getByTestId("local-polishing-model-qad_q4_0")).toBeInTheDocument();
  });

  it("shows an unavailable message and no model card when the catalog is not materialized", () => {
    renderSettings({
      catalog: {
        available: false,
        message: "catalog_not_materialized",
        models: [{ variant: "qad_q4_0", status: "unavailable", installed: false }],
      },
    });

    expect(
      screen.getByText("The local polishing model is unavailable in this build."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("local-polishing-model-qad_q4_0")).not.toBeInTheDocument();
  });

  it("requires an explicit use action and protects the active model from removal", () => {
    const props = renderSettings({ engine: "local" });
    const qadCard = screen.getByTestId("local-polishing-model-qad_q4_0");

    expect(within(qadCard).getByRole("button", { name: "In use" })).toBeDisabled();
    expect(within(qadCard).getByRole("button", { name: "Remove" })).toBeDisabled();
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
            variant: "qad_q4_0",
            status: "downloading",
            operationId: "operation-17",
            progress: 38,
          },
        ],
      },
    });

    const qadCard = screen.getByTestId("local-polishing-model-qad_q4_0");
    expect(
      within(qadCard).getByRole("progressbar", { name: "LFM2.5 350M · QAD Q4_0 download progress" }),
    ).toHaveAttribute(
      "aria-valuenow",
      "38",
    );
    fireEvent.click(within(qadCard).getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledWith("operation-17");
  });
});
