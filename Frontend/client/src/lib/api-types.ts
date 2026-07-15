export type TranscriptStatus = "completed" | "processing" | "failed" | "recording" | "stopped";
export type SummaryStatus = "idle" | "pending" | "completed" | "failed";

export const REST_API_VERSION = "1";

export type TranscriptType = "mic" | "file" | "youtube" | "meeting";

export type MeetingState =
  | "starting" | "recording" | "paused" | "stopping" | "finalizing" | "analyzing"
  | "ready" | "capture_failed" | "finalization_failed" | "analysis_failed"
  | "interrupted" | "discarded";

export type MeetingTranscriptionMode = "live_final" | "final_only";

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
  calendarEvent?: OutlookCalendarEvent;
}

export interface MeetingSummary {
  id: string;
  title: string;
  state: MeetingState;
  language: string;
  transcriptionMode: MeetingTranscriptionMode;
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
    displayNameSource?: string;
    sourceHint: string;
    profileId: string | null;
    confidence: number | null;
    voiceMatch?: {
      profileId: string;
      displayName: string;
      confidence: number | null;
      evidenceCount: number;
      matchState: string;
      canPreselect: boolean;
      requiresConfirmation: true;
    } | null;
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
  reprocessing?: {
    speakerIdentityAvailable: boolean;
    speakerIdentityUnavailableReason?: string;
    fullTranscriptAvailable: boolean;
    fullTranscriptUnavailableReason?: string;
    unavailableReason: string;
    selectedFinalProvider: string;
    selectedFinalModel: string;
    voiceLibraryEnabledForRun?: boolean;
    processingRunning: boolean;
    speakerIdentityRunning: boolean;
  };
  processingComponents?: {
    diarization: MeetingProcessingComponent;
    vad: MeetingProcessingComponent;
    turnDetection: MeetingProcessingComponent;
  };
}

