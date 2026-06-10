mod audio_frame_pipe;
mod redaction;

use audio_frame_pipe::{
    encode_audio_frame, AudioFrameHeader, AUDIO_FRAME_FLAG_PREBUFFER, AUDIO_FRAME_HEADER_LEN,
    AUDIO_FRAME_VERSION,
};
use redaction::hash_sensitive_identifier;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::{
    env,
    ffi::c_void,
    io::{self, BufRead, Write},
    process::ExitCode,
    sync::{
        mpsc::{self, Sender, TryRecvError},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use uuid::Uuid;

#[cfg(windows)]
use std::ptr::{null, null_mut};

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{
        CloseHandle, GetLastError, ERROR_NO_DATA, ERROR_PIPE_CONNECTED, ERROR_PIPE_LISTENING,
        HANDLE, INVALID_HANDLE_VALUE,
    },
    Storage::FileSystem::{FlushFileBuffers, WriteFile, PIPE_ACCESS_OUTBOUND},
    System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_NOWAIT, PIPE_READMODE_BYTE,
        PIPE_TYPE_BYTE,
    },
};

#[cfg(windows)]
use windows::{
    core::GUID,
    Win32::{
        Media::Audio::{
            eCapture, eConsole, IAudioCaptureClient, IAudioClient, IMMDevice, IMMDeviceEnumerator,
            MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
            DEVICE_STATE_ACTIVE, WAVEFORMATEX, WAVEFORMATEXTENSIBLE, WAVE_FORMAT_PCM,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL,
            COINIT_MULTITHREADED,
        },
    },
};

const SIDECAR_PROTOCOL_VERSION: &str = "1";
const SIDECAR_NAME: &str = "scriber-audio-sidecar";
const SYNTHETIC_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE";
const WASAPI_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE";
const WAVE_FORMAT_IEEE_FLOAT_TAG: u16 = 3;
const WAVE_FORMAT_EXTENSIBLE_TAG: u16 = 0xfffe;
#[cfg(windows)]
const KSDATAFORMAT_SUBTYPE_PCM: GUID = GUID::from_u128(0x00000001_0000_0010_8000_00aa00389b71);
#[cfg(windows)]
const KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: GUID =
    GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("--self-test") => write_json_line(&self_test_payload()),
        Some("--stdio") => run_stdio_loop(),
        Some("--help") | Some("-h") => {
            println!("{SIDECAR_NAME} --self-test | --stdio");
            Ok(())
        }
        Some(other) => {
            eprintln!("unsupported argument: {other}");
            Err(())
        }
        None => write_json_line(&self_test_payload()),
    };
    if result.is_ok() {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn run_stdio_loop() -> Result<(), ()> {
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    let mut state = AudioSidecarState::new();
    for line in stdin.lock().lines() {
        let line = line.map_err(|_| ())?;
        let response = state.handle_sidecar_request(&line);
        writeln!(stdout, "{response}").map_err(|_| ())?;
        stdout.flush().map_err(|_| ())?;
        if response
            .get("payload")
            .and_then(|payload| payload.get("shutdown"))
            .and_then(Value::as_bool)
            == Some(true)
        {
            break;
        }
    }
    Ok(())
}

fn write_json_line(payload: &Value) -> Result<(), ()> {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{payload}").map_err(|_| ())?;
    stdout.flush().map_err(|_| ())
}

struct AudioSidecarState {
    capture_sessions: HashMap<String, CaptureSession>,
    prewarm_sessions: HashMap<String, PrewarmSession>,
}

impl AudioSidecarState {
    fn new() -> Self {
        Self {
            capture_sessions: HashMap::new(),
            prewarm_sessions: HashMap::new(),
        }
    }

    fn handle_sidecar_request(&mut self, raw: &str) -> Value {
        let started = Instant::now();
        let request = match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(map)) => map,
            Ok(_) => {
                return response_payload(
                    "",
                    false,
                    "invalidRequest",
                    "request must be an object",
                    started,
                    json!({}),
                )
            }
            Err(_) => {
                return response_payload(
                    "",
                    false,
                    "invalidJson",
                    "request must be valid JSON",
                    started,
                    json!({}),
                )
            }
        };

        let request_id = request
            .get("requestId")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let protocol_version = request
            .get("protocolVersion")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if protocol_version != SIDECAR_PROTOCOL_VERSION {
            return response_payload(
                request_id,
                false,
                "protocolVersionMismatch",
                "unsupported sidecar protocolVersion",
                started,
                json!({}),
            );
        }

        let command = request
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let payload = request.get("payload").unwrap_or(&Value::Null);
        match command {
            "ping" => response_payload(
                request_id,
                true,
                "",
                "",
                started,
                json!({"pong": true, "sidecar": SIDECAR_NAME}),
            ),
            "capabilities" => {
                response_payload(request_id, true, "", "", started, capabilities_payload())
            }
            "captureStart" => self.handle_capture_start(request_id, payload, started),
            "captureStop" => self.handle_capture_stop(request_id, payload, started),
            "prewarmStart" => self.handle_prewarm_start(request_id, payload, started),
            "prewarmStop" => self.handle_prewarm_stop(request_id, payload, started),
            "shutdown" => {
                self.stop_all_sessions("shutdown");
                response_payload(
                    request_id,
                    true,
                    "",
                    "",
                    started,
                    json!({"sidecar": SIDECAR_NAME, "shutdown": true}),
                )
            }
            _ => response_payload(
                request_id,
                false,
                "unknownCommand",
                "unsupported audio sidecar command",
                started,
                json!({}),
            ),
        }
    }

    fn handle_capture_start(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let request = CaptureRequest::from_payload(payload);
        if !wasapi_capture_enabled() && !synthetic_capture_enabled() {
            return response_payload(
                request_id,
                false,
                "audioCaptureUnavailable",
                "Rust audio capture is disabled; set SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1 for the WASAPI prototype or SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1 for the transport harness",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "captureAvailable": false,
                    "requestedFormat": request.to_payload(),
                    "audioFrameProtocol": audio_frame_protocol_payload(),
                    "wasapiCaptureAvailable": false,
                    "syntheticFramePipeAvailable": false,
                }),
            );
        }

        match self.start_capture(request) {
            Ok(payload) => response_payload(request_id, true, "", "", started, payload),
            Err(reason) => response_payload(
                request_id,
                false,
                "audioCaptureUnavailable",
                &reason,
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "captureAvailable": false,
                    "audioFrameProtocol": audio_frame_protocol_payload(),
                    "wasapiCaptureAvailable": wasapi_capture_enabled(),
                    "syntheticFramePipeAvailable": synthetic_capture_enabled(),
                }),
            ),
        }
    }

    fn handle_capture_stop(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let stream_id = bounded_string(payload, "streamId", "", 96);
        let stop_payload = if let Some(mut session) = self.capture_sessions.remove(&stream_id) {
            session.stop("captureStop")
        } else {
            json!({
                "sidecar": SIDECAR_NAME,
                "stopped": false,
                "streamId": stream_id,
                "reason": "noActiveCapture",
            })
        };
        response_payload(request_id, true, "", "", started, stop_payload)
    }

    fn handle_prewarm_start(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let request = CaptureRequest::from_payload(payload);
        if !wasapi_capture_enabled() && !synthetic_capture_enabled() {
            return response_payload(
                request_id,
                false,
                "audioPrewarmUnavailable",
                "Rust audio prewarm is disabled; set SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1 for the WASAPI prototype or SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1 for the synthetic prewarm harness",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "prewarmAvailable": false,
                    "requestedFormat": request.to_payload(),
                    "audioFrameProtocol": audio_frame_protocol_payload(),
                    "syntheticPrewarmAvailable": false,
                    "wasapiPrewarmAvailable": false,
                }),
            );
        }

        match self.start_prewarm(request) {
            Ok(payload) => response_payload(request_id, true, "", "", started, payload),
            Err(reason) => response_payload(
                request_id,
                false,
                "audioPrewarmUnavailable",
                &reason,
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "prewarmAvailable": false,
                    "audioFrameProtocol": audio_frame_protocol_payload(),
                    "syntheticPrewarmAvailable": synthetic_capture_enabled(),
                    "wasapiPrewarmAvailable": wasapi_capture_enabled(),
                }),
            ),
        }
    }

    fn handle_prewarm_stop(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let prewarm_id = bounded_string(payload, "prewarmId", "", 96);
        let stop_payload = if let Some(mut session) = self.prewarm_sessions.remove(&prewarm_id) {
            session.stop("prewarmStop")
        } else {
            json!({
                "sidecar": SIDECAR_NAME,
                "stopped": false,
                "prewarmId": prewarm_id,
                "reason": "noActivePrewarm",
            })
        };
        response_payload(request_id, true, "", "", started, stop_payload)
    }

    fn start_capture(&mut self, request: CaptureRequest) -> Result<Value, String> {
        let adopted_prewarm = if request.prewarm_id.trim().is_empty() {
            None
        } else {
            let prewarm_id = request.prewarm_id.clone();
            let Some(mut prewarm_session) = self.prewarm_sessions.remove(&prewarm_id) else {
                return Err(format!("requested prewarmId was not found: {prewarm_id}"));
            };
            let adopted = prewarm_session.snapshot_buffer();
            let stop_payload = prewarm_session.stop("adoptedIntoCapture");
            Some((adopted, stop_payload))
        };
        let result = if wasapi_capture_enabled() {
            start_wasapi_capture_impl(request, adopted_prewarm)
        } else {
            start_synthetic_capture_impl(request, adopted_prewarm)
        };
        result.map(|(session, payload)| {
            let stream_id = session.stream_id.clone();
            if let Some(mut old_session) = self.capture_sessions.remove(&stream_id) {
                let _ = old_session.stop("duplicateStreamId");
            }
            self.capture_sessions.insert(stream_id, session);
            payload
        })
    }

    fn start_prewarm(&mut self, request: CaptureRequest) -> Result<Value, String> {
        let result = if wasapi_capture_enabled() {
            start_wasapi_prewarm_impl(request)
        } else {
            start_synthetic_prewarm_impl(request)
        };
        let (session, payload) = result?;
        let prewarm_id = session.prewarm_id.clone();
        if let Some(mut old_session) = self.prewarm_sessions.remove(&prewarm_id) {
            let _ = old_session.stop("duplicatePrewarmId");
        }
        self.prewarm_sessions.insert(prewarm_id, session);
        Ok(payload)
    }

    fn stop_all_sessions(&mut self, reason: &str) {
        let sessions: Vec<CaptureSession> = self
            .capture_sessions
            .drain()
            .map(|(_, session)| session)
            .collect();
        for mut session in sessions {
            let _ = session.stop(reason);
        }
        let prewarm_sessions: Vec<PrewarmSession> = self
            .prewarm_sessions
            .drain()
            .map(|(_, session)| session)
            .collect();
        for mut session in prewarm_sessions {
            let _ = session.stop(reason);
        }
    }
}

