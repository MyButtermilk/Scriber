use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use uuid::Uuid;

use crate::audio_devices::{
    collect_native_capture_endpoint_inventory, native_device_event_status_payload,
    run_passive_audio_probe, PassiveAudioProbeOptions,
};
use crate::audio_frame_pipe::{AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION};
use crate::audio_sidecar_client::{audio_sidecar_executable_available, call_audio_sidecar_command};

const API_VERSION: &str = "1";
const MAX_REQUEST_BYTES: usize = 512 * 1024;
const PIPE_BUFFER_BYTES: u32 = 64 * 1024;
const SHELL_IPC_PIPE_SECURITY_FALLBACK_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)";
const SE_GROUP_LOGON_ID_MASK: u32 = 0xC000_0000;
const MAX_INJECT_TEXT_BYTES: usize = 384 * 1024;
const DEFAULT_CLIPBOARD_RETRIES: u32 = 5;
const DEFAULT_CLIPBOARD_RETRY_DELAY_MS: u64 = 5;
const DEFAULT_RESTORE_DELAY_MS: u64 = 1500;
const DEFAULT_INJECT_DEADLINE_MS: u64 = 2_000;
const CLIENT_READ_TIMEOUT_MS: u64 = 750;

#[derive(Debug, Clone)]
struct InjectTextOptions {
    text: String,
    restore_clipboard: bool,
    restore_delay_ms: u64,
    pre_delay_ms: u64,
    pre_delay_mode: String,
    dispatch: String,
    max_clipboard_retries: u32,
    clipboard_retry_delay_ms: u64,
    deadline_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct AudioCaptureStartOptions {
    sample_rate: u32,
    channels: u16,
    block_size: u32,
    device_preference: String,
    port_audio_label: String,
    native_endpoint_id_hash: String,
    prebuffer_ms: u32,
    prewarm_id: String,
}

#[derive(Debug)]
struct ShellCommandError {
    code: &'static str,
    reason: String,
    payload: Value,
}

impl ShellCommandError {
    fn new(code: &'static str, reason: impl Into<String>) -> Self {
        Self {
            code,
            reason: reason.into(),
            payload: json!({}),
        }
    }

    fn with_payload(mut self, payload: Value) -> Self {
        self.payload = payload;
        self
    }
}

#[derive(Debug, Clone)]
pub struct ShellIpcConfig {
    pub pipe_name: String,
    pub token: String,
}

impl ShellIpcConfig {
    pub fn new() -> Self {
        let id = Uuid::new_v4().simple().to_string();
        Self {
            pipe_name: format!(r"\\.\pipe\scriber-shell-{id}"),
            token: Uuid::new_v4().simple().to_string(),
        }
    }

    pub fn pipe_name_hash(&self) -> String {
        hash_sensitive_identifier(&self.pipe_name)
    }
}

pub struct ShellIpcServerHandle {
    pipe_name: String,
    stop: Arc<AtomicBool>,
    join_handle: Option<JoinHandle<()>>,
}

impl Drop for ShellIpcServerHandle {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        wake_pipe_server(&self.pipe_name);
        let _ = self.join_handle.take();
    }
}

pub fn start_shell_ipc_server<L>(
    config: ShellIpcConfig,
    mut log: L,
) -> Result<Option<ShellIpcServerHandle>, String>
where
    L: FnMut(String) + Send + 'static,
{
    start_shell_ipc_server_impl(config, move |message| log(message))
}

#[cfg(windows)]
fn start_shell_ipc_server_impl<L>(
    config: ShellIpcConfig,
    mut log: L,
) -> Result<Option<ShellIpcServerHandle>, String>
where
    L: FnMut(String) + Send + 'static,
{
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_thread = Arc::clone(&stop);
    let pipe_name = config.pipe_name.clone();
    let pipe_name_for_thread = pipe_name.clone();
    let join_handle = thread::Builder::new()
        .name("shell-ipc".to_string())
        .spawn(move || run_shell_ipc_server(config, stop_for_thread, &mut log))
        .map_err(|err| format!("could not spawn shell IPC thread: {err}"))?;

    Ok(Some(ShellIpcServerHandle {
        pipe_name: pipe_name_for_thread,
        stop,
        join_handle: Some(join_handle),
    }))
}

#[cfg(not(windows))]
fn start_shell_ipc_server_impl<L>(
    _config: ShellIpcConfig,
    mut log: L,
) -> Result<Option<ShellIpcServerHandle>, String>
where
    L: FnMut(String) + Send + 'static,
{
    log("shell IPC unavailable on this platform".to_string());
    Ok(None)
}

