export type TranscriptStatus = "completed" | "processing" | "failed" | "recording" | "stopped";
export type SummaryStatus = "idle" | "pending" | "completed" | "failed";

export const REST_API_VERSION = "1";

export type TranscriptType = "mic" | "file" | "youtube" | "meeting";

export type MeetingState =
  | "starting" | "recording" | "paused" | "stopping" | "finalizing" | "analyzing"
  | "ready" | "capture_failed" | "finalization_failed" | "analysis_failed"
  | "interrupted" | "discarded";

export type MeetingImportState =
  | "created" | "receiving" | "received" | "probing" | "preparing"
  | "waiting_for_workspace" | "committing" | "finalizing" | "completed"
  | "cancel_requested" | "canceled" | "failed";

export interface MeetingImportJob {
  apiVersion: typeof REST_API_VERSION;
  id: string;
  state: MeetingImportState;
  sourceFilename: string;
  title: string;
  language: string;
  profileId: string;
  expectedBytes: number | null;
  receivedBytes: number;
  progress: number;
  status: string;
  meetingId: string | null;
  cancelRequested: boolean;
  canCancel: boolean;
  canRetry: boolean;
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
  finishedAt: string | null;
  uploadUrl?: string;
}

export interface MeetingImportsResponse {
  apiVersion: typeof REST_API_VERSION;
  items: MeetingImportJob[];
  total: number;
  limit: number;
}

export interface MeetingAecMetrics {
  measurement: "render-active-raw-to-clean-energy-ratio";
  renderActiveFrames?: number;
  renderActiveDurationMs?: number;
  renderEnergy?: number;
  rawMicEnergy?: number;
  cleanMicEnergy?: number;
  echoReductionDb?: number;
}

export interface MeetingNativeStopSnapshot {
  framesProcessed?: number;
  bytesForwarded?: number;
  sidecarUptimeMs?: number;
  relayHealthy: boolean;
  aecMetrics?: MeetingAecMetrics;
}

export interface MeetingCaptureMetadata extends Record<string, unknown> {
  captureStartLatencyMs?: number;
  aecActive?: boolean;
  aecRequested?: boolean;
  aecMetrics?: MeetingAecMetrics;
  nativeStopSessions?: MeetingNativeStopSnapshot[];
  audioPurgedAt?: string;
}

export interface MeetingSummary {
  id: string;
  title: string;
  state: MeetingState;
  language: string;
  liveProvider: string;
  finalProvider: string;
  analysisModel: string;
  aecEnabled: boolean;
  voiceLibraryEnabled: boolean;
  consentConfirmed: boolean;
  origin: "captured" | "imported";
  startedAt: string | null;
  endedAt: string | null;
  createdAt: string;
  updatedAt: string;
  errorCode: string;
  errorMessage: string;
  captureMetadata: MeetingCaptureMetadata;
  audioRetentionDays: number;
  smartTurnEnabled: boolean;
  autoAnalyze: boolean;
  transcriptEditVersion: number;
}

export interface MeetingSegment {
  id: string;
  meetingId: string;
  revision: "live" | "canonical";
  source: "microphone" | "system" | "mixed";
  speakerId: string | null;
  speakerLabel: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  text: string;
  confidence: number | null;
  alignmentQuality: "exact_word" | "provider_segment" | "estimated";
  isFinal: boolean;
  sequence: number;
  createdAt: string;
  editVersion: number;
  editedAt: string | null;
}

