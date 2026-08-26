export type GeminiTranscriptionUiValue = "gemini-realtime" | "gemini-stt";
export type GeminiTranscriptionService = "gemini_realtime" | "gemini_stt";

type GeminiTranscriptionOption = {
  value: GeminiTranscriptionUiValue;
  service: GeminiTranscriptionService;
  label: string;
  usdPerThousandMinutes: number;
  wordErrorRatePercent: number;
  group: "cloud_streaming" | "cloud_async";
  icon: "gemini";
  routeNote: string;
};

export const GEMINI_REALTIME_TRANSCRIPTION_OPTION = {
  value: "gemini-realtime",
  service: "gemini_realtime",
  label: "Gemini 3.5 Transcribe Live",
  usdPerThousandMinutes: 9.0,
  wordErrorRatePercent: 4.0,
  group: "cloud_streaming",
  icon: "gemini",
  routeNote: "Interim and final text · smart transcription",
} as const satisfies GeminiTranscriptionOption;

export const GEMINI_ASYNC_TRANSCRIPTION_OPTION = {
  value: "gemini-stt",
  service: "gemini_stt",
  label: "Gemini 3.5 Transcribe",
  usdPerThousandMinutes: 5.0,
  wordErrorRatePercent: 2.6,
  group: "cloud_async",
  icon: "gemini",
  routeNote: "Speaker diarization and word timestamps · final text",
} as const satisfies GeminiTranscriptionOption;

const GEMINI_TRANSCRIPTION_OPTIONS = [GEMINI_REALTIME_TRANSCRIPTION_OPTION, GEMINI_ASYNC_TRANSCRIPTION_OPTION] as const;

export const GEMINI_MEETING_FINAL_STT_OPTION = {
  value: "gemini_stt",
  label: "Gemini 3.5 Transcribe",
  model: "gemini-3.5-transcribe",
  credentialModel: "gemini-stt",
  recommended: false,
  nativeDiarization: true,
  fiveHourSupported: false,
  detail: "Creates the final transcript with speaker names and word-level timing after the meeting.",
} as const;

export const GEMINI_CREDENTIAL_REQUIREMENT = {
  provider: "Gemini",
  label: "Gemini API key",
  helpKey: "gemini",
} as const;

export function geminiFrontendModelForService(service: string): GeminiTranscriptionUiValue | null {
  return GEMINI_TRANSCRIPTION_OPTIONS.find((option) => option.service === service)?.value ?? null;
}

export function geminiSettingsPatchForModel(value: string): { defaultSttService: GeminiTranscriptionService } | null {
  const option = GEMINI_TRANSCRIPTION_OPTIONS.find((candidate) => candidate.value === value);
  return option ? { defaultSttService: option.service } : null;
}
