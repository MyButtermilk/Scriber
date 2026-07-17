import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  CalendarClock,
  Check,
  ChevronDown,
  Cloud,
  Download,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  Globe,
  Keyboard,
  Key,
  Languages,
  Loader2,
  Mic,
  RefreshCw,
  Save,
  Shield,
  Sparkles,
  Star,
  ToggleLeft,
  Trash2,
  Users,
  type LucideIcon,
} from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogDescription } from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  apiUrl,
  refreshGlobalHotkey,
  isTauriRuntime,
  setGlobalHotkeyCaptureActive,
  setAutostartEnabled as setDesktopAutostartEnabled,
} from "@/lib/backend";
import { invalidateSettingsBootstrap, loadSettingsBootstrap } from "@/lib/settings-bootstrap";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { responseErrorMessage } from "@/lib/request-errors";
import type {
  ApiMessageResponse,
  DiarizationComponentStatus,
  LocalModelActionResponse,
  MicrophoneDevice,
  MicrophonesResponse,
  MeetingProfilesResponse,
  MeetingAudioDevicesResponse,
  MeetingTranscriptionMode,
  OnnxModelInfo,
  OnnxModelsResponse,
  OutlookCalendarStatus,
  OutlookCalendarSyncResponse,
  SpeakerModelStatus,
  SpeakerEnrollmentResponse,
  SpeakerProfilesResponse,
  SettingsResponse,
  SettingsUpdatePayload,
} from "@/lib/api-types";
import { apiRequest, OUTLOOK_SYNC_REQUEST_TIMEOUT_MS } from "@/lib/queryClient";
import { refreshAllMeetingSpeakerIdentityCaches } from "@/lib/meeting-cache";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { PageIntro } from "@/components/page-intro";
import {
  checkDesktopUpdate,
  checkDesktopUpdateIfDue,
  initialDesktopUpdateStatus,
  installDesktopUpdate,
  openDesktopUpdateReleaseNotes,
  remindDesktopUpdateLater,
  skipDesktopUpdateVersion,
  subscribeDesktopUpdateStatus,
  type DesktopUpdateProgress,
  type DesktopUpdateStatus,
  updateDesktopUpdateSettings,
} from "@/lib/desktop-updates";
import {
  DEFAULT_VISUALIZER_BAR_COUNT,
  MAX_VISUALIZER_BAR_COUNT,
  MIN_VISUALIZER_BAR_COUNT,
  normalizeVisualizerBarCount,
} from "@/lib/visualizer-settings";
import { localizeOnnxDownloadMessage } from "@/lib/onnx-download-message";
import { getCurrentLocale, translateNow, useI18n } from "@/i18n";

type Translate = ReturnType<typeof useI18n>["t"];
type FormatDate = ReturnType<typeof useI18n>["formatDate"];

const LANGUAGE_OPTIONS = [
  { value: "auto", label: "Auto-detect" },
  { value: "de", label: "German" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "it", label: "Italian" },
] as const;

const SETTINGS_SECTION_REQUEST_KEY = "scriber:open-settings-section";
const VOICE_ENROLLMENT_DURATION_MS = 8_000;
const DEFAULT_VOICE_ENROLLMENT_DEVICE = "windows-default";
const SETTINGS_SECTION_IDS: Record<string, string> = {
  transcription: "settings-transcription",
  meetings: "settings-meetings",
  providers: "settings-providers",
  apiKeys: "settings-api-keys",
  summarization: "settings-summaries",
  updates: "settings-updates",
  language: "settings-language",
};

type ScrollSnapshot = {
  windowX: number;
  windowY: number;
  documentLeft: number | null;
  documentTop: number | null;
};

function captureScrollSnapshot(): ScrollSnapshot | null {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return null;
  }
  const scrollingElement = document.scrollingElement as HTMLElement | null;
  return {
    windowX: window.scrollX,
    windowY: window.scrollY,
    documentLeft: scrollingElement ? scrollingElement.scrollLeft : null,
    documentTop: scrollingElement ? scrollingElement.scrollTop : null,
  };
}

function formatUpdateTimestamp(value: string | undefined, formatDate: FormatDate, t: Translate): string {
  if (!value) return t("Never");
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return t("Unknown");
  return formatDate(parsed, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function outlookSyncErrorMessage(value: string | undefined, t: Translate): string {
  const code = String(value || "").toLocaleLowerCase();
  if (!code) return "";
  if (code.includes("cancel")) {
    return t("The last Microsoft sign-in was canceled. Connect again when you are ready.");
  }
  if (code.includes("author") || code.includes("token") || code.includes("credential")) {
    return t("Microsoft needs you to connect Outlook again before the calendar can refresh.");
  }
  if (code.includes("timeout") || code.includes("connector") || code.includes("network")) {
    return t("Outlook could not be reached. Check your connection, then choose Sync now.");
  }
  return t("The last calendar refresh did not finish. Your previously saved meetings are unchanged; choose Sync now to retry.");
}

function localizedSettingsError(
  error: unknown,
  fallback: string,
  locale: "de" | "en",
  t: Translate,
): string {
  const raw = error instanceof Error ? error.message.trim() : String(error || "").trim();
  if (!raw) return t(fallback);
  if (locale === "en") return raw;
  const translated = t(raw);
  return translated === raw ? t(fallback) : translated;
}

function localizedSettingsErrorNow(error: unknown, fallback: string): string {
  return localizedSettingsError(error, fallback, getCurrentLocale(), translateNow);
}

function restoreScrollSnapshot(snapshot: ScrollSnapshot) {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return;
  }
  const scrollingElement = document.scrollingElement as HTMLElement | null;
  if (scrollingElement && snapshot.documentTop !== null && snapshot.documentLeft !== null) {
    scrollingElement.scrollTo({
      top: snapshot.documentTop,
      left: snapshot.documentLeft,
      behavior: "auto",
    });
  }
  window.scrollTo({ top: snapshot.windowY, left: snapshot.windowX, behavior: "auto" });
}

const TRANSCRIPTION_MODEL_OPTIONS = [
  { value: "onnx_local", label: "Local (ONNX) - No API Key" },
  { value: "soniox-realtime", label: "Soniox STT Streaming" },
  { value: "soniox-async", label: "Soniox Async" },
  { value: "modulate-realtime", label: "Modulate.AI Multilingual Realtime" },
  { value: "modulate-async", label: "Modulate.AI Multilingual Batch" },
  { value: "gemini-stt", label: "Gemini STT" },
  { value: "mistral-realtime", label: "Mistral Live (Voxtral)" },
  { value: "mistral-async", label: "Mistral Async (Voxtral V2)" },
  { value: "smallest-realtime", label: "Smallest AI STT Streaming (Pulse)" },
  { value: "smallest-async", label: "Smallest AI Async (Pulse)" },
  { value: "assemblyai-realtime", label: "AssemblyAI Universal-3.5 Pro Realtime" },
  { value: "assemblyai", label: "AssemblyAI Universal-3.5 Pro Async" },
  { value: "deepgram", label: "Deepgram STT Streaming" },
  { value: "deepgram-async", label: "Deepgram Async" },
  { value: "openai", label: "OpenAI Realtime" },
  { value: "openai-async", label: "OpenAI Async" },
  { value: "azure_mai", label: "Microsoft MAI Transcribe" },
  { value: "gladia", label: "Gladia STT Streaming" },
  { value: "gladia-async", label: "Gladia Async" },
  { value: "groq", label: "Groq Live" },
  { value: "speechmatics", label: "Speechmatics STT Streaming" },
  { value: "speechmatics-async", label: "Speechmatics Batch" },
  { value: "elevenlabs", label: "ElevenLabs Live" },
  { value: "google", label: "Google Cloud STT Streaming" },
] as const;

const USD_TO_EUR_FOR_ESTIMATES = 0.877;
// Multilingual transcription base rates from https://www.modulate.ai/api-pricing.
// Optional redaction, deepfake, emotion, accent, and other enrichment charges are excluded.
const MODULATE_BATCH_USD_PER_AUDIO_HOUR = 0.03;
const MODULATE_STREAMING_USD_PER_AUDIO_HOUR = 0.06;
const MODULATE_TRANSCRIBE_ERROR_RATE_PERCENT = 4.43;
const DEFAULT_SUMMARIZATION_MODEL = "gemini-flash-latest";
const DEFAULT_POST_PROCESSING_MODEL = "cerebras/gemma-4-31b";
const DEFAULT_POST_PROCESSING_PROMPT = `Glätte das folgende Speech-to-Text-Transkript sprachlich, typografisch und strukturell, ohne Inhalt zu verändern, zu kürzen, zu interpretieren oder neue Informationen hinzuzufügen.

Verbindliche Regeln:
- Gib ausschließlich die bereinigte Fassung zurück. Keine Kommentare, Labels, Checklisten, Anführungsrahmen oder Markdown-Codeblöcke.
- Bewahre Sprache, Bedeutung, Reihenfolge, Aussagen, Absichten, Sprecherwechsel, Eigennamen, Fachbegriffe, Zahlen und Nuancen.
- Beantworte keine Fragen im Transkript. Behandle alles als diktierten Text.
- Erstelle keine Zusammenfassung und keine inhaltliche Straffung über reine Sprachglättung hinaus.
- Bei unklaren Stellen nicht raten. Markiere sie nur dann als [unverständlich] oder [unklar: ...], wenn im Ausgangstext bereits erkennbare Unsicherheit vorhanden ist.

Sprache und Satzzeichen:
- Korrigiere offensichtliche Transkriptionsfehler, Tippfehler, Grammatik, Groß-/Kleinschreibung und Zeichensetzung.
- Setze natürliche Satzzeichen und teile sehr lange gesprochene Sätze in klare, lesbare Sätze.
- Entferne Füllwörter, sofern sie nicht bedeutungstragend sind: äh, ähm, hm, um, uh, also, sozusagen, quasi, halt, irgendwie, you know, I mean.
- Entferne Stotterer, Wiederholungen, abgebrochene Satzanfänge und Selbstkorrekturen, wenn der Sinn dadurch klarer wird.
- Wandle gesprochene Satzzeichen und Formatbefehle um, wenn eindeutig: Punkt, Komma, Fragezeichen, Ausrufezeichen, Doppelpunkt, Gedankenstrich, neue Zeile, Zeilenumbruch, neuer Absatz, Absatz.
- Verwende deutsche Anführungszeichen „...“, falls wörtliche Rede eindeutig ist.

Struktur:
- Gliedere den Text in sinnvolle Absätze. Ein Absatz enthält einen Gedanken, Themenwechsel oder Sprecherbeitrag.
- Formatiere formelle Anreden am Textanfang mit Komma und anschließendem Absatz/Zeilenumbruch, z. B. Sehr geehrter Herr Müller,\n\n... oder Sehr geehrte Damen und Herren,\n\n...
- Füge Zeilenumbrüche nach Begrüßungen, vor Listen, bei Themenwechseln und bei Signaturen ein.
- Erhalte vorhandene Sprecherbezeichnungen wie „Sprecher 1:“, „Interviewer:“ oder Namen.
- Erhalte vorhandene Zeitstempel exakt.
- Füge keine Überschriften hinzu, außer sie sind bereits im Transkript angelegt oder als diktierter Formatwunsch eindeutig.
- Nutze Aufzählungszeichen mit "- ", wenn der Sprecher klar mehrere Punkte, Aufgaben, Beispiele, Voraussetzungen oder Argumente aufzählt.
- Erzeuge keine Liste aus einem normalen Fließsatz; nutze Listen nur für echte Aufzählungen.

Zahlen, Daten, Uhrzeiten und Einheiten:
- Formatiere Zahlen konsistent nach deutscher Schreibweise, wenn der Text deutsch ist: 1.250, 25.000, 1.000.000, 3,5.
- Verwende Ziffern für Mengen, Preise, Prozentwerte, Maße, Flächen, Zeitangaben, Daten, Telefonnummern, Adressen und technische Werte.
- Formatiere Geld, Prozent, Daten und Uhrzeiten, wenn eindeutig: fünfzehn Prozent -> 15 %, zweitausend fünfhundert Euro -> 2.500 €, am dritten vierten zwanzig vierundzwanzig -> am 03.04.2024, vierzehn Uhr dreißig -> 14:30 Uhr.
- Formatiere Einheiten kompakt und professionell: Euro pro Quadratmeter -> €/m², Quadratmeter -> m², Kubikmeter -> m³, Kilometer pro Stunde -> km/h, Kilowattstunden -> kWh, Kilowattstunden pro Quadratmeter und Jahr -> kWh/m²a, Grad Celsius -> °C, Meter -> m, Zentimeter -> cm, Kilogramm -> kg.
- Setze zwischen Zahl und Einheit ein Leerzeichen, sofern üblich: 25 m², 3,5 kg, 120 km/h, 15 %.
- Bei zusammengesetzten Einheiten ohne vorangestellte Zahl nutze kompakte Schreibweise: €/m², kWh/m²a.

Transkript:
\${output}`;

type HotkeyCaptureEvent = Pick<
  KeyboardEvent,
  "altKey" | "code" | "ctrlKey" | "key" | "metaKey" | "shiftKey"
> & {
  preventDefault?: () => void;
  stopPropagation?: () => void;
};

const HOTKEY_MODIFIER_KEYS = new Set(["Alt", "Control", "Meta", "OS", "Shift"]);
const HOTKEY_SPECIAL_KEYS: Record<string, string> = {
  " ": "Space",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  ArrowUp: "Up",
  Esc: "Escape",
};

function hotkeyDisplayFromKeyboardEvent(event: HotkeyCaptureEvent): string {
  let key = event.key || "";
  if (!key || key === "Unidentified") {
    if (event.code?.startsWith("Key")) {
      key = event.code.slice(3);
    } else if (event.code?.startsWith("Digit")) {
      key = event.code.slice(5);
    }
  }
  if (!key || HOTKEY_MODIFIER_KEYS.has(key)) {
    return "";
  }

  const keys: string[] = [];
  if (event.ctrlKey) keys.push("Ctrl");
  if (event.shiftKey) keys.push("Shift");
  if (event.altKey) keys.push("Alt");
  if (event.metaKey) keys.push("Meta");

  const displayKey = HOTKEY_SPECIAL_KEYS[key] || (key.length === 1 ? key.toUpperCase() : key);
  keys.push(displayKey);
  return keys.join(" + ");
}

type SummarizationModelOption = {
  value: string;
  label: string;
  detail: string;
  group: "gemini" | "openrouter" | "openai" | "cerebras";
  icon: ProviderIconKey;
};

function languageModelBenchmarkDetail(
  inputUsdPerToken: number,
  outputUsdPerToken: number,
  tokensPerSecond: number,
  localeTag: string,
  t: Translate,
): string {
  const euroPerMillionBlendedTokens =
    ((inputUsdPerToken + outputUsdPerToken) / 2) * 1_000_000 * USD_TO_EUR_FOR_ESTIMATES;
  const priceText = euroPerMillionBlendedTokens.toLocaleString(localeTag, {
    minimumFractionDigits: euroPerMillionBlendedTokens < 1 ? 2 : 1,
    maximumFractionDigits: euroPerMillionBlendedTokens < 1 ? 2 : 1,
  });
  return t("{{price}}€/M blended, ~{{tokens}} Token/s", { price: priceText, tokens: tokensPerSecond });
}

function aaLanguageBenchmarkDetail(
  usdPerMillionTokens: number,
  intelligenceScore: number,
  localeTag: string,
  t: Translate,
): string {
  const euroPerMillionTokens = usdPerMillionTokens * USD_TO_EUR_FOR_ESTIMATES;
  const priceText = euroPerMillionTokens.toLocaleString(localeTag, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return t("{{price}}€/M with AA Score {{score}}", { price: priceText, score: intelligenceScore });
}

function expandPromptTextarea(element: HTMLTextAreaElement, minimumHeightPx: number): void {
  element.style.height = "auto";
  element.style.height = `${Math.max(element.scrollHeight + 2, minimumHeightPx)}px`;
}

function createSummarizationModelOptions(localeTag: string, t: Translate): readonly SummarizationModelOption[] {
  return [
    { value: "gemini-3.1-flash-lite-preview", label: "Gemini 3.1 Flash Lite", detail: aaLanguageBenchmarkDetail(0.22, 25, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "gemini-flash-latest", label: "Gemini Flash Latest", detail: aaLanguageBenchmarkDetail(1.31, 50, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "gemini-3.5-flash", label: "Gemini 3.5 Flash", detail: aaLanguageBenchmarkDetail(1.31, 50, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro", detail: aaLanguageBenchmarkDetail(1.74, 46, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "cerebras/gemma-4-31b", label: "Gemma 4 31B", detail: aaLanguageBenchmarkDetail(1.04, 29, localeTag, t), group: "cerebras", icon: "cerebras" },
    { value: "minimax/minimax-m3:nitro", label: "MiniMax M3 Nitro", detail: aaLanguageBenchmarkDetail(0.22, 44, localeTag, t), group: "openrouter", icon: "openrouter" },
    { value: "z-ai/glm-5.2:nitro", label: "GLM 5.2 Nitro", detail: aaLanguageBenchmarkDetail(0.90, 51, localeTag, t), group: "openrouter", icon: "openrouter" },
    { value: "gpt-5.5", label: "OpenAI GPT 5.5", detail: aaLanguageBenchmarkDetail(4.35, 53, localeTag, t), group: "openai", icon: "openai" },
    { value: "gpt-5.4-mini", label: "OpenAI GPT 5.4 Mini", detail: aaLanguageBenchmarkDetail(0.65, 30, localeTag, t), group: "openai", icon: "openai" },
    { value: "gpt-5.4-nano", label: "OpenAI GPT 5.4 Nano", detail: aaLanguageBenchmarkDetail(0.18, 18, localeTag, t), group: "openai", icon: "openai" },
  ];
}

function createPostProcessingModelOptions(localeTag: string, t: Translate): readonly SummarizationModelOption[] {
  return [
    { value: "cerebras/gemma-4-31b", label: "Gemma 4 31B Cerebras", detail: languageModelBenchmarkDetail(0.0000006, 0.0000012, 500, localeTag, t), group: "cerebras", icon: "cerebras" },
    { value: "openai/gpt-oss-120b", label: "GPT-OSS 120B Baseten", detail: languageModelBenchmarkDetail(0.0000001, 0.0000005, 189, localeTag, t), group: "openrouter", icon: "baseten" },
    { value: "openai/gpt-oss-120b:cerebras", label: "GPT-OSS 120B Cerebras", detail: languageModelBenchmarkDetail(0.00000035, 0.00000075, 768, localeTag, t), group: "openrouter", icon: "cerebras" },
    { value: "google/gemini-2.5-flash-lite:nitro", label: "Gemini 2.5 Flash Lite Nitro", detail: languageModelBenchmarkDetail(0.0000001, 0.0000004, 45, localeTag, t), group: "openrouter", icon: "openrouter" },
    { value: "gpt-5.4-nano", label: "OpenAI GPT 5.4 Nano", detail: languageModelBenchmarkDetail(0.00000005, 0.0000004, 81, localeTag, t), group: "openai", icon: "openai" },
    { value: "gemini-3.1-flash-lite-preview", label: "Gemini 3.1 Flash Lite", detail: languageModelBenchmarkDetail(0.00000025, 0.0000015, 81, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "minimax/minimax-m3:nitro", label: "MiniMax M3 Nitro", detail: languageModelBenchmarkDetail(0.0000003, 0.0000012, 58, localeTag, t), group: "openrouter", icon: "openrouter" },
    { value: "gemini-3.5-flash", label: "Gemini 3.5 Flash", detail: languageModelBenchmarkDetail(0.0000015, 0.000009, 69, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "gpt-5.4-mini", label: "OpenAI GPT 5.4 Mini", detail: languageModelBenchmarkDetail(0.00000025, 0.000002, 72, localeTag, t), group: "openai", icon: "openai" },
    { value: "z-ai/glm-5.2:nitro", label: "GLM 5.2 Nitro", detail: languageModelBenchmarkDetail(0.00000093, 0.000003, 30, localeTag, t), group: "openrouter", icon: "openrouter" },
    { value: "gemini-flash-latest", label: "Gemini Flash Latest", detail: languageModelBenchmarkDetail(0.0000015, 0.000009, 69, localeTag, t), group: "gemini", icon: "gemini" },
    { value: "gpt-5.5", label: "OpenAI GPT 5.5", detail: languageModelBenchmarkDetail(0.00000175, 0.000014, 39, localeTag, t), group: "openai", icon: "openai" },
    { value: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro", detail: languageModelBenchmarkDetail(0.000002, 0.000012, 95, localeTag, t), group: "gemini", icon: "gemini" },
  ];
}

const API_KEY_HELP_LINKS = {
  openai: { href: "https://platform.openai.com/api-keys", label: "OpenAI keys" },
  deepgram: { href: "https://console.deepgram.com/", label: "Deepgram console" },
  assemblyai: { href: "https://www.assemblyai.com/dashboard", label: "AssemblyAI dashboard" },
  gemini: { href: "https://aistudio.google.com/app/apikey", label: "Google AI Studio" },
  openrouter: { href: "https://openrouter.ai/settings/keys", label: "OpenRouter keys" },
  cerebras: { href: "https://cloud.cerebras.ai/", label: "Cerebras Cloud" },
  youtube: { href: "https://console.cloud.google.com/apis/credentials", label: "Google Cloud credentials" },
  soniox: { href: "https://console.soniox.com/", label: "Soniox console" },
  smallest: { href: "https://app.smallest.ai/", label: "Smallest AI console" },
  mistral: { href: "https://console.mistral.ai/api-keys", label: "Mistral API keys" },
  modulate: { href: "https://platform.modulate.ai/", label: "Modulate.AI API keys" },
  elevenlabs: { href: "https://elevenlabs.io/app/settings/api-keys", label: "ElevenLabs API keys" },
  azure: { href: "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices", label: "Azure MAI Speech resource" },
  gladia: { href: "https://app.gladia.io/api-keys", label: "Gladia API keys" },
  groq: { href: "https://console.groq.com/keys", label: "Groq API keys" },
  speechmatics: { href: "https://portal.speechmatics.com/", label: "Speechmatics portal" },
  googleCloud: { href: "https://console.cloud.google.com/apis/credentials", label: "Google Cloud credentials" },
} as const;

type ApiKeyHelpKey = keyof typeof API_KEY_HELP_LINKS;
type CredentialRequirement = {
  provider: string;
  label: string;
  helpKey: ApiKeyHelpKey;
};

const MISSING_CREDENTIAL_CTA = "Add API Key";
const SONIOX_DATA_RESIDENCY_URL = "https://soniox.com/docs/data-residency";
const SONIOX_REGION_SUPPORT_URL = "mailto:support@soniox.com?subject=Enable%20EU%20data%20residency%20for%20my%20Soniox%20organization&body=Hello%20Soniox%20Support%2C%0A%0APlease%20enable%20EU%20regional%20deployment%20access%20for%20my%20organization.%0A%0AOrganization%20ID%3A%20%0A%0AThank%20you.";

async function openExternalHelpUrl(url: string): Promise<void> {
  if (isTauriRuntime()) {
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url);
      return;
    } catch (error) {
      console.warn("Tauri opener failed; falling back to browser window.open.", error);
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function ApiKeyLink({ helpKey, children }: { helpKey: ApiKeyHelpKey; children?: ReactNode }) {
  const { t } = useI18n();
  const help = API_KEY_HELP_LINKS[helpKey];
  return (
    <a
      href={help.href}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => {
        event.preventDefault();
        void openExternalHelpUrl(help.href);
      }}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      title={t(help.label)}
    >
      {children ?? t("Get key")}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

function hasValue(value: string | undefined): boolean {
  return Boolean((value || "").trim());
}

function uniqueCredentialRequirements(requirements: Array<CredentialRequirement | null>): CredentialRequirement[] {
  const seen = new Set<string>();
  const unique: CredentialRequirement[] = [];
  for (const requirement of requirements) {
    if (!requirement || seen.has(requirement.provider)) {
      continue;
    }
    seen.add(requirement.provider);
    unique.push(requirement);
  }
  return unique;
}

function LanguageFlag({ value, className }: { value: string; className?: string }) {
  if (value === "auto") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <Globe className="globe-svg" />
      </span>
    );
  }

  if (value === "de") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="3" height="1" y="0" fill="#000000" />
          <rect width="3" height="1" y="1" fill="#FF0000" />
          <rect width="3" height="1" y="2" fill="#FFCC00" />
        </svg>
      </span>
    );
  }

  if (value === "en") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 64 64" preserveAspectRatio="none">
          <rect width="64" height="64" fill="#012169" />
          <line x1="0" y1="0" x2="64" y2="64" stroke="#fff" strokeWidth="12" />
          <line x1="0" y1="64" x2="64" y2="0" stroke="#fff" strokeWidth="12" />
          <line x1="0" y1="0" x2="64" y2="64" stroke="#C8102E" strokeWidth="6" />
          <line x1="0" y1="64" x2="64" y2="0" stroke="#C8102E" strokeWidth="6" />
          <line x1="32" y1="0" x2="32" y2="64" stroke="#fff" strokeWidth="16" />
          <line x1="0" y1="32" x2="64" y2="32" stroke="#fff" strokeWidth="16" />
          <line x1="32" y1="0" x2="32" y2="64" stroke="#C8102E" strokeWidth="10" />
          <line x1="0" y1="32" x2="64" y2="32" stroke="#C8102E" strokeWidth="10" />
        </svg>
      </span>
    );
  }

  if (value === "es") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="3" height="0.75" y="0" fill="#AA151B" />
          <rect width="3" height="1.5" y="0.75" fill="#F1BF00" />
          <rect width="3" height="0.75" y="2.25" fill="#AA151B" />
          <circle cx="0.8" cy="1.5" r="0.35" fill="#AA151B" />
        </svg>
      </span>
    );
  }

  if (value === "fr") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="1" height="3" x="0" fill="#0055A4" />
          <rect width="1" height="3" x="1" fill="#FFFFFF" />
          <rect width="1" height="3" x="2" fill="#EF4135" />
        </svg>
      </span>
    );
  }

  return (
    <span className={cn("language-flag-icon", className)} aria-hidden="true">
      <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
        <rect width="1" height="3" x="0" fill="#009246" />
        <rect width="1" height="3" x="1" fill="#FFFFFF" />
        <rect width="1" height="3" x="2" fill="#CE2B37" />
      </svg>
    </span>
  );
}

const PROVIDER_ICON_PATHS = {
  anthropic: "/provider-icons/anthropic.svg",
  assemblyai: "/provider-icons/assemblyai.svg",
  azure: "/provider-icons/azure.svg",
  baseten: "/provider-icons/baseten.svg",
  cerebras: "/provider-icons/cerebras.svg",
  deepgram: "/provider-icons/deepgram.svg",
  elevenlabs: "/provider-icons/elevenlabs.svg",
  fal: "/provider-icons/fal.svg",
  gemini: "/provider-icons/gemini.svg",
  gladia: "/provider-icons/gladia.svg",
  googlecloud: "/provider-icons/googlecloud.svg",
  groq: "/provider-icons/groq.svg",
  mistral: "/provider-icons/mistral.svg",
  modulate: "/provider-icons/modulate.svg",
  openai: "/provider-icons/openai.svg",
  openrouter: "/provider-icons/openrouter.svg",
  soniox: "/provider-icons/soniox.svg",
  smallest: "/provider-icons/smallest.png",
  speechmatics: "/provider-icons/speechmatics.svg",
  youtube: "/provider-icons/youtube.svg",
} as const;

type ProviderIconKey = keyof typeof PROVIDER_ICON_PATHS;

interface ProviderModelOption {
  value: string;
  label: string;
  detail: string;
  group: "cloud_streaming" | "cloud_async" | "local";
  icon?: ProviderIconKey;
  hourlyCostEur?: number;
  wordErrorRatePercent?: number;
}

function sttHourlyBenchmarkDetail(
  usdPerHour: number,
  wordErrorRatePercent: number,
  localeTag: string,
  t: Translate,
): string {
  const euroPerHour = usdPerHour * USD_TO_EUR_FOR_ESTIMATES;
  const euroText = euroPerHour.toLocaleString(localeTag, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const errorText = wordErrorRatePercent.toLocaleString(localeTag, {
    minimumFractionDigits: wordErrorRatePercent % 1 === 0 ? 0 : 1,
    maximumFractionDigits: 2,
  });
  return t("{{price}}€/h with {{error}}% Error", { price: euroText, error: errorText });
}

function sttBenchmarkDetail(
  usdPerThousandMinutes: number,
  wordErrorRatePercent: number,
  localeTag: string,
  t: Translate,
): string {
  return sttHourlyBenchmarkDetail(usdPerThousandMinutes * 0.06, wordErrorRatePercent, localeTag, t);
}

function createProviderModelOptions(localeTag: string, t: Translate): ProviderModelOption[] {
  const benchmarkOption = (
    value: string,
    label: string,
    usdPerThousandMinutes: number,
    wordErrorRatePercent: number,
    group: ProviderModelOption["group"],
    icon?: ProviderIconKey,
  ): ProviderModelOption => ({
    value,
    label,
    detail: sttBenchmarkDetail(usdPerThousandMinutes, wordErrorRatePercent, localeTag, t),
    group,
    icon,
    hourlyCostEur: usdPerThousandMinutes * 0.06 * USD_TO_EUR_FOR_ESTIMATES,
    wordErrorRatePercent,
  });
  const hourlyOption = (
    value: string,
    label: string,
    usdPerHour: number,
    wordErrorRatePercent: number,
    group: ProviderModelOption["group"],
    icon?: ProviderIconKey,
  ): ProviderModelOption => ({
    value,
    label,
    detail: sttHourlyBenchmarkDetail(usdPerHour, wordErrorRatePercent, localeTag, t),
    group,
    icon,
    hourlyCostEur: usdPerHour * USD_TO_EUR_FOR_ESTIMATES,
    wordErrorRatePercent,
  });

  return [
    benchmarkOption("elevenlabs", "ElevenLabs Live", 6.50, 3.6, "cloud_streaming", "elevenlabs"),
    benchmarkOption("assemblyai-realtime", "AssemblyAI", 7.50, 4.1, "cloud_streaming", "assemblyai"),
    hourlyOption("modulate-realtime", "Modulate.AI Multilingual Realtime", MODULATE_STREAMING_USD_PER_AUDIO_HOUR, MODULATE_TRANSCRIBE_ERROR_RATE_PERCENT, "cloud_streaming", "modulate"),
    benchmarkOption("soniox-realtime", "Soniox", 2.00, 4.5, "cloud_streaming", "soniox"),
    benchmarkOption("google", "Google Cloud", 16.00, 4.8, "cloud_streaming", "googlecloud"),
    benchmarkOption("openai", "OpenAI Realtime", 17.00, 4.9, "cloud_streaming", "openai"),
    benchmarkOption("mistral-realtime", "Mistral Live", 6.00, 5.2, "cloud_streaming", "mistral"),
    benchmarkOption("smallest-realtime", "Smallest AI", 8.00, 6.5, "cloud_streaming", "smallest"),
    benchmarkOption("deepgram", "Deepgram", 4.80, 6.6, "cloud_streaming", "deepgram"),
    benchmarkOption("gladia", "Gladia", 12.50, 7.8, "cloud_streaming", "gladia"),
    benchmarkOption("speechmatics", "Speechmatics", 17.50, 8.0, "cloud_streaming", "speechmatics"),
    benchmarkOption("azure_mai", "Microsoft MAI", 6.00, 2.4, "cloud_async", "azure"),
    benchmarkOption("assemblyai", "AssemblyAI", 3.50, 3.1, "cloud_async", "assemblyai"),
    benchmarkOption("mistral-async", "Mistral Batch", 3.00, 3.6, "cloud_async", "mistral"),
    benchmarkOption("groq", "Groq Live", 4.00, 3.7, "cloud_async", "groq"),
    benchmarkOption("soniox-async", "Soniox", 1.66, 3.8, "cloud_async", "soniox"),
    benchmarkOption("speechmatics-async", "Speechmatics", 6.70, 4.0, "cloud_async", "speechmatics"),
    benchmarkOption("gladia-async", "Gladia", 4.07, 4.1, "cloud_async", "gladia"),
    benchmarkOption("smallest-async", "Smallest AI", 5.00, 4.4, "cloud_async", "smallest"),
    hourlyOption("modulate-async", "Modulate.AI Multilingual Batch", MODULATE_BATCH_USD_PER_AUDIO_HOUR, MODULATE_TRANSCRIBE_ERROR_RATE_PERCENT, "cloud_async", "modulate"),
    benchmarkOption("openai-async", "OpenAI Batch", 3.00, 4.5, "cloud_async", "openai"),
    benchmarkOption("gemini-stt", "Gemini", 6.66, 5.1, "cloud_async", "gemini"),
    benchmarkOption("deepgram-async", "Deepgram", 4.30, 5.2, "cloud_async", "deepgram"),
    { value: "onnx_local", label: "Local ONNX", detail: t("{{price}}€/h with model-dependent Error", { price: (0).toLocaleString(localeTag, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }), group: "local", hourlyCostEur: 0 },
  ];
}

const MEETING_FINAL_STT_OPTIONS = [
  { value: "soniox_async", label: "Soniox Async", model: "stt-async-v5", credentialModel: "soniox-async", recommended: true, nativeDiarization: true, fiveHourSupported: true, detail: "Keeps live and final transcription with the same service. Separates remote voices from system audio and keeps exact timing for meetings up to 5 hours." },
  { value: "assemblyai", label: "AssemblyAI", model: "Universal-3.5 Pro", credentialModel: "assemblyai", recommended: true, nativeDiarization: true, fiveHourSupported: true, detail: "Strong speaker naming and timing for meetings up to 5 hours." },
  { value: "mistral_async", label: "Mistral Voxtral", model: "Voxtral Mini Transcribe 2", credentialModel: "mistral-async", recommended: false, nativeDiarization: true, fiveHourSupported: false, detail: "Includes speaker names and timing for recordings up to 3 hours." },
  { value: "deepgram_async", label: "Deepgram", model: "Nova-3", credentialModel: "deepgram-async", recommended: false, nativeDiarization: true, fiveHourSupported: false, detail: "Includes word timing and speaker names. The current Scriber setup is not recommended for 5-hour meetings." },
  { value: "gladia_async", label: "Gladia", model: "Pre-recorded", credentialModel: "gladia-async", recommended: false, nativeDiarization: true, fiveHourSupported: false, detail: "Includes speaker names and timing for recordings up to 2 hours 15 minutes." },
  { value: "smallest_async", label: "Smallest AI", model: "Pulse batch", credentialModel: "smallest-async", recommended: false, nativeDiarization: true, fiveHourSupported: false, detail: "Can include speaker names when they are available." },
  { value: "speechmatics_async", label: "Speechmatics", model: "Batch", credentialModel: "speechmatics-async", recommended: false, nativeDiarization: true, fiveHourSupported: false, detail: "Includes speaker names in the completed transcript." },
  { value: "openai_async", label: "OpenAI Batch", model: "gpt-4o-mini-transcribe", credentialModel: "openai-async", recommended: false, nativeDiarization: false, fiveHourSupported: false, detail: "Creates the final transcript quickly. Scriber can add speaker names on this device." },
  { value: "gemini_stt", label: "Gemini STT", model: "Gemini audio", credentialModel: "gemini-stt", recommended: false, nativeDiarization: false, fiveHourSupported: false, detail: "Creates the final transcript, then Scriber can add speaker names on this device." },
  { value: "azure_mai", label: "Microsoft MAI", model: "mai-transcribe-1.5", credentialModel: "azure_mai", recommended: false, nativeDiarization: false, fiveHourSupported: true, detail: "Supports long meetings. Scriber can add speaker names on this device." },
  { value: "groq", label: "Groq Whisper", model: "whisper-large-v3-turbo", credentialModel: "groq", recommended: false, nativeDiarization: false, fiveHourSupported: false, detail: "Creates the final transcript, then Scriber can add speaker names on this device." },
  { value: "modulate_async", label: "Modulate.AI", model: "Multilingual Transcription", credentialModel: "modulate-async", recommended: false, nativeDiarization: false, fiveHourSupported: false, detail: "Creates one multilingual final transcript for meetings up to 3 hours without Modulate enrichment signals. Scriber can add speaker names on this device." },
  { value: "onnx_local", label: "Local ONNX STT", model: "Configured local model", credentialModel: "onnx_local", recommended: false, nativeDiarization: false, fiveHourSupported: true, detail: "Works without uploading audio. Scriber can also add speaker names on this device." },
] as const;

function providerErrorRate(option: ProviderModelOption): number {
  return option.wordErrorRatePercent ?? Number.POSITIVE_INFINITY;
}

function providerHourlyCost(option: ProviderModelOption): number {
  return option.hourlyCostEur ?? Number.POSITIVE_INFINITY;
}

function formatMeetingHourlyCost(value: number | null | undefined, localeTag: string, t: Translate): string {
  if (value == null) return t("Provider rate varies");
  const price = value.toLocaleString(localeTag, { style: "currency", currency: "USD" });
  if (value === 0) return t("{{price}} / meeting hour", { price });
  return t("~{{price}} / meeting hour", { price });
}

function compareMetricAscending(a: number, b: number): number {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

function sortProviderOptionsByErrorRate(options: ProviderModelOption[], localeTag: string): ProviderModelOption[] {
  return [...options].sort((a, b) => {
    const errorDelta = compareMetricAscending(providerErrorRate(a), providerErrorRate(b));
    if (errorDelta !== 0) return errorDelta;

    const costDelta = compareMetricAscending(providerHourlyCost(a), providerHourlyCost(b));
    if (costDelta !== 0) return costDelta;

    return a.label.localeCompare(b.label, localeTag);
  });
}

function ProviderIcon({
  icon,
  label,
  className,
}: {
  icon?: ProviderIconKey;
  label: string;
  className?: string;
}) {
  const { t } = useI18n();
  if (!icon) {
    return null;
  }
  return (
    <span
      className={cn(
        "flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white p-1 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.08)]",
        className,
      )}
    >
      <img
        src={PROVIDER_ICON_PATHS[icon]}
        alt={t("{{label}} logo", { label })}
        className="h-full w-full object-contain"
        draggable={false}
      />
    </span>
  );
}

function SectionPanel({
  title,
  description,
  icon: Icon,
  children,
  className,
  id,
}: {
  title: string;
  description: string;
  icon?: LucideIcon;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section
      id={id}
      className={cn(
        "settings-section min-w-0 scroll-mt-28 rounded-2xl border border-slate-200/80 bg-white/35 p-4 shadow-[0_18px_44px_-40px_rgba(15,23,42,0.45)] dark:border-[var(--workspace-border)] dark:bg-[var(--live-core)]",
        className,
      )}
    >
      <div className="mb-3.5 flex min-w-0 items-start gap-2.5">
        {Icon ? (
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-blue-50 text-blue-600 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.09)] dark:bg-blue-950/35 dark:text-blue-300">
            <Icon className="h-4 w-4" aria-hidden="true" />
          </span>
        ) : null}
        <div className="min-w-0 flex-1">
          <h2 className="text-[17px] !font-semibold leading-5 tracking-[-0.015em] text-slate-950 dark:text-slate-100 md:text-[18px]">{title}</h2>
          <p className="mt-1 max-w-[62ch] text-[11.5px] leading-[16px] text-slate-500 dark:text-slate-400">
            {description}
          </p>
        </div>
      </div>
      {children}
    </section>
  );
}

function SettingLine({
  label,
  description,
  children,
  className,
}: {
  label: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid gap-2.5 py-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(150px,220px)] sm:items-center", className)}>
      <div className="min-w-0">
        <Label className="text-[12.5px] font-semibold leading-4 text-slate-950 dark:text-slate-100">{label}</Label>
        {description ? (
          <p className="mt-1 text-[11.5px] leading-[15px] text-slate-500 dark:text-slate-400">{description}</p>
        ) : null}
      </div>
      <div className="min-w-0 sm:justify-self-end">{children}</div>
    </div>
  );
}

function SettingsSubsection({
  title,
  description,
  icon: Icon,
  action,
  children,
  className,
}: {
  title: string;
  description: string;
  icon?: LucideIcon;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "settings-subsection rounded-xl border border-slate-200/65 bg-white/65 p-3.5 shadow-[0_12px_32px_-30px_rgba(15,23,42,0.5)] dark:border-[var(--workspace-border)] dark:bg-[var(--live-card)]",
        className,
      )}
    >
      <div className="mb-3 flex min-w-0 items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-2">
          {Icon ? (
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-white/80 text-slate-500 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.07)] dark:bg-[var(--live-well)] dark:text-slate-400">
              <Icon className="h-3.5 w-3.5" aria-hidden="true" />
            </span>
          ) : null}
          <div className="min-w-0 flex-1">
            <h3 className="text-[13.5px] !font-semibold leading-4 text-slate-950 dark:text-slate-100">{title}</h3>
            <p className="mt-1 max-w-[62ch] text-[11.5px] leading-4 text-slate-500 dark:text-slate-400">{description}</p>
          </div>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      {children}
    </div>
  );
}