fn handle_shell_ipc_request(raw: &str, expected_token: &str) -> String {
    let started = Instant::now();
    let parsed = serde_json::from_str::<Value>(raw);
    let request = match parsed {
        Ok(Value::Object(map)) => map,
        Ok(_) => {
            return response_line(
                "",
                false,
                "invalidRequest",
                "request must be an object",
                started,
                json!({}),
            )
        }
        Err(_) => {
            return response_line(
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
    let api_version = request
        .get("apiVersion")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if api_version != API_VERSION {
        return response_line(
            request_id,
            false,
            "apiVersionMismatch",
            "unsupported apiVersion",
            started,
            json!({}),
        );
    }

    let provided_token = request
        .get("token")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if provided_token != expected_token {
        return response_line(
            request_id,
            false,
            "unauthorized",
            "invalid shell IPC token",
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
        "ping" => response_line(
            request_id,
            true,
            "",
            "",
            started,
            json!({
                "pong": true,
                "server": "tauri",
            }),
        ),
        "capabilities" => response_line(
            request_id,
            true,
            "",
            "",
            started,
            json!({
                "commands": [
                    "ping",
                    "capabilities",
                    "injectText",
                    "nativeDeviceEventsStatus",
                    "audioEndpointInventory",
                    "audioProbe",
                    "audioCaptureStart",
                    "audioCaptureStop",
                    "audioPrewarmStart",
                    "audioPrewarmStop",
                ],
                "textInjection": true,
                "nativeDeviceEventsStatus": true,
                "audioEndpointInventory": true,
                "audioProbe": true,
                "audioCapturePrototype": false,
                "audioPrewarmPrototype": false,
                "audioSidecar": {
                    "executableAvailable": audio_sidecar_executable_available(),
                    "stdioProtocolVersion": "1",
                },
                "audioFrameProtocol": audio_frame_protocol_payload(),
            }),
        ),
        "injectText" => match inject_text(payload) {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "nativeDeviceEventsStatus" => response_line(
            request_id,
            true,
            "",
            "",
            started,
            native_device_event_status_payload(),
        ),
        "audioEndpointInventory" => match collect_native_capture_endpoint_inventory()
            .map_err(|err| ShellCommandError::new("audioEndpointInventoryFailed", err))
        {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "audioProbe" => match parse_audio_probe_options(payload).and_then(|options| {
            run_passive_audio_probe(options)
                .map_err(|err| ShellCommandError::new("audioProbeFailed", err))
        }) {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "audioCaptureStart" => match parse_audio_capture_start_options(payload) {
            Ok(options) => {
                let result = call_audio_sidecar_command(
                    "captureStart",
                    audio_capture_start_sidecar_payload(&options),
                );
                let payload =
                    audio_capture_shell_payload(&options, result.payload.clone(), &result);
                response_line(
                    request_id,
                    result.success,
                    result.error_code.as_deref().unwrap_or(""),
                    result.fallback_reason.as_deref().unwrap_or(""),
                    started,
                    payload,
                )
            }
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "audioCaptureStop" => match parse_audio_capture_stop_payload(payload) {
            Ok(stream_id) => {
                let result = call_audio_sidecar_command(
                    "captureStop",
                    json!({
                        "streamId": stream_id,
                    }),
                );
                let payload = audio_capture_stop_shell_payload(result.payload.clone(), &result);
                response_line(request_id, true, "", "", started, payload)
            }
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "audioPrewarmStart" => match parse_audio_capture_start_options(payload) {
            Ok(options) => {
                let result = call_audio_sidecar_command(
                    "prewarmStart",
                    audio_prewarm_start_sidecar_payload(&options),
                );
                let payload =
                    audio_prewarm_shell_payload(&options, result.payload.clone(), &result);
                response_line(
                    request_id,
                    result.success,
                    result.error_code.as_deref().unwrap_or(""),
                    result.fallback_reason.as_deref().unwrap_or(""),
                    started,
                    payload,
                )
            }
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        "audioPrewarmStop" => match parse_audio_prewarm_stop_payload(payload) {
            Ok(prewarm_id) => {
                let result = call_audio_sidecar_command(
                    "prewarmStop",
                    json!({
                        "prewarmId": prewarm_id,
                    }),
                );
                let payload = audio_prewarm_stop_shell_payload(result.payload.clone(), &result);
                response_line(request_id, true, "", "", started, payload)
            }
            Err(err) => response_line(
                request_id,
                false,
                err.code,
                &err.reason,
                started,
                err.payload,
            ),
        },
        _ => response_line(
            request_id,
            false,
            "unknownCommand",
            "unsupported shell IPC command",
            started,
            json!({}),
        ),
    }
}

fn audio_capture_start_sidecar_payload(options: &AudioCaptureStartOptions) -> Value {
    json!({
        "sampleRate": options.sample_rate,
        "channels": options.channels,
        "blockSize": options.block_size,
        "devicePreference": options.device_preference,
        "portAudioLabel": options.port_audio_label,
        "nativeEndpointIdHash": options.native_endpoint_id_hash,
        "prebufferMs": options.prebuffer_ms,
        "prewarmId": options.prewarm_id,
        "frameProtocol": audio_frame_protocol_payload(),
    })
}

fn audio_prewarm_start_sidecar_payload(options: &AudioCaptureStartOptions) -> Value {
    json!({
        "sampleRate": options.sample_rate,
        "channels": options.channels,
        "blockSize": options.block_size,
        "devicePreference": options.device_preference,
        "portAudioLabel": options.port_audio_label,
        "nativeEndpointIdHash": options.native_endpoint_id_hash,
        "prebufferMs": options.prebuffer_ms,
        "prewarmId": options.prewarm_id,
        "frameProtocol": audio_frame_protocol_payload(),
    })
}

fn audio_capture_shell_payload(
    options: &AudioCaptureStartOptions,
    sidecar_payload: Value,
    result: &crate::audio_sidecar_client::AudioSidecarCallResult,
) -> Value {
    let original_sidecar_payload = sidecar_payload.clone();
    let mut payload = match sidecar_payload {
        Value::Object(map) => Value::Object(map),
        other => json!({
            "sidecarPayloadValue": other,
        }),
    };
    if let Some(object) = payload.as_object_mut() {
        object.insert("engine".to_string(), json!("rust-prototype"));
        object.insert("available".to_string(), json!(result.success));
        object.insert(
            "requestedFormat".to_string(),
            json!({
            "sampleRate": options.sample_rate,
            "channels": options.channels,
            "blockSize": options.block_size,
            "devicePreference": options.device_preference,
            "portAudioLabel": options.port_audio_label,
            "nativeEndpointIdHash": options.native_endpoint_id_hash,
            "prebufferMs": options.prebuffer_ms,
            "prewarmId": options.prewarm_id,
            }),
        );
        object.insert("frameProtocol".to_string(), audio_frame_protocol_payload());
        object.insert("sidecar".to_string(), sidecar_status_payload(result));
        object.insert("sidecarPayload".to_string(), original_sidecar_payload);
    }
    payload
}

fn audio_prewarm_shell_payload(
    options: &AudioCaptureStartOptions,
    sidecar_payload: Value,
    result: &crate::audio_sidecar_client::AudioSidecarCallResult,
) -> Value {
    let original_sidecar_payload = sidecar_payload.clone();
    let mut payload = match sidecar_payload {
        Value::Object(map) => Value::Object(map),
        other => json!({
            "sidecarPayloadValue": other,
        }),
    };
    if let Some(object) = payload.as_object_mut() {
        object.insert("engine".to_string(), json!("rust-prototype"));
        object.insert("available".to_string(), json!(result.success));
        object.insert(
            "requestedFormat".to_string(),
            json!({
            "sampleRate": options.sample_rate,
            "channels": options.channels,
            "blockSize": options.block_size,
            "devicePreference": options.device_preference,
            "portAudioLabel": options.port_audio_label,
            "nativeEndpointIdHash": options.native_endpoint_id_hash,
            "prebufferMs": options.prebuffer_ms,
            "prewarmId": options.prewarm_id,
            }),
        );
        object.insert("frameProtocol".to_string(), audio_frame_protocol_payload());
        object.insert("sidecar".to_string(), sidecar_status_payload(result));
        object.insert("sidecarPayload".to_string(), original_sidecar_payload);
    }
    payload
}

fn audio_capture_stop_shell_payload(
    sidecar_payload: Value,
    result: &crate::audio_sidecar_client::AudioSidecarCallResult,
) -> Value {
    let original_sidecar_payload = sidecar_payload.clone();
    let mut payload = match sidecar_payload {
        Value::Object(map) => Value::Object(map),
        other => json!({
            "sidecarPayloadValue": other,
        }),
    };
    if let Some(object) = payload.as_object_mut() {
        object.insert("engine".to_string(), json!("rust-prototype"));
        object
            .entry("stopped".to_string())
            .or_insert_with(|| json!(false));
        object
            .entry("streamId".to_string())
            .or_insert_with(|| json!(""));
        object
            .entry("reason".to_string())
            .or_insert_with(|| json!("noRustAudioSidecar"));
        object.insert("sidecar".to_string(), sidecar_status_payload(result));
        object.insert("sidecarPayload".to_string(), original_sidecar_payload);
    }
    payload
}

fn audio_prewarm_stop_shell_payload(
    sidecar_payload: Value,
    result: &crate::audio_sidecar_client::AudioSidecarCallResult,
) -> Value {
    let original_sidecar_payload = sidecar_payload.clone();
    let mut payload = match sidecar_payload {
        Value::Object(map) => Value::Object(map),
        other => json!({
            "sidecarPayloadValue": other,
        }),
    };
    if let Some(object) = payload.as_object_mut() {
        object.insert("engine".to_string(), json!("rust-prototype"));
        object
            .entry("stopped".to_string())
            .or_insert_with(|| json!(false));
        object
            .entry("prewarmId".to_string())
            .or_insert_with(|| json!(""));
        object
            .entry("reason".to_string())
            .or_insert_with(|| json!("noRustAudioSidecar"));
        object.insert("sidecar".to_string(), sidecar_status_payload(result));
        object.insert("sidecarPayload".to_string(), original_sidecar_payload);
    }
    payload
}

fn sidecar_status_payload(result: &crate::audio_sidecar_client::AudioSidecarCallResult) -> Value {
    json!({
        "executableAvailable": result.executable_available,
        "pathHash": result.executable_path_hash,
        "pid": result.pid,
        "errorCode": result.error_code,
        "fallbackReason": result.fallback_reason,
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

fn parse_audio_probe_options(
    payload: &Value,
) -> Result<PassiveAudioProbeOptions, ShellCommandError> {
    let Some(payload) = payload.as_object() else {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "audioProbe payload must be an object",
        ));
    };
    Ok(PassiveAudioProbeOptions {
        requested_sample_rate: optional_u64(payload, "sampleRate", 16_000, 192_000) as u32,
        requested_channels: optional_u64(payload, "channels", 1, 16) as u16,
        block_size: optional_u64(payload, "blockSize", 512, 16_384) as u32,
        device_preference: bounded_string(payload, "devicePreference", "default", 96),
        port_audio_label: bounded_string(payload, "portAudioLabel", "", 160),
        native_endpoint_id_hash: bounded_string(payload, "nativeEndpointIdHash", "", 64),
    })
}

fn parse_audio_capture_start_options(
    payload: &Value,
) -> Result<AudioCaptureStartOptions, ShellCommandError> {
    let Some(payload) = payload.as_object() else {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "audioCaptureStart payload must be an object",
        ));
    };
    Ok(AudioCaptureStartOptions {
        sample_rate: optional_u64(payload, "sampleRate", 16_000, 192_000) as u32,
        channels: optional_u64(payload, "channels", 1, 16) as u16,
        block_size: optional_u64(payload, "blockSize", 512, 16_384) as u32,
        device_preference: bounded_string(payload, "devicePreference", "default", 96),
        port_audio_label: bounded_string(payload, "portAudioLabel", "", 160),
        native_endpoint_id_hash: bounded_string(payload, "nativeEndpointIdHash", "", 64),
        prebuffer_ms: optional_u64(payload, "prebufferMs", 0, 2_000) as u32,
        prewarm_id: bounded_string(payload, "prewarmId", "", 96),
    })
}

fn parse_audio_capture_stop_payload(payload: &Value) -> Result<String, ShellCommandError> {
    let Some(payload) = payload.as_object() else {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "audioCaptureStop payload must be an object",
        ));
    };
    Ok(bounded_string(payload, "streamId", "", 96))
}

fn parse_audio_prewarm_stop_payload(payload: &Value) -> Result<String, ShellCommandError> {
    let Some(payload) = payload.as_object() else {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "audioPrewarmStop payload must be an object",
        ));
    };
    Ok(bounded_string(payload, "prewarmId", "", 96))
}

fn response_line(
    request_id: &str,
    success: bool,
    error_code: &str,
    fallback_reason: &str,
    started: Instant,
    payload: Value,
) -> String {
    let total_ms = started.elapsed().as_secs_f64() * 1000.0;
    let response = json!({
        "apiVersion": API_VERSION,
        "requestId": request_id,
        "success": success,
        "errorCode": if error_code.is_empty() { Value::Null } else { Value::String(error_code.to_string()) },
        "fallbackReason": if fallback_reason.is_empty() { Value::Null } else { Value::String(fallback_reason.to_string()) },
        "timingsMs": {
            "total": total_ms,
        },
        "payload": payload,
    });
    format!("{response}\n")
}

fn hash_sensitive_identifier(raw: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in raw.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn parse_inject_text_options(payload: &Value) -> Result<InjectTextOptions, ShellCommandError> {
    let Some(payload) = payload.as_object() else {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "injectText payload must be an object",
        ));
    };
    let text = payload
        .get("text")
        .and_then(Value::as_str)
        .ok_or_else(|| ShellCommandError::new("invalidPayload", "injectText requires text"))?
        .to_string();
    if text.as_bytes().len() > MAX_INJECT_TEXT_BYTES {
        return Err(ShellCommandError::new(
            "payloadTooLarge",
            "injectText text exceeded size limit",
        ));
    }
    if text.contains('\0') {
        return Err(ShellCommandError::new(
            "invalidPayload",
            "injectText rejects embedded NUL characters",
        ));
    }

    let dispatch = bounded_string(payload, "dispatch", "ctrlV", 32);
    if dispatch != "ctrlV" {
        return Err(ShellCommandError::new(
            "unsupportedDispatch",
            "injectText only supports ctrlV dispatch",
        ));
    }
    let pre_delay_mode = bounded_string(payload, "preDelayMode", "fixed", 32);
    if pre_delay_mode != "fixed" && pre_delay_mode != "auto" {
        return Err(ShellCommandError::new(
            "invalidPreDelayMode",
            "injectText preDelayMode must be fixed or auto",
        ));
    }

    Ok(InjectTextOptions {
        text,
        restore_clipboard: optional_bool(payload, "restoreClipboard", true),
        restore_delay_ms: optional_u64(payload, "restoreDelayMs", DEFAULT_RESTORE_DELAY_MS, 30_000),
        pre_delay_ms: optional_u64(payload, "preDelayMs", 0, 5_000),
        pre_delay_mode,
        dispatch,
        max_clipboard_retries: optional_u64(
            payload,
            "maxClipboardRetries",
            u64::from(DEFAULT_CLIPBOARD_RETRIES),
            50,
        ) as u32,
        clipboard_retry_delay_ms: optional_u64(
            payload,
            "clipboardRetryDelayMs",
            DEFAULT_CLIPBOARD_RETRY_DELAY_MS,
            500,
        ),
        deadline_ms: optional_u64(payload, "deadlineMs", DEFAULT_INJECT_DEADLINE_MS, 30_000),
    })
}

fn optional_bool(
    payload: &serde_json::Map<String, Value>,
    field: &str,
    default_value: bool,
) -> bool {
    payload
        .get(field)
        .and_then(Value::as_bool)
        .unwrap_or(default_value)
}

fn optional_u64(
    payload: &serde_json::Map<String, Value>,
    field: &str,
    default_value: u64,
    max_value: u64,
) -> u64 {
    payload
        .get(field)
        .and_then(Value::as_u64)
        .unwrap_or(default_value)
        .min(max_value)
}

fn bounded_string(
    payload: &serde_json::Map<String, Value>,
    field: &str,
    default_value: &str,
    max_len: usize,
) -> String {
    payload
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or(default_value)
        .chars()
        .take(max_len)
        .collect()
}

#[cfg(windows)]
fn inject_text(payload: &Value) -> Result<Value, ShellCommandError> {
    let options = parse_inject_text_options(payload)?;
    let started = Instant::now();
    let mut markers: Vec<&'static str> = Vec::new();
    let pre_delay_ms = resolve_pre_delay_ms(&options, foreground_title_for_policy().as_deref());
    let foreground_before = foreground_snapshot();

    let clipboard_options = ClipboardOptions {
        retries: options.max_clipboard_retries,
        retry_delay: Duration::from_millis(options.clipboard_retry_delay_ms),
    };
    let (previous_text, clipboard_read_ms) = if options.restore_clipboard {
        let read_started = Instant::now();
        match read_clipboard_text(&clipboard_options) {
            Ok(Some(value)) => (Some(value), Some(elapsed_ms(read_started))),
            Ok(None) => {
                let partial_payload = inject_response_payload(
                    &options,
                    &markers,
                    pre_delay_ms,
                    Some(elapsed_ms(read_started)),
                    None,
                    None,
                    elapsed_ms(started),
                    restore_status("previousClipboardUnavailable", None),
                    &foreground_before,
                    &foreground_before,
                );
                return Err(ShellCommandError::new(
                    "clipboardRestoreUnavailable",
                    "previous clipboard text could not be captured",
                )
                .with_payload(partial_payload));
            }
            Err(err) => {
                let partial_payload = inject_response_payload(
                    &options,
                    &markers,
                    pre_delay_ms,
                    Some(elapsed_ms(read_started)),
                    None,
                    None,
                    elapsed_ms(started),
                    restore_status("clipboardReadFailed", Some(err.code)),
                    &foreground_before,
                    &foreground_before,
                );
                return Err(err.with_payload(partial_payload));
            }
        }
    } else {
        (None, None)
    };

    ensure_deadline_budget(&options, started, 25, "deadlineBeforeSet", || {
        inject_response_payload(
            &options,
            &markers,
            pre_delay_ms,
            clipboard_read_ms,
            None,
            None,
            elapsed_ms(started),
            restore_status("notNeeded", None),
            &foreground_before,
            &foreground_before,
        )
    })?;

    let set_started = Instant::now();
    let clipboard_sequence_after_set = set_clipboard_text(&options.text, &clipboard_options)?;
    let clipboard_set_ms = elapsed_ms(set_started);
    markers.push("clipboard_set");

    if pre_delay_ms > 0 {
        ensure_deadline_budget(
            &options,
            started,
            pre_delay_ms + 50,
            "deadlineBeforePaste",
            || {
                let restore = restore_clipboard_now(
                    &options.text,
                    previous_text.as_deref(),
                    clipboard_sequence_after_set,
                    &clipboard_options,
                );
                inject_response_payload(
                    &options,
                    &markers,
                    pre_delay_ms,
                    clipboard_read_ms,
                    Some(clipboard_set_ms),
                    None,
                    elapsed_ms(started),
                    restore,
                    &foreground_before,
                    &foreground_snapshot(),
                )
            },
        )?;
    } else {
        ensure_deadline_budget(&options, started, 50, "deadlineBeforePaste", || {
            let restore = restore_clipboard_now(
                &options.text,
                previous_text.as_deref(),
                clipboard_sequence_after_set,
                &clipboard_options,
            );
            inject_response_payload(
                &options,
                &markers,
                pre_delay_ms,
                clipboard_read_ms,
                Some(clipboard_set_ms),
                None,
                elapsed_ms(started),
                restore,
                &foreground_before,
                &foreground_snapshot(),
            )
        })?;
    }

    if pre_delay_ms > 0 {
        thread::sleep(Duration::from_millis(pre_delay_ms));
    }

    ensure_deadline_budget(&options, started, 50, "deadlineBeforePaste", || {
        let restore = restore_clipboard_now(
            &options.text,
            previous_text.as_deref(),
            clipboard_sequence_after_set,
            &clipboard_options,
        );
        inject_response_payload(
            &options,
            &markers,
            pre_delay_ms,
            clipboard_read_ms,
            Some(clipboard_set_ms),
            None,
            elapsed_ms(started),
            restore,
            &foreground_before,
            &foreground_snapshot(),
        )
    })?;

    let paste_started = Instant::now();
    if let Err(err) = dispatch_ctrl_v() {
        let restore = restore_clipboard_now(
            &options.text,
            previous_text.as_deref(),
            clipboard_sequence_after_set,
            &clipboard_options,
        );
        let partial_payload = inject_response_payload(
            &options,
            &markers,
            pre_delay_ms,
            clipboard_read_ms,
            Some(clipboard_set_ms),
            Some(elapsed_ms(paste_started)),
            elapsed_ms(started),
            restore,
            &foreground_before,
            &foreground_snapshot(),
        );
        return Err(err.with_payload(partial_payload));
    }
    let paste_dispatch_ms = elapsed_ms(paste_started);
    markers.push("paste");
    let foreground_after = foreground_snapshot();

    let restore = if options.restore_clipboard {
        if let Some(previous_text) = previous_text {
            schedule_clipboard_restore(
                options.text.clone(),
                previous_text,
                clipboard_options,
                clipboard_sequence_after_set,
                options.restore_delay_ms,
            );
            restore_status("scheduled", None)
        } else {
            restore_status("previousClipboardUnavailable", None)
        }
    } else {
        restore_status("disabled", None)
    };

    Ok(inject_response_payload(
        &options,
        &markers,
        pre_delay_ms,
        clipboard_read_ms,
        Some(clipboard_set_ms),
        Some(paste_dispatch_ms),
        elapsed_ms(started),
        restore,
        &foreground_before,
        &foreground_after,
    ))
}

#[cfg(not(windows))]
fn inject_text(payload: &Value) -> Result<Value, ShellCommandError> {
    let _ = parse_inject_text_options(payload)?;
    Err(ShellCommandError::new(
        "unsupportedPlatform",
        "injectText is only available on Windows",
    ))
}

fn elapsed_ms(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1000.0
}

fn inject_response_payload(
    options: &InjectTextOptions,
    markers: &[&'static str],
    pre_delay_ms: u64,
    clipboard_read_ms: Option<f64>,
    clipboard_set_ms: Option<f64>,
    paste_dispatch_ms: Option<f64>,
    total_ms: f64,
    restore: Value,
    foreground_before: &Value,
    foreground_after: &Value,
) -> Value {
    json!({
        "method": "tauri",
        "dispatch": options.dispatch,
        "preDelayMode": options.pre_delay_mode,
        "requestedPreDelayMs": options.pre_delay_ms,
        "markers": markers,
        "restore": restore,
        "restoreScheduled": restore
            .get("scheduled")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        "foregroundBefore": foreground_before,
        "foregroundAfter": foreground_after,
        "foregroundChanged": foreground_before != foreground_after,
        "timingsMs": {
            "clipboardRead": clipboard_read_ms,
            "clipboardSet": clipboard_set_ms,
            "preDelay": pre_delay_ms as f64,
            "pasteDispatch": paste_dispatch_ms,
            "total": total_ms,
        },
    })
}

fn restore_status(skipped_reason: &str, error_code: Option<&str>) -> Value {
    json!({
        "scheduled": skipped_reason == "scheduled",
        "attempted": false,
        "succeeded": Value::Null,
        "skippedReason": skipped_reason,
        "errorCode": error_code,
    })
}

fn ensure_deadline_budget<F>(
    options: &InjectTextOptions,
    started: Instant,
    required_ms: u64,
    code: &'static str,
    payload: F,
) -> Result<(), ShellCommandError>
where
    F: FnOnce() -> Value,
{
    let elapsed_ms = started.elapsed().as_millis() as u64;
    let remaining_ms = options.deadline_ms.saturating_sub(elapsed_ms);
    if remaining_ms < required_ms {
        return Err(ShellCommandError::new(
            code,
            format!(
                "injectText deadline would be exceeded before side effect (remaining={remaining_ms}ms required={required_ms}ms)"
            ),
        )
        .with_payload(payload()));
    }
    Ok(())
}

#[cfg(windows)]
#[derive(Clone)]
struct ClipboardOptions {
    retries: u32,
    retry_delay: Duration,
}

const CLIPBOARD_OWNER_CLASS: &str = "ScriberClipboardOwner";

#[cfg(windows)]
struct ClipboardOwnerWindow {
    hwnd: windows_sys::Win32::Foundation::HWND,
}

#[cfg(windows)]
impl ClipboardOwnerWindow {
    fn create() -> Result<Self, ShellCommandError> {
        use std::{ffi::OsStr, os::windows::ffi::OsStrExt, ptr};
        use windows_sys::Win32::{
            Foundation::{GetLastError, ERROR_CLASS_ALREADY_EXISTS},
            System::LibraryLoader::GetModuleHandleW,
            UI::WindowsAndMessaging::{
                CreateWindowExW, DefWindowProcW, RegisterClassW, HWND_MESSAGE, WNDCLASSW,
            },
        };

        unsafe extern "system" fn window_proc(
            hwnd: windows_sys::Win32::Foundation::HWND,
            msg: u32,
            wparam: windows_sys::Win32::Foundation::WPARAM,
            lparam: windows_sys::Win32::Foundation::LPARAM,
        ) -> windows_sys::Win32::Foundation::LRESULT {
            unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
        }

        let class_name: Vec<u16> = OsStr::new(CLIPBOARD_OWNER_CLASS)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let hinstance = unsafe { GetModuleHandleW(ptr::null()) };
        if hinstance.is_null() {
            return Err(ShellCommandError::new(
                "clipboardOwnerFailed",
                "GetModuleHandleW failed while creating clipboard owner",
            ));
        }

        let wndclass = WNDCLASSW {
            style: 0,
            lpfnWndProc: Some(window_proc),
            cbClsExtra: 0,
            cbWndExtra: 0,
            hInstance: hinstance,
            hIcon: ptr::null_mut(),
            hCursor: ptr::null_mut(),
            hbrBackground: ptr::null_mut(),
            lpszMenuName: ptr::null(),
            lpszClassName: class_name.as_ptr(),
        };
        let atom = unsafe { RegisterClassW(&wndclass) };
        if atom == 0 {
            let err = unsafe { GetLastError() };
            if err != ERROR_CLASS_ALREADY_EXISTS {
                return Err(ShellCommandError::new(
                    "clipboardOwnerFailed",
                    format!("RegisterClassW failed while creating clipboard owner: {err}"),
                ));
            }
        }

        let hwnd = unsafe {
            CreateWindowExW(
                0,
                class_name.as_ptr(),
                class_name.as_ptr(),
                0,
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                ptr::null_mut(),
                hinstance,
                ptr::null(),
            )
        };
        if hwnd.is_null() {
            return Err(ShellCommandError::new(
                "clipboardOwnerFailed",
                format!(
                    "CreateWindowExW failed while creating clipboard owner: {}",
                    unsafe { GetLastError() }
                ),
            ));
        }

        Ok(Self { hwnd })
    }

    fn hwnd(&self) -> windows_sys::Win32::Foundation::HWND {
        self.hwnd
    }
}

#[cfg(windows)]
impl Drop for ClipboardOwnerWindow {
    fn drop(&mut self) {
        if !self.hwnd.is_null() {
            unsafe {
                let _ = windows_sys::Win32::UI::WindowsAndMessaging::DestroyWindow(self.hwnd);
            }
        }
    }
}

#[cfg(windows)]
fn foreground_snapshot() -> Value {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
    };

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null() {
            return json!({
                "available": false,
                "windowHash": Value::Null,
                "titleHash": Value::Null,
                "processIdHash": Value::Null,
            });
        }

        let mut pid = 0u32;
        let _ = GetWindowThreadProcessId(hwnd, &mut pid);
        let mut title_buffer = [0u16; 512];
        let title_len = GetWindowTextW(hwnd, title_buffer.as_mut_ptr(), title_buffer.len() as i32)
            .max(0) as usize;
        let title_hash = if title_len == 0 {
            Value::Null
        } else {
            let title = String::from_utf16_lossy(&title_buffer[..title_len]);
            Value::String(hash_sensitive_identifier(&title))
        };

        json!({
            "available": true,
            "windowHash": hash_sensitive_identifier(&format!("{hwnd:p}")),
            "titleHash": title_hash,
            "processIdHash": if pid == 0 {
                Value::Null
            } else {
                Value::String(hash_sensitive_identifier(&pid.to_string()))
            },
        })
    }
}

#[cfg(windows)]
fn foreground_title_for_policy() -> Option<String> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowTextW};

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null() {
            return None;
        }
        let mut title_buffer = [0u16; 512];
        let title_len = GetWindowTextW(hwnd, title_buffer.as_mut_ptr(), title_buffer.len() as i32)
            .max(0) as usize;
        if title_len == 0 {
            None
        } else {
            Some(String::from_utf16_lossy(&title_buffer[..title_len]))
        }
    }
}

