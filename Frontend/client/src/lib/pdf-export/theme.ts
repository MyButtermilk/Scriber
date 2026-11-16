import { defaultPrimitives } from "@/lib/pdf-themes/primitives";
import type { PdfcnTheme } from "@/types/pdf-themes";

/** Font family registered for every Scriber document (see `fonts.ts`). */
export const SCRIBER_PDF_FONT_FAMILY = "Inter";

/**
 * Scriber's pdfcn theme: the app's Inter typography on a restrained neutral
 * palette with one blue accent, sized for long-form reading on A4.
 */
export const scriberPdfTheme: PdfcnTheme = {
  colors: {
    accent: "#2563eb",
    background: "#ffffff",
    border: "#e4e4e7",
    destructive: "#b91c1c",
    foreground: "#18181b",
    info: "#0369a1",
    muted: "#f4f4f5",
    mutedForeground: "#71717a",
    primary: "#18181b",
    primaryForeground: "#ffffff",
    success: "#15803d",
    warning: "#a16207",
  },
  name: "scriber",
  page: {
    orientation: "portrait",
    size: "A4",
  },
  primitives: defaultPrimitives,
  spacing: {
    componentGap: 12,
    page: {
      marginBottom: 54,
      marginLeft: 60,
      marginRight: 60,
      marginTop: 54,
    },
    paragraphGap: 8,
    sectionGap: 22,
  },
  typography: {
    body: {
      fontFamily: SCRIBER_PDF_FONT_FAMILY,
      fontSize: 10.5,
      lineHeight: 1.55,
    },
    heading: {
      fontFamily: SCRIBER_PDF_FONT_FAMILY,
      fontSize: {
        h1: 24,
        h2: 15,
        h3: 12.5,
        h4: 11,
        h5: 10.5,
        h6: 10,
      },
      fontWeight: 700,
      lineHeight: 1.25,
    },
  },
};
