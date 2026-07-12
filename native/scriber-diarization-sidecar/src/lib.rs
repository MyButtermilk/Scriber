use hound::{SampleFormat, WavReader};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sherpa_onnx::{
    FastClusteringConfig, OfflineSpeakerDiarization, OfflineSpeakerDiarizationConfig,
    OfflineSpeakerSegmentationModelConfig, OfflineSpeakerSegmentationPyannoteModelConfig,
    SpeakerEmbeddingExtractorConfig,
};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{self, BufRead, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE},
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_PROCESS_MEMORY,
        },
        Threading::GetCurrentProcess,
    },
};

pub const PROTOCOL_SCHEMA_VERSION: u32 = 1;
pub const WORKER_NAME: &str = "scriber-diarization-sidecar";
pub const WORKER_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SHERPA_ONNX_VERSION: &str = "1.13.3";

const SEGMENTATION_MODEL_ID: &str = "pyannote-segmentation-3.0-int8";
const EMBEDDING_MODEL_ID: &str = "3d-speaker-eres2net-base-16k";
const AUDIO_ROOT_ENV: &str = "SCRIBER_DIARIZATION_JOB_ROOT";
const COMPONENT_ROOT_ENV: &str = "SCRIBER_DIARIZATION_COMPONENT_ROOT";
const REQUIRED_SAMPLE_RATE: u32 = 16_000;
const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_OUTPUT_BYTES: usize = 8 * 1024 * 1024;
const MAX_TURNS: usize = 100_000;
const MAX_DURATION_MS: u64 = 2 * 60 * 60 * 1000;
const MIN_RESIDENT_BYTES: u64 = 512 * 1024 * 1024;
const MAX_RESIDENT_BYTES: u64 = 1024 * 1024 * 1024;
const AUDIO_ALLOCATION_HEADROOM_BYTES: u64 = 384 * 1024 * 1024;
const MAX_WAV_BYTES: u64 = 300 * 1024 * 1024;
const MIN_MODEL_BYTES: u64 = 1024;
const MAX_MODEL_BYTES: u64 = 512 * 1024 * 1024;
const MAX_SPEAKERS: u16 = 64;
const NUM_THREADS: i32 = 2;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DiarizationRequest {
    schema_version: u32,
    job_id: String,
    audio_path: PathBuf,
    segmentation_model_path: PathBuf,
    embedding_model_path: PathBuf,
    clustering: ClusteringRequest,
    limits: LimitsRequest,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ClusteringRequest {
    num_speakers: Option<u16>,
    threshold: f32,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct LimitsRequest {
    max_duration_ms: u64,
    max_resident_bytes: u64,
}

#[derive(Debug)]
struct ValidatedRequest {
    job_id: String,
    audio_path: PathBuf,
    segmentation_model_path: PathBuf,
    embedding_model_path: PathBuf,
    num_speakers: Option<u16>,
    threshold: f32,
    max_resident_bytes: u64,
    duration_ms: u64,
    sample_count: u64,
}

#[derive(Debug)]
struct AllowedRoots {
    audio: PathBuf,
    component: PathBuf,
}

#[derive(Clone, Copy, Debug)]
struct WorkerError {
    code: &'static str,
    message: &'static str,
}

impl WorkerError {
    const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ErrorResponse<'a> {
    schema_version: u32,
    job_id: Option<&'a str>,
    ok: bool,
    error: ErrorPayload,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ErrorPayload {
    code: &'static str,
    message: &'static str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct SuccessResponse<'a> {
    schema_version: u32,
    job_id: &'a str,
    ok: bool,
    worker: WorkerIdentity,
    engine: EngineIdentity,
    models: ModelIdentities,
    sample_rate: u32,
    duration_ms: u64,
    speaker_count: usize,
    turns: Vec<Turn>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct WorkerIdentity {
    name: &'static str,
    version: &'static str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct EngineIdentity {
    name: &'static str,
    version: &'static str,
    link_mode: &'static str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ModelIdentities {
    segmentation: &'static str,
    embedding: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
struct Turn {
    start_ms: u64,
    end_ms: u64,
    speaker: usize,
}

#[derive(Clone, Copy, Debug)]
struct RawTurn {
    start_seconds: f32,
    end_seconds: f32,
    speaker: i32,
}

/// Runs the worker command-line contract. Normal mode processes exactly one
/// bounded JSON line and writes exactly one JSON response line.
pub fn run_cli() -> ExitCode {
    std::panic::set_hook(Box::new(|_| {
        // Never let panic metadata reveal a local source or user path.
    }));

    let args = env::args().skip(1).collect::<Vec<_>>();
    match args.as_slice() {
        [arg] if arg == "--version" => write_control_response(version_payload()),
        [arg] if arg == "--self-test" => write_control_response(self_test_payload()),
        [] => run_single_request(),
        [arg] if arg == "--stdio" => run_single_request(),
        [arg] if arg == "--help" || arg == "-h" => write_control_response(help_payload()),
        _ => write_control_response(error_value(
            None,
            WorkerError::new("unsupported_argument", "unsupported worker argument"),
        )),
    }
}

fn run_single_request() -> ExitCode {
    if let Err(error) = suppress_native_stderr() {
        return write_control_response(error_value(None, error));
    }
    let input = match read_request_line() {
        Ok(input) => input,
        Err(error) => return write_control_response(error_value(None, error)),
    };
    let job_id = extract_safe_job_id(&input);
    let outcome = std::panic::catch_unwind(|| process_request(&input));
    let payload = match outcome {
        Ok(Ok(payload)) => payload,
        Ok(Err(error)) => error_value(job_id.as_deref(), error),
        Err(_) => error_value(
            job_id.as_deref(),
            WorkerError::new("internal_error", "worker execution failed"),
        ),
    };
    write_control_response(payload)
}

#[cfg(windows)]
fn suppress_native_stderr() -> Result<(), WorkerError> {
    // Sherpa is configured with debug=false, but native dependency failures may
    // still write their input path to the C runtime's stderr. The worker never
    // uses stderr as a data channel, so close that leak before accepting input.
    let null_device = b"NUL\0";
    const STDERR_FD: libc::c_int = 2;
    unsafe {
        let null_fd = libc::open(null_device.as_ptr().cast(), libc::O_WRONLY);
        if null_fd < 0 {
            return Err(WorkerError::new(
                "diagnostic_redaction_unavailable",
                "worker diagnostics could not be secured",
            ));
        }
        let duplicated = libc::dup2(null_fd, STDERR_FD);
        let _ = libc::close(null_fd);
        if duplicated < 0 {
            return Err(WorkerError::new(
                "diagnostic_redaction_unavailable",
                "worker diagnostics could not be secured",
            ));
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn suppress_native_stderr() -> Result<(), WorkerError> {
    Ok(())
}

fn read_request_line() -> Result<Vec<u8>, WorkerError> {
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut input = Vec::new();
    let bytes = reader
        .by_ref()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_until(b'\n', &mut input)
        .map_err(|_| WorkerError::new("input_error", "worker request could not be read"))?;
    if bytes == 0 {
        return Err(WorkerError::new(
            "empty_request",
            "worker request is required",
        ));
    }
    if input.len() > MAX_REQUEST_BYTES {
        return Err(WorkerError::new(
            "request_too_large",
            "worker request exceeds the size limit",
        ));
    }
    while matches!(input.last(), Some(b'\n' | b'\r')) {
        input.pop();
    }
    if input.is_empty() {
        return Err(WorkerError::new(
            "empty_request",
            "worker request is required",
        ));
    }
    Ok(input)
}

fn process_request(input: &[u8]) -> Result<Value, WorkerError> {
    let request: DiarizationRequest = serde_json::from_slice(input)
        .map_err(|_| WorkerError::new("invalid_json", "worker request is invalid"))?;
    validate_scalar_fields(&request)?;
    let roots = roots_from_environment()?;
    let validated = validate_request(request, &roots)?;

    let _memory_limit = ProcessMemoryLimit::apply(validated.max_resident_bytes)?;
    let samples = read_wave_samples(&validated)?;
    let raw_turns = run_diarization(&validated, &samples)?;
    let turns = normalize_turns(raw_turns, validated.duration_ms)?;
    if turns.len() > MAX_TURNS {
        return Err(WorkerError::new(
            "result_limit_exceeded",
            "diarization result exceeds the turn limit",
        ));
    }
    let speaker_count = turns
        .iter()
        .map(|turn| turn.speaker)
        .max()
        .map_or(0, |speaker| speaker + 1);
    if speaker_count > usize::from(MAX_SPEAKERS) {
        return Err(WorkerError::new(
            "result_limit_exceeded",
            "diarization result exceeds the speaker limit",
        ));
    }
    let payload = SuccessResponse {
        schema_version: PROTOCOL_SCHEMA_VERSION,
        job_id: &validated.job_id,
        ok: true,
        worker: worker_identity(),
        engine: engine_identity(),
        models: ModelIdentities {
            segmentation: SEGMENTATION_MODEL_ID,
            embedding: EMBEDDING_MODEL_ID,
        },
        sample_rate: REQUIRED_SAMPLE_RATE,
        duration_ms: validated.duration_ms,
        speaker_count,
        turns,
    };
    let value = serde_json::to_value(payload)
        .map_err(|_| WorkerError::new("internal_error", "worker execution failed"))?;
    let encoded = serde_json::to_vec(&value)
        .map_err(|_| WorkerError::new("internal_error", "worker execution failed"))?;
    if encoded.len() > MAX_OUTPUT_BYTES {
        return Err(WorkerError::new(
            "result_limit_exceeded",
            "diarization result exceeds the output limit",
        ));
    }
    Ok(value)
}

fn validate_scalar_fields(request: &DiarizationRequest) -> Result<(), WorkerError> {
    if request.schema_version != PROTOCOL_SCHEMA_VERSION {
        return Err(WorkerError::new(
            "unsupported_schema",
            "worker schema version is unsupported",
        ));
    }
    if !is_safe_job_id(&request.job_id) {
        return Err(WorkerError::new(
            "invalid_job_id",
            "worker job id is invalid",
        ));
    }
    if !(0.0..=1.0).contains(&request.clustering.threshold) || request.clustering.threshold == 0.0 {
        return Err(WorkerError::new(
            "invalid_clustering",
            "clustering settings are invalid",
        ));
    }
    if request
        .clustering
        .num_speakers
        .is_some_and(|count| count == 0 || count > MAX_SPEAKERS)
    {
        return Err(WorkerError::new(
            "invalid_clustering",
            "clustering settings are invalid",
        ));
    }
    if request.limits.max_duration_ms == 0
        || request.limits.max_duration_ms > MAX_DURATION_MS
        || request.limits.max_resident_bytes < MIN_RESIDENT_BYTES
        || request.limits.max_resident_bytes > MAX_RESIDENT_BYTES
    {
        return Err(WorkerError::new(
            "invalid_limits",
            "worker limits are invalid",
        ));
    }
    Ok(())
}

fn roots_from_environment() -> Result<AllowedRoots, WorkerError> {
    let audio = canonical_root(AUDIO_ROOT_ENV, "audio_root_unavailable")?;
    let component = canonical_root(COMPONENT_ROOT_ENV, "component_root_unavailable")?;
    Ok(AllowedRoots { audio, component })
}

fn canonical_root(name: &str, code: &'static str) -> Result<PathBuf, WorkerError> {
    let raw = env::var_os(name)
        .ok_or_else(|| WorkerError::new(code, "required worker root is unavailable"))?;
    let path = PathBuf::from(raw);
    if !path.is_absolute() {
        return Err(WorkerError::new(
            code,
            "required worker root is unavailable",
        ));
    }
    let canonical = fs::canonicalize(path)
        .map_err(|_| WorkerError::new(code, "required worker root is unavailable"))?;
    if !canonical.is_dir() {
        return Err(WorkerError::new(
            code,
            "required worker root is unavailable",
        ));
    }
    Ok(canonical)
}

fn validate_request(
    request: DiarizationRequest,
    roots: &AllowedRoots,
) -> Result<ValidatedRequest, WorkerError> {
    let audio_path = validate_file_under_root(
        &request.audio_path,
        &roots.audio,
        "wav",
        1,
        MAX_WAV_BYTES,
        "invalid_audio_path",
    )?;
    let segmentation_model_path = validate_file_under_root(
        &request.segmentation_model_path,
        &roots.component,
        "onnx",
        MIN_MODEL_BYTES,
        MAX_MODEL_BYTES,
        "invalid_model_path",
    )?;
    let embedding_model_path = validate_file_under_root(
        &request.embedding_model_path,
        &roots.component,
        "onnx",
        MIN_MODEL_BYTES,
        MAX_MODEL_BYTES,
        "invalid_model_path",
    )?;
    if segmentation_model_path == embedding_model_path {
        return Err(WorkerError::new(
            "invalid_model_path",
            "worker model path is invalid",
        ));
    }
    let (duration_ms, sample_count) = inspect_wave(&audio_path)?;
    if duration_ms == 0 || duration_ms > request.limits.max_duration_ms {
        return Err(WorkerError::new(
            "audio_duration_exceeded",
            "audio duration exceeds the worker limit",
        ));
    }
    let sample_bytes = sample_count
        .checked_mul(std::mem::size_of::<f32>() as u64)
        .ok_or_else(|| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    let minimum_resident = sample_bytes
        .checked_add(AUDIO_ALLOCATION_HEADROOM_BYTES)
        .ok_or_else(|| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    if minimum_resident > request.limits.max_resident_bytes {
        return Err(WorkerError::new(
            "memory_budget_too_small",
            "worker memory limit is too small for this audio",
        ));
    }

    Ok(ValidatedRequest {
        job_id: request.job_id,
        audio_path,
        segmentation_model_path,
        embedding_model_path,
        num_speakers: request.clustering.num_speakers,
        threshold: request.clustering.threshold,
        max_resident_bytes: request.limits.max_resident_bytes,
        duration_ms,
        sample_count,
    })
}

fn validate_file_under_root(
    requested: &Path,
    root: &Path,
    extension: &str,
    min_bytes: u64,
    max_bytes: u64,
    error_code: &'static str,
) -> Result<PathBuf, WorkerError> {
    let error = || WorkerError::new(error_code, "worker file path is invalid");
    if !requested.is_absolute() {
        return Err(error());
    }
    let canonical = fs::canonicalize(requested).map_err(|_| error())?;
    if !canonical.starts_with(root) {
        return Err(error());
    }
    if canonical
        .extension()
        .and_then(|value| value.to_str())
        .is_none_or(|value| !value.eq_ignore_ascii_case(extension))
    {
        return Err(error());
    }
    let metadata = fs::metadata(&canonical).map_err(|_| error())?;
    if !metadata.is_file() || metadata.len() < min_bytes || metadata.len() > max_bytes {
        return Err(error());
    }
    Ok(canonical)
}

fn inspect_wave(path: &Path) -> Result<(u64, u64), WorkerError> {
    let reader = WavReader::open(path)
        .map_err(|_| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.sample_rate != REQUIRED_SAMPLE_RATE
        || spec.bits_per_sample != 16
        || spec.sample_format != SampleFormat::Int
    {
        return Err(WorkerError::new(
            "unsupported_audio_format",
            "audio must be mono 16-bit PCM at 16 kHz",
        ));
    }
    let sample_count = u64::from(reader.duration());
    let duration_ms = sample_count
        .checked_mul(1000)
        .and_then(|value| value.checked_add(u64::from(REQUIRED_SAMPLE_RATE / 2)))
        .map(|value| value / u64::from(REQUIRED_SAMPLE_RATE))
        .ok_or_else(|| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    Ok((duration_ms, sample_count))
}

fn read_wave_samples(request: &ValidatedRequest) -> Result<Vec<f32>, WorkerError> {
    let capacity = usize::try_from(request.sample_count)
        .map_err(|_| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    let mut reader = WavReader::open(&request.audio_path)
        .map_err(|_| WorkerError::new("invalid_audio", "audio input is invalid"))?;
    let mut samples = Vec::with_capacity(capacity);
    for sample in reader.samples::<i16>() {
        let sample =
            sample.map_err(|_| WorkerError::new("invalid_audio", "audio input is invalid"))?;
        samples.push(f32::from(sample) / 32768.0);
        if samples.len() > capacity {
            return Err(WorkerError::new("invalid_audio", "audio input is invalid"));
        }
    }
    if samples.len() != capacity {
        return Err(WorkerError::new("invalid_audio", "audio input is invalid"));
    }
    Ok(samples)
}

fn run_diarization(
    request: &ValidatedRequest,
    samples: &[f32],
) -> Result<Vec<RawTurn>, WorkerError> {
    let segmentation_model = request
        .segmentation_model_path
        .to_str()
        .ok_or_else(|| WorkerError::new("invalid_model_path", "worker model path is invalid"))?;
    let embedding_model = request
        .embedding_model_path
        .to_str()
        .ok_or_else(|| WorkerError::new("invalid_model_path", "worker model path is invalid"))?;
    let config = OfflineSpeakerDiarizationConfig {
        segmentation: OfflineSpeakerSegmentationModelConfig {
            pyannote: OfflineSpeakerSegmentationPyannoteModelConfig {
                model: Some(segmentation_model.to_owned()),
            },
            num_threads: NUM_THREADS,
            debug: false,
            provider: Some("cpu".to_owned()),
        },
        embedding: SpeakerEmbeddingExtractorConfig {
            model: Some(embedding_model.to_owned()),
            num_threads: NUM_THREADS,
            debug: false,
            provider: Some("cpu".to_owned()),
        },
        clustering: FastClusteringConfig {
            num_clusters: request.num_speakers.map_or(-1, i32::from),
            threshold: request.threshold,
        },
        ..Default::default()
    };
    let diarizer = OfflineSpeakerDiarization::create(&config).ok_or_else(|| {
        WorkerError::new(
            "engine_initialization_failed",
            "speaker diarization engine could not be initialized",
        )
    })?;
    if diarizer.sample_rate() != REQUIRED_SAMPLE_RATE as i32 {
        return Err(WorkerError::new(
            "model_sample_rate_mismatch",
            "speaker diarization model sample rate is unsupported",
        ));
    }
    let result = diarizer.process(samples).ok_or_else(|| {
        WorkerError::new(
            "diarization_failed",
            "speaker diarization could not be completed",
        )
    })?;
    if result.num_segments() < 0 || result.num_segments() as usize > MAX_TURNS {
        return Err(WorkerError::new(
            "result_limit_exceeded",
            "diarization result exceeds the turn limit",
        ));
    }
    Ok(result
        .sort_by_start_time()
        .into_iter()
        .map(|segment| RawTurn {
            start_seconds: segment.start,
            end_seconds: segment.end,
            speaker: segment.speaker,
        })
        .collect())
}

fn normalize_turns(raw_turns: Vec<RawTurn>, duration_ms: u64) -> Result<Vec<Turn>, WorkerError> {
    let mut raw_turns = raw_turns;
    raw_turns.sort_by(|left, right| {
        left.start_seconds
            .total_cmp(&right.start_seconds)
            .then_with(|| left.end_seconds.total_cmp(&right.end_seconds))
            .then_with(|| left.speaker.cmp(&right.speaker))
    });
    let mut speaker_ids = HashMap::<i32, usize>::new();
    let mut turns = Vec::with_capacity(raw_turns.len());
    for raw in raw_turns {
        if !raw.start_seconds.is_finite()
            || !raw.end_seconds.is_finite()
            || raw.start_seconds < 0.0
            || raw.end_seconds <= raw.start_seconds
            || raw.speaker < 0
        {
            return Err(WorkerError::new(
                "invalid_engine_result",
                "speaker diarization returned an invalid result",
            ));
        }
        let start_ms = seconds_to_ms(raw.start_seconds).min(duration_ms);
        let mut end_ms = seconds_to_ms(raw.end_seconds).min(duration_ms);
        if start_ms >= duration_ms {
            continue;
        }
        if end_ms <= start_ms {
            end_ms = start_ms + 1;
        }
        let next_speaker = speaker_ids.len();
        let speaker = *speaker_ids.entry(raw.speaker).or_insert(next_speaker);
        turns.push(Turn {
            start_ms,
            end_ms,
            speaker,
        });
        if turns.len() > MAX_TURNS {
            return Err(WorkerError::new(
                "result_limit_exceeded",
                "diarization result exceeds the turn limit",
            ));
        }
    }
    Ok(turns)
}

fn seconds_to_ms(seconds: f32) -> u64 {
    let millis = f64::from(seconds) * 1000.0;
    if millis >= u64::MAX as f64 {
        u64::MAX
    } else {
        millis.round() as u64
    }
}

fn extract_safe_job_id(input: &[u8]) -> Option<String> {
    serde_json::from_slice::<Value>(input)
        .ok()
        .and_then(|value| {
            value
                .get("jobId")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .filter(|job_id| is_safe_job_id(job_id))
}

fn is_safe_job_id(job_id: &str) -> bool {
    !job_id.is_empty()
        && job_id.len() <= 128
        && job_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
}

fn error_value(job_id: Option<&str>, error: WorkerError) -> Value {
    serde_json::to_value(ErrorResponse {
        schema_version: PROTOCOL_SCHEMA_VERSION,
        job_id,
        ok: false,
        error: ErrorPayload {
            code: error.code,
            message: error.message,
        },
    })
    .unwrap_or_else(|_| {
        json!({
            "schemaVersion": PROTOCOL_SCHEMA_VERSION,
            "jobId": null,
            "ok": false,
            "error": {"code": "internal_error", "message": "worker execution failed"}
        })
    })
}

fn worker_identity() -> WorkerIdentity {
    WorkerIdentity {
        name: WORKER_NAME,
        version: WORKER_VERSION,
    }
}

fn engine_identity() -> EngineIdentity {
    EngineIdentity {
        name: "sherpa-onnx",
        version: SHERPA_ONNX_VERSION,
        link_mode: "static",
    }
}

fn version_payload() -> Value {
    json!({
        "schemaVersion": PROTOCOL_SCHEMA_VERSION,
        "ok": true,
        "worker": worker_identity(),
        "engine": engine_identity(),
    })
}

fn self_test_payload() -> Value {
    json!({
        "schemaVersion": PROTOCOL_SCHEMA_VERSION,
        "ok": cfg!(windows),
        "worker": worker_identity(),
        "engine": engine_identity(),
        "platform": {
            "windows": cfg!(windows),
            "memoryLimit": if cfg!(windows) { "jobObject" } else { "unsupported" },
        },
        "protocol": {
            "mode": "single-json-line",
            "maxRequestBytes": MAX_REQUEST_BYTES,
            "maxOutputBytes": MAX_OUTPUT_BYTES,
            "maxTurns": MAX_TURNS,
            "maxDurationMs": MAX_DURATION_MS,
            "maxResidentBytes": MAX_RESIDENT_BYTES,
        },
        "loadsUserAudio": false,
        "loadsModels": false,
    })
}

fn help_payload() -> Value {
    json!({
        "schemaVersion": PROTOCOL_SCHEMA_VERSION,
        "ok": true,
        "worker": worker_identity(),
        "commands": ["--version", "--self-test", "--stdio"],
    })
}

fn write_control_response(payload: Value) -> ExitCode {
    let mut encoded = match serde_json::to_vec(&payload) {
        Ok(encoded) if encoded.len() <= MAX_OUTPUT_BYTES => encoded,
        _ => serde_json::to_vec(&error_value(
            None,
            WorkerError::new(
                "result_limit_exceeded",
                "worker response exceeds the output limit",
            ),
        ))
        .unwrap_or_else(|_| b"{\"schemaVersion\":1,\"ok\":false}".to_vec()),
    };
    encoded.push(b'\n');
    let mut stdout = BufWriter::new(io::stdout().lock());
    if stdout
        .write_all(&encoded)
        .and_then(|_| stdout.flush())
        .is_err()
    {
        return ExitCode::from(2);
    }
    if payload.get("ok").and_then(Value::as_bool) == Some(true) {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

#[cfg(windows)]
struct ProcessMemoryLimit {
    handle: HANDLE,
}

#[cfg(windows)]
impl ProcessMemoryLimit {
    fn apply(max_resident_bytes: u64) -> Result<Self, WorkerError> {
        let process_memory_limit = usize::try_from(max_resident_bytes)
            .map_err(|_| WorkerError::new("invalid_limits", "worker limits are invalid"))?;
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(WorkerError::new(
                    "memory_limit_unavailable",
                    "worker memory limit could not be applied",
                ));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY;
            info.ProcessMemoryLimit = process_memory_limit;
            let configured = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if configured == 0 || AssignProcessToJobObject(handle, GetCurrentProcess()) == 0 {
                let _ = CloseHandle(handle);
                return Err(WorkerError::new(
                    "memory_limit_unavailable",
                    "worker memory limit could not be applied",
                ));
            }
            Ok(Self { handle })
        }
    }
}

#[cfg(windows)]
impl Drop for ProcessMemoryLimit {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.handle);
        }
    }
}

#[cfg(not(windows))]
struct ProcessMemoryLimit;

#[cfg(not(windows))]
impl ProcessMemoryLimit {
    fn apply(_max_resident_bytes: u64) -> Result<Self, WorkerError> {
        Err(WorkerError::new(
            "unsupported_platform",
            "worker platform is unsupported",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use tempfile::TempDir;

    fn request_json(audio: &Path, segmentation: &Path, embedding: &Path) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "jobId": "meeting-import:abc-123",
            "audioPath": audio,
            "segmentationModelPath": segmentation,
            "embeddingModelPath": embedding,
            "clustering": {"numSpeakers": null, "threshold": 0.9},
            "limits": {"maxDurationMs": 7_200_000, "maxResidentBytes": 1_073_741_824_u64}
        }))
        .unwrap()
    }

    fn fixture_roots() -> (TempDir, AllowedRoots, PathBuf, PathBuf, PathBuf) {
        let temp = TempDir::new().unwrap();
        let audio_root = temp.path().join("jobs");
        let component_root = temp.path().join("component");
        fs::create_dir_all(&audio_root).unwrap();
        fs::create_dir_all(&component_root).unwrap();
        let audio = audio_root.join("track.wav");
        let segmentation = component_root.join("segmentation.onnx");
        let embedding = component_root.join("embedding.onnx");
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: REQUIRED_SAMPLE_RATE,
            bits_per_sample: 16,
            sample_format: SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&audio, spec).unwrap();
        for _ in 0..REQUIRED_SAMPLE_RATE {
            writer.write_sample(0_i16).unwrap();
        }
        writer.finalize().unwrap();
        File::create(&segmentation)
            .unwrap()
            .set_len(MIN_MODEL_BYTES)
            .unwrap();
        File::create(&embedding)
            .unwrap()
            .set_len(MIN_MODEL_BYTES + 1)
            .unwrap();
        let roots = AllowedRoots {
            audio: fs::canonicalize(audio_root).unwrap(),
            component: fs::canonicalize(component_root).unwrap(),
        };
        (temp, roots, audio, segmentation, embedding)
    }

    #[test]
    fn request_contract_accepts_documented_schema() {
        let (_temp, roots, audio, segmentation, embedding) = fixture_roots();
        let request: DiarizationRequest =
            serde_json::from_slice(&request_json(&audio, &segmentation, &embedding)).unwrap();
        validate_scalar_fields(&request).unwrap();
        let validated = validate_request(request, &roots).unwrap();
        assert_eq!(validated.duration_ms, 1000);
        assert_eq!(validated.sample_count, u64::from(REQUIRED_SAMPLE_RATE));
        assert_eq!(validated.threshold, 0.9);
        assert_eq!(validated.num_speakers, None);
    }

    #[test]
    fn request_contract_rejects_unknown_fields() {
        let payload = json!({
            "schemaVersion": 1,
            "jobId": "safe",
            "audioPath": "C:/job/audio.wav",
            "segmentationModelPath": "C:/component/a.onnx",
            "embeddingModelPath": "C:/component/b.onnx",
            "clustering": {"numSpeakers": null, "threshold": 0.9},
            "limits": {"maxDurationMs": 1000, "maxResidentBytes": 1_073_741_824_u64},
            "transcript": "must never enter this worker"
        });
        assert!(serde_json::from_value::<DiarizationRequest>(payload).is_err());
    }

    #[test]
    fn scalar_validation_rejects_unbounded_settings() {
        let (_temp, _roots, audio, segmentation, embedding) = fixture_roots();
        let mut request: DiarizationRequest =
            serde_json::from_slice(&request_json(&audio, &segmentation, &embedding)).unwrap();
        request.limits.max_duration_ms = MAX_DURATION_MS + 1;
        assert_eq!(
            validate_scalar_fields(&request).unwrap_err().code,
            "invalid_limits"
        );
        request.limits.max_duration_ms = MAX_DURATION_MS;
        request.clustering.num_speakers = Some(MAX_SPEAKERS + 1);
        assert_eq!(
            validate_scalar_fields(&request).unwrap_err().code,
            "invalid_clustering"
        );
        request.clustering.num_speakers = None;
        request.job_id = "unsafe/job".to_owned();
        assert_eq!(
            validate_scalar_fields(&request).unwrap_err().code,
            "invalid_job_id"
        );
    }

    #[test]
    fn paths_must_remain_below_their_canonical_roots() {
        let (temp, roots, _audio, _segmentation, _embedding) = fixture_roots();
        let outside = temp.path().join("outside.onnx");
        File::create(&outside)
            .unwrap()
            .set_len(MIN_MODEL_BYTES)
            .unwrap();
        let error = validate_file_under_root(
            &outside,
            &roots.component,
            "onnx",
            MIN_MODEL_BYTES,
            MAX_MODEL_BYTES,
            "invalid_model_path",
        )
        .unwrap_err();
        assert_eq!(error.code, "invalid_model_path");
    }

    #[test]
    fn wave_validation_is_strictly_mono_pcm16_at_16khz() {
        let (temp, _roots, audio, _segmentation, _embedding) = fixture_roots();
        assert_eq!(inspect_wave(&audio).unwrap(), (1000, 16_000));

        let wrong = temp.path().join("wrong.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 48_000,
            bits_per_sample: 16,
            sample_format: SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&wrong, spec).unwrap();
        writer.write_sample(0_i16).unwrap();
        writer.write_sample(0_i16).unwrap();
        writer.finalize().unwrap();
        assert_eq!(
            inspect_wave(&wrong).unwrap_err().code,
            "unsupported_audio_format"
        );
    }

    #[test]
    fn turns_are_sorted_clamped_and_speakers_follow_first_appearance() {
        let turns = normalize_turns(
            vec![
                RawTurn {
                    start_seconds: 1.5,
                    end_seconds: 2.0,
                    speaker: 4,
                },
                RawTurn {
                    start_seconds: 0.1,
                    end_seconds: 0.8,
                    speaker: 9,
                },
                RawTurn {
                    start_seconds: 0.9,
                    end_seconds: 1.2,
                    speaker: 4,
                },
                RawTurn {
                    start_seconds: 2.8,
                    end_seconds: 3.5,
                    speaker: 9,
                },
            ],
            3000,
        )
        .unwrap();
        assert_eq!(
            turns,
            vec![
                Turn {
                    start_ms: 100,
                    end_ms: 800,
                    speaker: 0,
                },
                Turn {
                    start_ms: 900,
                    end_ms: 1200,
                    speaker: 1,
                },
                Turn {
                    start_ms: 1500,
                    end_ms: 2000,
                    speaker: 1,
                },
                Turn {
                    start_ms: 2800,
                    end_ms: 3000,
                    speaker: 0,
                },
            ]
        );
    }

    #[test]
    fn errors_never_echo_sensitive_paths_or_text() {
        let sensitive = r#"C:\Users\Person\secret meeting.wav"#;
        let transcript = "confidential transcript sentence";
        let payload = error_value(
            Some("safe-job"),
            WorkerError::new("invalid_audio_path", "worker file path is invalid"),
        );
        let encoded = serde_json::to_string(&payload).unwrap();
        assert!(!encoded.contains(sensitive));
        assert!(!encoded.contains(transcript));
        assert!(encoded.len() < 512);
    }

    #[test]
    fn control_payloads_do_not_load_or_name_user_files() {
        let self_test = self_test_payload();
        assert_eq!(self_test["loadsUserAudio"], false);
        assert_eq!(self_test["loadsModels"], false);
        assert_eq!(self_test["engine"]["version"], SHERPA_ONNX_VERSION);
        assert!(!serde_json::to_string(&self_test).unwrap().contains("Path"));
    }
}
