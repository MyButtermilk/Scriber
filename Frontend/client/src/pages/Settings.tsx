import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
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
  type LucideIcon,
} from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useState, useEffect, useCallback, useRef, type ReactNode } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogDescription } from "@/components/ui/dialog";
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
import type {
  ApiMessageResponse,
  LocalModelActionResponse,
  MicrophoneDevice,
  MicrophonesResponse,
  NemoModelInfo,
  NemoModelsResponse,
  OnnxModelInfo,
  OnnxModelsResponse,
  SettingsResponse,
  SettingsUpdatePayload,
} from "@/lib/api-types";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { QueryErrorState } from "@/components/ui/query-error-state";
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

const LANGUAGE_OPTIONS = [
  { value: "auto", label: "Auto-detect" },
  { value: "de", label: "German" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "it", label: "Italian" },
] as const;

const TRANSCRIPTION_MODEL_OPTIONS = [
  { value: "onnx_local", label: "Local (ONNX) - No API Key" },
  { value: "nemo_local", label: "Local (NeMo) - Primeline" },
  { value: "soniox-realtime", label: "Soniox STT Streaming" },
  { value: "soniox-async", label: "Soniox Async" },
  { value: "mistral-realtime", label: "Mistral Live Segmented (Voxtral)" },
  { value: "mistral-async", label: "Mistral Async (Voxtral V2)" },
  { value: "smallest-realtime", label: "Smallest AI STT Streaming (Pulse)" },
  { value: "smallest-async", label: "Smallest AI Async (Pulse)" },
  { value: "assemblyai-realtime", label: "AssemblyAI Universal-3.5 Pro Realtime" },
  { value: "assemblyai", label: "AssemblyAI Universal-3.5 Pro Async" },
  { value: "deepgram", label: "Deepgram STT Streaming" },
  { value: "deepgram-async", label: "Deepgram Async" },
  { value: "openai", label: "OpenAI Live Segmented" },
  { value: "openai-async", label: "OpenAI Async" },
  { value: "azure_mai", label: "Microsoft MAI Transcribe" },
  { value: "gladia", label: "Gladia STT Streaming" },
  { value: "gladia-async", label: "Gladia Async" },
  { value: "groq", label: "Groq Live Segmented" },
  { value: "speechmatics", label: "Speechmatics STT Streaming" },
  { value: "speechmatics-async", label: "Speechmatics Batch" },
  { value: "elevenlabs", label: "ElevenLabs Live Segmented" },
  { value: "google", label: "Google Cloud STT Streaming" },
] as const;