impl Drop for AudioSidecarState {
    fn drop(&mut self) {
        self.stop_all_sessions("sidecarDrop");
    }
}

#[cfg(test)]
fn handle_sidecar_request(raw: &str) -> Value {
    let mut state = AudioSidecarState::new();
    state.handle_sidecar_request(raw)
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CaptureRequest {
    sample_rate: u32,
    channels: u16,
    block_size: u32,
    device_preference: String,
    port_audio_label: String,
    native_endpoint_id_hash: String,
    prebuffer_ms: u32,
    prewarm_id: String,
}

impl CaptureRequest {
    fn from_payload(payload: &Value) -> Self {
        Self {
            sample_rate: optional_u64(payload, "sampleRate", 16_000, 192_000).max(8_000) as u32,
            channels: optional_u64(payload, "channels", 1, 16).max(1) as u16,
            block_size: optional_u64(payload, "blockSize", 512, 16_384).max(16) as u32,
            device_preference: bounded_string(payload, "devicePreference", "default", 96),
            port_audio_label: bounded_string(payload, "portAudioLabel", "", 160),
            native_endpoint_id_hash: bounded_string(payload, "nativeEndpointIdHash", "", 64),
            prebuffer_ms: optional_u64(payload, "prebufferMs", 0, 2_000) as u32,
            prewarm_id: bounded_string(payload, "prewarmId", "", 96),
        }
    }

    fn to_payload(&self) -> Value {
        json!({
            "sampleRate": self.sample_rate,
            "channels": self.channels,
            "blockSize": self.block_size,
            "devicePreference": self.device_preference,
            "portAudioLabel": self.port_audio_label,
            "nativeEndpointIdHash": self.native_endpoint_id_hash,
            "prebufferMs": self.prebuffer_ms,
            "prewarmId": self.prewarm_id,
        })
    }
}

#[derive(Debug, Default)]
struct CaptureWriterStats {
    connected: bool,
    frames_written: u64,
    prebuffer_frames_written: u64,
    live_frames_written: u64,
    bytes_written: u64,
    error: Option<String>,
}

struct CaptureSession {
    stream_id: String,
    source: &'static str,
    stop_tx: Sender<()>,
    join_handle: Option<JoinHandle<CaptureWriterStats>>,
    started_at: Instant,
}

impl CaptureSession {
    fn stop(&mut self, reason: &str) -> Value {
        let _ = self.stop_tx.send(());
        let stats = self
            .join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| CaptureWriterStats {
                    connected: false,
                    frames_written: 0,
                    prebuffer_frames_written: 0,
                    live_frames_written: 0,
                    bytes_written: 0,
                    error: Some("writerThreadPanicked".to_string()),
                })
            })
            .unwrap_or_default();

        json!({
            "sidecar": SIDECAR_NAME,
            "stopped": true,
            "streamId": self.stream_id,
            "reason": reason,
            "source": self.source,
            "connected": stats.connected,
            "framesWritten": stats.frames_written,
            "prebufferFramesWritten": stats.prebuffer_frames_written,
            "liveFramesWritten": stats.live_frames_written,
            "bytesWritten": stats.bytes_written,
            "writerError": stats.error,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }
}

#[derive(Debug, Default)]
struct PrewarmStats {
    total_blocks_observed: u64,
    total_audio_frames_observed: u64,
    buffered_blocks: u64,
    buffered_audio_frames: u64,
    buffered_payload_bytes: u64,
    error: Option<String>,
}

#[derive(Debug, Default)]
struct PrewarmBuffer {
    max_blocks: usize,
    blocks: VecDeque<Vec<u8>>,
}

impl PrewarmBuffer {
    fn new(max_blocks: u32) -> Self {
        Self {
            max_blocks: max_blocks as usize,
            blocks: VecDeque::new(),
        }
    }

    fn push(&mut self, payload: Vec<u8>) {
        if self.max_blocks == 0 {
            return;
        }
        while self.blocks.len() >= self.max_blocks {
            self.blocks.pop_front();
        }
        self.blocks.push_back(payload);
    }

    fn snapshot(&self) -> Vec<Vec<u8>> {
        self.blocks.iter().cloned().collect()
    }

    fn block_count(&self) -> u64 {
        self.blocks.len() as u64
    }

    fn audio_frame_count(&self, block_size: u32) -> u64 {
        self.block_count().saturating_mul(u64::from(block_size))
    }

    fn payload_bytes(&self) -> u64 {
        self.blocks
            .iter()
            .map(|block| block.len() as u64)
            .sum::<u64>()
    }
}

struct PrewarmSession {
    prewarm_id: String,
    source: &'static str,
    stop_tx: Sender<()>,
    join_handle: Option<JoinHandle<PrewarmStats>>,
    started_at: Instant,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    block_size: u32,
}

impl PrewarmSession {
    fn snapshot_buffer(&self) -> AdoptedPrewarm {
        let (blocks, payload_bytes) = self
            .buffer
            .lock()
            .map(|buffer| (buffer.snapshot(), buffer.payload_bytes()))
            .unwrap_or_else(|_| (Vec::new(), 0));
        AdoptedPrewarm {
            prewarm_id: self.prewarm_id.clone(),
            source: self.source,
            block_count: blocks.len() as u64,
            audio_frame_count: (blocks.len() as u64).saturating_mul(u64::from(self.block_size)),
            payload_bytes,
            blocks,
        }
    }

    fn stop(&mut self, reason: &str) -> Value {
        let _ = self.stop_tx.send(());
        let stats = self
            .join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| PrewarmStats {
                    error: Some("prewarmThreadPanicked".to_string()),
                    ..PrewarmStats::default()
                })
            })
            .unwrap_or_default();

        json!({
            "sidecar": SIDECAR_NAME,
            "stopped": true,
            "prewarmId": self.prewarm_id,
            "reason": reason,
            "source": self.source,
            "totalBlocksObserved": stats.total_blocks_observed,
            "totalAudioFramesObserved": stats.total_audio_frames_observed,
            "bufferedBlocks": stats.buffered_blocks,
            "bufferedAudioFrames": stats.buffered_audio_frames,
            "bufferedPayloadBytes": stats.buffered_payload_bytes,
            "prewarmError": stats.error,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }
}

#[derive(Debug, Clone, Default)]
struct AdoptedPrewarm {
    prewarm_id: String,
    source: &'static str,
    block_count: u64,
    audio_frame_count: u64,
    payload_bytes: u64,
    blocks: Vec<Vec<u8>>,
}

impl AdoptedPrewarm {
    fn to_payload(&self, stop_payload: Value) -> Value {
        json!({
            "prewarmId": self.prewarm_id,
            "source": self.source,
            "blocks": self.block_count,
            "audioFrames": self.audio_frame_count,
            "payloadBytes": self.payload_bytes,
            "adopted": self.block_count > 0,
            "stop": stop_payload,
        })
    }
}

fn adopted_prewarm_payload(adopted_prewarm: Option<&(AdoptedPrewarm, Value)>) -> Value {
    match adopted_prewarm {
        Some((adopted, stop_payload)) => adopted.to_payload(stop_payload.clone()),
        None => json!({
            "adopted": false,
            "prewarmId": Value::Null,
            "blocks": 0,
            "audioFrames": 0,
            "payloadBytes": 0,
        }),
    }
}

fn synthetic_capture_enabled() -> bool {
    env_flag_enabled(env::var(SYNTHETIC_CAPTURE_ENV).ok().as_deref())
}

fn wasapi_capture_enabled() -> bool {
    env_flag_enabled(env::var(WASAPI_CAPTURE_ENV).ok().as_deref())
}

fn env_flag_enabled(raw: Option<&str>) -> bool {
    matches!(
        raw.unwrap_or_default().trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on" | "enabled"
    )
}

