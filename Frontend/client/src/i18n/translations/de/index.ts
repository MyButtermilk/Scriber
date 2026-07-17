import type { TranslationCatalog } from "@/i18n/types";
import { componentTranslations } from "./components";
import { coreTranslations } from "./core";
import { meetingsTranslations } from "./meetings";
import { settingsTranslations } from "./settings";
import { transcriptionTranslations } from "./transcription";

export const germanTranslations: TranslationCatalog = {
  ...coreTranslations,
  ...componentTranslations,
  ...transcriptionTranslations,
  ...meetingsTranslations,
  ...settingsTranslations,
};
