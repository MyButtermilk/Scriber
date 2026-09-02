// Both transports use the same published model; Async is not a second model.
export const META_TRANSCRIPTION_OPTIONS = [
  {
    value: "meta-realtime",
    service: "meta_stt",
    label: "Meta Muse Voice Transcribe Realtime",
    model: "muse-voice-transcribe-1.0",
    group: "cloud_streaming",
    icon: "meta",
    usdPerHour: 0.18,
    routeNote: "Live partials and completed turns · up to 60 minutes · Meta Model API key",
  },
  {
    value: "meta-async",
    service: "meta_stt_async",
    label: "Meta Muse Voice Transcribe Async",
    model: "muse-voice-transcribe-1.0",
    group: "cloud_async",
    icon: "meta",
    usdPerHour: 0.18,
    routeNote: "Transcribes after stop · files up to 10 minutes · Meta Model API key",
  },
] as const;

export const META_CREDENTIAL_REQUIREMENT = {
  provider: "Meta Model API",
  label: "Meta Model API key",
  helpKey: "meta",
} as const;

export const META_MEETING_FINAL_STT_OPTION = {
  value: "meta_stt_async",
  label: "Meta Muse Voice Transcribe",
  model: "muse-voice-transcribe-1.0",
  credentialModel: "meta-async",
  recommended: false,
  nativeDiarization: true,
  fiveHourSupported: false,
  detail: "Native speaker labels and approximate turn timestamps. Maximum 10 minutes per recording.",
} as const;

export function metaFrontendModelForService(service: string) {
  return META_TRANSCRIPTION_OPTIONS.find((option) => option.service === service)?.value ?? null;
}

export function metaSettingsPatchForModel(value: string) {
  const option = META_TRANSCRIPTION_OPTIONS.find((candidate) => candidate.value === value);
  return option ? { defaultSttService: option.service } : null;
}
