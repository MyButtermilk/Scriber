import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const runtime = vi.hoisted(() => ({ tauri: false }));

vi.mock("@/lib/backend", () => ({
  isTauriRuntime: () => runtime.tauri,
}));

import { LocaleProvider } from "@/i18n";
import { DesktopTitleBar } from "./DesktopTitleBar";

describe("DesktopTitleBar", () => {
  beforeEach(() => {
    runtime.tauri = false;
  });

  it("renders nothing outside the Tauri runtime", () => {
    const { container } = render(<DesktopTitleBar />, { wrapper: LocaleProvider });
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the drag region and window controls visible at every WebView width", () => {
    runtime.tauri = true;
    const { container } = render(<DesktopTitleBar />, { wrapper: LocaleProvider });
    const titlebar = container.querySelector(".desktop-titlebar");
    expect(titlebar).not.toBeNull();
    expect(titlebar).toHaveClass("flex");
    expect(titlebar).not.toHaveClass("hidden");
    expect(titlebar).not.toHaveClass("md:flex");
    expect(container.querySelector("[data-tauri-drag-region]")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Minimize window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Maximize window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close window" })).toBeInTheDocument();
  });
});
