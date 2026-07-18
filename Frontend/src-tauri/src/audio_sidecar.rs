mod audio_frame_pipe;
mod meeting_aec;
mod redaction;

use audio_frame_pipe::{
    encode_audio_frame, AudioFrameHeader, AUDIO_FRAME_FLAG_END_OF_STREAM,
    AUDIO_FRAME_FLAG_PREBUFFER, AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION,
};
use meeting_aec::{MeetingAec3, MEETING_AEC_FRAME_SAMPLES};
use redaction::hash_sensitive_identifier;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::{
    env,
    ffi::c_void,
    io::{self, BufRead, Read, Write},
    process::ExitCode,
    sync::{
        atomic::{AtomicBool, AtomicU8, Ordering},
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
        CloseHandle, GetLastError, ERROR_NO_DATA, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED,
        ERROR_PIPE_LISTENING, GENERIC_READ, HANDLE, INVALID_HANDLE_VALUE,
    },
    Storage::FileSystem::{
        CreateFileW, FlushFileBuffers, ReadFile, WriteFile, FILE_ATTRIBUTE_NORMAL, OPEN_EXISTING,
        PIPE_ACCESS_OUTBOUND,
    },
    System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, SetNamedPipeHandleState,
        PIPE_NOWAIT, PIPE_READMODE_BYTE, PIPE_TYPE_BYTE, PIPE_WAIT,
    },
};

#[cfg(windows)]
use windows::{
    core::GUID,
    Win32::{
        Media::Audio::{
            eCapture, eConsole, eRender, IAudioCaptureClient, IAudioClient, IMMDevice,
            IMMDeviceEnumerator, MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT,
            AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, DEVICE_STATE_ACTIVE,
            WAVEFORMATEX, WAVEFORMATEXTENSIBLE, WAVE_FORMAT_PCM,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL,
            COINIT_MULTITHREADED,
        },
    },
};

const SIDECAR_PROTOCOL_VERSION: &str = "1";
const SIDECAR_NAME: &str = "scriber-audio-sidecar";
const SIDECAR_JSON_LINE_MAX_BYTES: usize = 1024 * 1024;
const SYNTHETIC_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE";
const SYNTHETIC_SIGNAL_ENV: &str = "SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL";
const SYNTHETIC_MIC_PCM_ENV: &str = "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH";
const SYNTHETIC_PCM_MAX_BYTES: u64 = 64 * 1024 * 1024;
const WASAPI_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE";
const DISABLE_WASAPI_CAPTURE_ENV: &str = "SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE";
const FRAME_PIPE_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const FRAME_PIPE_WRITE_TIMEOUT: Duration = Duration::from_millis(500);
const PREWARM_BURST_WRITE_TIMEOUT: Duration = Duration::from_secs(2);
const PREWARM_PROMOTION_ACCEPT_TIMEOUT: Duration = Duration::from_secs(1);
const PREWARM_PROMOTION_ACCEPTED_COMPLETION_TIMEOUT: Duration = Duration::from_millis(500);
const PREWARM_PROMOTION_ENDPOINT_RESOLUTION_TIMEOUT: Duration = Duration::from_millis(300);
const PROMOTED_CAPTURE_STOP_JOIN_TIMEOUT: Duration = Duration::from_millis(750);
static PROMOTION_ENDPOINT_RESOLVER_ACTIVE: AtomicBool = AtomicBool::new(false);
// Handoff blocks are retained only while the replacement WASAPI client is
// being initialized.  Keep that queue bounded without making the prewarm
// worker wait on the capture writer.  A limit breach fails the capture
// visibly; it must never silently turn into a successful handoff with missing
// audio.
const PREWARM_HANDOFF_TAIL_MIN_BYTES: usize = 8 * 1024 * 1024;
const PREWARM_HANDOFF_TAIL_MAX_BYTES: usize = 64 * 1024 * 1024;
const IN_PLACE_PREWARM_HANDOFF_MODE: &str = "in-place-iaudio-client-promotion";
const WAVE_FORMAT_IEEE_FLOAT_TAG: u16 = 3;
const WAVE_FORMAT_EXTENSIBLE_TAG: u16 = 0xfffe;

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct WasapiInitializeArgs {
    stream_flags: u32,
    buffer_duration_100ns: i64,
    periodicity_100ns: i64,
}

#[cfg(windows)]
fn wasapi_shared_initialize_args(capture_kind: &str) -> WasapiInitializeArgs {
    WasapiInitializeArgs {
        stream_flags: if capture_kind.eq_ignore_ascii_case("loopback") {
            AUDCLNT_STREAMFLAGS_LOOPBACK
        } else {
            0
        },
        buffer_duration_100ns: 1_000_000,
        // Shared-mode clients must pass zero. Loopback is represented solely
        // by stream_flags and must never leak into this timing argument.
        periodicity_100ns: 0,
    }
}
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
    let mut stdin = stdin.lock();
    let mut stdout = io::stdout().lock();
    let mut state = AudioSidecarState::new();
    loop {
        let mut line = String::new();
        let bytes_read = read_json_line_limited(&mut stdin, &mut line).map_err(|_| ())?;
        if bytes_read == 0 {
            break;
        }
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

fn read_json_line_limited<R: BufRead>(reader: &mut R, line: &mut String) -> io::Result<usize> {
    line.clear();
    let bytes_read = reader
        .take((SIDECAR_JSON_LINE_MAX_BYTES + 1) as u64)
        .read_line(line)?;
    if bytes_read > SIDECAR_JSON_LINE_MAX_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "audio sidecar request line exceeded limit",
        ));
    }
    Ok(bytes_read)
}

fn write_json_line(payload: &Value) -> Result<(), ()> {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{payload}").map_err(|_| ())?;
    stdout.flush().map_err(|_| ())
}

struct AudioSidecarState {
    capture_sessions: HashMap<String, CaptureSession>,
    prewarm_sessions: HashMap<String, PrewarmSession>,
    meeting_sessions: HashMap<String, MeetingCaptureSession>,
}