#[cfg(windows)]
fn start_synthetic_capture_impl(
    request: CaptureRequest,
    adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
) -> Result<(CaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_frame_pipe(&pipe_path)?;
    let (stop_tx, stop_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let adopted_blocks = adopted_prewarm
        .as_ref()
        .map(|(adopted, _)| adopted.blocks.clone())
        .unwrap_or_default();
    let join_handle = thread::Builder::new()
        .name("scriber-audio-synthetic-frame-pipe".to_string())
        .spawn(move || {
            run_synthetic_frame_pipe_writer(
                pipe_handle_value as HANDLE,
                writer_pipe_path,
                writer_request,
                stop_rx,
                adopted_blocks,
            )
        })
        .map_err(|err| {
            unsafe {
                CloseHandle(pipe_handle);
            }
            format!("synthetic frame-pipe writer thread spawn failed: {err}")
        })?;
    let session = CaptureSession {
        stream_id: stream_id.clone(),
        source: "synthetic-frame-pipe",
        stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
    };
    let payload = json!({
        "sidecar": SIDECAR_NAME,
        "captureAvailable": true,
        "syntheticFramePipe": true,
        "source": "synthetic-frame-pipe",
        "streamId": stream_id,
        "framePipe": pipe_path,
        "sampleRate": request.sample_rate,
        "channels": request.channels,
        "captureChannels": request.channels,
        "sampleFormat": "pcm_i16_le",
        "nativeEndpointIdHash": Value::Null,
        "adoptedPrewarm": adopted_prewarm_payload(adopted_prewarm.as_ref()),
        "endpointSelection": endpoint_selection_payload(
            &request,
            Value::Null,
            "synthetic",
            false,
            Value::String("syntheticCaptureHasNoNativeEndpoint".to_string()),
        ),
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
    });
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_synthetic_capture_impl(
    _request: CaptureRequest,
    _adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
) -> Result<(CaptureSession, Value), String> {
    Err("synthetic frame-pipe capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
fn start_wasapi_capture_impl(
    request: CaptureRequest,
    adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
) -> Result<(CaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_frame_pipe(&pipe_path)?;
    let (stop_tx, stop_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let adopted_blocks = adopted_prewarm
        .as_ref()
        .map(|(adopted, _)| adopted.blocks.clone())
        .unwrap_or_default();
    let join_handle = thread::Builder::new()
        .name("scriber-audio-wasapi-frame-pipe".to_string())
        .spawn(move || {
            run_wasapi_capture_writer(
                pipe_handle_value as HANDLE,
                writer_pipe_path,
                writer_request,
                stop_rx,
                ready_tx,
                adopted_blocks,
            )
        })
        .map_err(|err| {
            unsafe {
                CloseHandle(pipe_handle);
            }
            format!("WASAPI frame-pipe writer thread spawn failed: {err}")
        })?;

    let ready = match ready_rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Ok(ready)) => ready,
        Ok(Err(err)) => {
            let _ = stop_tx.send(());
            let _ = join_handle.join();
            return Err(err);
        }
        Err(err) => {
            let _ = stop_tx.send(());
            let _ = join_handle.join();
            return Err(format!("WASAPI capture did not become ready: {err}"));
        }
    };

    let session = CaptureSession {
        stream_id: stream_id.clone(),
        source: "wasapi-capture",
        stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
    };
    let payload = json!({
        "sidecar": SIDECAR_NAME,
        "captureAvailable": true,
        "wasapiCapture": true,
        "syntheticFramePipe": false,
        "source": "wasapi-capture",
        "streamId": stream_id,
        "framePipe": pipe_path,
        "sampleRate": request.sample_rate,
        "channels": request.channels,
        "captureChannels": request.channels,
        "sampleFormat": "pcm_i16_le",
        "nativeEndpointIdHash": ready.endpoint_id_hash,
        "adoptedPrewarm": adopted_prewarm_payload(adopted_prewarm.as_ref()),
        "endpointSelection": ready.endpoint_selection,
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "mixFormat": ready.mix_format.to_payload(),
        "resampler": {
            "sourceSampleRate": ready.mix_format.sample_rate,
            "targetSampleRate": request.sample_rate,
            "sourceChannels": ready.mix_format.channels,
            "targetChannels": request.channels,
            "method": "nearest",
        },
    });
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_wasapi_capture_impl(
    _request: CaptureRequest,
    _adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
) -> Result<(CaptureSession, Value), String> {
    Err("WASAPI capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
#[derive(Debug, Clone)]
struct WasapiReady {
    endpoint_id_hash: Value,
    endpoint_selection: Value,
    mix_format: WasapiMixFormat,
}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WasapiSampleFormat {
    Float32,
    Pcm16,
    Pcm32,
}

#[cfg(windows)]
impl WasapiSampleFormat {
    fn as_str(self) -> &'static str {
        match self {
            Self::Float32 => "float32",
            Self::Pcm16 => "pcm16",
            Self::Pcm32 => "pcm32",
        }
    }
}

#[cfg(windows)]
#[derive(Debug, Clone)]
struct WasapiMixFormat {
    format_tag: u16,
    channels: u16,
    sample_rate: u32,
    average_bytes_per_second: u32,
    block_align: u16,
    bits_per_sample: u16,
    extra_size: u16,
    sample_format: WasapiSampleFormat,
}

#[cfg(windows)]
impl WasapiMixFormat {
    fn to_payload(&self) -> Value {
        json!({
            "formatTag": self.format_tag,
            "channels": self.channels,
            "sampleRate": self.sample_rate,
            "averageBytesPerSecond": self.average_bytes_per_second,
            "blockAlign": self.block_align,
            "bitsPerSample": self.bits_per_sample,
            "extraSize": self.extra_size,
            "sampleFormat": self.sample_format.as_str(),
        })
    }
}

#[cfg(windows)]
struct WasapiPcmConverter {
    source: WasapiMixFormat,
    target_sample_rate: u32,
    target_channels: u16,
    block_size: u32,
    mono_buffer: Vec<f32>,
    next_source_index: f64,
    pending_samples: Vec<i16>,
}

#[cfg(windows)]
impl WasapiPcmConverter {
    fn new(source: WasapiMixFormat, request: &CaptureRequest) -> Self {
        Self {
            source,
            target_sample_rate: request.sample_rate,
            target_channels: request.channels,
            block_size: request.block_size,
            mono_buffer: Vec::new(),
            next_source_index: 0.0,
            pending_samples: Vec::new(),
        }
    }

    fn push_packet(
        &mut self,
        data: *const u8,
        frame_count: u32,
        silent: bool,
    ) -> Result<Vec<Vec<u8>>, String> {
        if frame_count == 0 {
            return Ok(Vec::new());
        }
        if silent {
            self.mono_buffer
                .extend(std::iter::repeat(0.0).take(frame_count as usize));
        } else {
            let byte_len = frame_count as usize * usize::from(self.source.block_align);
            let bytes = unsafe { std::slice::from_raw_parts(data, byte_len) };
            self.decode_interleaved_to_mono(bytes, frame_count as usize)?;
        }
        self.resample_pending();
        Ok(self.drain_complete_blocks())
    }

    fn decode_interleaved_to_mono(
        &mut self,
        bytes: &[u8],
        frame_count: usize,
    ) -> Result<(), String> {
        let channels = usize::from(self.source.channels);
        if channels == 0 {
            return Err("WASAPI mix format returned zero channels".to_string());
        }
        let sample_bytes = usize::from(self.source.bits_per_sample / 8);
        if sample_bytes == 0 {
            return Err("WASAPI mix format returned invalid sample size".to_string());
        }
        let expected = frame_count
            .saturating_mul(channels)
            .saturating_mul(sample_bytes);
        if bytes.len() < expected {
            return Err(format!(
                "WASAPI packet too short: expected {expected} bytes, got {}",
                bytes.len()
            ));
        }

        for frame in 0..frame_count {
            let mut sum = 0.0_f32;
            for channel in 0..channels {
                let offset = (frame * channels + channel) * sample_bytes;
                let sample = match self.source.sample_format {
                    WasapiSampleFormat::Float32 => {
                        let raw = f32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
                        raw.clamp(-1.0, 1.0)
                    }
                    WasapiSampleFormat::Pcm16 => {
                        let raw = i16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap());
                        f32::from(raw) / f32::from(i16::MAX)
                    }
                    WasapiSampleFormat::Pcm32 => {
                        let raw = i32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
                        raw as f32 / i32::MAX as f32
                    }
                };
                sum += sample;
            }
            self.mono_buffer.push(sum / channels as f32);
        }
        Ok(())
    }

    fn resample_pending(&mut self) {
        let ratio = self.source.sample_rate as f64 / self.target_sample_rate as f64;
        while (self.next_source_index.floor() as usize) < self.mono_buffer.len() {
            let index = self.next_source_index.floor() as usize;
            let sample = f32_to_i16(self.mono_buffer[index]);
            for _ in 0..self.target_channels {
                self.pending_samples.push(sample);
            }
            self.next_source_index += ratio;
        }

        let consumed = self.next_source_index.floor() as usize;
        if consumed > 0 {
            let drain_count = consumed.min(self.mono_buffer.len());
            self.mono_buffer.drain(0..drain_count);
            self.next_source_index -= drain_count as f64;
        }
    }

    fn drain_complete_blocks(&mut self) -> Vec<Vec<u8>> {
        let samples_per_block = self.block_size as usize * usize::from(self.target_channels);
        let mut blocks = Vec::new();
        while self.pending_samples.len() >= samples_per_block {
            let samples: Vec<i16> = self.pending_samples.drain(0..samples_per_block).collect();
            let mut bytes = Vec::with_capacity(samples.len() * 2);
            for sample in samples {
                bytes.extend_from_slice(&sample.to_le_bytes());
            }
            blocks.push(bytes);
        }
        blocks
    }
}

fn f32_to_i16(sample: f32) -> i16 {
    let scaled = sample.clamp(-1.0, 1.0) * i16::MAX as f32;
    scaled.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16
}

fn start_synthetic_prewarm_impl(
    request: CaptureRequest,
) -> Result<(PrewarmSession, Value), String> {
    let prewarm_id = Uuid::new_v4().simple().to_string();
    let (stop_tx, stop_rx) = mpsc::channel();
    let worker_request = request.clone();
    let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(
        requested_prebuffer_frame_count(&request),
    )));
    let worker_buffer = Arc::clone(&buffer);
    let join_handle = thread::Builder::new()
        .name("scriber-audio-synthetic-prewarm".to_string())
        .spawn(move || run_synthetic_prewarm_worker(worker_request, stop_rx, worker_buffer))
        .map_err(|err| format!("synthetic prewarm worker thread spawn failed: {err}"))?;

    let session = PrewarmSession {
        prewarm_id: prewarm_id.clone(),
        source: "synthetic-prewarm",
        stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
        buffer,
        block_size: request.block_size,
    };
    let payload = json!({
        "sidecar": SIDECAR_NAME,
        "prewarmAvailable": true,
        "syntheticPrewarm": true,
        "wasapiPrewarm": false,
        "source": "synthetic-prewarm",
        "prewarmId": prewarm_id,
        "sampleRate": request.sample_rate,
        "channels": request.channels,
        "captureChannels": request.channels,
        "sampleFormat": "pcm_i16_le",
        "nativeEndpointIdHash": Value::Null,
        "endpointSelection": endpoint_selection_payload(
            &request,
            Value::Null,
            "synthetic",
            false,
            Value::String("syntheticPrewarmHasNoNativeEndpoint".to_string()),
        ),
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "prebufferFrameTarget": requested_prebuffer_frame_count(&request),
    });
    Ok((session, payload))
}

#[cfg(windows)]
fn start_wasapi_prewarm_impl(request: CaptureRequest) -> Result<(PrewarmSession, Value), String> {
    let prewarm_id = Uuid::new_v4().simple().to_string();
    let (stop_tx, stop_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_request = request.clone();
    let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(
        requested_prebuffer_frame_count(&request),
    )));
    let worker_buffer = Arc::clone(&buffer);
    let join_handle = thread::Builder::new()
        .name("scriber-audio-wasapi-prewarm".to_string())
        .spawn(move || run_wasapi_prewarm_worker(worker_request, stop_rx, ready_tx, worker_buffer))
        .map_err(|err| format!("WASAPI prewarm worker thread spawn failed: {err}"))?;

    let ready = match ready_rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Ok(ready)) => ready,
        Ok(Err(err)) => {
            let _ = stop_tx.send(());
            let _ = join_handle.join();
            return Err(err);
        }
        Err(err) => {
            let _ = stop_tx.send(());
            let _ = join_handle.join();
            return Err(format!("WASAPI prewarm did not become ready: {err}"));
        }
    };

    let session = PrewarmSession {
        prewarm_id: prewarm_id.clone(),
        source: "wasapi-prewarm",
        stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
        buffer,
        block_size: request.block_size,
    };
    let payload = json!({
        "sidecar": SIDECAR_NAME,
        "prewarmAvailable": true,
        "syntheticPrewarm": false,
        "wasapiPrewarm": true,
        "source": "wasapi-prewarm",
        "prewarmId": prewarm_id,
        "sampleRate": request.sample_rate,
        "channels": request.channels,
        "captureChannels": request.channels,
        "sampleFormat": "pcm_i16_le",
        "nativeEndpointIdHash": ready.endpoint_id_hash,
        "endpointSelection": ready.endpoint_selection,
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "prebufferFrameTarget": requested_prebuffer_frame_count(&request),
        "mixFormat": ready.mix_format.to_payload(),
        "resampler": {
            "sourceSampleRate": ready.mix_format.sample_rate,
            "targetSampleRate": request.sample_rate,
            "sourceChannels": ready.mix_format.channels,
            "targetChannels": request.channels,
            "method": "nearest",
        },
    });
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_wasapi_prewarm_impl(_request: CaptureRequest) -> Result<(PrewarmSession, Value), String> {
    Err("WASAPI prewarm is only implemented on Windows".to_string())
}