function revealRequestedSettingsSection(section: string) {
  const targetId = SETTINGS_SECTION_IDS[section];
  if (!targetId || typeof window === "undefined") {
    return;
  }

  window.requestAnimationFrame(() => {
    const target = document.getElementById(targetId);
    if (!target) {
      return;
    }
    const stickyHeader = document.querySelector<HTMLElement>(".settings-page .transcription-intro");
    const stickyOffset = Math.ceil(stickyHeader?.getBoundingClientRect().height || 0) + 16;
    target.style.scrollMarginTop = `${stickyOffset}px`;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ block: "start", behavior: reduceMotion ? "auto" : "smooth" });
    target.classList.add("settings-section-attention");
    window.setTimeout(() => {
      target.classList.remove("settings-section-attention");
    }, 1400);
  });
}

function ProviderChoice({
  option,
  selected,
  onSelect,
  disabled,
  disabledReason,
  onCredentialAction,
}: {
  option: ProviderModelOption;
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  disabledReason?: string;
  onCredentialAction?: () => void;
}) {
  const handleClick = () => {
    if (disabled) {
      onCredentialAction?.();
      return;
    }
    onSelect();
  };

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-disabled={disabled || undefined}
      onClick={handleClick}
      title={`${option.label}: ${option.detail}${disabledReason ? ` - ${disabledReason}` : ""}`}
      className={cn(
        "group flex min-h-[40px] w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-[background-color,box-shadow,transform] duration-200",
        "active:translate-y-px",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white",
        disabled
          ? "cursor-pointer text-slate-700 hover:bg-amber-50/75 dark:text-slate-300 dark:hover:bg-amber-950/20"
          : selected
          ? "bg-blue-50 text-blue-950 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.18)] dark:bg-blue-950/35 dark:text-blue-100"
          : "text-slate-800 hover:bg-slate-100/80 dark:text-slate-200 dark:hover:bg-[var(--live-card-hover)]",
      )}
    >
      {option.icon ? (
        <ProviderIcon icon={option.icon} label={option.label} />
      ) : (
        <span className="h-7 w-7 shrink-0" aria-hidden="true" />
      )}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12px] font-semibold leading-4">{option.label}</span>
        <span
          className="block truncate text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400"
        >
          {option.detail}
        </span>
        {disabledReason ? (
          <span className="mt-0.5 inline-flex w-fit rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold leading-3 text-amber-700 transition-colors group-hover:bg-amber-200 dark:bg-amber-950/50 dark:text-amber-300 dark:group-hover:bg-amber-900/70">
            {disabledReason}
          </span>
        ) : null}
      </span>
      <span
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          disabled
            ? "border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30"
            : selected
              ? "border-blue-600 bg-blue-600"
              : "border-slate-300 bg-white dark:border-[var(--workspace-border)] dark:bg-[var(--live-well)]",
        )}
        aria-hidden="true"
      >
        {selected ? <span className="h-1.5 w-1.5 rounded-full bg-white" /> : null}
      </span>
    </button>
  );
}

function SummaryModelChoice({
  option,
  selected,
  onSelect,
  disabled,
  disabledReason,
  onCredentialAction,
}: {
  option: SummarizationModelOption;
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  disabledReason?: string;
  onCredentialAction?: () => void;
}) {
  const handleClick = () => {
    if (disabled) {
      onCredentialAction?.();
      return;
    }
    onSelect();
  };

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-disabled={disabled || undefined}
      onClick={handleClick}
      title={`${option.label}: ${option.detail}${disabledReason ? ` - ${disabledReason}` : ""}`}
      className={cn(
        "group flex min-h-[44px] w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left outline-none transition-[background-color,box-shadow,transform] duration-200",
        "active:translate-y-px",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white",
        disabled
          ? "cursor-pointer text-slate-700 hover:bg-amber-50/75 dark:text-slate-300 dark:hover:bg-amber-950/20"
          : selected
          ? "bg-blue-50 text-blue-950 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.18)] dark:bg-blue-950/35 dark:text-blue-100"
          : "text-slate-800 hover:bg-slate-100/80 dark:text-slate-200 dark:hover:bg-[var(--live-card-hover)]",
      )}
    >
      <ProviderIcon icon={option.icon} label={option.label} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12px] font-semibold leading-4">{option.label}</span>
        <span
          className="block truncate text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400"
        >
          {option.detail}
        </span>
        {disabledReason ? (
          <span className="mt-0.5 inline-flex w-fit rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold leading-3 text-amber-700 transition-colors group-hover:bg-amber-200 dark:bg-amber-950/50 dark:text-amber-300 dark:group-hover:bg-amber-900/70">
            {disabledReason}
          </span>
        ) : null}
      </span>
      <span
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          disabled
            ? "border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30"
            : selected
              ? "border-blue-600 bg-blue-600"
              : "border-slate-300 bg-white dark:border-[var(--workspace-border)] dark:bg-[var(--live-well)]",
        )}
        aria-hidden="true"
      >
        {selected ? <span className="h-1.5 w-1.5 rounded-full bg-white" /> : null}
      </span>
    </button>
  );
}

function FieldShell({
  label,
  children,
  detail,
}: {
  label: string;
  children: ReactNode;
  detail?: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-[10.5px] font-bold text-slate-600 dark:text-slate-300">{label}</Label>
      {children}
      {detail ? <p className="text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">{detail}</p> : null}
    </div>
  );
}

function maskedSecret(value: string): string {
  return hasValue(value) ? "************" : "";
}