impl AudioSidecarState {
    fn new() -> Self {
        Self {
            capture_sessions: HashMap::new(),
            prewarm_sessions: HashMap::new(),
            meeting_sessions: HashMap::new(),
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
            "captureStatus" => self.handle_capture_status(request_id, payload, started),
            "captureStop" => self.handle_capture_stop(request_id, payload, started),
            "meetingCaptureStart" => {
                self.handle_meeting_capture_start(request_id, payload, started)
            }
            "meetingCaptureStatus" => {
                self.handle_meeting_capture_status(request_id, payload, started)
            }
            "meetingCaptureStop" => self.handle_meeting_capture_stop(request_id, payload, started),
            "prewarmStart" => self.handle_prewarm_start(request_id, payload, started),
            "prewarmStatus" => self.handle_prewarm_status(request_id, payload, started),
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
                "Rust audio capture is unavailable",
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

    fn handle_capture_status(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let stream_id = bounded_string(payload, "streamId", "", 96);
        let Some(session) = self.capture_sessions.get(&stream_id) else {
            return response_payload(
                request_id,
                true,
                "",
                "",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "active": false,
                    "streamId": stream_id,
                    "reason": "noActiveCapture",
                }),
            );
        };

        if session.worker_finished() {
            let stop_payload = self
                .capture_sessions
                .remove(&stream_id)
                .map(|mut session| session.stop("captureStatusWorkerFinished"))
                .unwrap_or_else(|| json!({"stopped": false, "reason": "noActiveCapture"}));
            return response_payload(
                request_id,
                true,
                "",
                "",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "active": false,
                    "streamId": stream_id,
                    "reason": "captureWorkerFinished",
                    "stop": stop_payload,
                }),
            );
        }

        response_payload(request_id, true, "", "", started, session.status())
    }

    fn handle_meeting_capture_start(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        match self.start_meeting_capture(payload) {
            Ok(payload) => response_payload(request_id, true, "", "", started, payload),
            Err(reason) => response_payload(
                request_id,
                false,
                "meetingCaptureUnavailable",
                &reason,
                started,
                json!({"sidecar": SIDECAR_NAME, "meetingCaptureAvailable": false}),
            ),
        }
    }

    fn handle_meeting_capture_status(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let meeting_capture_id = bounded_string(payload, "meetingCaptureId", "", 96);
        let value = self
            .meeting_sessions
            .get(&meeting_capture_id)
            .map(MeetingCaptureSession::status)
            .unwrap_or_else(|| {
                json!({
                    "active": false, "meetingCaptureId": meeting_capture_id,
                    "reason": "noActiveMeetingCapture"
                })
            });
        response_payload(request_id, true, "", "", started, value)
    }

    fn handle_meeting_capture_stop(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let meeting_capture_id = bounded_string(payload, "meetingCaptureId", "", 96);
        let value = self.stop_meeting_capture(&meeting_capture_id, "meetingCaptureStop");
        response_payload(request_id, true, "", "", started, value)
    }

    fn start_meeting_capture(&mut self, payload: &Value) -> Result<Value, String> {
        if !wasapi_capture_enabled() && !synthetic_capture_enabled() {
            return Err("Rust meeting capture is unavailable".to_string());
        }
        let meeting_clock_origin = Instant::now();
        let mut microphone_request = CaptureRequest::from_payload(payload);
        microphone_request.clock_origin = Some(meeting_clock_origin);
        microphone_request.capture_kind = "microphone".to_string();
        microphone_request.sample_rate = 48_000;
        microphone_request.channels = 1;
        microphone_request.block_size = 480;
        microphone_request.prewarm_id.clear();
        microphone_request.native_endpoint_id_hash =
            bounded_string(payload, "microphoneNativeEndpointIdHash", "", 128);
        microphone_request.device_preference =
            if microphone_request.native_endpoint_id_hash.is_empty() {
                "default".to_string()
            } else {
                "selected".to_string()
            };
        let microphone = self.start_capture(microphone_request)?;
        let microphone_stream_id = microphone
            .get("streamId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let microphone_pipe = microphone
            .get("framePipe")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();

        let mut system_request = CaptureRequest::from_payload(payload);
        system_request.clock_origin = Some(meeting_clock_origin);
        system_request.capture_kind = "loopback".to_string();
        system_request.sample_rate = 48_000;
        system_request.channels = 1;
        system_request.block_size = 480;
        system_request.prewarm_id.clear();
        system_request.native_endpoint_id_hash =
            bounded_string(payload, "renderNativeEndpointIdHash", "", 128);
        system_request.device_preference = if system_request.native_endpoint_id_hash.is_empty() {
            "default".to_string()
        } else {
            "selected".to_string()
        };
        let system = match self.start_capture(system_request) {
            Ok(value) => value,
            Err(error) => {
                if let Some(mut session) = self.capture_sessions.remove(&microphone_stream_id) {
                    let _ = session.stop("meetingSystemCaptureFailed");
                }
                return Err(error);
            }
        };
        let system_stream_id = system
            .get("streamId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let system_pipe = system
            .get("framePipe")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let meeting_capture_id = Uuid::new_v4().simple().to_string();
        let aec_enabled = payload
            .get("aecEnabled")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        match start_meeting_aec_relay(
            &meeting_capture_id,
            microphone_stream_id.clone(),
            system_stream_id.clone(),
            microphone_pipe,
            system_pipe,
            payload
                .get("aecDelayMs")
                .and_then(Value::as_i64)
                .unwrap_or(80)
                .clamp(0, 500) as i32,
            aec_enabled,
        ) {
            Ok((session, sources)) => {
                self.meeting_sessions
                    .insert(meeting_capture_id.clone(), session);
                Ok(json!({
                    "sidecar": SIDECAR_NAME,
                    "meetingCaptureId": meeting_capture_id,
                    "sampleRate": 16000,
                    "frameDurationMs": 10,
                    "clockMode": "windowsQueryPerformanceCounter",
                    "aecActive": aec_enabled,
                    "sources": sources,
                }))
            }
            Err(error) => {
                for stream_id in [&microphone_stream_id, &system_stream_id] {
                    if let Some(mut session) = self.capture_sessions.remove(stream_id) {
                        let _ = session.stop("meetingAecRelayStartFailed");
                    }
                }
                Err(error)
            }
        }
    }

    fn stop_meeting_capture(&mut self, meeting_capture_id: &str, reason: &str) -> Value {
        let Some(mut meeting) = self.meeting_sessions.remove(meeting_capture_id) else {
            return json!({"stopped": false, "meetingCaptureId": meeting_capture_id,
                          "reason": "noActiveMeetingCapture"});
        };
        let stream_ids = meeting.capture_stream_ids.clone();
        let relay = meeting.stop(reason);
        let sources: Vec<Value> = stream_ids
            .iter()
            .filter_map(|stream_id| {
                self.capture_sessions
                    .remove(stream_id)
                    .map(|mut session| session.stop(reason))
            })
            .collect();
        json!({"stopped": true, "meetingCaptureId": meeting_capture_id,
               "reason": reason, "relay": relay, "sources": sources})
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
                "Rust audio prewarm is unavailable",
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

    fn handle_prewarm_status(
        &mut self,
        request_id: &str,
        payload: &Value,
        started: Instant,
    ) -> Value {
        let prewarm_id = bounded_string(payload, "prewarmId", "", 96);
        let Some(session) = self.prewarm_sessions.get(&prewarm_id) else {
            return response_payload(
                request_id,
                true,
                "",
                "",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "active": false,
                    "prewarmId": prewarm_id,
                    "reason": "noActivePrewarm",
                }),
            );
        };

        if session.worker_finished() {
            let stop_payload = self
                .prewarm_sessions
                .remove(&prewarm_id)
                .map(|mut session| session.stop("prewarmStatusWorkerFinished"))
                .unwrap_or_else(|| {
                    json!({
                        "sidecar": SIDECAR_NAME,
                        "stopped": false,
                        "prewarmId": prewarm_id,
                        "reason": "noActivePrewarm",
                    })
                });
            return response_payload(
                request_id,
                true,
                "",
                "",
                started,
                json!({
                    "sidecar": SIDECAR_NAME,
                    "active": false,
                    "prewarmId": prewarm_id,
                    "reason": "prewarmWorkerFinished",
                    "stop": stop_payload,
                }),
            );
        }

        response_payload(request_id, true, "", "", started, session.status())
    }

    fn start_capture(&mut self, request: CaptureRequest) -> Result<Value, String> {
        let use_wasapi_capture = wasapi_capture_enabled();
        let result = if request.prewarm_id.trim().is_empty() {
            if use_wasapi_capture {
                start_wasapi_capture_impl(request, None, None)
            } else {
                start_synthetic_capture_impl(request, None)
            }
        } else {
            let prewarm_id = request.prewarm_id.clone();
            let Some(mut prewarm_session) = self.prewarm_sessions.remove(&prewarm_id) else {
                return Err(format!("requested prewarmId was not found: {prewarm_id}"));
            };

            if use_wasapi_capture && prewarm_session.source == "wasapi-prewarm" {
                match start_wasapi_promoted_capture(request.clone(), Box::new(prewarm_session)) {
                    Ok(result) => Ok(result),
                    Err((failure, returned_session)) => match failure.fallback {
                        PrewarmPromotionFallback::AdoptWithOverlap => {
                            start_wasapi_replacement_with_adopted_prewarm(
                                request,
                                returned_session,
                                &failure.reason,
                            )
                        }
                        PrewarmPromotionFallback::ColdWithOverlap => {
                            start_wasapi_cold_replacement_with_overlap(
                                request,
                                returned_session,
                                &failure.reason,
                            )
                        }
                        PrewarmPromotionFallback::Abort => {
                            // Once the prewarm worker atomically accepted an in-place
                            // promotion, it owns the handoff.  Starting a second capture
                            // would risk duplicating or dropping the prefix, so fail the
                            // request and let the supervisor's bounded sidecar cleanup
                            // reclaim the worker.
                            returned_session.request_stop();
                            Err(failure.reason)
                        }
                    },
                }
            } else if use_wasapi_capture {
                start_wasapi_cold_replacement_with_overlap(
                    request,
                    Box::new(prewarm_session),
                    "prewarmPromotionSourceMismatch",
                )
            } else {
                let mut adopted = match prewarm_session.begin_handoff() {
                    Ok(adopted) => adopted,
                    Err(err) => {
                        let _ = prewarm_session.stop("captureStartFailed");
                        return Err(err);
                    }
                };
                let (stop_payload, tail_blocks) =
                    prewarm_session.stop_and_finish_handoff("adoptedIntoCapture");
                adopted.append_tail(tail_blocks?, request.block_size);
                start_synthetic_capture_impl(request, Some((adopted, stop_payload)))
            }
        };
        match result {
            Ok((session, payload)) => {
                let stream_id = session.stream_id.clone();
                if let Some(mut old_session) = self.capture_sessions.remove(&stream_id) {
                    let _ = old_session.stop("duplicateStreamId");
                }
                self.capture_sessions.insert(stream_id, session);
                Ok(payload)
            }
            Err(err) => Err(err),
        }
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
        let meeting_ids: Vec<String> = self.meeting_sessions.keys().cloned().collect();
        for meeting_id in meeting_ids {
            let _ = self.stop_meeting_capture(&meeting_id, reason);
        }
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
    capture_kind: String,
    clock_origin: Option<Instant>,
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
            // Normal always-on prewarm uses a short rolling buffer. The
            // explicit first-hotkey path may retain up to six seconds while
            // Python lazily imports Pipecat, preventing the opening words from
            // being lost without turning the microphone on before user intent.
            prebuffer_ms: optional_u64(payload, "prebufferMs", 0, 6_000) as u32,
            prewarm_id: bounded_string(payload, "prewarmId", "", 96),
            capture_kind: bounded_string(payload, "captureKind", "microphone", 24),
            clock_origin: None,
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
            "captureKind": self.capture_kind,
            "sharedClock": self.clock_origin.is_some(),
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
    deferred_prewarm_stop: Option<Value>,
    error: Option<String>,
}

struct CaptureSession {
    stream_id: String,
    source: &'static str,
    worker: CaptureSessionWorker,
    started_at: Instant,
}

enum CaptureSessionWorker {
    Dedicated {
        stop_tx: Sender<()>,
        join_handle: Option<JoinHandle<CaptureWriterStats>>,
    },
    PromotedPrewarm(Box<PrewarmSession>),
}

impl CaptureSession {
    fn worker_finished(&self) -> bool {
        match &self.worker {
            CaptureSessionWorker::Dedicated { join_handle, .. } => join_handle
                .as_ref()
                .map(|handle| handle.is_finished())
                .unwrap_or(true),
            CaptureSessionWorker::PromotedPrewarm(session) => session.worker_finished(),
        }
    }

    fn status(&self) -> Value {
        let worker_finished = self.worker_finished();
        json!({
            "sidecar": SIDECAR_NAME,
            "active": !worker_finished,
            "streamId": self.stream_id,
            "reason": if worker_finished { "captureWorkerFinished" } else { "active" },
            "source": self.source,
            "workerFinished": worker_finished,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }

    fn stop(&mut self, reason: &str) -> Value {
        if let CaptureSessionWorker::PromotedPrewarm(session) = &mut self.worker {
            return session.stop_promoted_capture(&self.stream_id, reason, self.started_at);
        }

        let CaptureSessionWorker::Dedicated {
            stop_tx,
            join_handle,
        } = &mut self.worker
        else {
            unreachable!("promoted capture returned above")
        };
        let _ = stop_tx.send(());
        let stats = join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| CaptureWriterStats {
                    error: Some("writerThreadPanicked".to_string()),
                    ..CaptureWriterStats::default()
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
            "deferredPrewarmStop": stats.deferred_prewarm_stop,
            "writerError": stats.error,
            "wasapiClientReused": false,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }
}

#[derive(Debug, Default)]
struct MeetingRelayStats {
    frames_processed: u64,
    bytes_forwarded: u64,
    aec_render_active_frames: u64,
    aec_render_energy: f64,
    aec_raw_mic_energy: f64,
    aec_clean_mic_energy: f64,
    microphone_padding_frames: u64,
    system_padding_frames: u64,
    max_input_skew_micros: u64,
    error: Option<String>,
}

impl MeetingRelayStats {
    fn aec_metrics(&self, enabled: bool) -> Value {
        let echo_reduction_db = if enabled
            && self.aec_render_active_frames > 0
            && self.aec_raw_mic_energy > 0.0
            && self.aec_clean_mic_energy > 0.0
        {
            Some(
                (10.0 * (self.aec_raw_mic_energy / self.aec_clean_mic_energy).log10())
                    .clamp(-60.0, 60.0),
            )
        } else {
            None
        };
        json!({
            "measurement": "render-active-raw-to-clean-energy-ratio",
            "renderActiveFrames": self.aec_render_active_frames,
            "renderActiveDurationMs": self.aec_render_active_frames.saturating_mul(10),
            "renderEnergy": self.aec_render_energy,
            "rawMicEnergy": self.aec_raw_mic_energy,
            "cleanMicEnergy": self.aec_clean_mic_energy,
            "echoReductionDb": echo_reduction_db,
        })
    }
}

struct MeetingCaptureSession {
    meeting_capture_id: String,
    capture_stream_ids: Vec<String>,
    stop_tx: Sender<()>,
    join_handle: Option<JoinHandle<MeetingRelayStats>>,
    started_at: Instant,
    aec_enabled: bool,
}

impl MeetingCaptureSession {
    fn worker_finished(&self) -> bool {
        self.join_handle
            .as_ref()
            .map(|handle| handle.is_finished())
            .unwrap_or(true)
    }

    fn status(&self) -> Value {
        let finished = self.worker_finished();
        json!({
            "active": !finished,
            "meetingCaptureId": self.meeting_capture_id,
            "reason": if finished { "meetingRelayFinished" } else { "active" },
            "workerFinished": finished,
            "aecActive": self.aec_enabled && !finished,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }

    fn stop(&mut self, reason: &str) -> Value {
        let _ = self.stop_tx.send(());
        let stats = self
            .join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| MeetingRelayStats {
                    error: Some("meetingRelayThreadPanicked".to_string()),
                    ..Default::default()
                })
            })
            .unwrap_or_default();
        let aec_metrics = stats.aec_metrics(self.aec_enabled);
        json!({
            "stopped": true, "meetingCaptureId": self.meeting_capture_id, "reason": reason,
            "aecActive": false, "framesProcessed": stats.frames_processed,
            "bytesForwarded": stats.bytes_forwarded, "relayError": stats.error,
            "microphonePaddingFrames": stats.microphone_padding_frames,
            "systemPaddingFrames": stats.system_padding_frames,
            "maxInputSkewMicros": stats.max_input_skew_micros,
            "aecMetrics": aec_metrics,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }
}

#[cfg(windows)]
fn start_meeting_aec_relay(
    meeting_capture_id: &str,
    microphone_stream_id: String,
    system_stream_id: String,
    microphone_pipe: String,
    system_pipe: String,
    delay_ms: i32,
    aec_enabled: bool,
) -> Result<(MeetingCaptureSession, Value), String> {
    let pipe_specs = [
        (
            "mic_raw",
            format!(r"\\.\pipe\scriber-meeting-{meeting_capture_id}-mic-raw"),
        ),
        (
            "system",
            format!(r"\\.\pipe\scriber-meeting-{meeting_capture_id}-system"),
        ),
        (
            "mic_clean",
            format!(r"\\.\pipe\scriber-meeting-{meeting_capture_id}-mic-clean"),
        ),
    ];
    let mut handles = Vec::new();
    for (_, path) in &pipe_specs {
        match create_meeting_output_pipe(path) {
            Ok(handle) => handles.push(handle),
            Err(error) => {
                for handle in handles {
                    unsafe {
                        CloseHandle(handle);
                    }
                }
                return Err(error);
            }
        }
    }
    let (stop_tx, stop_rx) = mpsc::channel();
    let thread_handles: Vec<isize> = handles.iter().map(|handle| *handle as isize).collect();
    let join_handle = thread::Builder::new()
        .name("scriber-meeting-aec3-relay".to_string())
        .spawn(move || {
            run_meeting_aec_relay(
                microphone_pipe,
                system_pipe,
                thread_handles,
                stop_rx,
                delay_ms,
                aec_enabled,
            )
        })
        .map_err(|error| {
            for handle in handles {
                unsafe {
                    CloseHandle(handle);
                }
            }
            format!("meeting AEC3 relay thread spawn failed: {error}")
        })?;
    let session = MeetingCaptureSession {
        meeting_capture_id: meeting_capture_id.to_string(),
        capture_stream_ids: vec![microphone_stream_id, system_stream_id],
        stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
        aec_enabled,
    };
    let sources = json!([
        {"source": "microphone", "trackKind": "mic_raw", "framePipe": pipe_specs[0].1,
         "timelineOffsetMs": 0},
        {"source": "system", "trackKind": "system", "framePipe": pipe_specs[1].1,
         "timelineOffsetMs": 0},
        {"source": "mic_clean", "trackKind": "mic_clean", "framePipe": pipe_specs[2].1,
         "timelineOffsetMs": 0},
    ]);
    Ok((session, sources))
}

#[cfg(not(windows))]
fn start_meeting_aec_relay(
    _meeting_capture_id: &str,
    _microphone_stream_id: String,
    _system_stream_id: String,
    _microphone_pipe: String,
    _system_pipe: String,
    _delay_ms: i32,
    _aec_enabled: bool,
) -> Result<(MeetingCaptureSession, Value), String> {
    Err("meeting AEC3 relay is only implemented on Windows".to_string())
}

#[derive(Debug, Default)]
struct PrewarmStats {
    total_blocks_observed: u64,
    total_audio_frames_observed: u64,
    buffered_blocks: u64,
    buffered_audio_frames: u64,
    buffered_payload_bytes: u64,
    promoted_capture: Option<CaptureWriterStats>,
    error: Option<String>,
}

struct PrewarmPromotionRequest {
    pipe: PromotionPipeHandle,
    request: CaptureRequest,
    current_endpoint_id_hash: Value,
    decision: Arc<PrewarmPromotionDecision>,
    accepted_tx: Sender<Result<PrewarmPromotionAccepted, PrewarmPromotionFailure>>,
}

enum PrewarmWorkerCommand {
    Stop,
    Promote(Box<PrewarmPromotionRequest>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct PrewarmPromotionAccepted {
    blocks: u64,
    audio_frames: u64,
    payload_bytes: u64,
}

const PREWARM_PROMOTION_PENDING: u8 = 0;
const PREWARM_PROMOTION_ACCEPTED: u8 = 1;
const PREWARM_PROMOTION_CANCELLED: u8 = 2;

#[derive(Debug, Default)]
struct PrewarmPromotionDecision {
    state: AtomicU8,
}

impl PrewarmPromotionDecision {
    fn try_accept(&self) -> bool {
        self.state
            .compare_exchange(
                PREWARM_PROMOTION_PENDING,
                PREWARM_PROMOTION_ACCEPTED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }

    fn cancel_if_pending(&self) -> bool {
        self.state
            .compare_exchange(
                PREWARM_PROMOTION_PENDING,
                PREWARM_PROMOTION_CANCELLED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
    }
}

struct PromotionEndpointResolverLease;

impl PromotionEndpointResolverLease {
    fn try_acquire() -> Option<Self> {
        PROMOTION_ENDPOINT_RESOLVER_ACTIVE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| Self)
    }
}

impl Drop for PromotionEndpointResolverLease {
    fn drop(&mut self) {
        PROMOTION_ENDPOINT_RESOLVER_ACTIVE.store(false, Ordering::Release);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PrewarmPromotionFallback {
    AdoptWithOverlap,
    ColdWithOverlap,
    Abort,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PrewarmPromotionFailure {
    reason: String,
    fallback: PrewarmPromotionFallback,
}

impl PrewarmPromotionFailure {
    fn new(reason: impl Into<String>, fallback: PrewarmPromotionFallback) -> Self {
        Self {
            reason: reason.into(),
            fallback,
        }
    }
}

#[derive(Debug)]
struct PromotionPipeHandle {
    raw: isize,
}

impl PromotionPipeHandle {
    fn new(raw: isize) -> Self {
        Self { raw }
    }

    #[cfg(windows)]
    fn raw(&self) -> HANDLE {
        self.raw as HANDLE
    }
}

impl Drop for PromotionPipeHandle {
    fn drop(&mut self) {
        #[cfg(windows)]
        if self.raw != 0 && self.raw != INVALID_HANDLE_VALUE as isize {
            unsafe {
                CloseHandle(self.raw as HANDLE);
            }
            self.raw = 0;
        }
    }
}

#[derive(Debug, Default)]
struct PrewarmHandoffTail {
    blocks: VecDeque<Vec<u8>>,
    payload_bytes: usize,
    max_payload_bytes: usize,
    overflowed: bool,
}

#[derive(Debug, Default)]
struct PrewarmBuffer {
    max_blocks: usize,
    blocks: VecDeque<Vec<u8>>,
    handoff_tail: Option<PrewarmHandoffTail>,
}

impl PrewarmBuffer {
    fn new(max_blocks: u32) -> Self {
        Self {
            max_blocks: max_blocks as usize,
            blocks: VecDeque::new(),
            handoff_tail: None,
        }
    }

    fn push(&mut self, payload: Vec<u8>) -> Result<(), &'static str> {
        if let Some(handoff_tail) = self.handoff_tail.as_mut() {
            let next_payload_bytes = handoff_tail.payload_bytes.saturating_add(payload.len());
            if next_payload_bytes > handoff_tail.max_payload_bytes {
                handoff_tail.overflowed = true;
                return Err("prewarmHandoffTailOverflow");
            }
            handoff_tail.blocks.push_back(payload);
            handoff_tail.payload_bytes = next_payload_bytes;
            return Ok(());
        }
        if self.max_blocks == 0 {
            return Ok(());
        }
        while self.blocks.len() >= self.max_blocks {
            self.blocks.pop_front();
        }
        self.blocks.push_back(payload);
        Ok(())
    }

    fn begin_handoff(&mut self) -> Result<Vec<Vec<u8>>, &'static str> {
        self.begin_handoff_with_limit(None)
    }

    fn begin_handoff_with_limit(
        &mut self,
        tail_limit_override: Option<usize>,
    ) -> Result<Vec<Vec<u8>>, &'static str> {
        if self.handoff_tail.is_some() {
            return Err("prewarmHandoffAlreadyStarted");
        }
        let rolling_payload_bytes = self.rolling_payload_bytes();
        let max_payload_bytes = tail_limit_override.unwrap_or_else(|| {
            rolling_payload_bytes.saturating_mul(4).clamp(
                PREWARM_HANDOFF_TAIL_MIN_BYTES,
                PREWARM_HANDOFF_TAIL_MAX_BYTES,
            )
        });
        self.handoff_tail = Some(PrewarmHandoffTail {
            max_payload_bytes,
            ..PrewarmHandoffTail::default()
        });
        // Transfer rather than clone the initial rolling window. From this
        // exact lock boundary onward every producer block goes to handoff_tail,
        // so the two collections are disjoint and preserve arrival order.
        Ok(self.blocks.drain(..).collect())
    }

    fn finish_handoff(&mut self) -> Result<Vec<Vec<u8>>, &'static str> {
        let handoff_tail = self.handoff_tail.take().ok_or("prewarmHandoffNotStarted")?;
        if handoff_tail.overflowed {
            return Err("prewarmHandoffTailOverflow");
        }
        Ok(handoff_tail.blocks.into_iter().collect())
    }

    fn abort_handoff(&mut self) -> (u64, u64) {
        self.handoff_tail
            .take()
            .map(|tail| {
                (
                    tail.blocks.len() as u64,
                    tail.payload_bytes.min(u64::MAX as usize) as u64,
                )
            })
            .unwrap_or((0, 0))
    }

    fn restore_handoff_snapshot(&mut self, snapshot: Vec<Vec<u8>>) {
        let mut restored = VecDeque::from(snapshot);
        if let Some(tail) = self.handoff_tail.take() {
            restored.extend(tail.blocks);
        }
        restored.extend(self.blocks.drain(..));
        if self.max_blocks == 0 {
            restored.clear();
        } else {
            while restored.len() > self.max_blocks {
                restored.pop_front();
            }
        }
        self.blocks = restored;
    }

    fn block_count(&self) -> u64 {
        (self.blocks.len() as u64).saturating_add(
            self.handoff_tail
                .as_ref()
                .map(|tail| tail.blocks.len() as u64)
                .unwrap_or(0),
        )
    }

    fn audio_frame_count(&self, block_size: u32) -> u64 {
        self.block_count().saturating_mul(u64::from(block_size))
    }

    fn payload_bytes(&self) -> u64 {
        (self
            .blocks
            .iter()
            .map(|block| block.len() as u64)
            .sum::<u64>())
        .saturating_add(
            self.handoff_tail
                .as_ref()
                .map(|tail| tail.payload_bytes.min(u64::MAX as usize) as u64)
                .unwrap_or(0),
        )
    }

    fn rolling_payload_bytes(&self) -> usize {
        self.blocks
            .iter()
            .fold(0_usize, |total, block| total.saturating_add(block.len()))
    }
}

#[cfg(test)]
fn patch_adopted_prewarm_stop_payload(
    payload: &mut Value,
    stop_payload: Value,
    handoff_mode: &str,
) {
    if let Value::Object(root) = payload {
        if let Some(Value::Object(adopted)) = root.get_mut("adoptedPrewarm") {
            adopted.insert("stop".to_string(), stop_payload);
            adopted.insert(
                "handoffMode".to_string(),
                Value::String(handoff_mode.to_string()),
            );
        }
    }
}

struct PrewarmSession {
    prewarm_id: String,
    source: &'static str,
    request: CaptureRequest,
    control_tx: Sender<PrewarmWorkerCommand>,
    join_handle: Option<JoinHandle<PrewarmStats>>,
    started_at: Instant,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    block_size: u32,
    native_endpoint_id_hash: Value,
    endpoint_selection: Value,
    mix_format_payload: Value,
    resampler_payload: Value,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
}

type FinishedPrewarmHandoff = (Value, Result<Vec<Vec<u8>>, String>);

struct BoundedPrewarmJoin {
    stats: PrewarmStats,
    timed_out: bool,
}

impl PrewarmSession {
    fn begin_handoff(&self) -> Result<AdoptedPrewarm, String> {
        let (blocks, payload_bytes) = self
            .buffer
            .lock()
            .map_err(|_| "prewarmBufferLockPoisoned".to_string())
            .and_then(|mut buffer| {
                let blocks = buffer.begin_handoff().map_err(str::to_string)?;
                let payload_bytes = blocks.iter().map(|block| block.len() as u64).sum::<u64>();
                Ok((blocks, payload_bytes))
            })?;
        Ok(AdoptedPrewarm {
            prewarm_id: self.prewarm_id.clone(),
            source: self.source,
            block_count: blocks.len() as u64,
            audio_frame_count: (blocks.len() as u64).saturating_mul(u64::from(self.block_size)),
            payload_bytes,
            blocks,
            native_endpoint_id_hash: self.native_endpoint_id_hash.clone(),
            endpoint_selection: self.endpoint_selection.clone(),
            mix_format_payload: self.mix_format_payload.clone(),
            resampler_payload: self.resampler_payload.clone(),
            microphone_channel_selection: Arc::clone(&self.microphone_channel_selection),
        })
    }

    fn join_worker(&mut self) -> PrewarmStats {
        let _ = self.control_tx.send(PrewarmWorkerCommand::Stop);
        self.join_handle
            .take()
            .map(|handle| {
                handle.join().unwrap_or_else(|_| PrewarmStats {
                    error: Some("prewarmThreadPanicked".to_string()),
                    ..PrewarmStats::default()
                })
            })
            .unwrap_or_default()
    }

    fn request_stop(&self) {
        let _ = self.control_tx.send(PrewarmWorkerCommand::Stop);
    }

    fn join_worker_bounded(&mut self, timeout: Duration) -> BoundedPrewarmJoin {
        self.request_stop();
        let Some(handle) = self.join_handle.take() else {
            return BoundedPrewarmJoin {
                stats: PrewarmStats::default(),
                timed_out: false,
            };
        };
        let deadline = Instant::now() + timeout;
        while !handle.is_finished() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(2));
        }
        if handle.is_finished() {
            return BoundedPrewarmJoin {
                stats: handle.join().unwrap_or_else(|_| PrewarmStats {
                    error: Some("prewarmThreadPanicked".to_string()),
                    ..PrewarmStats::default()
                }),
                timed_out: false,
            };
        }
        drop(handle);
        BoundedPrewarmJoin {
            stats: PrewarmStats {
                error: Some("prewarmWorkerStopTimeout".to_string()),
                ..PrewarmStats::default()
            },
            timed_out: true,
        }
    }

    fn stop_worker(&mut self, reason: &str) -> Value {
        let stats = self.join_worker();

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

    fn stop_promoted_capture(
        &mut self,
        stream_id: &str,
        reason: &str,
        capture_started_at: Instant,
    ) -> Value {
        let bounded_join = self.join_worker_bounded(PROMOTED_CAPTURE_STOP_JOIN_TIMEOUT);
        let mut prewarm_stats = bounded_join.stats;
        let capture_stats =
            prewarm_stats
                .promoted_capture
                .take()
                .unwrap_or_else(|| CaptureWriterStats {
                    error: prewarm_stats
                        .error
                        .clone()
                        .or_else(|| Some("promotedPrewarmCaptureStatsMissing".to_string())),
                    ..CaptureWriterStats::default()
                });
        let (discarded_blocks, discarded_payload_bytes) = self
            .buffer
            .lock()
            .map(|mut buffer| buffer.abort_handoff())
            .unwrap_or((0, 0));
        let writer_error = capture_stats.error.or(prewarm_stats.error);

        json!({
            "sidecar": SIDECAR_NAME,
            "stopped": !bounded_join.timed_out,
            "streamId": stream_id,
            "reason": reason,
            "source": "wasapi-capture",
            "connected": capture_stats.connected,
            "framesWritten": capture_stats.frames_written,
            "prebufferFramesWritten": capture_stats.prebuffer_frames_written,
            "liveFramesWritten": capture_stats.live_frames_written,
            "bytesWritten": capture_stats.bytes_written,
            "deferredPrewarmStop": capture_stats.deferred_prewarm_stop,
            "writerError": writer_error,
            "workerStopTimedOut": bounded_join.timed_out,
            "discardedHandoffBlocks": discarded_blocks,
            "discardedHandoffPayloadBytes": discarded_payload_bytes,
            "wasapiClientReused": true,
            "handoffMode": "in-place-iaudio-client-promotion",
            "sidecarUptimeMs": capture_started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }

    fn stop_bounded(&mut self, reason: &str, timeout: Duration) -> Value {
        let bounded_join = self.join_worker_bounded(timeout);
        let stats = bounded_join.stats;
        let (discarded_blocks, discarded_payload_bytes) = self
            .buffer
            .lock()
            .map(|mut buffer| buffer.abort_handoff())
            .unwrap_or((0, 0));
        json!({
            "sidecar": SIDECAR_NAME,
            "stopped": !bounded_join.timed_out,
            "prewarmId": self.prewarm_id,
            "reason": reason,
            "source": self.source,
            "totalBlocksObserved": stats.total_blocks_observed,
            "totalAudioFramesObserved": stats.total_audio_frames_observed,
            "bufferedBlocks": stats.buffered_blocks,
            "bufferedAudioFrames": stats.buffered_audio_frames,
            "bufferedPayloadBytes": stats.buffered_payload_bytes,
            "prewarmError": stats.error,
            "workerStopTimedOut": bounded_join.timed_out,
            "discardedHandoffBlocks": discarded_blocks,
            "discardedHandoffPayloadBytes": discarded_payload_bytes,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }

    fn stop(&mut self, reason: &str) -> Value {
        let mut payload = self.stop_worker(reason);
        let (discarded_blocks, discarded_payload_bytes) = self
            .buffer
            .lock()
            .map(|mut buffer| buffer.abort_handoff())
            .unwrap_or((0, 0));
        if let Value::Object(root) = &mut payload {
            root.insert(
                "discardedHandoffBlocks".to_string(),
                Value::from(discarded_blocks),
            );
            root.insert(
                "discardedHandoffPayloadBytes".to_string(),
                Value::from(discarded_payload_bytes),
            );
        }
        payload
    }

    fn stop_and_finish_handoff(&mut self, reason: &str) -> FinishedPrewarmHandoff {
        let mut payload = self.stop_worker(reason);
        let tail_blocks = match self.buffer.lock() {
            Ok(mut buffer) => buffer.finish_handoff().map_err(str::to_string),
            Err(_) => Err("prewarmBufferLockPoisoned".to_string()),
        };
        let (tail_block_count, tail_payload_bytes) = tail_blocks
            .as_ref()
            .map(|blocks| {
                (
                    blocks.len() as u64,
                    blocks.iter().map(|block| block.len() as u64).sum::<u64>(),
                )
            })
            .unwrap_or((0, 0));
        if let Value::Object(root) = &mut payload {
            root.insert(
                "handoffTailBlocks".to_string(),
                Value::from(tail_block_count),
            );
            root.insert(
                "handoffTailPayloadBytes".to_string(),
                Value::from(tail_payload_bytes),
            );
        }
        (payload, tail_blocks)
    }

    fn stop_and_finish_handoff_bounded(
        &mut self,
        reason: &str,
        timeout: Duration,
    ) -> FinishedPrewarmHandoff {
        let bounded_join = self.join_worker_bounded(timeout);
        let stats = bounded_join.stats;
        // Never detach a still-writing worker and then claim its tail is
        // complete. On timeout the capture fails closed; the already-requested
        // Stop and process-level cleanup reclaim the detached worker later.
        let tail_blocks = if bounded_join.timed_out {
            Err("prewarmWorkerStopTimeout".to_string())
        } else {
            match self.buffer.lock() {
                Ok(mut buffer) => buffer.finish_handoff().map_err(str::to_string),
                Err(_) => Err("prewarmBufferLockPoisoned".to_string()),
            }
        };
        let (tail_block_count, tail_payload_bytes) = tail_blocks
            .as_ref()
            .map(|blocks| {
                (
                    blocks.len() as u64,
                    blocks.iter().map(|block| block.len() as u64).sum::<u64>(),
                )
            })
            .unwrap_or((0, 0));
        let payload = json!({
            "sidecar": SIDECAR_NAME,
            "stopped": !bounded_join.timed_out,
            "prewarmId": self.prewarm_id,
            "reason": reason,
            "source": self.source,
            "totalBlocksObserved": stats.total_blocks_observed,
            "totalAudioFramesObserved": stats.total_audio_frames_observed,
            "bufferedBlocks": stats.buffered_blocks,
            "bufferedAudioFrames": stats.buffered_audio_frames,
            "bufferedPayloadBytes": stats.buffered_payload_bytes,
            "prewarmError": stats.error,
            "workerStopTimedOut": bounded_join.timed_out,
            "handoffTailBlocks": tail_block_count,
            "handoffTailPayloadBytes": tail_payload_bytes,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        });
        (payload, tail_blocks)
    }

    fn worker_finished(&self) -> bool {
        self.join_handle
            .as_ref()
            .map(|handle| handle.is_finished())
            .unwrap_or(true)
    }

    fn status(&self) -> Value {
        let (buffered_blocks, buffered_audio_frames, buffered_payload_bytes) = self
            .buffer
            .lock()
            .map(|buffer| {
                (
                    buffer.block_count(),
                    buffer.audio_frame_count(self.block_size),
                    buffer.payload_bytes(),
                )
            })
            .unwrap_or((0, 0, 0));
        let worker_finished = self.worker_finished();
        json!({
            "sidecar": SIDECAR_NAME,
            "active": !worker_finished,
            "prewarmId": self.prewarm_id,
            "reason": if worker_finished { "prewarmWorkerFinished" } else { "active" },
            "source": self.source,
            "workerFinished": worker_finished,
            "bufferedBlocks": buffered_blocks,
            "bufferedAudioFrames": buffered_audio_frames,
            "bufferedPayloadBytes": buffered_payload_bytes,
            "sidecarUptimeMs": self.started_at.elapsed().as_millis().min(u128::from(u64::MAX)) as u64,
        })
    }
}

fn stop_deferred_prewarm_session(
    prewarm_session: &mut Option<PrewarmSession>,
    reason: &str,
) -> Option<Value> {
    prewarm_session.take().map(|mut prewarm_session| {
        prewarm_session.stop_bounded(reason, PROMOTED_CAPTURE_STOP_JOIN_TIMEOUT)
    })
}

fn finish_deferred_prewarm_handoff(
    prewarm_session: &mut Option<PrewarmSession>,
    reason: &str,
) -> Option<FinishedPrewarmHandoff> {
    prewarm_session.take().map(|mut prewarm_session| {
        prewarm_session.stop_and_finish_handoff_bounded(reason, PROMOTED_CAPTURE_STOP_JOIN_TIMEOUT)
    })
}

#[derive(Debug, Default)]
struct AdoptedPrewarm {
    prewarm_id: String,
    source: &'static str,
    block_count: u64,
    audio_frame_count: u64,
    payload_bytes: u64,
    blocks: Vec<Vec<u8>>,
    native_endpoint_id_hash: Value,
    endpoint_selection: Value,
    mix_format_payload: Value,
    resampler_payload: Value,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
}

impl AdoptedPrewarm {
    fn append_tail(&mut self, tail_blocks: Vec<Vec<u8>>, block_size: u32) {
        let tail_block_count = tail_blocks.len() as u64;
        let tail_payload_bytes = tail_blocks
            .iter()
            .map(|block| block.len() as u64)
            .sum::<u64>();
        self.block_count = self.block_count.saturating_add(tail_block_count);
        self.audio_frame_count = self
            .audio_frame_count
            .saturating_add(tail_block_count.saturating_mul(u64::from(block_size)));
        self.payload_bytes = self.payload_bytes.saturating_add(tail_payload_bytes);
        self.blocks.extend(tail_blocks);
    }

    fn to_payload(&self, stop_payload: Value) -> Value {
        json!({
            "prewarmId": self.prewarm_id,
            "source": self.source,
            "blocks": self.block_count,
            "audioFrames": self.audio_frame_count,
            "payloadBytes": self.payload_bytes,
            "adopted": self.block_count > 0,
            "stop": stop_payload,
            "nativeEndpointIdHash": self.native_endpoint_id_hash.clone(),
            "endpointSelection": self.endpoint_selection.clone(),
            "mixFormat": self.mix_format_payload.clone(),
            "resampler": self.resampler_payload.clone(),
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

fn synthetic_signal_enabled() -> bool {
    env_flag_enabled(env::var(SYNTHETIC_SIGNAL_ENV).ok().as_deref())
}

fn validate_synthetic_pcm_fixture_len(byte_len: u64) -> Result<(), String> {
    if byte_len == 0 {
        return Err(format!("{SYNTHETIC_MIC_PCM_ENV} must not be empty"));
    }
    if byte_len > SYNTHETIC_PCM_MAX_BYTES {
        return Err(format!(
            "{SYNTHETIC_MIC_PCM_ENV} exceeds the bounded synthetic PCM size"
        ));
    }
    if !byte_len.is_multiple_of(2) {
        return Err(format!(
            "{SYNTHETIC_MIC_PCM_ENV} must contain aligned signed 16-bit PCM"
        ));
    }
    Ok(())
}

fn load_synthetic_pcm_fixture(request: &CaptureRequest) -> Result<Option<Vec<u8>>, String> {
    if !request.capture_kind.eq_ignore_ascii_case("microphone") {
        return Ok(None);
    }
    let Some(raw_path) = env::var_os(SYNTHETIC_MIC_PCM_ENV) else {
        return Ok(None);
    };
    if raw_path.is_empty() {
        return Ok(None);
    }
    if request.sample_rate != 48_000 || request.channels != 1 {
        return Err(format!(
            "{SYNTHETIC_MIC_PCM_ENV} requires 48000 Hz mono capture"
        ));
    }
    let path = std::path::PathBuf::from(raw_path);
    if !path.is_absolute() {
        return Err(format!("{SYNTHETIC_MIC_PCM_ENV} must be an absolute path"));
    }
    let metadata = std::fs::metadata(&path)
        .map_err(|_| format!("{SYNTHETIC_MIC_PCM_ENV} could not be opened"))?;
    if !metadata.is_file() {
        return Err(format!("{SYNTHETIC_MIC_PCM_ENV} must reference a file"));
    }
    validate_synthetic_pcm_fixture_len(metadata.len())?;
    let payload =
        std::fs::read(&path).map_err(|_| format!("{SYNTHETIC_MIC_PCM_ENV} could not be read"))?;
    validate_synthetic_pcm_fixture_len(payload.len() as u64)?;
    Ok(Some(payload))
}

fn synthetic_frame_payload(
    request: &CaptureRequest,
    sequence: u64,
    pcm_fixture: Option<&[u8]>,
) -> Vec<u8> {
    let sample_count = usize::from(request.channels) * request.block_size as usize;
    let payload_bytes = sample_count.saturating_mul(2);
    if let Some(fixture) = pcm_fixture {
        let mut payload = vec![0_u8; payload_bytes];
        let offset = u64::try_from(payload_bytes)
            .ok()
            .and_then(|frame_bytes| sequence.checked_mul(frame_bytes))
            .and_then(|value| usize::try_from(value).ok());
        if let Some(offset) = offset.filter(|offset| *offset < fixture.len()) {
            let available = fixture.len() - offset;
            let copy_len = available.min(payload.len());
            payload[..copy_len].copy_from_slice(&fixture[offset..offset + copy_len]);
        }
        return payload;
    }
    if !synthetic_signal_enabled() {
        return vec![0_u8; payload_bytes];
    }
    let sample_rate = f64::from(request.sample_rate.max(1));
    let channels = usize::from(request.channels.max(1));
    let mut payload = Vec::with_capacity(sample_count * 2);
    for frame_index in 0..request.block_size as usize {
        let absolute_index = sequence
            .saturating_mul(u64::from(request.block_size))
            .saturating_add(frame_index as u64);
        let time = absolute_index as f64 / sample_rate;
        let render = (2.0 * std::f64::consts::PI * 660.0 * time).sin() * 6_000.0;
        let sample = if request.capture_kind == "loopback" {
            render
        } else {
            let near_end = (2.0 * std::f64::consts::PI * 220.0 * time).sin() * 4_000.0;
            let delayed_time = (time - 0.08).max(0.0);
            let echo = (2.0 * std::f64::consts::PI * 660.0 * delayed_time).sin() * 2_400.0;
            near_end + echo
        };
        let pcm = sample
            .round()
            .clamp(f64::from(i16::MIN), f64::from(i16::MAX)) as i16;
        for _ in 0..channels {
            payload.extend_from_slice(&pcm.to_le_bytes());
        }
    }
    payload
}

fn wasapi_capture_enabled() -> bool {
    if env_flag_enabled(env::var(DISABLE_WASAPI_CAPTURE_ENV).ok().as_deref()) {
        return false;
    }
    if synthetic_capture_enabled() {
        return env_flag_enabled(env::var(WASAPI_CAPTURE_ENV).ok().as_deref());
    }
    true
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
    mut adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
) -> Result<(CaptureSession, Value), String> {
    let synthetic_pcm_fixture = load_synthetic_pcm_fixture(&request)?;
    let synthetic_speech_fixture = synthetic_pcm_fixture.is_some();
    let synthetic_speech_fixture_bytes = synthetic_pcm_fixture
        .as_ref()
        .map(|payload| payload.len() as u64)
        .unwrap_or_default();
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = create_frame_pipe(&pipe_path)?;
    let (stop_tx, stop_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let adopted_blocks = adopted_prewarm
        .as_mut()
        .map(|(adopted, _)| std::mem::take(&mut adopted.blocks))
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
                synthetic_pcm_fixture,
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
        worker: CaptureSessionWorker::Dedicated {
            stop_tx,
            join_handle: Some(join_handle),
        },
        started_at: Instant::now(),
    };
    let payload = json!({
        "sidecar": SIDECAR_NAME,
        "captureAvailable": true,
        "syntheticFramePipe": true,
        "syntheticSpeechFixture": synthetic_speech_fixture,
        "syntheticSpeechFixtureBytes": synthetic_speech_fixture_bytes,
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

fn start_wasapi_replacement_with_adopted_prewarm(
    request: CaptureRequest,
    prewarm_session: Box<PrewarmSession>,
    fallback_reason: &str,
) -> Result<(CaptureSession, Value), String> {
    let adopted = match prewarm_session.begin_handoff() {
        Ok(adopted) => adopted,
        Err(error) => {
            prewarm_session.request_stop();
            return Err(format!(
                "{fallback_reason}: replacement prewarm handoff failed: {error}"
            ));
        }
    };
    let pending_stop_payload = json!({
        "sidecar": SIDECAR_NAME,
        "stopped": false,
        "prewarmId": adopted.prewarm_id,
        "reason": "pendingCaptureReady",
        "source": adopted.source,
    });
    start_wasapi_capture_impl(
        request,
        Some((adopted, pending_stop_payload)),
        Some(*prewarm_session),
    )
    .map(|(session, mut payload)| {
        patch_wasapi_promotion_fallback(&mut payload, fallback_reason);
        (session, payload)
    })
}

fn start_wasapi_cold_replacement_with_overlap(
    request: CaptureRequest,
    mut prewarm_session: Box<PrewarmSession>,
    fallback_reason: &str,
) -> Result<(CaptureSession, Value), String> {
    // Keep the existing prewarm IAudioClient running until the replacement is
    // ready.  The old audio is intentionally not replayed when endpoint or
    // format compatibility is uncertain, but Windows keeps a continuous
    // microphone-privacy indication across the replacement.
    match start_wasapi_capture_impl(request, None, None) {
        Ok((session, mut payload)) => {
            let stop_payload = prewarm_session.stop_bounded(
                "inPlacePromotionColdFallback",
                PROMOTED_CAPTURE_STOP_JOIN_TIMEOUT,
            );
            patch_wasapi_promotion_fallback(&mut payload, fallback_reason);
            if let Value::Object(root) = &mut payload {
                root.insert("prewarmFallbackStop".to_string(), stop_payload);
            }
            Ok((session, payload))
        }
        Err(error) => {
            prewarm_session.request_stop();
            Err(error)
        }
    }
}

fn patch_wasapi_promotion_fallback(payload: &mut Value, reason: &str) {
    if let Value::Object(root) = payload {
        root.insert("wasapiClientReused".to_string(), Value::Bool(false));
        root.insert(
            "handoffMode".to_string(),
            Value::String("replacement-wasapi-client".to_string()),
        );
        root.insert(
            "prewarmPromotionFallbackReason".to_string(),
            Value::String(reason.chars().take(96).collect()),
        );
    }
}

fn wasapi_promoted_capture_payload(
    stream_id: &str,
    pipe_path: &str,
    request: &CaptureRequest,
    prewarm_session: &PrewarmSession,
    accepted: PrewarmPromotionAccepted,
) -> Value {
    let pending_transition = json!({
        "stopped": false,
        "reason": "promotedInPlace",
        "source": prewarm_session.source,
        "physicalClientReused": true,
    });
    let adopted_prewarm = json!({
        "prewarmId": prewarm_session.prewarm_id.clone(),
        "source": prewarm_session.source,
        "blocks": accepted.blocks,
        "audioFrames": accepted.audio_frames,
        "payloadBytes": accepted.payload_bytes,
        "adopted": true,
        "stop": pending_transition,
        "nativeEndpointIdHash": prewarm_session.native_endpoint_id_hash.clone(),
        "endpointSelection": prewarm_session.endpoint_selection.clone(),
        "mixFormat": prewarm_session.mix_format_payload.clone(),
        "resampler": prewarm_session.resampler_payload.clone(),
        "handoffMode": IN_PLACE_PREWARM_HANDOFF_MODE,
    });
    json!({
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
        "nativeEndpointIdHash": prewarm_session.native_endpoint_id_hash.clone(),
        "adoptedPrewarm": adopted_prewarm,
        "endpointSelection": prewarm_session.endpoint_selection.clone(),
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "mixFormat": prewarm_session.mix_format_payload.clone(),
        "resampler": prewarm_session.resampler_payload.clone(),
        "wasapiReadyDeferred": false,
        "firstFramesFromAdoptedPrewarm": true,
        "wasapiClientReused": true,
        "handoffMode": IN_PLACE_PREWARM_HANDOFF_MODE,
    })
}

#[cfg(windows)]
fn start_wasapi_promoted_capture(
    request: CaptureRequest,
    prewarm_session: Box<PrewarmSession>,
) -> Result<(CaptureSession, Value), (PrewarmPromotionFailure, Box<PrewarmSession>)> {
    if let Err(reason) = validate_prewarm_promotion_format(&prewarm_session.request, &request) {
        return Err((
            PrewarmPromotionFailure::new(reason, PrewarmPromotionFallback::ColdWithOverlap),
            prewarm_session,
        ));
    }

    let current_endpoint_id_hash = match resolve_promotion_endpoint_hash(request.clone()) {
        Ok(hash) => hash,
        Err(failure) => return Err((failure, prewarm_session)),
    };
    if let Err(reason) = validate_in_place_endpoint_hashes(
        &prewarm_session.native_endpoint_id_hash,
        &current_endpoint_id_hash,
    ) {
        return Err((
            PrewarmPromotionFailure::new(reason, PrewarmPromotionFallback::ColdWithOverlap),
            prewarm_session,
        ));
    }

    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe = match create_frame_pipe(&pipe_path) {
        Ok(handle) => PromotionPipeHandle::new(handle as isize),
        Err(_error) => {
            return Err((
                PrewarmPromotionFailure::new(
                    "prewarmPromotionPipeCreateFailed",
                    PrewarmPromotionFallback::AdoptWithOverlap,
                ),
                prewarm_session,
            ))
        }
    };
    let (accepted_tx, accepted_rx) = mpsc::channel();
    let decision = Arc::new(PrewarmPromotionDecision::default());
    let command = PrewarmWorkerCommand::Promote(Box::new(PrewarmPromotionRequest {
        pipe,
        request: request.clone(),
        current_endpoint_id_hash,
        decision: Arc::clone(&decision),
        accepted_tx,
    }));
    if prewarm_session.control_tx.send(command).is_err() {
        return Err((
            PrewarmPromotionFailure::new(
                "prewarmPromotionWorkerUnavailable",
                PrewarmPromotionFallback::AdoptWithOverlap,
            ),
            prewarm_session,
        ));
    }

    let accepted = match accepted_rx.recv_timeout(PREWARM_PROMOTION_ACCEPT_TIMEOUT) {
        Ok(Ok(accepted)) => accepted,
        Ok(Err(failure)) => return Err((failure, prewarm_session)),
        Err(mpsc::RecvTimeoutError::Timeout) if decision.cancel_if_pending() => {
            return Err((
                PrewarmPromotionFailure::new(
                    "prewarmPromotionAcceptanceTimeout",
                    PrewarmPromotionFallback::AdoptWithOverlap,
                ),
                prewarm_session,
            ));
        }
        Err(mpsc::RecvTimeoutError::Disconnected) if decision.cancel_if_pending() => {
            return Err((
                PrewarmPromotionFailure::new(
                    "prewarmPromotionAcceptanceChannelClosed",
                    PrewarmPromotionFallback::AdoptWithOverlap,
                ),
                prewarm_session,
            ));
        }
        Err(_) => {
            // ACCEPTED won the atomic race. The worker may already have moved
            // the rolling prefix into the promoted state, so a replacement is
            // no longer safe. Give the completion message one short bounded
            // grace period, then abort instead of silently losing that prefix.
            match accepted_rx.recv_timeout(PREWARM_PROMOTION_ACCEPTED_COMPLETION_TIMEOUT) {
                Ok(Ok(accepted)) => accepted,
                Ok(Err(failure)) => return Err((failure, prewarm_session)),
                Err(_) => {
                    prewarm_session.request_stop();
                    return Err((
                        PrewarmPromotionFailure::new(
                            "prewarmPromotionAcceptedCompletionTimeout",
                            PrewarmPromotionFallback::Abort,
                        ),
                        prewarm_session,
                    ));
                }
            }
        }
    };
    let payload = wasapi_promoted_capture_payload(
        &stream_id,
        &pipe_path,
        &request,
        &prewarm_session,
        accepted,
    );
    let session = CaptureSession {
        stream_id,
        source: "wasapi-capture",
        worker: CaptureSessionWorker::PromotedPrewarm(prewarm_session),
        started_at: Instant::now(),
    };
    Ok((session, payload))
}

#[cfg(windows)]
fn resolve_promotion_endpoint_hash(
    request: CaptureRequest,
) -> Result<Value, PrewarmPromotionFailure> {
    let Some(resolver_lease) = PromotionEndpointResolverLease::try_acquire() else {
        return Err(PrewarmPromotionFailure::new(
            "prewarmPromotionEndpointResolverBusy",
            PrewarmPromotionFallback::AdoptWithOverlap,
        ));
    };
    let (result_tx, result_rx) = mpsc::channel();
    thread::Builder::new()
        .name("scriber-audio-promotion-endpoint".to_string())
        .spawn(move || {
            // Keep the single-flight lease in the worker, including after the
            // caller's 300 ms deadline. A stuck COM call therefore permits no
            // second detached resolver thread; later captures take the bounded
            // overlap fallback until this worker exits or the process restarts.
            let _resolver_lease = resolver_lease;
            let result = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
                .ok()
                .map_err(|error| format!("promotion endpoint COM initialization failed: {error}"))
                .and_then(|_| {
                    let result = (|| -> Result<Value, String> {
                        let enumerator: IMMDeviceEnumerator =
                            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                                .map_err(|error| {
                                    format!(
                                        "promotion endpoint enumerator creation failed: {error}"
                                    )
                                })?;
                        select_wasapi_capture_device(&enumerator, &request)
                            .map(|selected| selected.endpoint_id_hash)
                    })();
                    unsafe {
                        CoUninitialize();
                    }
                    result
                });
            let _ = result_tx.send(result);
        })
        .map_err(|_| {
            PrewarmPromotionFailure::new(
                "prewarmPromotionEndpointResolverUnavailable",
                PrewarmPromotionFallback::AdoptWithOverlap,
            )
        })?;

    match result_rx.recv_timeout(PREWARM_PROMOTION_ENDPOINT_RESOLUTION_TIMEOUT) {
        Ok(Ok(endpoint_id_hash)) => Ok(endpoint_id_hash),
        Ok(Err(_)) => Err(PrewarmPromotionFailure::new(
            "prewarmPromotionEndpointResolutionFailed",
            PrewarmPromotionFallback::AdoptWithOverlap,
        )),
        Err(mpsc::RecvTimeoutError::Timeout) => Err(PrewarmPromotionFailure::new(
            "prewarmPromotionEndpointResolutionTimeout",
            PrewarmPromotionFallback::AdoptWithOverlap,
        )),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(PrewarmPromotionFailure::new(
            "prewarmPromotionEndpointResolverDisconnected",
            PrewarmPromotionFallback::AdoptWithOverlap,
        )),
    }
}

#[cfg(not(windows))]
fn start_wasapi_promoted_capture(
    _request: CaptureRequest,
    prewarm_session: Box<PrewarmSession>,
) -> Result<(CaptureSession, Value), (PrewarmPromotionFailure, Box<PrewarmSession>)> {
    Err((
        PrewarmPromotionFailure::new(
            "prewarmPromotionUnsupportedPlatform",
            PrewarmPromotionFallback::Abort,
        ),
        prewarm_session,
    ))
}

#[cfg(windows)]
fn start_wasapi_capture_impl(
    request: CaptureRequest,
    mut adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
    mut deferred_prewarm_session: Option<PrewarmSession>,
) -> Result<(CaptureSession, Value), String> {
    let stream_id = Uuid::new_v4().simple().to_string();
    let pipe_path = format!(r"\\.\pipe\scriber-audio-{stream_id}");
    let pipe_handle = match create_frame_pipe(&pipe_path) {
        Ok(pipe_handle) => pipe_handle,
        Err(err) => {
            let _ =
                stop_deferred_prewarm_session(&mut deferred_prewarm_session, "captureStartFailed");
            return Err(err);
        }
    };
    let (stop_tx, stop_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let writer_request = request.clone();
    let writer_pipe_path = pipe_path.clone();
    let pipe_handle_value = pipe_handle as isize;
    let microphone_channel_selection = adopted_prewarm
        .as_ref()
        .map(|(adopted, _)| Arc::clone(&adopted.microphone_channel_selection))
        .unwrap_or_else(|| Arc::new(Mutex::new(MicrophoneChannelSelectionState::default())));
    let adopted_native_endpoint_id_hash = adopted_prewarm
        .as_ref()
        .map(|(adopted, _)| adopted.native_endpoint_id_hash.clone())
        .unwrap_or(Value::Null);
    let adopted_blocks = adopted_prewarm
        .as_mut()
        .map(|(adopted, _)| std::mem::take(&mut adopted.blocks))
        .unwrap_or_default();
    let defer_wasapi_ready = deferred_prewarm_session.is_some();
    let writer_deferred_prewarm_session = if defer_wasapi_ready {
        deferred_prewarm_session.take()
    } else {
        None
    };
    // Keep a recoverable owner outside the closure until thread creation has
    // succeeded. If Windows refuses to spawn the writer, dropping the closure
    // must not detach the still-running prewarm worker.
    let deferred_prewarm_holder = Arc::new(Mutex::new(writer_deferred_prewarm_session));
    let writer_deferred_prewarm_holder = Arc::clone(&deferred_prewarm_holder);
    let join_handle = thread::Builder::new()
        .name("scriber-audio-wasapi-frame-pipe".to_string())
        .spawn(move || {
            let writer_deferred_prewarm_session = writer_deferred_prewarm_holder
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .take();
            run_wasapi_capture_writer(
                pipe_handle_value as HANDLE,
                writer_pipe_path,
                writer_request,
                stop_rx,
                ready_tx,
                adopted_blocks,
                adopted_native_endpoint_id_hash,
                defer_wasapi_ready,
                microphone_channel_selection,
                writer_deferred_prewarm_session,
            )
        })
        .map_err(|err| {
            unsafe {
                CloseHandle(pipe_handle);
            }
            let mut holder = deferred_prewarm_holder
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let _ = stop_deferred_prewarm_session(&mut holder, "captureStartFailed");
            format!("WASAPI frame-pipe writer thread spawn failed: {err}")
        })?;

    if defer_wasapi_ready {
        let session = CaptureSession {
            stream_id: stream_id.clone(),
            source: "wasapi-capture",
            worker: CaptureSessionWorker::Dedicated {
                stop_tx,
                join_handle: Some(join_handle),
            },
            started_at: Instant::now(),
        };
        let payload = wasapi_capture_payload(
            &stream_id,
            &pipe_path,
            &request,
            adopted_prewarm.as_ref(),
            None,
            true,
        );
        return Ok((session, payload));
    }

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
        worker: CaptureSessionWorker::Dedicated {
            stop_tx,
            join_handle: Some(join_handle),
        },
        started_at: Instant::now(),
    };
    let payload = wasapi_capture_payload(
        &stream_id,
        &pipe_path,
        &request,
        adopted_prewarm.as_ref(),
        Some(ready),
        false,
    );
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_wasapi_capture_impl(
    _request: CaptureRequest,
    _adopted_prewarm: Option<(AdoptedPrewarm, Value)>,
    _deferred_prewarm_session: Option<PrewarmSession>,
) -> Result<(CaptureSession, Value), String> {
    Err("WASAPI capture is only implemented on Windows".to_string())
}

#[cfg(windows)]
fn wasapi_capture_payload(
    stream_id: &str,
    pipe_path: &str,
    request: &CaptureRequest,
    adopted_prewarm: Option<&(AdoptedPrewarm, Value)>,
    ready: Option<WasapiReady>,
    wasapi_ready_deferred: bool,
) -> Value {
    let (native_endpoint_id_hash, endpoint_selection, mix_format, resampler) =
        if let Some(ready) = ready {
            let resampler = json!({
                "sourceSampleRate": ready.mix_format.sample_rate,
                "targetSampleRate": request.sample_rate,
                "sourceChannels": ready.mix_format.channels,
                "targetChannels": request.channels,
                "method": "nearest",
            });
            (
                ready.endpoint_id_hash,
                ready.endpoint_selection,
                ready.mix_format.to_payload(),
                resampler,
            )
        } else {
            let adopted = adopted_prewarm.map(|(adopted, _)| adopted);
            let requested_endpoint = if request.native_endpoint_id_hash.trim().is_empty() {
                Value::Null
            } else {
                Value::String(request.native_endpoint_id_hash.clone())
            };
            let native_endpoint = adopted
                .map(|adopted| adopted.native_endpoint_id_hash.clone())
                .filter(|value| !value.is_null())
                .unwrap_or_else(|| requested_endpoint.clone());
            let endpoint_selection = adopted
                .map(|adopted| adopted.endpoint_selection.clone())
                .filter(|value| !value.is_null())
                .unwrap_or_else(|| {
                    endpoint_selection_payload(
                        request,
                        native_endpoint.clone(),
                        "deferredUntilCaptureReady",
                        is_default_device_preference(&request.device_preference),
                        Value::String("wasapiReadyDeferredUntilLiveCapture".to_string()),
                    )
                });
            let mix_format = adopted
                .map(|adopted| adopted.mix_format_payload.clone())
                .filter(|value| !value.is_null())
                .unwrap_or(Value::Null);
            let resampler = adopted
                .map(|adopted| adopted.resampler_payload.clone())
                .filter(|value| !value.is_null())
                .unwrap_or(Value::Null);
            (native_endpoint, endpoint_selection, mix_format, resampler)
        };

    json!({
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
        "nativeEndpointIdHash": native_endpoint_id_hash,
        "adoptedPrewarm": adopted_prewarm_payload(adopted_prewarm),
        "endpointSelection": endpoint_selection,
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "mixFormat": mix_format,
        "resampler": resampler,
        "wasapiReadyDeferred": wasapi_ready_deferred,
        "firstFramesFromAdoptedPrewarm": wasapi_ready_deferred,
        "wasapiClientReused": false,
        "handoffMode": if adopted_prewarm.is_some() {
            "overlap-capture-start-before-prewarm-stop"
        } else {
            "replacement-wasapi-client"
        },
    })
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
const MICROPHONE_CHANNEL_SWITCH_ENERGY_RATIO: f64 = 1.35;

#[derive(Debug, Default)]
struct MicrophoneChannelSelectionState {
    selected_channel: Option<usize>,
}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WasapiChannelMixPolicy {
    StrongestMicrophoneChannel,
    AverageAllChannels,
}

#[cfg(windows)]
struct WasapiPcmConverter {
    source: WasapiMixFormat,
    target_sample_rate: u32,
    target_channels: u16,
    block_size: u32,
    channel_mix_policy: WasapiChannelMixPolicy,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
    channel_energy: Vec<f64>,
    mono_buffer: Vec<f32>,
    next_source_index: f64,
    pending_samples: Vec<i16>,
}

#[cfg(windows)]
impl WasapiPcmConverter {
    #[cfg(test)]
    fn new(source: WasapiMixFormat, request: &CaptureRequest) -> Self {
        Self::with_channel_selection(
            source,
            request,
            Arc::new(Mutex::new(MicrophoneChannelSelectionState::default())),
        )
    }

    fn with_channel_selection(
        source: WasapiMixFormat,
        request: &CaptureRequest,
        microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
    ) -> Self {
        let channel_mix_policy = if request.capture_kind.eq_ignore_ascii_case("loopback") {
            WasapiChannelMixPolicy::AverageAllChannels
        } else {
            WasapiChannelMixPolicy::StrongestMicrophoneChannel
        };
        let source_channels = usize::from(source.channels);
        Self {
            source,
            target_sample_rate: request.sample_rate,
            target_channels: request.channels,
            block_size: request.block_size,
            channel_mix_policy,
            microphone_channel_selection,
            channel_energy: vec![0.0; source_channels],
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
                .extend(std::iter::repeat_n(0.0, frame_count as usize));
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

        match self.channel_mix_policy {
            WasapiChannelMixPolicy::AverageAllChannels => {
                for frame in 0..frame_count {
                    let mut sum = 0.0_f32;
                    for channel in 0..channels {
                        sum += decode_wasapi_sample(
                            bytes,
                            (frame * channels + channel) * sample_bytes,
                            self.source.sample_format,
                        );
                    }
                    self.mono_buffer.push(sum / channels as f32);
                }
            }
            WasapiChannelMixPolicy::StrongestMicrophoneChannel => {
                self.channel_energy.fill(0.0);
                for frame in 0..frame_count {
                    for channel in 0..channels {
                        let sample = decode_wasapi_sample(
                            bytes,
                            (frame * channels + channel) * sample_bytes,
                            self.source.sample_format,
                        );
                        self.channel_energy[channel] += f64::from(sample).powi(2);
                    }
                }

                let best_channel = self
                    .channel_energy
                    .iter()
                    .enumerate()
                    .max_by(|left, right| left.1.total_cmp(right.1))
                    .map(|(channel, _)| channel)
                    .unwrap_or(0);
                let best_energy = self.channel_energy[best_channel];
                let chosen_channel = {
                    let mut selection = self
                        .microphone_channel_selection
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    let previous_channel = selection
                        .selected_channel
                        .filter(|channel| *channel < channels);
                    let chosen = match previous_channel {
                        Some(previous) if best_energy <= f64::EPSILON => previous,
                        Some(previous)
                            if self.channel_energy[previous] > 0.0
                                && best_energy
                                    < self.channel_energy[previous]
                                        * MICROPHONE_CHANNEL_SWITCH_ENERGY_RATIO =>
                        {
                            previous
                        }
                        _ => best_channel,
                    };
                    selection.selected_channel = Some(chosen);
                    chosen
                };

                for frame in 0..frame_count {
                    self.mono_buffer.push(decode_wasapi_sample(
                        bytes,
                        (frame * channels + chosen_channel) * sample_bytes,
                        self.source.sample_format,
                    ));
                }
            }
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

#[cfg(windows)]
fn decode_wasapi_sample(bytes: &[u8], offset: usize, sample_format: WasapiSampleFormat) -> f32 {
    match sample_format {
        WasapiSampleFormat::Float32 => {
            f32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()).clamp(-1.0, 1.0)
        }
        WasapiSampleFormat::Pcm16 => {
            let raw = i16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap());
            f32::from(raw) / f32::from(i16::MAX)
        }
        WasapiSampleFormat::Pcm32 => {
            let raw = i32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
            raw as f32 / i32::MAX as f32
        }
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
    let (ready_tx, ready_rx) = mpsc::channel();
    let worker_request = request.clone();
    let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(
        requested_prebuffer_frame_count(&request),
    )));
    let worker_buffer = Arc::clone(&buffer);
    let join_handle = thread::Builder::new()
        .name("scriber-audio-synthetic-prewarm".to_string())
        .spawn(move || {
            run_synthetic_prewarm_worker(worker_request, stop_rx, worker_buffer, ready_tx)
        })
        .map_err(|err| format!("synthetic prewarm worker thread spawn failed: {err}"))?;

    // Synthetic capture is the deterministic transport harness used by installed and unit
    // tests. Do not advertise a usable prewarm session before the worker has actually observed
    // its first audio block; otherwise an immediate capture adoption can legitimately snapshot
    // an empty rolling buffer under scheduler pressure.
    match ready_rx.recv_timeout(Duration::from_secs(1)) {
        Ok(Ok(())) => {}
        Ok(Err(err)) => {
            let _ = stop_tx.send(PrewarmWorkerCommand::Stop);
            let _ = join_handle.join();
            return Err(err);
        }
        Err(err) => {
            let _ = stop_tx.send(PrewarmWorkerCommand::Stop);
            let _ = join_handle.join();
            return Err(format!(
                "synthetic prewarm did not observe its first audio block: {err}"
            ));
        }
    }

    let endpoint_selection = endpoint_selection_payload(
        &request,
        Value::Null,
        "synthetic",
        false,
        Value::String("syntheticPrewarmHasNoNativeEndpoint".to_string()),
    );
    let session = PrewarmSession {
        prewarm_id: prewarm_id.clone(),
        source: "synthetic-prewarm",
        request: request.clone(),
        control_tx: stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
        buffer,
        block_size: request.block_size,
        native_endpoint_id_hash: Value::Null,
        endpoint_selection: endpoint_selection.clone(),
        mix_format_payload: Value::Null,
        resampler_payload: Value::Null,
        microphone_channel_selection: Arc::new(Mutex::new(
            MicrophoneChannelSelectionState::default(),
        )),
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
        "endpointSelection": endpoint_selection,
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
    let microphone_channel_selection =
        Arc::new(Mutex::new(MicrophoneChannelSelectionState::default()));
    let worker_channel_selection = Arc::clone(&microphone_channel_selection);
    let join_handle = thread::Builder::new()
        .name("scriber-audio-wasapi-prewarm".to_string())
        .spawn(move || {
            run_wasapi_prewarm_worker(
                worker_request,
                stop_rx,
                ready_tx,
                worker_buffer,
                worker_channel_selection,
            )
        })
        .map_err(|err| format!("WASAPI prewarm worker thread spawn failed: {err}"))?;

    let ready = match ready_rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Ok(ready)) => ready,
        Ok(Err(err)) => {
            let _ = stop_tx.send(PrewarmWorkerCommand::Stop);
            let _ = join_handle.join();
            return Err(err);
        }
        Err(err) => {
            let _ = stop_tx.send(PrewarmWorkerCommand::Stop);
            let _ = join_handle.join();
            return Err(format!("WASAPI prewarm did not become ready: {err}"));
        }
    };

    let ready_endpoint_id_hash = ready.endpoint_id_hash.clone();
    let ready_endpoint_selection = ready.endpoint_selection.clone();
    let ready_mix_format = ready.mix_format.to_payload();
    let ready_resampler = json!({
        "sourceSampleRate": ready.mix_format.sample_rate,
        "targetSampleRate": request.sample_rate,
        "sourceChannels": ready.mix_format.channels,
        "targetChannels": request.channels,
        "method": "nearest",
    });
    let session = PrewarmSession {
        prewarm_id: prewarm_id.clone(),
        source: "wasapi-prewarm",
        request: request.clone(),
        control_tx: stop_tx,
        join_handle: Some(join_handle),
        started_at: Instant::now(),
        buffer,
        block_size: request.block_size,
        native_endpoint_id_hash: ready_endpoint_id_hash.clone(),
        endpoint_selection: ready_endpoint_selection.clone(),
        mix_format_payload: ready_mix_format.clone(),
        resampler_payload: ready_resampler.clone(),
        microphone_channel_selection,
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
        "nativeEndpointIdHash": ready_endpoint_id_hash,
        "endpointSelection": ready_endpoint_selection,
        "requestedFormat": request.to_payload(),
        "audioFrameProtocol": audio_frame_protocol_payload(),
        "prebufferFrameTarget": requested_prebuffer_frame_count(&request),
        "mixFormat": ready_mix_format,
        "resampler": ready_resampler,
    });
    Ok((session, payload))
}

#[cfg(not(windows))]
fn start_wasapi_prewarm_impl(_request: CaptureRequest) -> Result<(PrewarmSession, Value), String> {
    Err("WASAPI prewarm is only implemented on Windows".to_string())
}

fn run_synthetic_prewarm_worker(
    request: CaptureRequest,
    control_rx: mpsc::Receiver<PrewarmWorkerCommand>,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    ready_tx: Sender<Result<(), String>>,
) -> PrewarmStats {
    let mut stats = PrewarmStats::default();
    let mut ready_tx = Some(ready_tx);
    let frame_interval = Duration::from_secs_f64(
        (request.block_size as f64 / f64::from(request.sample_rate)).max(0.001),
    );
    let max_buffered_blocks = u64::from(requested_prebuffer_frame_count(&request));
    let payload_len = usize::from(request.channels) * request.block_size as usize * 2;
    loop {
        match control_rx.recv_timeout(frame_interval) {
            Ok(PrewarmWorkerCommand::Stop) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Ok(PrewarmWorkerCommand::Promote(promotion)) => {
                let _ = promotion.accepted_tx.send(Err(PrewarmPromotionFailure::new(
                    "prewarmPromotionRequiresWasapiWorker",
                    PrewarmPromotionFallback::AdoptWithOverlap,
                )));
                continue;
            }
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
                    if let Err(error) = buffer.push(payload) {
                        let error = error.to_string();
                        if let Some(ready_tx) = ready_tx.take() {
                            let _ = ready_tx.send(Err(error.clone()));
                        }
                        stats.error = Some(error);
                        break;
                    }
                    stats.buffered_blocks = buffer.block_count();
                    stats.buffered_audio_frames = buffer.audio_frame_count(request.block_size);
                    stats.buffered_payload_bytes = buffer.payload_bytes();
                }
                Err(_) => {
                    let error = "prewarmBufferLockPoisoned".to_string();
                    if let Some(ready_tx) = ready_tx.take() {
                        let _ = ready_tx.send(Err(error.clone()));
                    }
                    stats.error = Some(error);
                    break;
                }
            }
        }
        if let Some(ready_tx) = ready_tx.take() {
            let _ = ready_tx.send(Ok(()));
        }
    }
    stats
}

#[cfg(windows)]
fn create_frame_pipe(pipe_path: &str) -> Result<HANDLE, String> {
    create_frame_pipe_with_wait_mode(pipe_path, PIPE_NOWAIT)
}

#[cfg(windows)]
fn create_meeting_output_pipe(pipe_path: &str) -> Result<HANDLE, String> {
    // ConnectNamedPipe cannot be bounded on a synchronous PIPE_WAIT instance. Create every
    // server end nonblocking, poll it with a deadline, then switch the connected instance back
    // to PIPE_WAIT before writing. Python still consumes an ordinary blocking byte stream.
    create_frame_pipe_with_wait_mode(pipe_path, PIPE_NOWAIT)
}

#[cfg(windows)]
fn create_frame_pipe_with_wait_mode(pipe_path: &str, wait_mode: u32) -> Result<HANDLE, String> {
    let wide = wide_null(pipe_path);
    let handle = unsafe {
        CreateNamedPipeW(
            wide.as_ptr(),
            PIPE_ACCESS_OUTBOUND,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | wait_mode,
            1,
            64 * 1024,
            0,
            0,
            null(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!(
            "CreateNamedPipeW failed for audio frame pipe: {}",
            unsafe { GetLastError() }
        ));
    }
    Ok(handle)
}

#[cfg(windows)]
fn open_frame_pipe_reader(pipe_path: &str) -> Result<HANDLE, String> {
    let wide = wide_null(pipe_path);
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            GENERIC_READ,
            0,
            null(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!(
            "CreateFileW failed for upstream meeting frame pipe: {}",
            unsafe { GetLastError() }
        ));
    }
    Ok(handle)
}

#[cfg(windows)]
fn read_exact_from_pipe(
    handle: HANDLE,
    size: usize,
    stop_rx: &mpsc::Receiver<()>,
) -> Result<Vec<u8>, String> {
    let mut bytes = vec![0u8; size];
    let mut offset = 0usize;
    while offset < size {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => {
                return Err("meetingRelayStopped".to_string())
            }
            Err(TryRecvError::Empty) => {}
        }
        let mut read = 0u32;
        let ok = unsafe {
            ReadFile(
                handle,
                bytes[offset..].as_mut_ptr(),
                (size - offset).min(u32::MAX as usize) as u32,
                &mut read,
                null_mut(),
            )
        };
        if ok == 0 {
            let error = unsafe { GetLastError() };
            if error == ERROR_NO_DATA {
                thread::sleep(Duration::from_millis(2));
                continue;
            }
            return Err(format!("meeting upstream ReadFile failed: {error}"));
        }
        if read == 0 {
            thread::sleep(Duration::from_millis(2));
            continue;
        }
        offset += read as usize;
    }
    Ok(bytes)
}

#[cfg(windows)]
fn read_meeting_frame(
    handle: HANDLE,
    stop_rx: &mpsc::Receiver<()>,
) -> Result<(AudioFrameHeader, Vec<u8>), String> {
    let header_bytes = read_exact_from_pipe(handle, AUDIO_FRAME_HEADER_LEN, stop_rx)?;
    let header = AudioFrameHeader::decode(&header_bytes)
        .map_err(|error| format!("meeting upstream frame header invalid: {error}"))?;
    let payload = read_exact_from_pipe(handle, header.payload_len as usize, stop_rx)?;
    Ok((header, payload))
}

#[cfg(windows)]
fn pcm_i16_into(payload: &[u8], samples: &mut Vec<i16>) -> Result<(), String> {
    if !payload.len().is_multiple_of(2) {
        return Err("meeting PCM payload has an odd byte length".to_string());
    }
    samples.clear();
    samples.extend(
        payload
            .chunks_exact(2)
            .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]])),
    );
    Ok(())
}

fn meeting_frame_energy(samples: &[i16]) -> f64 {
    samples
        .iter()
        .map(|sample| f64::from(*sample).powi(2))
        .sum::<f64>()
}

fn meeting_render_active_energy_threshold() -> f64 {
    64.0_f64.powi(2) * MEETING_AEC_FRAME_SAMPLES as f64
}

#[cfg(windows)]
fn downsample_meeting_48k_to_16k_into(
    samples: &[i16],
    output: &mut Vec<i16>,
) -> Result<(), String> {
    if samples.len() != MEETING_AEC_FRAME_SAMPLES || !samples.len().is_multiple_of(3) {
        return Err("meeting downsampler requires one 48 kHz 10ms frame".to_string());
    }
    output.clear();
    output.extend(samples.chunks_exact(3).map(|group| {
        let sum = i32::from(group[0]) + i32::from(group[1]) + i32::from(group[2]);
        (sum / 3).clamp(i32::from(i16::MIN), i32::from(i16::MAX)) as i16
    }));
    Ok(())
}

#[cfg(windows)]
fn pcm_bytes_into(samples: &[i16], payload: &mut Vec<u8>) {
    payload.clear();
    payload.extend(samples.iter().flat_map(|sample| sample.to_le_bytes()));
}

#[cfg(windows)]
fn write_meeting_frame(
    handle: HANDLE,
    header: AudioFrameHeader,
    payload: &[u8],
) -> Result<u64, String> {
    let encoded = header.encode().map_err(|error| error.to_string())?;
    let header_bytes = write_all_to_pipe(handle, &encoded)?;
    let payload_bytes = write_all_to_pipe(handle, payload)?;
    Ok(u64::from(header_bytes) + u64::from(payload_bytes))
}

const MEETING_ALIGNMENT_TOLERANCE_MICROS: u64 = 5_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MeetingAlignmentAction {
    Pair,
    MicrophoneOnly,
    SystemOnly,
}

fn meeting_alignment_action(
    microphone_timestamp: Option<u64>,
    system_timestamp: Option<u64>,
) -> Option<MeetingAlignmentAction> {
    match (microphone_timestamp, system_timestamp) {
        (Some(microphone), Some(system))
            if microphone.abs_diff(system) <= MEETING_ALIGNMENT_TOLERANCE_MICROS =>
        {
            Some(MeetingAlignmentAction::Pair)
        }
        (Some(microphone), Some(system)) if microphone < system => {
            Some(MeetingAlignmentAction::MicrophoneOnly)
        }
        (Some(_), Some(_)) => Some(MeetingAlignmentAction::SystemOnly),
        (Some(_), None) => Some(MeetingAlignmentAction::MicrophoneOnly),
        (None, Some(_)) => Some(MeetingAlignmentAction::SystemOnly),
        (None, None) => None,
    }
}

#[cfg(windows)]
fn run_meeting_aec_relay(
    microphone_pipe: String,
    system_pipe: String,
    output_handles: Vec<isize>,
    stop_rx: mpsc::Receiver<()>,
    delay_ms: i32,
    aec_enabled: bool,
) -> MeetingRelayStats {
    let mut stats = MeetingRelayStats::default();
    let outputs: Vec<HANDLE> = output_handles
        .iter()
        .map(|handle| *handle as HANDLE)
        .collect();
    let result = (|| -> Result<(), String> {
        let microphone = open_frame_pipe_reader(&microphone_pipe)?;
        let system = match open_frame_pipe_reader(&system_pipe) {
            Ok(handle) => handle,
            Err(error) => {
                unsafe {
                    CloseHandle(microphone);
                }
                return Err(error);
            }
        };
        let processing = (|| -> Result<(), String> {
            for handle in &outputs {
                wait_for_meeting_output_pipe_client(*handle, &stop_rx)?;
            }
            let mut aec = if aec_enabled {
                Some(MeetingAec3::new(delay_ms)?)
            } else {
                None
            };
            let mut relay_sequence = 0u64;
            let mut microphone_frame = Some(read_meeting_frame(microphone, &stop_rx)?);
            let mut system_frame = Some(read_meeting_frame(system, &stop_rx)?);
            let mut microphone_done = false;
            let mut system_done = false;
            let mut mic_samples = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
            let mut system_samples = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
            let mut clean_samples = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
            let output_samples = MEETING_AEC_FRAME_SAMPLES / 3;
            let mut microphone_16k = Vec::with_capacity(output_samples);
            let mut system_16k = Vec::with_capacity(output_samples);
            let mut clean_16k = Vec::with_capacity(output_samples);
            let mut microphone_payload = Vec::with_capacity(output_samples * 2);
            let mut system_payload = Vec::with_capacity(output_samples * 2);
            let mut clean_payload = Vec::with_capacity(output_samples * 2);
            loop {
                let microphone_timestamp = microphone_frame
                    .as_ref()
                    .map(|(header, _)| header.timestamp_micros);
                let system_timestamp = system_frame
                    .as_ref()
                    .map(|(header, _)| header.timestamp_micros);
                let Some(action) = meeting_alignment_action(microphone_timestamp, system_timestamp)
                else {
                    break;
                };
                if let (Some(microphone), Some(system)) = (microphone_timestamp, system_timestamp) {
                    stats.max_input_skew_micros =
                        stats.max_input_skew_micros.max(microphone.abs_diff(system));
                }
                let consume_microphone = matches!(
                    action,
                    MeetingAlignmentAction::Pair | MeetingAlignmentAction::MicrophoneOnly
                );
                let consume_system = matches!(
                    action,
                    MeetingAlignmentAction::Pair | MeetingAlignmentAction::SystemOnly
                );
                let microphone_item = if consume_microphone {
                    microphone_frame.take()
                } else {
                    stats.microphone_padding_frames =
                        stats.microphone_padding_frames.saturating_add(1);
                    None
                };
                let system_item = if consume_system {
                    system_frame.take()
                } else {
                    stats.system_padding_frames = stats.system_padding_frames.saturating_add(1);
                    None
                };
                if let Some((_, payload)) = microphone_item.as_ref() {
                    pcm_i16_into(payload, &mut mic_samples)?;
                } else {
                    mic_samples.clear();
                    mic_samples.resize(MEETING_AEC_FRAME_SAMPLES, 0);
                }
                if let Some((_, payload)) = system_item.as_ref() {
                    pcm_i16_into(payload, &mut system_samples)?;
                } else {
                    system_samples.clear();
                    system_samples.resize(MEETING_AEC_FRAME_SAMPLES, 0);
                }
                if mic_samples.len() != MEETING_AEC_FRAME_SAMPLES
                    || system_samples.len() != MEETING_AEC_FRAME_SAMPLES
                {
                    return Err("meeting AEC3 received a non-10ms source frame".to_string());
                }
                if let Some(processor) = aec.as_mut() {
                    processor.process_into(&system_samples, &mic_samples, &mut clean_samples)?;
                } else {
                    clean_samples.clear();
                    clean_samples.extend_from_slice(&mic_samples);
                }
                let system_energy = meeting_frame_energy(&system_samples);
                if aec_enabled && system_energy >= meeting_render_active_energy_threshold() {
                    stats.aec_render_active_frames =
                        stats.aec_render_active_frames.saturating_add(1);
                    stats.aec_render_energy += system_energy;
                    stats.aec_raw_mic_energy += meeting_frame_energy(&mic_samples);
                    stats.aec_clean_mic_energy += meeting_frame_energy(&clean_samples);
                }
                downsample_meeting_48k_to_16k_into(&mic_samples, &mut microphone_16k)?;
                downsample_meeting_48k_to_16k_into(&system_samples, &mut system_16k)?;
                downsample_meeting_48k_to_16k_into(&clean_samples, &mut clean_16k)?;
                pcm_bytes_into(&microphone_16k, &mut microphone_payload);
                pcm_bytes_into(&system_16k, &mut system_payload);
                pcm_bytes_into(&clean_16k, &mut clean_payload);
                let timestamp_micros = match action {
                    MeetingAlignmentAction::Pair => microphone_timestamp
                        .unwrap_or_default()
                        .max(system_timestamp.unwrap_or_default()),
                    MeetingAlignmentAction::MicrophoneOnly => {
                        microphone_timestamp.unwrap_or_default()
                    }
                    MeetingAlignmentAction::SystemOnly => system_timestamp.unwrap_or_default(),
                };
                let microphone_eos = microphone_item
                    .as_ref()
                    .is_some_and(|(header, _)| header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM != 0);
                let system_eos = system_item
                    .as_ref()
                    .is_some_and(|(header, _)| header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM != 0);
                let will_finish =
                    (microphone_done || microphone_eos) && (system_done || system_eos);
                let mut combined_flags = microphone_item
                    .as_ref()
                    .map(|(header, _)| header.flags)
                    .unwrap_or_default()
                    | system_item
                        .as_ref()
                        .map(|(header, _)| header.flags)
                        .unwrap_or_default();
                combined_flags &= !AUDIO_FRAME_FLAG_END_OF_STREAM;
                if will_finish {
                    combined_flags |= AUDIO_FRAME_FLAG_END_OF_STREAM;
                }
                let common_header = AudioFrameHeader::new(
                    clean_payload.len() as u32,
                    relay_sequence,
                    timestamp_micros,
                    160,
                    1,
                    combined_flags,
                )
                .map_err(|error| error.to_string())?;
                stats.bytes_forwarded +=
                    write_meeting_frame(outputs[0], common_header, &microphone_payload)?;
                stats.bytes_forwarded +=
                    write_meeting_frame(outputs[1], common_header, &system_payload)?;
                let clean_header = common_header;
                stats.bytes_forwarded +=
                    write_meeting_frame(outputs[2], clean_header, &clean_payload)?;
                stats.frames_processed += 1;
                relay_sequence = relay_sequence.saturating_add(1);
                if will_finish {
                    break;
                }
                if consume_microphone {
                    if microphone_eos {
                        microphone_done = true;
                    } else {
                        microphone_frame = Some(read_meeting_frame(microphone, &stop_rx)?);
                    }
                }
                if consume_system {
                    if system_eos {
                        system_done = true;
                    } else {
                        system_frame = Some(read_meeting_frame(system, &stop_rx)?);
                    }
                }
            }
            Ok(())
        })();
        unsafe {
            CloseHandle(microphone);
            CloseHandle(system);
        }
        processing
    })();
    if let Err(error) = result {
        if error != "meetingRelayStopped" {
            stats.error = Some(error);
        }
    }
    for handle in outputs {
        unsafe {
            FlushFileBuffers(handle);
            DisconnectNamedPipe(handle);
            CloseHandle(handle);
        }
    }
    stats
}

#[cfg(windows)]
fn run_synthetic_frame_pipe_writer(
    pipe_handle: HANDLE,
    _pipe_path: String,
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
    synthetic_pcm_fixture: Option<Vec<u8>>,
) -> CaptureWriterStats {
    let mut stats = CaptureWriterStats::default();
    let started = request.clock_origin.unwrap_or_else(Instant::now);
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

    let frame_interval = Duration::from_secs_f64(
        (request.block_size as f64 / f64::from(request.sample_rate)).max(0.001),
    );
    let prebuffer_frame_target = if adopted_prebuffer_blocks.is_empty() {
        requested_prebuffer_frame_count(&request)
    } else {
        0
    };
    let mut prebuffer_frames_written = 0_u32;
    let stream_started_micros = started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
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
        let payload_sequence = sequence.saturating_sub(adopted_prebuffer_blocks.len() as u64);
        let payload =
            synthetic_frame_payload(&request, payload_sequence, synthetic_pcm_fixture.as_deref());
        let frame_duration_micros = u64::from(request.block_size).saturating_mul(1_000_000)
            / u64::from(request.sample_rate.max(1));
        let timestamp_micros =
            stream_started_micros.saturating_add(sequence.saturating_mul(frame_duration_micros));
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
    let write_deadline = Instant::now() + PREWARM_BURST_WRITE_TIMEOUT;
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
        let bytes_written = write_all_to_pipe_until(pipe_handle, &frame, write_deadline)?;
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
// Capture handoff state is kept explicit because these values cross the
// prewarm/pipe/readiness ownership boundary and are audited independently.
#[allow(clippy::too_many_arguments)]
fn run_wasapi_capture_writer(
    pipe_handle: HANDLE,
    _pipe_path: String,
    request: CaptureRequest,
    stop_rx: mpsc::Receiver<()>,
    ready_tx: Sender<Result<WasapiReady, String>>,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
    adopted_native_endpoint_id_hash: Value,
    defer_ready_until_after_pipe: bool,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
    mut deferred_prewarm_session: Option<PrewarmSession>,
) -> CaptureWriterStats {
    let mut stats = CaptureWriterStats::default();
    let mut ready_sent = false;
    let started = request.clock_origin.unwrap_or_else(Instant::now);
    let result = run_wasapi_capture_writer_inner(
        pipe_handle,
        &request,
        &stop_rx,
        &ready_tx,
        &mut ready_sent,
        &mut stats,
        started,
        adopted_prebuffer_blocks,
        &adopted_native_endpoint_id_hash,
        defer_ready_until_after_pipe,
        microphone_channel_selection,
        &mut deferred_prewarm_session,
    );
    if let Err(err) = result {
        if !ready_sent {
            let _ = ready_tx.send(Err(err.clone()));
        }
        stats.error = Some(err);
    }
    if let Some(stop_payload) = stop_deferred_prewarm_session(
        &mut deferred_prewarm_session,
        "captureWriterFinishedBeforePrewarmHandoff",
    ) {
        stats.deferred_prewarm_stop = Some(stop_payload);
    }
    unsafe {
        DisconnectNamedPipe(pipe_handle);
        CloseHandle(pipe_handle);
    }
    stats
}

#[cfg(windows)]
fn run_wasapi_prewarm_worker(
    request: CaptureRequest,
    control_rx: mpsc::Receiver<PrewarmWorkerCommand>,
    ready_tx: Sender<Result<WasapiReady, String>>,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
) -> PrewarmStats {
    let mut stats = PrewarmStats::default();
    let mut ready_sent = false;
    let result = run_wasapi_prewarm_worker_inner(
        &request,
        &control_rx,
        &ready_tx,
        &mut ready_sent,
        &mut stats,
        buffer,
        microphone_channel_selection,
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
    let loopback = request.capture_kind.eq_ignore_ascii_case("loopback");
    let data_flow = if loopback { eRender } else { eCapture };
    let requested_hash = request.native_endpoint_id_hash.trim();
    if !requested_hash.is_empty() {
        let collection =
            unsafe { enumerator.EnumAudioEndpoints(data_flow, DEVICE_STATE_ACTIVE) }
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

    let device = unsafe { enumerator.GetDefaultAudioEndpoint(data_flow, eConsole) }
        .map_err(|err| format!("default WASAPI endpoint unavailable: {err}"))?;
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

fn validate_prewarm_endpoint_continuity(
    prewarm_endpoint_id_hash: &Value,
    replacement_endpoint_id_hash: &Value,
) -> Result<(), String> {
    let prewarm_hash = prewarm_endpoint_id_hash
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let replacement_hash = replacement_endpoint_id_hash
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if let (Some(prewarm_hash), Some(replacement_hash)) = (prewarm_hash, replacement_hash) {
        if prewarm_hash != replacement_hash {
            // Do not put either endpoint hash in the error. The caller needs
            // only the fail-closed reason, and no prewarm block may be written
            // once this mismatch has been observed.
            return Err("prewarmEndpointChangedDuringHandoff".to_string());
        }
    }
    Ok(())
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
#[allow(clippy::too_many_arguments)]
fn run_wasapi_capture_writer_inner(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    ready_tx: &Sender<Result<WasapiReady, String>>,
    ready_sent: &mut bool,
    stats: &mut CaptureWriterStats,
    started: Instant,
    adopted_prebuffer_blocks: Vec<Vec<u8>>,
    adopted_native_endpoint_id_hash: &Value,
    defer_ready_until_after_pipe: bool,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
    deferred_prewarm_session: &mut Option<PrewarmSession>,
) -> Result<(), String> {
    let mut sequence = 0_u64;
    if defer_ready_until_after_pipe {
        wait_for_pipe_client(pipe_handle, stop_rx)?;
        stats.connected = true;
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => return Ok(()),
            Err(TryRecvError::Empty) => {}
        }
    }

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
        if defer_ready_until_after_pipe {
            validate_prewarm_endpoint_continuity(
                adopted_native_endpoint_id_hash,
                &endpoint_id_hash,
            )?;
        }
        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|err| format!("IAudioClient activation failed: {err}"))?;
        let mix_format_ptr = unsafe { client.GetMixFormat() }
            .map_err(|err| format!("GetMixFormat failed: {err}"))?;
        let mix_format = unsafe { wasapi_mix_format_from_ptr(mix_format_ptr) };
        let initialize_args = wasapi_shared_initialize_args(&request.capture_kind);
        let init_result = unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                initialize_args.stream_flags,
                initialize_args.buffer_duration_100ns,
                initialize_args.periodicity_100ns,
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

        if defer_ready_until_after_pipe {
            // Selection is now stable and its opened endpoint was verified
            // against the prewarm source. Only now may old-source audio become
            // visible to the consumer; a default-device A→B switch therefore
            // fails without emitting a mixed transcript prefix.
            write_adopted_prebuffer_blocks(
                pipe_handle,
                request,
                &adopted_prebuffer_blocks,
                started,
                &mut sequence,
                stats,
            )?;
            match stop_rx.try_recv() {
                Ok(()) | Err(TryRecvError::Disconnected) => return Ok(()),
                Err(TryRecvError::Empty) => {}
            }
        }

        unsafe { client.Start() }.map_err(|err| format!("WASAPI Start failed: {err}"))?;
        let stream_started_micros = started.elapsed().as_micros().min(u128::from(u64::MAX)) as u64;
        let capture_result = (|| -> Result<(), String> {
            if defer_ready_until_after_pipe {
                if let Some((stop_payload, tail_blocks)) =
                    finish_deferred_prewarm_handoff(deferred_prewarm_session, "adoptedIntoCapture")
                {
                    stats.deferred_prewarm_stop = Some(stop_payload);
                    let tail_blocks = tail_blocks?;
                    // The initial rolling window was written before the
                    // replacement client started. Append every block produced
                    // after that snapshot exactly once, still marked as
                    // prebuffer, before consuming replacement-client frames.
                    write_adopted_prebuffer_blocks(
                        pipe_handle,
                        request,
                        &tail_blocks,
                        started,
                        &mut sequence,
                        stats,
                    )?;
                }
            }
            let ready = Ok(WasapiReady {
                endpoint_id_hash,
                endpoint_selection: selected.endpoint_selection,
                mix_format: mix_format.clone(),
            });
            if defer_ready_until_after_pipe {
                let _ = ready_tx.send(ready);
            } else {
                ready_tx
                    .send(ready)
                    .map_err(|err| format!("could not report WASAPI readiness: {err}"))?;
            }
            *ready_sent = true;

            if !defer_ready_until_after_pipe {
                wait_for_pipe_client(pipe_handle, stop_rx)?;
                stats.connected = true;
                write_adopted_prebuffer_blocks(
                    pipe_handle,
                    request,
                    &adopted_prebuffer_blocks,
                    started,
                    &mut sequence,
                    stats,
                )?;
            }
            pump_wasapi_capture(
                pipe_handle,
                request,
                stop_rx,
                &capture_client,
                mix_format,
                stats,
                stream_started_micros,
                sequence,
                !defer_ready_until_after_pipe,
                microphone_channel_selection,
            )
        })();
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
    control_rx: &mpsc::Receiver<PrewarmWorkerCommand>,
    ready_tx: &Sender<Result<WasapiReady, String>>,
    ready_sent: &mut bool,
    stats: &mut PrewarmStats,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
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
        let initialize_args = wasapi_shared_initialize_args("microphone");
        let init_result = unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                initialize_args.stream_flags,
                initialize_args.buffer_duration_100ns,
                initialize_args.periodicity_100ns,
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

        unsafe { client.Start() }.map_err(|err| format!("WASAPI Start failed: {err}"))?;
        let prewarm_result = (|| -> Result<(), String> {
            // Readiness means the replacement IAudioClient is already running,
            // not merely initialized. The Tauri owner may stop the previous
            // Live Mic capture as soon as it receives this message, so sending
            // it before Start() creates a visible privacy-indicator gap.
            ready_tx
                .send(Ok(WasapiReady {
                    endpoint_id_hash: endpoint_id_hash.clone(),
                    endpoint_selection: selected.endpoint_selection,
                    mix_format: mix_format.clone(),
                }))
                .map_err(|err| format!("could not report WASAPI prewarm readiness: {err}"))?;
            *ready_sent = true;

            pump_wasapi_prewarm(WasapiPrewarmPumpContext {
                request,
                control_rx,
                prewarm_endpoint_id_hash: &endpoint_id_hash,
                capture_client: &capture_client,
                mix_format,
                stats,
                buffer,
                microphone_channel_selection,
            })
        })();
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
#[allow(clippy::too_many_arguments)]
fn pump_wasapi_capture(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    stop_rx: &mpsc::Receiver<()>,
    capture_client: &IAudioCaptureClient,
    mix_format: WasapiMixFormat,
    stats: &mut CaptureWriterStats,
    stream_started_micros: u64,
    initial_sequence: u64,
    use_live_prebuffer: bool,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
) -> Result<(), String> {
    let mut converter = WasapiPcmConverter::with_channel_selection(
        mix_format,
        request,
        microphone_channel_selection,
    );
    let loopback = request.capture_kind.eq_ignore_ascii_case("loopback");
    let loopback_frame_interval = Duration::from_secs_f64(
        f64::from(request.block_size) / f64::from(request.sample_rate.max(1)),
    );
    let mut next_loopback_frame_at = Instant::now() + loopback_frame_interval;
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
            let now = Instant::now();
            if loopback && now >= next_loopback_frame_at {
                let payload = vec![
                    0_u8;
                    request.block_size as usize
                        * request.channels as usize
                        * std::mem::size_of::<i16>()
                ];
                write_wasapi_capture_frame(
                    pipe_handle,
                    request,
                    &payload,
                    0,
                    stream_started_micros,
                    &mut sequence,
                    stats,
                )?;
                next_loopback_frame_at = advance_loopback_frame_deadline(
                    next_loopback_frame_at,
                    now,
                    loopback_frame_interval,
                );
            }
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
                write_wasapi_capture_frame(
                    pipe_handle,
                    request,
                    &payload,
                    flags,
                    stream_started_micros,
                    &mut sequence,
                    stats,
                )?;
                if loopback {
                    next_loopback_frame_at = Instant::now() + loopback_frame_interval;
                }
            }
            packet_frames = unsafe { capture_client.GetNextPacketSize() }
                .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
        }
    }
}

#[cfg(windows)]
fn write_wasapi_capture_frame(
    pipe_handle: HANDLE,
    request: &CaptureRequest,
    payload: &[u8],
    flags: u16,
    stream_started_micros: u64,
    sequence: &mut u64,
    stats: &mut CaptureWriterStats,
) -> Result<(), String> {
    let frame_duration_micros = u64::from(request.block_size).saturating_mul(1_000_000)
        / u64::from(request.sample_rate.max(1));
    let timestamp_micros =
        stream_started_micros.saturating_add(sequence.saturating_mul(frame_duration_micros));
    let header = AudioFrameHeader::new(
        payload.len() as u32,
        *sequence,
        timestamp_micros,
        request.block_size,
        request.channels,
        flags,
    )
    .map_err(|err| format!("WASAPI frame header failed: {err}"))?;
    let frame = encode_audio_frame(&header, payload)
        .map_err(|err| format!("WASAPI frame encode failed: {err}"))?;
    let bytes_written = write_all_to_pipe(pipe_handle, &frame)?;
    stats.frames_written = stats.frames_written.saturating_add(1);
    if flags & AUDIO_FRAME_FLAG_PREBUFFER != 0 {
        stats.prebuffer_frames_written = stats.prebuffer_frames_written.saturating_add(1);
    } else {
        stats.live_frames_written = stats.live_frames_written.saturating_add(1);
    }
    stats.bytes_written = stats.bytes_written.saturating_add(u64::from(bytes_written));
    *sequence = sequence.saturating_add(1);
    Ok(())
}

fn advance_loopback_frame_deadline(
    previous_deadline: Instant,
    now: Instant,
    frame_interval: Duration,
) -> Instant {
    let scheduled = previous_deadline + frame_interval;
    if scheduled + frame_interval < now {
        now + frame_interval
    } else {
        scheduled
    }
}

#[cfg(windows)]
struct PromotedCaptureState {
    pipe: PromotionPipeHandle,
    request: CaptureRequest,
    started: Instant,
    connect_started: Instant,
    connected: bool,
    stream_started_micros: u64,
    sequence: u64,
    adopted_blocks: Vec<Vec<u8>>,
}

#[cfg(windows)]
fn validate_prewarm_promotion_format(
    prewarm_request: &CaptureRequest,
    capture_request: &CaptureRequest,
) -> Result<(), &'static str> {
    if !capture_request
        .capture_kind
        .eq_ignore_ascii_case("microphone")
    {
        return Err("prewarmPromotionCaptureKindMismatch");
    }
    if prewarm_request.sample_rate != capture_request.sample_rate
        || prewarm_request.channels != capture_request.channels
        || prewarm_request.block_size != capture_request.block_size
    {
        return Err("prewarmPromotionFormatMismatch");
    }
    Ok(())
}

fn validate_in_place_endpoint_hashes(
    prewarm_endpoint_id_hash: &Value,
    current_endpoint_id_hash: &Value,
) -> Result<(), &'static str> {
    let prewarm_hash = prewarm_endpoint_id_hash
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or("prewarmPromotionEndpointIdentityUnavailable")?;
    let current_hash = current_endpoint_id_hash
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or("prewarmPromotionEndpointIdentityUnavailable")?;
    if prewarm_hash != current_hash {
        return Err("prewarmPromotionEndpointMismatch");
    }
    Ok(())
}

#[cfg(windows)]
struct WasapiPrewarmPumpContext<'a> {
    request: &'a CaptureRequest,
    control_rx: &'a mpsc::Receiver<PrewarmWorkerCommand>,
    prewarm_endpoint_id_hash: &'a Value,
    capture_client: &'a IAudioCaptureClient,
    mix_format: WasapiMixFormat,
    stats: &'a mut PrewarmStats,
    buffer: Arc<Mutex<PrewarmBuffer>>,
    microphone_channel_selection: Arc<Mutex<MicrophoneChannelSelectionState>>,
}

#[cfg(windows)]
fn pump_wasapi_prewarm(context: WasapiPrewarmPumpContext<'_>) -> Result<(), String> {
    let WasapiPrewarmPumpContext {
        request,
        control_rx,
        prewarm_endpoint_id_hash,
        capture_client,
        mix_format,
        stats,
        buffer,
        microphone_channel_selection,
    } = context;
    let mut converter = WasapiPcmConverter::with_channel_selection(
        mix_format,
        request,
        microphone_channel_selection,
    );
    let max_buffered_blocks = u64::from(requested_prebuffer_frame_count(request));
    let mut promotion_state: Option<PromotedCaptureState> = None;
    let result = (|| -> Result<(), String> {
        loop {
            loop {
                match control_rx.try_recv() {
                    Ok(PrewarmWorkerCommand::Stop) | Err(TryRecvError::Disconnected) => {
                        return Ok(())
                    }
                    Ok(PrewarmWorkerCommand::Promote(promotion)) => {
                        let PrewarmPromotionRequest {
                            pipe,
                            request: capture_request,
                            current_endpoint_id_hash,
                            decision,
                            accepted_tx,
                        } = *promotion;
                        if promotion_state.is_some() {
                            let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                "prewarmPromotionAlreadyStarted",
                                PrewarmPromotionFallback::Abort,
                            )));
                            continue;
                        }
                        if let Err(reason) =
                            validate_prewarm_promotion_format(request, &capture_request)
                        {
                            let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                reason,
                                PrewarmPromotionFallback::ColdWithOverlap,
                            )));
                            continue;
                        }
                        if let Err(reason) = validate_in_place_endpoint_hashes(
                            prewarm_endpoint_id_hash,
                            &current_endpoint_id_hash,
                        ) {
                            let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                reason,
                                PrewarmPromotionFallback::ColdWithOverlap,
                            )));
                            continue;
                        }
                        // This is the single ownership boundary. A caller that
                        // timed out and changed PENDING to CANCELLED wins before
                        // any rolling audio leaves the prewarm buffer. Once this
                        // CAS succeeds the caller is forbidden from launching a
                        // replacement capture.
                        if !decision.try_accept() {
                            let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                "prewarmPromotionCancelledBeforeHandoff",
                                PrewarmPromotionFallback::AdoptWithOverlap,
                            )));
                            continue;
                        }

                        let adopted_blocks = match buffer.lock() {
                            Ok(mut buffer) => match buffer.begin_handoff() {
                                Ok(blocks) => blocks,
                                Err(reason) => {
                                    let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                        reason,
                                        PrewarmPromotionFallback::Abort,
                                    )));
                                    continue;
                                }
                            },
                            Err(_) => {
                                let _ = accepted_tx.send(Err(PrewarmPromotionFailure::new(
                                    "prewarmBufferLockPoisoned",
                                    PrewarmPromotionFallback::Abort,
                                )));
                                continue;
                            }
                        };
                        let accepted = PrewarmPromotionAccepted {
                            blocks: adopted_blocks.len() as u64,
                            audio_frames: (adopted_blocks.len() as u64)
                                .saturating_mul(u64::from(request.block_size)),
                            payload_bytes: adopted_blocks
                                .iter()
                                .map(|block| block.len() as u64)
                                .sum(),
                        };
                        let promoted_capture_stats = CaptureWriterStats {
                            deferred_prewarm_stop: Some(json!({
                                "stopped": false,
                                "reason": "promotedInPlace",
                                "physicalClientReused": true,
                            })),
                            ..CaptureWriterStats::default()
                        };
                        let promoted_state = PromotedCaptureState {
                            pipe,
                            started: capture_request.clock_origin.unwrap_or_else(Instant::now),
                            connect_started: Instant::now(),
                            connected: false,
                            stream_started_micros: 0,
                            sequence: 0,
                            request: capture_request,
                            adopted_blocks,
                        };
                        if accepted_tx.send(Ok(accepted)).is_err() {
                            // The caller disappeared after ACCEPTED but before it
                            // obtained ownership. Restore the exact snapshot and
                            // any tail accumulated under the handoff marker, then
                            // continue the original prewarm stream.
                            buffer
                                .lock()
                                .map_err(|_| "prewarmBufferLockPoisoned".to_string())?
                                .restore_handoff_snapshot(promoted_state.adopted_blocks);
                            continue;
                        }
                        stats.promoted_capture = Some(promoted_capture_stats);
                        promotion_state = Some(promoted_state);
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }

            if let Some(state) = promotion_state.as_mut() {
                if !state.connected {
                    if state.connect_started.elapsed() >= FRAME_PIPE_CONNECT_TIMEOUT {
                        return Err(format!(
                            "audio frame pipe client did not connect within {} ms",
                            FRAME_PIPE_CONNECT_TIMEOUT.as_millis()
                        ));
                    }
                    if poll_frame_pipe_client(state.pipe.raw())? {
                        state.connected = true;
                        state.stream_started_micros = state
                            .started
                            .elapsed()
                            .as_micros()
                            .min(u128::from(u64::MAX))
                            as u64;
                        let tail_blocks = buffer
                            .lock()
                            .map_err(|_| "prewarmBufferLockPoisoned".to_string())?
                            .finish_handoff()
                            .map_err(str::to_string)?;
                        state.adopted_blocks.extend(tail_blocks);
                        let capture_stats = stats
                            .promoted_capture
                            .as_mut()
                            .ok_or_else(|| "promotedCaptureStatsMissing".to_string())?;
                        capture_stats.connected = true;
                        write_adopted_prebuffer_blocks(
                            state.pipe.raw(),
                            &state.request,
                            &state.adopted_blocks,
                            state.started,
                            &mut state.sequence,
                            capture_stats,
                        )?;
                        state.adopted_blocks.clear();
                    }
                }
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
                    if let Some(state) = promotion_state.as_mut().filter(|state| state.connected) {
                        let capture_stats = stats
                            .promoted_capture
                            .as_mut()
                            .ok_or_else(|| "promotedCaptureStatsMissing".to_string())?;
                        write_wasapi_capture_frame(
                            state.pipe.raw(),
                            &state.request,
                            &payload,
                            0,
                            state.stream_started_micros,
                            &mut state.sequence,
                            capture_stats,
                        )?;
                    } else if max_buffered_blocks > 0 || promotion_state.is_some() {
                        match buffer.lock() {
                            Ok(mut buffer) => {
                                buffer.push(payload).map_err(str::to_string)?;
                                stats.buffered_blocks = buffer.block_count();
                                stats.buffered_audio_frames =
                                    buffer.audio_frame_count(request.block_size);
                                stats.buffered_payload_bytes = buffer.payload_bytes();
                            }
                            Err(_) => {
                                return Err("prewarmBufferLockPoisoned".to_string());
                            }
                        }
                    }
                }
                packet_frames = unsafe { capture_client.GetNextPacketSize() }
                    .map_err(|err| format!("WASAPI GetNextPacketSize failed: {err}"))?;
            }
        }
    })();

    if let Some(state) = promotion_state.take() {
        if let Err(reason) = &result {
            if let Some(capture_stats) = stats.promoted_capture.as_mut() {
                capture_stats.error = Some(reason.clone());
            }
        }
        if state.connected {
            unsafe {
                DisconnectNamedPipe(state.pipe.raw());
            }
        }
        // PromotionPipeHandle closes the OS handle here.  Do not flush: a
        // disconnected or stalled client must never make stop/rollback wait
        // without a bound.
    }
    result
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
    wait_for_pipe_client_with_timeout(
        pipe_handle,
        stop_rx,
        FRAME_PIPE_CONNECT_TIMEOUT,
        FramePipePostConnectMode::PreserveNonBlocking,
    )
}