#[cfg(not(windows))]
fn foreground_snapshot() -> Value {
    json!({
        "available": false,
        "windowHash": Value::Null,
        "titleHash": Value::Null,
        "processIdHash": Value::Null,
    })
}

#[cfg(not(windows))]
fn foreground_title_for_policy() -> Option<String> {
    None
}

fn resolve_pre_delay_ms(options: &InjectTextOptions, foreground_title: Option<&str>) -> u64 {
    if options.pre_delay_mode == "auto" {
        return if foreground_title
            .map(is_slow_text_injection_foreground_title)
            .unwrap_or(false)
        {
            options.pre_delay_ms
        } else {
            0
        };
    }
    options.pre_delay_ms
}

fn is_slow_text_injection_foreground_title(title: &str) -> bool {
    let title_lower = title.trim().to_lowercase();
    title_lower.ends_with(" - word") || title_lower.ends_with(" - outlook")
}

#[cfg(windows)]
fn read_clipboard_text(options: &ClipboardOptions) -> Result<Option<String>, ShellCommandError> {
    use windows_sys::Win32::System::{
        DataExchange::{
            CloseClipboard, GetClipboardData, IsClipboardFormatAvailable, OpenClipboard,
        },
        Memory::{GlobalLock, GlobalSize, GlobalUnlock},
        Ole::CF_UNICODETEXT,
    };

    let owner = ClipboardOwnerWindow::create()?;
    for _ in 0..options.retries.max(1) {
        if unsafe { OpenClipboard(owner.hwnd()) } == 0 {
            thread::sleep(options.retry_delay);
            continue;
        }
        let result = unsafe {
            if IsClipboardFormatAvailable(CF_UNICODETEXT as u32) == 0 {
                Ok(None)
            } else {
                let handle = GetClipboardData(CF_UNICODETEXT as u32);
                if handle.is_null() {
                    Err(ShellCommandError::new(
                        "clipboardReadFailed",
                        "GetClipboardData returned null",
                    ))
                } else {
                    let ptr = GlobalLock(handle);
                    if ptr.is_null() {
                        Err(ShellCommandError::new(
                            "clipboardReadFailed",
                            "GlobalLock failed for clipboard data",
                        ))
                    } else {
                        let byte_len = GlobalSize(handle);
                        let text = if byte_len < std::mem::size_of::<u16>() {
                            String::new()
                        } else {
                            let max_len = byte_len / std::mem::size_of::<u16>();
                            let mut len = 0usize;
                            let chars = ptr.cast::<u16>();
                            while len < max_len && *chars.add(len) != 0 {
                                len += 1;
                            }
                            let slice = std::slice::from_raw_parts(chars, len);
                            String::from_utf16_lossy(slice)
                        };
                        let _ = GlobalUnlock(handle);
                        Ok(Some(text))
                    }
                }
            }
        };
        unsafe {
            CloseClipboard();
        }
        return result;
    }
    Err(ShellCommandError::new(
        "clipboardBusy",
        "could not open clipboard for read",
    ))
}