const DEFAULT_SUMMARIZATION_MODEL = "gemini-flash-latest";
const DEFAULT_POST_PROCESSING_MODEL = "openai/gpt-oss-120b";
const DEFAULT_POST_PROCESSING_PROMPT = `Du bist Scribers präziser Live-Diktat-Editor.

Aufgabe: Glätte das folgende Speech-to-Text-Transkript sprachlich, typografisch und strukturell, ohne Inhalt zu verändern, zu kürzen, zu interpretieren oder neue Informationen hinzuzufügen.

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
  group: "gemini" | "openrouter" | "openai";
  icon: ProviderIconKey;
};

const SUMMARIZATION_MODEL_OPTIONS: readonly SummarizationModelOption[] = [
  { value: "gemini-flash-latest", label: "Gemini Flash Latest", detail: "Default fast summary model", group: "gemini", icon: "gemini" },
  { value: "gemini-3.5-flash", label: "Gemini 3.5 Flash", detail: "Fast Google summary model", group: "gemini", icon: "gemini" },
  { value: "gemini-3-flash-preview", label: "Gemini 3.0 Flash Preview", detail: "Preview Flash model", group: "gemini", icon: "gemini" },
  { value: "gemini-3.1-flash-lite-preview", label: "Gemini 3.1 Flash Lite", detail: "Compact preview model", group: "gemini", icon: "gemini" },
  { value: "gemini-3-pro-preview", label: "Gemini 3 Pro", detail: "Higher reasoning preview", group: "gemini", icon: "gemini" },
  { value: "minimax/minimax-m3:nitro", label: "MiniMax M3 Nitro", detail: "OpenRouter Nitro route", group: "openrouter", icon: "openrouter" },
  { value: "z-ai/glm-5.2:nitro", label: "GLM 5.2 Nitro", detail: "OpenRouter Nitro route", group: "openrouter", icon: "openrouter" },
  { value: "gpt-5.2", label: "OpenAI GPT 5.2", detail: "OpenAI summary model", group: "openai", icon: "openai" },
  { value: "gpt-5-mini", label: "OpenAI GPT 5 Mini", detail: "Lower latency OpenAI model", group: "openai", icon: "openai" },
  { value: "gpt-5-nano", label: "OpenAI GPT 5 Nano", detail: "Smallest OpenAI model", group: "openai", icon: "openai" },
] as const;

const POST_PROCESSING_MODEL_OPTIONS: readonly SummarizationModelOption[] = [
  { value: "openai/gpt-oss-120b", label: "GPT-OSS 120B Baseten", detail: "Default OpenRouter route: Baseten, Cerebras fallback", group: "openrouter", icon: "openai" },
  { value: "google/gemini-2.5-flash-lite:nitro", label: "Gemini 2.5 Flash Lite Nitro", detail: "Low-cost OpenRouter route", group: "openrouter", icon: "openrouter" },
  { value: "gpt-5-nano", label: "OpenAI GPT 5 Nano", detail: "Low-cost cleanup model", group: "openai", icon: "openai" },
  { value: "gemini-3.1-flash-lite-preview", label: "Gemini 3.1 Flash Lite", detail: "Compact Gemini cleanup", group: "gemini", icon: "gemini" },
  { value: "minimax/minimax-m3:nitro", label: "MiniMax M3 Nitro", detail: "Fast OpenRouter Nitro route", group: "openrouter", icon: "openrouter" },
  { value: "gemini-3.5-flash", label: "Gemini 3.5 Flash", detail: "Fast Gemini fallback", group: "gemini", icon: "gemini" },
  { value: "gpt-5-mini", label: "OpenAI GPT 5 Mini", detail: "Fast cleanup with more headroom", group: "openai", icon: "openai" },
  { value: "z-ai/glm-5.2:nitro", label: "GLM 5.2 Nitro", detail: "OpenRouter fallback route", group: "openrouter", icon: "openrouter" },
  { value: "gemini-flash-latest", label: "Gemini Flash Latest", detail: "Shared summary default", group: "gemini", icon: "gemini" },
  { value: "gemini-3-flash-preview", label: "Gemini 3.0 Flash Preview", detail: "Preview Flash model", group: "gemini", icon: "gemini" },
  { value: "gpt-5.2", label: "OpenAI GPT 5.2", detail: "Higher-cost fallback", group: "openai", icon: "openai" },
  { value: "gemini-3-pro-preview", label: "Gemini 3 Pro", detail: "Higher-cost fallback", group: "gemini", icon: "gemini" },
] as const;

const API_KEY_HELP_LINKS = {
  openai: { href: "https://platform.openai.com/api-keys", label: "OpenAI keys" },
  deepgram: { href: "https://console.deepgram.com/", label: "Deepgram console" },
  assemblyai: { href: "https://www.assemblyai.com/dashboard", label: "AssemblyAI dashboard" },
  gemini: { href: "https://aistudio.google.com/app/apikey", label: "Google AI Studio" },
  openrouter: { href: "https://openrouter.ai/settings/keys", label: "OpenRouter keys" },
  youtube: { href: "https://console.cloud.google.com/apis/credentials", label: "Google Cloud credentials" },
  soniox: { href: "https://console.soniox.com/", label: "Soniox console" },
  smallest: { href: "https://app.smallest.ai/", label: "Smallest AI console" },
  mistral: { href: "https://console.mistral.ai/api-keys", label: "Mistral API keys" },
  fal: { href: "https://fal.ai/dashboard/keys", label: "fal.ai keys" },
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

function ApiKeyLink({ helpKey, children = "Get key" }: { helpKey: ApiKeyHelpKey; children?: ReactNode }) {
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
      title={help.label}
    >
      {children}
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
  deepgram: "/provider-icons/deepgram.svg",
  elevenlabs: "/provider-icons/elevenlabs.svg",
  fal: "/provider-icons/fal.svg",
  gemini: "/provider-icons/gemini.svg",
  gladia: "/provider-icons/gladia.svg",
  googlecloud: "/provider-icons/googlecloud.svg",
  groq: "/provider-icons/groq.svg",
  mistral: "/provider-icons/mistral.svg",
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
  group: "cloud_streaming" | "cloud_segmented" | "cloud_async" | "local";
  icon?: ProviderIconKey;
}

const PROVIDER_MODEL_OPTIONS: ProviderModelOption[] = [
  { value: "onnx_local", label: "Local ONNX", detail: "Runs fully on this device", group: "local" },
  { value: "nemo_local", label: "Local NeMo", detail: "Uses local NeMo toolkit models", group: "local" },
  { value: "soniox-realtime", label: "Soniox", detail: "stt-rt-v5 WebSocket stream", group: "cloud_streaming", icon: "soniox" },
  { value: "smallest-realtime", label: "Smallest AI", detail: "Pulse realtime WebSocket", group: "cloud_streaming", icon: "smallest" },
  { value: "deepgram", label: "Deepgram", detail: "Listen WebSocket streaming", group: "cloud_streaming", icon: "deepgram" },
  { value: "gladia", label: "Gladia", detail: "Live v2 WebSocket; files use pre-recorded API", group: "cloud_streaming", icon: "gladia" },
  { value: "google", label: "Google Cloud", detail: "Speech-to-Text streaming", group: "cloud_streaming", icon: "googlecloud" },
  { value: "speechmatics", label: "Speechmatics", detail: "Realtime WebSocket API", group: "cloud_streaming", icon: "speechmatics" },
  { value: "assemblyai-realtime", label: "AssemblyAI", detail: "Universal-3.5 Pro realtime", group: "cloud_streaming", icon: "assemblyai" },
  { value: "mistral-realtime", label: "Mistral", detail: "Voxtral API per speech segment", group: "cloud_segmented", icon: "mistral" },
  { value: "openai", label: "OpenAI", detail: "Transcription API per speech segment", group: "cloud_segmented", icon: "openai" },
  { value: "groq", label: "Groq", detail: "Whisper API per speech segment", group: "cloud_segmented", icon: "groq" },
  { value: "elevenlabs", label: "ElevenLabs", detail: "File API per speech segment", group: "cloud_segmented", icon: "elevenlabs" },
  { value: "soniox-async", label: "Soniox", detail: "Direct file and final transcript", group: "cloud_async", icon: "soniox" },
  { value: "mistral-async", label: "Mistral", detail: "Voxtral batch transcription", group: "cloud_async", icon: "mistral" },
  { value: "smallest-async", label: "Smallest AI", detail: "Pulse pre-recorded STT", group: "cloud_async", icon: "smallest" },
  { value: "deepgram-async", label: "Deepgram", detail: "Pre-recorded Listen API", group: "cloud_async", icon: "deepgram" },
  { value: "gladia-async", label: "Gladia", detail: "Pre-recorded v2 API", group: "cloud_async", icon: "gladia" },
  { value: "openai-async", label: "OpenAI", detail: "Audio transcription API", group: "cloud_async", icon: "openai" },
  { value: "speechmatics-async", label: "Speechmatics", detail: "Batch transcription API", group: "cloud_async", icon: "speechmatics" },
  { value: "assemblyai", label: "AssemblyAI", detail: "Universal-3.5 Pro async", group: "cloud_async", icon: "assemblyai" },
  { value: "azure_mai", label: "Microsoft MAI", detail: "Azure MAI batch STT", group: "cloud_async", icon: "azure" },
];

function ProviderIcon({
  icon,
  label,
  className,
}: {
  icon?: ProviderIconKey;
  label: string;
  className?: string;
}) {
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
        alt={`${label} logo`}
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
}: {
  title: string;
  description: string;
  icon?: LucideIcon;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("settings-section min-w-0 border-t border-slate-300/70 pb-3 pt-4 dark:border-slate-800", className)}>
      <div className="mb-3 flex min-w-0 items-start gap-2.5">
        {Icon ? (
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white/70 text-slate-500 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.07)] dark:bg-slate-900/70 dark:text-slate-400">
            <Icon className="h-3.5 w-3.5" aria-hidden="true" />
          </span>
        ) : null}
        <div className="min-w-0 flex-1">
          <h2 className="text-[14px] font-semibold leading-5 text-slate-950 dark:text-slate-100">{title}</h2>
          <p className="mt-0.5 max-w-[46ch] text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400">
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
    <div className={cn("grid gap-2 py-2 sm:grid-cols-[minmax(0,1fr)_minmax(150px,220px)] sm:items-center", className)}>
      <div className="min-w-0">
        <Label className="text-[12px] font-semibold leading-4 text-slate-950 dark:text-slate-100">{label}</Label>
        {description ? (
          <p className="mt-0.5 text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400">{description}</p>
        ) : null}
      </div>
      <div className="min-w-0 sm:justify-self-end">{children}</div>
    </div>
  );
}

function ProviderChoice({
  option,
  selected,
  onSelect,
  disabled,
  disabledReason,
}: {
  option: ProviderModelOption;
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={onSelect}
      title={disabledReason}
      className={cn(
        "group flex min-h-[35px] w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors",
        !disabled && "active:translate-y-px",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white",
        disabled
          ? "cursor-not-allowed text-slate-400 opacity-65 dark:text-slate-600"
          : selected
          ? "bg-blue-50 text-blue-950 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.18)] dark:bg-blue-950/35 dark:text-blue-100"
          : "text-slate-800 hover:bg-slate-100/80 dark:text-slate-200 dark:hover:bg-slate-900",
      )}
    >
      {option.icon ? (
        <ProviderIcon icon={option.icon} label={option.label} />
      ) : (
        <span className="h-7 w-7 shrink-0" aria-hidden="true" />
      )}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[11.5px] font-semibold leading-[15px]">{option.label}</span>
        <span
          className={cn(
            "block truncate text-[10px] leading-3",
            disabled ? "text-amber-600 dark:text-amber-400" : "text-slate-500 dark:text-slate-400",
          )}
        >
          {disabledReason || option.detail}
        </span>
      </span>
      <span
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          disabled
            ? "border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30"
            : selected
              ? "border-blue-600 bg-blue-600"
              : "border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-950",
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
}: {
  option: SummarizationModelOption;
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={onSelect}
      title={disabledReason}
      className={cn(
        "group flex min-h-[35px] w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left outline-none transition-colors",
        !disabled && "active:translate-y-px",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white",
        disabled
          ? "cursor-not-allowed text-slate-400 opacity-65 dark:text-slate-600"
          : selected
          ? "bg-blue-50 text-blue-950 shadow-[inset_0_0_0_1px_rgba(37,99,235,0.18)] dark:bg-blue-950/35 dark:text-blue-100"
          : "text-slate-800 hover:bg-slate-100/80 dark:text-slate-200 dark:hover:bg-slate-900",
      )}
    >
      <ProviderIcon icon={option.icon} label={option.label} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[11.5px] font-semibold leading-[15px]">{option.label}</span>
        <span
          className={cn(
            "block truncate text-[10px] leading-3",
            disabled ? "text-amber-600 dark:text-amber-400" : "text-slate-500 dark:text-slate-400",
          )}
        >
          {disabledReason || option.detail}
        </span>
      </span>
      <span
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          disabled
            ? "border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30"
            : selected
              ? "border-blue-600 bg-blue-600"
              : "border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-950",
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
      <Label className="text-[10.5px] font-semibold text-slate-600 dark:text-slate-300">{label}</Label>
      {children}
      {detail ? <p className="text-[10.5px] leading-4 text-slate-500 dark:text-slate-400">{detail}</p> : null}
    </div>
  );
}

function maskedSecret(value: string): string {
  return hasValue(value) ? "************" : "Not set";
}

function ApiCredentialRow({
  provider,
  icon,
  value,
  onValueChange,
  show,
  onShowChange,
  helpKey,
  saved,
  onSave,
  note,
  placeholder,
  inputType = "password",
  children,
}: {
  provider: string;
  icon?: ProviderIconKey;
  value: string;
  onValueChange: (value: string) => void;
  show?: boolean;
  onShowChange?: (value: boolean) => void;
  helpKey: ApiKeyHelpKey;
  saved: boolean;
  onSave: () => void;
  note?: string;
  placeholder?: string;
  inputType?: "password" | "text";
  children?: ReactNode;
}) {
  const hasCredential = hasValue(value);
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className="group grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5 rounded-lg px-2 py-2.5 text-left outline-none transition-colors hover:bg-slate-100/80 focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:hover:bg-slate-900"
        >
          <span className="flex min-w-0 items-center gap-2">
            <ProviderIcon icon={icon} label={provider} className="h-5.5 w-5.5 rounded-[7px] p-1" />
            <span className="min-w-0">
              <span className="block truncate text-[11.5px] font-semibold leading-[15px] text-slate-950 dark:text-slate-100">
                {provider}
              </span>
              <span className={cn("block truncate font-mono text-[10px] leading-3", hasCredential ? "text-slate-500" : "text-slate-400")}>
                {maskedSecret(value)}
              </span>
            </span>
          </span>
          <span className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-blue-600 group-hover:text-blue-700 dark:text-blue-400">
            Open
            <ArrowRight className="h-3 w-3" aria-hidden="true" />
          </span>
        </button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{provider}</DialogTitle>
          <DialogDescription>
            {note || "Add or update the credential for this provider."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <FieldShell label="Credential">
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Input
                  type={inputType === "text" ? "text" : show ? "text" : "password"}
                  value={value}
                  onChange={(event) => onValueChange(event.target.value)}
                  placeholder={placeholder || `Enter ${provider} credential`}
                  className="pr-10 font-mono text-sm"
                />
                {typeof show === "boolean" && onShowChange ? (
                  <button
                    type="button"
                    onClick={() => onShowChange(!show)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 transition-colors hover:text-slate-950 dark:hover:text-slate-100"
                    aria-label={show ? "Hide credential" : "Show credential"}
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
                {saved ? "Saved" : "Save"}
              </Button>
            </div>
          </FieldShell>
          {children}
          <ApiKeyLink helpKey={helpKey}>Open provider page</ApiKeyLink>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function Settings() {
  const [openAIKey, setOpenAIKey] = useState("");
  const [deepgramKey, setDeepgramKey] = useState("");
  const [assemblyAIKey, setAssemblyAIKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [openRouterKey, setOpenRouterKey] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [sonioxKey, setSonioxKey] = useState("");
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
  const [summarizationPrompt, setSummarizationPrompt] = useState("");
  const [postProcessingPrompt, setPostProcessingPrompt] = useState(DEFAULT_POST_PROCESSING_PROMPT);

  const [showOpenAIKey, setShowOpenAIKey] = useState(false);
  const [showDeepgramKey, setShowDeepgramKey] = useState(false);
  const [showAssemblyAIKey, setShowAssemblyAIKey] = useState(false);
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [showOpenRouterKey, setShowOpenRouterKey] = useState(false);
  const [showYoutubeKey, setShowYoutubeKey] = useState(false);
  const [showSonioxKey, setShowSonioxKey] = useState(false);
  const [showMistralKey, setShowMistralKey] = useState(false);
  const [showSmallestKey, setShowSmallestKey] = useState(false);
  const [showElevenLabsKey, setShowElevenLabsKey] = useState(false);
  const [showAzureMaiKey, setShowAzureMaiKey] = useState(false);
  const [showGladiaKey, setShowGladiaKey] = useState(false);
  const [showGroqKey, setShowGroqKey] = useState(false);
  const [showSpeechmaticsKey, setShowSpeechmaticsKey] = useState(false);

  const [hotkey, setHotkey] = useState("Ctrl + Shift + S");
  const [postProcessingHotkey, setPostProcessingHotkey] = useState("Ctrl + Shift + P");
  const [recordingMode, setRecordingMode] = useState("press_hold");
  const [isRecordingHotkey, setIsRecordingHotkey] = useState(false);
  const [isRecordingPostProcessingHotkey, setIsRecordingPostProcessingHotkey] = useState(false);
  const hotkeyCaptureRef = useRef<HTMLDivElement | null>(null);
  const postProcessingHotkeyCaptureRef = useRef<HTMLDivElement | null>(null);
  const { toast } = useToast();
  const [savedKeys, setSavedKeys] = useState<Record<string, boolean>>({});

  const [inputDevices, setInputDevices] = useState<MicrophoneDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("default");
  const [transcriptionModel, setTranscriptionModel] = useState("soniox-realtime");
  const [summarizationModel, setSummarizationModel] = useState(DEFAULT_SUMMARIZATION_MODEL);
  const [postProcessingModel, setPostProcessingModel] = useState(DEFAULT_POST_PROCESSING_MODEL);
  const [autoSummarize, setAutoSummarize] = useState(false);
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
  const [favoriteMic, setFavoriteMic] = useState("");
  const [isMicDropdownOpen, setIsMicDropdownOpen] = useState(false);
  const [isLanguageDropdownOpen, setIsLanguageDropdownOpen] = useState(false);
  const [isTranscriptionModelDropdownOpen, setIsTranscriptionModelDropdownOpen] = useState(false);

  const [onnxAvailable, setOnnxAvailable] = useState<boolean | null>(null);
  const [onnxMessage, setOnnxMessage] = useState("");
  const [onnxModels, setOnnxModels] = useState<OnnxModelInfo[]>([]);
  const [onnxModel, setOnnxModel] = useState("");
  const [onnxQuantization, setOnnxQuantization] = useState("int8");

  const [nemoAvailable, setNemoAvailable] = useState<boolean | null>(null);
  const [nemoMessage, setNemoMessage] = useState("");
  const [nemoModels, setNemoModels] = useState<NemoModelInfo[]>([]);
  const [nemoModel, setNemoModel] = useState("");

  useEffect(() => {
    return subscribeDesktopUpdateStatus(setDesktopUpdate);
  }, []);

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

  const savedCredentialAvailable = (provider: string, value: string, extraValue?: string) =>
    savedKeys[provider] === true && hasValue(value) && (extraValue === undefined || hasValue(extraValue));

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
      case "Soniox":
        return savedCredentialAvailable("Soniox", sonioxKey);
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
    requirement && !isCredentialReady(requirement) ? `${requirement.label} required` : undefined;

  const requiredCredentialForTranscriptionModel = (model: string): CredentialRequirement | null => {
    switch (model) {
      case "soniox-realtime":
      case "soniox-async":
        return { provider: "Soniox", label: "Soniox API key", helpKey: "soniox" };
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
        return { provider: "ElevenLabs", label: "fal.ai API key for ElevenLabs", helpKey: "fal" };
      case "google":
        return { provider: "Google Cloud", label: "Google Cloud credentials JSON path", helpKey: "googleCloud" };
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
  const missingActiveCredentialRequirements = uniqueCredentialRequirements([
    missingSelectedCredentialRequirement,
    missingSummarizationCredentialRequirement,
    missingPostProcessingCredentialRequirement,
  ]);

  const hasAnyManagedCloudSttCredential = [
    sonioxKey,
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
    googleApplicationCredentials,
  ].some(hasValue);

  const markCredentialChanged = (provider: string, setter: (value: string) => void) => (value: string) => {
    setter(value);
    setSavedKeys((prev) => ({ ...prev, [provider]: false }));
  };

  const loadOnnxModels = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/onnx/models"), { credentials: "include" });
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
      setOnnxMessage(String(e?.message || e));
    }
  }, []);

  const loadNemoModels = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/nemo/models"), { credentials: "include" });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data = (await res.json()) as NemoModelsResponse;
      const available = data.available !== false;
      setNemoAvailable(available);
      setNemoMessage(data.message || "");
      const models = data.models || [];
      setNemoModels(models);

      const current = data.currentModel || "";
      const selected = models.find((m) => m.id === current) ? current : (models[0]?.id || "");
      setNemoModel(selected);
    } catch (e: any) {
      setNemoAvailable(false);
      setNemoMessage(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    const serviceToModel = (service: string, sonioxMode: string) => {
      if (service === "soniox" || service === "soniox_async") {
        return sonioxMode === "async" || service === "soniox_async"
          ? "soniox-async"
          : "soniox-realtime";
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
        setHotkey(settings.hotkey || settings.hotkeyRaw || "");
        setPostProcessingHotkey(settings.postProcessingHotkey || settings.postProcessingHotkeyRaw || "Ctrl + Shift + P");
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
        setPostProcessingEnabled(settings.postProcessingEnabled !== false);
        setPostProcessingPrompt(settings.postProcessingPrompt || DEFAULT_POST_PROCESSING_PROMPT);
        const loadedVisualizerBarCount = normalizeVisualizerBarCount(settings.visualizerBarCount);
        setVisualizerBarCount(loadedVisualizerBarCount);
        setSavedVisualizerBarCount(loadedVisualizerBarCount);
        setMicAlwaysOn(settings.micAlwaysOn === true);
        setFavoriteMic(settings.favoriteMic || "");
        setNemoModel(settings.nemoModel || "");

        setSonioxKey(keys.soniox || "");
        setMistralKey(keys.mistral || "");
        setSmallestKey(keys.smallest || "");
        setAssemblyAIKey(keys.assemblyai || "");
        setDeepgramKey(keys.deepgram || "");
        setOpenAIKey(keys.openai || "");
        setGeminiKey(keys.googleApiKey || "");
        setOpenRouterKey(keys.openrouter || "");
        setYoutubeKey(keys.youtubeApiKey || "");
        setElevenLabsKey(keys.elevenlabs || "");
        setAzureMaiKey(keys.azureMaiSpeechKey || "");
        setAzureMaiRegion(keys.azureMaiRegion || "northeurope");
        setAzureMaiModel(keys.azureMaiModel || "mai-transcribe-1.5");
        setGladiaKey(keys.gladia || "");
        setGroqKey(keys.groq || "");
        setSpeechmaticsKey(keys.speechmatics || "");
        setGoogleApplicationCredentials(keys.googleApplicationCredentials || "");
        setSavedKeys({
          OpenAI: hasValue(keys.openai),
          Gemini: hasValue(keys.googleApiKey),
          OpenRouter: hasValue(keys.openrouter),
          YouTube: hasValue(keys.youtubeApiKey),
          Soniox: hasValue(keys.soniox),
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
        });

        let microphonePayload = mics;
        if (!Array.isArray(microphonePayload.devices)) {
          const micsRes = await fetch(apiUrl("/api/microphones"), { credentials: "include" });
          if (micsRes.ok) {
            microphonePayload = (await micsRes.json()) as MicrophonesResponse;
          }
        }
        setInputDevices(microphonePayload.devices || []);

        // Show page immediately - don't wait for model info
        setSettingsLoaded(true);
      } catch (e: any) {
        setSettingsLoaded(true); // Still mark as loaded even on error
        setSettingsError(String(e?.message || e));
        toast({
          title: "Failed to load settings",
          description: String(e?.message || e),
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
    if (transcriptionModel === "onnx_local" && onnxAvailable === null) {
      loadOnnxModels();
    }
    if (transcriptionModel === "nemo_local" && nemoAvailable === null) {
      loadNemoModels();
    }
  }, [transcriptionModel, onnxAvailable, nemoAvailable, loadOnnxModels, loadNemoModels]);

  const updateSettings = async (patch: SettingsUpdatePayload): Promise<SettingsResponse> => {
    const res = await fetch(apiUrl("/api/settings"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    invalidateSettingsBootstrap();
    return (await res.json()) as SettingsResponse;
  };

  const saveCustomVocabulary = useCallback(async (nextValue: string) => {
    if (nextValue === savedCustomVocabularyRef.current) {
      return;
    }

    try {
      await updateSettings({ customVocab: nextValue });
      savedCustomVocabularyRef.current = nextValue;
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
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
      const res = await fetch(apiUrl("/api/microphones"), { credentials: "include" });
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
      if (provider === "YouTube") apiKeys.youtubeApiKey = youtubeKey;
      if (provider === "Soniox") apiKeys.soniox = sonioxKey;
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

      await updateSettings({ apiKeys });

      setSavedKeys((prev) => ({ ...prev, [provider]: true }));
      toast({
        title: "Saved",
        description: `${provider} settings updated.`,
        duration: 2000,
      });
      setTimeout(() => {
        setSavedKeys((prev) => ({ ...prev, [provider]: false }));
      }, 2000);
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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

  const handleMicDeviceChange = async (deviceId: string) => {
    const previousDeviceId = selectedDeviceId;
    setSelectedDeviceId(deviceId);
    try {
      await updateSettings({ micDevice: deviceId });
      return true;
    } catch (e: any) {
      setSelectedDeviceId(previousDeviceId);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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
        title: newFavorite ? "Favorite set" : "Favorite cleared",
        description: newFavorite
          ? "This microphone will be used automatically when available."
          : "No preferred microphone set.",
        duration: 2000,
      });
    } catch (e: any) {
      setFavoriteMic(originalFavorite); // Revert to original value on error
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelChange = async (value: string) => {
    const requirement = requiredCredentialForTranscriptionModel(value);
    if (!isCredentialReady(requirement)) {
      toast({
        title: "API key required",
        description: `${requirement?.label || "API key"} must be saved below before this model can be selected.`,
        duration: 4000,
      });
      return;
    }
    const previousValue = transcriptionModel;
    setTranscriptionModel(value);
    try {
      if (value === "soniox-async") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "async" });
      } else if (value === "soniox-realtime") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "realtime" });
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
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleLanguageChange = async (value: string) => {
    setLanguage(value);
    try {
      await updateSettings({ language: value });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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
        title: "Saved",
        description: "Local model selection updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleOnnxQuantizationChange = async (value: string) => {
    setOnnxQuantization(value);
    try {
      await updateSettings({ onnxQuantization: value });
      await loadOnnxModels();
      toast({
        title: "Saved",
        description: "Quantization updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleOnnxDownload = async (modelId: string) => {
    if (!modelId) return;
    setOnnxModels((prev) =>
      prev.map((m) =>
        m.id === modelId
          ? { ...m, status: "downloading", progress: 0, message: "Starting download..." }
          : m
      )
    );
    try {
      const res = await fetch(apiUrl("/api/onnx/download"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelId, quantization: onnxQuantization }),
        credentials: "include",
      });
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Download failed");
      }
      toast({
        title: "Download finished",
        description: "Model downloaded successfully.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Download failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      await loadOnnxModels();
    }
  };

  const handleOnnxDelete = async (modelId: string) => {
    if (!modelId) return;
    try {
      const res = await fetch(
        apiUrl(`/api/onnx/models/${encodeURIComponent(modelId)}?quantization=${encodeURIComponent(onnxQuantization)}`),
        {
          method: "DELETE",
          credentials: "include",
        }
      );
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Delete failed");
      }
      toast({
        title: "Deleted",
        description: "Model removed from cache.",
        duration: 2000,
      });
      await loadOnnxModels();
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleNemoModelChange = async (value: string) => {
    setNemoModel(value);
    try {
      await updateSettings({ nemoModel: value });
      toast({
        title: "Saved",
        description: "NeMo model updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleNemoDownload = async (modelId: string) => {
    if (!modelId) return;
    setNemoModels((prev) =>
      prev.map((m) =>
        m.id === modelId
          ? { ...m, status: "downloading", progress: 0, message: "Starting download..." }
          : m
      )
    );
    try {
      const res = await fetch(apiUrl("/api/nemo/download"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelId }),
        credentials: "include",
      });
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Download failed");
      }
      toast({
        title: "Download finished",
        description: "Model downloaded successfully.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Download failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      await loadNemoModels();
    }
  };

  const handleNemoDelete = async (modelId: string) => {
    if (!modelId) return;
    try {
      const res = await fetch(
        apiUrl(`/api/nemo/models/${encodeURIComponent(modelId)}`),
        {
          method: "DELETE",
          credentials: "include",
        }
      );
      const data = (await res.json().catch(() => ({}))) as LocalModelActionResponse;
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Delete failed");
      }
      toast({
        title: "Deleted",
        description: "Model removed from cache.",
        duration: 2000,
      });
      await loadNemoModels();
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSummarizationModelChange = async (value: string) => {
    const requirement = requiredCredentialForLanguageModel(value);
    if (!isCredentialReady(requirement)) {
      toast({
        title: "API key required",
        description: `${requirement?.label || "API key"} must be saved below before this model can be selected.`,
        duration: 4000,
      });
      return;
    }
    const previousValue = summarizationModel;
    setSummarizationModel(value);
    try {
      await updateSettings({ summarizationModel: value });
      toast({
        title: "Saved",
        description: "Summarization model updated.",
        duration: 2000,
      });
    } catch (e: any) {
      setSummarizationModel(previousValue);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handlePostProcessingModelChange = async (value: string) => {
    const requirement = requiredCredentialForLanguageModel(value);
    if (!isCredentialReady(requirement)) {
      toast({
        title: "API key required",
        description: `${requirement?.label || "API key"} must be saved below before this model can be selected.`,
        duration: 4000,
      });
      return;
    }
    const previousValue = postProcessingModel;
    setPostProcessingModel(value);
    try {
      await updateSettings({ postProcessingModel: value });
      toast({
        title: "Saved",
        description: "Live post-processing model updated.",
        duration: 2000,
      });
    } catch (e: any) {
      setPostProcessingModel(previousValue);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleAutoSummarizeChange = async (enabled: boolean) => {
    setAutoSummarize(enabled);
    try {
      await updateSettings({ autoSummarize: enabled });
      toast({
        title: "Saved",
        description: enabled ? "Auto-summarize enabled." : "Auto-summarize disabled.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handlePostProcessingEnabledChange = async (enabled: boolean) => {
    setPostProcessingEnabled(enabled);
    try {
      await updateSettings({ postProcessingEnabled: enabled });
      await refreshGlobalHotkey();
      toast({
        title: "Saved",
        description: enabled ? "Live post-processing enabled." : "Live post-processing disabled.",
        duration: 2000,
      });
    } catch (e: any) {
      setPostProcessingEnabled(!enabled);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSaveHotkey = async () => {
    try {
      const updated = await updateSettings({ hotkey });
      setHotkey(updated.hotkey || hotkey);
      await setGlobalHotkeyCaptureActive(false);
      await refreshGlobalHotkey();
      toast({
        title: "Saved",
        description: "Hotkey updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setIsRecordingHotkey(false);
    }
  };

  const handleSavePostProcessingHotkey = async () => {
    try {
      const updated = await updateSettings({ postProcessingHotkey });
      setPostProcessingHotkey(updated.postProcessingHotkey || postProcessingHotkey);
      await setGlobalHotkeyCaptureActive(false);
      await refreshGlobalHotkey();
      toast({
        title: "Saved",
        description: "Post-processing hotkey updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setIsRecordingPostProcessingHotkey(false);
    }
  };

  const handleRecordingModeChange = async (mode: string) => {
    setRecordingMode(mode);
    try {
      await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });
      await refreshGlobalHotkey();
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
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
        title: "Saved",
        description: "Summarization prompt updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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
        title: "Save failed",
        description: String(e?.message || e),
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
        title: "Saved",
        description: enabled ? "Autostart enabled" : "Autostart disabled",
        duration: 2000,
      });
    } catch (e: any) {
      // Revert on error
      setAutostartEnabled(!enabled);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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
        title: status.available ? "Update available" : "Update check finished",
        description: status.message,
        duration: 3500,
      });
    } catch (e: any) {
      const message = String(e?.message || e);
      setDesktopUpdate((prev) => ({
        ...prev,
        phase: "error",
        enabled: false,
        available: false,
        message,
      }));
      toast({
        title: "Update check failed",
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
        title: "Saved",
        description: "Live post-processing prompt updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleResetPostProcessingPrompt = async () => {
    setPostProcessingPrompt(DEFAULT_POST_PROCESSING_PROMPT);
    try {
      await updateSettings({ postProcessingPrompt: DEFAULT_POST_PROCESSING_PROMPT });
      toast({
        title: "Saved",
        description: "Live post-processing prompt reset.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
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
      const message = String(e?.message || e);
      setDesktopUpdate((prev) => ({
        ...prev,
        phase: "error",
        message,
      }));
      toast({
        title: "Update failed",
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
      title: "Saved",
      description: enabled ? "Weekly update checks enabled." : "Automatic update checks disabled.",
      duration: 2500,
    });
  };

  const handleRemindDesktopUpdateLater = () => {
    const status = remindDesktopUpdateLater(desktopUpdate.version);
    setDesktopUpdate(status);
    toast({
      title: "Reminder set",
      description: "Scriber will remind you about this update tomorrow.",
      duration: 3000,
    });
  };

  const handleSkipDesktopUpdateVersion = () => {
    const status = skipDesktopUpdateVersion(desktopUpdate.version);
    setDesktopUpdate(status);
    toast({
      title: "Update skipped",
      description: "This version will not be announced again.",
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
        title: "Saved",
        description: enabled ? "Mic pre-warming enabled" : "Mic pre-warming disabled",
        duration: 2000,
      });
    } catch (e: any) {
      setMicAlwaysOn(!enabled);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
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
          title: "Mikrofon getrennt",
          description: "Das ausgewahlte Mikrofon ist nicht mehr verfugbar. Es wurde auf Default zuruckgestellt.",
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
          title: "Favorite mic restored",
          description: `Favorite microphone '${restoredLabel}' is available again.`,
          duration: 2500,
        });
      }
      return;
    }
    if (msg.type === "onnx_download_progress") {
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
    if (msg.type === "nemo_download_progress") {
      setNemoModels((prev) =>
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
    if (msg.type === "nemo_models_updated") {
      loadNemoModels();
    }
  }, [loadOnnxModels, loadNemoModels, selectedDeviceId, toast]);

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
  const selectedNemoModel = nemoModels.find((m) => m.id === nemoModel) || nemoModels[0];
  const selectedMicDevice = inputDevices.find(
    (device, index) => (device.deviceId || `device-${index}`) === selectedDeviceId
  );
  const selectedMicLabel = inputDevices.length === 0
    ? "Loading devices..."
    : (selectedMicDevice?.label || (selectedDeviceId === "default" ? "Default" : ""));
  const hasSelectedMic = Boolean(selectedMicDevice || selectedDeviceId === "default");
  const selectedLanguage = LANGUAGE_OPTIONS.find((option) => option.value === language) || LANGUAGE_OPTIONS[0];
  const selectedTranscriptionModelOption = TRANSCRIPTION_MODEL_OPTIONS.find((option) => option.value === transcriptionModel);
  const supportedQuantizations = selectedOnnxModel?.supportedQuantizations || ["int8", "fp32"];
  const quantizationSupported = supportedQuantizations.includes(onnxQuantization);
  const formatSize = (sizeMb?: number) => {
    if (!sizeMb) return "";
    if (sizeMb >= 1024) return `${(sizeMb / 1024).toFixed(1)} GB`;
    return `${sizeMb} MB`;
  };
  const getStatusLabel = (status?: string) => {
    if (status === "ready") return "Downloaded";
    if (status === "downloading") return "Downloading";
    if (status === "error") return "Error";
    return "Not downloaded";
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
    if (desktopUpdate.available && desktopUpdate.dismissed) return "Skipped";
    if (desktopUpdate.available && desktopUpdate.deferred) return "Later";
    if (desktopUpdate.available) return "Available";
    if (desktopUpdate.phase === "idle") return "Not checked";
    if (desktopUpdate.enabled) return "Current";
    return "Not configured";
  })();
  const desktopUpdateLastCheckedLabel = desktopUpdate.lastCheckedAt
    ? new Date(desktopUpdate.lastCheckedAt).toLocaleString()
    : "Never";
  const desktopUpdateNextCheckLabel = !desktopUpdate.autoCheckEnabled
    ? "Automatic checks disabled"
    : desktopUpdate.lastCheckedAt && desktopUpdate.nextCheckAt
      ? new Date(desktopUpdate.nextCheckAt).toLocaleString()
      : "When Scriber starts";
  const desktopUpdateAvailableVersionLabel = (() => {
    if (desktopUpdate.version) return desktopUpdate.version;
    if (desktopUpdate.phase === "idle") return "Not checked";
    if (desktopUpdate.phase === "error") return "Check failed";
    if (!desktopUpdate.enabled) return "Not configured";
    return "No newer version";
  })();

  const customVocabularyTermCount = customVocabulary
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean).length;
  const providerGroups = [
    {
      key: "cloud_streaming",
      label: "Cloud streaming",
      description: "True realtime STT streams.",
      items: PROVIDER_MODEL_OPTIONS.filter((option) => option.group === "cloud_streaming"),
    },
    {
      key: "cloud_segmented",
      label: "Cloud live / segmented",
      description: "Live microphone mode with per-segment finalization.",
      items: PROVIDER_MODEL_OPTIONS.filter((option) => option.group === "cloud_segmented"),
    },
    {
      key: "cloud_async",
      label: "Cloud async / batch",
      description: "Final transcript after upload or recording stop.",
      items: PROVIDER_MODEL_OPTIONS.filter((option) => option.group === "cloud_async"),
    },
    {
      key: "local",
      label: "Local",
      description: "Runs on this device.",
      items: PROVIDER_MODEL_OPTIONS.filter((option) => option.group === "local"),
    },
  ];
  const summaryModelGroups = [
    {
      key: "gemini",
      label: "Gemini",
      items: SUMMARIZATION_MODEL_OPTIONS.filter((option) => option.group === "gemini"),
    },
    {
      key: "openrouter",
      label: "OpenRouter",
      items: SUMMARIZATION_MODEL_OPTIONS.filter((option) => option.group === "openrouter"),
    },
    {
      key: "openai",
      label: "OpenAI",
      items: SUMMARIZATION_MODEL_OPTIONS.filter((option) => option.group === "openai"),
    },
  ];
  const compactTranscriptionModelLabel =
    selectedTranscriptionModelOption?.label.replace(" - No API Key", "") || transcriptionModel || "Select provider";

  const customVocabularySettings = (
    <div className="rounded-xl bg-slate-50/90 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <FileText className="h-3.5 w-3.5 text-slate-500 dark:text-slate-400" aria-hidden="true" />
            <p className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100">Custom vocabulary</p>
          </div>
          <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            Names, brands, and domain terms passed to supported STT providers.
          </p>
        </div>
        <Badge variant="secondary">{customVocabularyTermCount} terms</Badge>
      </div>
      <Textarea
        value={customVocabulary}
        onChange={(event) => setCustomVocabulary(event.target.value)}
        onBlur={handleCustomVocabBlur}
        placeholder="Enter terms, one per line..."
        className="min-h-[54px] resize-none bg-white/70 font-mono text-[12px] leading-5 dark:bg-slate-950/60"
      />
    </div>
  );

  const livePostProcessingSettings = (
    <div className="rounded-xl bg-slate-50/90 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100">Live post-processing</p>
          <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
            A separate live-mic shortcut cleans dictation before paste. Files and YouTube stay unchanged.
          </p>
        </div>
        <Switch checked={postProcessingEnabled} onCheckedChange={handlePostProcessingEnabledChange} />
      </div>

      <div className="mt-3 grid gap-3">
        <SettingLine label="Post-processing hotkey" description="Starts Live Mic with cleanup enabled.">
          <Dialog open={isRecordingPostProcessingHotkey} onOpenChange={setIsRecordingPostProcessingHotkey}>
            <DialogTrigger asChild>
              <Button variant="outline" className="h-8 w-[220px] max-w-full justify-start font-mono text-[11px]" disabled={!postProcessingEnabled}>
                <Keyboard className="mr-2 h-4 w-4 text-muted-foreground" />
                {postProcessingHotkey}
              </Button>
            </DialogTrigger>
            <DialogContent className="sm:max-w-[425px]">
              <DialogHeader>
                <DialogTitle>Post-processing hotkey</DialogTitle>
                <DialogDescription>Press the key combination for cleaned live dictation.</DialogDescription>
              </DialogHeader>
              <div
                ref={postProcessingHotkeyCaptureRef}
                className="flex h-32 items-center justify-center rounded-lg border-2 border-dashed bg-secondary/20 outline-none transition-colors focus:border-primary focus:bg-primary/5"
                tabIndex={0}
                aria-label="Post-processing hotkey capture area"
              >
                <p className="text-lg font-medium text-primary">{postProcessingHotkey}</p>
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setIsRecordingPostProcessingHotkey(false)}>Cancel</Button>
                <Button onClick={handleSavePostProcessingHotkey}>Save</Button>
              </div>
            </DialogContent>
          </Dialog>
        </SettingLine>

        <FieldShell
          label="Post-processing model"
          detail="Use a low-cost, low-latency model for simple dictation cleanup."
        >
          <Select value={postProcessingModel} onValueChange={(value) => void handlePostProcessingModelChange(value)}>
            <SelectTrigger className="h-9 bg-white/70 dark:bg-slate-950/40">
              <SelectValue placeholder="Select cleanup model" />
            </SelectTrigger>
            <SelectContent>
              {POST_PROCESSING_MODEL_OPTIONS.map((option) => {
                const disabledReason = missingCredentialReason(requiredCredentialForLanguageModel(option.value));
                return (
                  <SelectItem key={option.value} value={option.value} disabled={Boolean(disabledReason)}>
                    {disabledReason ? `${option.label} - ${disabledReason}` : option.label}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
          {missingPostProcessingCredentialRequirement ? (
            <p className="text-[10.5px] leading-4 text-amber-600 dark:text-amber-400">
              Save the {missingPostProcessingCredentialRequirement.label} below before using the selected cleanup model.
            </p>
          ) : null}
        </FieldShell>

        <FieldShell label="Live cleanup prompt">
          <Textarea
            value={postProcessingPrompt}
            onChange={(event) => setPostProcessingPrompt(event.target.value)}
            onBlur={handlePostProcessingPromptBlur}
            placeholder={DEFAULT_POST_PROCESSING_PROMPT}
            className="min-h-[64px] resize-none bg-white/70 text-sm dark:bg-slate-950/60"
            disabled={!postProcessingEnabled}
          />
          <div className="mt-2 flex items-center justify-between gap-3">
            <p className="text-[11px] leading-4 text-slate-500 dark:text-slate-400">
              Use <span className="font-mono">${"{output}"}</span> where the raw transcript should be inserted.
            </p>
            <Button size="sm" variant="outline" onClick={handleResetPostProcessingPrompt} disabled={!postProcessingEnabled}>
              Reset prompt
            </Button>
          </div>
        </FieldShell>
      </div>
    </div>
  );

  const summarizationPromptSettings = (
    <div className="rounded-xl bg-slate-50/90 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
      <FieldShell
        label="Summarization prompt"
        detail="Used for automatic and manual transcript summaries."
      >
        <Textarea
          value={summarizationPrompt}
          onChange={(event) => setSummarizationPrompt(event.target.value)}
          onBlur={handleSummarizationPromptBlur}
          placeholder="Summarize the key points, decisions, and action items. Keep it concise and structured."
          className="min-h-[60px] resize-none bg-white/70 text-sm dark:bg-slate-950/60"
        />
      </FieldShell>
    </div>
  );

  const onnxLocalModelSettings = (
    <div className="rounded-xl bg-white/70 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-950/40">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold text-slate-950 dark:text-slate-100">ONNX model</p>
          <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">Whisper / Parakeet ONNX runtime</p>
        </div>
        {selectedOnnxModel ? (
          <Badge variant={getStatusVariant(selectedOnnxModel.status)}>{getStatusLabel(selectedOnnxModel.status)}</Badge>
        ) : null}
      </div>

      {onnxAvailable === null ? (
        <p className="text-[12px] text-slate-500">Loading local models...</p>
      ) : onnxAvailable === false ? (
        <p className="text-[12px] leading-4 text-slate-500">{onnxMessage || "onnx-asr is not installed."}</p>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <FieldShell label="Model">
              <Select value={onnxModel} onValueChange={handleOnnxModelChange}>
                <SelectTrigger className="h-9">
                  <SelectValue placeholder="Select local model" />
                </SelectTrigger>
                <SelectContent>
                  {onnxModels.map((model) => (
                    <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FieldShell>
            <FieldShell label="Quantization">
              <Select value={onnxQuantization} onValueChange={handleOnnxQuantizationChange}>
                <SelectTrigger className="h-9">
                  <SelectValue placeholder="Select quantization" />
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
            <p className="text-[11px] leading-4 text-slate-500 dark:text-slate-400">{selectedOnnxModel.description}</p>
          ) : null}
          {selectedOnnxModel?.status === "downloading" && (
            <div className="space-y-1.5">
              <Progress value={selectedOnnxModel.progress || 0} />
              <p className="flex items-center gap-2 text-[11px] text-slate-500">
                <Loader2 className="h-3 w-3 animate-spin" />
                {selectedOnnxModel.message || "Downloading..."}
                <span className="ml-auto">{Math.round(selectedOnnxModel.progress || 0)}%</span>
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
              Download
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => selectedOnnxModel && handleOnnxDelete(selectedOnnxModel.id)}
              disabled={!selectedOnnxModel?.downloaded || selectedOnnxModel.status === "downloading"}
            >
              <Trash2 className="mr-2 h-4 w-4" />
              Delete
            </Button>
          </div>
        </div>
      )}
    </div>
  );

  const nemoLocalModelSettings = (
    <div className="rounded-xl bg-white/70 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-950/40">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold text-slate-950 dark:text-slate-100">NeMo model</p>
          <p className="mt-0.5 text-[11px] leading-4 text-slate-500 dark:text-slate-400">Local .nemo toolkit</p>
        </div>
        {selectedNemoModel ? (
          <Badge variant={getStatusVariant(selectedNemoModel.status)}>{getStatusLabel(selectedNemoModel.status)}</Badge>
        ) : null}
      </div>

      {nemoAvailable === null ? (
        <p className="text-[12px] text-slate-500">Loading NeMo models...</p>
      ) : nemoAvailable === false ? (
        <p className="text-[12px] leading-4 text-slate-500">{nemoMessage || "NeMo toolkit is not installed."}</p>
      ) : (
        <div className="space-y-3">
          <FieldShell label="Model">
            <Select value={nemoModel} onValueChange={handleNemoModelChange}>
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select NeMo model" />
              </SelectTrigger>
              <SelectContent>
                {nemoModels.map((model) => (
                  <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldShell>
          {selectedNemoModel?.description ? (
            <p className="text-[11px] leading-4 text-slate-500 dark:text-slate-400">{selectedNemoModel.description}</p>
          ) : null}
          {selectedNemoModel?.status === "downloading" && (
            <div className="space-y-1.5">
              <Progress value={selectedNemoModel.progress || 0} />
              <p className="flex items-center gap-2 text-[11px] text-slate-500">
                <Loader2 className="h-3 w-3 animate-spin" />
                {selectedNemoModel.message || "Downloading..."}
                <span className="ml-auto">{Math.round(selectedNemoModel.progress || 0)}%</span>
              </p>
            </div>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => selectedNemoModel && handleNemoDownload(selectedNemoModel.id)}
              disabled={!selectedNemoModel || selectedNemoModel.status === "downloading" || selectedNemoModel.downloaded}
            >
              <Download className="mr-2 h-4 w-4" />
              Download
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => selectedNemoModel && handleNemoDelete(selectedNemoModel.id)}
              disabled={!selectedNemoModel?.downloaded || selectedNemoModel.status === "downloading"}
            >
              <Trash2 className="mr-2 h-4 w-4" />
              Delete
            </Button>
          </div>
        </div>
      )}
    </div>
  );

  const activeLocalModelSettings =
    transcriptionModel === "onnx_local"
      ? onnxLocalModelSettings
      : transcriptionModel === "nemo_local"
        ? nemoLocalModelSettings
        : null;

  return (
    <div className={cn(
      "settings-page mx-auto w-full max-w-[1400px] px-4 py-5 text-[13px] transition-opacity duration-150 md:px-5 md:py-6",
      settingsLoaded ? "opacity-100" : "opacity-0",
    )}>
      {settingsError && (
        <QueryErrorState
          className="mb-4"
          title="Could not load settings"
          description={settingsError}
          onRetry={() => window.location.reload()}
        />
      )}

      <header className="mb-4 border-b border-slate-200 pb-3 dark:border-slate-800">
        <h1 className="text-[34px] font-semibold leading-none tracking-normal text-slate-950 dark:text-slate-100">
          Settings
        </h1>
        <p className="mt-1.5 max-w-[64ch] text-[12px] leading-4 text-slate-500 dark:text-slate-400">
          Configure transcription, providers, summaries, credentials, updates, and language behavior.
        </p>
      </header>

      <div className="grid gap-x-7 gap-y-9 lg:grid-cols-2">
        <SectionPanel
          title="Transcription"
          description="Control how audio is captured and how the recording hotkey behaves."
          icon={Mic}
        >
          <div className="divide-y divide-slate-200/80 dark:divide-slate-800">
            {autostartAvailable && (
              <SettingLine label="Start with Windows" description="Launch Scriber when you log in.">
                <Switch checked={autostartEnabled} onCheckedChange={handleAutostartChange} />
              </SettingLine>
            )}

            <SettingLine label="Input device" description="Select the active microphone.">
              <div className={cn("mic-device-dropdown w-full", isMicDropdownOpen && "is-open")}>
                <button
                  type="button"
                  className="mic-device-dropdown-header"
                  onClick={() => setIsMicDropdownOpen((prev) => !prev)}
                  aria-label="Select input device"
                  aria-expanded={isMicDropdownOpen}
                  aria-controls="mic-device-dropdown-tray"
                >
                  <span className="mic-device-dropdown-header-info">
                    <span className={cn("mic-device-dropdown-selected-text", hasSelectedMic && "is-selected")}>
                      {selectedMicLabel || "Select a device..."}
                    </span>
                  </span>
                  <ChevronDown className="mic-device-dropdown-chevron" />
                </button>

                <div id="mic-device-dropdown-tray" className="mic-device-dropdown-tray" aria-hidden={!isMicDropdownOpen}>
                  <div className="mic-device-dropdown-content">
                    <div className="mic-device-dropdown-tray-inner">
                      <div className="mic-device-list">
                        {inputDevices.length === 0 ? (
                          <div className="px-2 py-2 text-sm text-muted-foreground">Loading devices...</div>
                        ) : (
                          inputDevices.map((device, index) => {
                            const deviceValue = device.deviceId || `device-${index}`;
                            const deviceLabel = device.label || `Device ${index + 1}`;
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
                                  aria-label={`Select microphone ${deviceLabel}`}
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
                                  aria-label={isFavorite ? `Remove ${deviceLabel} from favorites` : `Set ${deviceLabel} as favorite`}
                                />
                                <label
                                  htmlFor={favoriteInputId}
                                  className="mic-device-star-label"
                                  title={isFavorite ? "Remove from favorites" : "Set as favorite"}
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
                Favorite microphone is used automatically when connected.
              </div>
            )}

            <SettingLine label="Mic always on" description="Keep capture pre-warmed for minimum latency.">
              <Switch checked={micAlwaysOn} onCheckedChange={handleMicAlwaysOnChange} />
            </SettingLine>

            <SettingLine label="Recording mode" description="Choose how the hotkey behaves.">
              <ToggleGroup
                type="single"
                value={recordingMode}
                onValueChange={(value) => value && void handleRecordingModeChange(value)}
                className="grid w-[220px] max-w-full grid-cols-2 rounded-lg bg-slate-100 p-1 dark:bg-slate-900"
              >
                <ToggleGroupItem value="start_stop" className="h-8 rounded-md text-[11px] data-[state=on]:bg-white data-[state=on]:text-blue-700 data-[state=on]:shadow-sm dark:data-[state=on]:bg-slate-800">
                  <ToggleLeft className="h-4 w-4" />
                  Toggle
                </ToggleGroupItem>
                <ToggleGroupItem value="press_hold" className="h-8 rounded-md text-[11px] data-[state=on]:bg-white data-[state=on]:text-blue-700 data-[state=on]:shadow-sm dark:data-[state=on]:bg-slate-800">
                  <Mic className="h-4 w-4" />
                  Push-to-talk
                </ToggleGroupItem>
              </ToggleGroup>
            </SettingLine>

            <SettingLine label="Global hotkey" description="Shortcut to start or stop recording.">
                <Dialog open={isRecordingHotkey} onOpenChange={setIsRecordingHotkey}>
                <DialogTrigger asChild>
                  <Button variant="outline" className="h-8 w-[220px] max-w-full justify-start font-mono text-[11px]">
                    <Keyboard className="mr-2 h-4 w-4 text-muted-foreground" />
                    {hotkey}
                  </Button>
                </DialogTrigger>
                <DialogContent className="sm:max-w-[425px]">
                  <DialogHeader>
                    <DialogTitle>Record hotkey</DialogTitle>
                    <DialogDescription>Press the key combination you want to use as a shortcut.</DialogDescription>
                  </DialogHeader>
                  <div
                    ref={hotkeyCaptureRef}
                    className="flex h-32 items-center justify-center rounded-lg border-2 border-dashed bg-secondary/20 outline-none transition-colors focus:border-primary focus:bg-primary/5"
                    tabIndex={0}
                    aria-label="Hotkey capture area"
                  >
                    <p className="text-lg font-medium text-primary">{hotkey}</p>
                  </div>
                  <div className="flex justify-end gap-2">
                    <Button variant="ghost" onClick={() => setIsRecordingHotkey(false)}>Cancel</Button>
                    <Button onClick={handleSaveHotkey}>Save</Button>
                  </div>
                </DialogContent>
              </Dialog>
            </SettingLine>

            <SettingLine label="Visualizer bars" description={`Current count: ${visualizerBarCount}`}>
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
          <div className="mt-4 space-y-4">
            {customVocabularySettings}
            {livePostProcessingSettings}
            {summarizationPromptSettings}
          </div>
        </SectionPanel>

        <SectionPanel
          title="Speech-to-text provider"
          description="Choose the primary transcription provider."
          icon={Cloud}
        >
          <div className="mb-2.5 rounded-xl bg-slate-50 p-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
            <p className="text-[11px] font-semibold text-slate-500 dark:text-slate-400">Current provider</p>
            <div className="mt-1 flex items-center gap-2">
              <ProviderIcon
                icon={PROVIDER_MODEL_OPTIONS.find((option) => option.value === transcriptionModel)?.icon}
                label={compactTranscriptionModelLabel}
              />
              <p className="truncate text-[14px] font-semibold text-slate-950 dark:text-slate-100">
                {compactTranscriptionModelLabel}
              </p>
            </div>
            {transcriptionModel === "assemblyai" && (
              <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
                Async mode returns the transcript after recording stops.
              </p>
            )}
            {transcriptionModel === "assemblyai-realtime" && (
              <p className="mt-1 text-[11px] leading-4 text-slate-500 dark:text-slate-400">
                Realtime mode uses AssemblyAI Universal-3.5 Pro through Pipecat.
              </p>
            )}
          </div>

          <div className="space-y-2.5">
            <div className="space-y-2.5">
              {providerGroups.map((group) => (
                <div
                  key={group.key}
                  role="radiogroup"
                  aria-label={`${group.label} transcription providers`}
                  className="rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60"
                >
                  <div className="mb-1.5">
                    <div className="min-w-0">
                      <h3 className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100">
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
                          onSelect={() => void handleTranscriptionModelChange(option.value)}
                        />
                      );
                    })}
                  </div>
                  {group.key === "local" && activeLocalModelSettings ? (
                    <div className="mt-2 border-t border-slate-200/80 pt-2 dark:border-slate-800/80">
                      {activeLocalModelSettings}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        </SectionPanel>

        <SectionPanel
          title="API keys"
          description="Manage provider credentials without expanding the whole page."
          icon={Key}
        >
          <div className="space-y-2">
            {missingActiveCredentialRequirements.length > 0 && (
              <div className="rounded-xl border border-amber-500/35 bg-amber-50 p-2.5 text-[11px] leading-[15px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
                <div className="flex gap-2">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-300" aria-hidden="true" />
                  <div>
                    <p className="font-semibold">API key required before model selection.</p>
                    <p className="mt-1">
                      Save{" "}
                      {missingActiveCredentialRequirements.map((requirement, index) => (
                        <span key={requirement.provider}>
                          {index > 0 ? ", " : ""}
                          <ApiKeyLink helpKey={requirement.helpKey}>{requirement.label}</ApiKeyLink>
                        </span>
                      ))}{" "}
                      below, or choose a model that already has credentials.
                    </p>
                  </div>
                </div>
              </div>
            )}

            {!hasAnyManagedCloudSttCredential && (
              <div className="rounded-xl bg-slate-50 p-2.5 text-[11px] leading-[15px] text-slate-500 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
                No cloud STT credentials are saved yet.
              </div>
            )}

            <div className="grid gap-x-2 gap-y-2 sm:grid-cols-2">
              <ApiCredentialRow provider="OpenAI" icon="openai" value={openAIKey} onValueChange={markCredentialChanged("OpenAI", setOpenAIKey)} show={showOpenAIKey} onShowChange={setShowOpenAIKey} helpKey="openai" saved={savedKeys.OpenAI === true} onSave={() => handleSaveApiKey("OpenAI")} note="Used for OpenAI STT and summarization." />
              <ApiCredentialRow provider="Gemini" icon="gemini" value={geminiKey} onValueChange={markCredentialChanged("Gemini", setGeminiKey)} show={showGeminiKey} onShowChange={setShowGeminiKey} helpKey="gemini" saved={savedKeys.Gemini === true} onSave={() => handleSaveApiKey("Gemini")} />
              <ApiCredentialRow provider="OpenRouter" icon="openrouter" value={openRouterKey} onValueChange={markCredentialChanged("OpenRouter", setOpenRouterKey)} show={showOpenRouterKey} onShowChange={setShowOpenRouterKey} helpKey="openrouter" saved={savedKeys.OpenRouter === true} onSave={() => handleSaveApiKey("OpenRouter")} />
              <ApiCredentialRow provider="YouTube" icon="youtube" value={youtubeKey} onValueChange={markCredentialChanged("YouTube", setYoutubeKey)} show={showYoutubeKey} onShowChange={setShowYoutubeKey} helpKey="youtube" saved={savedKeys.YouTube === true} onSave={() => handleSaveApiKey("YouTube")} note="Used for search and metadata in the YouTube tab." />
              <ApiCredentialRow provider="Soniox" icon="soniox" value={sonioxKey} onValueChange={markCredentialChanged("Soniox", setSonioxKey)} show={showSonioxKey} onShowChange={setShowSonioxKey} helpKey="soniox" saved={savedKeys.Soniox === true} onSave={() => handleSaveApiKey("Soniox")} />
              <ApiCredentialRow provider="Mistral" icon="mistral" value={mistralKey} onValueChange={markCredentialChanged("Mistral", setMistralKey)} show={showMistralKey} onShowChange={setShowMistralKey} helpKey="mistral" saved={savedKeys.Mistral === true} onSave={() => handleSaveApiKey("Mistral")} />
              <ApiCredentialRow provider="Smallest AI" icon="smallest" value={smallestKey} onValueChange={markCredentialChanged("Smallest AI", setSmallestKey)} show={showSmallestKey} onShowChange={setShowSmallestKey} helpKey="smallest" saved={savedKeys["Smallest AI"] === true} onSave={() => handleSaveApiKey("Smallest AI")} />
              <ApiCredentialRow provider="AssemblyAI" icon="assemblyai" value={assemblyAIKey} onValueChange={markCredentialChanged("AssemblyAI", setAssemblyAIKey)} show={showAssemblyAIKey} onShowChange={setShowAssemblyAIKey} helpKey="assemblyai" saved={savedKeys.AssemblyAI === true} onSave={() => handleSaveApiKey("AssemblyAI")} />
              <ApiCredentialRow provider="Deepgram" icon="deepgram" value={deepgramKey} onValueChange={markCredentialChanged("Deepgram", setDeepgramKey)} show={showDeepgramKey} onShowChange={setShowDeepgramKey} helpKey="deepgram" saved={savedKeys.Deepgram === true} onSave={() => handleSaveApiKey("Deepgram")} />
              <ApiCredentialRow provider="Gladia" icon="gladia" value={gladiaKey} onValueChange={markCredentialChanged("Gladia", setGladiaKey)} show={showGladiaKey} onShowChange={setShowGladiaKey} helpKey="gladia" saved={savedKeys.Gladia === true} onSave={() => handleSaveApiKey("Gladia")} />
              <ApiCredentialRow provider="Groq" icon="groq" value={groqKey} onValueChange={markCredentialChanged("Groq", setGroqKey)} show={showGroqKey} onShowChange={setShowGroqKey} helpKey="groq" saved={savedKeys.Groq === true} onSave={() => handleSaveApiKey("Groq")} />
              <ApiCredentialRow provider="Speechmatics" icon="speechmatics" value={speechmaticsKey} onValueChange={markCredentialChanged("Speechmatics", setSpeechmaticsKey)} show={showSpeechmaticsKey} onShowChange={setShowSpeechmaticsKey} helpKey="speechmatics" saved={savedKeys.Speechmatics === true} onSave={() => handleSaveApiKey("Speechmatics")} />
              <ApiCredentialRow provider="ElevenLabs" icon="elevenlabs" value={elevenLabsKey} onValueChange={markCredentialChanged("ElevenLabs", setElevenLabsKey)} show={showElevenLabsKey} onShowChange={setShowElevenLabsKey} helpKey="fal" saved={savedKeys.ElevenLabs === true} onSave={() => handleSaveApiKey("ElevenLabs")} note="Scriber stores the fal.ai key for this provider." />
              <ApiCredentialRow provider="Google Cloud" icon="googlecloud" value={googleApplicationCredentials} onValueChange={markCredentialChanged("Google Cloud", setGoogleApplicationCredentials)} helpKey="googleCloud" saved={savedKeys["Google Cloud"] === true} onSave={() => handleSaveApiKey("Google Cloud")} inputType="text" placeholder="C:\\path\\to\\service-account.json" note="Path to the service account JSON used by Google Cloud STT." />
              <ApiCredentialRow provider="Azure MAI" icon="azure" value={azureMaiKey} onValueChange={markCredentialChanged("Azure", setAzureMaiKey)} show={showAzureMaiKey} onShowChange={setShowAzureMaiKey} helpKey="azure" saved={savedKeys.Azure === true} onSave={() => handleSaveApiKey("Azure")} note="The key must belong to a region that supports the configured model.">
                <div className="grid gap-3 sm:grid-cols-2">
                  <FieldShell label="Region">
                    <Input value={azureMaiRegion} onChange={(event) => markCredentialChanged("Azure", setAzureMaiRegion)(event.target.value)} placeholder="northeurope" className="font-mono text-sm" />
                  </FieldShell>
                  <FieldShell label="Model">
                    <Input value={azureMaiModel} onChange={(event) => markCredentialChanged("Azure", setAzureMaiModel)(event.target.value)} placeholder="mai-transcribe-1.5" className="font-mono text-sm" />
                  </FieldShell>
                </div>
              </ApiCredentialRow>
            </div>
          </div>
        </SectionPanel>

        <SectionPanel
          title="Summarization"
          description="Choose the model and automatic summary behavior."
          icon={Sparkles}
        >
          <div className="space-y-4">
            <div
              role="radiogroup"
              aria-label="Summary models"
              className="space-y-2"
            >
              {summaryModelGroups.map((group) => (
                <div
                  key={group.key}
                  className="rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60"
                >
                  <div className="mb-1.5">
                    <h3 className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100">
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
                          onSelect={() => void handleSummarizationModelChange(option.value)}
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            <SettingLine label="Auto-summarize" description="Summarize new transcripts automatically.">
              <Switch checked={autoSummarize} onCheckedChange={handleAutoSummarizeChange} />
            </SettingLine>
          </div>
        </SectionPanel>

        <SectionPanel
          title="Update app"
          description="Keep Scriber current without interrupting recordings."
          icon={Shield}
        >
          <div className="space-y-3">
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
              <div>
                <p className="text-[12px] font-semibold leading-4 text-slate-950 dark:text-slate-100">Update status</p>
                <p className="mt-0.5 text-[11px] leading-[15px] text-slate-500 dark:text-slate-400">{desktopUpdate.message}</p>
              </div>
              <Badge variant={desktopUpdateBadgeVariant}>{desktopUpdateBadgeLabel}</Badge>
            </div>

            <div className="grid gap-2 rounded-xl bg-slate-50/90 p-2.5 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center dark:bg-slate-900/60">
              <div>
                <p className="text-[12px] font-semibold leading-4 text-slate-950 dark:text-slate-100">Automatic checks</p>
                <p className="mt-0.5 text-[10.5px] leading-[14px] text-slate-500 dark:text-slate-400">Weekly background checks via GitHub.</p>
              </div>
              <Switch checked={desktopUpdate.autoCheckEnabled} onCheckedChange={handleDesktopAutoCheckChange} disabled={isInstallingDesktopUpdate} />
            </div>

            <div className="grid grid-cols-2 gap-2 text-[11px]">
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
                <p className="text-[10px] leading-3 text-slate-500">Current</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdate.currentVersion || "Unknown"}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
                <p className="text-[10px] leading-3 text-slate-500">Available</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateAvailableVersionLabel}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
                <p className="text-[10px] leading-3 text-slate-500">Last check</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateLastCheckedLabel}</p>
              </div>
              <div className="rounded-lg bg-slate-50 p-2 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-slate-900/60">
                <p className="text-[10px] leading-3 text-slate-500">Next check</p>
                <p className="truncate font-semibold leading-4 text-slate-950 dark:text-slate-100">{desktopUpdateNextCheckLabel}</p>
              </div>
            </div>

            {desktopUpdateProgress && (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3 text-[11px] text-slate-500">
                  <span>{desktopUpdateProgress.message}</span>
                  {typeof desktopUpdateProgress.percent === "number" && <span>{desktopUpdateProgress.percent}%</span>}
                </div>
                <Progress value={desktopUpdateProgress.percent ?? 0} />
              </div>
            )}

            <div className="grid gap-2 sm:grid-cols-2">
              <Button variant="outline" className="h-8 text-[12px]" onClick={handleCheckDesktopUpdate} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                {isCheckingDesktopUpdate ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                Check for updates
              </Button>
              <Button className="h-8 text-[12px]" onClick={handleInstallDesktopUpdate} disabled={!desktopUpdate.available || isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                {isInstallingDesktopUpdate ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
                Install and restart
              </Button>
            </div>
            {desktopUpdate.available && (
              <div className="grid grid-cols-2 gap-2">
                <Button variant="outline" className="h-8 text-[12px]" onClick={handleRemindDesktopUpdateLater} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                  Remind tomorrow
                </Button>
                <Button variant="outline" className="h-8 text-[12px]" onClick={handleSkipDesktopUpdateVersion} disabled={isCheckingDesktopUpdate || isInstallingDesktopUpdate}>
                  Skip version
                </Button>
              </div>
            )}
            <Button variant="ghost" size="sm" className="h-8 justify-start px-1 text-[12px]" onClick={handleOpenDesktopUpdateReleaseNotes} disabled={isInstallingDesktopUpdate}>
              <ExternalLink className="mr-2 h-4 w-4" />
              Release notes
            </Button>
          </div>
        </SectionPanel>

        <SectionPanel
          title="Language"
          description="Auto-detect or choose a preferred transcription language."
          icon={Languages}
        >
          <div className="space-y-4">
            <SettingLine label="Auto-detect language" description="Let the provider infer spoken language.">
              <Switch
                checked={language === "auto"}
                onCheckedChange={(enabled) => void handleLanguageChange(enabled ? "auto" : "en")}
              />
            </SettingLine>

            <SettingLine label="Preferred language" description="Used when auto-detect is off.">
              <div className={cn("language-dropdown w-full", isLanguageDropdownOpen && "is-open")}>
                <button
                  type="button"
                  className="language-dropdown-header"
                  onClick={() => setIsLanguageDropdownOpen((prev) => !prev)}
                  aria-label="Select default transcription language"
                  aria-expanded={isLanguageDropdownOpen}
                  aria-controls="language-dropdown-tray"
                >
                  <span className="language-dropdown-header-info">
                    <span className="language-dropdown-selected-value-wrapper">
                      <LanguageFlag value={selectedLanguage.value} className="language-header-flag" />
                      <span className="language-dropdown-selected-text is-selected">{selectedLanguage.label}</span>
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
                                aria-label={`Select ${option.label} as default transcription language`}
                              />
                              <label htmlFor={inputId} className="language-option-label">
                                <LanguageFlag value={option.value} />
                                <span className="language-name">{option.label}</span>
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
    </div>
  );
}