#[cfg(windows)]
fn poll_frame_pipe_client(pipe_handle: HANDLE) -> Result<bool, String> {
    let connected = unsafe { ConnectNamedPipe(pipe_handle, null_mut()) };
    if connected != 0 {
        finish_frame_pipe_connect(pipe_handle, FramePipePostConnectMode::PreserveNonBlocking)?;
        return Ok(true);
    }
    let error = unsafe { GetLastError() };
    if error == ERROR_PIPE_CONNECTED {
        finish_frame_pipe_connect(pipe_handle, FramePipePostConnectMode::PreserveNonBlocking)?;
        return Ok(true);
    }
    if error == ERROR_PIPE_LISTENING {
        return Ok(false);
    }
    Err(format!(
        "ConnectNamedPipe failed for audio frame pipe: {error}"
    ))
}

#[cfg(windows)]
fn wait_for_meeting_output_pipe_client(
    pipe_handle: HANDLE,
    stop_rx: &mpsc::Receiver<()>,
) -> Result<(), String> {
    wait_for_pipe_client_with_timeout(
        pipe_handle,
        stop_rx,
        FRAME_PIPE_CONNECT_TIMEOUT,
        FramePipePostConnectMode::SwitchToBlocking,
    )
}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FramePipePostConnectMode {
    PreserveNonBlocking,
    SwitchToBlocking,
}