#[cfg(windows)]
fn set_clipboard_text(text: &str, options: &ClipboardOptions) -> Result<u32, ShellCommandError> {
    use std::{mem, ptr};
    use windows_sys::Win32::{
        Foundation::GlobalFree,
        System::{
            DataExchange::{CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData},
            Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE},
            Ole::CF_UNICODETEXT,
        },
    };

    let mut encoded: Vec<u16> = text.encode_utf16().collect();
    encoded.push(0);
    let byte_len = encoded.len() * mem::size_of::<u16>();

    let owner = ClipboardOwnerWindow::create()?;
    for _ in 0..options.retries.max(1) {
        if unsafe { OpenClipboard(owner.hwnd()) } == 0 {
            thread::sleep(options.retry_delay);
            continue;
        }
        let result = unsafe {
            let handle = GlobalAlloc(GMEM_MOVEABLE, byte_len);
            if handle.is_null() {
                Err(ShellCommandError::new(
                    "clipboardSetFailed",
                    "GlobalAlloc failed for clipboard data",
                ))
            } else {
                let locked_ptr = GlobalLock(handle);
                if locked_ptr.is_null() {
                    let _ = GlobalFree(handle);
                    Err(ShellCommandError::new(
                        "clipboardSetFailed",
                        "GlobalLock failed for clipboard data",
                    ))
                } else {
                    ptr::copy_nonoverlapping(
                        encoded.as_ptr().cast::<u8>(),
                        locked_ptr.cast::<u8>(),
                        byte_len,
                    );
                    let _ = GlobalUnlock(handle);
                    if EmptyClipboard() == 0 {
                        let _ = GlobalFree(handle);
                        Err(ShellCommandError::new(
                            "clipboardSetFailed",
                            "EmptyClipboard failed",
                        ))
                    } else if SetClipboardData(CF_UNICODETEXT as u32, handle).is_null() {
                        let _ = GlobalFree(handle);
                        Err(ShellCommandError::new(
                            "clipboardSetFailed",
                            "SetClipboardData failed",
                        ))
                    } else {
                        Ok(())
                    }
                }
            }
        };
        unsafe {
            CloseClipboard();
        }
        return result.map(|()| clipboard_sequence_number());
    }
    Err(ShellCommandError::new(
        "clipboardBusy",
        "could not open clipboard for write",
    ))
}