export interface MeetingProcessingComponent {
  used: boolean;
  engine: string;
  model: string;
  mode: string;
  analysisCount?: number;
  failureCount?: number;
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
  transcriptionMode: MeetingTranscriptionMode;
  liveProvider: string;
  livePreviewAvailable?: boolean;
  livePreviewWarning?: string;
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
  costEstimate: {
    currency: "USD";
    pricingUpdatedAt: string;
    audioTrackAssumption: number;
    livePreviewPerMeetingHour: number;
    livePerMeetingHour: number;
    finalPerMeetingHour: number | null;
    singleTrackFinalPerAudioHour: number | null;
    totalPerMeetingHour: number | null;
    estimateKind: string;
    sources: Array<{ label: string; url: string }>;
    assumption: string;
  };
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
  source: "rust-wasapi" | "rust-wasapi+pycaw-fallback" | "pycaw-fallback" | "unavailable";
  partial: boolean;
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

export interface OutlookCalendarContact {
  participantId?: string;
  name: string;
  address: string;
  aliases?: string[];
  isCurrentUser?: boolean;
  type?: "required" | "optional" | "resource" | string;
  response?: "none" | "organizer" | "tentativelyAccepted" | "accepted" | "declined" | "notResponded" | string;
}

export interface OutlookCalendarEvent {
  id: string;
  subject: string;
  start_at: string;
  end_at: string;
  join_url: string;
  organizer: OutlookCalendarContact | null;
  participants: OutlookCalendarContact[];
  currentUser?: OutlookCalendarContact | null;
  isCurrentUserOrganizer?: boolean;
  etag?: string;
  location?: string;
  isAllDay?: boolean;
  lastModifiedAt?: string;
  syncedAt?: string;
  calendarSyncedAt?: string;
  snapshotCreatedAt?: string;
}

export interface OutlookCalendarStatus {
  apiVersion: typeof REST_API_VERSION;
  configured: boolean;
  connected: boolean;
  credentialStatusAvailable: boolean;
  authorizationPending: boolean;
  reauthRequired: boolean;
  scopes: string[];
  lastSyncAt: string;
  lastError: string;
  account: OutlookCalendarContact | null;
  nextEvent: OutlookCalendarEvent | null;
}

export interface OutlookCalendarSyncResponse extends OutlookCalendarStatus {
  changed: number;
}

export interface OutlookCalendarEventsResponse {
  apiVersion: typeof REST_API_VERSION;
  date: string;
  timeZone: string;
  lastSyncAt: string;
  account: OutlookCalendarContact | null;
  truncated: boolean;
  items: OutlookCalendarEvent[];
}

export type MeetingSpeakerSuggestionSource = "account" | "voice_profile" | "llm";

export interface MeetingSpeakerSuggestion {
  attendee: OutlookCalendarContact;
  source: MeetingSpeakerSuggestionSource;
  confidence: number | null;
  reason: string;
  requiresConfirmation?: boolean;
}

export interface MeetingSpeakerAssignment {
  speakerId: string;
  speakerLabel: string;
  currentDisplayName: string;
  sourceHint?: string;
  profileId?: string | null;
  /** Canonical Voice Library label; never an Outlook/custom Meeting label. */
  profileDisplayName?: string | null;
  profileIsNamed?: boolean;
  profileMatch: {
    profileId: string;
    displayName: string;
    confidence: number | null;
    /** Newer backends explain whether a match is safe to preselect. */
    matchState?: string;
    canPreselect?: boolean;
    evidenceCount?: number;
  } | null;
  suggestions: MeetingSpeakerSuggestion[];
  confirmedAttendee: OutlookCalendarContact | null;
  /** A confirmed meeting-only label, never an Outlook identity or recipient. */
  confirmedCustomName?: string | null;
  participantLinkSource?: "manual" | "custom_name" | MeetingSpeakerSuggestionSource | string;
}

export interface MeetingSpeakerAssignmentsResponse {
  apiVersion: typeof REST_API_VERSION;
  calendarEvent: OutlookCalendarEvent | null;
  items: MeetingSpeakerAssignment[];
  requiresConfirmation: true;
  llmSuggestionAvailable: boolean;
  llmModel?: string;
  llmRequested?: boolean;
  privacy?: string;
}

export interface MeetingSpeakerAssignmentUpdate {
  speakerId: string;
  displayName: string;
  confirmedAttendee: OutlookCalendarContact | null;
  customDisplayName?: string | null;
  source: "" | "manual" | "custom_name" | MeetingSpeakerSuggestionSource | string;
  confirmedAt: string;
}

export interface MeetingSpeakerAssignmentConfirmationResponse {
  apiVersion: typeof REST_API_VERSION;
  assignment: MeetingSpeakerAssignmentUpdate;
  requiresConfirmation: false;
}

export interface SpeakerProfileSummary {
  id: string;
  displayName: string;
  sampleCount: number;
  isNamed: boolean;
  enrolled: boolean;
  enrollmentSampleCount: number;
  enrolledAt: string;
  createdAt: string;
  updatedAt: string;
  /** Optional short, token-protected sample; embeddings never leave the backend. */
  preview?: {
    token: string;
    url: string;
    startMs: number;
    endMs: number;
    durationMs: number;
    source: string;
    expiresInSeconds?: number;
  } | null;
}

export interface SpeakerProfilesResponse {
  apiVersion: typeof REST_API_VERSION;
  enabled: boolean;
  items: SpeakerProfileSummary[];
  message: string;
}

export interface SpeakerEnrollmentResponse {
  apiVersion: typeof REST_API_VERSION;
  profile: SpeakerProfileSummary;
  capture: {
    durationMs: number;
    rms: number;
    peak: number;
    quality: number;
  };
  audioPersisted: false;
  audioSentToProvider: false;
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
  voiceEnrollmentActive: boolean;
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

export interface LiveMicStopRequestResponse {
  apiVersion: typeof REST_API_VERSION;
  stopAccepted: boolean;
  stopScheduled: boolean;
  alreadyFinalizing: boolean;
  alreadyStopped: boolean;
  finalizing: boolean;
  sessionId: string | null;
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

export interface FrontendLongTaskEntry {
  sequence: number;
  startTimeMs: number;
  durationMs: number;
}

export interface FrontendPerformanceReportRequest {
  apiVersion: typeof REST_API_VERSION;
  sourceInstanceId: string;
  observerSupported: boolean;
  windowStartedAtMs: number;
  observedAtMs: number;
  droppedEntries: number;
  heartbeatSequence: number;
  entries: FrontendLongTaskEntry[];
}

export interface FrontendPerformanceWindow {
  startedAtFrontendUptimeMs: number;
  observedAtFrontendUptimeMs: number;
  receivedAtUptimeSeconds: number;
  queryAfterSequence: number | null;
  count: number;
  cumulativeCount: number;
  maxDurationMs: number;
  totalDurationMs: number;
  lastSequence: number;
  droppedEntries: number;
  sequenceGaps: number;
  retainedEntries: number;
  heartbeatSequence: number;
  heartbeatObservedAtFrontendUptimeMs: number | null;
  heartbeatReceivedAtUptimeSeconds: number | null;
  truncated: boolean;
}

export interface FrontendPerformanceFlushRequest {
  apiVersion: typeof REST_API_VERSION;
  sourceInstanceId: string;
}

export interface FrontendPerformanceFlushResponse {
  apiVersion: typeof REST_API_VERSION;
  accepted: boolean;
  sourceInstanceId: string;
  heartbeatSequence: number;
  requestedAfterFrontendUptimeMs: number;
  requestedAtUptimeSeconds: number;
}

export interface FrontendPerformanceResponse {
  apiVersion: typeof REST_API_VERSION;
  available: boolean;
  reason: "not_reported" | "source_instance_changed" | null;
  observerSupported: boolean | null;
  sourceInstanceId: string | null;
  window: FrontendPerformanceWindow | null;
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
  modulate?: string;
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
  sonioxRealtimeModel?: string;
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
  meetingTranscriptionMode?: MeetingTranscriptionMode;
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
  meetingTranscriptionMode?: MeetingTranscriptionMode;
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