fn run_synthetic_prewarm_worker(
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    buffer: Arc<Mutex<PrewarmBuffer>>,
) -> PrewarmStats {
    let mut stats = PrewarmStats::default();
    let frame_interval = Duration::from_secs_f64(
        (request.block_size as f64 / f64::from(request.sample_rate)).max(0.001),
    );
    let max_buffered_blocks = u64::from(requested_prebuffer_frame_count(&request));
    let payload_len = usize::from(request.channels) * request.block_size as usize * 2;
    loop {
        match stop_rx.recv_timeout(frame_interval) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        let payload = vec![0_u8; payload_len];
        stats.total_blocks_observed = stats.total_blocks_observed.saturating_add(1);
        stats.total_audio_frames_observed = stats
            .total_audio_frames_observed
            .saturating_add(u64::from(request.block_size));
        if max_buffered_blocks > 0 {
            match buffer.lock() {
                Ok(mut buffer) => {
                    buffer.push(payload);
                    stats.buffered_blocks = buffer.block_count();
                    stats.buffered_audio_frames = buffer.audio_frame_count(request.block_size);
                    stats.buffered_payload_bytes = buffer.payload_bytes();
                }
                Err(_) => {
                    stats.error = Some("prewarmBufferLockPoisoned".to_string());
                    break;
                }
            }
        }
    }
    stats
}

#[cfg(windows)]
fn create_frame_pipe(pipe_path: &str) -> Result<HANDLE, String> {
    let wide = wide_null(pipe_path);
    let handle = unsafe {
        CreateNamedPipeW(
            wide.as_ptr(),
            PIPE_ACCESS_OUTBOUND,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT,
            1,
            64 * 1024,
            0,
            0,
            null(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!(
            "CreateNamedPipeW failed for synthetic frame pipe: {}",
            unsafe { GetLastError() }
        ));
    }
    Ok(handle)
}

#[cfg(windows)]
fn run_synthetic_frame_pipe_writer(
    pipe_handle: HANDLE,
    _pipe_path: String,
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
) -> CaptureWriterStats {
    let mut stats = CaptureWriterStats::default();
    let started = Instant::now();
    let connect_result = wait_for_pipe_client(pipe_handle, &stop_rx);
    if let Err(err) = connect_result {
        stats.error = Some(err);
        unsafe {
            CloseHandle(pipe_handle);
        }
        return stats;
    }
    stats.connected = true;

    let mut sequence = 0_u64;
    if let Err(err) = write_adopted_prebuffer_blocks(
        pipe_handle,
        &request,
        &adopted_prebuffer_blocks,
        started,
        &mut sequence,
        &mut stats,
    ) {
        stats.error = Some(err);
        unsafe {
            FlushFileBuffers(pipe_handle);
            DisconnectNamedPipe(pipe_handle);
            CloseHandle(pipe_handle);
        }
        return stats;
    }

    let payload_len = usize::from(request.channels) * request.block_size as usize * 2;
    let payload = vec![0_u8; payload_len];
    let frame_interval = Duration::from_secs_f64(
        (request.block_size as f64 / f64::from(request.sample_rate)).max(0.001),
    );
    let prebuffer_frame_target = if adopted_prebuffer_blocks.is_empty() {
        requested_prebuffer_frame_count(&request)
    } else {
        0
    };
    let mut prebuffer_frames_written = 0_u32;
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => break,
            Err(TryRecvError::Empty) => {}
        }

        let flags = if prebuffer_frames_written < prebuffer_frame_target {
            prebuffer_frames_written = prebuffer_frames_written.saturating_add(1);
            AUDIO_FRAME_FLAG_PREBUFFER
        } else {
            0
        };
        let timestamp_micros = started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
        let header = match AudioFrameHeader::new(
            payload.len() as u32,
            sequence,
            timestamp_micros,
            request.block_size,
            request.channels,
            flags,
        ) {
            Ok(header) => header,
            Err(err) => {
                stats.error = Some(format!("synthetic frame header failed: {err}"));
                break;
            }
        };
        let frame = match encode_audio_frame(&header, &payload) {
            Ok(frame) => frame,
            Err(err) => {
                stats.error = Some(format!("synthetic frame encode failed: {err}"));
                break;
            }
        };
        match write_all_to_pipe(pipe_handle, &frame) {
            Ok(bytes) => {
                stats.frames_written = stats.frames_written.saturating_add(1);
                if flags & AUDIO_FRAME_FLAG_PREBUFFER != 0 {
                    stats.prebuffer_frames_written =
                        stats.prebuffer_frames_written.saturating_add(1);
                } else {
                    stats.live_frames_written = stats.live_frames_written.saturating_add(1);
                }
                stats.bytes_written = stats.bytes_written.saturating_add(u64::from(bytes));
                sequence = sequence.saturating_add(1);
            }
            Err(err) => {
                stats.error = Some(err);
                break;
            }
        }
        thread::sleep(frame_interval);
    }

    unsafe {
        FlushFileBuffers(pipe_handle);
        DisconnectNamedPipe(pipe_handle);
        CloseHandle(pipe_handle);
    }
    stats
}