#[cfg(windows)]
fn wait_for_pipe_client_with_timeout(
    pipe_handle: HANDLE,
    stop_rx: &mpsc::Receiver<()>,
    timeout: Duration,
    post_connect_mode: FramePipePostConnectMode,
) -> Result<(), String> {
    let started = Instant::now();
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => {
                return Err("audio frame pipe stopped before client connected".to_string())
            }
            Err(TryRecvError::Empty) => {}
        }
        if started.elapsed() >= timeout {
            return Err(format!(
                "audio frame pipe client did not connect within {} ms",
                timeout.as_millis()
            ));
        }
        let connected = unsafe { ConnectNamedPipe(pipe_handle, null_mut()) };
        if connected != 0 {
            return finish_frame_pipe_connect(pipe_handle, post_connect_mode);
        }
        let err = unsafe { GetLastError() };
        if err == ERROR_PIPE_CONNECTED {
            return finish_frame_pipe_connect(pipe_handle, post_connect_mode);
        }
        if err == ERROR_PIPE_LISTENING {
            thread::sleep(Duration::from_millis(10));
            continue;
        }
        return Err(format!(
            "ConnectNamedPipe failed for audio frame pipe: {err}"
        ));
    }
}

#[cfg(windows)]
fn finish_frame_pipe_connect(
    pipe_handle: HANDLE,
    post_connect_mode: FramePipePostConnectMode,
) -> Result<(), String> {
    let Some(mode) = frame_pipe_wait_mode_update(post_connect_mode) else {
        return Ok(());
    };
    let updated = unsafe { SetNamedPipeHandleState(pipe_handle, &mode, null_mut(), null_mut()) };
    if updated == 0 {
        return Err(format!(
            "SetNamedPipeHandleState failed for connected audio frame pipe: {}",
            unsafe { GetLastError() }
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn frame_pipe_wait_mode_update(post_connect_mode: FramePipePostConnectMode) -> Option<u32> {
    match post_connect_mode {
        FramePipePostConnectMode::PreserveNonBlocking => None,
        FramePipePostConnectMode::SwitchToBlocking => Some(PIPE_WAIT),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum PipeWriteAttemptError {
    Retry,
    Fatal(String),
}

fn write_all_with_bounded_retry<Attempt, Wait>(
    bytes: &[u8],
    deadline: Instant,
    mut attempt: Attempt,
    mut wait: Wait,
) -> Result<u32, String>
where
    Attempt: FnMut(&[u8]) -> Result<usize, PipeWriteAttemptError>,
    Wait: FnMut(),
{
    let mut offset = 0usize;
    while offset < bytes.len() {
        if Instant::now() >= deadline {
            return Err("audioFramePipeWriteTimeout".to_string());
        }
        match attempt(&bytes[offset..]) {
            Ok(0) | Err(PipeWriteAttemptError::Retry) => {
                wait();
            }
            Ok(written) => {
                if written > bytes.len() - offset {
                    return Err("audioFramePipeWriteReportedInvalidLength".to_string());
                }
                offset += written;
            }
            Err(PipeWriteAttemptError::Fatal(error)) => return Err(error),
        }
    }
    u32::try_from(offset).map_err(|_| "audioFramePipeWriteLengthOverflow".to_string())
}

#[cfg(windows)]
fn write_all_to_pipe_until(
    pipe_handle: HANDLE,
    bytes: &[u8],
    deadline: Instant,
) -> Result<u32, String> {
    write_all_with_bounded_retry(
        bytes,
        deadline,
        |remaining| {
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
            if ok != 0 {
                return Ok(written as usize);
            }
            let error = unsafe { GetLastError() };
            if error == ERROR_NO_DATA || error == ERROR_PIPE_LISTENING || error == ERROR_PIPE_BUSY {
                return Err(PipeWriteAttemptError::Retry);
            }
            Err(PipeWriteAttemptError::Fatal(format!(
                "WriteFile failed for audio frame pipe: {error}"
            )))
        },
        || thread::sleep(Duration::from_millis(1)),
    )
}

#[cfg(windows)]
fn write_all_to_pipe(pipe_handle: HANDLE, bytes: &[u8]) -> Result<u32, String> {
    write_all_to_pipe_until(
        pipe_handle,
        bytes,
        Instant::now() + FRAME_PIPE_WRITE_TIMEOUT,
    )
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
            "captureStatus",
            "captureStop",
            "meetingCaptureStart",
            "meetingCaptureStatus",
            "meetingCaptureStop",
            "prewarmStart",
            "prewarmStatus",
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
        "wasapiLoopbackAvailable": wasapi_capture_enabled(),
        "meetingCaptureAvailable": wasapi_capture_enabled() || synthetic_capture_enabled(),
        "meetingAec3": {"available": true, "implementation": "aec3-rs", "version": "0.2.0"},
        "wasapiCaptureEnv": WASAPI_CAPTURE_ENV,
        "syntheticFramePipeAvailable": synthetic_capture_enabled(),
        "syntheticFramePipeEnv": SYNTHETIC_CAPTURE_ENV,
        "syntheticSignalAvailable": synthetic_signal_enabled(),
        "syntheticSignalEnv": SYNTHETIC_SIGNAL_ENV,
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
    use std::sync::{MutexGuard, OnceLock};

    static AUDIO_ENV_TEST_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

    struct EnvVarGuard {
        key: &'static str,
        old_value: Option<String>,
    }

    impl Drop for EnvVarGuard {
        fn drop(&mut self) {
            unsafe {
                match &self.old_value {
                    Some(value) => env::set_var(self.key, value),
                    None => env::remove_var(self.key),
                }
            }
        }
    }

    fn audio_env_test_lock() -> MutexGuard<'static, ()> {
        AUDIO_ENV_TEST_LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .expect("audio env test lock poisoned")
    }

    fn set_audio_test_env(key: &'static str, value: &str) -> EnvVarGuard {
        let old_value = env::var(key).ok();
        unsafe {
            env::set_var(key, value);
        }
        EnvVarGuard { key, old_value }
    }

    fn test_microphone_capture_request() -> CaptureRequest {
        CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 512,
            device_preference: "default".to_string(),
            port_audio_label: String::new(),
            native_endpoint_id_hash: String::new(),
            prebuffer_ms: 400,
            prewarm_id: "prewarm-test".to_string(),
            capture_kind: "microphone".to_string(),
            clock_origin: None,
        }
    }

    fn test_prewarm_session_with_stats(stats: PrewarmStats) -> PrewarmSession {
        let (control_tx, control_rx) = mpsc::channel();
        let join_handle = thread::spawn(move || loop {
            match control_rx.recv() {
                Ok(PrewarmWorkerCommand::Stop) | Err(_) => return stats,
                Ok(PrewarmWorkerCommand::Promote(promotion)) => {
                    let _ = promotion.accepted_tx.send(Err(PrewarmPromotionFailure::new(
                        "testWorkerDoesNotPromote",
                        PrewarmPromotionFallback::Abort,
                    )));
                }
            }
        });
        PrewarmSession {
            prewarm_id: "prewarm-test".to_string(),
            source: "wasapi-prewarm",
            request: test_microphone_capture_request(),
            control_tx,
            join_handle: Some(join_handle),
            started_at: Instant::now(),
            buffer: Arc::new(Mutex::new(PrewarmBuffer::new(8))),
            block_size: 512,
            native_endpoint_id_hash: Value::String("endpoint-a".to_string()),
            endpoint_selection: json!({
                "mode": "default",
                "selectedNativeEndpointIdHash": "endpoint-a",
                "usedDefaultEndpoint": true,
            }),
            mix_format_payload: json!({"sampleRate": 48_000, "channels": 2}),
            resampler_payload: json!({
                "sourceSampleRate": 48_000,
                "targetSampleRate": 16_000,
                "sourceChannels": 2,
                "targetChannels": 1,
                "method": "nearest",
            }),
            microphone_channel_selection: Arc::new(Mutex::new(
                MicrophoneChannelSelectionState::default(),
            )),
        }
    }

    fn test_prewarm_session_with_worker(
        buffer: Arc<Mutex<PrewarmBuffer>>,
        control_tx: Sender<PrewarmWorkerCommand>,
        join_handle: JoinHandle<PrewarmStats>,
    ) -> PrewarmSession {
        PrewarmSession {
            prewarm_id: "prewarm-test".to_string(),
            source: "wasapi-prewarm",
            request: test_microphone_capture_request(),
            control_tx,
            join_handle: Some(join_handle),
            started_at: Instant::now(),
            buffer,
            block_size: 512,
            native_endpoint_id_hash: Value::String("endpoint-a".to_string()),
            endpoint_selection: Value::Null,
            mix_format_payload: Value::Null,
            resampler_payload: Value::Null,
            microphone_channel_selection: Arc::new(Mutex::new(
                MicrophoneChannelSelectionState::default(),
            )),
        }
    }

    #[test]
    fn stdio_request_reader_rejects_oversized_lines() {
        let mut input = io::Cursor::new(vec![b'x'; SIDECAR_JSON_LINE_MAX_BYTES + 1]);
        let mut line = String::new();

        let error = read_json_line_limited(&mut input, &mut line).unwrap_err();

        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn prewarm_handoff_delivers_snapshot_and_marked_tail_exactly_once_in_order() {
        let mut buffer = PrewarmBuffer::new(8);
        buffer.push(vec![0x11]).unwrap();
        buffer.push(vec![0x22]).unwrap();

        let snapshot = buffer.begin_handoff().unwrap();
        buffer.push(vec![0x33]).unwrap();
        buffer.push(vec![0x44]).unwrap();
        buffer.push(vec![0x55]).unwrap();
        let tail = buffer.finish_handoff().unwrap();

        let markers = snapshot
            .into_iter()
            .chain(tail)
            .map(|block| block[0])
            .collect::<Vec<_>>();
        assert_eq!(markers, vec![0x11, 0x22, 0x33, 0x44, 0x55]);
        assert_eq!(buffer.finish_handoff(), Err("prewarmHandoffNotStarted"));
    }

    #[test]
    fn canceled_promotion_cannot_be_accepted_or_begin_a_handoff() {
        let decision = PrewarmPromotionDecision::default();

        assert!(decision.cancel_if_pending());
        assert!(!decision.try_accept());
        assert!(!decision.cancel_if_pending());
    }

    #[test]
    fn accepted_promotion_cannot_fall_back_after_the_ownership_boundary() {
        let decision = PrewarmPromotionDecision::default();

        assert!(decision.try_accept());
        assert!(!decision.cancel_if_pending());
        assert!(!decision.try_accept());
    }

    #[test]
    fn endpoint_resolver_lease_allows_only_one_detached_worker() {
        assert!(!PROMOTION_ENDPOINT_RESOLVER_ACTIVE.load(Ordering::Acquire));
        let first = PromotionEndpointResolverLease::try_acquire()
            .expect("first endpoint resolver should acquire the lease");

        assert!(PROMOTION_ENDPOINT_RESOLVER_ACTIVE.load(Ordering::Acquire));
        assert!(PromotionEndpointResolverLease::try_acquire().is_none());

        drop(first);
        assert!(!PROMOTION_ENDPOINT_RESOLVER_ACTIVE.load(Ordering::Acquire));
        let replacement = PromotionEndpointResolverLease::try_acquire()
            .expect("lease should become available after worker exit");
        drop(replacement);
        assert!(!PROMOTION_ENDPOINT_RESOLVER_ACTIVE.load(Ordering::Acquire));
    }

    #[test]
    fn rejected_promotion_restores_snapshot_and_tail_in_original_order() {
        let mut buffer = PrewarmBuffer::new(8);
        buffer.push(vec![0x11]).unwrap();
        buffer.push(vec![0x22]).unwrap();
        let snapshot = buffer.begin_handoff().unwrap();
        buffer.push(vec![0x33]).unwrap();
        buffer.push(vec![0x44]).unwrap();

        buffer.restore_handoff_snapshot(snapshot);
        let restored = buffer.begin_handoff().unwrap();

        assert_eq!(
            restored
                .into_iter()
                .map(|block| block[0])
                .collect::<Vec<_>>(),
            vec![0x11, 0x22, 0x33, 0x44]
        );
    }

    #[test]
    fn rejected_promotion_restores_only_the_newest_bounded_audio() {
        let mut buffer = PrewarmBuffer::new(4);
        buffer.push(vec![0x11]).unwrap();
        buffer.push(vec![0x22]).unwrap();
        buffer.push(vec![0x33]).unwrap();
        let snapshot = buffer.begin_handoff().unwrap();
        buffer.push(vec![0x44]).unwrap();
        buffer.push(vec![0x55]).unwrap();

        buffer.restore_handoff_snapshot(snapshot);
        let restored = buffer.begin_handoff().unwrap();

        assert_eq!(
            restored
                .into_iter()
                .map(|block| block[0])
                .collect::<Vec<_>>(),
            vec![0x22, 0x33, 0x44, 0x55]
        );
    }

    #[test]
    fn bounded_pipe_write_retries_partial_zero_and_transient_attempts_without_duplication() {
        let input = b"abcdefgh";
        let mut output = Vec::new();
        let mut attempts = 0_u32;

        let written = write_all_with_bounded_retry(
            input,
            Instant::now() + Duration::from_secs(1),
            |remaining| {
                attempts += 1;
                match attempts {
                    1 => Ok(0),
                    2 => Err(PipeWriteAttemptError::Retry),
                    _ => {
                        let count = remaining.len().min(2);
                        output.extend_from_slice(&remaining[..count]);
                        Ok(count)
                    }
                }
            },
            || {},
        )
        .unwrap();

        assert_eq!(written, input.len() as u32);
        assert_eq!(output, input);
        assert!(attempts >= 6);
    }

    #[test]
    fn bounded_pipe_write_times_out_instead_of_spinning_forever() {
        let started = Instant::now();
        let error = write_all_with_bounded_retry(
            b"audio",
            started + Duration::from_millis(4),
            |_| Err(PipeWriteAttemptError::Retry),
            || thread::sleep(Duration::from_millis(1)),
        )
        .unwrap_err();

        assert_eq!(error, "audioFramePipeWriteTimeout");
        assert!(started.elapsed() < Duration::from_millis(100));
    }

    #[test]
    fn bounded_pipe_write_preserves_fatal_errors() {
        let error = write_all_with_bounded_retry(
            b"audio",
            Instant::now() + Duration::from_secs(1),
            |_| Err(PipeWriteAttemptError::Fatal("brokenPipe".to_string())),
            || {},
        )
        .unwrap_err();

        assert_eq!(error, "brokenPipe");
    }

    #[cfg(windows)]
    #[test]
    fn in_place_prewarm_promotion_requires_matching_live_format() {
        let prewarm = test_microphone_capture_request();
        let mut capture = prewarm.clone();

        assert_eq!(
            validate_prewarm_promotion_format(&prewarm, &capture),
            Ok(())
        );

        capture.sample_rate = 48_000;
        assert_eq!(
            validate_prewarm_promotion_format(&prewarm, &capture),
            Err("prewarmPromotionFormatMismatch")
        );
        capture = prewarm.clone();
        capture.channels = 2;
        assert_eq!(
            validate_prewarm_promotion_format(&prewarm, &capture),
            Err("prewarmPromotionFormatMismatch")
        );
        capture = prewarm.clone();
        capture.block_size = 160;
        assert_eq!(
            validate_prewarm_promotion_format(&prewarm, &capture),
            Err("prewarmPromotionFormatMismatch")
        );
        capture = prewarm.clone();
        capture.capture_kind = "loopback".to_string();
        assert_eq!(
            validate_prewarm_promotion_format(&prewarm, &capture),
            Err("prewarmPromotionCaptureKindMismatch")
        );
    }

    #[test]
    fn in_place_prewarm_promotion_requires_same_actual_endpoint() {
        let endpoint_a = Value::String("endpoint-a".to_string());
        let another_endpoint_a = Value::String("endpoint-a".to_string());
        let endpoint_b = Value::String("endpoint-b".to_string());

        assert_eq!(
            validate_in_place_endpoint_hashes(&endpoint_a, &another_endpoint_a),
            Ok(())
        );
        assert_eq!(
            validate_in_place_endpoint_hashes(&endpoint_a, &endpoint_b),
            Err("prewarmPromotionEndpointMismatch")
        );
        assert_eq!(
            validate_in_place_endpoint_hashes(&Value::Null, &endpoint_b),
            Err("prewarmPromotionEndpointIdentityUnavailable")
        );
        assert_eq!(
            validate_in_place_endpoint_hashes(&endpoint_a, &Value::Null),
            Err("prewarmPromotionEndpointIdentityUnavailable")
        );
    }

    #[test]
    fn promoted_capture_payload_is_explicit_about_reused_wasapi_client() {
        let request = test_microphone_capture_request();
        let mut session = test_prewarm_session_with_stats(PrewarmStats::default());
        let payload = wasapi_promoted_capture_payload(
            "stream-test",
            r"\\.\pipe\scriber-audio-test",
            &request,
            &session,
            PrewarmPromotionAccepted {
                blocks: 4,
                audio_frames: 2_048,
                payload_bytes: 4_096,
            },
        );

        assert_eq!(payload["wasapiClientReused"], true);
        assert_eq!(payload["handoffMode"], IN_PLACE_PREWARM_HANDOFF_MODE);
        assert_eq!(payload["wasapiReadyDeferred"], false);
        assert_eq!(payload["adoptedPrewarm"]["adopted"], true);
        assert_eq!(payload["adoptedPrewarm"]["blocks"], 4);
        assert_eq!(
            payload["adoptedPrewarm"]["handoffMode"],
            IN_PLACE_PREWARM_HANDOFF_MODE
        );
        assert_eq!(
            payload["adoptedPrewarm"]["stop"]["reason"],
            "promotedInPlace"
        );
        let _ = session.stop("testCleanup");
    }

    #[test]
    fn prewarm_promotion_fallback_is_explicit_and_bounded() {
        let mut payload = json!({
            "wasapiClientReused": true,
            "handoffMode": IN_PLACE_PREWARM_HANDOFF_MODE,
        });
        let long_reason = "x".repeat(128);

        patch_wasapi_promotion_fallback(&mut payload, &long_reason);

        assert_eq!(payload["wasapiClientReused"], false);
        assert_eq!(payload["handoffMode"], "replacement-wasapi-client");
        assert_eq!(
            payload["prewarmPromotionFallbackReason"]
                .as_str()
                .map(str::len),
            Some(96)
        );
    }

    #[test]
    fn capture_stop_owns_and_cleans_promoted_prewarm_worker() {
        let promoted_capture = CaptureWriterStats {
            connected: true,
            frames_written: 8,
            prebuffer_frames_written: 3,
            live_frames_written: 5,
            bytes_written: 16_384,
            ..CaptureWriterStats::default()
        };
        let prewarm = test_prewarm_session_with_stats(PrewarmStats {
            promoted_capture: Some(promoted_capture),
            ..PrewarmStats::default()
        });
        let mut capture = CaptureSession {
            stream_id: "stream-promoted".to_string(),
            source: "wasapi-capture",
            worker: CaptureSessionWorker::PromotedPrewarm(Box::new(prewarm)),
            started_at: Instant::now(),
        };

        let stopped = capture.stop("responseAckRollback");

        assert_eq!(stopped["stopped"], true);
        assert_eq!(stopped["reason"], "responseAckRollback");
        assert_eq!(stopped["wasapiClientReused"], true);
        assert_eq!(stopped["handoffMode"], IN_PLACE_PREWARM_HANDOFF_MODE);
        assert_eq!(stopped["connected"], true);
        assert_eq!(stopped["prebufferFramesWritten"], 3);
        assert_eq!(stopped["liveFramesWritten"], 5);
        assert_eq!(stopped["writerError"], Value::Null);
        assert_eq!(stopped["workerStopTimedOut"], false);
        assert!(capture.worker_finished());
    }

    #[test]
    fn promoted_capture_stop_detaches_a_stalled_worker_within_the_bound() {
        let (control_tx, control_rx) = mpsc::channel();
        let join_handle = thread::spawn(move || {
            let _ = control_rx.recv();
            thread::sleep(Duration::from_millis(100));
            PrewarmStats::default()
        });
        let mut session = PrewarmSession {
            prewarm_id: "bounded-stop-test".to_string(),
            source: "wasapi-prewarm",
            request: test_microphone_capture_request(),
            control_tx,
            join_handle: Some(join_handle),
            started_at: Instant::now(),
            buffer: Arc::new(Mutex::new(PrewarmBuffer::new(8))),
            block_size: 512,
            native_endpoint_id_hash: Value::String("endpoint-a".to_string()),
            endpoint_selection: Value::Null,
            mix_format_payload: Value::Null,
            resampler_payload: Value::Null,
            microphone_channel_selection: Arc::new(Mutex::new(
                MicrophoneChannelSelectionState::default(),
            )),
        };
        let started = Instant::now();

        let joined = session.join_worker_bounded(Duration::from_millis(5));

        assert!(joined.timed_out);
        assert_eq!(
            joined.stats.error.as_deref(),
            Some("prewarmWorkerStopTimeout")
        );
        assert!(started.elapsed() < Duration::from_millis(50));
        assert!(session.join_handle.is_none());
    }

    #[test]
    fn bounded_deferred_handoff_joins_before_returning_the_complete_tail() {
        let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(8)));
        buffer.lock().unwrap().push(vec![0x11]).unwrap();
        let worker_buffer = Arc::clone(&buffer);
        let (control_tx, control_rx) = mpsc::channel();
        let join_handle = thread::spawn(move || {
            assert!(matches!(control_rx.recv(), Ok(PrewarmWorkerCommand::Stop)));
            worker_buffer.lock().unwrap().push(vec![0x22]).unwrap();
            PrewarmStats::default()
        });
        let mut session =
            test_prewarm_session_with_worker(Arc::clone(&buffer), control_tx, join_handle);
        let snapshot = session.begin_handoff().unwrap();

        let (stop, tail) = session
            .stop_and_finish_handoff_bounded("adoptedIntoCapture", Duration::from_millis(100));
        let tail = tail.unwrap();

        assert_eq!(stop["stopped"], true);
        assert_eq!(stop["workerStopTimedOut"], false);
        assert_eq!(stop["handoffTailBlocks"], 1);
        assert_eq!(
            snapshot
                .blocks
                .into_iter()
                .chain(tail)
                .map(|block| block[0])
                .collect::<Vec<_>>(),
            vec![0x11, 0x22]
        );
    }

    #[test]
    fn bounded_deferred_handoff_never_claims_a_tail_from_a_stalled_worker() {
        let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(8)));
        buffer.lock().unwrap().push(vec![0x11]).unwrap();
        let worker_buffer = Arc::clone(&buffer);
        let (control_tx, control_rx) = mpsc::channel();
        let join_handle = thread::spawn(move || {
            assert!(matches!(control_rx.recv(), Ok(PrewarmWorkerCommand::Stop)));
            thread::sleep(Duration::from_millis(60));
            worker_buffer.lock().unwrap().push(vec![0x33]).unwrap();
            PrewarmStats::default()
        });
        let mut session =
            test_prewarm_session_with_worker(Arc::clone(&buffer), control_tx, join_handle);
        let snapshot = session.begin_handoff().unwrap();
        let started = Instant::now();

        let (stop, tail) =
            session.stop_and_finish_handoff_bounded("adoptedIntoCapture", Duration::from_millis(5));

        assert!(started.elapsed() < Duration::from_millis(50));
        assert_eq!(stop["stopped"], false);
        assert_eq!(stop["workerStopTimedOut"], true);
        assert_eq!(tail.unwrap_err(), "prewarmWorkerStopTimeout");
        assert_eq!(snapshot.blocks, vec![vec![0x11]]);

        // The detached worker still owns the handoff marker. Once it exits its
        // late block remains in the tail; the timed-out call did not falsely
        // finalize or replay an incomplete prefix.
        thread::sleep(Duration::from_millis(80));
        assert_eq!(
            buffer.lock().unwrap().finish_handoff().unwrap(),
            vec![vec![0x33]]
        );
    }

    #[test]
    fn prewarm_handoff_tail_is_bounded_and_overflow_fails_closed() {
        let mut buffer = PrewarmBuffer::new(2);
        buffer.push(vec![0x11]).unwrap();
        let snapshot = buffer.begin_handoff_with_limit(Some(2)).unwrap();
        assert_eq!(snapshot, vec![vec![0x11]]);
        buffer.push(vec![0x22, 0x23]).unwrap();

        assert_eq!(buffer.push(vec![0x33]), Err("prewarmHandoffTailOverflow"));
        assert_eq!(buffer.block_count(), 1);
        assert_eq!(buffer.payload_bytes(), 2);
        assert_eq!(buffer.finish_handoff(), Err("prewarmHandoffTailOverflow"));
        assert_eq!(buffer.block_count(), 0);
        assert_eq!(buffer.payload_bytes(), 0);
    }

    #[test]
    fn aborting_prewarm_handoff_releases_tail_without_replaying_it() {
        let mut buffer = PrewarmBuffer::new(4);
        buffer.push(vec![0x11]).unwrap();
        let snapshot = buffer.begin_handoff().unwrap();
        buffer.push(vec![0x22, 0x23]).unwrap();

        assert_eq!(buffer.abort_handoff(), (1, 2));
        assert_eq!(snapshot, vec![vec![0x11]]);
        assert_eq!(buffer.block_count(), 0);
        assert_eq!(buffer.payload_bytes(), 0);
        assert_eq!(buffer.finish_handoff(), Err("prewarmHandoffNotStarted"));
    }

    #[test]
    fn canceled_prewarm_session_stops_worker_and_discards_only_uncommitted_tail() {
        let buffer = Arc::new(Mutex::new(PrewarmBuffer::new(4)));
        buffer.lock().unwrap().push(vec![0x11]).unwrap();
        let (stop_tx, stop_rx) = mpsc::channel();
        let join_handle = thread::spawn(move || {
            let _ = stop_rx.recv();
            PrewarmStats::default()
        });
        let mut session = PrewarmSession {
            prewarm_id: "cancel-test".to_string(),
            source: "test-prewarm",
            request: CaptureRequest {
                block_size: 1,
                ..test_microphone_capture_request()
            },
            control_tx: stop_tx,
            join_handle: Some(join_handle),
            started_at: Instant::now(),
            buffer: Arc::clone(&buffer),
            block_size: 1,
            native_endpoint_id_hash: Value::Null,
            endpoint_selection: Value::Null,
            mix_format_payload: Value::Null,
            resampler_payload: Value::Null,
            microphone_channel_selection: Arc::new(Mutex::new(
                MicrophoneChannelSelectionState::default(),
            )),
        };

        let adopted = session.begin_handoff().unwrap();
        buffer.lock().unwrap().push(vec![0x22, 0x23]).unwrap();
        let stopped = session.stop("captureStartFailed");

        assert_eq!(adopted.blocks, vec![vec![0x11]]);
        assert_eq!(stopped["reason"], "captureStartFailed");
        assert_eq!(stopped["discardedHandoffBlocks"], 1);
        assert_eq!(stopped["discardedHandoffPayloadBytes"], 2);
        assert_eq!(buffer.lock().unwrap().block_count(), 0);
    }

    #[test]
    fn sidecar_self_test_reports_protocol_and_frame_contract() {
        let _lock = audio_env_test_lock();
        let _enable_default_wasapi = set_audio_test_env(DISABLE_WASAPI_CAPTURE_ENV, "0");
        let _disable_synthetic = set_audio_test_env(SYNTHETIC_CAPTURE_ENV, "0");
        let payload = self_test_payload();

        assert_eq!(payload["sidecar"], SIDECAR_NAME);
        assert_eq!(payload["ok"], true);
        assert_eq!(payload["protocolVersion"], SIDECAR_PROTOCOL_VERSION);
        assert_eq!(payload["capabilities"]["captureAvailable"], true);
        assert_eq!(payload["capabilities"]["prewarmAvailable"], true);
        assert_eq!(
            payload["capabilities"]["syntheticFramePipeEnv"],
            SYNTHETIC_CAPTURE_ENV
        );
        assert_eq!(
            payload["capabilities"]["syntheticSignalEnv"],
            SYNTHETIC_SIGNAL_ENV
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

    #[cfg(windows)]
    #[test]
    fn synthetic_meeting_capture_relays_raw_system_and_aec_clean_frames() {
        let _lock = audio_env_test_lock();
        let _disable_wasapi = set_audio_test_env(DISABLE_WASAPI_CAPTURE_ENV, "1");
        let _enable_synthetic = set_audio_test_env(SYNTHETIC_CAPTURE_ENV, "1");
        let _enable_signal = set_audio_test_env(SYNTHETIC_SIGNAL_ENV, "1");
        let mut state = AudioSidecarState::new();
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "meeting-start",
            "command": "meetingCaptureStart",
            "payload": {"aecEnabled": true, "aecDelayMs": 80}
        });
        let response = state.handle_sidecar_request(&request.to_string());
        assert_eq!(response["success"], true, "{response}");
        assert_eq!(response["payload"]["aecActive"], true);
        let sources = response["payload"]["sources"].as_array().unwrap();
        assert_eq!(sources.len(), 3);
        let readers: Vec<_> = sources
            .iter()
            .map(|source| {
                let path = source["framePipe"].as_str().unwrap().to_string();
                thread::spawn(move || {
                    let mut file = loop {
                        match std::fs::File::open(&path) {
                            Ok(file) => break file,
                            Err(_) => thread::sleep(Duration::from_millis(5)),
                        }
                    };
                    let mut header = [0u8; AUDIO_FRAME_HEADER_LEN];
                    file.read_exact(&mut header).unwrap();
                    let decoded = AudioFrameHeader::decode(&header).unwrap();
                    let mut payload = vec![0u8; decoded.payload_len as usize];
                    file.read_exact(&mut payload).unwrap();
                    let peak = payload
                        .chunks_exact(2)
                        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]).unsigned_abs())
                        .max()
                        .unwrap_or(0);
                    (decoded, payload, peak)
                })
            })
            .collect();
        let mut received = Vec::new();
        for reader in readers {
            let (header, payload, peak) = reader.join().unwrap();
            assert_eq!(header.frame_count, 160);
            assert_eq!(payload.len(), 320);
            assert!(peak > 0, "synthetic Meeting source must carry a signal");
            received.push(header);
        }
        assert!(received.windows(2).all(|pair| {
            pair[0].sequence == pair[1].sequence
                && pair[0].timestamp_micros == pair[1].timestamp_micros
        }));
        let capture_id = response["payload"]["meetingCaptureId"].as_str().unwrap();
        let stop = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "meeting-stop",
            "command": "meetingCaptureStop",
            "payload": {"meetingCaptureId": capture_id}
        });
        let stopped = state.handle_sidecar_request(&stop.to_string());
        assert_eq!(stopped["success"], true);
        assert_eq!(stopped["payload"]["stopped"], true);
        assert!(stopped["payload"]["relay"]["aecMetrics"].is_object());
        assert_eq!(
            stopped["payload"]["relay"]["aecMetrics"]["measurement"],
            "render-active-raw-to-clean-energy-ratio"
        );
    }

    #[test]
    fn meeting_relay_stats_report_measurable_render_active_attenuation() {
        let stats = MeetingRelayStats {
            frames_processed: 100,
            bytes_forwarded: 1_000,
            aec_render_active_frames: 25,
            aec_render_energy: 50_000.0,
            aec_raw_mic_energy: 10_000.0,
            aec_clean_mic_energy: 1_000.0,
            error: None,
            ..Default::default()
        };
        let metrics = stats.aec_metrics(true);
        assert_eq!(metrics["renderActiveFrames"], 25);
        assert_eq!(metrics["renderActiveDurationMs"], 250);
        assert_eq!(metrics["echoReductionDb"], 10.0);
    }

    #[test]
    fn meeting_alignment_preserves_large_source_start_skew() {
        assert_eq!(
            meeting_alignment_action(Some(0), Some(250_000)),
            Some(MeetingAlignmentAction::MicrophoneOnly)
        );
        assert_eq!(
            meeting_alignment_action(Some(250_000), Some(0)),
            Some(MeetingAlignmentAction::SystemOnly)
        );
        assert_eq!(
            meeting_alignment_action(Some(250_000), Some(254_000)),
            Some(MeetingAlignmentAction::Pair)
        );
    }

    #[cfg(windows)]
    #[test]
    fn meeting_relay_pcm_scratch_buffers_keep_their_allocations() {
        let source: Vec<i16> = (0..MEETING_AEC_FRAME_SAMPLES)
            .map(|index| (index as i16).wrapping_mul(31))
            .collect();
        let mut source_payload = Vec::with_capacity(source.len() * 2);
        pcm_bytes_into(&source, &mut source_payload);

        let mut decoded = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
        let mut downsampled = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES / 3);
        let mut payload = Vec::with_capacity((MEETING_AEC_FRAME_SAMPLES / 3) * 2);
        pcm_i16_into(&source_payload, &mut decoded).expect("initial PCM decode");
        downsample_meeting_48k_to_16k_into(&decoded, &mut downsampled).expect("initial downsample");
        pcm_bytes_into(&downsampled, &mut payload);
        let allocations = (decoded.as_ptr(), downsampled.as_ptr(), payload.as_ptr());

        for _ in 0..10_000 {
            pcm_i16_into(&source_payload, &mut decoded).expect("reused PCM decode");
            downsample_meeting_48k_to_16k_into(&decoded, &mut downsampled)
                .expect("reused downsample");
            pcm_bytes_into(&downsampled, &mut payload);
            assert_eq!(decoded.as_ptr(), allocations.0);
            assert_eq!(downsampled.as_ptr(), allocations.1);
            assert_eq!(payload.as_ptr(), allocations.2);
        }
        assert_eq!(decoded, source);
        assert_eq!(downsampled.len(), MEETING_AEC_FRAME_SAMPLES / 3);
        assert_eq!(payload.len(), downsampled.len() * 2);
    }

    #[cfg(windows)]
    #[test]
    fn wasapi_loopback_initialize_changes_flags_not_periodicity() {
        let microphone = wasapi_shared_initialize_args("microphone");
        let loopback = wasapi_shared_initialize_args("loopback");
        assert_eq!(microphone.periodicity_100ns, 0);
        assert_eq!(loopback.periodicity_100ns, 0);
        assert_eq!(
            microphone.buffer_duration_100ns,
            loopback.buffer_duration_100ns
        );
        assert_eq!(microphone.stream_flags, 0);
        assert_eq!(loopback.stream_flags, AUDCLNT_STREAMFLAGS_LOOPBACK);
    }

    #[cfg(windows)]
    #[test]
    fn frame_pipe_connect_wait_has_a_hard_deadline() {
        let pipe_path = format!(
            r"\\.\pipe\scriber-audio-connect-timeout-{}",
            Uuid::new_v4().simple()
        );
        let handle = create_frame_pipe(&pipe_path).expect("test frame pipe should be created");
        let (_stop_tx, stop_rx) = mpsc::channel::<()>();
        let started = Instant::now();

        let error = wait_for_pipe_client_with_timeout(
            handle,
            &stop_rx,
            Duration::from_millis(40),
            FramePipePostConnectMode::PreserveNonBlocking,
        )
        .expect_err("a frame pipe without a client must time out");

        unsafe {
            CloseHandle(handle);
        }
        assert!(error.contains("did not connect within 40 ms"));
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[cfg(windows)]
    fn connect_test_frame_pipe(post_connect_mode: FramePipePostConnectMode) {
        use std::{fs::OpenOptions, sync::mpsc, thread};

        let pipe_path = format!(
            r"\\.\pipe\scriber-audio-connect-mode-{}",
            Uuid::new_v4().simple()
        );
        let handle = create_frame_pipe(&pipe_path).expect("test frame pipe should be created");
        let client_path = pipe_path.clone();
        let (connected_tx, connected_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        let client = thread::spawn(move || {
            let file = OpenOptions::new()
                .read(true)
                .open(client_path)
                .expect("test client should connect");
            connected_tx.send(()).unwrap();
            let _ = release_rx.recv_timeout(Duration::from_secs(1));
            drop(file);
        });
        let (_stop_tx, stop_rx) = mpsc::channel::<()>();

        wait_for_pipe_client_with_timeout(
            handle,
            &stop_rx,
            Duration::from_secs(1),
            post_connect_mode,
        )
        .expect("connected frame pipe should finish in its requested mode");
        connected_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("client should report its connection");

        let _ = release_tx.send(());
        client.join().unwrap();
        unsafe {
            DisconnectNamedPipe(handle);
            CloseHandle(handle);
        }
    }

    #[cfg(windows)]
    #[test]
    fn live_frame_pipe_preserves_nonblocking_writes_after_connect() {
        let mode = FramePipePostConnectMode::PreserveNonBlocking;
        assert_eq!(frame_pipe_wait_mode_update(mode), None);
        connect_test_frame_pipe(mode);
    }

    #[cfg(windows)]
    #[test]
    fn meeting_output_pipe_switches_to_blocking_mode_after_connect() {
        let mode = FramePipePostConnectMode::SwitchToBlocking;
        assert_eq!(frame_pipe_wait_mode_update(mode), Some(PIPE_WAIT));
        connect_test_frame_pipe(mode);
    }

    #[test]
    fn loopback_silence_deadline_keeps_ten_ms_cadence() {
        let start = Instant::now();
        let interval = Duration::from_millis(10);
        assert_eq!(
            advance_loopback_frame_deadline(start + interval, start + interval, interval),
            start + Duration::from_millis(20)
        );
    }

    #[test]
    fn loopback_silence_deadline_drops_large_catch_up_bursts() {
        let start = Instant::now();
        let interval = Duration::from_millis(10);
        let now = start + Duration::from_millis(100);
        assert_eq!(
            advance_loopback_frame_deadline(start + interval, now, interval),
            now + interval
        );
    }

    #[test]
    fn sidecar_capture_start_returns_explicit_unavailable_payload() {
        let _lock = audio_env_test_lock();
        let _disable_wasapi = set_audio_test_env(DISABLE_WASAPI_CAPTURE_ENV, "1");
        let _disable_synthetic = set_audio_test_env(SYNTHETIC_CAPTURE_ENV, "0");
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
        assert_eq!(response["payload"]["requestedFormat"]["prebufferMs"], 6_000);
        assert_eq!(response["payload"]["audioFrameProtocol"]["version"], 1);
        assert_eq!(
            response["payload"]["requestedFormat"]["nativeEndpointIdHash"],
            ""
        );
    }

    #[test]
    fn sidecar_prewarm_start_returns_explicit_unavailable_payload() {
        let _lock = audio_env_test_lock();
        let _disable_wasapi = set_audio_test_env(DISABLE_WASAPI_CAPTURE_ENV, "1");
        let _disable_synthetic = set_audio_test_env(SYNTHETIC_CAPTURE_ENV, "0");
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
        assert_eq!(response["payload"]["requestedFormat"]["prebufferMs"], 6_000);
        assert_eq!(response["payload"]["prewarmAvailable"], false);
        assert_eq!(response["payload"]["wasapiPrewarmAvailable"], false);
    }

    #[test]
    fn sidecar_prewarm_status_without_active_session_is_successful() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r-prewarm-status",
            "command": "prewarmStatus",
            "payload": {
                "prewarmId": "missing-prewarm"
            }
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["requestId"], "r-prewarm-status");
        assert_eq!(response["success"], true);
        assert_eq!(response["payload"]["active"], false);
        assert_eq!(response["payload"]["prewarmId"], "missing-prewarm");
        assert_eq!(response["payload"]["reason"], "noActivePrewarm");
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
    fn deferred_prewarm_stop_reports_adopted_handoff_reason() {
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 16,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 4,
            prewarm_id: "".to_string(),
            capture_kind: "microphone".to_string(),
            clock_origin: None,
        };

        let (session, _) = start_synthetic_prewarm_impl(request).unwrap();
        let mut deferred_session = Some(session);
        let stop = stop_deferred_prewarm_session(&mut deferred_session, "adoptedIntoCapture")
            .expect("deferred prewarm stop payload");

        assert!(deferred_session.is_none());
        assert_eq!(stop["stopped"], true);
        assert_eq!(stop["reason"], "adoptedIntoCapture");
        assert_eq!(stop["source"], "synthetic-prewarm");
        assert_eq!(stop["workerStopTimedOut"], false);
        assert!(stop["totalBlocksObserved"].as_u64().unwrap_or_default() > 0);
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
        assert_eq!(request.prebuffer_ms, 6_000);
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
    fn default_prewarm_handoff_rejects_endpoint_a_to_b_switch() {
        let prewarm_endpoint = Value::String("default-endpoint-a".to_string());
        let replacement_endpoint = Value::String("default-endpoint-b".to_string());

        assert_eq!(
            validate_prewarm_endpoint_continuity(&prewarm_endpoint, &replacement_endpoint),
            Err("prewarmEndpointChangedDuringHandoff".to_string())
        );
    }

    #[test]
    fn default_prewarm_handoff_accepts_same_opened_endpoint() {
        let prewarm_endpoint = Value::String("default-endpoint-a".to_string());
        let replacement_endpoint = Value::String("default-endpoint-a".to_string());

        assert_eq!(
            validate_prewarm_endpoint_continuity(&prewarm_endpoint, &replacement_endpoint),
            Ok(())
        );
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
    fn synthetic_pcm_fixture_plays_once_then_becomes_silence() {
        let request = CaptureRequest {
            sample_rate: 48_000,
            channels: 1,
            block_size: 4,
            device_preference: "default".to_string(),
            port_audio_label: String::new(),
            native_endpoint_id_hash: String::new(),
            prebuffer_ms: 0,
            prewarm_id: String::new(),
            capture_kind: "microphone".to_string(),
            clock_origin: None,
        };
        let fixture: Vec<u8> = (1_u8..=12).collect();

        assert_eq!(
            synthetic_frame_payload(&request, 0, Some(&fixture)),
            fixture[..8]
        );
        assert_eq!(
            synthetic_frame_payload(&request, 1, Some(&fixture)),
            vec![9, 10, 11, 12, 0, 0, 0, 0]
        );
        assert_eq!(
            synthetic_frame_payload(&request, 2, Some(&fixture)),
            vec![0; 8]
        );
    }

    #[test]
    fn synthetic_pcm_fixture_length_validation_is_bounded_and_sample_aligned() {
        assert!(validate_synthetic_pcm_fixture_len(2).is_ok());
        assert!(validate_synthetic_pcm_fixture_len(0)
            .unwrap_err()
            .contains("must not be empty"));
        assert!(validate_synthetic_pcm_fixture_len(3)
            .unwrap_err()
            .contains("signed 16-bit PCM"));
        assert!(
            validate_synthetic_pcm_fixture_len(SYNTHETIC_PCM_MAX_BYTES + 2)
                .unwrap_err()
                .contains("bounded synthetic PCM size")
        );
    }

    #[test]
    fn synthetic_pcm_fixture_loader_is_microphone_only_and_redacts_its_path() {
        let _lock = audio_env_test_lock();
        let path = env::temp_dir().join(format!(
            "scriber-synthetic-mic-{}.s16le",
            Uuid::new_v4().simple()
        ));
        std::fs::write(&path, [1_u8, 0, 2, 0]).unwrap();
        let path_text = path.to_string_lossy().to_string();
        let _fixture = set_audio_test_env(SYNTHETIC_MIC_PCM_ENV, &path_text);
        let mut request = test_microphone_capture_request();
        request.sample_rate = 48_000;
        request.channels = 1;
        request.block_size = 480;

        let microphone = load_synthetic_pcm_fixture(&request).unwrap();
        assert_eq!(microphone, Some(vec![1, 0, 2, 0]));

        request.capture_kind = "loopback".to_string();
        assert_eq!(load_synthetic_pcm_fixture(&request).unwrap(), None);

        request.capture_kind = "microphone".to_string();
        request.sample_rate = 16_000;
        let error = load_synthetic_pcm_fixture(&request).unwrap_err();
        assert!(error.contains("requires 48000 Hz mono capture"));
        assert!(!error.contains(&path_text));

        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn synthetic_pcm_fixture_loader_rejects_relative_paths_without_disclosure() {
        let _lock = audio_env_test_lock();
        let private_relative_path = "private-fixtures\\meeting-microphone.s16le";
        let _fixture = set_audio_test_env(SYNTHETIC_MIC_PCM_ENV, private_relative_path);
        let mut request = test_microphone_capture_request();
        request.sample_rate = 48_000;
        request.channels = 1;

        let error = load_synthetic_pcm_fixture(&request).unwrap_err();

        assert!(error.contains("must be an absolute path"));
        assert!(!error.contains(private_relative_path));
    }

    #[test]
    fn wasapi_capture_is_enabled_by_default_unless_disabled() {
        let _lock = audio_env_test_lock();
        let _enable_default_wasapi = set_audio_test_env(DISABLE_WASAPI_CAPTURE_ENV, "0");
        let _disable_synthetic = set_audio_test_env(SYNTHETIC_CAPTURE_ENV, "0");
        assert!(!env_flag_enabled(Some("off")));
        assert!(env_flag_enabled(Some("true")));
        assert!(env_flag_enabled(Some("on")));
        assert!(wasapi_capture_enabled());
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
    fn stereo_float_mix_format(sample_rate: u32) -> WasapiMixFormat {
        WasapiMixFormat {
            format_tag: WAVE_FORMAT_IEEE_FLOAT_TAG,
            channels: 2,
            sample_rate,
            average_bytes_per_second: sample_rate * 8,
            block_align: 8,
            bits_per_sample: 32,
            extra_size: 0,
            sample_format: WasapiSampleFormat::Float32,
        }
    }

    #[cfg(windows)]
    fn mono_capture_request(capture_kind: &str, block_size: u32) -> CaptureRequest {
        CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 0,
            prewarm_id: "".to_string(),
            capture_kind: capture_kind.to_string(),
            clock_origin: None,
        }
    }

    #[cfg(windows)]
    fn stereo_float_bytes(frames: &[[f32; 2]]) -> Vec<u8> {
        frames
            .iter()
            .flat_map(|frame| frame.iter().flat_map(|sample| sample.to_le_bytes()))
            .collect()
    }

    #[cfg(windows)]
    fn pcm_i16_block_samples(block: &[u8]) -> Vec<i16> {
        block
            .chunks_exact(2)
            .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
            .collect()
    }

    #[cfg(windows)]
    #[test]
    fn microphone_converter_preserves_antiphase_array_speech() {
        let request = mono_capture_request("microphone", 4);
        let frames = [[0.75, -0.75]; 4];
        let bytes = stereo_float_bytes(&frames);
        let mut converter = WasapiPcmConverter::new(stereo_float_mix_format(16_000), &request);

        let blocks = converter
            .push_packet(bytes.as_ptr(), frames.len() as u32, false)
            .unwrap();
        let samples = pcm_i16_block_samples(&blocks[0]);

        assert!(samples.iter().all(|sample| sample.unsigned_abs() > 20_000));
    }

    #[cfg(windows)]
    #[test]
    fn microphone_converter_selects_active_channel_beside_silent_channel() {
        let request = mono_capture_request("microphone", 4);
        let frames = [[0.0, 0.5]; 4];
        let bytes = stereo_float_bytes(&frames);
        let selection = Arc::new(Mutex::new(MicrophoneChannelSelectionState::default()));
        let mut converter = WasapiPcmConverter::with_channel_selection(
            stereo_float_mix_format(16_000),
            &request,
            Arc::clone(&selection),
        );

        let blocks = converter
            .push_packet(bytes.as_ptr(), frames.len() as u32, false)
            .unwrap();
        let samples = pcm_i16_block_samples(&blocks[0]);

        assert!(samples.iter().all(|sample| *sample > 15_000));
        assert_eq!(selection.lock().unwrap().selected_channel, Some(1));
    }

    #[cfg(windows)]
    #[test]
    fn loopback_converter_keeps_average_mix_for_antiphase_channels() {
        let request = mono_capture_request("loopback", 4);
        let frames = [[0.75, -0.75]; 4];
        let bytes = stereo_float_bytes(&frames);
        let mut converter = WasapiPcmConverter::new(stereo_float_mix_format(16_000), &request);

        let blocks = converter
            .push_packet(bytes.as_ptr(), frames.len() as u32, false)
            .unwrap();
        let samples = pcm_i16_block_samples(&blocks[0]);

        assert!(samples.iter().all(|sample| sample.unsigned_abs() <= 1));
    }

    #[cfg(windows)]
    #[test]
    fn microphone_channel_selection_survives_prewarm_handoff_with_hysteresis() {
        let request = mono_capture_request("microphone", 4);
        let selection = Arc::new(Mutex::new(MicrophoneChannelSelectionState::default()));
        let prewarm_frames = [[0.2, 0.6]; 4];
        let prewarm_bytes = stereo_float_bytes(&prewarm_frames);
        let mut prewarm_converter = WasapiPcmConverter::with_channel_selection(
            stereo_float_mix_format(16_000),
            &request,
            Arc::clone(&selection),
        );
        prewarm_converter
            .push_packet(prewarm_bytes.as_ptr(), prewarm_frames.len() as u32, false)
            .unwrap();
        assert_eq!(selection.lock().unwrap().selected_channel, Some(1));

        let live_frames = [[0.65, 0.6]; 4];
        let live_bytes = stereo_float_bytes(&live_frames);
        let mut live_converter = WasapiPcmConverter::with_channel_selection(
            stereo_float_mix_format(16_000),
            &request,
            Arc::clone(&selection),
        );
        let blocks = live_converter
            .push_packet(live_bytes.as_ptr(), live_frames.len() as u32, false)
            .unwrap();
        let samples = pcm_i16_block_samples(&blocks[0]);
        assert!(samples
            .iter()
            .all(|sample| (19_000..20_500).contains(sample)));
        assert_eq!(selection.lock().unwrap().selected_channel, Some(1));

        let stronger_frames = [[0.9, 0.4]; 4];
        let stronger_bytes = stereo_float_bytes(&stronger_frames);
        live_converter
            .push_packet(stronger_bytes.as_ptr(), stronger_frames.len() as u32, false)
            .unwrap();
        assert_eq!(selection.lock().unwrap().selected_channel, Some(0));
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
        };
        let (mut prewarm_session, _) = start_synthetic_prewarm_impl(prewarm_request).unwrap();
        let adopted = prewarm_session.begin_handoff().unwrap();
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
            capture_kind: "microphone".to_string(),
            clock_origin: None,
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
    #[test]
    fn wasapi_capture_payload_can_defer_ready_for_adopted_prewarm() {
        let request = CaptureRequest {
            sample_rate: 16_000,
            channels: 1,
            block_size: 16,
            device_preference: "default".to_string(),
            port_audio_label: "".to_string(),
            native_endpoint_id_hash: "".to_string(),
            prebuffer_ms: 400,
            prewarm_id: "prewarm-1".to_string(),
            capture_kind: "microphone".to_string(),
            clock_origin: None,
        };
        let adopted = AdoptedPrewarm {
            prewarm_id: "prewarm-1".to_string(),
            source: "wasapi-prewarm",
            block_count: 1,
            audio_frame_count: 16,
            payload_bytes: 32,
            blocks: vec![vec![0; 32]],
            native_endpoint_id_hash: Value::String("prewarm-endpoint-hash".to_string()),
            endpoint_selection: json!({
                "mode": "default",
                "selectedNativeEndpointIdHash": "prewarm-endpoint-hash",
                "usedDefaultEndpoint": true,
            }),
            mix_format_payload: json!({
                "sampleRate": 48_000,
                "channels": 2,
            }),
            resampler_payload: json!({
                "sourceSampleRate": 48_000,
                "targetSampleRate": 16_000,
                "sourceChannels": 2,
                "targetChannels": 1,
                "method": "nearest",
            }),
            microphone_channel_selection: Arc::new(Mutex::new(
                MicrophoneChannelSelectionState::default(),
            )),
        };
        let stop_payload = json!({
            "stopped": false,
            "reason": "pendingCaptureReady",
        });

        let payload = wasapi_capture_payload(
            "stream-1",
            r"\\.\pipe\scriber-audio-test",
            &request,
            Some(&(adopted, stop_payload)),
            None,
            true,
        );

        assert_eq!(payload["captureAvailable"], true);
        assert_eq!(payload["wasapiCapture"], true);
        assert_eq!(payload["wasapiReadyDeferred"], true);
        assert_eq!(payload["firstFramesFromAdoptedPrewarm"], true);
        assert_eq!(payload["wasapiClientReused"], false);
        assert_eq!(
            payload["handoffMode"],
            "overlap-capture-start-before-prewarm-stop"
        );
        assert_eq!(payload["nativeEndpointIdHash"], "prewarm-endpoint-hash");
        assert_eq!(payload["adoptedPrewarm"]["adopted"], true);
        assert_eq!(payload["adoptedPrewarm"]["blocks"], 1);
        assert_eq!(payload["endpointSelection"]["mode"], "default");
        assert_eq!(payload["mixFormat"]["sampleRate"], 48_000);
        assert_eq!(payload["resampler"]["sourceSampleRate"], 48_000);
    }

    #[test]
    fn adopted_prewarm_stop_payload_is_patched_after_overlap_start() {
        let mut payload = serde_json::json!({
            "adoptedPrewarm": {
                "adopted": true,
                "blocks": 12,
                "stop": {
                    "stopped": false,
                    "reason": "pendingCaptureReady"
                }
            }
        });

        patch_adopted_prewarm_stop_payload(
            &mut payload,
            serde_json::json!({
                "stopped": true,
                "reason": "adoptedIntoCapture",
                "totalBlocksObserved": 155
            }),
            "overlap-capture-start-before-prewarm-stop",
        );

        assert_eq!(payload["adoptedPrewarm"]["adopted"], true);
        assert_eq!(payload["adoptedPrewarm"]["blocks"], 12);
        assert_eq!(payload["adoptedPrewarm"]["stop"]["stopped"], true);
        assert_eq!(
            payload["adoptedPrewarm"]["stop"]["reason"],
            "adoptedIntoCapture"
        );
        assert_eq!(
            payload["adoptedPrewarm"]["stop"]["totalBlocksObserved"],
            155
        );
        assert_eq!(
            payload["adoptedPrewarm"]["handoffMode"],
            "overlap-capture-start-before-prewarm-stop"
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