#[cfg(windows)]
fn clipboard_sequence_number() -> u32 {
    use windows_sys::Win32::System::DataExchange::GetClipboardSequenceNumber;
    unsafe { GetClipboardSequenceNumber() }
}

#[cfg(windows)]
fn dispatch_ctrl_v() -> Result<(), ShellCommandError> {
    use std::mem;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, KEYEVENTF_KEYUP, VIRTUAL_KEY, VK_CONTROL,
    };

    const VK_V: VIRTUAL_KEY = 0x56;
    let inputs = [
        keyboard_input(VK_CONTROL, 0),
        keyboard_input(VK_V, 0),
        keyboard_input(VK_V, KEYEVENTF_KEYUP),
        keyboard_input(VK_CONTROL, KEYEVENTF_KEYUP),
    ];
    let sent = unsafe {
        SendInput(
            inputs.len() as u32,
            inputs.as_ptr(),
            mem::size_of::<INPUT>() as i32,
        )
    };
    if sent != inputs.len() as u32 {
        return Err(ShellCommandError::new(
            "pasteDispatchFailed",
            format!("SendInput sent {sent}/{} events", inputs.len()),
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn keyboard_input(
    virtual_key: windows_sys::Win32::UI::Input::KeyboardAndMouse::VIRTUAL_KEY,
    flags: windows_sys::Win32::UI::Input::KeyboardAndMouse::KEYBD_EVENT_FLAGS,
) -> windows_sys::Win32::UI::Input::KeyboardAndMouse::INPUT {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT,
    };

    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: virtual_key,
                wScan: 0,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

#[cfg(windows)]
fn schedule_clipboard_restore(
    injected_text: String,
    previous_text: String,
    options: ClipboardOptions,
    expected_sequence: u32,
    restore_delay_ms: u64,
) {
    thread::spawn(move || {
        if restore_delay_ms > 0 {
            thread::sleep(Duration::from_millis(restore_delay_ms));
        }
        let _ = restore_clipboard_now(
            &injected_text,
            Some(&previous_text),
            expected_sequence,
            &options,
        );
    });
}

#[cfg(windows)]
fn restore_clipboard_now(
    injected_text: &str,
    previous_text: Option<&str>,
    expected_sequence: u32,
    options: &ClipboardOptions,
) -> Value {
    let Some(previous_text) = previous_text else {
        return json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "previousClipboardUnavailable",
            "errorCode": Value::Null,
        });
    };

    let current_sequence = clipboard_sequence_number();
    if current_sequence != expected_sequence {
        return json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "clipboardSequenceChanged",
            "errorCode": Value::Null,
        });
    }

    match read_clipboard_text(options) {
        Ok(Some(current)) if current == injected_text => {
            match set_clipboard_text(previous_text, options) {
                Ok(_) => json!({
                    "scheduled": false,
                    "attempted": true,
                    "succeeded": true,
                    "skippedReason": Value::Null,
                    "errorCode": Value::Null,
                }),
                Err(err) => json!({
                    "scheduled": false,
                    "attempted": true,
                    "succeeded": false,
                    "skippedReason": "restoreFailed",
                    "errorCode": err.code,
                }),
            }
        }
        Ok(Some(_)) => json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "clipboardContentChanged",
            "errorCode": Value::Null,
        }),
        Ok(None) => json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "clipboardFormatChanged",
            "errorCode": Value::Null,
        }),
        Err(err) => json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "restoreReadFailed",
            "errorCode": err.code,
        }),
    }
}

#[cfg(windows)]
struct PipeSecurityAttributes {
    security_descriptor: windows_sys::Win32::Security::PSECURITY_DESCRIPTOR,
    attributes: windows_sys::Win32::Security::SECURITY_ATTRIBUTES,
}

#[cfg(windows)]
impl PipeSecurityAttributes {
    fn as_ptr(&self) -> *const windows_sys::Win32::Security::SECURITY_ATTRIBUTES {
        &self.attributes
    }
}

#[cfg(windows)]
impl Drop for PipeSecurityAttributes {
    fn drop(&mut self) {
        if !self.security_descriptor.is_null() {
            unsafe {
                let _ = windows_sys::Win32::Foundation::LocalFree(self.security_descriptor as _);
            }
        }
    }
}

fn shell_ipc_pipe_security_sddl(logon_sid: Option<&str>) -> String {
    match logon_sid {
        Some(sid) if !sid.trim().is_empty() => {
            format!("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{sid})")
        }
        _ => SHELL_IPC_PIPE_SECURITY_FALLBACK_SDDL.to_string(),
    }
}

#[cfg(windows)]
struct TokenHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl Drop for TokenHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                let _ = windows_sys::Win32::Foundation::CloseHandle(self.0);
            }
        }
    }
}

#[cfg(windows)]
fn sid_to_string(sid: windows_sys::Win32::Security::PSID) -> Result<String, String> {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::{GetLastError, LocalFree},
        Security::Authorization::ConvertSidToStringSidW,
    };

    let mut string_sid: windows_sys::core::PWSTR = ptr::null_mut();
    let converted = unsafe { ConvertSidToStringSidW(sid, &mut string_sid) };
    if converted == 0 || string_sid.is_null() {
        return Err(format!("ConvertSidToStringSidW failed with {}", unsafe {
            GetLastError()
        }));
    }

    let mut len = 0usize;
    unsafe {
        while *string_sid.add(len) != 0 {
            len += 1;
        }
    }
    let value = unsafe { String::from_utf16_lossy(std::slice::from_raw_parts(string_sid, len)) };
    unsafe {
        let _ = LocalFree(string_sid as _);
    }
    Ok(value)
}