#[cfg(windows)]
fn write_adopted_prebuffer_blocks(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    blocks: &[Vec<u8>],
    started: Instant,
    sequence: &mut u64,
    stats: &mut CaptureWriterStats,
) -> Result<(), String> {
    for payload in blocks {
        let timestamp_micros = started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
        let header = AudioFrameHeader::new(
            payload.len() as u32,
            *sequence,
            timestamp_micros,
            request.block_size,
            request.channels,
            AUDIO_FRAME_FLAG_PREBUFFER,
        )
        .map_err(|err| format!("adopted prewarm frame header failed: {err}"))?;
        let frame = encode_audio_frame(&header, payload)
            .map_err(|err| format!("adopted prewarm frame encode failed: {err}"))?;
        let bytes_written = write_all_to_pipe(pipe_handle, &frame)?;
        stats.frames_written = stats.frames_written.saturating_add(1);
        stats.prebuffer_frames_written = stats.prebuffer_frames_written.saturating_add(1);
        stats.bytes_written = stats.bytes_written.saturating_add(u64::from(bytes_written));
        *sequence = (*sequence).saturating_add(1);
    }
    Ok(())
}

fn requested_prebuffer_frame_count(request: &CaptureRequest) -> u32 {
    if request.prebuffer_ms == 0 || request.block_size == 0 || request.sample_rate == 0 {
        return 0;
    }
    let requested_samples =
        u64::from(request.sample_rate).saturating_mul(u64::from(request.prebuffer_ms)) / 1000;
    if requested_samples == 0 {
        return 0;
    }
    let blocks = requested_samples.div_ceil(u64::from(request.block_size));
    blocks.min(u64::from(u32::MAX)) as u32
}

#[cfg(windows)]
fn run_wasapi_capture_writer(
    pipe_handle: HANDLE,
    _pipe_path: String,
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    ready_tx: Sender<Result<WasapiReady, String>>,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
) -> CaptureWriterStats {
    let mut stats = CaptureWriterStats::default();
    let mut ready_sent = false;
    let started = Instant::now();
    let result = run_wasapi_capture_writer_inner(
        pipe_handle,
        &request,
        &stop_rx,
        &ready_tx,
        &mut ready_sent,
        &mut stats,
        started,
        adopted_prebuffer_blocks,
    );
    if let Err(err) = result {
        if !ready_sent {
            let _ = ready_tx.send(Err(err.clone()));
        }
        stats.error = Some(err);
    }
    unsafe {
        FlushFileBuffers(pipe_handle);
        DisconnectNamedPipe(pipe_handle);
        CloseHandle(pipe_handle);
    }
    stats
}

#[cfg(windows)]
fn run_wasapi_prewarm_worker(
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    ready_tx: Sender<Result<WasapiReady, String>>,
    buffer: Arc<Mutex<PrewarmBuffer>>,
) -> PrewarmStats {
    let mut stats = PrewarmStats::default();
    let mut ready_sent = false;
    let result = run_wasapi_prewarm_worker_inner(
        &request,
        &stop_rx,
        &ready_tx,
        &mut ready_sent,
        &mut stats,
        buffer,
    );
    if let Err(err) = result {
        if !ready_sent {
            let _ = ready_tx.send(Err(err.clone()));
        }
        stats.error = Some(err);
    }
    stats
}

#[cfg(windows)]
struct WasapiSelectedDevice {
    device: IMMDevice,
    endpoint_id_hash: Value,
    endpoint_selection: Value,
}

#[cfg(windows)]
fn select_wasapi_capture_device(
    enumerator: &IMMDeviceEnumerator,
    request: &CaptureRequest,
) -> Result<WasapiSelectedDevice, String> {
    let requested_hash = request.native_endpoint_id_hash.trim();
    if !requested_hash.is_empty() {
        let collection = unsafe { enumerator.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE) }
            .map_err(|err| format!("WASAPI capture endpoint enumeration failed: {err}"))?;
        let count = unsafe { collection.GetCount() }
            .map_err(|err| format!("WASAPI capture endpoint count failed: {err}"))?;
        for index in 0..count {
            let device = unsafe { collection.Item(index) }
                .map_err(|err| format!("WASAPI capture endpoint item {index} failed: {err}"))?;
            let endpoint_hash = unsafe { wasapi_endpoint_id_hash_string(&device) };
            if endpoint_hash.as_deref() == Some(requested_hash) {
                let endpoint_id_hash = Value::String(requested_hash.to_string());
                return Ok(WasapiSelectedDevice {
                    device,
                    endpoint_selection: endpoint_selection_payload(
                        request,
                        endpoint_id_hash.clone(),
                        "nativeEndpointHash",
                        false,
                        Value::Null,
                    ),
                    endpoint_id_hash,
                });
            }
        }
        return Err(format!(
            "requested native WASAPI capture endpoint hash was not found: {requested_hash}"
        ));
    }

    if !is_default_device_preference(&request.device_preference) {
        return Err(
            "requested non-default WASAPI capture has no native endpoint hash; refusing default fallback"
                .to_string(),
        );
    }

    let device = unsafe { enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
        .map_err(|err| format!("default WASAPI capture endpoint unavailable: {err}"))?;
    let endpoint_id_hash = unsafe { wasapi_endpoint_id_hash(&device) };
    Ok(WasapiSelectedDevice {
        device,
        endpoint_selection: endpoint_selection_payload(
            request,
            endpoint_id_hash.clone(),
            "default",
            true,
            Value::Null,
        ),
        endpoint_id_hash,
    })
}

fn is_default_device_preference(value: &str) -> bool {
    let normalized = value.trim().to_ascii_lowercase();
    normalized.is_empty() || normalized == "default" || normalized == "none"
}

fn endpoint_selection_payload(
    request: &CaptureRequest,
    selected_endpoint_id_hash: Value,
    mode: &str,
    used_default_endpoint: bool,
    fallback_reason: Value,
) -> Value {
    json!({
        "mode": mode,
        "requestedDevicePreference": request.device_preference,
        "requestedPortAudioLabel": request.port_audio_label,
        "requestedNativeEndpointIdHash": if request.native_endpoint_id_hash.is_empty() {
            Value::Null
        } else {
            Value::String(request.native_endpoint_id_hash.clone())
        },
        "selectedNativeEndpointIdHash": selected_endpoint_id_hash,
        "usedDefaultEndpoint": used_default_endpoint,
        "fallbackReason": fallback_reason,
    })
}

#[cfg(windows)]
fn run_wasapi_capture_writer_inner(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    ready_tx: &Sender<Result<WasapiReady, String>>,
    ready_sent: &mut bool,
    stats: &mut CaptureWriterStats,
    started: Instant,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
) -> Result<(), String> {
    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|err| format!("COM initialization failed for WASAPI capture: {err}"))?;

    let result = (|| -> Result<(), String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let selected = select_wasapi_capture_device(&enumerator, request)?;
        let device = selected.device;
        let endpoint_id_hash = selected.endpoint_id_hash.clone();
        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|err| format!("IAudioClient activation failed: {err}"))?;
        let mix_format_ptr = unsafe { client.GetMixFormat() }
            .map_err(|err| format!("GetMixFormat failed: {err}"))?;
        let mix_format = unsafe { wasapi_mix_format_from_ptr(mix_format_ptr) };
        let init_result = unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                0,
                1_000_000,
                0,
                mix_format_ptr,
                None,
            )
        };
        unsafe {
            CoTaskMemFree(Some(mix_format_ptr.cast::<c_void>()));
        }
        let mix_format = mix_format?;
        init_result.map_err(|err| format!("WASAPI shared-mode Initialize failed: {err}"))?;
        let capture_client: IAudioCaptureClient = unsafe { client.GetService() }
            .map_err(|err| format!("IAudioCaptureClient service unavailable: {err}"))?;

        ready_tx
            .send(Ok(WasapiReady {
                endpoint_id_hash,
                endpoint_selection: selected.endpoint_selection,
                mix_format: mix_format.clone(),
            }))
            .map_err(|err| format!("could not report WASAPI readiness: {err}"))?;
        *ready_sent = true;

        wait_for_pipe_client(pipe_handle, stop_rx)?;
        stats.connected = true;
        let mut sequence = 0_u64;
        write_adopted_prebuffer_blocks(
            pipe_handle,
            request,
            &adopted_prebuffer_blocks,
            started,
            &mut sequence,
            stats,
        )?;
        unsafe { client.Start() }.map_err(|err| format!("WASAPI Start failed: {err}"))?;
        let capture_result = pump_wasapi_capture(
            pipe_handle,
            request,
            stop_rx,
            &capture_client,
            mix_format,
            stats,
            started,
            sequence,
            adopted_prebuffer_blocks.is_empty(),
        );
        let stop_result = unsafe { client.Stop() };
        if let Err(err) = stop_result {
            if capture_result.is_ok() {
                return Err(format!("WASAPI Stop failed: {err}"));
            }
        }
        capture_result
    })();

    unsafe {
        CoUninitialize();
    }
    result
}

