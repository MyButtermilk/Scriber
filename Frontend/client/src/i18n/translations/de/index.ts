import type { TranslationCatalog } from "@/i18n/types";
import { componentTranslations } from "./components";
import { coreTranslations } from "./core";
import { diagnosticsTranslations } from "./diagnostics";
import { meetingsTranslations } from "./meetings";
import { podcastTranslations } from "./podcasts";
import { settingsTranslations } from "./settings";
import { transcriptionTranslations } from "./transcription";

export const germanTranslations: TranslationCatalog = {
  ...coreTranslations,
  ...diagnosticsTranslations,
  ...componentTranslations,
  ...transcriptionTranslations,
  ...meetingsTranslations,
  ...podcastTranslations,
  ...settingsTranslations,
};