function SonioxRegionPicker({
  value,
  onValueChange,
}: {
  value: "us" | "eu";
  onValueChange: (value: "us" | "eu") => void;
}) {
  const { t } = useI18n();
  const options = [
    {
      value: "us" as const,
      label: t("US - Region (default)"),
      detail: t("Use the standard Soniox US project and API endpoint."),
    },
    {
      value: "eu" as const,
      label: t("EUR - Region (recommended for better latency)"),
      detail: t("Process and store audio and transcripts in the European Union."),
    },
  ];

  return (
    <FieldShell
      label={t("Data processing region")}
      detail={t("This selection applies to Soniox realtime and uploaded-audio transcription.")}
    >
      <fieldset className="space-y-2.5">
        <legend className="sr-only">{t("Soniox data processing region")}</legend>
        <div className="grid gap-2">
          {options.map((option) => {
            const selected = value === option.value;
            return (
              <label
                key={option.value}
                className={cn(
                  "flex min-h-[64px] cursor-pointer items-start gap-2.5 rounded-xl px-3 py-2.5 outline-none transition-[background-color,box-shadow,transform] duration-150 active:scale-[0.99]",
                  selected
                    ? "bg-blue-50 text-blue-950 shadow-[inset_0_0_0_1.5px_rgba(37,99,235,0.38)] dark:bg-blue-950/35 dark:text-blue-100"
                    : "bg-slate-50 text-slate-800 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.08)] hover:bg-slate-100/80 dark:bg-[var(--live-card)] dark:text-slate-200 dark:hover:bg-[var(--live-card-hover)]",
                )}
              >
                <input
                  type="radio"
                  name="soniox-data-region"
                  value={option.value}
                  checked={selected}
                  onChange={() => onValueChange(option.value)}
                  className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2"
                />
                <span className="min-w-0">
                  <span className="block text-[11.5px] font-semibold leading-4">{option.label}</span>
                  <span className="mt-1 block text-[10.5px] leading-[15px] text-slate-500 dark:text-slate-400">
                    {option.detail}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
        <div className="rounded-xl border border-amber-500/35 bg-amber-50 p-3 text-[11px] leading-[16px] text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-300" aria-hidden="true" />
            <div>
              <p className="font-semibold">{t("EU access must be enabled by Soniox first")}</p>
              <p className="mt-1">
                {t("Email Soniox with your Organization ID so they can enable regional deployments. Then open the Soniox API Console, create a new project with the European Union region, and paste that separate EU project's API key above. The selected region and API key must match.")}
              </p>
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                <button
                  type="button"
                  onClick={() => void openExternalHelpUrl(API_KEY_HELP_LINKS.soniox.href)}
                  className="inline-flex items-center gap-1 rounded-md font-semibold text-amber-950 underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60 dark:text-amber-100"
                >
                  {t("Open Soniox API Console")}
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => void openExternalHelpUrl(SONIOX_REGION_SUPPORT_URL)}
                  className="inline-flex items-center gap-1 rounded-md font-semibold text-amber-950 underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60 dark:text-amber-100"
                >
                  {t("Email Soniox support")}
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => void openExternalHelpUrl(SONIOX_DATA_RESIDENCY_URL)}
                  className="inline-flex items-center gap-1 rounded-md font-semibold text-amber-950 underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60 dark:text-amber-100"
                >
                  {t("Read the official setup guide")}
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </fieldset>
    </FieldShell>
  );
}

function ApiCredentialRow({
  provider,
  credentialId = provider,
  icon,
  value,
  onValueChange,
  show,
  onShowChange,
  open,
  onOpenChange,
  preserveScrollOnClose,
  onPreservedCloseAutoFocus,
  helpKey,
  saved,
  onSave,
  note,
  placeholder,
  inputType = "password",
  children,
}: {
  provider: string;
  credentialId?: string;
  icon?: ProviderIconKey;
  value: string;
  onValueChange: (value: string) => void;
  show?: boolean;
  onShowChange?: (value: boolean) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  preserveScrollOnClose?: boolean;
  onPreservedCloseAutoFocus?: () => void;
  helpKey: ApiKeyHelpKey;
  saved: boolean;
  onSave: () => void;
  note?: string;
  placeholder?: string;
  inputType?: "password" | "text";
  children?: ReactNode;
}) {
  const { t } = useI18n();
  const hasCredential = hasValue(value);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <button
          type="button"
          data-credential-id={credentialId}
          className="group grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5 rounded-lg px-2 py-1.5 text-left outline-none transition-colors hover:bg-slate-100/80 focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:hover:bg-[var(--live-card-hover)]"
        >
          <span className="flex min-w-0 items-center gap-2">
            <ProviderIcon icon={icon} label={provider} className="h-5.5 w-5.5 rounded-[7px] p-1" />
            <span className="min-w-0">
              <span className="block truncate text-[11.5px] font-semibold leading-[15px] text-slate-950 dark:text-slate-100">
                {provider}
              </span>
              <span className={cn("block truncate font-mono text-[10px] leading-3", hasCredential ? "text-slate-500" : "text-slate-400")}>
                {maskedSecret(value) || t("Not set")}
              </span>
            </span>
          </span>
          <span className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-blue-600 group-hover:text-blue-700 dark:text-blue-400">
            {t("Open")}
            <ArrowRight className="h-3 w-3" aria-hidden="true" />
          </span>
        </button>
      </DialogTrigger>
      <DialogContent
        className="sm:max-w-[520px]"
        onCloseAutoFocus={
          preserveScrollOnClose
            ? (event) => {
                event.preventDefault();
                onPreservedCloseAutoFocus?.();
              }
            : undefined
        }
      >
        <DialogHeader>
          <DialogTitle>{provider}</DialogTitle>
          <DialogDescription>
            {note || t("Add or update the credential for this provider.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <FieldShell label={t("Credential")}>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Input
                  type={inputType === "text" ? "text" : show ? "text" : "password"}
                  value={value}
                  onChange={(event) => onValueChange(event.target.value)}
                  placeholder={placeholder || t("Enter {{provider}} credential", { provider })}
                  className="pr-10 font-mono text-sm"
                />
                {typeof show === "boolean" && onShowChange ? (
                  <button
                    type="button"
                    onClick={() => onShowChange(!show)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 transition-colors hover:text-slate-950 dark:hover:text-slate-100"
                    aria-label={show ? t("Hide credential") : t("Show credential")}
                  >
                    {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                ) : null}
              </div>
              <Button
                variant={saved ? "default" : "outline"}
                onClick={onSave}
                className={cn(saved && "border-emerald-600 bg-emerald-600 text-white hover:bg-emerald-700")}
              >
                {saved ? <Check className="mr-2 h-4 w-4" /> : <Save className="mr-2 h-4 w-4" />}
                {saved ? t("Saved") : t("Save")}
              </Button>
            </div>
          </FieldShell>
          {children}
          <ApiKeyLink helpKey={helpKey}>{t("Open provider page")}</ApiKeyLink>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function Settings() {
  const { t, locale, localeTag, setLocale, formatDate, formatNumber } = useI18n();
  const providerModelOptions = useMemo(
    () => createProviderModelOptions(localeTag, t),
    [localeTag, t],
  );
  const summarizationModelOptions = useMemo(
    () => createSummarizationModelOptions(localeTag, t),
    [localeTag, t],
  );
  const postProcessingModelOptions = useMemo(
    () => createPostProcessingModelOptions(localeTag, t),
    [localeTag, t],
  );
  const queryClient = useQueryClient();
  const [openAIKey, setOpenAIKey] = useState("");
  const [deepgramKey, setDeepgramKey] = useState("");
  const [assemblyAIKey, setAssemblyAIKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [openRouterKey, setOpenRouterKey] = useState("");
  const [cerebrasKey, setCerebrasKey] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [sonioxKey, setSonioxKey] = useState("");
  const [sonioxRegion, setSonioxRegion] = useState<"us" | "eu">("us");
  const [modulateKey, setModulateKey] = useState("");
  const [mistralKey, setMistralKey] = useState("");
  const [smallestKey, setSmallestKey] = useState("");
  const [elevenLabsKey, setElevenLabsKey] = useState("");
  const [azureMaiKey, setAzureMaiKey] = useState("");
  const [azureMaiRegion, setAzureMaiRegion] = useState("northeurope");
  const [azureMaiModel, setAzureMaiModel] = useState("mai-transcribe-1.5");
  const [gladiaKey, setGladiaKey] = useState("");
  const [groqKey, setGroqKey] = useState("");
  const [speechmaticsKey, setSpeechmaticsKey] = useState("");
  const [googleApplicationCredentials, setGoogleApplicationCredentials] = useState("");

  const [customVocabulary, setCustomVocabulary] = useState("");
  const savedCustomVocabularyRef = useRef("");
  const pendingCustomVocabularyRef = useRef<string | null>(null);
  const customVocabularySaveInFlightRef = useRef<Promise<void> | null>(null);
  const settingsUpdateQueueRef = useRef<Promise<void>>(Promise.resolve());
  const [summarizationPrompt, setSummarizationPrompt] = useState("");
  const [postProcessingPrompt, setPostProcessingPrompt] = useState(DEFAULT_POST_PROCESSING_PROMPT);

  const [showOpenAIKey, setShowOpenAIKey] = useState(false);
  const [showDeepgramKey, setShowDeepgramKey] = useState(false);
  const [showAssemblyAIKey, setShowAssemblyAIKey] = useState(false);
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [showOpenRouterKey, setShowOpenRouterKey] = useState(false);
  const [showCerebrasKey, setShowCerebrasKey] = useState(false);
  const [showYoutubeKey, setShowYoutubeKey] = useState(false);
  const [showSonioxKey, setShowSonioxKey] = useState(false);
  const [showModulateKey, setShowModulateKey] = useState(false);
  const [showMistralKey, setShowMistralKey] = useState(false);
  const [showSmallestKey, setShowSmallestKey] = useState(false);
  const [showElevenLabsKey, setShowElevenLabsKey] = useState(false);
  const [showAzureMaiKey, setShowAzureMaiKey] = useState(false);
  const [showGladiaKey, setShowGladiaKey] = useState(false);
  const [showGroqKey, setShowGroqKey] = useState(false);
  const [showSpeechmaticsKey, setShowSpeechmaticsKey] = useState(false);

  const [hotkey, setHotkey] = useState("Ctrl + Shift + D");
  const [postProcessingHotkey, setPostProcessingHotkey] = useState("Ctrl + Shift + F");
  const [meetingHotkey, setMeetingHotkey] = useState("Ctrl + Shift + M");
  const [sonioxRealtimeModel, setSonioxRealtimeModel] = useState("stt-rt-v5");
  const [meetingTranscriptionMode, setMeetingTranscriptionMode] = useState<MeetingTranscriptionMode>("live_final");
  const [meetingFinalProvider, setMeetingFinalProvider] = useState("soniox_async");
  const [meetingAnalysisModel, setMeetingAnalysisModel] = useState(DEFAULT_SUMMARIZATION_MODEL);
  const [meetingSmartTurnEnabled, setMeetingSmartTurnEnabled] = useState(true);
  const [meetingAutoAnalyze, setMeetingAutoAnalyze] = useState(true);
  const [meetingAecEnabled, setMeetingAecEnabled] = useState(true);
  const [meetingAudioRetentionDays, setMeetingAudioRetentionDays] = useState(0);
  const [speakerDiarizationFallbackEnabled, setSpeakerDiarizationFallbackEnabled] = useState(true);
  const [diarizationComponent, setDiarizationComponent] = useState<DiarizationComponentStatus | null>(null);
  const [diarizationComponentPending, setDiarizationComponentPending] = useState(false);
  const [recordingMode, setRecordingMode] = useState("press_hold");
  const [isRecordingHotkey, setIsRecordingHotkey] = useState(false);
  const [isRecordingPostProcessingHotkey, setIsRecordingPostProcessingHotkey] = useState(false);
  const [isRecordingMeetingHotkey, setIsRecordingMeetingHotkey] = useState(false);
  const hotkeyCaptureRef = useRef<HTMLDivElement | null>(null);
  const postProcessingHotkeyCaptureRef = useRef<HTMLDivElement | null>(null);
  const meetingHotkeyCaptureRef = useRef<HTMLDivElement | null>(null);
  const { toast } = useToast();
  const [savedKeys, setSavedKeys] = useState<Record<string, boolean>>({});
  const savedKeyResetTimersRef = useRef<Map<string, number>>(new Map());
  const [credentialReadyKeys, setCredentialReadyKeys] = useState<Record<string, boolean>>({});
  const [credentialDialogProvider, setCredentialDialogProvider] = useState<string | null>(null);
  const remoteCredentialDialogScrollRef = useRef<ScrollSnapshot | null>(null);

  const [inputDevices, setInputDevices] = useState<MicrophoneDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("default");
  const [transcriptionModel, setTranscriptionModel] = useState("soniox-realtime");
  const [summarizationModel, setSummarizationModel] = useState(DEFAULT_SUMMARIZATION_MODEL);
  const [postProcessingModel, setPostProcessingModel] = useState(DEFAULT_POST_PROCESSING_MODEL);
  const [autoSummarize, setAutoSummarize] = useState(false);
  const [youtubePreferCaptions, setYoutubePreferCaptions] = useState(true);
  const [voiceprintLibraryOptIn, setVoiceprintLibraryOptIn] = useState(false);
  const [mergeTargetProfileId, setMergeTargetProfileId] = useState("");
  const [mergeSourceProfileId, setMergeSourceProfileId] = useState("");
  const [editingSpeakerProfileId, setEditingSpeakerProfileId] = useState("");
  const [speakerProfileName, setSpeakerProfileName] = useState("");
  const [postProcessingEnabled, setPostProcessingEnabled] = useState(true);
  const [language, setLanguage] = useState("auto");
  const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);
  const [savedVisualizerBarCount, setSavedVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);
  const [autostartEnabled, setAutostartEnabled] = useState(false);
  const [autostartAvailable, setAutostartAvailable] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [desktopUpdate, setDesktopUpdate] = useState<DesktopUpdateStatus>(initialDesktopUpdateStatus);
  const [desktopUpdateProgress, setDesktopUpdateProgress] = useState<DesktopUpdateProgress | null>(null);
  const [isCheckingDesktopUpdate, setIsCheckingDesktopUpdate] = useState(false);
  const [isInstallingDesktopUpdate, setIsInstallingDesktopUpdate] = useState(false);
  const [micAlwaysOn, setMicAlwaysOn] = useState(false);
  const [segmentSpeechWithVad, setSegmentSpeechWithVad] = useState(false);
  const [segmentSpeechWithVadSaving, setSegmentSpeechWithVadSaving] = useState(false);
  const [favoriteMic, setFavoriteMic] = useState("");
  const [isMicDropdownOpen, setIsMicDropdownOpen] = useState(false);
  const [isLanguageDropdownOpen, setIsLanguageDropdownOpen] = useState(false);
  const [isTranscriptionModelDropdownOpen, setIsTranscriptionModelDropdownOpen] = useState(false);
  const [speakerProfilePendingDelete, setSpeakerProfilePendingDelete] = useState<{ id: string; name: string } | null>(null);
  const [voiceLibraryDeleteOpen, setVoiceLibraryDeleteOpen] = useState(false);
  const [outlookDisconnectOpen, setOutlookDisconnectOpen] = useState(false);
  const [voiceLibraryDeletePending, setVoiceLibraryDeletePending] = useState(false);
  const [voiceEnrollmentOpen, setVoiceEnrollmentOpen] = useState(false);
  const [voiceEnrollmentName, setVoiceEnrollmentName] = useState("");
  const [voiceEnrollmentDevice, setVoiceEnrollmentDevice] = useState(DEFAULT_VOICE_ENROLLMENT_DEVICE);
  const [voiceEnrollmentStartedAt, setVoiceEnrollmentStartedAt] = useState<number | null>(null);
  const [voiceEnrollmentProgress, setVoiceEnrollmentProgress] = useState(0);
  const [voiceEnrollmentStage, setVoiceEnrollmentStage] = useState<"idle" | "preparing" | "recording" | "processing" | "success" | "error">("idle");
  const [voiceEnrollmentResult, setVoiceEnrollmentResult] = useState<SpeakerEnrollmentResponse | null>(null);

  const [onnxAvailable, setOnnxAvailable] = useState<boolean | null>(null);
  const [onnxMessage, setOnnxMessage] = useState("");
  const [onnxModels, setOnnxModels] = useState<OnnxModelInfo[]>([]);
  const [onnxModel, setOnnxModel] = useState("");
  const [onnxQuantization, setOnnxQuantization] = useState("int8");
  const onnxModelActionInFlightRef = useRef<Set<string>>(new Set());

  const speakerProfilesQuery = useQuery<SpeakerProfilesResponse>({
    queryKey: ["/api/meetings/speaker-profiles"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(apiUrl("/api/meetings/speaker-profiles"), {
        credentials: "include",
        signal,
      }, 10_000);
      if (!response.ok) throw new Error(`Saved speakers unavailable (${response.status})`);
      return response.json();
    },
  });
  const voiceEnrollmentDevicesQuery = useQuery<MeetingAudioDevicesResponse>({
    queryKey: ["/api/meetings/audio-devices"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(apiUrl("/api/meetings/audio-devices"), {
        credentials: "include",
        signal,
      }, 10_000);
      if (!response.ok) throw new Error(`Microphones unavailable (${response.status})`);
      return response.json();
    },
    enabled: voiceEnrollmentOpen,
    staleTime: 10_000,
  });
  const meetingProfilesQuery = useQuery<MeetingProfilesResponse>({
    queryKey: ["/api/meeting-profiles"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(apiUrl("/api/meeting-profiles"), {
        credentials: "include",
        signal,
      }, 10_000);
      if (!response.ok) throw new Error(`Meeting transcription options unavailable (${response.status})`);
      return response.json();
    },
  });
  const speakerModelQuery = useQuery<SpeakerModelStatus>({
    queryKey: ["/api/meetings/speaker-model"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(apiUrl("/api/meetings/speaker-model"), {
        credentials: "include",
        signal,
      }, 10_000);
      if (!response.ok) throw new Error(`Speaker model unavailable (${response.status})`);
      return response.json();
    },
  });
  const outlookQuery = useQuery<OutlookCalendarStatus>({
    queryKey: ["/api/calendar/outlook/status"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(apiUrl("/api/calendar/outlook/status"), {
        credentials: "include",
        signal,
      }, 10_000);
      if (!response.ok) throw new Error(`Outlook status unavailable (${response.status})`);
      return response.json();
    },
    refetchInterval: (query) => query.state.data?.authorizationPending ? 2_000 : false,
  });
  const speakerProfileMutation = useMutation({
    mutationFn: async ({ action, id, displayName }: { action: "rename" | "delete"; id: string; displayName?: string }) => {
      const response = action === "delete"
        ? await apiRequest("DELETE", `/api/meetings/speaker-profiles/${id}`)
        : await apiRequest("PATCH", `/api/meetings/speaker-profiles/${id}`, { displayName });
      return response.json();
    },
    onSuccess: (_result, variables) => {
      setSpeakerProfilePendingDelete(null);
      setEditingSpeakerProfileId("");
      setSpeakerProfileName("");
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      toast({ title: variables.action === "delete" ? t("Saved speaker deleted") : t("Speaker name saved") });
    },
    onError: (error) => toast({ title: t("Saved speaker could not be updated"), description: localizedSettingsError(error, "The requested settings action failed.", locale, t), variant: "destructive" }),
  });
  const speakerModelMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", "/api/meetings/speaker-model");
      return response.json() as Promise<SpeakerModelStatus>;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-model"] });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      toast({ title: t("Voice recognition ready"), description: t("Scriber can now recognize familiar speakers in new meetings.") });
    },
    onError: (error) => toast({ title: t("Voice recognition download failed"), description: localizedSettingsError(error, "The requested settings action failed.", locale, t), variant: "destructive" }),
  });
  const voiceEnrollmentMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", "/api/meetings/speaker-profiles/enroll", {
        displayName: voiceEnrollmentName.trim(),
        durationMs: VOICE_ENROLLMENT_DURATION_MS,
        microphoneNativeEndpointIdHash: voiceEnrollmentDevice === DEFAULT_VOICE_ENROLLMENT_DEVICE ? "" : voiceEnrollmentDevice,
      });
      return response.json() as Promise<SpeakerEnrollmentResponse>;
    },
    onMutate: () => {
      setVoiceEnrollmentResult(null);
      setVoiceEnrollmentProgress(3);
      setVoiceEnrollmentStage("preparing");
      setVoiceEnrollmentStartedAt(Date.now());
    },
    onSuccess: (result) => {
      setVoiceEnrollmentStartedAt(null);
      setVoiceEnrollmentProgress(100);
      setVoiceEnrollmentStage("success");
      setVoiceEnrollmentResult(result);
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      toast({ title: t("{{name}} is ready", { name: result.profile.displayName }), description: t("Scriber can match this voice in future meetings.") });
    },
    onError: () => {
      setVoiceEnrollmentStartedAt(null);
      setVoiceEnrollmentProgress(0);
      setVoiceEnrollmentStage("error");
    },
  });
  const mergeProfilesMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", "/api/meetings/speaker-profiles/merge", {
        targetProfileId: mergeTargetProfileId,
        sourceProfileId: mergeSourceProfileId,
      });
      return response.json() as Promise<{ targetProfileId: string; mergedProfileId: string }>;
    },
    onSuccess: async (payload) => {
      setMergeTargetProfileId(payload.targetProfileId);
      setMergeSourceProfileId("");
      await refreshAllMeetingSpeakerIdentityCaches(queryClient);
      toast({ title: t("Duplicate speakers merged") });
    },
    onError: (error) => toast({ title: t("Speakers could not be merged"), description: localizedSettingsError(error, "The requested settings action failed.", locale, t), variant: "destructive" }),
  });
  const outlookMutation = useMutation({
    mutationFn: async (action: "connect" | "sync" | "disconnect") => {
      const response = action === "disconnect"
        ? await apiRequest("DELETE", "/api/calendar/outlook")
        : await apiRequest(
          "POST",
          `/api/calendar/outlook/${action}`,
          action === "connect" ? { openBrowser: true } : undefined,
          action === "sync" ? { timeoutMs: OUTLOOK_SYNC_REQUEST_TIMEOUT_MS } : undefined,
        );
      return response.json() as Promise<OutlookCalendarSyncResponse | Record<string, unknown>>;
    },
    onSuccess: (_result, action) => {
      if (action === "sync") {
        // A successful sync changes both the lightweight connection status and
        // every cached day view. Read status back from its authoritative
        // endpoint instead of assuming every compatible backend returns the
        // complete status object in the mutation response.
        void queryClient.refetchQueries({
          queryKey: ["/api/calendar/outlook/status"],
          exact: true,
          type: "active",
        });
        void queryClient.invalidateQueries({
          queryKey: ["/api/calendar/outlook/events"],
        });
      } else {
        void queryClient.invalidateQueries({
          queryKey: ["/api/calendar/outlook/status"],
          exact: true,
        });
        queryClient.removeQueries({ queryKey: ["/api/calendar/outlook/events"] });
      }
      if (action === "disconnect") setOutlookDisconnectOpen(false);
      toast({ title: action === "connect" ? t("Continue in your browser") : action === "sync" ? t("Outlook calendar synchronized") : t("Outlook disconnected") });
    },
    onError: (error) => {
      // A failed refresh can be the first proof that Microsoft revoked the
      // stored credential. Refresh the lightweight status contract so the UI
      // immediately offers Reconnect instead of continuing to show Connected.
      void queryClient.invalidateQueries({
        queryKey: ["/api/calendar/outlook/status"],
        exact: true,
      });
      toast({ title: t("Outlook action failed"), description: localizedSettingsError(error, "The requested settings action failed.", locale, t), variant: "destructive" });
    },
  });
  const outlookCredentialStatusUnavailable = outlookQuery.data?.credentialStatusAvailable === false;

  useEffect(() => {
    return subscribeDesktopUpdateStatus(setDesktopUpdate);
  }, []);

  useEffect(() => () => {
    savedKeyResetTimersRef.current.forEach((timer) => {
      window.clearTimeout(timer);
    });
    savedKeyResetTimersRef.current.clear();
  }, []);

  useEffect(() => {
    if (!voiceEnrollmentMutation.isPending || voiceEnrollmentStartedAt === null) {
      return;
    }
    const updateEnrollmentProgress = () => {
      const elapsedMs = Date.now() - voiceEnrollmentStartedAt;
      if (elapsedMs < 600) {
        setVoiceEnrollmentStage("preparing");
        setVoiceEnrollmentProgress(Math.max(3, Math.round((elapsedMs / 600) * 8)));
        return;
      }
      if (elapsedMs < VOICE_ENROLLMENT_DURATION_MS + 600) {
        setVoiceEnrollmentStage("recording");
        const recordingElapsed = elapsedMs - 600;
        setVoiceEnrollmentProgress(Math.min(86, 8 + Math.round((recordingElapsed / VOICE_ENROLLMENT_DURATION_MS) * 78)));
        return;
      }
      setVoiceEnrollmentStage("processing");
      setVoiceEnrollmentProgress(92);
    };
    updateEnrollmentProgress();
    const timer = window.setInterval(updateEnrollmentProgress, 150);
    return () => window.clearInterval(timer);
  }, [voiceEnrollmentMutation.isPending, voiceEnrollmentStartedAt]);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    let cancelled = false;
    void checkDesktopUpdateIfDue({ force: true })
      .then((result) => {
        if (!cancelled) {
          setDesktopUpdate(result.status);
        }
      })
      .catch((error) => {
        console.debug("Settings update background check failed.", error);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const consumeRequestedSection = () => {
      let section = "";
      try {
        section = window.sessionStorage.getItem(SETTINGS_SECTION_REQUEST_KEY) || "";
        if (section) {
          window.sessionStorage.removeItem(SETTINGS_SECTION_REQUEST_KEY);
        }
      } catch {
        section = "";
      }
      if (section) {
        revealRequestedSettingsSection(section);
      }
    };

    const handleSectionRequest = (event: Event) => {
      const section = String((event as CustomEvent<{ section?: string }>).detail?.section || "");
      if (section) {
        revealRequestedSettingsSection(section);
      }
    };

    consumeRequestedSection();
    const retryTimer = window.setTimeout(consumeRequestedSection, 120);
    window.addEventListener("scriber-open-settings-section", handleSectionRequest);
    return () => {
      window.clearTimeout(retryTimer);
      window.removeEventListener("scriber-open-settings-section", handleSectionRequest);
    };
  }, []);

  const savedCredentialAvailable = (provider: string, value: string, extraValue?: string) =>
    credentialReadyKeys[provider] === true && hasValue(value) && (extraValue === undefined || hasValue(extraValue));

  const restoreRemoteCredentialDialogScroll = useCallback(() => {
    const snapshot = remoteCredentialDialogScrollRef.current;
    remoteCredentialDialogScrollRef.current = null;
    if (!snapshot || typeof window === "undefined") {
      return;
    }
    window.requestAnimationFrame(() => {
      restoreScrollSnapshot(snapshot);
      window.requestAnimationFrame(() => restoreScrollSnapshot(snapshot));
    });
  }, []);

  const openCredentialDialog = useCallback((requirement: CredentialRequirement | null) => {
    if (!requirement) {
      return;
    }
    remoteCredentialDialogScrollRef.current = captureScrollSnapshot();
    setCredentialDialogProvider(requirement.provider);
  }, []);

  const credentialDialogProps = (credentialId: string) => ({
    open: credentialDialogProvider === credentialId,
    onOpenChange: (open: boolean) => {
      if (open) {
        remoteCredentialDialogScrollRef.current = null;
      }
      setCredentialDialogProvider((current) => {
        if (open) {
          return credentialId;
        }
        return current === credentialId ? null : current;
      });
    },
    preserveScrollOnClose: credentialDialogProvider === credentialId && remoteCredentialDialogScrollRef.current !== null,
    onPreservedCloseAutoFocus: restoreRemoteCredentialDialogScroll,
  });

  const isCredentialReady = (requirement: CredentialRequirement | null) => {
    if (!requirement) {
      return true;
    }
    switch (requirement.provider) {
      case "OpenAI":
        return savedCredentialAvailable("OpenAI", openAIKey);
      case "Gemini":
        return savedCredentialAvailable("Gemini", geminiKey);
      case "OpenRouter":
        return savedCredentialAvailable("OpenRouter", openRouterKey);
      case "Cerebras":
        return savedCredentialAvailable("Cerebras", cerebrasKey);
      case "Soniox":
        return savedCredentialAvailable("Soniox", sonioxKey);
      case "Modulate.AI":
        return savedCredentialAvailable("Modulate.AI", modulateKey);
      case "Mistral":
        return savedCredentialAvailable("Mistral", mistralKey);
      case "Smallest AI":
        return savedCredentialAvailable("Smallest AI", smallestKey);
      case "AssemblyAI":
        return savedCredentialAvailable("AssemblyAI", assemblyAIKey);
      case "Deepgram":
        return savedCredentialAvailable("Deepgram", deepgramKey);
      case "Azure":
        return savedCredentialAvailable("Azure", azureMaiKey, azureMaiRegion);
      case "Gladia":
        return savedCredentialAvailable("Gladia", gladiaKey);
      case "Groq":
        return savedCredentialAvailable("Groq", groqKey);
      case "Speechmatics":
        return savedCredentialAvailable("Speechmatics", speechmaticsKey);
      case "ElevenLabs":
        return savedCredentialAvailable("ElevenLabs", elevenLabsKey);
      case "Google Cloud":
        return savedCredentialAvailable("Google Cloud", googleApplicationCredentials);
      default:
        return false;
    }
  };

  const missingCredentialReason = (requirement: CredentialRequirement | null) =>
    requirement && !isCredentialReady(requirement) ? t(MISSING_CREDENTIAL_CTA) : undefined;

  const requiredCredentialForTranscriptionModel = (model: string): CredentialRequirement | null => {
    switch (model) {
      case "soniox-realtime":
      case "soniox-async":
        return { provider: "Soniox", label: "Soniox API key", helpKey: "soniox" };
      case "modulate-realtime":
      case "modulate-async":
        return { provider: "Modulate.AI", label: "Modulate.AI API key", helpKey: "modulate" };
      case "gemini-stt":
        return { provider: "Gemini", label: "Gemini API key", helpKey: "gemini" };
      case "mistral-realtime":
      case "mistral-async":
        return { provider: "Mistral", label: "Mistral API key", helpKey: "mistral" };
      case "smallest-realtime":
      case "smallest-async":
        return { provider: "Smallest AI", label: "Smallest AI API key", helpKey: "smallest" };
      case "assemblyai":
      case "assemblyai-realtime":
        return { provider: "AssemblyAI", label: "AssemblyAI API key", helpKey: "assemblyai" };
      case "deepgram":
      case "deepgram-async":
        return { provider: "Deepgram", label: "Deepgram API key", helpKey: "deepgram" };
      case "openai":
      case "openai-async":
        return { provider: "OpenAI", label: "OpenAI API key", helpKey: "openai" };
      case "azure_mai":
        return { provider: "Azure", label: "Azure MAI Speech key and region", helpKey: "azure" };
      case "gladia":
      case "gladia-async":
        return { provider: "Gladia", label: "Gladia API key", helpKey: "gladia" };
      case "groq":
        return { provider: "Groq", label: "Groq API key", helpKey: "groq" };
      case "speechmatics":
      case "speechmatics-async":
        return { provider: "Speechmatics", label: "Speechmatics API key", helpKey: "speechmatics" };
      case "elevenlabs":
        return { provider: "ElevenLabs", label: "ElevenLabs API key", helpKey: "elevenlabs" };
      case "google":
        return { provider: "Google Cloud", label: "Google Cloud credentials", helpKey: "googleCloud" };
      default:
        return null;
    }
  };

  const requiredCredentialForLanguageModel = (model: string): CredentialRequirement | null => {
    if (model.startsWith("gpt-")) {
      return { provider: "OpenAI", label: "OpenAI API key", helpKey: "openai" };
    }
    if (model.startsWith("gemini-")) {
      return { provider: "Gemini", label: "Gemini API key", helpKey: "gemini" };
    }
    if (model.startsWith("cerebras/")) {
      return { provider: "Cerebras", label: "Cerebras API key", helpKey: "cerebras" };
    }
    if (model.includes("/")) {
      return { provider: "OpenRouter", label: "OpenRouter API key", helpKey: "openrouter" };
    }
    return null;
  };

  const selectedCredentialRequirement = requiredCredentialForTranscriptionModel(transcriptionModel);
  const missingSelectedCredentialRequirement = isCredentialReady(selectedCredentialRequirement)
    ? null
    : selectedCredentialRequirement;
  const missingSummarizationCredentialRequirement = (() => {
    const requirement = requiredCredentialForLanguageModel(summarizationModel);
    return isCredentialReady(requirement) ? null : requirement;
  })();
  const missingPostProcessingCredentialRequirement = (() => {
    const requirement = requiredCredentialForLanguageModel(postProcessingModel);
    return isCredentialReady(requirement) ? null : requirement;
  })();
  const selectedPostProcessingModelOption =
    postProcessingModelOptions.find((option) => option.value === postProcessingModel) ?? null;
  const selectedMeetingFinalOption =
    MEETING_FINAL_STT_OPTIONS.find((option) => option.value === meetingFinalProvider)
    ?? MEETING_FINAL_STT_OPTIONS[0];
  const selectedMeetingProfile = meetingProfilesQuery.data?.profiles.find(
    (profile) => profile.finalProvider === meetingFinalProvider,
  ) ?? meetingProfilesQuery.data?.profiles[0];
  const meetingCostEstimate = selectedMeetingProfile?.costEstimate;
  const finalOnlyHourlyCost = meetingCostEstimate?.finalPerMeetingHour;
  const liveAndFinalHourlyCost = finalOnlyHourlyCost == null
    ? null
    : finalOnlyHourlyCost + (meetingCostEstimate?.livePreviewPerMeetingHour ?? 0.24);
  const missingActiveCredentialRequirements = uniqueCredentialRequirements([
    missingSelectedCredentialRequirement,
    missingSummarizationCredentialRequirement,
    missingPostProcessingCredentialRequirement,
  ]);

  const hasAnyManagedCloudSttCredential = [
    sonioxKey,
    modulateKey,
    mistralKey,
    smallestKey,
    assemblyAIKey,
    deepgramKey,
    openAIKey,
    azureMaiKey,
    gladiaKey,
    groqKey,
    speechmaticsKey,
    elevenLabsKey,
    geminiKey,
    googleApplicationCredentials,
  ].some(hasValue);

  const markCredentialChanged = (provider: string, setter: (value: string) => void) => (value: string) => {
    setter(value);
    setSavedKeys((prev) => ({ ...prev, [provider]: false }));
    setCredentialReadyKeys((prev) => ({ ...prev, [provider]: false }));
  };

  const loadOnnxModels = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(
        apiUrl("/api/onnx/models"),
        { credentials: "include" },
        30_000,
      );
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data = (await res.json()) as OnnxModelsResponse;
      const available = data.available !== false;
      setOnnxAvailable(available);
      setOnnxMessage(data.message || "");
      const models = data.models || [];
      setOnnxModels(models);

      const current = data.currentModel || "";
      const selected = models.find((m) => m.id === current) ? current : (models[0]?.id || "");
      setOnnxModel(selected);
      setOnnxQuantization(data.quantization || "int8");
    } catch (e: any) {
      setOnnxAvailable(false);
      setOnnxMessage(localizedSettingsError(e, "The requested settings action failed.", locale, t));
    }
  }, [locale, t]);

  useEffect(() => {
    let cancelled = false;

    const serviceToModel = (service: string, sonioxMode: string) => {
      if (service === "soniox" || service === "soniox_async") {
        return sonioxMode === "async" || service === "soniox_async"
          ? "soniox-async"
          : "soniox-realtime";
      }
      if (service === "modulate" || service === "modulate_async") {
        return service === "modulate_async" ? "modulate-async" : "modulate-realtime";
      }
      if (service === "mistral" || service === "mistral_async") {
        return service === "mistral_async" ? "mistral-async" : "mistral-realtime";
      }
      if (service === "smallest" || service === "smallest_async") {
        return service === "smallest_async" ? "smallest-async" : "smallest-realtime";
      }
      if (service === "assemblyai_realtime") {
        return "assemblyai-realtime";
      }
      if (service === "gemini_stt") {
        return "gemini-stt";
      }
      if (service === "deepgram_async") {
        return "deepgram-async";
      }
      if (service === "gladia_async") {
        return "gladia-async";
      }
      if (service === "openai_async") {
        return "openai-async";
      }
      if (service === "speechmatics_async") {
        return "speechmatics-async";
      }
      if (service === "nemo_local") {
        return "onnx_local";
      }
      return service || "soniox-realtime";
    };

    const load = async () => {
      try {
        setSettingsError("");
        const { settings, microphones: mics, autostart } = await loadSettingsBootstrap();
        if (cancelled) return;

        const keys = settings.apiKeys || {};
        setAutostartEnabled(autostart.enabled || false);
        setAutostartAvailable(autostart.available || false);
        setHotkey(settings.hotkey || settings.hotkeyRaw || "Ctrl + Shift + D");
        setPostProcessingHotkey(settings.postProcessingHotkey || settings.postProcessingHotkeyRaw || "Ctrl + Shift + F");
        setMeetingHotkey(settings.meetingHotkey || settings.meetingHotkeyRaw || "Ctrl + Shift + M");
        setSonioxRealtimeModel(settings.sonioxRealtimeModel || "stt-rt-v5");
        setSonioxRegion(settings.sonioxRegion === "eu" ? "eu" : "us");
        setMeetingTranscriptionMode(settings.meetingTranscriptionMode === "final_only" ? "final_only" : "live_final");
        setMeetingFinalProvider(settings.meetingFinalProvider || "soniox_async");
        setMeetingAnalysisModel(settings.meetingAnalysisModel || settings.summarizationModel || DEFAULT_SUMMARIZATION_MODEL);
        setMeetingSmartTurnEnabled(settings.meetingSmartTurnEnabled !== false);
        setMeetingAutoAnalyze(settings.meetingAutoAnalyze !== false);
        setMeetingAecEnabled(settings.meetingAecEnabled !== false);
        setMeetingAudioRetentionDays(settings.meetingAudioRetentionDays ?? 0);
        setSpeakerDiarizationFallbackEnabled(settings.speakerDiarizationFallbackEnabled !== false);
        setRecordingMode(settings.mode === "push_to_talk" ? "press_hold" : "start_stop");
        setSelectedDeviceId(settings.micDevice || "default");
        setLanguage(settings.language || "auto");
        setTranscriptionModel(serviceToModel(settings.defaultSttService || "", settings.sonioxMode || "realtime"));
        savedCustomVocabularyRef.current = settings.customVocab || "";
        setCustomVocabulary(savedCustomVocabularyRef.current);
        setSummarizationPrompt(settings.summarizationPrompt || "");
        setSummarizationModel(settings.summarizationModel || DEFAULT_SUMMARIZATION_MODEL);
        setPostProcessingModel(settings.postProcessingModel || DEFAULT_POST_PROCESSING_MODEL);
        setAutoSummarize(settings.autoSummarize === true);
        setYoutubePreferCaptions(settings.youtubePreferCaptions !== false);
        setVoiceprintLibraryOptIn(settings.voiceprintLibraryOptIn === true);
        setPostProcessingEnabled(settings.postProcessingEnabled !== false);
        setPostProcessingPrompt(settings.postProcessingPrompt || DEFAULT_POST_PROCESSING_PROMPT);
        const loadedVisualizerBarCount = normalizeVisualizerBarCount(settings.visualizerBarCount);
        setVisualizerBarCount(loadedVisualizerBarCount);
        setSavedVisualizerBarCount(loadedVisualizerBarCount);
        setMicAlwaysOn(settings.micAlwaysOn === true);
        setSegmentSpeechWithVad(settings.segmentSpeechWithVad === true);
        setFavoriteMic(settings.favoriteMic || "");

        setSonioxKey(keys.soniox || "");
        setModulateKey(keys.modulate || "");
        setMistralKey(keys.mistral || "");
        setSmallestKey(keys.smallest || "");
        setAssemblyAIKey(keys.assemblyai || "");
        setDeepgramKey(keys.deepgram || "");
        setOpenAIKey(keys.openai || "");
        setGeminiKey(keys.googleApiKey || "");
        setOpenRouterKey(keys.openrouter || "");
        setCerebrasKey(keys.cerebras || "");
        setYoutubeKey(keys.youtubeApiKey || "");
        setElevenLabsKey(keys.elevenlabs || "");
        setAzureMaiKey(keys.azureMaiSpeechKey || "");
        setAzureMaiRegion(keys.azureMaiRegion || "northeurope");
        setAzureMaiModel(keys.azureMaiModel || "mai-transcribe-1.5");
        setGladiaKey(keys.gladia || "");
        setGroqKey(keys.groq || "");
        setSpeechmaticsKey(keys.speechmatics || "");
        setGoogleApplicationCredentials(keys.googleApplicationCredentials || "");
        const loadedCredentialReadyKeys = {
          OpenAI: hasValue(keys.openai),
          Gemini: hasValue(keys.googleApiKey),
          OpenRouter: hasValue(keys.openrouter),
          Cerebras: hasValue(keys.cerebras),
          YouTube: hasValue(keys.youtubeApiKey),
          Soniox: hasValue(keys.soniox),
          "Modulate.AI": hasValue(keys.modulate),
          Mistral: hasValue(keys.mistral),
          "Smallest AI": hasValue(keys.smallest),
          AssemblyAI: hasValue(keys.assemblyai),
          Deepgram: hasValue(keys.deepgram),
          Gladia: hasValue(keys.gladia),
          Groq: hasValue(keys.groq),
          Speechmatics: hasValue(keys.speechmatics),
          ElevenLabs: hasValue(keys.elevenlabs),
          Azure: hasValue(keys.azureMaiSpeechKey) && hasValue(keys.azureMaiRegion || "northeurope"),
          "Google Cloud": hasValue(keys.googleApplicationCredentials),
        };
        setCredentialReadyKeys(loadedCredentialReadyKeys);
        setSavedKeys(loadedCredentialReadyKeys);

        let microphonePayload = mics;
        if (!Array.isArray(microphonePayload.devices)) {
          const micsRes = await fetchWithTimeout(
            apiUrl("/api/microphones"),
            { credentials: "include" },
            10_000,
          );
          if (cancelled) return;
          if (micsRes.ok) {
            microphonePayload = (await micsRes.json()) as MicrophonesResponse;
          }
        }
        setInputDevices(microphonePayload.devices || []);

        // Show page immediately - don't wait for model info
        setSettingsLoaded(true);
      } catch (e: any) {
        setSettingsLoaded(true); // Still mark as loaded even on error
        setSettingsError(localizedSettingsErrorNow(e, "The requested settings action failed."));
        toast({
          title: translateNow("Failed to load settings"),
          description: localizedSettingsErrorNow(e, "The requested settings action failed."),
          duration: 4000,
        });
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [toast]);

  useEffect(() => {
    if (settingsLoaded && onnxAvailable === null) {
      loadOnnxModels();
    }
  }, [settingsLoaded, onnxAvailable, loadOnnxModels]);

  const updateSettings = async (patch: SettingsUpdatePayload): Promise<SettingsResponse> => {
    const request = settingsUpdateQueueRef.current.then(async () => {
      const res = await fetchWithTimeout(apiUrl("/api/settings"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
        credentials: "include",
      }, 15_000);
      if (!res.ok) {
        throw new Error(await responseErrorMessage(res));
      }
      const updatedSettings = (await res.json()) as SettingsResponse;
      queryClient.setQueryData<SettingsResponse>(["/api/settings"], updatedSettings);
      invalidateSettingsBootstrap();
      return updatedSettings;
    });
    settingsUpdateQueueRef.current = request.then(
      () => undefined,
      () => undefined,
    );
    return request;
  };

  const saveCustomVocabulary = useCallback((nextValue: string): Promise<void> => {
    pendingCustomVocabularyRef.current = nextValue;
    if (customVocabularySaveInFlightRef.current) {
      return customVocabularySaveInFlightRef.current;
    }

    const request = (async () => {
      while (pendingCustomVocabularyRef.current !== null) {
        const valueToSave = pendingCustomVocabularyRef.current;
        pendingCustomVocabularyRef.current = null;
        if (valueToSave === savedCustomVocabularyRef.current) {
          continue;
        }
        try {
          await updateSettings({ customVocab: valueToSave });
          savedCustomVocabularyRef.current = valueToSave;
        } catch (e: any) {
          toast({
            title: translateNow("Save failed"),
            description: localizedSettingsErrorNow(e, "The requested settings action failed."),
            duration: 4000,
          });
        }
      }
    })();
    customVocabularySaveInFlightRef.current = request;
    void request.finally(() => {
      if (customVocabularySaveInFlightRef.current === request) {
        customVocabularySaveInFlightRef.current = null;
      }
    });
    return request;
  }, [toast]);

  useEffect(() => {
    if (!settingsLoaded || customVocabulary === savedCustomVocabularyRef.current) {
      return;
    }

    const timer = window.setTimeout(() => {
      void saveCustomVocabulary(customVocabulary);
    }, 650);

    return () => {
      window.clearTimeout(timer);
    };
  }, [customVocabulary, saveCustomVocabulary, settingsLoaded]);

  const refreshMicrophones = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(
        apiUrl("/api/microphones"),
        { credentials: "include" },
        10_000,
      );
      if (!res.ok) {
        return;
      }
      const data = (await res.json()) as MicrophonesResponse;
      const devices = data.devices || [];
      setInputDevices(devices);
      const availableIds = new Set(devices.map((d) => d.deviceId));
      setSelectedDeviceId((prev) => {
        if (prev !== "default" && !availableIds.has(prev)) {
          return "default";
        }
        return prev;
      });
    } catch {
      // Best effort refresh on window focus.
    }
  }, []);

  const handleSaveApiKey = async (provider: string) => {
    try {
      const apiKeys: Record<string, string> = {};
      if (provider === "OpenAI") apiKeys.openai = openAIKey;
      if (provider === "Deepgram") apiKeys.deepgram = deepgramKey;
      if (provider === "AssemblyAI") apiKeys.assemblyai = assemblyAIKey;
      if (provider === "Gemini") apiKeys.googleApiKey = geminiKey;
      if (provider === "OpenRouter") apiKeys.openrouter = openRouterKey;
      if (provider === "Cerebras") apiKeys.cerebras = cerebrasKey;
      if (provider === "YouTube") apiKeys.youtubeApiKey = youtubeKey;
      if (provider === "Soniox") apiKeys.soniox = sonioxKey;
      if (provider === "Modulate.AI") apiKeys.modulate = modulateKey;
      if (provider === "Mistral") apiKeys.mistral = mistralKey;
      if (provider === "Smallest AI") apiKeys.smallest = smallestKey;
      if (provider === "ElevenLabs") apiKeys.elevenlabs = elevenLabsKey;
      if (provider === "Azure") {
        apiKeys.azureMaiSpeechKey = azureMaiKey;
        apiKeys.azureMaiRegion = azureMaiRegion || "northeurope";
        apiKeys.azureMaiModel = azureMaiModel || "mai-transcribe-1.5";
      }
      if (provider === "Gladia") apiKeys.gladia = gladiaKey;
      if (provider === "Groq") apiKeys.groq = groqKey;
      if (provider === "Speechmatics") apiKeys.speechmatics = speechmaticsKey;
      if (provider === "Google Cloud") apiKeys.googleApplicationCredentials = googleApplicationCredentials;

      await updateSettings({
        apiKeys,
        ...(provider === "Soniox" ? { sonioxRegion } : {}),
      });

      const credentialReady = (() => {
        switch (provider) {
          case "OpenAI":
            return hasValue(openAIKey);
          case "Deepgram":
            return hasValue(deepgramKey);
          case "AssemblyAI":
            return hasValue(assemblyAIKey);
          case "Gemini":
            return hasValue(geminiKey);
          case "OpenRouter":
            return hasValue(openRouterKey);
          case "Cerebras":
            return hasValue(cerebrasKey);
          case "YouTube":
            return hasValue(youtubeKey);
          case "Soniox":
            return hasValue(sonioxKey);
          case "Modulate.AI":
            return hasValue(modulateKey);
          case "Mistral":
            return hasValue(mistralKey);
          case "Smallest AI":
            return hasValue(smallestKey);
          case "ElevenLabs":
            return hasValue(elevenLabsKey);
          case "Azure":
            return hasValue(azureMaiKey) && hasValue(azureMaiRegion || "northeurope");
          case "Gladia":
            return hasValue(gladiaKey);
          case "Groq":
            return hasValue(groqKey);
          case "Speechmatics":
            return hasValue(speechmaticsKey);
          case "Google Cloud":
            return hasValue(googleApplicationCredentials);
          default:
            return false;
        }
      })();

      setCredentialReadyKeys((prev) => ({ ...prev, [provider]: credentialReady }));
      setSavedKeys((prev) => ({ ...prev, [provider]: credentialReady }));
      toast({
        title: t("Saved"),
        description: t("{{provider}} settings updated.", { provider }),
        duration: 2000,
      });
      const previousResetTimer = savedKeyResetTimersRef.current.get(provider);
      if (previousResetTimer !== undefined) {
        window.clearTimeout(previousResetTimer);
      }
      const resetTimer = window.setTimeout(() => {
        setSavedKeys((prev) => ({ ...prev, [provider]: false }));
        savedKeyResetTimersRef.current.delete(provider);
      }, 2000);
      savedKeyResetTimersRef.current.set(provider, resetTimer);
    } catch (e: any) {
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleHotkeyRecord = useCallback((event: HotkeyCaptureEvent) => {
    event.preventDefault?.();
    event.stopPropagation?.();
    if (event.key === "Escape") {
      setIsRecordingHotkey(false);
      return;
    }
    const nextHotkey = hotkeyDisplayFromKeyboardEvent(event);
    if (nextHotkey) {
      setHotkey(nextHotkey);
    }
  }, []);

  const handlePostProcessingHotkeyRecord = useCallback((event: HotkeyCaptureEvent) => {
    event.preventDefault?.();
    event.stopPropagation?.();
    if (event.key === "Escape") {
      setIsRecordingPostProcessingHotkey(false);
      return;
    }
    const nextHotkey = hotkeyDisplayFromKeyboardEvent(event);
    if (nextHotkey) {
      setPostProcessingHotkey(nextHotkey);
    }
  }, []);

  const handleMeetingHotkeyRecord = useCallback((event: HotkeyCaptureEvent) => {
    event.preventDefault?.();
    event.stopPropagation?.();
    if (event.key === "Escape") {
      setIsRecordingMeetingHotkey(false);
      return;
    }
    const nextHotkey = hotkeyDisplayFromKeyboardEvent(event);
    if (nextHotkey) setMeetingHotkey(nextHotkey);
  }, []);

  useEffect(() => {
    if (!isRecordingHotkey) {
      return;
    }
    void setGlobalHotkeyCaptureActive(true).catch((error) => {
      console.debug("Could not suspend global hotkey while recording shortcut.", error);
    });
    const focusFrame = window.requestAnimationFrame(() => {
      hotkeyCaptureRef.current?.focus();
    });
    const handleWindowKeyDown = (event: KeyboardEvent) => {
      handleHotkeyRecord(event);
    };
    window.addEventListener("keydown", handleWindowKeyDown, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleWindowKeyDown, true);
      void setGlobalHotkeyCaptureActive(false).catch((error) => {
        console.debug("Could not resume global hotkey after recording shortcut.", error);
      });
    };
  }, [handleHotkeyRecord, isRecordingHotkey]);

  useEffect(() => {
    if (!isRecordingPostProcessingHotkey) {
      return;
    }
    void setGlobalHotkeyCaptureActive(true).catch((error) => {
      console.debug("Could not suspend global hotkey while recording post-processing shortcut.", error);
    });
    const focusFrame = window.requestAnimationFrame(() => {
      postProcessingHotkeyCaptureRef.current?.focus();
    });
    const handleWindowKeyDown = (event: KeyboardEvent) => {
      handlePostProcessingHotkeyRecord(event);
    };
    window.addEventListener("keydown", handleWindowKeyDown, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleWindowKeyDown, true);
      void setGlobalHotkeyCaptureActive(false).catch((error) => {
        console.debug("Could not resume global hotkey after recording post-processing shortcut.", error);
      });
    };
  }, [handlePostProcessingHotkeyRecord, isRecordingPostProcessingHotkey]);

  useEffect(() => {
    if (!isRecordingMeetingHotkey) return;
    void setGlobalHotkeyCaptureActive(true).catch((error) => {
      console.debug("Could not suspend global hotkeys while recording meeting shortcut.", error);
    });
    const focusFrame = window.requestAnimationFrame(() => meetingHotkeyCaptureRef.current?.focus());
    const handleWindowKeyDown = (event: KeyboardEvent) => handleMeetingHotkeyRecord(event);
    window.addEventListener("keydown", handleWindowKeyDown, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleWindowKeyDown, true);
      void setGlobalHotkeyCaptureActive(false).catch((error) => {
        console.debug("Could not resume global hotkeys after recording meeting shortcut.", error);
      });
    };
  }, [handleMeetingHotkeyRecord, isRecordingMeetingHotkey]);

  const handleMicDeviceChange = async (deviceId: string) => {
    const previousDeviceId = selectedDeviceId;
    setSelectedDeviceId(deviceId);
    try {
      await updateSettings({ micDevice: deviceId });
      return true;
    } catch (e: any) {
      setSelectedDeviceId(previousDeviceId);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
      return false;
    }
  };

  const handleMicDeviceSelectFromDropdown = async (deviceId: string) => {
    const saved = await handleMicDeviceChange(deviceId);
    if (!saved) {
      return;
    }
    window.setTimeout(() => {
      setIsMicDropdownOpen(false);
    }, 500);
  };

  const handleSetFavoriteMic = async (deviceId: string) => {
    // Toggle favorite - if already favorite, clear it
    const originalFavorite = favoriteMic;  // Capture before optimistic update
    const newFavorite = favoriteMic === deviceId ? "" : deviceId;
    setFavoriteMic(newFavorite);
    try {
      await updateSettings({ favoriteMic: newFavorite });
      toast({
        title: newFavorite ? t("Favorite set") : t("Favorite cleared"),
        description: newFavorite
          ? t("This microphone will be used automatically when available.")
          : t("No preferred microphone set."),
        duration: 2000,
      });
    } catch (e: any) {
      setFavoriteMic(originalFavorite); // Revert to original value on error
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelChange = async (value: string) => {
    const requirement = requiredCredentialForTranscriptionModel(value);
    if (!isCredentialReady(requirement)) {
      openCredentialDialog(requirement);
      return;
    }
    const previousValue = transcriptionModel;
    setTranscriptionModel(value);
    try {
      if (value === "soniox-async") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "async" });
      } else if (value === "soniox-realtime") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "realtime" });
      } else if (value === "modulate-async") {
        await updateSettings({ defaultSttService: "modulate_async" });
      } else if (value === "modulate-realtime") {
        await updateSettings({ defaultSttService: "modulate" });
      } else if (value === "gemini-stt") {
        await updateSettings({ defaultSttService: "gemini_stt" });
      } else if (value === "mistral-async") {
        await updateSettings({ defaultSttService: "mistral_async" });
      } else if (value === "mistral-realtime") {
        await updateSettings({ defaultSttService: "mistral" });
      } else if (value === "smallest-async") {
        await updateSettings({ defaultSttService: "smallest_async" });
      } else if (value === "smallest-realtime") {
        await updateSettings({ defaultSttService: "smallest" });
      } else if (value === "assemblyai-realtime") {
        await updateSettings({ defaultSttService: "assemblyai_realtime" });
      } else if (value === "deepgram-async") {
        await updateSettings({ defaultSttService: "deepgram_async" });
      } else if (value === "gladia-async") {
        await updateSettings({ defaultSttService: "gladia_async" });
      } else if (value === "openai-async") {
        await updateSettings({ defaultSttService: "openai_async" });
      } else if (value === "speechmatics-async") {
        await updateSettings({ defaultSttService: "speechmatics_async" });
      } else {
        await updateSettings({ defaultSttService: value });
      }
    } catch (e: any) {
      setTranscriptionModel(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleLanguageChange = async (value: string) => {
    const previousValue = language;
    setLanguage(value);
    try {
      await updateSettings({ language: value });
    } catch (e: any) {
      setLanguage(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelSelectFromDropdown = async (value: string) => {
    await handleTranscriptionModelChange(value);
    window.setTimeout(() => {
      setIsTranscriptionModelDropdownOpen(false);
    }, 500);
  };

  const handleLanguageSelectFromDropdown = async (value: string) => {
    await handleLanguageChange(value);
    window.setTimeout(() => {
      setIsLanguageDropdownOpen(false);
    }, 500);
  };

  const handleOnnxModelChange = async (value: string) => {
    const previousModel = onnxModel;
    const previousQuantization = onnxQuantization;
    setOnnxModel(value);
    try {
      const selected = onnxModels.find((m) => m.id === value);
      const supported = selected?.supportedQuantizations || ["int8", "fp32"];
      if (!supported.includes(onnxQuantization)) {
        const nextQuant = supported[0];
        setOnnxQuantization(nextQuant);
        await updateSettings({ onnxModel: value, onnxQuantization: nextQuant });
      } else {
        await updateSettings({ onnxModel: value });
      }
      toast({
        title: t("Saved"),
        description: t("Local model selection updated."),
        duration: 2000,
      });
    } catch (e: any) {
      setOnnxModel(previousModel);
      setOnnxQuantization(previousQuantization);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleOnnxQuantizationChange = async (value: string) => {
    const previousValue = onnxQuantization;
    setOnnxQuantization(value);
    try {
      await updateSettings({ onnxQuantization: value });
      await loadOnnxModels();
      toast({
        title: t("Saved"),
        description: t("Quantization updated."),
        duration: 2000,
      });
    } catch (e: any) {
      setOnnxQuantization(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleOnnxDownload = async (modelId: string) => {
    if (!modelId || onnxModelActionInFlightRef.current.has(modelId)) return;
    onnxModelActionInFlightRef.current.add(modelId);
    setOnnxModels((prev) =>
      prev.map((m) =>
        m.id === modelId
          ? { ...m, status: "downloading", progress: 0, message: "Starting download..." }
          : m
      )
    );
    try {
      const res = await fetchWithTimeout(apiUrl("/api/onnx/download"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelId, quantization: onnxQuantization }),
        credentials: "include",
      }, 2 * 60 * 60_000);
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Download failed");
      }
      toast({
        title: t("Download finished"),
        description: t("Model downloaded successfully."),
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: t("Download failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    } finally {
      onnxModelActionInFlightRef.current.delete(modelId);
      await loadOnnxModels();
    }
  };

  const handleOnnxDelete = async (modelId: string) => {
    if (!modelId || onnxModelActionInFlightRef.current.has(modelId)) return;
    onnxModelActionInFlightRef.current.add(modelId);
    try {
      const res = await fetchWithTimeout(
        apiUrl(`/api/onnx/models/${encodeURIComponent(modelId)}?quantization=${encodeURIComponent(onnxQuantization)}`),
        {
          method: "DELETE",
          credentials: "include",
        },
        30_000,
      );
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Delete failed");
      }
      toast({
        title: t("Deleted"),
        description: t("Model removed from cache."),
        duration: 2000,
      });
      await loadOnnxModels();
    } catch (e: any) {
      toast({
        title: t("Delete failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    } finally {
      onnxModelActionInFlightRef.current.delete(modelId);
    }
  };

  const handleSummarizationModelChange = async (value: string) => {
    const requirement = requiredCredentialForLanguageModel(value);
    if (!isCredentialReady(requirement)) {
      openCredentialDialog(requirement);
      return;
    }
    const previousValue = summarizationModel;
    setSummarizationModel(value);
    try {
      await updateSettings({ summarizationModel: value });
      toast({
        title: t("Saved"),
        description: t("Summarization model updated."),
        duration: 2000,
      });
    } catch (e: any) {
      setSummarizationModel(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handlePostProcessingModelChange = async (value: string) => {
    const requirement = requiredCredentialForLanguageModel(value);
    if (!isCredentialReady(requirement)) {
      openCredentialDialog(requirement);
      return;
    }
    const previousValue = postProcessingModel;
    setPostProcessingModel(value);
    try {
      await updateSettings({ postProcessingModel: value });
      toast({
        title: t("Saved"),
        description: t("Live post-processing model updated."),
        duration: 2000,
      });
    } catch (e: any) {
      setPostProcessingModel(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleAutoSummarizeChange = async (enabled: boolean) => {
    const previousValue = autoSummarize;
    setAutoSummarize(enabled);
    try {
      await updateSettings({ autoSummarize: enabled });
      toast({
        title: t("Saved"),
        description: enabled ? t("Auto-summarize enabled.") : t("Auto-summarize disabled."),
        duration: 2000,
      });
    } catch (e: any) {
      setAutoSummarize(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleYoutubePreferCaptionsChange = async (enabled: boolean) => {
    const previousValue = youtubePreferCaptions;
    setYoutubePreferCaptions(enabled);
    try {
      await updateSettings({ youtubePreferCaptions: enabled });
      toast({
        title: t("Saved"),
        description: enabled
          ? t("YouTube captions will be used before audio transcription.")
          : t("YouTube videos will always be transcribed from audio."),
        duration: 2000,
      });
    } catch (e: any) {
      setYoutubePreferCaptions(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleVoiceprintOptInChange = async (enabled: boolean) => {
    const previousValue = voiceprintLibraryOptIn;
    setVoiceprintLibraryOptIn(enabled);
    try {
      await updateSettings({ voiceprintLibraryOptIn: enabled });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-model"] }),
        queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] }),
      ]);
      toast({
        title: t("Saved"),
        description: enabled
          ? t("Voice Library will recognize recurring speakers in new meetings after its local model is installed.")
          : t("Scriber will not learn new voices. Existing saved speakers remain until you delete them."),
        duration: 3500,
      });
    } catch (e: any) {
      setVoiceprintLibraryOptIn(previousValue);
      toast({ title: t("Save failed"), description: localizedSettingsError(e, "The requested settings action failed.", locale, t), duration: 4000 });
    }
  };

  const handleVoiceEnrollmentOpenChange = (open: boolean) => {
    if (!open && voiceEnrollmentMutation.isPending) return;
    setVoiceEnrollmentOpen(open);
    if (open) {
      setVoiceEnrollmentName("");
      setVoiceEnrollmentDevice(DEFAULT_VOICE_ENROLLMENT_DEVICE);
      setVoiceEnrollmentStartedAt(null);
      setVoiceEnrollmentProgress(0);
      setVoiceEnrollmentStage("idle");
      setVoiceEnrollmentResult(null);
      voiceEnrollmentMutation.reset();
    }
  };

  const handleDeleteVoiceprintLibrary = async () => {
    if (voiceLibraryDeletePending) return;
    setVoiceLibraryDeletePending(true);
    try {
      const response = await fetchWithTimeout(apiUrl("/api/meetings/speaker-library"), {
        method: "DELETE",
        credentials: "include",
      }, 15_000);
      if (!response.ok) throw new Error(`Delete failed (${response.status})`);
      setVoiceprintLibraryOptIn(false);
      setVoiceLibraryDeleteOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-model"] });
      toast({ title: t("Voice data deleted"), description: t("All saved speakers and the local download were removed."), duration: 3500 });
    } catch (e: any) {
      toast({ title: t("Delete failed"), description: localizedSettingsError(e, "The requested settings action failed.", locale, t), duration: 5000 });
    } finally {
      setVoiceLibraryDeletePending(false);
    }
  };

  const handlePostProcessingEnabledChange = async (enabled: boolean) => {
    const previousValue = postProcessingEnabled;
    setPostProcessingEnabled(enabled);
    try {
      await updateSettings({ postProcessingEnabled: enabled });
      toast({
        title: t("Saved"),
        description: enabled ? t("Live post-processing enabled.") : t("Live post-processing disabled."),
        duration: 2000,
      });
    } catch (e: any) {
      setPostProcessingEnabled(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
      return;
    }
    try {
      await refreshGlobalHotkey();
    } catch (e: any) {
      toast({
        title: t("Saved, hotkey refresh failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 5000,
      });
    }
  };

  const handleSaveHotkey = async () => {
    try {
      const updated = await updateSettings({ hotkey });
      setHotkey(updated.hotkey || hotkey);
      toast({
        title: t("Saved"),
        description: t("Hotkey updated."),
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
      setIsRecordingHotkey(false);
      return;
    }
    try {
      await setGlobalHotkeyCaptureActive(false);
      await refreshGlobalHotkey();
    } catch (e: any) {
      toast({
        title: t("Saved, hotkey refresh failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 5000,
      });
    } finally {
      setIsRecordingHotkey(false);
    }
  };

  const handleSavePostProcessingHotkey = async () => {
    try {
      const updated = await updateSettings({ postProcessingHotkey });
      setPostProcessingHotkey(updated.postProcessingHotkey || postProcessingHotkey);
      toast({
        title: t("Saved"),
        description: t("Post-processing hotkey updated."),
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
      setIsRecordingPostProcessingHotkey(false);
      return;
    }
    try {
      await setGlobalHotkeyCaptureActive(false);
      await refreshGlobalHotkey();
    } catch (e: any) {
      toast({
        title: t("Saved, hotkey refresh failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 5000,
      });
    } finally {
      setIsRecordingPostProcessingHotkey(false);
    }
  };

  const handleSaveMeetingHotkey = async () => {
    try {
      const updated = await updateSettings({ meetingHotkey });
      setMeetingHotkey(updated.meetingHotkey || meetingHotkey);
      await setGlobalHotkeyCaptureActive(false);
      const status = await refreshGlobalHotkey();
      if (status?.meetingHotkey) setMeetingHotkey(status.meetingHotkey);
      toast({ title: t("Saved"), description: t("Meeting hotkey updated."), duration: 2000 });
    } catch (e: any) {
      toast({ title: t("Meeting hotkey save failed"), description: localizedSettingsError(e, "The requested settings action failed.", locale, t), duration: 5000 });
    } finally {
      setIsRecordingMeetingHotkey(false);
    }
  };

  const updateMeetingPreferences = async (patch: SettingsUpdatePayload) => {
    try {
      const updated = await updateSettings(patch);
      setMeetingTranscriptionMode(updated.meetingTranscriptionMode === "final_only" ? "final_only" : "live_final");
      setMeetingFinalProvider(updated.meetingFinalProvider || meetingFinalProvider);
      setMeetingAnalysisModel(updated.meetingAnalysisModel || meetingAnalysisModel);
      setMeetingSmartTurnEnabled(updated.meetingSmartTurnEnabled !== false);
      setMeetingAutoAnalyze(updated.meetingAutoAnalyze !== false);
      setMeetingAecEnabled(updated.meetingAecEnabled !== false);
      setMeetingAudioRetentionDays(updated.meetingAudioRetentionDays ?? meetingAudioRetentionDays);
      setSpeakerDiarizationFallbackEnabled(updated.speakerDiarizationFallbackEnabled !== false);
      await queryClient.invalidateQueries({ queryKey: ["/api/meeting-profiles"] });
      toast({ title: t("Meeting settings saved"), description: t("New meetings will use these choices."), duration: 2200 });
    } catch (error: any) {
      toast({ title: t("Meeting settings could not be saved"), description: localizedSettingsError(error, "The requested settings action failed.", locale, t), duration: 5000 });
      throw error;
    }
  };

  const refreshDiarizationComponent = useCallback(async () => {
    const response = await fetchWithTimeout(
      apiUrl("/api/meetings/diarization-component"),
      { credentials: "include" },
      15_000,
    );
    if (!response.ok) throw new Error(`Component status failed (${response.status})`);
    setDiarizationComponent(await response.json() as DiarizationComponentStatus);
  }, []);

  useEffect(() => {
    void refreshDiarizationComponent().catch(() => setDiarizationComponent(null));
  }, [refreshDiarizationComponent]);

  const installDiarizationComponent = async () => {
    setDiarizationComponentPending(true);
    try {
      const response = await fetchWithTimeout(
        apiUrl("/api/meetings/diarization-component"),
        { method: "POST", credentials: "include" },
        600_000,
      );
      const payload = await response.json() as DiarizationComponentStatus & { message?: string };
      if (!response.ok) throw new Error(payload.message || `Install failed (${response.status})`);
      setDiarizationComponent(payload);
      toast({
        title: t("Local speaker separation installed"),
        description: t("Local speaker separation is ready to use."),
        duration: 3500,
      });
    } catch (error: any) {
      toast({
        title: t("Local speaker separation could not be installed"),
        description: localizedSettingsError(error, "The requested settings action failed.", locale, t),
        duration: 6000,
      });
    } finally {
      setDiarizationComponentPending(false);
    }
  };

  const handleRecordingModeChange = async (mode: string) => {
    const previousMode = recordingMode;
    setRecordingMode(mode);
    try {
      await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });
    } catch (e: any) {
      setRecordingMode(previousMode);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
      return;
    }
    try {
      await refreshGlobalHotkey();
    } catch (e: any) {
      toast({
        title: t("Saved, hotkey refresh failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 5000,
      });
    }
  };

  const handleCustomVocabBlur = async () => {
    await saveCustomVocabulary(customVocabulary);
  };

  const handleSummarizationPromptBlur = async () => {
    try {
      await updateSettings({ summarizationPrompt });
      toast({
        title: t("Saved"),
        description: t("Summarization prompt updated."),
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleVisualizerBarCountChange = (value: number[]) => {
    const count = normalizeVisualizerBarCount(value[0], savedVisualizerBarCount);
    setVisualizerBarCount(count);
  };

  const handleVisualizerBarCountCommit = async (value: number[]) => {
    const count = normalizeVisualizerBarCount(value[0], savedVisualizerBarCount);
    if (count === savedVisualizerBarCount) {
      return;
    }
    try {
      await updateSettings({ visualizerBarCount: count });
      setSavedVisualizerBarCount(count);
    } catch (e: any) {
      setVisualizerBarCount(savedVisualizerBarCount);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleAutostartChange = async (enabled: boolean) => {
    setAutostartEnabled(enabled);
    try {
      const autostart = await setDesktopAutostartEnabled(enabled);
      setAutostartEnabled(autostart.enabled);
      setAutostartAvailable(autostart.available);

      toast({
        title: t("Saved"),
        description: enabled ? t("Autostart enabled") : t("Autostart disabled"),
        duration: 2000,
      });
    } catch (e: any) {
      // Revert on error
      setAutostartEnabled(!enabled);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleCheckDesktopUpdate = async () => {
    setIsCheckingDesktopUpdate(true);
    setDesktopUpdateProgress(null);
    try {
      const status = await checkDesktopUpdate();
      setDesktopUpdate(status);
      toast({
        title: status.available ? t("Update available") : t("Update check finished"),
        description: t(status.message, status.messageValues),
        duration: 3500,
      });
    } catch (e: any) {
      const message = localizedSettingsError(e, "The requested settings action failed.", locale, t);
      setDesktopUpdate((prev) => ({
        ...prev,
        phase: "error",
        enabled: false,
        available: false,
        message: "Update check failed.",
      }));
      toast({
        title: t("Update check failed"),
        description: message,
        duration: 5000,
      });
    } finally {
      setIsCheckingDesktopUpdate(false);
    }
  };

  const handlePostProcessingPromptBlur = async () => {
    try {
      await updateSettings({ postProcessingPrompt });
      toast({
        title: t("Saved"),
        description: t("Live post-processing prompt updated."),
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleResetPostProcessingPrompt = async () => {
    const previousPrompt = postProcessingPrompt;
    setPostProcessingPrompt(DEFAULT_POST_PROCESSING_PROMPT);
    try {
      await updateSettings({ postProcessingPrompt: DEFAULT_POST_PROCESSING_PROMPT });
      toast({
        title: t("Saved"),
        description: t("Live post-processing prompt reset."),
        duration: 2000,
      });
    } catch (e: any) {
      setPostProcessingPrompt(previousPrompt);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleInstallDesktopUpdate = async () => {
    setIsInstallingDesktopUpdate(true);
    try {
      const status = await installDesktopUpdate((progress) => {
        setDesktopUpdateProgress(progress);
      });
      setDesktopUpdate(status);
    } catch (e: any) {
      const message = localizedSettingsError(e, "The requested settings action failed.", locale, t);
      setDesktopUpdate((prev) => ({
        ...prev,
        phase: "error",
        message: "Update installation failed.",
      }));
      toast({
        title: t("Update failed"),
        description: message,
        duration: 5000,
      });
    } finally {
      setIsInstallingDesktopUpdate(false);
    }
  };

  const handleDesktopAutoCheckChange = (enabled: boolean) => {
    const status = updateDesktopUpdateSettings({ autoCheckEnabled: enabled });
    setDesktopUpdate(status);
    toast({
      title: t("Saved"),
      description: enabled ? t("Weekly update checks enabled.") : t("Automatic update checks disabled."),
      duration: 2500,
    });
  };

  const handleRemindDesktopUpdateLater = () => {
    const status = remindDesktopUpdateLater(desktopUpdate.version);
    setDesktopUpdate(status);
    toast({
      title: t("Reminder set"),
      description: t("Scriber will remind you about this update tomorrow."),
      duration: 3000,
    });
  };

  const handleSkipDesktopUpdateVersion = () => {
    const status = skipDesktopUpdateVersion(desktopUpdate.version);
    setDesktopUpdate(status);
    toast({
      title: t("Update skipped"),
      description: t("This version will not be announced again."),
      duration: 3000,
    });
  };

  const handleOpenDesktopUpdateReleaseNotes = () => {
    void openDesktopUpdateReleaseNotes();
  };

  const handleMicAlwaysOnChange = async (enabled: boolean) => {
    setMicAlwaysOn(enabled);
    try {
      await updateSettings({ micAlwaysOn: enabled });
      toast({
        title: t("Saved"),
        description: enabled ? t("Mic pre-warming enabled") : t("Mic pre-warming disabled"),
        duration: 2000,
      });
    } catch (e: any) {
      setMicAlwaysOn(!enabled);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(e, "The requested settings action failed.", locale, t),
        duration: 4000,
      });
    }
  };

  const handleSegmentSpeechWithVadChange = async (enabled: boolean) => {
    if (segmentSpeechWithVadSaving) return;
    const previousValue = segmentSpeechWithVad;
    setSegmentSpeechWithVadSaving(true);
    try {
      const updated = await updateSettings({ segmentSpeechWithVad: enabled });
      const savedValue = updated.segmentSpeechWithVad === true;
      setSegmentSpeechWithVad(savedValue);
      toast({
        title: t("Saved"),
        description: savedValue ? t("Silero voice detection enabled.") : t("Silero voice detection disabled."),
        duration: 2000,
      });
    } catch (error) {
      setSegmentSpeechWithVad(previousValue);
      toast({
        title: t("Save failed"),
        description: localizedSettingsError(error, "Silero voice detection could not be saved.", locale, t),
        duration: 4000,
      });
    } finally {
      setSegmentSpeechWithVadSaving(false);
    }
  };

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (!msg) return;
    if (msg.type === "microphones_updated") {
      const devices = msg.devices || [];
      setInputDevices(devices);

      const availableIds = new Set(devices.map((d) => d.deviceId));
      if (selectedDeviceId !== "default" && !availableIds.has(selectedDeviceId)) {
        setSelectedDeviceId("default");
        toast({
          title: t("Microphone disconnected"),
          description: t("The selected microphone is no longer available. Scriber switched back to the Windows default."),
          duration: 3000,
        });
      }

      if (msg.favoriteMicRestored && typeof msg.restoredDeviceId === "string" && msg.restoredDeviceId) {
        setSelectedDeviceId(msg.restoredDeviceId);
        setFavoriteMic(msg.restoredDeviceId);
        const restoredLabel =
          typeof msg.restoredDeviceLabel === "string" && msg.restoredDeviceLabel
            ? msg.restoredDeviceLabel
            : msg.restoredDeviceId;
        toast({
          title: t("Favorite mic restored"),
          description: t("Favorite microphone '{{name}}' is available again.", { name: restoredLabel }),
          duration: 2500,
        });
      }
      return;
    }
    if (msg.type === "onnx_download_progress") {
      if (msg.quantization && msg.quantization !== onnxQuantization) {
        return;
      }
      setOnnxModels((prev) =>
        prev.map((m) =>
          m.id === msg.modelId
            ? {
              ...m,
              status: msg.status,
              progress: typeof msg.progress === "number" ? msg.progress : m.progress,
              message: msg.message || m.message,
              downloaded: msg.status === "ready" ? true : m.downloaded,
            }
            : m
        )
      );
    }
    if (msg.type === "onnx_models_updated") {
      loadOnnxModels();
    }
    if (msg.type === "meeting_state" && ["ready", "capture_failed", "finalization_failed", "analysis_failed", "discarded"].includes(msg.meeting.state)) {
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
    }
  }, [loadOnnxModels, onnxQuantization, queryClient, selectedDeviceId, t, toast]);

  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    const onWindowFocus = () => {
      void refreshMicrophones();
    };
    window.addEventListener("focus", onWindowFocus);
    return () => {
      window.removeEventListener("focus", onWindowFocus);
    };
  }, [refreshMicrophones]);

  const selectedOnnxModel = onnxModels.find((m) => m.id === onnxModel) || onnxModels[0];
  const selectedMicIndex = inputDevices.findIndex(
    (device, index) => (device.deviceId || `device-${index}`) === selectedDeviceId
  );
  const selectedMicDevice = selectedMicIndex >= 0 ? inputDevices[selectedMicIndex] : undefined;
  const selectedMicLabel = inputDevices.length === 0
    ? t("Loading devices...")
    : selectedMicDevice
      ? ((selectedMicDevice.deviceId || `device-${selectedMicIndex}`) === "default"
        ? t("Default microphone")
        : (selectedMicDevice.label || t("Device {{number}}", { number: formatNumber(selectedMicIndex + 1) })))
      : (selectedDeviceId === "default" ? t("Default microphone") : "");
  const savedSpeakerCount = speakerProfilesQuery.data?.items.length ?? 0;
  const hasSelectedMic = Boolean(selectedMicDevice || selectedDeviceId === "default");
  const selectedLanguage = LANGUAGE_OPTIONS.find((option) => option.value === language) || LANGUAGE_OPTIONS[0];
  const selectedTranscriptionModelOption = TRANSCRIPTION_MODEL_OPTIONS.find((option) => option.value === transcriptionModel);
  const supportedQuantizations = selectedOnnxModel?.supportedQuantizations || ["int8", "fp32"];
  const quantizationSupported = supportedQuantizations.includes(onnxQuantization);
  const formatSize = (sizeMb?: number) => {
    if (!sizeMb) return "";
    if (sizeMb >= 1024) return t("{{size}} GB", { size: formatNumber(sizeMb / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) });
    return t("{{size}} MB", { size: formatNumber(sizeMb) });
  };
  const formatOnnxRuntime = (runtime?: string) => {
    if (!runtime || runtime === "onnx_asr") return t("ONNX Runtime");
    return runtime.replace(/_/g, " ");
  };
  const selectedOnnxSize =
    selectedOnnxModel?.sizeMbByQuantization?.[onnxQuantization] ?? selectedOnnxModel?.sizeMb;
  const selectedOnnxRepo =
    selectedOnnxModel?.hfRepoByQuantization?.[onnxQuantization] || selectedOnnxModel?.hfRepo || "";
  const getStatusLabel = (status?: string) => {
    if (status === "ready") return t("Downloaded");
    if (status === "downloading") return t("Downloading");
    if (status === "error") return t("Error");
    return t("Not downloaded");
  };
  const getStatusVariant = (status?: string): "default" | "secondary" | "destructive" | "outline" => {
    if (status === "ready") return "default";
    if (status === "downloading") return "secondary";
    if (status === "error") return "destructive";
    return "outline";
  };
  const desktopUpdateBadgeVariant: "default" | "secondary" | "destructive" | "outline" = (() => {
    if (desktopUpdate.phase === "error") return "destructive";
    if (desktopUpdate.dismissed) return "outline";
    if (desktopUpdate.deferred) return "secondary";
    if (desktopUpdate.available) return "default";
    if (desktopUpdate.enabled) return "secondary";
    return "outline";
  })();
  const desktopUpdateBadgeLabel = (() => {
    if (desktopUpdate.available && desktopUpdate.dismissed) return t("Skipped");
    if (desktopUpdate.available && desktopUpdate.deferred) return t("Later");
    if (desktopUpdate.available) return t("Available");
    if (desktopUpdate.phase === "idle") return t("Not checked");
    if (desktopUpdate.enabled) return t("Current");
    return t("Not configured");
  })();
  const desktopUpdateLastCheckedLabel = formatUpdateTimestamp(desktopUpdate.lastCheckedAt, formatDate, t);
  const desktopUpdateNextCheckLabel = !desktopUpdate.autoCheckEnabled
    ? t("Automatic checks disabled")
    : desktopUpdate.lastCheckedAt && desktopUpdate.nextCheckAt
      ? formatUpdateTimestamp(desktopUpdate.nextCheckAt, formatDate, t)
      : t("When Scriber starts");
  const desktopUpdateAvailableVersionLabel = (() => {
    if (desktopUpdate.version) return desktopUpdate.version;
    if (desktopUpdate.phase === "idle") return t("Not checked");
    if (desktopUpdate.phase === "error") return t("Check failed");
    if (!desktopUpdate.enabled) return t("Not configured");
    return t("No newer version");
  })();

  const providerGroups = [
    {
      key: "cloud_streaming",
      label: t("Cloud streaming"),
      description: t("True realtime STT streams."),
      items: sortProviderOptionsByErrorRate(providerModelOptions.filter((option) => option.group === "cloud_streaming"), localeTag),
    },
    {
      key: "cloud_async",
      label: t("Cloud async / batch"),
      description: t("Finalizes captured audio after upload or recording stop."),
      items: sortProviderOptionsByErrorRate(providerModelOptions.filter((option) => option.group === "cloud_async"), localeTag),
    },
    {
      key: "local",
      label: t("Local"),
      description: t("Runs on this device."),
      items: providerModelOptions.filter((option) => option.group === "local"),
    },
  ];
  const summaryModelGroups = [
    {
      key: "gemini",
      label: "Gemini",
      items: summarizationModelOptions.filter((option) => option.group === "gemini"),
    },
    {
      key: "cerebras",
      label: "Cerebras",
      items: summarizationModelOptions.filter((option) => option.group === "cerebras"),
    },
    {
      key: "openrouter",
      label: "OpenRouter",
      items: summarizationModelOptions.filter((option) => option.group === "openrouter"),
    },
    {
      key: "openai",
      label: "OpenAI",
      items: summarizationModelOptions.filter((option) => option.group === "openai"),
    },
  ];
  const compactTranscriptionModelLabel = transcriptionModel === "onnx_local"
    ? t("Local ONNX")
    : selectedTranscriptionModelOption
      ? t(selectedTranscriptionModelOption.label.replace(" - No API Key", ""))
      : transcriptionModel || t("Select provider");

  const customVocabularySettings = (
    <FieldShell
      label={t("Custom vocabulary")}
      detail={t("Names, brands, and domain terms passed to supported STT providers.")}
    >
      <Textarea
        value={customVocabulary}
        onChange={(event) => setCustomVocabulary(event.target.value)}
        onBlur={handleCustomVocabBlur}
        placeholder={t("Enter terms, one per line...")}
        className="min-h-[54px] resize-none bg-white/70 font-mono text-[12px] leading-5 dark:bg-[var(--live-well)]"
      />
    </FieldShell>
  );

  const livePostProcessingSettings = (
    <SettingsSubsection
      title={t("Live post-processing")}
      description={t("A separate live-mic shortcut cleans dictation before paste. Files and YouTube stay unchanged.")}
      icon={Sparkles}
      action={<Switch checked={postProcessingEnabled} onCheckedChange={handlePostProcessingEnabledChange} />}
    >
      <div className="grid gap-3">
        <SettingLine label={t("Post-processing hotkey")} description={t("Starts Live Mic with cleanup enabled.")}>
          <Dialog open={isRecordingPostProcessingHotkey} onOpenChange={setIsRecordingPostProcessingHotkey}>
            <DialogTrigger asChild>
              <Button variant="outline" className="h-8 w-[220px] max-w-full justify-start font-mono text-[11px]" disabled={!postProcessingEnabled}>
                <Keyboard className="mr-2 h-4 w-4 text-muted-foreground" />
                {postProcessingHotkey}
              </Button>
            </DialogTrigger>
            <DialogContent className="sm:max-w-[425px]">
              <DialogHeader>
                <DialogTitle>{t("Post-processing hotkey")}</DialogTitle>
                <DialogDescription>{t("Press the key combination for cleaned live dictation.")}</DialogDescription>
              </DialogHeader>
              <div
                ref={postProcessingHotkeyCaptureRef}
                className="flex h-32 items-center justify-center rounded-lg border-2 border-dashed bg-secondary/20 outline-none transition-colors focus:border-primary focus:bg-primary/5"
                tabIndex={0}
                aria-label={t("Post-processing hotkey capture area")}
              >
                <p className="text-lg font-medium text-primary">{postProcessingHotkey}</p>
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setIsRecordingPostProcessingHotkey(false)}>{t("Cancel")}</Button>
                <Button onClick={handleSavePostProcessingHotkey}>{t("Save")}</Button>
              </div>
            </DialogContent>
          </Dialog>
        </SettingLine>

        <FieldShell
          label={t("Post-processing model")}
          detail={t("Use a low-cost, low-latency model for simple dictation cleanup.")}
        >
          <Select value={postProcessingModel} onValueChange={(value) => void handlePostProcessingModelChange(value)}>
            <SelectTrigger className="h-10 bg-white/70 dark:bg-[var(--live-well)]">
              {selectedPostProcessingModelOption ? (
                <div className="flex min-w-0 items-center gap-2 text-left">
                  <ProviderIcon
                    icon={selectedPostProcessingModelOption.icon}
                    label={selectedPostProcessingModelOption.label}
                    className="h-5 w-5 rounded"
                  />
                  <span className="min-w-0 truncate text-[12px] font-semibold">
                    {selectedPostProcessingModelOption.label}
                  </span>
                </div>
              ) : (
                <SelectValue placeholder={t("Select cleanup model")} />
              )}
            </SelectTrigger>
            <SelectContent className="min-w-[320px]">
              {postProcessingModelOptions.map((option) => {
                const requirement = requiredCredentialForLanguageModel(option.value);
                const disabledReason = missingCredentialReason(requirement);
                return (
                  <SelectItem key={option.value} value={option.value}>
                    <span className="flex min-w-0 items-center gap-2 py-0.5">
                      <ProviderIcon icon={option.icon} label={option.label} className="h-5 w-5 rounded" />
                      <span className="min-w-0">
                        <span className="block truncate text-[12px] font-semibold leading-4">{option.label}</span>
                        <span className="block truncate text-[10.5px] leading-3 text-slate-500 dark:text-slate-400">
                          {option.detail}
                        </span>
                        {disabledReason ? (
                          <span className="mt-0.5 inline-flex w-fit rounded-full bg-amber-100 px-1.5 py-0.5 text-[10.5px] font-semibold leading-3 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300">
                            {disabledReason}
                          </span>
                        ) : null}
                      </span>
                    </span>
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
          {missingPostProcessingCredentialRequirement ? (
            <button
              type="button"
              onClick={() => openCredentialDialog(missingPostProcessingCredentialRequirement)}
              className="inline-flex w-fit rounded-full bg-amber-100 px-2 py-1 text-[10.5px] font-semibold leading-4 text-amber-700 transition-colors hover:bg-amber-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 dark:bg-amber-950/50 dark:text-amber-300 dark:hover:bg-amber-900/70"
            >
              {t(MISSING_CREDENTIAL_CTA)}
            </button>
          ) : null}
        </FieldShell>

        <FieldShell label={t("Live cleanup prompt")}>
          <Textarea
            value={postProcessingPrompt}
            onFocus={(event) => expandPromptTextarea(event.currentTarget, 560)}
            onChange={(event) => {
              setPostProcessingPrompt(event.target.value);
              expandPromptTextarea(event.currentTarget, 560);
            }}
            onBlur={(event) => {
              event.currentTarget.style.height = "";
              void handlePostProcessingPromptBlur();
            }}
            placeholder={DEFAULT_POST_PROCESSING_PROMPT}
            className="min-h-[64px] resize-none overflow-hidden break-words bg-white/70 text-sm transition-[height,box-shadow,transform,border-color] duration-300 ease-out focus:-translate-y-0.5 focus:border-blue-300 focus:shadow-[0_18px_45px_-30px_rgba(37,99,235,0.75)] motion-reduce:transform-none motion-reduce:transition-none dark:bg-[var(--live-well)] dark:focus:border-blue-700"
            disabled={!postProcessingEnabled}
          />
          <div className="mt-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
            <p className="min-w-[220px] flex-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
              {t("Use")} <span className="font-mono">${"{output}"}</span> {t("where the raw transcript should be inserted.")}
            </p>
            <Button size="sm" variant="outline" className="shrink-0" onClick={handleResetPostProcessingPrompt} disabled={!postProcessingEnabled}>
              {t("Reset prompt")}
            </Button>
          </div>
        </FieldShell>
      </div>
    </SettingsSubsection>
  );

  const summarizationPromptSettings = (
    <FieldShell
      label={t("Summarization prompt")}
      detail={t("Controls content and emphasis. Scriber always applies a safe HTML structure for display.")}
    >
      <Textarea
        value={summarizationPrompt}
        onFocus={(event) => expandPromptTextarea(event.currentTarget, 420)}
        onChange={(event) => {
          setSummarizationPrompt(event.target.value);
          expandPromptTextarea(event.currentTarget, 420);
        }}
        onBlur={(event) => {
          event.currentTarget.style.height = "";
          void handleSummarizationPromptBlur();
        }}
        placeholder={t("Summarize the key points, decisions, and action items. Keep it concise and structured.")}
        className="min-h-[60px] resize-none overflow-hidden bg-white/70 text-sm transition-[height,box-shadow,transform,border-color] duration-300 ease-out focus:-translate-y-0.5 focus:border-blue-300 focus:shadow-[0_18px_45px_-30px_rgba(37,99,235,0.75)] motion-reduce:transform-none motion-reduce:transition-none dark:bg-[var(--live-well)] dark:focus:border-blue-700"
      />
    </FieldShell>
  );

  const onnxLocalModelSettings = (
    <div className="rounded-xl bg-white/70 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-well)]">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-bold text-slate-950 dark:text-slate-100">{t("ONNX model")}</p>
          <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">{t("Whisper / Parakeet local ONNX Runtime")}</p>
        </div>
        {selectedOnnxModel ? (
          <Badge variant={getStatusVariant(selectedOnnxModel.status)}>{getStatusLabel(selectedOnnxModel.status)}</Badge>
        ) : null}
      </div>

      {onnxAvailable === null ? (
        <p className="text-[12px] text-slate-500">{t("Loading local models...")}</p>
      ) : onnxAvailable === false ? (
        <p className="text-[12px] leading-4 text-slate-500">{t(onnxMessage || "onnx-asr is not installed.")}</p>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <FieldShell label={t("Model")}>
              <Select value={onnxModel} onValueChange={handleOnnxModelChange}>
                <SelectTrigger className="h-9">
                  {selectedOnnxModel ? (
                    <span className="min-w-0 truncate text-left text-[12px] font-semibold">
                      {selectedOnnxModel.name}
                    </span>
                  ) : (
                    <SelectValue placeholder={t("Select local model")} />
                  )}
                </SelectTrigger>
                <SelectContent className="min-w-[320px]">
                  {onnxModels.map((model) => (
                    <SelectItem key={model.id} value={model.id} className="py-2">
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate text-[12px] font-semibold leading-4">{model.name}</span>
                        <span className="truncate text-[10.5px] leading-3 text-slate-500 dark:text-slate-400">
                          {formatOnnxRuntime(model.runtime)} · {formatSize(model.sizeMbByQuantization?.[model.supportedQuantizations?.[0] || ""] ?? model.sizeMb)}
                        </span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldShell>
            <FieldShell label={t("Quantization")}>
              <Select value={onnxQuantization} onValueChange={handleOnnxQuantizationChange}>
                <SelectTrigger className="h-9">
                  <SelectValue placeholder={t("Select quantization")} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="int8" disabled={!supportedQuantizations.includes("int8")}>int8</SelectItem>
                  <SelectItem value="fp16" disabled={!supportedQuantizations.includes("fp16")}>fp16</SelectItem>
                  <SelectItem value="fp32" disabled={!supportedQuantizations.includes("fp32")}>fp32</SelectItem>
                </SelectContent>
              </Select>
            </FieldShell>
          </div>
          {selectedOnnxModel?.description ? (
            <div className="space-y-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
              <p>{t(selectedOnnxModel.description)}</p>
              <p>
                {formatOnnxRuntime(selectedOnnxModel.runtime)}
                {selectedOnnxSize ? ` · ${formatSize(selectedOnnxSize)}` : ""}
                {selectedOnnxRepo ? ` · ${selectedOnnxRepo}` : ""}
              </p>
            </div>
          ) : null}
          {selectedOnnxModel?.status === "downloading" && (
            <div className="space-y-1.5">
              <Progress value={selectedOnnxModel.progress || 0} />
              <p className="flex items-center gap-2 text-[11px] text-slate-500">
                <Loader2 className="h-3 w-3 animate-spin" />
                {localizeOnnxDownloadMessage(selectedOnnxModel.message, t, formatNumber)}
                <span className="ml-auto">{formatNumber(Math.round(selectedOnnxModel.progress || 0))}%</span>
              </p>
            </div>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => selectedOnnxModel && handleOnnxDownload(selectedOnnxModel.id)}
              disabled={!selectedOnnxModel || selectedOnnxModel.status === "downloading" || selectedOnnxModel.downloaded || !quantizationSupported}
            >
              <Download className="mr-2 h-4 w-4" />
              {t("Download")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => selectedOnnxModel && handleOnnxDelete(selectedOnnxModel.id)}
              disabled={!selectedOnnxModel?.downloaded || selectedOnnxModel.status === "downloading"}
            >
              <Trash2 className="mr-2 h-4 w-4" />
              {t("Delete")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );

  const activeLocalModelSettings = onnxLocalModelSettings;

  const speechToTextProviderPanel = (
    <SectionPanel
      id="settings-providers"
      title={t("Speech-to-text provider")}
      description={t("Choose the primary transcription provider.")}
      icon={Cloud}
    >
      <div className="mb-2.5 rounded-xl bg-slate-50 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
        <p className="text-[11px] font-semibold text-slate-500 dark:text-slate-400">{t("Current provider")}</p>
        <div className="mt-1 flex items-center gap-2">
          <ProviderIcon
            icon={providerModelOptions.find((option) => option.value === transcriptionModel)?.icon}
            label={compactTranscriptionModelLabel}
          />
          <p className="truncate text-[14px] font-semibold text-slate-950 dark:text-slate-100">
            {compactTranscriptionModelLabel}
          </p>
        </div>
        {transcriptionModel === "assemblyai" && (
          <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            {t("Async mode returns the transcript after recording stops.")}
          </p>
        )}
        {transcriptionModel === "assemblyai-realtime" && (
          <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            {t("Realtime mode uses AssemblyAI Universal-3.5 Pro through Pipecat.")}
          </p>
        )}
        {transcriptionModel === "modulate-realtime" && (
          <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            {t("Multilingual streaming shows finalized text only. Scriber does not request partial text or enrichment signals.")}
          </p>
        )}
        {transcriptionModel === "modulate-async" && (
          <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            {t("Multilingual batch returns one final transcript after recording stops. Scriber does not request enrichment signals.")}
          </p>
        )}
      </div>

      <div className="space-y-2.5">
        <div className="space-y-2.5">
          {providerGroups.map((group) => (
            <div
              key={group.key}
              role="radiogroup"
              aria-label={t("{{group}} transcription providers", { group: group.label })}
              className="rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]"
            >
              <div className="mb-1.5">
                <div className="min-w-0">
                  <h3 className="text-[13px] !font-bold leading-4 text-slate-950 dark:text-slate-100">
                    {group.label}
                  </h3>
                  <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
                    {group.description}
                  </p>
                </div>
              </div>
              <div className="grid gap-x-2 gap-y-1 sm:grid-cols-2">
                {group.items.map((option) => {
                  const requirement = requiredCredentialForTranscriptionModel(option.value);
                  const disabledReason = missingCredentialReason(requirement);
                  return (
                    <ProviderChoice
                      key={option.value}
                      option={option}
                      selected={transcriptionModel === option.value}
                      disabled={Boolean(disabledReason)}
                      disabledReason={disabledReason}
                      onCredentialAction={() => openCredentialDialog(requirement)}
                      onSelect={() => void handleTranscriptionModelChange(option.value)}
                    />
                  );
                })}
              </div>
              {group.key === "local" && activeLocalModelSettings ? (
                <div className="mt-2 border-t border-slate-200/80 pt-2 dark:border-[var(--workspace-border)]">
                  {activeLocalModelSettings}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </div>
    </SectionPanel>
  );

  return (
    <div data-page-shell="settings" className={cn(
      "app-page-shell settings-page px-4 py-5 text-[13px] transition-opacity duration-150 md:px-6 md:py-6",
      settingsLoaded ? "opacity-100" : "opacity-0",
    )}>
      {settingsError && (
        <QueryErrorState
          className="mb-4"
          title={t("Could not load settings")}
          description={settingsError}
          onRetry={() => window.location.reload()}
        />
      )}

      <PageIntro
        eyebrow={t("Workspace controls · 06")}
        title={t("Settings")}
        description={t("Configure capture, transcription providers, AI processing, credentials, updates, and language behavior.")}
        bottomContent={(
          <nav aria-label={t("Settings sections")} className="settings-section-nav overflow-x-auto">
            <div className="flex w-max items-center gap-1 rounded-xl bg-slate-100/80 p-1 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.05)] dark:bg-[var(--live-well)]">
              {[
                { section: "transcription", href: "#settings-transcription", label: "Transcription", icon: Mic },
                { section: "providers", href: "#settings-providers", label: "Speech-to-text", icon: Cloud },
                { section: "meetings", href: "#settings-meetings", label: "Meetings", icon: Users },
                { section: "apiKeys", href: "#settings-api-keys", label: "API keys", icon: Key },
                { section: "summarization", href: "#settings-summaries", label: "Summarization", icon: Sparkles },
                { section: "updates", href: "#settings-updates", label: "Updates", icon: Shield },
                { section: "language", href: "#settings-language", label: "Language", icon: Languages },
              ].map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  onClick={(event) => {
                    event.preventDefault();
                    window.history.replaceState(null, "", item.href);
                    revealRequestedSettingsSection(item.section);
                  }}
                  className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-[10.5px] font-semibold text-slate-500 no-underline outline-none transition-[background-color,color,box-shadow,transform] duration-200 hover:bg-white hover:text-slate-950 hover:shadow-sm active:translate-y-px focus-visible:ring-2 focus-visible:ring-blue-500/60 dark:hover:bg-[var(--live-card-hover)] dark:hover:text-slate-100"
                >
                  <item.icon className="h-3.5 w-3.5" aria-hidden="true" />
                  {t(item.label)}
                </a>
              ))}
            </div>
          </nav>
        )}
      />

      <div className="grid gap-4 min-[1440px]:grid-cols-2 min-[1440px]:items-start">
        <SectionPanel
          id="settings-transcription"
          title={t("Transcription")}
          description={t("Control how audio is captured and how the recording hotkey behaves.")}
          icon={Mic}
        >
          <div className="space-y-3">
            {autostartAvailable && (
              <SettingsSubsection
                title={t("Startup")}
                description={t("Control whether Scriber is ready after Windows login.")}
                icon={Shield}
              >
                <SettingLine label={t("Start with Windows")} description={t("Launch Scriber when you log in.")} className="py-0">
                  <Switch checked={autostartEnabled} onCheckedChange={handleAutostartChange} />
                </SettingLine>
              </SettingsSubsection>
            )}

            <SettingsSubsection
              title={t("Microphone input")}
              description={t("Choose the active device and keep capture warm when low latency matters.")}
              icon={Mic}
            >
              <div className="divide-y divide-slate-200/80 dark:divide-[var(--workspace-border)]">
                <SettingLine label={t("Input device")} description={t("Select the active microphone.")}>
                  <div className={cn("mic-device-dropdown w-full", isMicDropdownOpen && "is-open")}>
                    <button
                      type="button"
                      className="mic-device-dropdown-header"
                      onClick={() => setIsMicDropdownOpen((prev) => !prev)}
                      aria-label={t("Select input device")}
                      aria-expanded={isMicDropdownOpen}
                      aria-controls="mic-device-dropdown-tray"
                    >
                      <span className="mic-device-dropdown-header-info">
                        <span className={cn("mic-device-dropdown-selected-text", hasSelectedMic && "is-selected")}>
                          {selectedMicLabel || t("Select a device...")}
                        </span>
                      </span>
                      <ChevronDown className="mic-device-dropdown-chevron" />
                    </button>

                    <div id="mic-device-dropdown-tray" className="mic-device-dropdown-tray" aria-hidden={!isMicDropdownOpen}>
                      <div className="mic-device-dropdown-content">
                        <div className="mic-device-dropdown-tray-inner">
                          <div className="mic-device-list">
                            {inputDevices.length === 0 ? (
                              <div className="px-2 py-2 text-sm text-muted-foreground">{t("Loading devices...")}</div>
                            ) : (
                              inputDevices.map((device, index) => {
                                const deviceValue = device.deviceId || `device-${index}`;
                                const deviceLabel = deviceValue === "default"
                                  ? t("Default microphone")
                                  : (device.label || t("Device {{number}}", { number: formatNumber(index + 1) }));
                                const micInputId = `mic-device-${index}`;
                                const favoriteInputId = `favorite-mic-${index}`;
                                const isSelected = selectedDeviceId === deviceValue;
                                const isFavorite = favoriteMic === deviceValue;
                                return (
                                  <div
                                    key={`${deviceValue}-${index}`}
                                    className={cn("mic-device-item", isSelected && "is-selected", isFavorite && "is-favorite")}
                                  >
                                    <div className="mic-device-row-waves" aria-hidden="true">
                                      <div className="mic-device-wave-row" />
                                    </div>
                                    <input
                                      type="radio"
                                      id={micInputId}
                                      name="mic-input-device"
                                      className="mic-device-radio sr-only"
                                      checked={isSelected}
                                      onChange={() => handleMicDeviceSelectFromDropdown(deviceValue)}
                                      aria-label={t("Select microphone {{microphone}}", { microphone: deviceLabel })}
                                    />
                                    <label htmlFor={micInputId} className="mic-device-label">
                                      <span className="mic-device-name">{deviceLabel}</span>
                                      <svg className="mic-device-check" viewBox="0 0 24 24" aria-hidden="true">
                                        <path d="M 4 12 L 10 18 L 20 6" />
                                      </svg>
                                    </label>
                                    <div className="mic-device-divider" aria-hidden="true" />
                                    <input
                                      type="checkbox"
                                      id={favoriteInputId}
                                      className="mic-device-star-radio sr-only"
                                      checked={isFavorite}
                                      onChange={() => handleSetFavoriteMic(deviceValue)}
                                      aria-label={isFavorite
                                        ? t("Remove {{microphone}} from favorites", { microphone: deviceLabel })
                                        : t("Set {{microphone}} as favorite", { microphone: deviceLabel })}
                                    />
                                    <label
                                      htmlFor={favoriteInputId}
                                      className="mic-device-star-label"
                                      title={isFavorite ? t("Remove from favorites") : t("Set as favorite")}
                                    >
                                      <svg className="mic-device-star" viewBox="0 0 24 24" aria-hidden="true">
                                        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                                      </svg>
                                    </label>
                                  </div>
                                );
                              })
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </SettingLine>

                {favoriteMic && (
                  <div className="flex items-center gap-1.5 py-2 text-[11px] font-medium text-amber-600 dark:text-amber-400">
                    <Star className="h-3 w-3 fill-current" />
                    {t("Favorite microphone is used automatically when connected.")}
                  </div>
                )}

                <SettingLine label={t("Mic always on")} description={t("Keep capture pre-warmed for minimum latency.")}>
                  <Switch checked={micAlwaysOn} onCheckedChange={handleMicAlwaysOnChange} />
                </SettingLine>
              </div>
            </SettingsSubsection>

            <SettingsSubsection
              title={t("Recording control")}
              description={t("Configure the main hotkey, trigger mode, and overlay density.")}
              icon={Keyboard}
            >
              <div className="divide-y divide-slate-200/80 dark:divide-[var(--workspace-border)]">
                <SettingLine label={t("Recording mode")} description={t("Choose how the hotkey behaves.")}>
                  <ToggleGroup
                    type="single"
                    value={recordingMode}
                    onValueChange={(value) => value && void handleRecordingModeChange(value)}
                    className="grid w-[220px] max-w-full grid-cols-2 rounded-lg bg-slate-100 p-1 dark:bg-[var(--live-well)]"
                  >
                    <ToggleGroupItem value="start_stop" className="h-8 rounded-md text-[11px] data-[state=on]:bg-white data-[state=on]:text-blue-700 data-[state=on]:shadow-sm dark:data-[state=on]:bg-slate-800">
                      <ToggleLeft className="h-4 w-4" />
                      {t("Toggle")}
                    </ToggleGroupItem>
                    <ToggleGroupItem value="press_hold" className="h-8 rounded-md text-[11px] data-[state=on]:bg-white data-[state=on]:text-blue-700 data-[state=on]:shadow-sm dark:data-[state=on]:bg-slate-800">
                      <Mic className="h-4 w-4" />
                      {t("Push-to-talk")}
                    </ToggleGroupItem>
                  </ToggleGroup>
                </SettingLine>

                <SettingLine
                  label={t("Silero voice detection")}
                  description={t("Optional. Detect pauses for segmented live transcription and Soniox SmartTurn. When off, Silero is not loaded and the recording is transcribed as one continuous segment.")}
                >
                  <Switch
                    checked={segmentSpeechWithVad}
                    disabled={segmentSpeechWithVadSaving}
                    aria-busy={segmentSpeechWithVadSaving}
                    onCheckedChange={handleSegmentSpeechWithVadChange}
                  />
                </SettingLine>

                <SettingLine label={t("Global hotkey")} description={t("Shortcut to start or stop recording.")}>
                <Dialog open={isRecordingHotkey} onOpenChange={setIsRecordingHotkey}>
                <DialogTrigger asChild>
                  <Button variant="outline" className="h-8 w-[220px] max-w-full justify-start font-mono text-[11px]">
                    <Keyboard className="mr-2 h-4 w-4 text-muted-foreground" />
                    {hotkey}
                  </Button>
                </DialogTrigger>
                <DialogContent className="sm:max-w-[425px]">
                  <DialogHeader>
                    <DialogTitle>{t("Record hotkey")}</DialogTitle>
                    <DialogDescription>{t("Press the key combination you want to use as a shortcut.")}</DialogDescription>
                  </DialogHeader>
                  <div
                    ref={hotkeyCaptureRef}
                    className="flex h-32 items-center justify-center rounded-lg border-2 border-dashed bg-secondary/20 outline-none transition-colors focus:border-primary focus:bg-primary/5"
                    tabIndex={0}
                    aria-label={t("Hotkey capture area")}
                  >
                    <p className="text-lg font-medium text-primary">{hotkey}</p>
                  </div>
                  <div className="flex justify-end gap-2">
                    <Button variant="ghost" onClick={() => setIsRecordingHotkey(false)}>{t("Cancel")}</Button>
                    <Button onClick={handleSaveHotkey}>{t("Save")}</Button>
                  </div>
                </DialogContent>
              </Dialog>
                </SettingLine>

                <SettingLine
                  label={t("Visualizer bars")}
                  description={t("Current count: {{count}}", { count: formatNumber(visualizerBarCount) })}
                >
                  <div className="flex w-full items-center gap-2">
                    <BarChart3 className="h-4 w-4 shrink-0 text-slate-500" />
                    <Slider
                      value={[visualizerBarCount]}
                      onValueChange={handleVisualizerBarCountChange}
                      onValueCommit={handleVisualizerBarCountCommit}
                      min={MIN_VISUALIZER_BAR_COUNT}
                      max={MAX_VISUALIZER_BAR_COUNT}
                      step={1}
                      className="min-w-[132px] flex-1"
                    />
                  </div>
                </SettingLine>
              </div>
            </SettingsSubsection>

            <SettingsSubsection
              title={t("Transcript context")}
              description={t("Give providers and summaries the domain terms and summary behavior they need.")}
              icon={FileText}
            >
              <div className="grid gap-3">
                {customVocabularySettings}
                {summarizationPromptSettings}
              </div>
            </SettingsSubsection>

            {livePostProcessingSettings}
          </div>
        </SectionPanel>

        {speechToTextProviderPanel}

        <SectionPanel
          id="settings-meetings"
          title={t("Meetings")}
          description={t("Choose how new meetings are transcribed, summarized, protected, and connected to Outlook. Changes apply to new meetings.")}
          icon={Users}
          className="min-[1440px]:col-span-2"
        >
          <div className="grid gap-3 lg:grid-cols-2">
            <SettingsSubsection
              title={t("Transcription")}
              description={t("Choose whether to see live text or wait for the accurate transcript after the meeting.")}
              icon={Mic}
            >
              <div className="divide-y divide-slate-200/80 dark:divide-[var(--workspace-border)]">
                <div className="pb-3" role="radiogroup" aria-label={t("Meeting transcription timing")}>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {([
                      {
                        value: "final_only" as const,
                        title: t("Transcript after meeting"),
                        badge: t("Lowest cost"),
                        description: t("Records safely without cloud live text. The accurate transcript appears after you stop."),
                        cost: finalOnlyHourlyCost,
                      },
                      {
                        value: "live_final" as const,
                        title: t("Live text + accurate transcript"),
                        badge: t("Live captions"),
                        description: t("Shows words while people speak, then transcribes both saved tracks again for the final version."),
                        cost: liveAndFinalHourlyCost,
                      },
                    ] satisfies Array<{
                      value: MeetingTranscriptionMode;
                      title: string;
                      badge: string;
                      description: string;
                      cost: number | null | undefined;
                    }>).map((option) => {
                      const selected = meetingTranscriptionMode === option.value;
                      return (
                        <button
                          key={option.value}
                          type="button"
                          role="radio"
                          aria-checked={selected}
                          onClick={() => void updateMeetingPreferences({ meetingTranscriptionMode: option.value })}
                          className={cn(
                            "rounded-xl px-3.5 py-3 text-left outline-none transition-[background-color,box-shadow,transform] duration-150 focus-visible:ring-2 focus-visible:ring-primary/60 active:scale-[0.99]",
                            selected
                              ? "bg-primary/[0.08] shadow-[inset_0_0_0_1.5px_hsl(var(--primary)/0.42)]"
                              : "bg-slate-50 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.07)] hover:bg-slate-100/80 dark:bg-[var(--live-card)] dark:hover:bg-[var(--live-card-hover)]",
                          )}
                        >
                          <span className="flex items-start justify-between gap-3">
                            <span className="text-xs font-semibold text-slate-950 dark:text-slate-100">{option.title}</span>
                            <Badge variant="outline" className="shrink-0 text-[9.5px]">{option.badge}</Badge>
                          </span>
                          <span className="mt-1.5 block text-[11px] leading-4 text-slate-600 dark:text-slate-300">{option.description}</span>
                          <span className="mt-2 block font-mono text-[10.5px] font-semibold text-slate-700 dark:text-slate-200">
                            {formatMeetingHourlyCost(option.cost, localeTag, t)}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <p className="mt-2 text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">
                    {t(meetingCostEstimate?.assumption || "Estimate for a typical meeting hour with separate microphone and system-audio tracks. Provider prices can change.")}
                  </p>
                </div>
                <SettingLine label={t("Live text")} description={t("Shows words from your microphone and speakers as people talk.")}>
                  <div className="text-right">
                    <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">{meetingTranscriptionMode === "live_final" ? "Soniox Realtime" : t("Off")}</p>
                    <p className="font-mono text-[10.5px] text-slate-500">{meetingTranscriptionMode === "live_final" ? sonioxRealtimeModel : t("No live provider cost")}</p>
                  </div>
                </SettingLine>
                <SettingLine label={t("Final transcript")} description={t("Choose the service that creates the accurate transcript and speaker names after the meeting.")}>
                  <Select value={meetingFinalProvider} onValueChange={(value) => void updateMeetingPreferences({ meetingFinalProvider: value })}>
                    <SelectTrigger className="h-9 w-[248px] max-w-full text-xs" aria-label={t("Final meeting transcription model")}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MEETING_FINAL_STT_OPTIONS.map((option) => {
                        const unavailable = Boolean(missingCredentialReason(requiredCredentialForTranscriptionModel(option.credentialModel)));
                        return (
                          <SelectItem key={option.value} value={option.value} disabled={unavailable}>
                            {option.recommended ? t("Recommended: ") : ""}{option.label} ({option.model})
                          </SelectItem>
                        );
                      })}
                    </SelectContent>
                  </Select>
                </SettingLine>
                <div className="py-3">
                  <div className="rounded-lg bg-slate-50 px-3 py-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">{selectedMeetingFinalOption.label}</p>
                      <div className="flex flex-wrap items-center justify-end gap-1.5">
                        {selectedMeetingFinalOption.recommended && <Badge variant="outline" className="text-[10px]">{t("Recommended")}</Badge>}
                        <Badge variant="outline" className={selectedMeetingFinalOption.fiveHourSupported ? "border-emerald-500/40 text-[10px] text-emerald-700 dark:text-emerald-300" : "text-[10px] text-slate-500"}>
                          {selectedMeetingFinalOption.fiveHourSupported ? t("Ready for 5 hours") : t("Not for 5-hour meetings")}
                        </Badge>
                      </div>
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-slate-600 dark:text-slate-300">{t(selectedMeetingFinalOption.detail)}</p>
                    <p className="mt-1.5 font-mono text-[10.5px] text-slate-500 dark:text-slate-400">
                      {selectedMeetingFinalOption.model}. {selectedMeetingFinalOption.nativeDiarization
                        ? t("Includes speaker names and exact timing.")
                        : t("Scriber can add speaker names on this device.")} {selectedMeetingFinalOption.fiveHourSupported
                          ? t("Works with meetings up to 5 hours.")
                          : t("Choose a 5-hour option for very long meetings.")}
                    </p>
                    <p className="mt-1.5 text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">
                      {t("Remote voices coming through your speakers are separated. People sharing the selected microphone currently appear together as")} <span className="font-semibold text-slate-700 dark:text-slate-200">{t("You")}</span>.
                    </p>
                    <div className="mt-2.5 grid gap-1 border-t border-slate-200/80 pt-2.5 text-[10.5px] dark:border-[var(--workspace-border)] sm:grid-cols-3">
                      <span className="text-slate-500">{t("During meeting")} <strong className="block font-mono font-semibold text-slate-800 dark:text-slate-200">{formatMeetingHourlyCost(meetingTranscriptionMode === "live_final" ? meetingCostEstimate?.livePreviewPerMeetingHour : 0, localeTag, t)}</strong></span>
                      <span className="text-slate-500">{t("After meeting")} <strong className="block font-mono font-semibold text-slate-800 dark:text-slate-200">{formatMeetingHourlyCost(finalOnlyHourlyCost, localeTag, t)}</strong></span>
                      <span className="text-slate-500">{t("Estimated total")} <strong className="block font-mono font-semibold text-primary">{formatMeetingHourlyCost(meetingTranscriptionMode === "live_final" ? liveAndFinalHourlyCost : finalOnlyHourlyCost, localeTag, t)}</strong></span>
                    </div>
                    {meetingCostEstimate?.sources.length ? <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                      {meetingCostEstimate.sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[10.5px] text-primary hover:underline">{source.label}<ExternalLink className="h-3 w-3" /></a>)}
                      <span className="text-[10px] text-slate-500">
                        {t("Prices checked {{date}}", {
                          date: formatDate(`${meetingCostEstimate.pricingUpdatedAt}T00:00:00Z`, { dateStyle: "medium", timeZone: "UTC" }),
                        })}
                      </span>
                    </div> : null}
                  </div>
                </div>
                <SettingLine label={t("Add speaker names locally")} description={t("When the chosen service cannot identify speakers, Scriber can do it on this device for File, YouTube, and Meetings.")}>
                  <Switch
                    checked={speakerDiarizationFallbackEnabled}
                    onCheckedChange={(enabled) => void updateMeetingPreferences({ speakerDiarizationFallbackEnabled: enabled })}
                    aria-label={t("Add speaker names on this device when needed")}
                  />
                </SettingLine>
                {speakerDiarizationFallbackEnabled && <div className="py-3">
                  <div className="rounded-lg bg-slate-50 px-3 py-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">{t("Local speaker separation")}</p>
                          <Badge variant="outline" className="text-[10px]">
                            {diarizationComponent?.installed
                              ? t("Installed")
                              : diarizationComponent?.workerReady === false
                                ? t("Unavailable in this build")
                                : t("Optional download")}
                          </Badge>
                        </div>
                        <p className="mt-1 text-[11px] leading-4 text-slate-600 dark:text-slate-300">
                          {t("Separates speakers on this device for recordings up to 60 minutes. For longer recordings, choose a service that already includes speaker names.")}
                        </p>
                      </div>
                      {!diarizationComponent?.installed && <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={diarizationComponentPending || diarizationComponent?.available === false || diarizationComponent?.workerReady === false}
                        onClick={() => void installDiarizationComponent()}
                      >
                        {diarizationComponentPending
                          ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                          : <Download className="mr-2 h-3.5 w-3.5" />}
                        {t("Install speaker tool")}
                      </Button>}
                    </div>
                    {diarizationComponent?.installed && <p className="mt-2 font-mono text-[10.5px] text-slate-500 dark:text-slate-400">
                      {t("v{{version}} · {{size}} MB installed locally", {
                        version: diarizationComponent.version,
                        size: formatNumber(diarizationComponent.byteSize / 1_048_576, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
                      })}
                    </p>}
                    {!diarizationComponent?.installed && diarizationComponent?.workerReady === false && <p className="mt-2 text-[10.5px] leading-4 text-amber-700 dark:text-amber-300">
                      {t("This version of Scriber does not include local speaker separation. Update Scriber or choose a service that includes speaker names.")}
                    </p>}
                  </div>
                </div>}
                {meetingTranscriptionMode === "live_final" && <SettingLine label={t("Keep live sentences together")} description={t("Smart Turn V3 improves where the live preview ends a thought. It does not change the final transcript or its price.")}>
                  <Switch checked={meetingSmartTurnEnabled} onCheckedChange={(enabled) => void updateMeetingPreferences({ meetingSmartTurnEnabled: enabled })} aria-label={t("Keep meeting live sentences together across short pauses")} />
                </SettingLine>}
                <details className="group py-3 text-[11px]">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 font-medium text-slate-700 marker:content-none dark:text-slate-200">
                    {t("Why Scriber does not upload one-minute pieces")}
                    <ChevronDown className="h-3.5 w-3.5 text-slate-500 transition-transform group-open:rotate-180 motion-reduce:transition-none" />
                  </summary>
                  <p className="mt-2 max-w-[70ch] leading-5 text-slate-600 dark:text-slate-300">
                    {t("Small cloud requests do not reduce the audio duration you pay for and can reset speaker labels or cut words at the boundary. Scriber instead protects audio locally every 30 seconds, then gives the final service the longest supported context.")}
                  </p>
                </details>
                <SettingLine label={t("Reduce speaker echo")} description={t("Helps prevent voices from your speakers being recorded again through your microphone.")}>
                  <Switch checked={meetingAecEnabled} onCheckedChange={(enabled) => void updateMeetingPreferences({ meetingAecEnabled: enabled })} aria-label={t("Reduce speaker echo in meetings")} />
                </SettingLine>
              </div>
            </SettingsSubsection>

            <SettingsSubsection
              title={t("Summaries and storage")}
              description={t("Choose how Scriber creates the meeting brief and how long it keeps local audio.")}
              icon={Sparkles}
            >
              <div className="divide-y divide-slate-200/80 dark:divide-[var(--workspace-border)]">
                <SettingLine label={t("Meeting shortcut")} description={t("Open the meeting workspace from anywhere in Windows.")}>
                  <Dialog open={isRecordingMeetingHotkey} onOpenChange={setIsRecordingMeetingHotkey}>
                    <DialogTrigger asChild>
                      <Button variant="outline" className="h-8 w-[220px] max-w-full justify-start font-mono text-[11px]">
                        <Keyboard className="mr-2 h-4 w-4 text-muted-foreground" />
                        {meetingHotkey}
                      </Button>
                    </DialogTrigger>
                    <DialogContent className="sm:max-w-[425px]">
                      <DialogHeader>
                        <DialogTitle>{t("Meeting shortcut")}</DialogTitle>
                        <DialogDescription>{t("Press the key combination that should open the meeting workspace.")}</DialogDescription>
                      </DialogHeader>
                      <div
                        ref={meetingHotkeyCaptureRef}
                        className="flex h-32 items-center justify-center rounded-lg border-2 border-dashed bg-secondary/20 outline-none transition-colors focus:border-primary focus:bg-primary/5"
                        tabIndex={0}
                        aria-label={t("Meeting shortcut capture area")}
                      >
                        <p className="text-lg font-medium text-primary">{meetingHotkey}</p>
                      </div>
                      <div className="flex justify-end gap-2">
                        <Button variant="ghost" onClick={() => setIsRecordingMeetingHotkey(false)}>{t("Cancel")}</Button>
                        <Button onClick={handleSaveMeetingHotkey}>{t("Save")}</Button>
                      </div>
                    </DialogContent>
                  </Dialog>
                </SettingLine>
                <SettingLine label={t("Summary model")} description={t("Creates the summary, decisions, action items, and answers after the transcript is ready.")}>
                  <Select value={meetingAnalysisModel} onValueChange={(value) => void updateMeetingPreferences({ meetingAnalysisModel: value })}>
                    <SelectTrigger className="h-9 w-[220px] max-w-full text-xs" aria-label={t("Meeting summary model")}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {summarizationModelOptions.map((option) => (
                        <SelectItem
                          key={option.value}
                          value={option.value}
                          disabled={Boolean(missingCredentialReason(requiredCredentialForLanguageModel(option.value)))}
                        >
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingLine>
                <SettingLine label={t("Create meeting brief automatically")} description={t("Creates the summary, decisions, and action items when transcription is finished.")}>
                  <Switch checked={meetingAutoAnalyze} onCheckedChange={(enabled) => void updateMeetingPreferences({ meetingAutoAnalyze: enabled })} aria-label={t("Automatically analyze completed meetings")} />
                </SettingLine>
                <SettingLine label={t("Keep meeting audio")} description={t("Choose how long audio stays on this device. The transcript and notes remain until you delete them.")}>
                  <Select value={String(meetingAudioRetentionDays)} onValueChange={(value) => void updateMeetingPreferences({ meetingAudioRetentionDays: Number(value) })}>
                    <SelectTrigger className="h-9 w-[180px] max-w-full text-xs" aria-label={t("Default meeting audio retention")}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="0">{t("Until deleted")}</SelectItem>
                      <SelectItem value="7">{t("7 days")}</SelectItem>
                      <SelectItem value="30">{t("30 days")}</SelectItem>
                      <SelectItem value="90">{t("90 days")}</SelectItem>
                    </SelectContent>
                  </Select>
                </SettingLine>
                <div className="py-3">
                  <div className="flex items-start gap-2.5 rounded-lg bg-slate-50 px-3 py-2.5 text-[11.5px] leading-4 text-slate-600 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)] dark:text-slate-300">
                    <Shield className="mt-0.5 h-4 w-4 shrink-0 text-blue-600 dark:text-blue-300" />
                    <p><span className="font-semibold text-slate-950 dark:text-slate-100">{t("Protected every 30 seconds.")}</span> {t("Scriber saves audio and transcript progress while the meeting runs, so a crash should not lose the whole meeting.")}</p>
                  </div>
                </div>
              </div>
            </SettingsSubsection>

            <SettingsSubsection
              title={t("Voice Library")}
              description={t("Optionally remember familiar voices and add their names in future meetings. Voice data stays on this device and is never included in exports or support files.")}
              icon={Users}
            >
              <div className="space-y-2.5">
                <div className="divide-y divide-slate-200/80 rounded-lg border border-slate-200/80 px-3 dark:divide-[var(--workspace-border)] dark:border-[var(--workspace-border)]">
                  <SettingLine
                    label={t("Recognize familiar speakers")}
                    description={t("Turn this on only when everyone has agreed. Saved voice data stays on this device.")}
                    className="py-3"
                  >
                    <div className="flex flex-wrap items-center justify-end gap-2">
                      {(speakerModelQuery.data?.installed || (speakerProfilesQuery.data?.items.length ?? 0) > 0) && (
                        <Button type="button" size="sm" variant="ghost" className="text-destructive hover:text-destructive" disabled={voiceLibraryDeletePending || speakerProfileMutation.isPending} onClick={() => setVoiceLibraryDeleteOpen(true)}>
                          <Trash2 className="mr-1.5 h-3.5 w-3.5" />{t("Delete voice data")}
                        </Button>
                      )}
                      <Switch
                        checked={voiceprintLibraryOptIn}
                        disabled={voiceLibraryDeletePending}
                        onCheckedChange={handleVoiceprintOptInChange}
                        aria-label={t("Recognize familiar speakers in future meetings")}
                      />
                    </div>
                  </SettingLine>
                  <SettingLine
                    label={t("Voice recognition download")}
                    description={t("A one-time local download. Scriber checks it before use and applies it automatically to new meetings.")}
                    className="py-3"
                  >
                    {speakerModelQuery.data?.installed ? (
                      <Badge variant="outline" className={cn(
                        "text-[10px]",
                        voiceprintLibraryOptIn
                          ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
                          : "text-slate-500",
                      )}>{voiceprintLibraryOptIn ? t("Ready") : t("Installed, off")}</Badge>
                    ) : (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={!voiceprintLibraryOptIn || speakerModelMutation.isPending || speakerModelQuery.isLoading}
                        onClick={() => speakerModelMutation.mutate()}
                      >
                        {speakerModelMutation.isPending ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : <Download className="mr-2 h-3.5 w-3.5" />}
                        {voiceprintLibraryOptIn ? t("Download") : t("Turn on first")}
                      </Button>
                    )}
                  </SettingLine>
                </div>
                <div className="flex flex-col gap-3 rounded-lg bg-slate-50 px-3 py-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)] sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">
                      {speakerProfilesQuery.data?.enabled ? t("Voice recognition is ready") : t("Voice recognition is off")}
                    </p>
                    <p className="mt-1 text-[11px] leading-4 text-slate-600 dark:text-slate-300">
                      {voiceprintLibraryOptIn && speakerModelQuery.data?.installed
                        ? t("Add a short named sample now, or let Scriber learn familiar voices from meetings. Saved voice data never leaves this device.")
                        : voiceprintLibraryOptIn
                          ? t("Download voice recognition above before adding a named voice sample.")
                          : t("Turn on familiar speaker recognition before adding a named voice sample.")}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
                    <Badge variant="outline" className="text-[10px]">
                      {t(savedSpeakerCount === 1 ? "{{count}} saved speaker" : "{{count}} saved speakers", {
                        count: formatNumber(savedSpeakerCount),
                      })}
                    </Badge>
                    <Button
                      type="button"
                      size="sm"
                      className="whitespace-nowrap active:scale-[0.98]"
                      disabled={!voiceprintLibraryOptIn || !speakerModelQuery.data?.installed || speakerModelQuery.isLoading || voiceLibraryDeletePending}
                      onClick={() => handleVoiceEnrollmentOpenChange(true)}
                    >
                      <Mic className="mr-1.5 h-3.5 w-3.5" />
                      {t("Add voice")}
                    </Button>
                  </div>
                </div>
                {speakerProfilesQuery.isLoading && (
                  <div className="space-y-2" aria-label={t("Loading saved speakers")}>
                    {[0, 1].map((item) => (
                      <div key={item} className="h-12 animate-pulse rounded-lg bg-slate-100 motion-reduce:animate-none dark:bg-[var(--live-card)]" />
                    ))}
                  </div>
                )}
                {speakerProfilesQuery.isError && (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-500/35 bg-amber-50 px-3 py-2 text-[11px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
                    <span>{t("Saved speakers could not be loaded.")}</span>
                    <Button type="button" size="sm" variant="outline" className="h-7" onClick={() => void speakerProfilesQuery.refetch()}>{t("Try again")}</Button>
                  </div>
                )}
                {speakerProfilesQuery.data?.items.length === 0 && (
                  <p className="rounded-lg border border-dashed border-slate-300 px-3 py-4 text-center text-[11px] text-slate-500 dark:border-[var(--workspace-border)]">
                    {t("No saved speakers yet. Add a named voice sample, or let Scriber learn familiar voices from future meetings.")}
                  </p>
                )}
                {speakerProfilesQuery.data?.items.map((profile) => (
                  <div key={profile.id} className="flex min-w-0 items-center gap-2 rounded-lg border border-slate-200/80 px-2.5 py-2 dark:border-[var(--workspace-border)]">
                    {editingSpeakerProfileId === profile.id ? (
                      <Input
                        autoFocus
                        value={speakerProfileName}
                        onChange={(event) => setSpeakerProfileName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && speakerProfileName.trim()) speakerProfileMutation.mutate({ action: "rename", id: profile.id, displayName: speakerProfileName });
                          if (event.key === "Escape") setEditingSpeakerProfileId("");
                        }}
                        className="h-8 min-w-0 flex-1 text-xs"
                        aria-label={t("Name saved speaker {{name}}", { name: profile.displayName })}
                      />
                    ) : (
                      <button
                        type="button"
                        className="min-w-0 flex-1 rounded-md px-1 py-0.5 text-left transition-transform duration-150 active:scale-[0.98]"
                        onClick={() => { setEditingSpeakerProfileId(profile.id); setSpeakerProfileName(profile.displayName); }}
                        title={t("Rename saved speaker")}
                      >
                        <span className="block truncate text-xs font-semibold text-slate-950 dark:text-slate-100">{profile.displayName}</span>
                        <span className="block text-[10.5px] text-slate-500">
                          {profile.enrolled
                            ? t(
                                profile.sampleCount === 1
                                  ? "Named voice sample saved. {{count}} sample total."
                                  : "Named voice sample saved. {{count}} samples total.",
                                { count: formatNumber(profile.sampleCount) },
                              )
                            : t(
                                profile.sampleCount === 1
                                  ? profile.isNamed
                                    ? "{{count}} meeting match. Name saved."
                                    : "{{count}} meeting match. Choose a name."
                                  : profile.isNamed
                                    ? "{{count}} meeting matches. Name saved."
                                    : "{{count}} meeting matches. Choose a name.",
                                { count: formatNumber(profile.sampleCount) },
                              )}
                        </span>
                      </button>
                    )}
                    {editingSpeakerProfileId === profile.id && (
                      <Button size="sm" className="h-8" disabled={!speakerProfileName.trim() || speakerProfileMutation.isPending} onClick={() => speakerProfileMutation.mutate({ action: "rename", id: profile.id, displayName: speakerProfileName })}>{t("Save")}</Button>
                    )}
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      className="h-8 w-8 shrink-0 text-slate-500 hover:text-destructive active:scale-[0.96]"
                      disabled={speakerProfileMutation.isPending || voiceLibraryDeletePending}
                      onClick={() => setSpeakerProfilePendingDelete({ id: profile.id, name: profile.displayName })}
                      aria-label={t("Delete saved speaker {{name}}", { name: profile.displayName })}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ))}
                {(speakerProfilesQuery.data?.items.length ?? 0) >= 2 && (
                  <div className="rounded-lg border border-slate-200/80 p-3 dark:border-[var(--workspace-border)]">
                    <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">{t("Merge duplicate speakers")}</p>
                    <p className="mt-1 text-[11px] leading-4 text-slate-500">{t("Keep the correct speaker and merge the duplicate into it.")}</p>
                    <div className="mt-2.5 grid gap-2 sm:grid-cols-2">
                      <Select value={mergeTargetProfileId} onValueChange={setMergeTargetProfileId}>
                        <SelectTrigger className="h-9 min-w-0 text-xs" aria-label={t("Saved speaker to keep")}><SelectValue placeholder={t("Keep speaker…")} /></SelectTrigger>
                        <SelectContent>{speakerProfilesQuery.data?.items.map((profile) => <SelectItem key={profile.id} value={profile.id}>{profile.displayName}, {formatNumber(profile.sampleCount)} {profile.sampleCount === 1 ? t("sample") : t("samples")}</SelectItem>)}</SelectContent>
                      </Select>
                      <Select value={mergeSourceProfileId} onValueChange={setMergeSourceProfileId}>
                        <SelectTrigger className="h-9 min-w-0 text-xs" aria-label={t("Duplicate saved speaker")}><SelectValue placeholder={t("Merge duplicate…")} /></SelectTrigger>
                        <SelectContent>{speakerProfilesQuery.data?.items.filter((profile) => profile.id !== mergeTargetProfileId).map((profile) => <SelectItem key={profile.id} value={profile.id}>{profile.displayName}, {formatNumber(profile.sampleCount)} {profile.sampleCount === 1 ? t("sample") : t("samples")}</SelectItem>)}</SelectContent>
                      </Select>
                      <Button type="button" size="sm" variant="outline" className="h-9 sm:col-span-2" disabled={!mergeTargetProfileId || !mergeSourceProfileId || mergeTargetProfileId === mergeSourceProfileId || mergeProfilesMutation.isPending} onClick={() => mergeProfilesMutation.mutate()}>{t("Merge speakers")}</Button>
                    </div>
                  </div>
                )}
              </div>
            </SettingsSubsection>

            <SettingsSubsection
              title={t("Outlook calendar")}
              description={t("Connect Outlook once. Scriber then suggests meeting titles and participants and addresses recap emails for you.")}
              icon={CalendarClock}
            >
              <div className="space-y-3">
                <div className="rounded-lg bg-slate-50 px-3 py-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs font-semibold text-slate-950 dark:text-slate-100">
                      {outlookQuery.isLoading
                        ? t("Checking Outlook")
                        : outlookQuery.isError || outlookCredentialStatusUnavailable
                          ? t("Outlook status could not be checked")
                        : outlookQuery.data?.authorizationPending
                          ? t("Finish signing in with Microsoft")
                          : outlookQuery.data?.connected
                            ? t("Outlook is connected")
                            : outlookQuery.data?.configured
                              ? outlookQuery.data.lastError ? t("Outlook needs to reconnect") : t("Outlook is ready to connect")
                              : t("Outlook is not available in this release")}
                    </p>
                    <Badge variant="outline" className={cn(
                      "text-[10px]",
                      outlookQuery.data?.connected && !outlookQuery.data.authorizationPending && !outlookCredentialStatusUnavailable && "border-emerald-500/40 text-emerald-700 dark:text-emerald-300",
                      !outlookQuery.isLoading && (!outlookQuery.data?.connected || outlookQuery.data.authorizationPending) && "border-amber-500/40 text-amber-700 dark:text-amber-300",
                    )}>
                      {outlookQuery.isLoading ? t("Checking") : outlookQuery.isError || outlookCredentialStatusUnavailable ? t("Unavailable") : outlookQuery.data?.authorizationPending ? t("Waiting") : outlookQuery.data?.connected ? t("Connected") : t("Not connected")}
                    </Badge>
                  </div>
                  <p className="mt-1 text-[11px] leading-4 text-slate-600 dark:text-slate-300">
                    {outlookQuery.isError || outlookCredentialStatusUnavailable
                        ? t("Scriber could not check the protected Outlook sign-in right now. Previously synchronized calendar entries stay on this device; choose Check again before reconnecting.")
                      : outlookQuery.data?.authorizationPending
                        ? t("Complete the Microsoft sign-in in your browser. This page updates automatically when you return.")
                      : outlookQuery.data?.connected
                        ? t("Upcoming meeting titles and participants now appear automatically. Scriber cannot edit your calendar or see your Microsoft password.")
                        : outlookQuery.data?.configured
                          ? t("Click Connect Outlook below. Microsoft opens in your browser and asks for read-only calendar access.")
                          : t("This release was published without Microsoft sign-in. Reinstalling the same version will not fix it. Check for a newer release that lists Outlook calendar support.")}
                  </p>
                  {outlookQuery.data?.connected && !outlookQuery.data.authorizationPending && outlookQuery.data.account && (
                    <p className="mt-1.5 truncate text-[10.5px] text-slate-500">
                      {t("Connected as")} {outlookQuery.data.account.name || outlookQuery.data.account.address} · {outlookQuery.data.account.address}
                    </p>
                  )}
                  {outlookQuery.data?.lastSyncAt && <p className="mt-1.5 font-mono text-[10.5px] text-slate-500">{t("Last sync ·")} {formatUpdateTimestamp(outlookQuery.data.lastSyncAt, formatDate, t)}</p>}
                  {outlookQuery.data?.lastError && <p className="mt-1.5 text-[10.5px] text-amber-700 dark:text-amber-300">{outlookSyncErrorMessage(outlookQuery.data.lastError, t)}</p>}
                </div>
                {!outlookQuery.isLoading && (!outlookQuery.data?.connected || outlookQuery.data.authorizationPending) && (
                  <ol className="grid gap-2 rounded-lg border border-slate-200/80 p-3 text-[11px] leading-4 text-slate-600 dark:border-[var(--workspace-border)] dark:text-slate-300">
                    {(outlookQuery.isError || outlookCredentialStatusUnavailable
                      ? [
                          "Restart Scriber.",
                          "Return to this page and check the Outlook status again.",
                          "If the message remains, check for a newer Scriber release.",
                        ]
                      : outlookQuery.data?.authorizationPending
                      ? [
                          "Return to the Microsoft sign-in in your browser.",
                          "Finish signing in and allow read-only calendar access.",
                          "Come back to Scriber; this status updates automatically.",
                        ]
                      : outlookQuery.data?.configured
                        ? [
                          "Choose Connect Outlook below.",
                          "Sign in with Microsoft and allow read-only calendar access.",
                          "Return to Scriber; upcoming meetings sync automatically.",
                          ]
                        : [
                          "Check whether a newer Scriber version is available.",
                          "Read its release notes and install a version that lists Outlook calendar support.",
                          "Restart Scriber, then return here and choose Connect Outlook.",
                          ]).map((step, index) => (
                          <li key={step} className="flex items-start gap-2">
                            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-blue-600 text-[10px] font-semibold text-white">{index + 1}</span>
                            <span className="pt-0.5">{t(step)}</span>
                          </li>
                        ))}
                  </ol>
                )}
                {!outlookQuery.isLoading && !outlookQuery.isError && !outlookQuery.data?.configured && (
                  <details className="rounded-lg border border-slate-200/80 px-3 py-2 dark:border-[var(--workspace-border)]">
                    <summary className="cursor-pointer text-[11px] font-semibold text-slate-700 dark:text-slate-200">{t("Help for self-built copies")}</summary>
                    <p className="mt-2 text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">
                      {t("Before starting Scriber, set")} <code className="rounded bg-slate-100 px-1 py-0.5 font-mono dark:bg-[var(--live-well)]">SCRIBER_OUTLOOK_CLIENT_ID</code> {t("to the application ID from your Microsoft Entra public-client registration.")}
                    </p>
                  </details>
                )}
                {outlookQuery.data?.nextEvent && (
                  <div className="rounded-lg border border-slate-200/80 px-3 py-2.5 dark:border-[var(--workspace-border)]">
                    <p className="truncate text-xs font-semibold text-slate-950 dark:text-slate-100">{outlookQuery.data.nextEvent.subject}</p>
                    <p className="mt-1 text-[10.5px] text-slate-500">{t("Next event,")} {formatUpdateTimestamp(outlookQuery.data.nextEvent.start_at, formatDate, t)}, {formatNumber(outlookQuery.data.nextEvent.participants.length)} {t("participants")}</p>
                  </div>
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  {outlookQuery.isError || outlookCredentialStatusUnavailable ? (
                    <Button size="sm" variant="outline" disabled={outlookQuery.isFetching} onClick={() => void outlookQuery.refetch()}>
                      <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", outlookQuery.isFetching && "animate-spin motion-reduce:animate-none")} />
                      {t("Check again")}
                    </Button>
                  ) : outlookQuery.data?.authorizationPending ? (
                    <Button size="sm" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("connect")}>
                      {outlookMutation.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ExternalLink className="mr-1.5 h-3.5 w-3.5" />}
                      {t("Reopen Microsoft sign-in")}
                    </Button>
                  ) : outlookQuery.data?.connected ? (
                    <>
                      <Button size="sm" variant="outline" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("sync")}>
                        <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", outlookMutation.isPending && "animate-spin motion-reduce:animate-none")} />
                        {t("Sync now")}
                      </Button>
                      <Button size="sm" variant="outline" className="border-destructive/45 text-destructive hover:bg-destructive/10" disabled={outlookMutation.isPending} onClick={() => setOutlookDisconnectOpen(true)}>{t("Disconnect Outlook")}</Button>
                    </>
                  ) : outlookQuery.data?.configured ? (
                    <Button size="sm" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("connect")}>
                      {outlookMutation.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ExternalLink className="mr-1.5 h-3.5 w-3.5" />}
                      {outlookQuery.data.lastError ? t("Reconnect Outlook") : t("Connect Outlook")}
                    </Button>
                  ) : null}
                </div>
              </div>
            </SettingsSubsection>
          </div>
        </SectionPanel>

        <SectionPanel
          id="settings-api-keys"
          title={t("API keys")}
          description={t("Manage provider credentials without expanding the whole page.")}
          icon={Key}
          className="flex h-full self-stretch flex-col"
        >
          <div className="flex flex-1 flex-col gap-3.5">
            {missingActiveCredentialRequirements.length > 0 && (
              <div className="rounded-xl border border-amber-500/35 bg-amber-50 p-2.5 text-[11px] leading-[15px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
                <div className="flex gap-2">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-300" aria-hidden="true" />
                  <div>
                    <p className="font-semibold">{t("Credential required before model selection.")}</p>
                    <p className="mt-1">
                      {t("Save")}{" "}
                      {missingActiveCredentialRequirements.map((requirement, index) => (
                        <span key={requirement.provider}>
                          {index > 0 ? ", " : ""}
                          <button
                            type="button"
                            onClick={() => openCredentialDialog(requirement)}
                            className="rounded-md px-1.5 py-0.5 font-semibold text-amber-950 underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/50 dark:text-amber-100"
                          >
                            {t(requirement.label)}
                          </button>
                        </span>
                      ))}{" "}
                      {t("below, or choose a model that already has credentials.")}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {!hasAnyManagedCloudSttCredential && (
              <div className="rounded-xl bg-slate-50 p-2.5 text-[11px] leading-[15px] text-slate-500 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                {t("No cloud STT credentials are saved yet.")}
              </div>
            )}

            <div className="grid flex-1 content-between gap-x-2 gap-y-2.5 sm:grid-cols-2 sm:gap-y-3 xl:gap-y-3.5">
              <ApiCredentialRow provider="OpenAI" icon="openai" value={openAIKey} onValueChange={markCredentialChanged("OpenAI", setOpenAIKey)} show={showOpenAIKey} onShowChange={setShowOpenAIKey} helpKey="openai" saved={savedKeys.OpenAI === true} onSave={() => handleSaveApiKey("OpenAI")} note={t("Used for OpenAI STT and summarization.")} {...credentialDialogProps("OpenAI")} />
              <ApiCredentialRow provider="Gemini" icon="gemini" value={geminiKey} onValueChange={markCredentialChanged("Gemini", setGeminiKey)} show={showGeminiKey} onShowChange={setShowGeminiKey} helpKey="gemini" saved={savedKeys.Gemini === true} onSave={() => handleSaveApiKey("Gemini")} note={t("One key unlocks Gemini STT, summaries, and cleanup.")} {...credentialDialogProps("Gemini")} />
              <ApiCredentialRow provider="OpenRouter" icon="openrouter" value={openRouterKey} onValueChange={markCredentialChanged("OpenRouter", setOpenRouterKey)} show={showOpenRouterKey} onShowChange={setShowOpenRouterKey} helpKey="openrouter" saved={savedKeys.OpenRouter === true} onSave={() => handleSaveApiKey("OpenRouter")} {...credentialDialogProps("OpenRouter")} />
              <ApiCredentialRow provider="Cerebras" icon="cerebras" value={cerebrasKey} onValueChange={markCredentialChanged("Cerebras", setCerebrasKey)} show={showCerebrasKey} onShowChange={setShowCerebrasKey} helpKey="cerebras" saved={savedKeys.Cerebras === true} onSave={() => handleSaveApiKey("Cerebras")} note={t("Used for direct Cerebras summary and cleanup models.")} {...credentialDialogProps("Cerebras")} />
              <ApiCredentialRow provider="YouTube" icon="youtube" value={youtubeKey} onValueChange={markCredentialChanged("YouTube", setYoutubeKey)} show={showYoutubeKey} onShowChange={setShowYoutubeKey} helpKey="youtube" saved={savedKeys.YouTube === true} onSave={() => handleSaveApiKey("YouTube")} note={t("Used for search and metadata in the YouTube tab.")} {...credentialDialogProps("YouTube")} />
              <ApiCredentialRow provider="Soniox" icon="soniox" value={sonioxKey} onValueChange={markCredentialChanged("Soniox", setSonioxKey)} show={showSonioxKey} onShowChange={setShowSonioxKey} helpKey="soniox" saved={savedKeys.Soniox === true} onSave={() => handleSaveApiKey("Soniox")} note={t("Use one Soniox API key and choose where Soniox processes your audio.")} {...credentialDialogProps("Soniox")}>
                <SonioxRegionPicker
                  value={sonioxRegion}
                  onValueChange={(nextRegion) => {
                    setSonioxRegion(nextRegion);
                    setSavedKeys((prev) => ({ ...prev, Soniox: false }));
                    setCredentialReadyKeys((prev) => ({ ...prev, Soniox: false }));
                  }}
                />
              </ApiCredentialRow>
              <ApiCredentialRow provider="Modulate.AI" icon="modulate" value={modulateKey} onValueChange={markCredentialChanged("Modulate.AI", setModulateKey)} show={showModulateKey} onShowChange={setShowModulateKey} helpKey="modulate" saved={savedKeys["Modulate.AI"] === true} onSave={() => handleSaveApiKey("Modulate.AI")} note={t("One key enables multilingual realtime and batch transcription. Scriber requests final transcript text only and leaves enrichment signals off.")} placeholder={t("Enter Modulate.AI API key")} {...credentialDialogProps("Modulate.AI")} />
              <ApiCredentialRow provider="Mistral" icon="mistral" value={mistralKey} onValueChange={markCredentialChanged("Mistral", setMistralKey)} show={showMistralKey} onShowChange={setShowMistralKey} helpKey="mistral" saved={savedKeys.Mistral === true} onSave={() => handleSaveApiKey("Mistral")} {...credentialDialogProps("Mistral")} />
              <ApiCredentialRow provider="Smallest AI" icon="smallest" value={smallestKey} onValueChange={markCredentialChanged("Smallest AI", setSmallestKey)} show={showSmallestKey} onShowChange={setShowSmallestKey} helpKey="smallest" saved={savedKeys["Smallest AI"] === true} onSave={() => handleSaveApiKey("Smallest AI")} {...credentialDialogProps("Smallest AI")} />
              <ApiCredentialRow provider="AssemblyAI" icon="assemblyai" value={assemblyAIKey} onValueChange={markCredentialChanged("AssemblyAI", setAssemblyAIKey)} show={showAssemblyAIKey} onShowChange={setShowAssemblyAIKey} helpKey="assemblyai" saved={savedKeys.AssemblyAI === true} onSave={() => handleSaveApiKey("AssemblyAI")} {...credentialDialogProps("AssemblyAI")} />
              <ApiCredentialRow provider="Deepgram" icon="deepgram" value={deepgramKey} onValueChange={markCredentialChanged("Deepgram", setDeepgramKey)} show={showDeepgramKey} onShowChange={setShowDeepgramKey} helpKey="deepgram" saved={savedKeys.Deepgram === true} onSave={() => handleSaveApiKey("Deepgram")} {...credentialDialogProps("Deepgram")} />
              <ApiCredentialRow provider="Gladia" icon="gladia" value={gladiaKey} onValueChange={markCredentialChanged("Gladia", setGladiaKey)} show={showGladiaKey} onShowChange={setShowGladiaKey} helpKey="gladia" saved={savedKeys.Gladia === true} onSave={() => handleSaveApiKey("Gladia")} {...credentialDialogProps("Gladia")} />
              <ApiCredentialRow provider="Groq" icon="groq" value={groqKey} onValueChange={markCredentialChanged("Groq", setGroqKey)} show={showGroqKey} onShowChange={setShowGroqKey} helpKey="groq" saved={savedKeys.Groq === true} onSave={() => handleSaveApiKey("Groq")} {...credentialDialogProps("Groq")} />
              <ApiCredentialRow provider="Speechmatics" icon="speechmatics" value={speechmaticsKey} onValueChange={markCredentialChanged("Speechmatics", setSpeechmaticsKey)} show={showSpeechmaticsKey} onShowChange={setShowSpeechmaticsKey} helpKey="speechmatics" saved={savedKeys.Speechmatics === true} onSave={() => handleSaveApiKey("Speechmatics")} {...credentialDialogProps("Speechmatics")} />
              <ApiCredentialRow provider="ElevenLabs" icon="elevenlabs" value={elevenLabsKey} onValueChange={markCredentialChanged("ElevenLabs", setElevenLabsKey)} show={showElevenLabsKey} onShowChange={setShowElevenLabsKey} helpKey="elevenlabs" saved={savedKeys.ElevenLabs === true} onSave={() => handleSaveApiKey("ElevenLabs")} {...credentialDialogProps("ElevenLabs")} />
              <ApiCredentialRow provider="Google Cloud" icon="googlecloud" value={googleApplicationCredentials} onValueChange={markCredentialChanged("Google Cloud", setGoogleApplicationCredentials)} helpKey="googleCloud" saved={savedKeys["Google Cloud"] === true} onSave={() => handleSaveApiKey("Google Cloud")} inputType="text" placeholder={"C:\\\\path\\\\to\\\\service-account.json"} note={t("Google Cloud STT uses Cloud credentials, not the Gemini API key. Enter the service account JSON path for the speech.googleapis.com project.")} {...credentialDialogProps("Google Cloud")} />
              <ApiCredentialRow provider="Azure MAI" credentialId="Azure" icon="azure" value={azureMaiKey} onValueChange={markCredentialChanged("Azure", setAzureMaiKey)} show={showAzureMaiKey} onShowChange={setShowAzureMaiKey} helpKey="azure" saved={savedKeys.Azure === true} onSave={() => handleSaveApiKey("Azure")} note={t("The key must belong to a region that supports the configured model.")} {...credentialDialogProps("Azure")}>
                <div className="grid gap-3 sm:grid-cols-2">
                  <FieldShell label={t("Region")}>
                    <Input value={azureMaiRegion} onChange={(event) => markCredentialChanged("Azure", setAzureMaiRegion)(event.target.value)} placeholder="northeurope" className="font-mono text-sm" />
                  </FieldShell>
                  <FieldShell label={t("Model")}>
                    <Input value={azureMaiModel} onChange={(event) => markCredentialChanged("Azure", setAzureMaiModel)(event.target.value)} placeholder="mai-transcribe-1.5" className="font-mono text-sm" />
                  </FieldShell>
                </div>
              </ApiCredentialRow>
            </div>
          </div>
        </SectionPanel>

        <SectionPanel
          id="settings-summaries"
          title={t("Summarization")}
          description={t("Choose the model and automatic summary behavior.")}
          icon={Sparkles}
          className="flex h-full self-stretch flex-col"
        >
          <div className="flex flex-1 flex-col justify-between gap-3">
            <div
              role="radiogroup"
              aria-label={t("Summary models")}
              className="space-y-1.5"
            >
              {summaryModelGroups.map((group) => (
                <div
                  key={group.key}
                  className="rounded-xl bg-slate-50/90 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]"
                >
                  <div className="mb-1">
                    <h3 className="text-[13px] !font-bold leading-4 text-slate-950 dark:text-slate-100">
                      {group.label}
                    </h3>
                  </div>
                  <div className="grid gap-x-2 gap-y-1 sm:grid-cols-2">
                    {group.items.map((option) => {
                      const requirement = requiredCredentialForLanguageModel(option.value);
                      const disabledReason = missingCredentialReason(requirement);
                      return (
                        <SummaryModelChoice
                          key={option.value}
                          option={option}
                          selected={summarizationModel === option.value}
                          disabled={Boolean(disabledReason)}
                          disabledReason={disabledReason}
                          onCredentialAction={() => openCredentialDialog(requirement)}
                          onSelect={() => void handleSummarizationModelChange(option.value)}
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            <div className="grid gap-x-4 border-t border-slate-200/80 pt-2 dark:border-[var(--workspace-border)] sm:grid-cols-2">
              <SettingLine
                label={t("Auto-summarize")}
                description={t("Summarize new transcripts automatically.")}
                className="py-1.5 sm:grid-cols-[minmax(0,1fr)_auto]"
              >
                <Switch checked={autoSummarize} onCheckedChange={handleAutoSummarizeChange} />
              </SettingLine>
              <SettingLine
                label={t("YouTube captions first")}
                description={t("Prefer available captions, then fall back to audio.")}
                className="border-t border-slate-200/80 py-1.5 pt-2 dark:border-[var(--workspace-border)] sm:grid-cols-[minmax(0,1fr)_auto] sm:border-l sm:border-t-0 sm:pl-4 sm:pt-1.5"
              >
                <Switch
                  checked={youtubePreferCaptions}
                  onCheckedChange={handleYoutubePreferCaptionsChange}
                  aria-label={t("Use YouTube captions before audio transcription")}
                />
              </SettingLine>
            </div>
          </div>
        </SectionPanel>

        <SectionPanel
          id="settings-updates"
          title={t("Update app")}
          description={t("Keep Scriber current without interrupting recordings.")}
          icon={Shield}
          className="flex h-full self-stretch flex-col"
        >
          <div className="flex flex-1 flex-col justify-between gap-3">
            <div className="grid gap-2 sm:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] sm:items-stretch">
              <div className="grid gap-2 rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div>
                  <p className="text-[12px] font-semibold leading-4 text-slate-950 dark:text-slate-100">{t("Update status")}</p>
                  <p className="mt-0.5 text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400">{t(desktopUpdate.message, desktopUpdate.messageValues)}</p>
                </div>
                <Badge variant={desktopUpdateBadgeVariant}>{desktopUpdateBadgeLabel}</Badge>
              </div>
              <div className="grid gap-2 rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center dark:bg-[var(--live-card)]">
                <div>
                  <p className="text-[12px] font-semibold leading-4 text-slate-950 dark:text-slate-100">{t("Automatic checks")}</p>
                  <p className="mt-0.5 text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400">{t("Weekly background checks via GitHub.")}</p>
                </div>
                <Switch checked={desktopUpdate.autoCheckEnabled} onCheckedChange={handleDesktopAutoCheckChange} disabled={isInstallingDesktopUpdate} />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                <p className="text-[10px] leading-3 text-slate-500">{t("Current")}</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdate.currentVersion || t("Unknown")}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                <p className="text-[10px] leading-3 text-slate-500">{t("Available")}</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateAvailableVersionLabel}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                <p className="text-[10px] leading-3 text-slate-500">{t("Last check")}</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateLastCheckedLabel}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)]">
                <p className="text-[10px] leading-3 text-slate-500">{t("Next check")}</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateNextCheckLabel}</p>
              </div>
            </div>

            {desktopUpdateProgress && (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3 text-[11px] text-slate-500">
                  <span>{t(desktopUpdateProgress.message)}</span>
                  {typeof desktopUpdateProgress.percent === "number" && <span>{formatNumber(desktopUpdateProgress.percent)}%</span>}
                </div>
                <Progress value={desktopUpdateProgress.percent ?? 0} />
              </div>
            )}

            <div className="space-y-2">
              <div className="grid gap-2 sm:grid-cols-2">
                <Button variant="outline" className="h-8 text-[12px]" onClick={handleCheckDesktopUpdate} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                  {isCheckingDesktopUpdate ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                  {t("Check for updates")}
                </Button>
                <Button className="h-8 text-[12px]" onClick={handleInstallDesktopUpdate} disabled={!desktopUpdate.available || isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                  {isInstallingDesktopUpdate ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
                  {t("Install and restart")}
                </Button>
              </div>
              {desktopUpdate.available && (
                <div className="grid grid-cols-2 gap-2">
                  <Button variant="outline" className="h-8 text-[12px]" onClick={handleRemindDesktopUpdateLater} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                    {t("Remind tomorrow")}
                  </Button>
                  <Button variant="outline" className="h-8 text-[12px]" onClick={handleSkipDesktopUpdateVersion} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                    {t("Skip version")}
                  </Button>
                </div>
              )}
              <Button variant="ghost" size="sm" className="h-8 justify-start px-1 text-[12px]" onClick={handleOpenDesktopUpdateReleaseNotes} disabled={isInstallingDesktopUpdate}>
                <ExternalLink className="mr-2 h-4 w-4" />
                {t("Release notes")}
              </Button>
            </div>
          </div>
        </SectionPanel>

        <SectionPanel
          id="settings-language"
          title={t("Language")}
          description={t("Auto-detect or choose a preferred transcription language.")}
          icon={Languages}
          className="flex h-full self-stretch flex-col"
        >
          <div className="flex flex-1 flex-col justify-evenly gap-4">
            <SettingLine
              label={t("Interface language")}
              description={t("The interface language applies immediately and is saved on this device.")}
            >
              <ToggleGroup
                type="single"
                value={locale}
                onValueChange={(value) => {
                  if (value === "de" || value === "en") setLocale(value);
                }}
                aria-label={t("Interface language")}
                className="grid w-full grid-cols-2"
              >
                <ToggleGroupItem value="de" aria-label={t("Switch interface to German")} className="h-9 px-3 text-xs">
                  Deutsch
                </ToggleGroupItem>
                <ToggleGroupItem value="en" aria-label={t("Switch interface to English")} className="h-9 px-3 text-xs">
                  English
                </ToggleGroupItem>
              </ToggleGroup>
            </SettingLine>

            <SettingLine label={t("Auto-detect language")} description={t("Let the provider infer spoken language.")}>
              <Switch
                checked={language === "auto"}
                onCheckedChange={(enabled) => void handleLanguageChange(enabled ? "auto" : "en")}
              />
            </SettingLine>

            <SettingLine label={t("Preferred language")} description={t("Used when auto-detect is off.")}>
              <div className={cn("language-dropdown w-full", isLanguageDropdownOpen && "is-open")}>
                <button
                  type="button"
                  className="language-dropdown-header"
                  onClick={() => setIsLanguageDropdownOpen((prev) => !prev)}
                  aria-label={t("Select default transcription language")}
                  aria-expanded={isLanguageDropdownOpen}
                  aria-controls="language-dropdown-tray"
                >
                  <span className="language-dropdown-header-info">
                    <span className="language-dropdown-selected-value-wrapper">
                      <LanguageFlag value={selectedLanguage.value} className="language-header-flag" />
                      <span className="language-dropdown-selected-text is-selected">{t(selectedLanguage.label)}</span>
                    </span>
                  </span>
                  <ChevronDown className="language-dropdown-chevron" />
                </button>

                <div id="language-dropdown-tray" className="language-dropdown-tray" aria-hidden={!isLanguageDropdownOpen}>
                  <div className="language-dropdown-content">
                    <div className="language-dropdown-tray-inner">
                      <div className="language-list">
                        {LANGUAGE_OPTIONS.map((option) => {
                          const isSelected = option.value === language;
                          const inputId = `lang-option-${option.value}`;
                          return (
                            <div key={option.value} className={cn("language-item", isSelected && "is-selected")}>
                              <div className="language-row-waves" aria-hidden="true">
                                <div className="language-wave-row" />
                              </div>
                              <input
                                type="radio"
                                id={inputId}
                                name="default-transcription-language"
                                className="language-radio sr-only"
                                checked={isSelected}
                                onChange={() => handleLanguageSelectFromDropdown(option.value)}
                                aria-label={t("Select {{language}} as default transcription language", { language: t(option.label) })}
                              />
                              <label htmlFor={inputId} className="language-option-label">
                                <LanguageFlag value={option.value} />
                                <span className="language-name">{t(option.label)}</span>
                                <svg className="language-check" viewBox="0 0 24 24" aria-hidden="true">
                                  <path d="M 4 12 L 10 18 L 20 6" />
                                </svg>
                              </label>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </SettingLine>
          </div>
        </SectionPanel>
      </div>
      <Dialog open={voiceEnrollmentOpen} onOpenChange={handleVoiceEnrollmentOpenChange}>
        <DialogContent
          className={cn(
            "max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] overflow-y-auto sm:max-w-[520px]",
            voiceEnrollmentMutation.isPending && "[&>button:last-child]:pointer-events-none [&>button:last-child]:opacity-30",
          )}
          onEscapeKeyDown={(event) => {
            if (voiceEnrollmentMutation.isPending) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (voiceEnrollmentMutation.isPending) event.preventDefault();
          }}
        >
          <DialogHeader>
            <DialogTitle>{t("Teach Scriber a voice")}</DialogTitle>
            <DialogDescription>
              {t("Record one short sample so Scriber can show this person's name in future meeting transcripts.")}
            </DialogDescription>
          </DialogHeader>

          {voiceEnrollmentStage === "success" && voiceEnrollmentResult ? (
            <div className="space-y-4" aria-live="polite">
              <div className="flex items-start gap-3 rounded-lg border border-emerald-500/35 bg-emerald-50 px-3 py-3 dark:bg-emerald-950/25">
                <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-emerald-600 text-white dark:bg-emerald-500 dark:text-slate-950">
                  <Check className="h-5 w-5" aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-emerald-950 dark:text-emerald-100">{voiceEnrollmentResult.profile.displayName} {t("is ready")}</p>
                  <p className="mt-1 text-xs leading-5 text-emerald-900/80 dark:text-emerald-100/80">
                    {t("Scriber can now match this voice in future meetings. You can rename or delete it from the list at any time.")}
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-2.5 rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-5 text-slate-600 dark:bg-slate-900/60 dark:text-slate-300">
                <Shield className="mt-0.5 h-4 w-4 shrink-0 text-blue-600 dark:text-blue-300" aria-hidden="true" />
                <p>{t("The recording was not saved or uploaded. Only the local voice profile remains on this device.")}</p>
              </div>
              <div className="flex justify-end">
                <Button type="button" onClick={() => handleVoiceEnrollmentOpenChange(false)}>{t("Done")}</Button>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="grid gap-2">
                <Label htmlFor="voice-enrollment-name">{t("Person's name")}</Label>
                <Input
                  id="voice-enrollment-name"
                  autoFocus
                  maxLength={120}
                  value={voiceEnrollmentName}
                  disabled={voiceEnrollmentMutation.isPending}
                  onChange={(event) => setVoiceEnrollmentName(event.target.value)}
                  placeholder={t("For example, Alex")}
                  aria-describedby="voice-enrollment-name-help"
                />
                <p id="voice-enrollment-name-help" className="text-[11px] leading-4 text-muted-foreground">{t("This name appears beside matching transcript segments.")}</p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="voice-enrollment-microphone">{t("Microphone")}</Label>
                <Select value={voiceEnrollmentDevice} disabled={voiceEnrollmentMutation.isPending} onValueChange={setVoiceEnrollmentDevice}>
                  <SelectTrigger id="voice-enrollment-microphone" className="w-full" aria-describedby="voice-enrollment-microphone-help">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={DEFAULT_VOICE_ENROLLMENT_DEVICE}>{t("Windows default microphone")}</SelectItem>
                    {voiceEnrollmentDevicesQuery.data?.capture.map((endpoint) => (
                      <SelectItem key={endpoint.endpointIdHash} value={endpoint.endpointIdHash}>
                        {endpoint.friendlyName}{endpoint.isDefault ? t(" (currently default)") : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div id="voice-enrollment-microphone-help" className="min-h-4 text-[11px] leading-4 text-muted-foreground">
                  {voiceEnrollmentDevicesQuery.isLoading ? (
                    <span className="inline-block h-3 w-44 animate-pulse rounded bg-slate-200 motion-reduce:animate-none dark:bg-slate-800" aria-label={t("Looking for microphones")} />
                  ) : voiceEnrollmentDevicesQuery.isError ? (
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-amber-700 dark:text-amber-300">
                      {t("Microphone choices could not be loaded. Windows default can still be used.")}
                      <button type="button" className="font-semibold underline underline-offset-2" onClick={() => void voiceEnrollmentDevicesQuery.refetch()}>{t("Try again")}</button>
                    </span>
                  ) : voiceEnrollmentDevicesQuery.data?.available ? (
                    t(
                      voiceEnrollmentDevicesQuery.data.capture.length === 1
                        ? "{{count}} microphone choice found."
                        : "{{count}} microphone choices found.",
                      { count: formatNumber(voiceEnrollmentDevicesQuery.data.capture.length) },
                    )
                  ) : (
                    t("Windows default will be used.")
                  )}
                </div>
              </div>

              {voiceEnrollmentMutation.isPending && (
                <div className="rounded-lg border border-blue-500/30 bg-blue-50 px-3 py-3 dark:bg-blue-950/25" aria-live="polite">
                  <div className="flex items-start gap-3">
                    <Mic className="mt-0.5 h-5 w-5 shrink-0 text-blue-700 dark:text-blue-300" aria-hidden="true" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-semibold text-blue-950 dark:text-blue-100">
                        {voiceEnrollmentStage === "preparing"
                          ? t("Starting the microphone")
                          : voiceEnrollmentStage === "processing"
                            ? t("Creating the voice profile")
                            : t("Listening to {{name}}", { name: voiceEnrollmentName.trim() })}
                      </p>
                      <p className="mt-1 text-xs leading-5 text-blue-900/80 dark:text-blue-100/80">
                        {voiceEnrollmentStage === "processing"
                          ? t("Scriber is finishing the sample on this device. Keep the app open.")
                          : t("Speak naturally in a quiet room until the recording finishes. Keep Scriber open.")}
                      </p>
                      <Progress value={voiceEnrollmentProgress} className="mt-3 h-1.5" aria-label={t("Voice sample progress")} />
                    </div>
                  </div>
                </div>
              )}

              {voiceEnrollmentStage === "error" && (
                <div className="flex items-start gap-2.5 rounded-lg border border-amber-500/35 bg-amber-50 px-3 py-2.5 text-xs leading-5 text-amber-900 dark:bg-amber-950/30 dark:text-amber-100" role="alert">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                  <div>
                    <p className="font-semibold">{t("The voice sample was not saved.")}</p>
                    <p className="mt-0.5">{localizedSettingsError(
                      voiceEnrollmentMutation.error,
                      "Check the microphone and try again.",
                      locale,
                      t,
                    )}</p>
                  </div>
                </div>
              )}

              {!voiceEnrollmentMutation.isPending && (
                <div className="flex items-start gap-2.5 rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-5 text-slate-600 dark:bg-slate-900/60 dark:text-slate-300">
                  <Shield className="mt-0.5 h-4 w-4 shrink-0 text-blue-600 dark:text-blue-300" aria-hidden="true" />
                  <p>{t("Scriber listens for about 8 seconds. The recording is not saved or uploaded. The local voice profile remains until you delete it.")}</p>
                </div>
              )}

              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button type="button" variant="ghost" disabled={voiceEnrollmentMutation.isPending} onClick={() => handleVoiceEnrollmentOpenChange(false)}>{t("Cancel")}</Button>
                <Button
                  type="button"
                  className="whitespace-nowrap active:scale-[0.98]"
                  disabled={!voiceEnrollmentName.trim() || voiceEnrollmentMutation.isPending}
                  onClick={() => voiceEnrollmentMutation.mutate()}
                >
                  {voiceEnrollmentMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Mic className="mr-2 h-4 w-4" />}
                  {voiceEnrollmentMutation.isPending
                    ? voiceEnrollmentStage === "processing" ? t("Saving voice") : t("Recording voice")
                    : voiceEnrollmentStage === "error" ? t("Try sample again") : t("Record 8-second sample")}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
      <AlertDialog open={Boolean(speakerProfilePendingDelete)} onOpenChange={(open) => { if (!open) setSpeakerProfilePendingDelete(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Delete this saved speaker?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {speakerProfilePendingDelete?.name || t("This speaker")} {t("will no longer be recognized automatically in future meetings. Existing transcripts stay intact.")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={speakerProfileMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (speakerProfilePendingDelete) speakerProfileMutation.mutate({ action: "delete", id: speakerProfilePendingDelete.id });
              }}
            >
              {t("Delete speaker")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog open={outlookDisconnectOpen} onOpenChange={(open) => { if (!outlookMutation.isPending) setOutlookDisconnectOpen(open); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Disconnect Outlook?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("Scriber will remove the protected Microsoft sign-in and its locally synchronized calendar entries. Existing meetings, transcripts, and exports stay available. You can connect this or another Microsoft account again later.")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {outlookQuery.data?.account && (
            <div className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2.5 text-sm">
              <p className="font-medium">{outlookQuery.data.account.name || outlookQuery.data.account.address}</p>
              <p className="mt-0.5 text-xs text-muted-foreground">{outlookQuery.data.account.address}</p>
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={outlookMutation.isPending}>{t("Keep connected")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={outlookMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                outlookMutation.mutate("disconnect");
              }}
            >
              {outlookMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("Disconnect Outlook")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog open={voiceLibraryDeleteOpen} onOpenChange={(open) => { if (!voiceLibraryDeletePending) setVoiceLibraryDeleteOpen(open); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Delete all saved voice data?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("This removes every saved speaker and the local voice-recognition download, then turns off future recognition. Existing meetings and transcripts remain available.")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={voiceLibraryDeletePending}>{t("Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={voiceLibraryDeletePending}
              onClick={(event) => { event.preventDefault(); void handleDeleteVoiceprintLibrary(); }}
            >
              {voiceLibraryDeletePending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("Delete voice data")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