#[cfg(windows)]
fn run_wasapi_prewarm_worker_inner(
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    ready_tx: &Sender<Result<WasapiReady, String>>,
    ready_sent: &mut bool,
    stats: &mut PrewarmStats,
    buffer: Arc<Mutex<PrewarmBuffer>>,
) -> Result<(), String> {
    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|err| format!("COM initialization failed for WASAPI prewarm: {err}"))?;

    let result = (|| -> Result<(), String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let selected = select_wasapi_capture_device(&enumerator, request)?;
        let device = selected.device;
        let endpoint_id_hash = selected.endpoint_id_hash.clone();
        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|err| format!("IAudioClient activation failed: {err}"))?;
        let mix_format_ptr = unsafe { client.GetMixFormat() }
            .map_err(|err| format!("GetMixFormat failed: {err}"))?;
        let mix_format = unsafe { wasapi_mix_format_from_ptr(mix_format_ptr) };
        let init_result = unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                0,
                1_000_000,
                0,
                mix_format_ptr,
                None,
            )
        };
        unsafe {
            CoTaskMemFree(Some(mix_format_ptr.cast::<c_void>()));
        }
        let mix_format = mix_format?;
        init_result.map_err(|err| format!("WASAPI shared-mode Initialize failed: {err}"))?;
        let capture_client: IAudioCaptureClient = unsafe { client.GetService() }
            .map_err(|err| format!("IAudioCaptureClient service unavailable: {err}"))?;

        ready_tx
            .send(Ok(WasapiReady {
                endpoint_id_hash,
                endpoint_selection: selected.endpoint_selection,
                mix_format: mix_format.clone(),
            }))
            .map_err(|err| format!("could not report WASAPI prewarm readiness: {err}"))?;
        *ready_sent = true;

        unsafe { client.Start() }.map_err(|err| format!("WASAPI Start failed: {err}"))?;
        let prewarm_result =
            pump_wasapi_prewarm(request, stop_rx, &capture_client, mix_format, stats, buffer);
        let stop_result = unsafe { client.Stop() };
        if let Err(err) = stop_result {
            if prewarm_result.is_ok() {
                return Err(format!("WASAPI Stop failed: {err}"));
            }
        }
        prewarm_result
    })();

    unsafe {
        CoUninitialize();
    }
    result
}

#[cfg(windows)]
fn pump_wasapi_capture(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    capture_client: &IAudioCaptureClient,
    mix_format: WasapiMixFormat,
    stats: &mut CaptureWriterStats,
    started: Instant,
    initial_sequence: u64,
    use_live_prebuffer: bool,
) -> Result<(), String> {
    let mut converter = WasapiPcmConverter::new(mix_format, request);
    let prebuffer_frame_target = if use_live_prebuffer {
        requested_prebuffer_frame_count(request)
    } else {
        0
    };
    let mut sequence = initial_sequence;
    let mut prebuffer_frames_written = 0_u32;
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => return Ok(()),
            Err(TryRecvError::Empty) => {}
        }

        let mut packet_frames = unsafe { capture_client.GetNextPacketSize() }
            .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        if packet_frames == 0 {
            thread::sleep(Duration::from_millis(5));
            continue;
        }

        while packet_frames > 0 {
            let mut data_ptr: *mut u8 = null_mut();
            let mut frames_to_read = 0_u32;
            let mut flags = 0_u32;
            unsafe {
                capture_client
                    .GetBuffer(&mut data_ptr, &mut frames_to_read, &mut flags, None, None)
                    .map_err(|err| format!("WASAPI GetBuffer failed: {err}"))?;
            }
            let silent = flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0;
            let blocks = converter.push_packet(data_ptr.cast_const(), frames_to_read, silent);
            let release_result = unsafe { capture_client.ReleaseBuffer(frames_to_read) };
            release_result.map_err(|err| format!("WASAPI ReleaseBuffer failed: {err}"))?;
            for payload in blocks? {
                let flags = if prebuffer_frames_written < prebuffer_frame_target {
                    prebuffer_frames_written = prebuffer_frames_written.saturating_add(1);
                    AUDIO_FRAME_FLAG_PREBUFFER
                } else {
                    0
                };
                let timestamp_micros =
                    started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
                let header = AudioFrameHeader::new(
                    payload.len() as u32,
                    sequence,
                    timestamp_micros,
                    request.block_size,
                    request.channels,
                    flags,
                )
                .map_err(|err| format!("WASAPI frame header failed: {err}"))?;
                let frame = encode_audio_frame(&header, &payload)
                    .map_err(|err| format!("WASAPI frame encode failed: {err}"))?;
                let bytes_written = write_all_to_pipe(pipe_handle, &frame)?;
                stats.frames_written = stats.frames_written.saturating_add(1);
                if flags & AUDIO_FRAME_FLAG_PREBUFFER != 0 {
                    stats.prebuffer_frames_written =
                        stats.prebuffer_frames_written.saturating_add(1);
                } else {
                    stats.live_frames_written = stats.live_frames_written.saturating_add(1);
                }
                stats.bytes_written = stats.bytes_written.saturating_add(u64::from(bytes_written));
                sequence = sequence.saturating_add(1);
            }
            packet_frames = unsafe { capture_client.GetNextPacketSize() }
                .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        }
    }
}

#[cfg(windows)]
fn pump_wasapi_prewarm(
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    capture_client: &IAudioCaptureClient,
    mix_format: WasapiMixFormat,
    stats: &mut PrewarmStats,
    buffer: Arc<Mutex<PrewarmBuffer>>,
) -> Result<(), String> {
    let mut converter = WasapiPcmConverter::new(mix_format, request);
    let max_buffered_blocks = u64::from(requested_prebuffer_frame_count(request));
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => return Ok(()),
            Err(TryRecvError::Empty) => {}
        }

        let mut packet_frames = unsafe { capture_client.GetNextPacketSize() }
            .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        if packet_frames == 0 {
            thread::sleep(Duration::from_millis(5));
            continue;
        }

        while packet_frames > 0 {
            let mut data_ptr: *mut u8 = null_mut();
            let mut frames_to_read = 0_u32;
            let mut flags = 0_u32;
            unsafe {
                capture_client
                    .GetBuffer(&mut data_ptr, &mut frames_to_read, &mut flags, None, None)
                    .map_err(|err| format!("WASAPI GetBuffer failed: {err}"))?;
            }
            let silent = flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0;
            let blocks = converter.push_packet(data_ptr.cast_const(), frames_to_read, silent);
            let release_result = unsafe { capture_client.ReleaseBuffer(frames_to_read) };
            release_result.map_err(|err| format!("WASAPI ReleaseBuffer failed: {err}"))?;
            for payload in blocks? {
                stats.total_blocks_observed = stats.total_blocks_observed.saturating_add(1);
                stats.total_audio_frames_observed = stats
                    .total_audio_frames_observed
                    .saturating_add(u64::from(request.block_size));
                if max_buffered_blocks > 0 {
                    match buffer.lock() {
                        Ok(mut buffer) => {
                            buffer.push(payload);
                            stats.buffered_blocks = buffer.block_count();
                            stats.buffered_audio_frames =
                                buffer.audio_frame_count(request.block_size);
                            stats.buffered_payload_bytes = buffer.payload_bytes();
                        }
                        Err(_) => {
                            return Err("prewarmBufferLockPoisoned".to_string());
                        }
                    }
                } else {
                    drop(payload);
                }
            }
            packet_frames = unsafe { capture_client.GetNextPacketSize() }
                .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        }
    }
}

#[cfg(windows)]
unsafe fn wasapi_endpoint_id_hash(device: &windows::Win32::Media::Audio::IMMDevice) -> Value {
    match unsafe { wasapi_endpoint_id_hash_string(device) } {
        Some(hash) => Value::String(hash),
        None => Value::Null,
    }
}

#[cfg(windows)]
unsafe fn wasapi_endpoint_id_hash_string(
    device: &windows::Win32::Media::Audio::IMMDevice,
) -> Option<String> {
    let id = unsafe { device.GetId() }.ok();
    let id = id?;
    let text = unsafe { id.to_string() }.unwrap_or_default();
    unsafe {
        CoTaskMemFree(Some(id.as_ptr().cast::<c_void>()));
    }
    if text.is_empty() {
        None
    } else {
        Some(hash_sensitive_identifier(&text))
    }
}