#[cfg(windows)]
fn current_logon_sid_string() -> Result<String, String> {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::{GetLastError, HANDLE},
        Security::{GetTokenInformation, TokenGroups, TOKEN_GROUPS, TOKEN_QUERY},
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };

    let mut token: HANDLE = ptr::null_mut();
    let opened = unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) };
    if opened == 0 || token.is_null() {
        return Err(format!("OpenProcessToken failed with {}", unsafe {
            GetLastError()
        }));
    }
    let token = TokenHandle(token);

    let mut needed = 0u32;
    unsafe {
        let _ = GetTokenInformation(token.0, TokenGroups, ptr::null_mut(), 0, &mut needed);
    }
    if needed == 0 {
        return Err(format!(
            "GetTokenInformation(TokenGroups) returned zero size with {}",
            unsafe { GetLastError() }
        ));
    }

    let mut buffer = vec![0u8; needed as usize];
    let read = unsafe {
        GetTokenInformation(
            token.0,
            TokenGroups,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    };
    if read == 0 {
        return Err(format!(
            "GetTokenInformation(TokenGroups) failed with {}",
            unsafe { GetLastError() }
        ));
    }

    let groups = buffer.as_ptr().cast::<TOKEN_GROUPS>();
    let group_count = unsafe { (*groups).GroupCount as usize };
    let first_group = unsafe { (*groups).Groups.as_ptr() };
    for index in 0..group_count {
        let group = unsafe { *first_group.add(index) };
        if (group.Attributes & SE_GROUP_LOGON_ID_MASK) == SE_GROUP_LOGON_ID_MASK {
            return sid_to_string(group.Sid);
        }
    }

    Err("current token does not include a logon SID group".to_string())
}

#[cfg(windows)]
fn create_shell_ipc_security_attributes() -> Result<PipeSecurityAttributes, String> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::GetLastError,
        Security::{
            Authorization::{
                ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
            },
            PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES,
        },
    };

    let logon_sid = current_logon_sid_string().ok();
    let sddl_text = shell_ipc_pipe_security_sddl(logon_sid.as_deref());
    let sddl: Vec<u16> = OsStr::new(&sddl_text)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut security_descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
    let converted = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            SDDL_REVISION_1,
            &mut security_descriptor,
            ptr::null_mut(),
        )
    };
    if converted == 0 || security_descriptor.is_null() {
        return Err(format!(
            "ConvertStringSecurityDescriptorToSecurityDescriptorW failed with {}",
            unsafe { GetLastError() }
        ));
    }

    Ok(PipeSecurityAttributes {
        security_descriptor,
        attributes: SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security_descriptor,
            bInheritHandle: 0,
        },
    })
}

#[cfg(windows)]
fn run_shell_ipc_server<L>(config: ShellIpcConfig, stop: Arc<AtomicBool>, log: &mut L)
where
    L: FnMut(String),
{
    log(format!(
        "shell IPC server starting pipe_hash={}",
        config.pipe_name_hash()
    ));
    while !stop.load(Ordering::SeqCst) {
        match serve_one_client(&config) {
            Ok(()) => {}
            Err(err) => {
                if !stop.load(Ordering::SeqCst) {
                    log(format!("shell IPC request failed: {err}"));
                }
            }
        }
    }
    log("shell IPC server stopped".to_string());
}

#[cfg(windows)]
fn serve_one_client(config: &ShellIpcConfig) -> Result<(), String> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{
            CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
        },
        Storage::FileSystem::{FlushFileBuffers, PIPE_ACCESS_DUPLEX},
        System::Pipes::{
            ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_MESSAGE,
            PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_MESSAGE, PIPE_WAIT,
        },
    };

    let name: Vec<u16> = OsStr::new(&config.pipe_name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let security_attributes = create_shell_ipc_security_attributes()?;
    let pipe: HANDLE = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            PIPE_BUFFER_BYTES,
            PIPE_BUFFER_BYTES,
            250,
            security_attributes.as_ptr(),
        )
    };
    if pipe == INVALID_HANDLE_VALUE {
        return Err(format!("CreateNamedPipeW failed with {}", unsafe {
            GetLastError()
        }));
    }

    let connected = unsafe { ConnectNamedPipe(pipe, ptr::null_mut()) } != 0
        || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
    if !connected {
        let err = unsafe { GetLastError() };
        unsafe {
            CloseHandle(pipe);
        }
        return Err(format!("ConnectNamedPipe failed with {err}"));
    }

    let result = handle_connected_client(pipe, &config.token);
    unsafe {
        let _ = FlushFileBuffers(pipe);
        let _ = DisconnectNamedPipe(pipe);
        let _ = CloseHandle(pipe);
    }
    result
}

#[cfg(windows)]
fn handle_connected_client(
    pipe: windows_sys::Win32::Foundation::HANDLE,
    expected_token: &str,
) -> Result<(), String> {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::GetLastError, Storage::FileSystem::ReadFile, System::Pipes::PeekNamedPipe,
    };

    let mut request = Vec::<u8>::new();
    let read_started = Instant::now();
    loop {
        if read_started.elapsed() > Duration::from_millis(CLIENT_READ_TIMEOUT_MS) {
            return write_response(
                pipe,
                &response_line(
                    "",
                    false,
                    "readTimeout",
                    "shell IPC client did not send a complete request in time",
                    read_started,
                    json!({}),
                ),
            );
        }

        let mut available = 0u32;
        let peek_ok = unsafe {
            PeekNamedPipe(
                pipe,
                ptr::null_mut(),
                0,
                ptr::null_mut(),
                &mut available,
                ptr::null_mut(),
            )
        };
        if peek_ok == 0 {
            return Err(format!("PeekNamedPipe failed with {}", unsafe {
                GetLastError()
            }));
        }
        if available == 0 {
            thread::sleep(Duration::from_millis(5));
            continue;
        }

        let mut buffer = [0u8; 4096];
        let mut bytes_read = 0u32;
        let bytes_to_read = available.min(buffer.len() as u32);
        let ok = unsafe {
            ReadFile(
                pipe,
                buffer.as_mut_ptr(),
                bytes_to_read,
                &mut bytes_read,
                ptr::null_mut(),
            )
        };
        let read_error = if ok == 0 {
            unsafe { GetLastError() }
        } else {
            0
        };
        let read_status = pipe_read_chunk_status(ok, bytes_read, read_error)?;
        if matches!(read_status, PipeReadChunkStatus::Finished) {
            break;
        }
        request.extend_from_slice(&buffer[..bytes_read as usize]);
        if request.len() > MAX_REQUEST_BYTES {
            return write_response(
                pipe,
                &response_line(
                    "",
                    false,
                    "payloadTooLarge",
                    "shell IPC request exceeded size limit",
                    Instant::now(),
                    json!({}),
                ),
            );
        }
        if request.contains(&b'\n') {
            break;
        }
    }

    let first_line = request
        .split(|byte| *byte == b'\n')
        .next()
        .unwrap_or_default();
    let raw = String::from_utf8_lossy(first_line);
    let response = handle_shell_ipc_request(raw.trim(), expected_token);
    write_response(pipe, &response)
}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PipeReadChunkStatus {
    Complete,
    MoreData,
    Finished,
}

#[cfg(windows)]
fn pipe_read_chunk_status(
    ok: i32,
    bytes_read: u32,
    error_code: u32,
) -> Result<PipeReadChunkStatus, String> {
    use windows_sys::Win32::Foundation::ERROR_MORE_DATA;

    if ok != 0 {
        return if bytes_read == 0 {
            Ok(PipeReadChunkStatus::Finished)
        } else {
            Ok(PipeReadChunkStatus::Complete)
        };
    }

    if error_code == ERROR_MORE_DATA && bytes_read > 0 {
        return Ok(PipeReadChunkStatus::MoreData);
    }

    Err(format!("ReadFile failed with {error_code}"))
}

#[cfg(windows)]
fn write_response(
    pipe: windows_sys::Win32::Foundation::HANDLE,
    response: &str,
) -> Result<(), String> {
    use std::ptr;
    use windows_sys::Win32::Storage::FileSystem::WriteFile;

    let bytes = response.as_bytes();
    let mut written = 0u32;
    let ok = unsafe {
        WriteFile(
            pipe,
            bytes.as_ptr(),
            bytes.len() as u32,
            &mut written,
            ptr::null_mut(),
        )
    };
    if ok == 0 || written as usize != bytes.len() {
        return Err("WriteFile failed for shell IPC response".to_string());
    }
    Ok(())
}

