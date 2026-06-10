mod audio_frame_pipe;

use audio_frame_pipe::{
    encode_audio_frame, AudioFrameHeader, AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::{
    env,
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

const SIDECAR_PROTOCOL_VERSION: &str = "1";
const SIDECAR_NAME: &str = "scriber-audio-sidecar";
const SYNTHETIC_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE";

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
    synthetic_sessions: HashMap<String, SyntheticCaptureSession>,
}

impl AudioSidecarState {
    fn new() -> Self {
        Self {
            synthetic_sessions: HashMap::new(),
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
        if !synthetic_capture_enabled() {
            return response_payload(
                request_id,
                false,
                "audioCaptureUnavailable",
                "WASAPI capture is not implemented in this sidecar skeleton",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "captureAvailable": false,
                    "requestedFormat": request.to_payload(),
                    "audioFrameProtocol": audio_frame_protocol_payload(),
                    "syntheticFramePipeAvailable": false,
                }),
            );
        }

        match self.start_synthetic_capture(request) {
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
                    "syntheticFramePipeAvailable": true,
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
        let stop_payload = if let Some(mut session) = self.synthetic_sessions.remove(&stream_id) {
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

    fn start_synthetic_capture(&mut self, request: CaptureRequest) -> Result<Value, String> {
        start_synthetic_capture_impl(request).map(|(session, payload)| {
            let stream_id = session.stream_id.clone();
            if let Some(mut old_session) = self.synthetic_sessions.remove(&stream_id) {
                let _ = old_session.stop("duplicateStreamId");
            }
            self.synthetic_sessions.insert(stream_id, session);
            payload
        })
    }

    fn stop_all_sessions(&mut self, reason: &str) {
        let sessions: Vec<SyntheticCaptureSession> = self
            .synthetic_sessions
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
            sample_rate: optional_u64(payload, "sampleRate", 16_000, 192_000) as u32,
            channels: optional_u64(payload, "channels", 1, 16) as u16,
            block_size: optional_u64(payload, "blockSize", 512, 16_384) as u32,
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
struct SyntheticWriterStats {
    connected: bool,
    frames_written: u64,
    bytes_written: u64,
    error: Option<String>,
}

struct SyntheticCaptureSession {
    stream_id: String,
    stop_tx: Sender<()>,
    join_handle: Option<JoinHandle<SyntheticWriterStats>>,
    started_at: Instant,
}

impl SyntheticCaptureSession {
    fn stop(&mut self, reason: &str) -> Value {
        let _ = self.stop_tx.send(());
        let stats = self
            .join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| SyntheticWriterStats {
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
            "source": "synthetic-frame-pipe",
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

fn env_flag_enabled(raw: Option<&str>) -> bool {
    matches!(
        raw.unwrap_or_default().trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on" | "enabled"
    )
}

#[cfg(windows)]
fn start_synthetic_capture_impl(
    request: CaptureRequest,
) -> Result<(SyntheticCaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_synthetic_frame_pipe(&pipe_path)?;
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
    let session = SyntheticCaptureSession {
        stream_id: stream_id.clone(),
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
) -> Result<(SyntheticCaptureSession, Value), String> {
    Err("synthetic frame-pipe capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
fn create_synthetic_frame_pipe(pipe_path: &str) -> Result<HANDLE, String> {
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
) -> SyntheticWriterStats {
    let mut stats = SyntheticWriterStats::default();
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
        "captureAvailable": false,
        "captureUnavailableReason": "wasapiCaptureNotImplemented",
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
    fn synthetic_capture_env_flag_is_explicit_opt_in() {
        assert!(!env_flag_enabled(None));
        assert!(!env_flag_enabled(Some("")));
        assert!(!env_flag_enabled(Some("false")));
        assert!(env_flag_enabled(Some("1")));
        assert!(env_flag_enabled(Some("enabled")));
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