#[cfg(windows)]
unsafe fn wasapi_mix_format_from_ptr(
    format_ptr: *const WAVEFORMATEX,
) -> Result<WasapiMixFormat, String> {
    if format_ptr.is_null() {
        return Err("GetMixFormat returned null".to_string());
    }
    let base = unsafe { std::ptr::read_unaligned(format_ptr) };
    let format_tag = base.wFormatTag;
    let channels = base.nChannels;
    let sample_rate = base.nSamplesPerSec;
    let average_bytes_per_second = base.nAvgBytesPerSec;
    let block_align = base.nBlockAlign;
    let bits_per_sample = base.wBitsPerSample;
    let extra_size = base.cbSize;
    if channels == 0 || sample_rate == 0 || block_align == 0 {
        return Err(format!(
            "invalid WASAPI mix format: channels={channels}, sampleRate={sample_rate}, blockAlign={block_align}"
        ));
    }

    let mut sample_format = match format_tag {
        tag if tag == WAVE_FORMAT_PCM as u16 && bits_per_sample == 16 => {
            Some(WasapiSampleFormat::Pcm16)
        }
        tag if tag == WAVE_FORMAT_PCM as u16 && bits_per_sample == 32 => {
            Some(WasapiSampleFormat::Pcm32)
        }
        tag if tag == WAVE_FORMAT_IEEE_FLOAT_TAG && bits_per_sample == 32 => {
            Some(WasapiSampleFormat::Float32)
        }
        _ => None,
    };

    if format_tag == WAVE_FORMAT_EXTENSIBLE_TAG && extra_size >= 22 {
        let extensible =
            unsafe { std::ptr::read_unaligned(format_ptr.cast::<WAVEFORMATEXTENSIBLE>()) };
        let sub_format = extensible.SubFormat;
        sample_format = if sub_format == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT && bits_per_sample == 32 {
            Some(WasapiSampleFormat::Float32)
        } else if sub_format == KSDATAFORMAT_SUBTYPE_PCM && bits_per_sample == 16 {
            Some(WasapiSampleFormat::Pcm16)
        } else if sub_format == KSDATAFORMAT_SUBTYPE_PCM && bits_per_sample == 32 {
            Some(WasapiSampleFormat::Pcm32)
        } else {
            None
        };
    }

    let Some(sample_format) = sample_format else {
        return Err(format!(
            "unsupported WASAPI mix format: tag={format_tag}, bitsPerSample={bits_per_sample}, extraSize={extra_size}"
        ));
    };

    Ok(WasapiMixFormat {
        format_tag,
        channels,
        sample_rate,
        average_bytes_per_second,
        block_align,
        bits_per_sample,
        extra_size,
        sample_format,
    })
}

#[cfg(windows)]
fn wait_for_pipe_client(pipe_handle: HANDLE, stop_rx: &mpsc::Receiver<()>) -> Result<(), String> {
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => {
                return Err("synthetic frame pipe stopped before client connected".to_string())
            }
            Err(TryRecvError::Empty) => {}
        }
        let connected = unsafe { ConnectNamedPipe(pipe_handle, null_mut()) };
        if connected != 0 {
            return Ok(());
        }
        let err = unsafe { GetLastError() };
        if err == ERROR_PIPE_CONNECTED {
            return Ok(());
        }
        if err == ERROR_PIPE_LISTENING {
            thread::sleep(Duration::from_millis(10));
            continue;
        }
        return Err(format!(
            "ConnectNamedPipe failed for synthetic frame pipe: {err}"
        ));
    }
}

#[cfg(windows)]
fn write_all_to_pipe(pipe_handle: HANDLE, bytes: &[u8]) -> Result<u32, String> {
    let mut offset = 0usize;
    let mut total_written = 0u32;
    while offset < bytes.len() {
        let remaining = &bytes[offset..];
        let chunk_len = remaining.len().min(u32::MAX as usize) as u32;
        let mut written = 0u32;
        let ok = unsafe {
            WriteFile(
                pipe_handle,
                remaining.as_ptr(),
                chunk_len,
                &mut written,
                null_mut(),
            )
        };
        if ok == 0 {
            let err = unsafe { GetLastError() };
            if err == ERROR_NO_DATA {
                return Err("synthetic frame pipe client disconnected".to_string());
            }
            return Err(format!("WriteFile failed for synthetic frame pipe: {err}"));
        }
        if written == 0 {
            return Err("WriteFile wrote zero bytes to synthetic frame pipe".to_string());
        }
        offset = offset.saturating_add(written as usize);
        total_written = total_written.saturating_add(written);
    }
    Ok(total_written)
}

