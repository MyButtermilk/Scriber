export type TranscriptStatus = "completed" | "processing" | "failed" | "recording" | "stopped";

export type TranscriptType = "mic" | "file" | "youtube";

export interface TranscriptHistoryItem {
  id: string;
  title: string;
  date: string;
  duration: string;
  status: TranscriptStatus;
  type: TranscriptType;
  language?: string;
  step?: string;
  sourceUrl?: string;
  channel?: string;
  channelTitle?: string;
  thumbnailUrl?: string;
  createdAt?: string;
  updatedAt?: string;
  preview?: string;
  summary?: string;
  content?: string;
}

export interface FileUploadLimits {
  provider: string;
  providerLabel: string;
  usesDirectProviderLimit: boolean;
  audioMaxBytes: number;
  audioMaxLabel: string;
  rawAudioIngestMaxBytes: number;
  rawAudioIngestMaxLabel: string;
  videoMaxBytes: number;
  videoMaxLabel: string;
  compressionThresholdBytes: number;
  compressionThresholdLabel: string;
}

export interface SettingsApiKeys {
  soniox?: string;
  mistral?: string;
  smallest?: string;
  assemblyai?: string;
  deepgram?: string;
  openai?: string;
  azureSpeechKey?: string;
  azureSpeechRegion?: string;
  azureMaiSpeechKey?: string;
  azureMaiRegion?: string;
  gladia?: string;
  groq?: string;
  speechmatics?: string;
  elevenlabs?: string;
  googleApiKey?: string;
  googleApplicationCredentials?: string;
  youtubeApiKey?: string;
}

export interface SettingsResponse {
  hotkey?: string;
  hotkeyRaw?: string;
  mode?: "toggle" | "push_to_talk" | string;
  defaultSttService?: string;
  sonioxMode?: "realtime" | "async" | string;
  sonioxAsyncModel?: string;
  language?: string;
  micDevice?: string;
  favoriteMic?: string;
  favoriteMicAvailable?: boolean;
  micAlwaysOn?: boolean;
  debug?: boolean;
  customVocab?: string;
  summarizationPrompt?: string;
  summarizationModel?: string;
  autoSummarize?: boolean;
  openaiSttModel?: string;
  onnxModel?: string;
  onnxQuantization?: string;
  onnxUseGpu?: boolean;
  nemoModel?: string;
  visualizerBarCount?: number;
  fileUploadLimits?: FileUploadLimits;
  apiKeys?: SettingsApiKeys;
}

export interface SettingsUpdatePayload {
  hotkey?: string;
  mode?: "toggle" | "push_to_talk";
  defaultSttService?: string;
  sonioxMode?: "realtime" | "async";
  sonioxAsyncModel?: string;
  language?: string;
  micDevice?: string;
  favoriteMic?: string;
  micAlwaysOn?: boolean;
  debug?: boolean;
  customVocab?: string;
  summarizationPrompt?: string;
  summarizationModel?: string;
  autoSummarize?: boolean;
  openaiSttModel?: string;
  onnxModel?: string;
  onnxQuantization?: string;
  onnxUseGpu?: boolean;
  nemoModel?: string;
  visualizerBarCount?: number;
  apiKeys?: SettingsApiKeys;
}
