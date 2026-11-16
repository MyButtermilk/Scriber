import type { FontRegistration } from "@formepdf/react";

import italicUrl from "@fontsource/inter/files/inter-latin-400-italic.woff?url";
import regularUrl from "@fontsource/inter/files/inter-latin-400-normal.woff?url";
import mediumUrl from "@fontsource/inter/files/inter-latin-500-normal.woff?url";
import semiboldUrl from "@fontsource/inter/files/inter-latin-600-normal.woff?url";
import boldItalicUrl from "@fontsource/inter/files/inter-latin-700-italic.woff?url";
import boldUrl from "@fontsource/inter/files/inter-latin-700-normal.woff?url";

import { SCRIBER_PDF_FONT_FAMILY } from "./theme";
import { woffToSfnt } from "./woff";

const FONT_FACES = [
  { url: regularUrl, fontWeight: 400, fontStyle: "normal" },
  { url: italicUrl, fontWeight: 400, fontStyle: "italic" },
  { url: mediumUrl, fontWeight: 500, fontStyle: "normal" },
  { url: semiboldUrl, fontWeight: 600, fontStyle: "normal" },
  { url: boldUrl, fontWeight: 700, fontStyle: "normal" },
  { url: boldItalicUrl, fontWeight: 700, fontStyle: "italic" },
] as const;

let fontsPromise: Promise<FontRegistration[]> | null = null;

async function loadFonts(): Promise<FontRegistration[]> {
  return Promise.all(
    FONT_FACES.map(async (face) => {
      const response = await fetch(face.url);
      if (!response.ok) throw new Error(`PDF font is unavailable (${response.status})`);
      const sfnt = await woffToSfnt(new Uint8Array(await response.arrayBuffer()));
      return {
        family: SCRIBER_PDF_FONT_FAMILY,
        src: sfnt,
        fontWeight: face.fontWeight,
        fontStyle: face.fontStyle,
      } satisfies FontRegistration;
    }),
  );
}

/** Load the app's bundled Inter faces once and keep them for later exports. */
export function loadScriberPdfFonts(): Promise<FontRegistration[]> {
  if (!fontsPromise) {
    fontsPromise = loadFonts().catch((error) => {
      fontsPromise = null;
      throw error;
    });
  }
  return fontsPromise;
}