#[cfg(windows)]
fn wide_null(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn self_test_payload() -> Value {
    json!({
        "sidecar": SIDECAR_NAME,
        "ok": true,
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "capabilities": capabilities_payload(),
    })
}

fn capabilities_payload() -> Value {
    json!({
        "sidecar": SIDECAR_NAME,
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "commands": [
            "ping",
            "capabilities",
            "captureStart",
            "captureStop",
            "prewarmStart",
            "prewarmStop",
            "shutdown"
        ],
        "captureAvailable": wasapi_capture_enabled() || synthetic_capture_enabled(),
        "captureUnavailableReason": if wasapi_capture_enabled() || synthetic_capture_enabled() {
            Value::Null
        } else {
            Value::String("rustAudioCaptureDisabled".to_string())
        },
        "wasapiCaptureAvailable": wasapi_capture_enabled(),
        "wasapiCaptureEnv": WASAPI_CAPTURE_ENV,
        "syntheticFramePipeAvailable": synthetic_capture_enabled(),
        "syntheticFramePipeEnv": SYNTHETIC_CAPTURE_ENV,
        "prewarmAvailable": wasapi_capture_enabled() || synthetic_capture_enabled(),
        "prewarmUnavailableReason": if wasapi_capture_enabled() || synthetic_capture_enabled() {
            Value::Null
        } else {
            Value::String("rustAudioPrewarmDisabled".to_string())
        },
        "syntheticPrewarmAvailable": synthetic_capture_enabled(),
        "wasapiPrewarmAvailable": wasapi_capture_enabled(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
    })
}

fn audio_frame_protocol_payload() -> Value {
    json!({
        "magic": "SAF1",
        "version": AUDIO_FRAME_VERSION,
        "headerBytes": AUDIO_FRAME_HEADER_LEN,
        "sampleFormat": "pcm_i16_le",
    })
}

fn response_payload(
    request_id: &str,
    success: bool,
    error_code: &str,
    fallback_reason: &str,
    started: Instant,
    payload: Value,
) -> Value {
    json!({
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "requestId": request_id,
        "success": success,
        "errorCode": if error_code.is_empty() { Value::Null } else { Value::String(error_code.to_string()) },
        "fallbackReason": if fallback_reason.is_empty() { Value::Null } else { Value::String(fallback_reason.to_string()) },
        "timingsMs": {
            "total": started.elapsed().as_secs_f64() * 1000.0,
        },
        "payload": payload,
    })
}

fn optional_u64(payload: &Value, key: &str, default: u64, max: u64) -> u64 {
    payload
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_u64)
        .unwrap_or(default)
        .min(max)
}

fn bounded_string(payload: &Value, key: &str, default: &str, max_chars: usize) -> String {
    let value = payload
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_str)
        .unwrap_or(default)
        .trim()
        .chars()
        .take(max_chars)
        .collect::<String>();
    if value.is_empty() {
        default.to_string()
    } else {
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidecar_self_test_reports_protocol_and_frame_contract() {
        let payload = self_test_payload();

        assert_eq!(payload["sidecar"], SIDECAR_NAME);
        assert_eq!(payload["ok"], true);
        assert_eq!(payload["protocolVersion"], SIDECAR_PROTOCOL_VERSION);
        assert_eq!(payload["capabilities"]["captureAvailable"], false);
        assert_eq!(payload["capabilities"]["prewarmAvailable"], false);
        assert_eq!(
            payload["capabilities"]["syntheticFramePipeEnv"],
            SYNTHETIC_CAPTURE_ENV
        );
        assert_eq!(
            payload["capabilities"]["audioFrameProtocol"]["sampleFormat"],
            "pcm_i16_le"
        );
    }

    #[test]
    fn sidecar_ping_uses_newline_safe_json_contract() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r1",
            "command": "ping",
            "payload": {}
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], true);
        assert_eq!(response["requestId"], "r1");
        assert_eq!(response["payload"]["pong"], true);
    }

    #[test]
    fn sidecar_capture_start_returns_explicit_unavailable_payload() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r-capture",
            "command": "captureStart",
            "payload": {
                "sampleRate": 999_999,
                "channels": 99,
                "blockSize": 999_999,
                "devicePreference": "default",
                "prebufferMs": 999_999,
            }
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioCaptureUnavailable");
        assert_eq!(
            response["payload"]["requestedFormat"]["sampleRate"],
            192_000
        );
        assert_eq!(response["payload"]["requestedFormat"]["channels"], 16);
        assert_eq!(response["payload"]["requestedFormat"]["prebufferMs"], 2_000);
        assert_eq!(response["payload"]["audioFrameProtocol"]["version"], 1);
        assert_eq!(
            response["payload"]["requestedFormat"]["nativeEndpointIdHash"],
            ""
        );
    }

    #[test]
    fn sidecar_prewarm_start_returns_explicit_unavailable_payload() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r-prewarm",
            "command": "prewarmStart",
            "payload": {
                "sampleRate": 999_999,
                "channels": 99,
                "blockSize": 999_999,
                "devicePreference": "default",
                "prebufferMs": 999_999,
            }
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioPrewarmUnavailable");
        assert_eq!(
            response["payload"]["requestedFormat"]["sampleRate"],
            192_000
        );
        assert_eq!(response["payload"]["requestedFormat"]["channels"], 16);
        assert_eq!(response["payload"]["requestedFormat"]["prebufferMs"], 2_000);
        assert_eq!(response["payload"]["prewarmAvailable"], false);
        assert_eq!(response["payload"]["wasapiPrewarmAvailable"], false);
    }

    #[test]
    fn synthetic_prewarm_session_reports_stop_health() {
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 16,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 4,
            prewarm_id: "".to_string(),
        };

        let (mut session, payload) = start_synthetic_prewarm_impl(request).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let stop = session.stop("test");

        assert_eq!(payload["prewarmAvailable"], true);
        assert_eq!(payload["syntheticPrewarm"], true);
        assert_eq!(payload["wasapiPrewarm"], false);
        assert_eq!(payload["prebufferFrameTarget"], 4);
        assert_eq!(stop["stopped"], true);
        assert_eq!(stop["reason"], "test");
        assert_eq!(stop["source"], "synthetic-prewarm");
        assert!(stop["totalBlocksObserved"].as_u64().unwrap_or_default() > 0);
        assert!(stop["bufferedBlocks"].as_u64().unwrap_or_default() <= 4);
        assert!(stop["sidecarUptimeMs"].as_u64().is_some());
    }

    #[test]
    fn sidecar_rejects_protocol_mismatch_before_command_dispatch() {
        let request = json!({
            "protocolVersion": "2",
            "requestId": "r-bad-version",
            "command": "ping",
            "payload": {}
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "protocolVersionMismatch");
    }

    #[test]
    fn capture_request_clamps_to_frame_pipe_contract_bounds() {
        let payload = json!({
            "sampleRate": 999_999,
            "channels": 99,
            "blockSize": 999_999,
            "devicePreference": "default-capture-device-with-a-longer-than-needed-label",
            "prebufferMs": 999_999,
        });

        let request = CaptureRequest::from_payload(&payload);

        assert_eq!(request.sample_rate, 192_000);
        assert_eq!(request.channels, 16);
        assert_eq!(request.block_size, 16_384);
        assert_eq!(request.prebuffer_ms, 2_000);
        assert_eq!(request.native_endpoint_id_hash, "");
        assert!(request
            .device_preference
            .starts_with("default-capture-device"));
    }

    #[test]
    fn capture_request_clamps_low_values_away_from_invalid_audio_contracts() {
        let payload = json!({
            "sampleRate": 0,
            "channels": 0,
            "blockSize": 0,
            "devicePreference": "",
            "prebufferMs": 0,
        });

        let request = CaptureRequest::from_payload(&payload);

        assert_eq!(request.sample_rate, 8_000);
        assert_eq!(request.channels, 1);
        assert_eq!(request.block_size, 16);
        assert_eq!(request.device_preference, "default");
    }

    #[test]
    fn requested_prebuffer_frame_count_rounds_up_to_whole_blocks() {
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 512,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 400,
            prewarm_id: "".to_string(),
        };

        assert_eq!(requested_prebuffer_frame_count(&request), 13);

        let mut disabled = request.clone();
        disabled.prebuffer_ms = 0;
        assert_eq!(requested_prebuffer_frame_count(&disabled), 0);
    }

    #[test]
    fn native_endpoint_hash_matches_python_fixture() {
        let raw = r"SWD\MMDEVAPI\{0.0.1.00000000}.{secret-device-guid}";

        assert_eq!(hash_sensitive_identifier(raw), "e9a658ee3eff25fd");
        assert!(!hash_sensitive_identifier(raw).contains("secret-device-guid"));
    }

    #[test]
    fn endpoint_selection_payload_redacts_requested_selection() {
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 512,
            device_preference: "7".to_string(),
            port_audio_label: "Dock Mic, Windows WASAPI".to_string(),
            native_endpoint_id_hash: "endpoint-hash".to_string(),
            prebuffer_ms: 0,
            prewarm_id: "".to_string(),
        };

        let payload = endpoint_selection_payload(
            &request,
            Value::String("endpoint-hash".to_string()),
            "nativeEndpointHash",
            false,
            Value::Null,
        );

        assert_eq!(payload["requestedDevicePreference"], "7");
        assert_eq!(
            payload["requestedPortAudioLabel"],
            "Dock Mic, Windows WASAPI"
        );
        assert_eq!(payload["requestedNativeEndpointIdHash"], "endpoint-hash");
        assert_eq!(payload["selectedNativeEndpointIdHash"], "endpoint-hash");
        assert_eq!(payload["usedDefaultEndpoint"], false);
    }

    #[test]
    fn synthetic_capture_env_flag_is_explicit_opt_in() {
        assert!(!env_flag_enabled(None));
        assert!(!env_flag_enabled(Some("")));
        assert!(!env_flag_enabled(Some("false")));
        assert!(env_flag_enabled(Some("1")));
        assert!(env_flag_enabled(Some("enabled")));
    }

    #[test]
    fn wasapi_capture_env_flag_uses_same_explicit_opt_in_contract() {
        assert!(!env_flag_enabled(Some("off")));
        assert!(env_flag_enabled(Some("true")));
        assert!(env_flag_enabled(Some("on")));
    }

    #[cfg(windows)]
    #[test]
    fn wasapi_converter_downmixes_resamples_and_chunks_to_pcm_i16() {
        let source = WasapiMixFormat {
            format_tag: WAVE_FORMAT_IEEE_FLOAT_TAG,
            channels: 1,
            sample_rate: 48_000,
            average_bytes_per_second: 192_000,
            block_align: 4,
            bits_per_sample: 32,
            extra_size: 0,
            sample_format: WasapiSampleFormat::Float32,
        };
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 4,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 0,
            prewarm_id: "".to_string(),
        };
        let samples = [
            0.0_f32, 0.1, 0.2, 0.3, 0.4, 0.5, -0.5, -0.4, -0.3, -0.2, -0.1, 0.0,
        ];
        let mut bytes = Vec::new();
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        let mut converter = WasapiPcmConverter::new(source, &request);

        let blocks = converter
            .push_packet(bytes.as_ptr(), samples.len() as u32, false)
            .unwrap();

        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].len(), 8);
    }

    #[cfg(windows)]
    #[test]
    fn synthetic_capture_writes_prebuffer_then_live_frames() {
        use std::fs::OpenOptions;

        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 32,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 2,
            prewarm_id: "".to_string(),
        };
        let (mut session, payload) = start_synthetic_capture_impl(request, None).unwrap();
        let pipe_path = payload["framePipe"].as_str().unwrap();

        let mut reader = OpenOptions::new().read(true).open(pipe_path).unwrap();
        let (first_header, first_payload) = read_test_audio_frame(&mut reader);
        let (second_header, second_payload) = read_test_audio_frame(&mut reader);
        let stop_payload = session.stop("test");

        assert_eq!(first_header.sequence, 0);
        assert_eq!(first_header.frame_count, 32);
        assert_eq!(first_header.channels, 1);
        assert_eq!(
            first_header.flags & AUDIO_FRAME_FLAG_PREBUFFER,
            AUDIO_FRAME_FLAG_PREBUFFER
        );
        assert_eq!(first_payload.len(), 64);
        assert_eq!(second_header.sequence, 1);
        assert_eq!(second_header.flags & AUDIO_FRAME_FLAG_PREBUFFER, 0);
        assert_eq!(second_payload.len(), 64);
        assert_eq!(stop_payload["stopped"], true);
        assert!(
            stop_payload["prebufferFramesWritten"]
                .as_u64()
                .unwrap_or_default()
                >= 1
        );
        assert!(
            stop_payload["liveFramesWritten"]
                .as_u64()
                .unwrap_or_default()
                >= 1
        );
    }

    #[cfg(windows)]
    #[test]
    fn synthetic_capture_adopts_prewarm_buffer_before_live_frames() {
        use std::fs::OpenOptions;

        let prewarm_request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 16,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 1,
            prewarm_id: "".to_string(),
        };
        let (mut prewarm_session, _) = start_synthetic_prewarm_impl(prewarm_request).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let adopted = prewarm_session.snapshot_buffer();
        assert_eq!(adopted.block_count, 1);
        let prewarm_stop = prewarm_session.stop("adoptedIntoCapture");

        let capture_request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 16,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 1,
            prewarm_id: adopted.prewarm_id.clone(),
        };
        let (mut capture_session, payload) =
            start_synthetic_capture_impl(capture_request, Some((adopted, prewarm_stop))).unwrap();
        let pipe_path = payload["framePipe"].as_str().unwrap();

        let mut reader = OpenOptions::new().read(true).open(pipe_path).unwrap();
        let (first_header, first_payload) = read_test_audio_frame(&mut reader);
        let (second_header, second_payload) = read_test_audio_frame(&mut reader);
        let stop_payload = capture_session.stop("test");

        assert_eq!(payload["adoptedPrewarm"]["adopted"], true);
        assert_eq!(payload["adoptedPrewarm"]["blocks"], 1);
        assert_eq!(
            payload["adoptedPrewarm"]["stop"]["reason"],
            "adoptedIntoCapture"
        );
        assert_eq!(first_header.sequence, 0);
        assert_eq!(
            first_header.flags & AUDIO_FRAME_FLAG_PREBUFFER,
            AUDIO_FRAME_FLAG_PREBUFFER
        );
        assert_eq!(first_payload.len(), 32);
        assert_eq!(second_header.sequence, 1);
        assert_eq!(second_header.flags & AUDIO_FRAME_FLAG_PREBUFFER, 0);
        assert_eq!(second_payload.len(), 32);
        assert_eq!(stop_payload["prebufferFramesWritten"], 1);
        assert!(
            stop_payload["liveFramesWritten"]
                .as_u64()
                .unwrap_or_default()
                >= 1
        );
    }

    #[cfg(windows)]
    fn read_test_audio_frame<R: std::io::Read>(reader: &mut R) -> (AudioFrameHeader, Vec<u8>) {
        let mut header_bytes = [0_u8; AUDIO_FRAME_HEADER_LEN];
        reader.read_exact(&mut header_bytes).unwrap();
        let header = AudioFrameHeader::decode(&header_bytes).unwrap();
        let mut frame_payload = vec![0_u8; header.payload_len as usize];
        reader.read_exact(&mut frame_payload).unwrap();
        (header, frame_payload)
    }
}