export interface MeetingNote {
  id: string;
  meetingId: string;
  body: string;
  atMs: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface MeetingTranscriptCheckpoint {
  id: string;
  meetingId: string;
  sequence: number;
  cutoffMs: number;
  segmentCount: number;
  sources: Array<"microphone" | "system" | "mic_clean">;
  frontiers: Record<string, unknown>;
  commitOrdinal: number;
  snapshotSha256: string;
  createdAt: string;
  updatedAt: string;
}

export interface MeetingActionItem {
  id: string;
  meetingId: string;
  text: string;
  owner: string | null;
  dueDate: string | null;
  status: "open" | "done" | "dismissed";
  segmentIds: string[];
  userModified: boolean;
  provenance: "automatic" | "user_modified" | "carried_user";
  createdAt: string;
  updatedAt: string;
}

export interface MeetingOutput {
  id: string;
  kind: string;
  schemaVersion: string;
  version: number;
  supersedesId: string | null;
  transcriptRevision: "canonical" | "live";
  transcriptEditVersion: number;
  provider: string;
  status: string;
  payload: Record<string, unknown>;
  errorMessage: string;
  updatedAt: string;
}

export type MeetingAudioTrackSource = "microphone" | "system" | "mic_clean" | "mixed";

export interface MeetingAudioTrackManifestEntry {
  source: MeetingAudioTrackSource;
  streamIndex: number;
  codec: string;
  sampleRate: number;
  channels: number;
  /** Meeting-clock position represented by asset-local currentTime 0. */
  timelineOriginMs?: number;
  durationMs: number;
  sampleCount: number;
  pcmSha256: string;
  equalityVerified: boolean;
}

export interface MeetingAudioAsset {
  id: string;
  meetingId: string;
  kind: "multitrack_flac" | "playback_mix" | string;
  relativePath: string;
  codec: string;
  sampleRate: number | null;
  channels: number | null;
  durationMs: number | null;
  byteSize: number;
  sha256: string;
  /** Optional for cached responses created before track-manifest v2. */
  trackManifestVersion?: number;
  trackManifest?: MeetingAudioTrackManifestEntry[];
  equalityVerified?: boolean;
  createdAt: string;
}

export interface MeetingDetail extends MeetingSummary {
  apiVersion: typeof REST_API_VERSION;
  segments: MeetingSegment[];
  speakers: Array<{
    id: string;
    meetingId: string;
    label: string;
    displayName: string;
    sourceHint: string;
    profileId: string | null;
    confidence: number | null;
    createdAt: string;
    updatedAt: string;
  }>;
  notes: MeetingNote[];
  actionItems: MeetingActionItem[];
  outputs: MeetingOutput[];
  outputVersions: Array<Omit<MeetingOutput, "updatedAt"> & { createdAt: string }>;
  audioGaps: Array<{
    id: string;
    meetingId: string;
    source: "microphone" | "system" | "mic_clean" | "all";
    startedAtMs: number;
    endedAtMs: number;
    reason: string;
    createdAt: string;
  }>;
  audioAssets: MeetingAudioAsset[];
  transcriptCheckpoints: MeetingTranscriptCheckpoint[];
  finalRoute?: {
    provider: string;
    model: string;
    transport: string;
    language: string;
    timestampMode: string;
    diarizationMode: string;
  } | null;
}

export interface MeetingTranscriptSearchResponse {
  apiVersion: typeof REST_API_VERSION;
  query: string;
  items: MeetingSegment[];
}

export interface MeetingsResponse {
  apiVersion: typeof REST_API_VERSION;
  items: MeetingSummary[];
  total: number;
  limit: number;
  offset: number;
  activeMeeting: MeetingSummary | null;
}

export interface MeetingCapabilities {
  apiVersion: typeof REST_API_VERSION;
  platform: "windows" | "unsupported";
  shellIpcAvailable: boolean;
  nativeMeetingCapture: boolean;
  liveMicBusy: boolean;
  activeMeeting: MeetingSummary | null;
  sources: Array<"microphone" | "system">;
  requiresPermissionConfirmation: boolean;
  longSession: {
    targetDurationSeconds: number;
    checkpointIntervalSeconds: number;
    requiredFreeBytes: number;
    availableFreeBytes: number | null;
    estimatedCaptureSeconds: number | null;
    storageReady: boolean;
  };
}

export interface MeetingProviderProfile {
  id: string;
  name: string;
  description: string;
  liveProvider: string;
  finalProvider: string;
  analysisModel: string;
  stages: Array<{
    id: "live" | "final" | "analysis" | string;
    label: string;
    provider: string;
    model: string;
    purpose: string;
  }>;
  language: string;
  aecEnabled: boolean;
  voiceLibraryEnabled: boolean;
  audioRetentionDays: number;
  smartTurnEnabled: boolean;
  autoAnalyze: boolean;
  available: boolean;
  fiveHourSupported: boolean;
  fiveHourReason: string;
  maxDurationSeconds: number | null;
  unavailableReason: string;
}

export interface MeetingProfilesResponse {
  apiVersion: typeof REST_API_VERSION;
  defaultProfileId: string;
  profiles: MeetingProviderProfile[];
  providerCapabilities: Record<string, {
    live: boolean;
    timestamps: boolean;
    liveDiarization: boolean;
    batchDiarization: boolean;
    local: boolean;
    maxDurationSeconds: number | null;
    structuredTokens: boolean;
    localDiarizationFallback?: boolean;
    fiveHourSupported: boolean;
    fiveHourReason: string;
  }>;
  finalProviderOptions: Array<{
    id: string;
    label: string;
    model: string;
    diarization: boolean;
    recommendation: string;
    fiveHourSupported: boolean;
    fiveHourReason: string;
    maxDurationSeconds: number | null;
    available?: boolean;
    unavailableReason?: string;
  }>;
}

export interface MeetingAudioEndpoint {
  endpointIdHash: string;
  friendlyName: string;
  isDefault: boolean;
  defaultRoles: string[];
}

export interface MeetingAudioDevicesResponse {
  apiVersion: typeof REST_API_VERSION;
  available: boolean;
  capture: MeetingAudioEndpoint[];
  render: MeetingAudioEndpoint[];
  reason: string;
}

export interface MeetingDeviceTestResponse {
  apiVersion: typeof REST_API_VERSION;
  available: boolean;
  durationMs: number;
  aecActive: boolean;
  testTonePlayed: boolean;
  sources: Record<string, {
    frames: number;
    audioFrames: number;
    rms: number;
    peak: number;
    active: boolean;
    errorCode: string;
  }>;
  audioPersisted: false;
  audioSentToProvider: false;
}

export interface MeetingDetectionResponse {
  apiVersion: typeof REST_API_VERSION;
  available: boolean;
  detection: null | {
    detectionId: string;
    label: string;
    source: string;
    detectedAt: string;
    calendarEvent: OutlookCalendarStatus["nextEvent"];
  };
}

export interface OutlookCalendarStatus {
  apiVersion: typeof REST_API_VERSION;
  configured: boolean;
  connected: boolean;
  authorizationPending: boolean;
  scopes: string[];
  lastSyncAt: string;
  lastError: string;
  nextEvent: {
    id: string;
    subject: string;
    start_at: string;
    end_at: string;
    join_url: string;
    organizer: { name: string; address: string } | null;
    participants: Array<{ name: string; address: string }>;
  } | null;
}

export interface SpeakerProfileSummary {
  id: string;
  displayName: string;
  sampleCount: number;
  isNamed: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface SpeakerProfilesResponse {
  apiVersion: typeof REST_API_VERSION;
  enabled: boolean;
  items: SpeakerProfileSummary[];
  message: string;
}

export interface SpeakerModelStatus {
  apiVersion: typeof REST_API_VERSION;
  optedIn: boolean;
  installed: boolean;
  model: string;
  revision: string;
  byteSize: number;
  expectedByteSize: number;
  sha256: string;
  license: string;
}

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

export interface RuntimeLogsClearResponse {
  apiVersion: typeof REST_API_VERSION;
  ok: boolean;
  cleared: number;
  failed: number;
  clearedSources: string[];
  failures: Array<{
    source: string;
    error: string;
  }>;
}

export interface PostProcessingDiagnostic {
  apiVersion?: typeof REST_API_VERSION;
  createdAt?: string;
  durationMs?: number | null;
  error?: string;
  errorType?: string;
  fallbackToRaw?: boolean;
  maxOutputTokens?: number | null;
  model?: string;
  outputChanged?: boolean | null;
  postProcessed?: boolean;
  processedChars?: number | null;
  promptChars?: number | null;
  provider?: string | null;
  providerResponseChars?: number | null;
  rawChars?: number;
  rawWords?: number;
  sessionIdPrefix?: string;
  status?: "started" | "success" | "failure" | "empty_output" | "skipped" | string;
  transcriptId?: string;
}

export interface PostProcessingDiagnosticsResponse {
  apiVersion: typeof REST_API_VERSION;
  items: PostProcessingDiagnostic[];
  latest: PostProcessingDiagnostic | null;
  count: number;
  limit: number;
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
  processingStartedAt?: string;
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
  runtime?: string;
  hfRepo?: string;
  hfRepoByQuantization?: Record<string, string>;
  localDirName?: string;
  sizeMbByQuantization?: Record<string, number>;
  supportedQuantizations?: string[];
}

export interface OnnxModelsResponse {
  available: boolean;
  message?: string;
  models: OnnxModelInfo[];
  currentModel?: string;
  quantization?: string;
}

export interface LocalModelActionResponse {
  success?: boolean;
  message?: string;
  modelId?: string;
  quantization?: string;
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
  openrouter?: string;
  cerebras?: string;
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
  segmentSpeechWithVad?: boolean;
  debug?: boolean;
  customVocab?: string;
  summarizationPrompt?: string;
  summarizationModel?: string;
  autoSummarize?: boolean;
  youtubePreferCaptions?: boolean;
  voiceprintLibraryOptIn?: boolean;
  postProcessingEnabled?: boolean;
  postProcessingHotkey?: string;
  postProcessingHotkeyRaw?: string;
  meetingHotkey?: string;
  meetingHotkeyRaw?: string;
  meetingFinalProvider?: string;
  meetingAnalysisModel?: string;
  meetingSmartTurnEnabled?: boolean;
  meetingAutoAnalyze?: boolean;
  meetingAecEnabled?: boolean;
  meetingAudioRetentionDays?: number;
  speakerDiarizationFallbackEnabled?: boolean;
  postProcessingPrompt?: string;
  postProcessingModel?: string;
  openaiSttModel?: string;
  openaiRealtimeSttModel?: string;
  onnxModel?: string;
  onnxQuantization?: string;
  onnxUseGpu?: boolean;
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
  segmentSpeechWithVad?: boolean;
  debug?: boolean;
  customVocab?: string;
  summarizationPrompt?: string;
  summarizationModel?: string;
  autoSummarize?: boolean;
  youtubePreferCaptions?: boolean;
  voiceprintLibraryOptIn?: boolean;
  postProcessingEnabled?: boolean;
  postProcessingHotkey?: string;
  meetingHotkey?: string;
  meetingFinalProvider?: string;
  meetingAnalysisModel?: string;
  meetingSmartTurnEnabled?: boolean;
  meetingAutoAnalyze?: boolean;
  meetingAecEnabled?: boolean;
  meetingAudioRetentionDays?: number;
  speakerDiarizationFallbackEnabled?: boolean;
  postProcessingPrompt?: string;
  postProcessingModel?: string;
  openaiSttModel?: string;
  openaiRealtimeSttModel?: string;
  onnxModel?: string;
  onnxQuantization?: string;
  onnxUseGpu?: boolean;
  visualizerBarCount?: number;
  apiKeys?: SettingsApiKeys;
}

export interface DiarizationComponentStatus {
  apiVersion: number;
  available: boolean;
  enabled: boolean;
  installed: boolean;
  verificationState?: "pending" | "verified" | "failed" | string;
  reason?: string | null;
  engine: "sherpa-onnx" | string;
  version: string;
  worker?: string;
  workerVersion?: string;
  workerReady?: boolean;
  workerSource?: string | null;
  workerByteSize?: number;
  distribution?: string;
  activeJobs?: number;
  maxEligibleDurationMs?: number;
  segmentationModel: string;
  embeddingModel: string;
  byteSize: number;
  license: string;
}
