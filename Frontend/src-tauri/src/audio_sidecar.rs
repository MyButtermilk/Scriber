mod audio_frame_pipe;

use audio_frame_pipe::{
    encode_audio_frame, AudioFrameHeader, AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::{
    env,
    ffi::c_void,
    io::{self, BufRead, Write},
    process::ExitCode,
    sync::mpsc::{self, Sender, TryRecvError},
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
            eCapture, eConsole, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator,
            MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED, WAVEFORMATEX,
            WAVEFORMATEXTENSIBLE, WAVE_FORMAT_PCM,
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
}

impl AudioSidecarState {
    fn new() -> Self {
        Self {
            capture_sessions: HashMap::new(),
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

    fn start_capture(&mut self, request: CaptureRequest) -> Result<Value, String> {
        let result = if wasapi_capture_enabled() {
            start_wasapi_capture_impl(request)
        } else {
            start_synthetic_capture_impl(request)
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

    fn stop_all_sessions(&mut self, reason: &str) {
        let sessions: Vec<CaptureSession> = self
            .capture_sessions
            .drain()
            .map(|(_, session)| session)
            .collect();
        for mut session in sessions {
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
    prebuffer_ms: u32,
}

impl CaptureRequest {
    fn from_payload(payload: &Value) -> Self {
        Self {
            sample_rate: optional_u64(payload, "sampleRate", 16_000, 192_000).max(8_000) as u32,
            channels: optional_u64(payload, "channels", 1, 16).max(1) as u16,
            block_size: optional_u64(payload, "blockSize", 512, 16_384).max(16) as u32,
            device_preference: bounded_string(payload, "devicePreference", "default", 96),
            prebuffer_ms: optional_u64(payload, "prebufferMs", 0, 2_000) as u32,
        }
    }

    fn to_payload(&self) -> Value {
        json!({
            "sampleRate": self.sample_rate,
            "channels": self.channels,
            "blockSize": self.block_size,
            "devicePreference": self.device_preference,
            "prebufferMs": self.prebuffer_ms,
        })
    }
}

#[derive(Debug, Default)]
struct CaptureWriterStats {
    connected: bool,
    frames_written: u64,
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
            "bytesWritten": stats.bytes_written,
            "writerError": stats.error,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
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
) -> Result<(CaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_frame_pipe(&pipe_path)?;
    let (stop_tx, stop_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let join_handle = thread::Builder::new()
        .name("scriber-audio-synthetic-frame-pipe".to_string())
        .spawn(move || {
            run_synthetic_frame_pipe_writer(
                pipe_handle_value as HANDLE,
                writer_pipe_path,
                writer_request,
                stop_rx,
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
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
    });
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_synthetic_capture_impl(
    _request: CaptureRequest,
) -> Result<(CaptureSession, Value), String> {
    Err("synthetic frame-pipe capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
fn start_wasapi_capture_impl(request: CaptureRequest) -> Result<(CaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_frame_pipe(&pipe_path)?;
    let (stop_tx, stop_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let join_handle = thread::Builder::new()
        .name("scriber-audio-wasapi-frame-pipe".to_string())
        .spawn(move || {
            run_wasapi_capture_writer(
                pipe_handle_value as HANDLE,
                writer_pipe_path,
                writer_request,
                stop_rx,
                ready_tx,
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
fn start_wasapi_capture_impl(_request: CaptureRequest) -> Result<(CaptureSession, Value), String> {
    Err("WASAPI capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
#[derive(Debug, Clone)]
struct WasapiReady {
    endpoint_id_hash: Value,
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

    let payload_len = usize::from(request.channels) * request.block_size as usize * 2;
    let payload = vec![0_u8; payload_len];
    let frame_interval = Duration::from_secs_f64(
        (request.block_size as f64 / f64::from(request.sample_rate)).max(0.001),
    );
    let mut sequence = 0_u64;
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => break,
            Err(TryRecvError::Empty) => {}
        }

        let timestamp_micros = started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
        let header = match AudioFrameHeader::new(
            payload.len() as u32,
            sequence,
            timestamp_micros,
            request.block_size,
            request.channels,
            0,
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
fn run_wasapi_capture_writer(
    pipe_handle: HANDLE,
    _pipe_path: String,
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    ready_tx: Sender<Result<WasapiReady, String>>,
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
fn run_wasapi_capture_writer_inner(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    ready_tx: &Sender<Result<WasapiReady, String>>,
    ready_sent: &mut bool,
    stats: &mut CaptureWriterStats,
    started: Instant,
) -> Result<(), String> {
    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|err| format!("COM initialization failed for WASAPI capture: {err}"))?;

    let result = (|| -> Result<(), String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let device = unsafe { enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
            .map_err(|err| format!("default WASAPI capture endpoint unavailable: {err}"))?;
        let endpoint_id_hash = unsafe { wasapi_endpoint_id_hash(&device) };
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
                mix_format: mix_format.clone(),
            }))
            .map_err(|err| format!("could not report WASAPI readiness: {err}"))?;
        *ready_sent = true;

        wait_for_pipe_client(pipe_handle, stop_rx)?;
        stats.connected = true;
        unsafe { client.Start() }.map_err(|err| format!("WASAPI Start failed: {err}"))?;
        let capture_result = pump_wasapi_capture(
            pipe_handle,
            request,
            stop_rx,
            &capture_client,
            mix_format,
            stats,
            started,
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
fn pump_wasapi_capture(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    capture_client: &IAudioCaptureClient,
    mix_format: WasapiMixFormat,
    stats: &mut CaptureWriterStats,
    started: Instant,
) -> Result<(), String> {
    let mut converter = WasapiPcmConverter::new(mix_format, request);
    let mut sequence = 0_u64;
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
                let timestamp_micros =
                    started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
                let header = AudioFrameHeader::new(
                    payload.len() as u32,
                    sequence,
                    timestamp_micros,
                    request.block_size,
                    request.channels,
                    0,
                )
                .map_err(|err| format!("WASAPI frame header failed: {err}"))?;
                let frame = encode_audio_frame(&header, &payload)
                    .map_err(|err| format!("WASAPI frame encode failed: {err}"))?;
                let bytes_written = write_all_to_pipe(pipe_handle, &frame)?;
                stats.frames_written = stats.frames_written.saturating_add(1);
                stats.bytes_written = stats.bytes_written.saturating_add(u64::from(bytes_written));
                sequence = sequence.saturating_add(1);
            }
            packet_frames = unsafe { capture_client.GetNextPacketSize() }
                .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        }
    }
}

#[cfg(windows)]
unsafe fn wasapi_endpoint_id_hash(device: &windows::Win32::Media::Audio::IMMDevice) -> Value {
    let id = unsafe { device.GetId() }.ok();
    let Some(id) = id else {
        return Value::Null;
    };
    let text = unsafe { id.to_string() }.unwrap_or_default();
    unsafe {
        CoTaskMemFree(Some(id.as_ptr().cast::<c_void>()));
    }
    if text.is_empty() {
        Value::Null
    } else {
        Value::String(hash_sensitive_identifier(&text))
    }
}

fn hash_sensitive_identifier(value: &str) -> String {
    use std::hash::{Hash, Hasher};

    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    value.hash(&mut hasher);
    format!("{:016x}", hasher.finish())
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
        "commands": ["ping", "capabilities", "captureStart", "captureStop", "shutdown"],
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
            prebuffer_ms: 0,
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
    fn synthetic_capture_writes_a_valid_named_pipe_frame() {
        use std::{fs::OpenOptions, io::Read as _};

        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 32,
            device_preference: "default".to_string(),
            prebuffer_ms: 0,
        };
        let (mut session, payload) = start_synthetic_capture_impl(request).unwrap();
        let pipe_path = payload["framePipe"].as_str().unwrap();

        let mut reader = OpenOptions::new().read(true).open(pipe_path).unwrap();
        let mut header_bytes = [0_u8; AUDIO_FRAME_HEADER_LEN];
        reader.read_exact(&mut header_bytes).unwrap();
        let header = AudioFrameHeader::decode(&header_bytes).unwrap();
        let mut frame_payload = vec![0_u8; header.payload_len as usize];
        reader.read_exact(&mut frame_payload).unwrap();
        let stop_payload = session.stop("test");

        assert_eq!(header.sequence, 0);
        assert_eq!(header.frame_count, 32);
        assert_eq!(header.channels, 1);
        assert_eq!(frame_payload.len(), 64);
        assert_eq!(stop_payload["stopped"], true);
        assert!(stop_payload["framesWritten"].as_u64().unwrap_or_default() >= 1);
    }
}