#[cfg(windows)]
fn wake_pipe_server(pipe_name: &str) {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GENERIC_READ, GENERIC_WRITE, INVALID_HANDLE_VALUE},
        Storage::FileSystem::{CreateFileW, OPEN_EXISTING},
    };

    let name: Vec<u16> = OsStr::new(pipe_name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let handle = unsafe {
        CreateFileW(
            name.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            0,
            ptr::null(),
            OPEN_EXISTING,
            0,
            ptr::null_mut(),
        )
    };
    if handle != INVALID_HANDLE_VALUE {
        unsafe {
            CloseHandle(handle);
        }
    }
}

#[cfg(not(windows))]
fn wake_pipe_server(_pipe_name: &str) {}

#[cfg(test)]
mod tests {
    use super::{
        handle_shell_ipc_request, response_line, shell_ipc_pipe_security_sddl, ShellIpcConfig,
        API_VERSION,
    };
    use serde_json::json;
    use std::time::Instant;

    #[test]
    fn shell_ipc_config_uses_private_pipe_and_token() {
        let config = ShellIpcConfig::new();

        assert!(config.pipe_name.starts_with(r"\\.\pipe\scriber-shell-"));
        assert!(config.token.len() >= 32);
        assert!(!config.pipe_name.contains(&config.token));
        assert_ne!(config.pipe_name_hash(), config.pipe_name);
        assert_eq!(config.pipe_name_hash().len(), 16);
    }

    #[test]
    fn shell_ipc_pipe_security_sddl_is_restricted() {
        let sddl = shell_ipc_pipe_security_sddl(None);

        assert!(sddl.starts_with("D:P"));
        assert!(sddl.contains("(A;;GA;;;SY)"));
        assert!(sddl.contains("(A;;GA;;;BA)"));
        assert!(sddl.contains("(A;;GA;;;OW)"));
        assert!(!sddl.contains("WD"));
        assert!(!sddl.contains("AU"));
        assert!(!sddl.contains("IU"));
    }

    #[test]
    fn shell_ipc_pipe_security_sddl_can_use_logon_sid() {
        let sddl = shell_ipc_pipe_security_sddl(Some("S-1-5-5-123-456"));

        assert!(sddl.contains("(A;;GA;;;SY)"));
        assert!(sddl.contains("(A;;GA;;;BA)"));
        assert!(sddl.contains("(A;;GA;;;S-1-5-5-123-456)"));
        assert!(!sddl.contains("(A;;GA;;;OW)"));
    }

    #[test]
    fn clipboard_owner_class_is_named_for_diagnostics() {
        assert_eq!(super::CLIPBOARD_OWNER_CLASS, "ScriberClipboardOwner");
    }

