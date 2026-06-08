export type TranscriptStatus = "completed" | "processing" | "failed" | "recording" | "stopped";
export type SummaryStatus = "idle" | "pending" | "completed" | "failed";

export const REST_API_VERSION = "1";

export type TranscriptType = "mic" | "file" | "youtube";

export interface BackendHealthResponse {
  ok: boolean;
  ready: boolean;
  version: string;
  apiVersion: typeof REST_API_VERSION;
  workerVersion: string;
  pid: number;
  host: string;
  port: number;
  startedAt: string;
  uptimeSeconds: number;
  activeSession: string | null;
  recordingState: string;
  runtimeMode: string;
}

export interface BackendStateResponse {
  listening: boolean;
  status: string;
  inputWarning?: string;
  inputWarningCode?: string;
  inputWarningActions?: Array<{
    id: string;
    label: string;
    uri: string;
  }>;
  current?: {
    id?: string | number;
    content?: string;
    [key: string]: unknown;
  } | null;
  sessionId?: string | null;
  backgroundProcessing: boolean;
  recordingState: string;
  transcribing: boolean;
}

export interface AutostartStatus {
  enabled: boolean;
  available: boolean;
  message?: string;
}

export interface MicrophoneDevice {
  deviceId: string;
  label: string;
}

export interface MicrophonesResponse {
  devices: MicrophoneDevice[];
}

export interface MicrophonesRefreshResponse {
  scheduled: boolean;
  deviceMonitor: "running" | "disabled" | string;
}

export interface FrontendReadyRequest {
  apiVersion: typeof REST_API_VERSION;
  tauriRuntime: boolean;
  backendBaseUrl: string;
  locationOrigin: string;
  path: string;
}

export interface FrontendReadyLastSeen {
  receivedAt: string;
  receivedAtUptimeSeconds: number;
  runtimeMode: string;
  pid: number;
  tauriRuntime: boolean;
  backendBaseUrl: string | null;
  locationOrigin: string | null;
  path: string | null;
  origin: string | null;
  userAgent: string | null;
}

export interface FrontendReadyResponse {
  apiVersion: typeof REST_API_VERSION;
  ready: boolean;
  lastSeen: FrontendReadyLastSeen | null;
}

export interface RuntimeLogEntry {
  source: string;
  line: number;
  level: "TRACE" | "DEBUG" | "INFO" | "SUCCESS" | "WARNING" | "ERROR" | "CRITICAL" | string;
  message: string;
  timestamp?: string | null;
  timestampMs?: number | null;
  component?: string | null;
}

export interface RuntimeLogsResponse {
  apiVersion: typeof REST_API_VERSION;
  items: RuntimeLogEntry[];
  sources: string[];
  limit: number;
  truncated: boolean;
}

export interface ApiMessageResponse {
  message?: string;
}

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
  summaryStatus?: SummaryStatus;
  summaryError?: string;
  summaryUpdatedAt?: string;
  content?: string;
}

export type TranscriptDetailResponse = TranscriptHistoryItem;

export type FileTranscribeResponse = TranscriptHistoryItem;

export interface TranscriptDeleteResponse {
  success: boolean;
  id?: string;
  message?: string;
}

export interface YouTubeSearchItem {
  videoId: string;
  url: string;
  title: string;
  description: string;
  channelTitle: string;
  publishedAt: string;
  thumbnailUrl: string;
  duration: string;
  durationSeconds: number;
  viewCount?: number;
  likeCount?: number;
}

export interface YouTubeSearchResponse {
  query: string;
  nextPageToken: string;
  prevPageToken: string;
  totalResults: number;
  resultsPerPage: number;
  items: YouTubeSearchItem[];
}

export type LocalModelStatus = "ready" | "not_downloaded" | "downloading" | "error";

export interface LocalModelInfo {
  id: string;
  name: string;
  description: string;
  languages: string[];
  sizeMb: number;
  supportsTimestamps?: boolean;
  downloaded?: boolean;
  status?: LocalModelStatus;
  progress?: number;
  message?: string;
}

export interface OnnxModelInfo extends LocalModelInfo {
  sizeMbByQuantization?: Record<string, number>;
  supportedQuantizations?: string[];
}

export type NemoModelInfo = LocalModelInfo;

export interface OnnxModelsResponse {
  available: boolean;
  message?: string;
  models: OnnxModelInfo[];
  currentModel?: string;
  quantization?: string;
}

export interface NemoModelsResponse {
  available: boolean;
  message?: string;
  models: NemoModelInfo[];
  currentModel?: string;
}

export interface LocalModelActionResponse {
  success?: boolean;
  message?: string;
  modelId?: string;
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
  azureMaiModel?: string;
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