    #[test]
    fn shell_ipc_ping_requires_valid_token() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r1",
            "command": "ping",
            "token": "secret",
            "payload": {}
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r1");
        assert_eq!(value["success"], true);
        assert_eq!(value["payload"]["pong"], true);
    }

    #[test]
    fn shell_ipc_rejects_bad_token() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r2",
            "command": "ping",
            "token": "wrong",
            "payload": {}
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r2");
        assert_eq!(value["success"], false);
        assert_eq!(value["errorCode"], "unauthorized");
    }

    #[test]
    fn shell_ipc_capabilities_are_explicitly_limited() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r3",
            "command": "capabilities",
            "token": "secret",
            "payload": {}
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["success"], true);
        assert_eq!(value["payload"]["textInjection"], true);
        assert_eq!(value["payload"]["nativeDeviceEventsStatus"], true);
        assert_eq!(value["payload"]["audioEndpointInventory"], true);
        assert_eq!(value["payload"]["audioProbe"], true);
        assert_eq!(value["payload"]["audioCapturePrototype"], false);
        assert_eq!(value["payload"]["audioPrewarmPrototype"], false);
        assert_eq!(value["payload"]["audioFrameProtocol"]["version"], 1);
        assert_eq!(value["payload"]["commands"][0], "ping");
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "injectText"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "nativeDeviceEventsStatus"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "audioEndpointInventory"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "audioProbe"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "audioCaptureStart"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "audioPrewarmStart"));
    }

    #[test]
    fn shell_ipc_native_device_event_status_is_available() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r-native-events",
            "command": "nativeDeviceEventsStatus",
            "token": "secret",
            "payload": {}
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r-native-events");
        assert_eq!(value["success"], true);
        assert_eq!(value["payload"]["source"], "tauri");
        assert_eq!(value["payload"]["monitorKind"], "wasapi-imm-notification");
        assert!(value["payload"].get("eventCount").is_some());
    }

    #[test]
    fn shell_ipc_inject_text_rejects_missing_text_before_os_access() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r-inject",
            "command": "injectText",
            "token": "secret",
            "payload": {
                "restoreClipboard": true,
            }
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r-inject");
        assert_eq!(value["success"], false);
        assert_eq!(value["errorCode"], "invalidPayload");
    }

    #[test]
    fn parse_inject_text_options_clamps_retry_and_delay_values() {
        let payload = json!({
            "text": "hello",
            "restoreClipboard": false,
            "restoreDelayMs": 999_999,
            "preDelayMs": 999_999,
            "preDelayMode": "auto",
            "dispatch": "ctrlV",
            "maxClipboardRetries": 999,
            "clipboardRetryDelayMs": 999_999,
            "deadlineMs": 999_999,
        });

        let options = super::parse_inject_text_options(&payload).unwrap();

        assert_eq!(options.text, "hello");
        assert!(!options.restore_clipboard);
        assert_eq!(options.restore_delay_ms, 30_000);
        assert_eq!(options.pre_delay_ms, 5_000);
        assert_eq!(options.pre_delay_mode, "auto");
        assert_eq!(options.max_clipboard_retries, 50);
        assert_eq!(options.clipboard_retry_delay_ms, 500);
        assert_eq!(options.deadline_ms, 30_000);
    }

    #[test]
    fn parse_inject_text_options_rejects_unknown_pre_delay_mode() {
        let payload = json!({
            "text": "hello",
            "dispatch": "ctrlV",
            "preDelayMode": "guess",
        });

        let err = super::parse_inject_text_options(&payload).unwrap_err();

        assert_eq!(err.code, "invalidPreDelayMode");
    }

    #[test]
    fn auto_pre_delay_policy_uses_rust_foreground_title_only_for_slow_apps() {
        let mut options = super::parse_inject_text_options(&json!({
            "text": "hello",
            "dispatch": "ctrlV",
            "preDelayMode": "auto",
            "preDelayMs": 80,
        }))
        .unwrap();

        assert_eq!(
            super::resolve_pre_delay_ms(&options, Some("Quarterly Report - Word")),
            80
        );
        assert_eq!(
            super::resolve_pre_delay_ms(&options, Some("Inbox - Outlook")),
            80
        );
        assert_eq!(
            super::resolve_pre_delay_ms(&options, Some("Scriber - Notepad")),
            0
        );
        assert_eq!(super::resolve_pre_delay_ms(&options, None), 0);

        options.pre_delay_mode = "fixed".to_string();
        assert_eq!(
            super::resolve_pre_delay_ms(&options, Some("Scriber - Notepad")),
            80
        );
    }

    #[test]
    fn parse_audio_probe_options_clamps_and_normalizes_payload() {
        let payload = json!({
            "sampleRate": 999_999,
            "channels": 64,
            "blockSize": 99_999,
            "devicePreference": "default-capture-device-with-a-longer-than-needed-label",
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        });

        let options = super::parse_audio_probe_options(&payload).unwrap();

        assert_eq!(options.requested_sample_rate, 192_000);
        assert_eq!(options.requested_channels, 16);
        assert_eq!(options.block_size, 16_384);
        assert_eq!(options.port_audio_label, "Default Mic, Windows WASAPI");
        assert_eq!(options.native_endpoint_id_hash, "endpoint-hash");
        assert!(options
            .device_preference
            .starts_with("default-capture-device"));
    }

    #[test]
    fn parse_audio_probe_options_rejects_non_object_payload() {
        let err = super::parse_audio_probe_options(&json!("bad")).unwrap_err();

        assert_eq!(err.code, "invalidPayload");
    }

    #[test]
    fn parse_audio_capture_start_options_clamps_payload() {
        let payload = json!({
            "sampleRate": 999_999,
            "channels": 32,
            "blockSize": 99_999,
            "devicePreference": "default-capture-device-with-a-longer-than-needed-label",
            "prebufferMs": 999_999,
        });

        let options = super::parse_audio_capture_start_options(&payload).unwrap();

        assert_eq!(options.sample_rate, 192_000);
        assert_eq!(options.channels, 16);
        assert_eq!(options.block_size, 16_384);
        assert_eq!(options.prebuffer_ms, 2_000);
        assert_eq!(options.native_endpoint_id_hash, "");
        assert!(options
            .device_preference
            .starts_with("default-capture-device"));
    }

    #[test]
    fn shell_ipc_audio_capture_start_returns_explicit_unavailable() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r-audio-start",
            "command": "audioCaptureStart",
            "token": "secret",
            "payload": {
                "sampleRate": 16000,
                "channels": 1,
                "blockSize": 512,
                "devicePreference": "default",
                "prebufferMs": 0,
                "prewarmId": "prewarm-adopt-1",
            }
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r-audio-start");
        assert_eq!(value["success"], false);
        assert_eq!(value["errorCode"], "audioCaptureUnavailable");
        assert_eq!(value["payload"]["engine"], "rust-prototype");
        assert_eq!(
            value["payload"]["requestedFormat"]["prewarmId"],
            "prewarm-adopt-1"
        );
        assert_eq!(
            value["payload"]["frameProtocol"]["sampleFormat"],
            "pcm_i16_le"
        );
    }

    #[test]
    fn shell_ipc_audio_prewarm_start_returns_explicit_status() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r-audio-prewarm-start",
            "command": "audioPrewarmStart",
            "token": "secret",
            "payload": {
                "sampleRate": 16000,
                "channels": 1,
                "blockSize": 512,
                "devicePreference": "default",
                "prebufferMs": 400,
            }
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r-audio-prewarm-start");
        assert_eq!(value["success"], false);
        assert!(
            matches!(
                value["errorCode"].as_str(),
                Some("audioPrewarmUnavailable" | "unknownCommand")
            ),
            "unexpected prewarm start error: {}",
            value["errorCode"]
        );
        assert_eq!(value["payload"]["engine"], "rust-prototype");
        assert_eq!(value["payload"]["requestedFormat"]["prebufferMs"], 400);
        assert_eq!(
            value["payload"]["frameProtocol"]["sampleFormat"],
            "pcm_i16_le"
        );
    }

    #[test]
    fn audio_capture_shell_payload_exposes_sidecar_stream_contract_top_level() {
        let options = super::AudioCaptureStartOptions {
            sample_rate: 16_000,
            channels: 1,
            block_size: 512,
            device_preference: "default".to_string(),
            port_audio_label: "Default Mic, Windows WASAPI".to_string(),
            native_endpoint_id_hash: "endpoint-hash".to_string(),
            prebuffer_ms: 0,
            prewarm_id: "prewarm-1".to_string(),
        };
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "streamId": "stream-1",
                "framePipe": r"\\.\pipe\scriber-audio-test",
                "sampleRate": 16_000,
                "channels": 1,
                "captureChannels": 1,
                "sampleFormat": "pcm_i16_le",
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(1234),
        };

        let payload = super::audio_capture_shell_payload(&options, result.payload.clone(), &result);

        assert_eq!(payload["streamId"], "stream-1");
        assert_eq!(payload["framePipe"], r"\\.\pipe\scriber-audio-test");
        assert_eq!(payload["sampleFormat"], "pcm_i16_le");
        assert_eq!(
            payload["requestedFormat"]["portAudioLabel"],
            "Default Mic, Windows WASAPI"
        );
        assert_eq!(
            payload["requestedFormat"]["nativeEndpointIdHash"],
            "endpoint-hash"
        );
        assert_eq!(payload["requestedFormat"]["prewarmId"], "prewarm-1");
        assert_eq!(payload["sidecar"]["pid"], 1234);
        assert_eq!(payload["sidecarPayload"]["streamId"], "stream-1");
    }

    #[test]
    fn audio_prewarm_stop_shell_payload_preserves_sidecar_health_fields() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "stopped": true,
                "prewarmId": "prewarm-1",
                "reason": "prewarmStop",
                "totalBlocksObserved": 8,
                "bufferedAudioFrames": 2048,
                "prewarmError": null,
                "sidecarUptimeMs": 123,
                "exitStatus": 0,
                "sidecarKilledAfterTimeout": false,
                "sidecarWaitError": null,
                "sidecarPid": 9876,
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };

        let payload = super::audio_prewarm_stop_shell_payload(result.payload.clone(), &result);

        assert_eq!(payload["engine"], "rust-prototype");
        assert_eq!(payload["stopped"], true);
        assert_eq!(payload["prewarmId"], "prewarm-1");
        assert_eq!(payload["totalBlocksObserved"], 8);
        assert_eq!(payload["bufferedAudioFrames"], 2048);
        assert_eq!(payload["sidecarUptimeMs"], 123);
        assert_eq!(payload["exitStatus"], 0);
        assert_eq!(payload["sidecarKilledAfterTimeout"], false);
        assert!(payload["sidecarWaitError"].is_null());
        assert_eq!(payload["sidecar"]["pid"], 9876);
        assert_eq!(payload["sidecarPayload"]["prewarmId"], "prewarm-1");
    }

    #[test]
    fn audio_capture_stop_shell_payload_preserves_sidecar_health_fields() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "stopped": true,
                "streamId": "stream-1",
                "reason": "captureStop",
                "connected": true,
                "framesWritten": 42,
                "bytesWritten": 13_440,
                "writerError": null,
                "sidecarUptimeMs": 123,
                "exitStatus": 0,
                "sidecarKilledAfterTimeout": false,
                "sidecarWaitError": null,
                "sidecarPid": 9876,
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };

        let payload = super::audio_capture_stop_shell_payload(result.payload.clone(), &result);

        assert_eq!(payload["engine"], "rust-prototype");
        assert_eq!(payload["stopped"], true);
        assert_eq!(payload["framesWritten"], 42);
        assert_eq!(payload["bytesWritten"], 13_440);
        assert_eq!(payload["sidecarUptimeMs"], 123);
        assert_eq!(payload["exitStatus"], 0);
        assert_eq!(payload["sidecarKilledAfterTimeout"], false);
        assert!(payload["sidecarWaitError"].is_null());
        assert_eq!(payload["sidecar"]["pid"], 9876);
        assert_eq!(payload["sidecarPayload"]["streamId"], "stream-1");
    }

    #[test]
    fn shell_ipc_audio_capture_stop_is_idempotent_until_sidecar_exists() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "r-audio-stop",
            "command": "audioCaptureStop",
            "token": "secret",
            "payload": {
                "streamId": "stream-1",
            }
        })
        .to_string();

        let response = handle_shell_ipc_request(&request, "secret");
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(value["requestId"], "r-audio-stop");
        assert_eq!(value["success"], true);
        assert_eq!(value["payload"]["stopped"], false);
        assert!(
            matches!(
                value["payload"]["reason"].as_str(),
                Some("noRustAudioSidecar" | "noActiveCapture")
            ),
            "unexpected stop reason: {}",
            value["payload"]["reason"]
        );
    }

    #[test]
    fn parse_inject_text_options_rejects_embedded_nul() {
        let payload = json!({
            "text": "hello\u{0}world",
            "dispatch": "ctrlV",
        });

        let err = super::parse_inject_text_options(&payload).unwrap_err();

        assert_eq!(err.code, "invalidPayload");
    }

    #[test]
    fn inject_text_byte_budget_fits_inside_request_budget_with_overhead() {
        assert!(super::MAX_INJECT_TEXT_BYTES + 8192 < super::MAX_REQUEST_BYTES);
    }

    #[cfg(windows)]
    #[test]
    fn shell_ipc_pipe_read_accepts_message_mode_partial_chunks() {
        use windows_sys::Win32::Foundation::ERROR_MORE_DATA;

        assert_eq!(
            super::pipe_read_chunk_status(0, 4096, ERROR_MORE_DATA).unwrap(),
            super::PipeReadChunkStatus::MoreData
        );
        assert_eq!(
            super::pipe_read_chunk_status(1, 128, 0).unwrap(),
            super::PipeReadChunkStatus::Complete
        );
        assert_eq!(
            super::pipe_read_chunk_status(1, 0, 0).unwrap(),
            super::PipeReadChunkStatus::Finished
        );
        assert!(super::pipe_read_chunk_status(0, 0, ERROR_MORE_DATA).is_err());
    }

    #[test]
    fn deadline_budget_rejects_side_effect_when_remaining_time_is_too_short() {
        let payload = json!({
            "text": "hello",
            "dispatch": "ctrlV",
            "deadlineMs": 1,
        });
        let options = super::parse_inject_text_options(&payload).unwrap();

        std::thread::sleep(std::time::Duration::from_millis(2));
        let err = super::ensure_deadline_budget(
            &options,
            Instant::now() - std::time::Duration::from_millis(2),
            25,
            "deadlineBeforeSet",
            || json!({"partial": true}),
        )
        .unwrap_err();

        assert_eq!(err.code, "deadlineBeforeSet");
        assert_eq!(err.payload["partial"], true);
    }

    #[test]
    fn response_line_is_newline_delimited_json() {
        let response = response_line("r4", true, "", "", Instant::now(), json!({"ok": true}));

        assert!(response.ends_with('\n'));
        let value: serde_json::Value = serde_json::from_str(response.trim()).unwrap();
        assert_eq!(value["apiVersion"], API_VERSION);
        assert_eq!(value["requestId"], "r4");
        assert!(value["timingsMs"]["total"].as_f64().unwrap() >= 0.0);
    }
}
