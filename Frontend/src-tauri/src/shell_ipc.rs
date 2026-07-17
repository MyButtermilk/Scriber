use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, MutexGuard, OnceLock,
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
use crate::audio_sidecar_client::{
    audio_sidecar_executable_available, call_audio_sidecar_command, AudioSidecarCallResult,
};

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
const CLIENT_RESPONSE_ACK_TIMEOUT_MS: u64 = 1_000;
const MAX_RESPONSE_ACK_BYTES: usize = 1_024;
const SHELL_IPC_CLIENT_WORKER_LIMIT: usize = 6;
const SHELL_IPC_PIPE_INSTANCE_LIMIT: u32 = (SHELL_IPC_CLIENT_WORKER_LIMIT as u32) + 1;
const SHELL_IPC_SHUTDOWN_GRACE_MS: u64 = 1_000;
const SHELL_IPC_SERVER_JOIN_GRACE_MS: u64 = 1_250;

#[derive(Debug, Clone)]
struct InjectTextOptions {
    text: String,
    expected_foreground_title: String,
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

fn inject_text_mutation_lock() -> MutexGuard<'static, ()> {
    static LANE: OnceLock<Mutex<()>> = OnceLock::new();
    LANE.get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
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
        if let Some(join_handle) = self.join_handle.take() {
            let deadline = Instant::now() + Duration::from_millis(SHELL_IPC_SERVER_JOIN_GRACE_MS);
            while !join_handle.is_finished() && Instant::now() < deadline {
                // Retrying the wake closes the small race where the accept loop is between pipe
                // instances when shutdown starts. Never turn Drop into an unbounded join.
                wake_pipe_server(&self.pipe_name);
                thread::sleep(Duration::from_millis(5));
            }
            if join_handle.is_finished() {
                let _ = join_handle.join();
            }
        } else {
            wake_pipe_server(&self.pipe_name);
        }
    }
}

pub fn start_shell_ipc_server<L>(
    config: ShellIpcConfig,
    log: L,
) -> Result<Option<ShellIpcServerHandle>, String>
where
    L: FnMut(String) + Send + 'static,
{
    start_shell_ipc_server_impl(config, log)
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
    contain_shell_ipc_request_panic(raw, || {
        handle_shell_ipc_request_unchecked(raw, expected_token)
    })
}

fn contain_shell_ipc_request_panic<F>(raw: &str, dispatch: F) -> String
where
    F: FnOnce() -> String,
{
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(dispatch)) {
        Ok(response) => response,
        Err(_) => {
            let request_id = serde_json::from_str::<Value>(raw)
                .ok()
                .and_then(|request| {
                    request
                        .get("requestId")
                        .and_then(Value::as_str)
                        .map(|value| value.chars().take(128).collect::<String>())
                })
                .unwrap_or_default();
            response_line(
                &request_id,
                false,
                "internalCommandPanic",
                "shell command failed internally",
                Instant::now(),
                json!({}),
            )
        }
    }
}

fn handle_shell_ipc_request_unchecked(raw: &str, expected_token: &str) -> String {
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
                    "audioPrewarmStatus",
                    "audioPrewarmStop",
                    "audioMeetingStart",
                    "audioMeetingStatus",
                    "audioMeetingPause",
                    "audioMeetingResume",
                    "audioMeetingStop",
                    "meetingDetectionStatus",
                    "outlookCredentialStore",
                    "outlookCredentialStatus",
                    "outlookCredentialDelete",
                    "outlookAuthorizationCodeExchange",
                    "outlookTokenAcquire",
                    "overlayPrepare",
                    "overlayShow",
                    "overlayHide",
                    "overlayAudioLevel",
                    "overlayStatus",
                ],
                "textInjection": true,
                "nativeDeviceEventsStatus": true,
                "audioEndpointInventory": true,
                "audioProbe": true,
                "audioCapturePrototype": false,
                "audioPrewarmPrototype": false,
                "audioMeetingCapture": true,
                "meetingDetection": cfg!(windows),
                "audioSidecar": {
                    "executableAvailable": audio_sidecar_executable_available(),
                    "stdioProtocolVersion": "1",
                },
                "nativeOverlay": {
                    "renderer": "tauri-webview",
                    "windowLabel": crate::native_overlay::OVERLAY_WINDOW_LABEL,
                },
                "audioFrameProtocol": audio_frame_protocol_payload(),
            }),
        ),
        "injectText" => {
            let _mutation_guard = inject_text_mutation_lock();
            match inject_text(payload) {
                Ok(payload) => response_line(request_id, true, "", "", started, payload),
                Err(err) => response_line(
                    request_id,
                    false,
                    err.code,
                    &err.reason,
                    started,
                    err.payload,
                ),
            }
        }
        "nativeDeviceEventsStatus" => response_line(
            request_id,
            true,
            "",
            "",
            started,
            native_device_event_status_payload(),
        ),
        "meetingDetectionStatus" => match detect_meeting_context() {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(err) => response_line(
                request_id,
                false,
                "meetingDetectionUnavailable",
                &err,
                started,
                json!({}),
            ),
        },
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
        "overlayPrepare" | "overlayShow" | "overlayHide" | "overlayAudioLevel"
        | "overlayStatus" => {
            match crate::native_overlay::handle_shell_command_on_ui_thread(command, payload) {
                Ok(payload) => response_line(request_id, true, "", "", started, payload),
                Err(err) => response_line(
                    request_id,
                    false,
                    "overlayUnavailable",
                    &err,
                    started,
                    json!({
                        "renderer": "tauri-webview",
                        "windowLabel": crate::native_overlay::OVERLAY_WINDOW_LABEL,
                    }),
                ),
            }
        }
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
                audio_sidecar_result_response_line(request_id, started, payload, &result)
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
                audio_sidecar_result_response_line(request_id, started, payload, &result)
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
        "audioPrewarmStatus" => match parse_audio_prewarm_stop_payload(payload) {
            Ok(prewarm_id) => {
                let result = call_audio_sidecar_command(
                    "prewarmStatus",
                    json!({
                        "prewarmId": prewarm_id,
                    }),
                );
                let payload = audio_prewarm_status_shell_payload(result.payload.clone(), &result);
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
        "audioMeetingStart" | "audioMeetingResume" => match start_meeting_audio(payload) {
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
        "audioMeetingStatus" => match status_meeting_audio(payload) {
            Ok((payload, result)) => {
                audio_sidecar_result_response_line(request_id, started, payload, &result)
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
        "audioMeetingPause" | "audioMeetingStop" => match stop_meeting_audio(payload) {
            Ok((payload, result)) => {
                audio_sidecar_result_response_line(request_id, started, payload, &result)
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
        "outlookCredentialStore" => match outlook_credential_store(payload) {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(reason) => response_line(
                request_id,
                false,
                "outlookCredentialStoreFailed",
                &reason,
                started,
                json!({}),
            ),
        },
        "outlookCredentialStatus" => match outlook_credential_status() {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(reason) => response_line(
                request_id,
                false,
                "outlookCredentialStatusFailed",
                &reason,
                started,
                json!({}),
            ),
        },
        "outlookCredentialDelete" => match outlook_credential_delete() {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(reason) => response_line(
                request_id,
                false,
                "outlookCredentialDeleteFailed",
                &reason,
                started,
                json!({}),
            ),
        },
        "outlookAuthorizationCodeExchange" => match outlook_token_request(payload, true) {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(reason) => response_line(
                request_id,
                false,
                "outlookAuthorizationFailed",
                &reason,
                started,
                json!({}),
            ),
        },
        "outlookTokenAcquire" => match outlook_token_request(payload, false) {
            Ok(payload) => response_line(request_id, true, "", "", started, payload),
            Err(reason) => response_line(
                request_id,
                false,
                "outlookTokenAcquireFailed",
                &reason,
                started,
                json!({}),
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

const OUTLOOK_CREDENTIAL_TARGET: &str = "Scriber.Outlook.RefreshToken.v1";

#[cfg(windows)]
fn outlook_credential_store(payload: &Value) -> Result<Value, String> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::Credentials::{
        CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };
    let refresh_token = payload
        .get("refreshToken")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if refresh_token.is_empty() || refresh_token.len() > 2400 || refresh_token.contains('\0') {
        return Err(
            "Outlook refresh token is missing or exceeds the Credential Manager limit".to_string(),
        );
    }
    let mut target: Vec<u16> = OUTLOOK_CREDENTIAL_TARGET
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let mut username: Vec<u16> = "Scriber Outlook".encode_utf16().chain(Some(0)).collect();
    let mut blob = refresh_token.as_bytes().to_vec();
    let credential = CREDENTIALW {
        Type: CRED_TYPE_GENERIC,
        TargetName: target.as_mut_ptr(),
        CredentialBlobSize: blob.len() as u32,
        CredentialBlob: blob.as_mut_ptr(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        UserName: username.as_mut_ptr(),
        Comment: null_mut(),
        Attributes: null_mut(),
        TargetAlias: null_mut(),
        ..Default::default()
    };
    let written = unsafe { CredWriteW(&credential, 0) };
    blob.fill(0);
    if written == 0 {
        return Err(format!(
            "Windows Credential Manager write failed: {}",
            unsafe { windows_sys::Win32::Foundation::GetLastError() }
        ));
    }
    Ok(json!({"stored": true, "targetHash": hash_sensitive_identifier(OUTLOOK_CREDENTIAL_TARGET)}))
}

#[cfg(not(windows))]
fn outlook_credential_store(_payload: &Value) -> Result<Value, String> {
    Err("Outlook Credential Manager storage is only supported on Windows".to_string())
}

#[cfg(windows)]
fn outlook_read_refresh_token() -> Result<String, String> {
    use std::{ptr::null_mut, slice};
    use windows_sys::Win32::Security::Credentials::{
        CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
    };
    let target: Vec<u16> = OUTLOOK_CREDENTIAL_TARGET
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let mut credential: *mut CREDENTIALW = null_mut();
    let found = unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) };
    if found == 0 {
        return Err("Outlook is not connected".to_string());
    }
    let value = unsafe {
        let item = &*credential;
        let bytes = slice::from_raw_parts(item.CredentialBlob, item.CredentialBlobSize as usize);
        String::from_utf8(bytes.to_vec())
            .map_err(|_| "Stored Outlook credential is invalid".to_string())
    };
    unsafe { CredFree(credential.cast()) };
    value
}

#[cfg(windows)]
fn outlook_token_request(payload: &Value, authorization_code: bool) -> Result<Value, String> {
    const SCOPES: &str = "User.Read Calendars.Read offline_access";
    let client_id = payload
        .get("clientId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let Some(client_id) = crate::outlook_config::normalize_client_id(client_id) else {
        return Err("Outlook public client ID is missing or invalid".to_string());
    };
    let mut form = vec![("client_id", client_id), ("scope", SCOPES.to_string())];
    let mut consumed_refresh_token = None;
    if authorization_code {
        let code = payload
            .get("code")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let verifier = payload
            .get("codeVerifier")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let redirect_uri = payload
            .get("redirectUri")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if code.is_empty()
            || code.len() > 4096
            || verifier.len() < 43
            || verifier.len() > 128
            || !crate::outlook_config::is_valid_redirect_uri(redirect_uri)
        {
            return Err("Outlook authorization response is invalid".to_string());
        }
        form.extend([
            ("grant_type", "authorization_code".to_string()),
            ("code", code.to_string()),
            ("code_verifier", verifier.to_string()),
            ("redirect_uri", redirect_uri.to_string()),
        ]);
    } else {
        let refresh_token = outlook_read_refresh_token()?;
        form.extend([
            ("grant_type", "refresh_token".to_string()),
            ("refresh_token", refresh_token.clone()),
        ]);
        consumed_refresh_token = Some(refresh_token);
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Outlook token client initialization failed".to_string())?;
    let response = client
        .post("https://login.microsoftonline.com/common/oauth2/v2.0/token")
        .form(&form)
        .send()
        .map_err(|error| {
            format!(
                "Outlook token request failed: {}",
                error.status().map(|v| v.as_u16()).unwrap_or(0)
            )
        })?;
    let status = response.status();
    let token: Value = response
        .json()
        .map_err(|_| "Outlook token response was invalid".to_string())?;
    if !status.is_success() {
        let code = token
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("token_request_failed");
        return Err(format!(
            "Outlook token endpoint rejected the request ({code})"
        ));
    }
    let access_token = token
        .get("access_token")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if access_token.is_empty() {
        return Err("Outlook token response omitted the access token".to_string());
    }
    if let Some(refresh_token) = token.get("refresh_token").and_then(Value::as_str) {
        outlook_credential_store(&json!({"refreshToken": refresh_token}))?;
    } else if authorization_code {
        return Err("Outlook authorization did not issue an offline refresh token".to_string());
    }
    if let Some(mut value) = consumed_refresh_token {
        unsafe {
            value.as_bytes_mut().fill(0);
        }
    }
    Ok(json!({
        "accessToken": access_token,
        "expiresIn": token.get("expires_in").and_then(Value::as_u64).unwrap_or(0),
        "scope": token.get("scope").and_then(Value::as_str).unwrap_or(SCOPES),
        "tokenType": token.get("token_type").and_then(Value::as_str).unwrap_or("Bearer"),
    }))
}

#[cfg(not(windows))]
fn outlook_token_request(_payload: &Value, _authorization_code: bool) -> Result<Value, String> {
    Err("Outlook token acquisition is only supported on Windows".to_string())
}

#[cfg(windows)]
fn outlook_credential_status() -> Result<Value, String> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::Credentials::{
        CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
    };
    let target: Vec<u16> = OUTLOOK_CREDENTIAL_TARGET
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let mut credential: *mut CREDENTIALW = null_mut();
    let found = unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) };
    if found == 0 {
        let error = unsafe { windows_sys::Win32::Foundation::GetLastError() };
        if error == 1168 {
            return Ok(json!({"connected": false, "credentialStored": false}));
        }
        return Err(format!("Windows Credential Manager read failed: {error}"));
    }
    unsafe { CredFree(credential.cast()) };
    Ok(json!({
        "connected": true,
        "credentialStored": true,
        "targetHash": hash_sensitive_identifier(OUTLOOK_CREDENTIAL_TARGET),
    }))
}

#[cfg(not(windows))]
fn outlook_credential_status() -> Result<Value, String> {
    Ok(json!({"connected": false, "credentialStored": false, "reason": "unsupportedPlatform"}))
}

#[cfg(windows)]
fn outlook_credential_delete() -> Result<Value, String> {
    use windows_sys::Win32::Security::Credentials::{CredDeleteW, CRED_TYPE_GENERIC};
    let target: Vec<u16> = OUTLOOK_CREDENTIAL_TARGET
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let deleted = unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) };
    if deleted == 0 {
        let error = unsafe { windows_sys::Win32::Foundation::GetLastError() };
        if error != 1168 {
            return Err(format!("Windows Credential Manager delete failed: {error}"));
        }
    }
    Ok(json!({"deleted": true}))
}

#[cfg(not(windows))]
fn outlook_credential_delete() -> Result<Value, String> {
    Ok(json!({"deleted": true}))
}

#[cfg(windows)]
fn detect_meeting_context() -> Result<Value, String> {
    use windows::core::Interface;
    use windows::Win32::{
        Media::Audio::{
            eConsole, eRender, AudioSessionStateActive, IAudioSessionControl2,
            IAudioSessionManager2, IMMDeviceEnumerator, MMDeviceEnumerator,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED,
        },
    };
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::Threading::{
            OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
        },
        UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId},
    };

    fn process_family(pid: u32) -> String {
        if pid == 0 {
            return String::new();
        }
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return String::new();
        }
        let mut buffer = vec![0u16; 1024];
        let mut len = buffer.len() as u32;
        let ok = unsafe { QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut len) };
        unsafe { CloseHandle(handle) };
        if ok == 0 || len == 0 {
            return String::new();
        }
        let path = String::from_utf16_lossy(&buffer[..len as usize]).to_ascii_lowercase();
        if path.ends_with("teams.exe") || path.contains("ms-teams") {
            "teams".to_string()
        } else if path.ends_with("zoom.exe") {
            "zoom".to_string()
        } else if path.ends_with("webex.exe") || path.contains("webexhost") {
            "webex".to_string()
        } else if path.ends_with("chrome.exe")
            || path.ends_with("msedge.exe")
            || path.ends_with("firefox.exe")
        {
            "browser".to_string()
        } else {
            String::new()
        }
    }

    let hwnd = unsafe { GetForegroundWindow() };
    if hwnd.is_null() {
        return Ok(json!({"detected": false, "reason": "noForegroundWindow"}));
    }
    let mut foreground_pid = 0u32;
    unsafe { GetWindowThreadProcessId(hwnd, &mut foreground_pid) };
    let mut title_buffer = [0u16; 512];
    let title_len = unsafe {
        GetWindowTextW(hwnd, title_buffer.as_mut_ptr(), title_buffer.len() as i32)
    }
    .max(0) as usize;
    let title = String::from_utf16_lossy(&title_buffer[..title_len]).to_ascii_lowercase();
    let foreground_family = process_family(foreground_pid);
    let label = if foreground_family == "teams" || title.contains("microsoft teams") {
        "Microsoft Teams"
    } else if foreground_family == "zoom" || title.contains("zoom meeting") {
        "Zoom"
    } else if foreground_family == "webex" || title.contains("webex") {
        "Webex"
    } else if foreground_family == "browser"
        && (title.contains("google meet") || title.contains("meet.google.com"))
    {
        "Google Meet"
    } else {
        ""
    };
    if label.is_empty() {
        return Ok(json!({
            "detected": false,
            "reason": "foregroundNotMeeting",
            "windowHash": hash_sensitive_identifier(&format!("{hwnd:p}")),
        }));
    }

    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|err| format!("COM initialization failed: {err}"))?;
    let render_result = (|| -> Result<bool, String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let device = unsafe { enumerator.GetDefaultAudioEndpoint(eRender, eConsole) }
            .map_err(|err| format!("default render endpoint unavailable: {err}"))?;
        let manager: IAudioSessionManager2 = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|err| format!("audio session manager unavailable: {err}"))?;
        let sessions = unsafe { manager.GetSessionEnumerator() }
            .map_err(|err| format!("audio session enumeration unavailable: {err}"))?;
        let count = unsafe { sessions.GetCount() }
            .map_err(|err| format!("audio session count unavailable: {err}"))?;
        for index in 0..count {
            let control = unsafe { sessions.GetSession(index) }
                .map_err(|err| format!("audio session item unavailable: {err}"))?;
            if unsafe { control.GetState() }.ok() != Some(AudioSessionStateActive) {
                continue;
            }
            let Ok(control2) = control.cast::<IAudioSessionControl2>() else {
                continue;
            };
            let Ok(pid) = (unsafe { control2.GetProcessId() }) else {
                continue;
            };
            let family = process_family(pid);
            if pid == foreground_pid
                || (!foreground_family.is_empty() && family == foreground_family)
            {
                return Ok(true);
            }
        }
        Ok(false)
    })();
    unsafe { CoUninitialize() };
    let active_render_session = render_result?;
    Ok(json!({
        "detected": active_render_session,
        "candidate": true,
        "label": label,
        "source": "windowAndRenderSession",
        "activeRenderSession": active_render_session,
        "windowHash": hash_sensitive_identifier(&format!("{hwnd:p}")),
        "processHash": hash_sensitive_identifier(&foreground_pid.to_string()),
        "reason": if active_render_session { "meetingWindowWithActiveRender" } else { "meetingWindowWithoutActiveRender" },
    }))
}

#[cfg(not(windows))]
fn detect_meeting_context() -> Result<Value, String> {
    Ok(json!({"detected": false, "reason": "unsupportedPlatform"}))
}

fn start_meeting_audio(payload: &Value) -> Result<Value, ShellCommandError> {
    if !payload.is_object() {
        return Err(ShellCommandError::new(
            "invalidMeetingCapturePayload",
            "audioMeetingStart payload must be an object",
        ));
    }
    let meeting_id = payload
        .get("meetingId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if meeting_id.is_empty() || meeting_id.len() > 96 {
        return Err(ShellCommandError::new(
            "invalidMeetingId",
            "audioMeetingStart requires a bounded meetingId",
        ));
    }
    let request = json!({
        "sampleRate": 16_000,
        "channels": 1,
        "blockSize": 160,
        "devicePreference": "default",
        "prebufferMs": 0,
        "aecEnabled": payload.get("aecEnabled").and_then(Value::as_bool).unwrap_or(true),
        "aecDelayMs": payload.get("aecDelayMs").and_then(Value::as_i64).unwrap_or(80),
        "microphoneNativeEndpointIdHash": payload.get("microphoneNativeEndpointIdHash").and_then(Value::as_str).unwrap_or(""),
        "renderNativeEndpointIdHash": payload.get("renderNativeEndpointIdHash").and_then(Value::as_str).unwrap_or(""),
    });
    let result = call_audio_sidecar_command("meetingCaptureStart", request);
    if !result.success {
        return Err(ShellCommandError::new(
            "meetingCaptureFailed",
            result
                .fallback_reason
                .unwrap_or_else(|| "meeting capture failed".to_string()),
        ));
    }
    let meeting_capture_id = result
        .payload
        .get("meetingCaptureId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if meeting_capture_id.is_empty() {
        return Err(ShellCommandError::new(
            "meetingCaptureMissingStream",
            "meeting capture did not return a meeting capture identifier",
        ));
    }
    let mut response = result.payload;
    response["captureId"] = json!(meeting_capture_id);
    response["aecRequested"] = json!(payload
        .get("aecEnabled")
        .and_then(Value::as_bool)
        .unwrap_or(true));
    Ok(response)
}

fn stop_meeting_audio(
    payload: &Value,
) -> Result<(Value, AudioSidecarCallResult), ShellCommandError> {
    let capture_id = payload
        .get("captureId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if capture_id.is_empty() || capture_id.len() > 96 {
        return Err(ShellCommandError::new(
            "invalidMeetingCaptureId",
            "meeting capture identifier is missing or invalid",
        ));
    }
    let result = call_audio_sidecar_command(
        "meetingCaptureStop",
        json!({"meetingCaptureId": capture_id}),
    );
    let response = meeting_audio_stop_shell_payload(capture_id, &result);
    Ok((response, result))
}

fn status_meeting_audio(
    payload: &Value,
) -> Result<(Value, AudioSidecarCallResult), ShellCommandError> {
    let capture_id = payload
        .get("captureId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if capture_id.is_empty() || capture_id.len() > 96 {
        return Err(ShellCommandError::new(
            "invalidMeetingCaptureId",
            "meeting capture identifier is missing or invalid",
        ));
    }
    let result = call_audio_sidecar_command(
        "meetingCaptureStatus",
        json!({"meetingCaptureId": capture_id}),
    );
    let response = meeting_audio_status_shell_payload(capture_id, &result);
    Ok((response, result))
}

fn meeting_audio_stop_shell_payload(capture_id: &str, result: &AudioSidecarCallResult) -> Value {
    let stopped = result.success
        && result
            .payload
            .get("stopped")
            .and_then(Value::as_bool)
            .unwrap_or(true);
    json!({
        "captureId": capture_id,
        "stopped": stopped,
        "sidecar": result.payload,
        "sidecarStatus": sidecar_status_payload(result),
    })
}

fn meeting_audio_status_shell_payload(capture_id: &str, result: &AudioSidecarCallResult) -> Value {
    let active = result
        .payload
        .get("active")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let sidecar_reason = result
        .payload
        .get("reason")
        .and_then(Value::as_str)
        .filter(|reason| !reason.trim().is_empty());
    let reason = if !result.success {
        sidecar_reason
            .or(result.error_code.as_deref())
            .unwrap_or("meetingCaptureStatusFailed")
    } else if active {
        "active"
    } else {
        sidecar_reason.unwrap_or("meetingCaptureSourceInactive")
    };
    json!({
        "captureId": capture_id,
        "active": active,
        "reason": reason,
        "sidecar": result.payload,
        "sidecarStatus": sidecar_status_payload(result),
    })
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

fn audio_sidecar_result_response_line(
    request_id: &str,
    started: Instant,
    payload: Value,
    result: &crate::audio_sidecar_client::AudioSidecarCallResult,
) -> String {
    response_line(
        request_id,
        result.success,
        result.error_code.as_deref().unwrap_or(""),
        result.fallback_reason.as_deref().unwrap_or(""),
        started,
        payload,
    )
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
        object.insert("engine".to_string(), json!("rust-wasapi"));
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
        object.insert("engine".to_string(), json!("rust-wasapi"));
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
        object.insert("engine".to_string(), json!("rust-wasapi"));
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
        object.insert("engine".to_string(), json!("rust-wasapi"));
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

fn audio_prewarm_status_shell_payload(
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
        object.insert("engine".to_string(), json!("rust-wasapi"));
        object
            .entry("active".to_string())
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
        // Cold process startup can spend several seconds importing the Python
        // transcription runtime. Keep this transport boundary aligned with the
        // audio sidecar's 6-second rolling-buffer contract so installed builds
        // do not silently truncate the capture-first prebuffer to two seconds.
        prebuffer_ms: optional_u64(payload, "prebufferMs", 0, 6_000) as u32,
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
    if text.len() > MAX_INJECT_TEXT_BYTES {
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
        expected_foreground_title: bounded_string(payload, "expectedForegroundTitle", "", 512)
            .trim()
            .to_string(),
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
    let foreground_title_before = foreground_title_for_policy();
    let pre_delay_ms = resolve_pre_delay_ms(&options, foreground_title_before.as_deref());
    let foreground_before = foreground_snapshot();
    if !foreground_title_matches_expected(&options, foreground_title_before.as_deref()) {
        return Err(foreground_target_mismatch_error(
            "before clipboard set",
            inject_response_payload(
                &options,
                &markers,
                pre_delay_ms,
                None,
                None,
                None,
                elapsed_ms(started),
                restore_status("notNeeded", None),
                &foreground_before,
                &foreground_before,
            ),
        ));
    }

    let clipboard_options = ClipboardOptions {
        retries: options.max_clipboard_retries,
        retry_delay: Duration::from_millis(options.clipboard_retry_delay_ms),
    };
    let (previous_clipboard, clipboard_read_ms) = if options.restore_clipboard {
        let read_started = Instant::now();
        match read_clipboard_snapshot(&clipboard_options) {
            Ok(snapshot) => (Some(snapshot), Some(elapsed_ms(read_started))),
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
    if !foreground_title_matches_expected(&options, foreground_title_for_policy().as_deref()) {
        return Err(foreground_target_mismatch_error(
            "before clipboard set",
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
                &foreground_snapshot(),
            ),
        ));
    }

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
                    previous_clipboard.as_ref(),
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
                previous_clipboard.as_ref(),
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
            previous_clipboard.as_ref(),
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
    if !foreground_title_matches_expected(&options, foreground_title_for_policy().as_deref()) {
        let restore = restore_clipboard_now(
            previous_clipboard.as_ref(),
            clipboard_sequence_after_set,
            &clipboard_options,
        );
        return Err(foreground_target_mismatch_error(
            "before paste dispatch",
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
            ),
        ));
    }

    let paste_started = Instant::now();
    if let Err(err) = dispatch_ctrl_v() {
        let restore = restore_clipboard_now(
            previous_clipboard.as_ref(),
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
        if let Some(previous_clipboard) = previous_clipboard {
            let restore =
                restore_status_with_snapshot("scheduled", None, Some(&previous_clipboard));
            schedule_clipboard_restore(
                previous_clipboard,
                clipboard_options,
                clipboard_sequence_after_set,
                options.restore_delay_ms,
            );
            restore
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

// The response mirrors distinct measured injection phases; keeping the named
// scalar arguments makes accidental timing swaps visible at each call site.
#[allow(clippy::too_many_arguments)]
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
        "deadlineMs": options.deadline_ms,
        "expectedForegroundTitleHash": if options.expected_foreground_title.is_empty() {
            Value::Null
        } else {
            Value::String(hash_sensitive_identifier(&options.expected_foreground_title))
        },
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

#[cfg(windows)]
fn restore_status_with_snapshot(
    skipped_reason: &str,
    error_code: Option<&str>,
    snapshot: Option<&ClipboardSnapshot>,
) -> Value {
    let mut status = restore_status(skipped_reason, error_code);
    if let Some(snapshot) = snapshot {
        if let Some(object) = status.as_object_mut() {
            if let Some(summary) = snapshot.summary().as_object() {
                for (key, value) in summary {
                    object.insert(key.clone(), value.clone());
                }
            }
        }
    }
    status
}

fn foreground_title_matches_expected(
    options: &InjectTextOptions,
    foreground_title: Option<&str>,
) -> bool {
    if options.expected_foreground_title.is_empty() {
        return true;
    }
    foreground_title
        .map(|title| title == options.expected_foreground_title)
        .unwrap_or(false)
}

fn foreground_target_mismatch_error(phase: &'static str, payload: Value) -> ShellCommandError {
    ShellCommandError::new(
        "foregroundTargetMismatch",
        format!("foreground target title did not match expected target {phase}"),
    )
    .with_payload(payload)
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

#[cfg(windows)]
#[derive(Clone)]
struct ClipboardFormatSnapshot {
    format: u32,
    data: Vec<u8>,
}

#[cfg(windows)]
#[derive(Clone)]
struct ClipboardSnapshot {
    formats: Vec<ClipboardFormatSnapshot>,
    unsupported_format_count: usize,
    total_bytes: usize,
}

#[cfg(windows)]
impl ClipboardSnapshot {
    fn summary(&self) -> Value {
        json!({
            "restoreKind": "snapshot",
            "formatCount": self.formats.len(),
            "unsupportedFormatCount": self.unsupported_format_count,
            "totalBytes": self.total_bytes,
        })
    }
}

#[cfg(windows)]
const MAX_CLIPBOARD_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;
#[cfg(windows)]
const MAX_CLIPBOARD_SNAPSHOT_FORMATS: usize = 64;

#[cfg(any(windows, test))]
const REGISTERED_CLIPBOARD_FORMAT_FIRST: u32 = 0xC000;
#[cfg(any(windows, test))]
const REGISTERED_CLIPBOARD_FORMAT_LAST: u32 = 0xFFFF;

#[cfg(any(windows, test))]
fn is_restorable_clipboard_format(format: u32) -> bool {
    // RegisterClipboardFormatW values are required to carry HGLOBAL-backed
    // data, so browser HTML/source metadata and RTF can be copied as raw bytes.
    // Predefined GDI-handle and private formats remain excluded.
    matches!(
        format,
        1  // CF_TEXT
            | 7  // CF_OEMTEXT
            | 8  // CF_DIB
            | 13 // CF_UNICODETEXT
            | 15 // CF_HDROP
            | 16 // CF_LOCALE
            | 17 // CF_DIBV5
    ) || (REGISTERED_CLIPBOARD_FORMAT_FIRST..=REGISTERED_CLIPBOARD_FORMAT_LAST).contains(&format)
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
fn read_clipboard_snapshot(
    options: &ClipboardOptions,
) -> Result<ClipboardSnapshot, ShellCommandError> {
    use windows_sys::Win32::System::{
        DataExchange::{CloseClipboard, EnumClipboardFormats, GetClipboardData, OpenClipboard},
        Memory::{GlobalLock, GlobalSize, GlobalUnlock},
    };

    let owner = ClipboardOwnerWindow::create()?;
    for _ in 0..options.retries.max(1) {
        if unsafe { OpenClipboard(owner.hwnd()) } == 0 {
            thread::sleep(options.retry_delay);
            continue;
        }

        let result = unsafe {
            let mut snapshot = ClipboardSnapshot {
                formats: Vec::new(),
                unsupported_format_count: 0,
                total_bytes: 0,
            };
            let mut snapshot_error: Option<ShellCommandError> = None;
            let mut seen_formats = std::collections::HashSet::new();
            let mut format = EnumClipboardFormats(0);
            while format != 0 {
                if !seen_formats.insert(format) {
                    break;
                }
                if seen_formats.len() > MAX_CLIPBOARD_SNAPSHOT_FORMATS {
                    break;
                }
                if !is_restorable_clipboard_format(format) {
                    snapshot.unsupported_format_count += 1;
                    format = EnumClipboardFormats(format);
                    continue;
                }

                let handle = GetClipboardData(format);
                if handle.is_null() {
                    snapshot.unsupported_format_count += 1;
                    format = EnumClipboardFormats(format);
                    continue;
                }

                let byte_len = GlobalSize(handle);
                if byte_len == 0 {
                    snapshot.unsupported_format_count += 1;
                    format = EnumClipboardFormats(format);
                    continue;
                }
                if snapshot.total_bytes.saturating_add(byte_len) > MAX_CLIPBOARD_SNAPSHOT_BYTES {
                    snapshot_error = Some(ShellCommandError::new(
                        "clipboardSnapshotTooLarge",
                        format!(
                            "clipboard snapshot exceeds {} bytes",
                            MAX_CLIPBOARD_SNAPSHOT_BYTES
                        ),
                    ));
                    break;
                }

                let ptr = GlobalLock(handle);
                if ptr.is_null() {
                    snapshot.unsupported_format_count += 1;
                    format = EnumClipboardFormats(format);
                    continue;
                }
                let data = std::slice::from_raw_parts(ptr.cast::<u8>(), byte_len).to_vec();
                let _ = GlobalUnlock(handle);
                snapshot.total_bytes += byte_len;
                snapshot
                    .formats
                    .push(ClipboardFormatSnapshot { format, data });

                format = EnumClipboardFormats(format);
            }

            if let Some(err) = snapshot_error {
                Err(err)
            } else if snapshot.formats.is_empty() && snapshot.unsupported_format_count > 0 {
                Err(ShellCommandError::new(
                    "clipboardSnapshotUnsupported",
                    "clipboard only contains formats that cannot be captured for restore",
                ))
            } else {
                Ok(snapshot)
            }
        };
        unsafe {
            CloseClipboard();
        }
        return result;
    }
    Err(ShellCommandError::new(
        "clipboardBusy",
        "could not open clipboard for snapshot read",
    ))
}

#[cfg(windows)]
fn set_clipboard_snapshot(
    snapshot: &ClipboardSnapshot,
    options: &ClipboardOptions,
) -> Result<u32, ShellCommandError> {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::GlobalFree,
        System::{
            DataExchange::{CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData},
            Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE},
        },
    };

    let owner = ClipboardOwnerWindow::create()?;
    for _ in 0..options.retries.max(1) {
        if unsafe { OpenClipboard(owner.hwnd()) } == 0 {
            thread::sleep(options.retry_delay);
            continue;
        }
        let result = unsafe {
            if EmptyClipboard() == 0 {
                Err(ShellCommandError::new(
                    "clipboardRestoreFailed",
                    "EmptyClipboard failed while restoring clipboard snapshot",
                ))
            } else {
                let mut restored_any = false;
                let mut skipped_count = 0usize;
                for item in &snapshot.formats {
                    if !is_restorable_clipboard_format(item.format) {
                        skipped_count += 1;
                        continue;
                    }
                    let handle = GlobalAlloc(GMEM_MOVEABLE, item.data.len());
                    if handle.is_null() {
                        skipped_count += 1;
                        continue;
                    }
                    let locked_ptr = GlobalLock(handle);
                    if locked_ptr.is_null() {
                        let _ = GlobalFree(handle);
                        skipped_count += 1;
                        continue;
                    }
                    ptr::copy_nonoverlapping(
                        item.data.as_ptr(),
                        locked_ptr.cast::<u8>(),
                        item.data.len(),
                    );
                    let _ = GlobalUnlock(handle);
                    if SetClipboardData(item.format, handle).is_null() {
                        let _ = GlobalFree(handle);
                        skipped_count += 1;
                        continue;
                    }
                    restored_any = true;
                }

                if restored_any || snapshot.formats.is_empty() {
                    Ok(())
                } else {
                    Err(ShellCommandError::new(
                        "clipboardRestoreFailed",
                        format!(
                            "no clipboard snapshot formats could be restored; skipped={skipped_count}"
                        ),
                    ))
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
        "could not open clipboard for snapshot restore",
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

#[cfg(any(windows, test))]
fn clipboard_sequence_is_unchanged(expected_sequence: u32, current_sequence: u32) -> bool {
    expected_sequence != 0 && current_sequence != 0 && current_sequence == expected_sequence
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
    previous_clipboard: ClipboardSnapshot,
    options: ClipboardOptions,
    expected_sequence: u32,
    restore_delay_ms: u64,
) {
    thread::spawn(move || {
        if restore_delay_ms > 0 {
            thread::sleep(Duration::from_millis(restore_delay_ms));
        }
        let _ = restore_clipboard_now(Some(&previous_clipboard), expected_sequence, &options);
    });
}

#[cfg(windows)]
fn restore_clipboard_now(
    previous_clipboard: Option<&ClipboardSnapshot>,
    expected_sequence: u32,
    options: &ClipboardOptions,
) -> Value {
    let Some(previous_clipboard) = previous_clipboard else {
        return json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": "previousClipboardUnavailable",
            "errorCode": Value::Null,
        });
    };

    let current_sequence = clipboard_sequence_number();
    if !clipboard_sequence_is_unchanged(expected_sequence, current_sequence) {
        let skipped_reason = if expected_sequence == 0 || current_sequence == 0 {
            "clipboardSequenceUnavailable"
        } else {
            "clipboardSequenceChanged"
        };
        let mut status = json!({
            "scheduled": false,
            "attempted": false,
            "succeeded": Value::Null,
            "skippedReason": skipped_reason,
            "errorCode": Value::Null,
        });
        if let Some(object) = status.as_object_mut() {
            if let Some(summary) = previous_clipboard.summary().as_object() {
                for (key, value) in summary {
                    object.insert(key.clone(), value.clone());
                }
            }
        }
        return status;
    }

    match set_clipboard_snapshot(previous_clipboard, options) {
        Ok(_) => {
            let mut status = json!({
                "scheduled": false,
                "attempted": true,
                "succeeded": true,
                "skippedReason": Value::Null,
                "errorCode": Value::Null,
            });
            if let Some(object) = status.as_object_mut() {
                if let Some(summary) = previous_clipboard.summary().as_object() {
                    for (key, value) in summary {
                        object.insert(key.clone(), value.clone());
                    }
                }
            }
            status
        }
        Err(err) => {
            let mut status = json!({
                "scheduled": false,
                "attempted": true,
                "succeeded": false,
                "skippedReason": "restoreFailed",
                "errorCode": err.code,
            });
            if let Some(object) = status.as_object_mut() {
                if let Some(summary) = previous_clipboard.summary().as_object() {
                    for (key, value) in summary {
                        object.insert(key.clone(), value.clone());
                    }
                }
            }
            status
        }
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

#[derive(Debug, Clone, PartialEq, Eq)]
enum LifecycleStartResource {
    Capture(String),
    Prewarm(String),
    Meeting(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ResponseDelivery {
    lifecycle_start: Option<LifecycleStartResource>,
    api_version: String,
    request_id: String,
}

impl LifecycleStartResource {
    fn cleanup(&self) -> bool {
        let result = match self {
            Self::Capture(stream_id) => {
                call_audio_sidecar_command("captureStop", json!({"streamId": stream_id}))
            }
            Self::Prewarm(prewarm_id) => {
                call_audio_sidecar_command("prewarmStop", json!({"prewarmId": prewarm_id}))
            }
            Self::Meeting(meeting_capture_id) => call_audio_sidecar_command(
                "meetingCaptureStop",
                json!({"meetingCaptureId": meeting_capture_id}),
            ),
        };
        result.success
    }
}

fn lifecycle_start_resource(
    request_raw: &str,
    response_raw: &str,
) -> Option<LifecycleStartResource> {
    let request: Value = serde_json::from_str(request_raw).ok()?;
    let response: Value = serde_json::from_str(response_raw.trim()).ok()?;
    if !response
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return None;
    }
    let command = request.get("command").and_then(Value::as_str)?;
    let payload = response.get("payload")?;
    let bounded_id = |field: &str| {
        let value = payload.get(field).and_then(Value::as_str)?.trim();
        (!value.is_empty() && value.len() <= 96).then(|| value.to_string())
    };
    match command {
        "audioCaptureStart" => bounded_id("streamId").map(LifecycleStartResource::Capture),
        "audioPrewarmStart" => bounded_id("prewarmId").map(LifecycleStartResource::Prewarm),
        "audioMeetingStart" | "audioMeetingResume" => {
            bounded_id("captureId").map(LifecycleStartResource::Meeting)
        }
        _ => None,
    }
}

fn response_delivery(request_raw: &str, response_raw: &str) -> Option<ResponseDelivery> {
    let request: Value = serde_json::from_str(request_raw).ok()?;
    let response: Value = serde_json::from_str(response_raw.trim()).ok()?;
    let api_version = request.get("apiVersion").and_then(Value::as_str)?.trim();
    let request_id = request.get("requestId").and_then(Value::as_str)?.trim();
    if api_version.is_empty()
        || api_version.len() > 16
        || request_id.is_empty()
        || request_id.len() > 128
        || !response.get("success").is_some_and(Value::is_boolean)
        || response.get("apiVersion").and_then(Value::as_str) != Some(api_version)
        || response.get("requestId").and_then(Value::as_str) != Some(request_id)
    {
        return None;
    }
    Some(ResponseDelivery {
        lifecycle_start: lifecycle_start_resource(request_raw, response_raw),
        api_version: api_version.to_string(),
        request_id: request_id.to_string(),
    })
}

fn response_ack_matches(raw: &str, delivery: &ResponseDelivery) -> bool {
    let acknowledgement: Value = match serde_json::from_str(raw.trim()) {
        Ok(value) => value,
        Err(_) => return false,
    };
    acknowledgement.get("type").and_then(Value::as_str) == Some("responseAck")
        && acknowledgement.get("apiVersion").and_then(Value::as_str)
            == Some(delivery.api_version.as_str())
        && acknowledgement.get("requestId").and_then(Value::as_str)
            == Some(delivery.request_id.as_str())
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
    let mut workers = Vec::<JoinHandle<Result<(), String>>>::new();
    while !stop.load(Ordering::SeqCst) {
        reap_finished_shell_ipc_workers(&mut workers, log);
        if workers.len() >= SHELL_IPC_CLIENT_WORKER_LIMIT {
            thread::sleep(Duration::from_millis(5));
            continue;
        }

        match accept_shell_ipc_client(&config) {
            Ok(pipe) => {
                if stop.load(Ordering::SeqCst) {
                    disconnect_and_close_pipe(pipe);
                    break;
                }

                let pipe_value = pipe as usize;
                let expected_token = config.token.clone();
                let stop_for_worker = Arc::clone(&stop);
                match thread::Builder::new()
                    .name("shell-ipc-client".to_string())
                    .spawn(move || {
                        let pipe = pipe_value as windows_sys::Win32::Foundation::HANDLE;
                        let _pipe_guard = ConnectedPipeGuard::new(pipe);
                        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            serve_connected_client(pipe, &expected_token, &stop_for_worker)
                        })) {
                            Ok(result) => result,
                            Err(_) => Err("shell IPC client command panicked".to_string()),
                        }
                    }) {
                    Ok(worker) => workers.push(worker),
                    Err(err) => {
                        disconnect_and_close_pipe(pipe);
                        log(format!("shell IPC client worker could not start: {err}"));
                    }
                }
            }
            Err(err) => {
                if !stop.load(Ordering::SeqCst) {
                    log(format!("shell IPC request failed: {err}"));
                }
            }
        }
    }

    let shutdown_deadline = Instant::now() + Duration::from_millis(SHELL_IPC_SHUTDOWN_GRACE_MS);
    while !workers.is_empty() && Instant::now() < shutdown_deadline {
        reap_finished_shell_ipc_workers(&mut workers, log);
        if !workers.is_empty() {
            thread::sleep(Duration::from_millis(5));
        }
    }
    reap_finished_shell_ipc_workers(&mut workers, log);
    if !workers.is_empty() {
        log(format!(
            "shell IPC server detached {} client worker(s) during shutdown",
            workers.len()
        ));
    }
    log("shell IPC server stopped".to_string());
}

#[cfg(windows)]
fn reap_finished_shell_ipc_workers<L>(
    workers: &mut Vec<JoinHandle<Result<(), String>>>,
    log: &mut L,
) where
    L: FnMut(String),
{
    let mut index = 0usize;
    while index < workers.len() {
        if !workers[index].is_finished() {
            index += 1;
            continue;
        }

        let worker = workers.swap_remove(index);
        match worker.join() {
            Ok(Ok(())) => {}
            Ok(Err(err)) => log(format!("shell IPC request failed: {err}")),
            Err(_) => log("shell IPC client worker panicked".to_string()),
        }
    }
}

#[cfg(windows)]
fn accept_shell_ipc_client(
    config: &ShellIpcConfig,
) -> Result<windows_sys::Win32::Foundation::HANDLE, String> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{
            CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
        },
        Storage::FileSystem::PIPE_ACCESS_DUPLEX,
        System::Pipes::{
            ConnectNamedPipe, CreateNamedPipeW, PIPE_READMODE_MESSAGE, PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_TYPE_MESSAGE, PIPE_WAIT,
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
            SHELL_IPC_PIPE_INSTANCE_LIMIT,
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

    Ok(pipe)
}

#[cfg(windows)]
fn serve_connected_client(
    pipe: windows_sys::Win32::Foundation::HANDLE,
    expected_token: &str,
    stop: &AtomicBool,
) -> Result<(), String> {
    let result = match handle_connected_client(pipe, expected_token, stop) {
        Ok(ClientServiceOutcome::ResponseWritten {
            delivery: Some(delivery),
        }) => {
            let acknowledgement = wait_for_response_ack(pipe, stop, &delivery);
            if acknowledgement.was_acknowledged() {
                Ok(())
            } else if let Some(resource) = delivery.lifecycle_start {
                let cleanup_succeeded = resource.cleanup();
                Err(format!(
                    "shell IPC lifecycle response acknowledgement failed: outcome={}; cleanup_success={cleanup_succeeded}",
                    acknowledgement.diagnostic_code()
                ))
            } else {
                Err(format!(
                    "shell IPC response acknowledgement failed: outcome={}",
                    acknowledgement.diagnostic_code()
                ))
            }
        }
        Ok(ClientServiceOutcome::ResponseWritten { delivery: None }) => Ok(()),
        Ok(ClientServiceOutcome::ClosedWithoutResponse) => Ok(()),
        Err(err) => Err(err),
    };
    result
}

#[cfg(windows)]
fn wait_for_response_ack(
    pipe: windows_sys::Win32::Foundation::HANDLE,
    stop: &AtomicBool,
    delivery: &ResponseDelivery,
) -> ResponseAckOutcome {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::GetLastError, Storage::FileSystem::ReadFile, System::Pipes::PeekNamedPipe,
    };

    // Client disconnect is not proof of delivery: a Python ReadFile timeout also cancels and
    // closes the pipe. Every complete response waits for a bounded, request-bound ACK. Only a
    // successful audio lifecycle start attaches cleanup ownership to that delivery record.
    let deadline = Instant::now() + Duration::from_millis(CLIENT_RESPONSE_ACK_TIMEOUT_MS);
    let mut acknowledgement = Vec::<u8>::new();
    while Instant::now() < deadline {
        if stop.load(Ordering::SeqCst) {
            return ResponseAckOutcome::ServerStopping;
        }
        let mut available = 0u32;
        let connected = unsafe {
            PeekNamedPipe(
                pipe,
                ptr::null_mut(),
                0,
                ptr::null_mut(),
                &mut available,
                ptr::null_mut(),
            )
        } != 0;
        if !connected {
            return ResponseAckOutcome::Disconnected;
        }
        if available == 0 {
            thread::sleep(Duration::from_millis(2));
            continue;
        }

        let mut buffer = [0u8; 256];
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
        let read_status = match pipe_read_chunk_status(ok, bytes_read, read_error) {
            Ok(status) => status,
            Err(_) => return ResponseAckOutcome::Disconnected,
        };
        if matches!(read_status, PipeReadChunkStatus::Finished) {
            return ResponseAckOutcome::Disconnected;
        }
        acknowledgement.extend_from_slice(&buffer[..bytes_read as usize]);
        if acknowledgement.len() > MAX_RESPONSE_ACK_BYTES {
            return ResponseAckOutcome::Invalid;
        }
        if let Some(newline_at) = acknowledgement.iter().position(|byte| *byte == b'\n') {
            let first_line = &acknowledgement[..newline_at];
            let raw = String::from_utf8_lossy(first_line);
            return if response_ack_matches(raw.as_ref(), delivery) {
                ResponseAckOutcome::Acknowledged
            } else {
                ResponseAckOutcome::Invalid
            };
        }
    }
    ResponseAckOutcome::TimedOut
}

#[cfg(windows)]
fn disconnect_and_close_pipe(pipe: windows_sys::Win32::Foundation::HANDLE) {
    use windows_sys::Win32::{Foundation::CloseHandle, System::Pipes::DisconnectNamedPipe};

    unsafe {
        let _ = DisconnectNamedPipe(pipe);
        let _ = CloseHandle(pipe);
    }
}

#[cfg(windows)]
struct ConnectedPipeGuard {
    pipe: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl ConnectedPipeGuard {
    fn new(pipe: windows_sys::Win32::Foundation::HANDLE) -> Self {
        Self { pipe }
    }
}

#[cfg(windows)]
impl Drop for ConnectedPipeGuard {
    fn drop(&mut self) {
        disconnect_and_close_pipe(self.pipe);
    }
}

#[cfg(windows)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ResponseAckOutcome {
    Acknowledged,
    Invalid,
    Disconnected,
    TimedOut,
    ServerStopping,
}

#[cfg(windows)]
impl ResponseAckOutcome {
    fn was_acknowledged(self) -> bool {
        self == Self::Acknowledged
    }

    fn diagnostic_code(self) -> &'static str {
        match self {
            Self::Acknowledged => "acknowledged",
            Self::Invalid => "invalid",
            Self::Disconnected => "disconnected",
            Self::TimedOut => "timedOut",
            Self::ServerStopping => "serverStopping",
        }
    }
}

#[cfg(windows)]
#[derive(Debug, Clone, PartialEq, Eq)]
enum ClientServiceOutcome {
    ResponseWritten { delivery: Option<ResponseDelivery> },
    ClosedWithoutResponse,
}

#[cfg(windows)]
fn handle_connected_client(
    pipe: windows_sys::Win32::Foundation::HANDLE,
    expected_token: &str,
    stop: &AtomicBool,
) -> Result<ClientServiceOutcome, String> {
    use std::ptr;
    use windows_sys::Win32::{
        Foundation::GetLastError, Storage::FileSystem::ReadFile, System::Pipes::PeekNamedPipe,
    };

    let mut request = Vec::<u8>::new();
    let read_started = Instant::now();
    loop {
        if stop.load(Ordering::SeqCst) {
            return Ok(ClientServiceOutcome::ClosedWithoutResponse);
        }
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
            )
            .map(|_| ClientServiceOutcome::ResponseWritten { delivery: None });
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
            return Ok(ClientServiceOutcome::ClosedWithoutResponse);
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
            )
            .map(|_| ClientServiceOutcome::ResponseWritten { delivery: None });
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
    let delivery = response_delivery(raw.trim(), &response);
    match write_response(pipe, &response) {
        Ok(()) => Ok(ClientServiceOutcome::ResponseWritten { delivery }),
        Err(err) => {
            if let Some(resource) = delivery.and_then(|value| value.lifecycle_start) {
                let cleanup_succeeded = resource.cleanup();
                Err(format!(
                    "{err}; lifecycle start cleanup_success={cleanup_succeeded}"
                ))
            } else {
                Err(err)
            }
        }
    }
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

    #[cfg(windows)]
    fn open_test_pipe(pipe_name: &str, timeout: std::time::Duration) -> std::fs::File {
        use std::{fs::OpenOptions, thread, time::Duration};

        let deadline = Instant::now() + timeout;
        loop {
            match OpenOptions::new().read(true).write(true).open(pipe_name) {
                Ok(file) => return file,
                Err(_) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(5));
                }
                Err(err) => panic!("could not connect to test shell IPC pipe: {err}"),
            }
        }
    }

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
    fn inject_text_mutation_lane_serializes_clipboard_side_effects() {
        use std::{sync::mpsc, thread, time::Duration};

        let first = super::inject_text_mutation_lock();
        let (acquired_tx, acquired_rx) = mpsc::sync_channel(1);
        let waiter = thread::spawn(move || {
            let _second = super::inject_text_mutation_lock();
            let _ = acquired_tx.send(());
        });

        assert!(acquired_rx.recv_timeout(Duration::from_millis(25)).is_err());
        drop(first);
        acquired_rx
            .recv_timeout(Duration::from_millis(250))
            .expect("second injection should proceed after the mutation lane is released");
        waiter.join().unwrap();
    }

    #[test]
    fn successful_lifecycle_start_response_retains_cleanup_ownership() {
        use super::LifecycleStartResource;

        let capture_request = json!({"command": "audioCaptureStart"}).to_string();
        let capture_response =
            json!({"success": true, "payload": {"streamId": "stream-1"}}).to_string();
        assert_eq!(
            super::lifecycle_start_resource(&capture_request, &capture_response),
            Some(LifecycleStartResource::Capture("stream-1".to_string()))
        );

        let prewarm_request = json!({"command": "audioPrewarmStart"}).to_string();
        let prewarm_response =
            json!({"success": true, "payload": {"prewarmId": "prewarm-1"}}).to_string();
        assert_eq!(
            super::lifecycle_start_resource(&prewarm_request, &prewarm_response),
            Some(LifecycleStartResource::Prewarm("prewarm-1".to_string()))
        );

        let meeting_request = json!({"command": "audioMeetingResume"}).to_string();
        let meeting_response =
            json!({"success": true, "payload": {"captureId": "meeting-1"}}).to_string();
        assert_eq!(
            super::lifecycle_start_resource(&meeting_request, &meeting_response),
            Some(LifecycleStartResource::Meeting("meeting-1".to_string()))
        );

        let failed_response =
            json!({"success": false, "payload": {"streamId": "stream-2"}}).to_string();
        assert_eq!(
            super::lifecycle_start_resource(&capture_request, &failed_response),
            None
        );

        let delivery_request = json!({
            "apiVersion": API_VERSION,
            "requestId": "delivery-1",
            "command": "audioCaptureStart"
        })
        .to_string();
        let delivery_response = json!({
            "apiVersion": API_VERSION,
            "requestId": "delivery-1",
            "success": true,
            "payload": {"streamId": "stream-1"}
        })
        .to_string();
        let delivery = super::response_delivery(&delivery_request, &delivery_response)
            .expect("complete responses retain request-bound delivery ownership");
        assert_eq!(delivery.request_id, "delivery-1");
        assert_eq!(delivery.api_version, API_VERSION);
        assert_eq!(
            delivery.lifecycle_start,
            Some(LifecycleStartResource::Capture("stream-1".to_string()))
        );
        assert!(super::response_ack_matches(
            &json!({
                "apiVersion": API_VERSION,
                "requestId": "delivery-1",
                "type": "responseAck"
            })
            .to_string(),
            &delivery
        ));
        assert!(!super::response_ack_matches(
            &json!({
                "apiVersion": API_VERSION,
                "requestId": "another-request",
                "type": "responseAck"
            })
            .to_string(),
            &delivery
        ));
        assert!(super::response_delivery(
            &delivery_request,
            &delivery_response.replace("delivery-1", "mismatched-response")
        )
        .is_none());

        let failure_delivery = super::response_delivery(
            &delivery_request,
            &delivery_response.replace("\"success\":true", "\"success\":false"),
        )
        .expect("failed responses are acknowledged too");
        assert_eq!(failure_delivery.lifecycle_start, None);
    }

    #[cfg(windows)]
    #[test]
    fn response_ack_outcomes_distinguish_complete_delivery() {
        use super::ResponseAckOutcome;

        assert!(ResponseAckOutcome::Acknowledged.was_acknowledged());
        assert!(!ResponseAckOutcome::TimedOut.was_acknowledged());
        assert!(!ResponseAckOutcome::Disconnected.was_acknowledged());
        assert!(!ResponseAckOutcome::Invalid.was_acknowledged());
        assert!(!ResponseAckOutcome::ServerStopping.was_acknowledged());
    }

    #[test]
    fn shell_command_panics_become_sanitized_responses() {
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "panic-request",
            "command": "ping"
        })
        .to_string();
        let response =
            super::contain_shell_ipc_request_panic(&request, || panic!("secret panic detail"));
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();
        assert_eq!(response["requestId"], "panic-request");
        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "internalCommandPanic");
        assert!(!response.to_string().contains("secret panic detail"));
    }

    #[cfg(windows)]
    #[test]
    fn connected_pipe_guard_closes_its_handle_during_unwind() {
        use std::ptr;
        use windows_sys::Win32::{
            Foundation::WAIT_FAILED,
            System::Threading::{CreateEventW, WaitForSingleObject},
        };

        let handle = unsafe { CreateEventW(ptr::null(), 1, 0, ptr::null()) };
        assert!(!handle.is_null());
        let unwind = std::panic::catch_unwind(|| {
            let _guard = super::ConnectedPipeGuard::new(handle);
            panic!("test unwind");
        });
        assert!(unwind.is_err());
        assert_eq!(unsafe { WaitForSingleObject(handle, 0) }, WAIT_FAILED);
    }

    #[cfg(windows)]
    #[test]
    fn stalled_shell_ipc_client_does_not_block_a_second_request() {
        use std::{
            io::{BufRead, BufReader, Write},
            sync::mpsc,
            thread,
            time::Duration,
        };

        let config = ShellIpcConfig::new();
        let server = super::start_shell_ipc_server(config.clone(), |_| {})
            .expect("server should start")
            .expect("Windows server should be available");

        let mut stalled_client = open_test_pipe(&config.pipe_name, Duration::from_secs(1));
        stalled_client
            .write_all(br#"{"apiVersion":"1""#)
            .expect("partial request should be written");
        thread::sleep(Duration::from_millis(20));

        // This connection must become available while the first client's 750 ms read timeout is
        // still running; a single-instance server cannot satisfy it within this deadline.
        let second_client = open_test_pipe(&config.pipe_name, Duration::from_millis(500));
        let token = config.token.clone();
        let (response_tx, response_rx) = mpsc::sync_channel(1);
        thread::spawn(move || {
            let result = (|| -> Result<String, String> {
                let mut client = second_client;
                let request = json!({
                    "apiVersion": API_VERSION,
                    "requestId": "parallel-ping",
                    "command": "ping",
                    "token": token,
                    "payload": {}
                });
                client
                    .write_all(format!("{request}\n").as_bytes())
                    .map_err(|err| err.to_string())?;
                let mut response = String::new();
                BufReader::new(client)
                    .read_line(&mut response)
                    .map_err(|err| err.to_string())?;
                Ok(response)
            })();
            let _ = response_tx.send(result);
        });

        let response = response_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("second request should finish")
            .expect("second request should succeed");
        let response: serde_json::Value =
            serde_json::from_str(response.trim()).expect("response should be valid JSON");
        assert_eq!(response["requestId"], "parallel-ping");
        assert_eq!(response["success"], true);
        assert_eq!(response["payload"]["pong"], true);

        drop(stalled_client);
        drop(server);
    }

    #[cfg(windows)]
    #[test]
    fn non_lifecycle_response_waits_for_request_bound_ack_not_client_close() {
        use std::{
            io::{BufRead, BufReader, Write},
            time::Duration,
        };

        let config = ShellIpcConfig::new();
        let server = super::start_shell_ipc_server(config.clone(), |_| {})
            .expect("server should start")
            .expect("Windows server should be available");
        let mut client = open_test_pipe(&config.pipe_name, Duration::from_secs(1));
        let request = json!({
            "apiVersion": API_VERSION,
            "requestId": "slow-reader-ping",
            "command": "ping",
            "token": config.token,
            "payload": {}
        });
        client
            .write_all(format!("{request}\n").as_bytes())
            .expect("ping should be written");

        let mut response = String::new();
        BufReader::new(&mut client)
            .read_line(&mut response)
            .expect("client should receive the response");
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();
        assert_eq!(response["requestId"], "slow-reader-ping");
        assert_eq!(response["success"], true);

        let acknowledgement = json!({
            "apiVersion": API_VERSION,
            "requestId": "slow-reader-ping",
            "type": "responseAck"
        });
        client
            .write_all(format!("{acknowledgement}\n").as_bytes())
            .expect("response acknowledgement should be written");

        // Keeping this client handle open after ACK must not keep the server worker alive.
        let next_client = open_test_pipe(&config.pipe_name, Duration::from_millis(250));
        drop(next_client);
        drop(server);
    }

    #[cfg(windows)]
    #[test]
    fn shell_ipc_server_shutdown_does_not_flush_a_stalled_client() {
        use std::{
            io::Write,
            sync::mpsc,
            thread,
            time::{Duration, Instant},
        };

        let config = ShellIpcConfig::new();
        let (log_tx, log_rx) = mpsc::channel();
        let server = super::start_shell_ipc_server(config.clone(), move |message| {
            let _ = log_tx.send(message);
        })
        .expect("server should start")
        .expect("Windows server should be available");
        let mut stalled_client = open_test_pipe(&config.pipe_name, Duration::from_secs(1));
        stalled_client
            .write_all(br#"{"apiVersion":"1""#)
            .expect("partial request should be written");
        thread::sleep(Duration::from_millis(20));

        let started = Instant::now();
        drop(server);
        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(log_rx
            .try_iter()
            .any(|message| message == "shell IPC server stopped"));

        drop(stalled_client);
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
    fn clipboard_format_filter_keeps_registered_hglobal_and_rejects_handle_formats() {
        assert!(super::is_restorable_clipboard_format(8)); // CF_DIB
        assert!(super::is_restorable_clipboard_format(13)); // CF_UNICODETEXT
        assert!(super::is_restorable_clipboard_format(0xC000));
        assert!(super::is_restorable_clipboard_format(0xFFFF));
        assert!(!super::is_restorable_clipboard_format(0xBFFF));
        assert!(!super::is_restorable_clipboard_format(2)); // CF_BITMAP
        assert!(!super::is_restorable_clipboard_format(14)); // CF_ENHMETAFILE
    }

    #[test]
    fn clipboard_restore_requires_a_nonzero_unchanged_sequence() {
        assert!(super::clipboard_sequence_is_unchanged(42, 42));
        assert!(!super::clipboard_sequence_is_unchanged(42, 43));
        assert!(!super::clipboard_sequence_is_unchanged(0, 0));
        assert!(!super::clipboard_sequence_is_unchanged(42, 0));
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
        assert_eq!(
            value["payload"]["nativeOverlay"]["windowLabel"],
            crate::native_overlay::OVERLAY_WINDOW_LABEL
        );
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "overlayPrepare"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "overlayShow"));
        assert!(value["payload"]["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command == "overlayHide"));
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
            "expectedForegroundTitle": "Scriber Hot Path Text Target",
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
        assert_eq!(
            options.expected_foreground_title,
            "Scriber Hot Path Text Target"
        );
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
    fn expected_foreground_title_requires_exact_match_when_present() {
        let options = super::parse_inject_text_options(&json!({
            "text": "hello",
            "dispatch": "ctrlV",
            "expectedForegroundTitle": "Scriber Hot Path Text Target",
        }))
        .unwrap();

        assert!(super::foreground_title_matches_expected(
            &options,
            Some("Scriber Hot Path Text Target")
        ));
        assert!(!super::foreground_title_matches_expected(
            &options,
            Some("Codex")
        ));
        assert!(!super::foreground_title_matches_expected(&options, None));

        let options_without_target = super::parse_inject_text_options(&json!({
            "text": "hello",
            "dispatch": "ctrlV",
        }))
        .unwrap();
        assert!(super::foreground_title_matches_expected(
            &options_without_target,
            Some("Codex")
        ));
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
        assert_eq!(options.prebuffer_ms, 6_000);
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
        assert_eq!(value["payload"]["engine"], "rust-wasapi");
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
        let success = value["success"].as_bool().unwrap_or(false);
        if success {
            assert!(
                value
                    .get("errorCode")
                    .is_none_or(|code| code.as_str().unwrap_or_default().is_empty()),
                "successful prewarm start should not return an error code: {}",
                value["errorCode"]
            );
            assert_eq!(value["payload"]["prewarmAvailable"], true);
            assert!(
                value["payload"]["prewarmId"]
                    .as_str()
                    .is_some_and(|prewarm_id| !prewarm_id.is_empty()),
                "successful prewarm start must return a prewarm id: {}",
                value["payload"]
            );
            crate::audio_sidecar_client::shutdown_all_audio_sidecars("test");
        } else {
            assert!(
                matches!(
                    value["errorCode"].as_str(),
                    Some("audioPrewarmUnavailable" | "unknownCommand")
                ),
                "unexpected prewarm start error: {}",
                value["errorCode"]
            );
        }
        assert_eq!(value["payload"]["engine"], "rust-wasapi");
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

        assert_eq!(payload["engine"], "rust-wasapi");
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
    fn audio_prewarm_stop_response_propagates_sidecar_failure() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: false,
            error_code: Some("audioSidecarResponseTimeout".to_string()),
            fallback_reason: Some("prewarm stop timed out".to_string()),
            payload: json!({"stopped": false, "prewarmId": "prewarm-1"}),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };

        let response = super::audio_sidecar_result_response_line(
            "prewarm-stop-failed",
            Instant::now(),
            json!({"stopped": false}),
            &result,
        );
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioSidecarResponseTimeout");
        assert_eq!(response["fallbackReason"], "prewarm stop timed out");
    }

    #[test]
    fn audio_prewarm_status_shell_payload_preserves_status_fields() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "active": true,
                "prewarmId": "prewarm-1",
                "reason": "active",
                "bufferedBlocks": 3,
                "bufferedAudioFrames": 480,
                "sidecarPid": 9876,
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };

        let payload = super::audio_prewarm_status_shell_payload(result.payload.clone(), &result);

        assert_eq!(payload["engine"], "rust-wasapi");
        assert_eq!(payload["active"], true);
        assert_eq!(payload["prewarmId"], "prewarm-1");
        assert_eq!(payload["reason"], "active");
        assert_eq!(payload["bufferedBlocks"], 3);
        assert_eq!(payload["bufferedAudioFrames"], 480);
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

        assert_eq!(payload["engine"], "rust-wasapi");
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
    fn audio_capture_stop_response_propagates_sidecar_failure() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: false,
            error_code: Some("audioSidecarResponseTimeout".to_string()),
            fallback_reason: Some("capture stop timed out".to_string()),
            payload: json!({
                "stopped": false,
                "streamId": "stream-1",
                "reason": "responseTimeout",
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };
        let payload = super::audio_capture_stop_shell_payload(result.payload.clone(), &result);

        let response = super::audio_sidecar_result_response_line(
            "capture-stop-failed",
            Instant::now(),
            payload,
            &result,
        );
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioSidecarResponseTimeout");
        assert_eq!(response["fallbackReason"], "capture stop timed out");
        assert_eq!(response["payload"]["stopped"], false);
        assert_eq!(
            response["payload"]["sidecar"]["errorCode"],
            "audioSidecarResponseTimeout"
        );
    }

    #[test]
    fn meeting_audio_stop_response_propagates_sidecar_failure() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: false,
            error_code: Some("audioSidecarWriteFailed".to_string()),
            fallback_reason: Some("meeting stop transport failed".to_string()),
            payload: json!({
                "stopped": false,
                "meetingCaptureId": "meeting-1",
                "reason": "transportFailure",
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };
        let payload = super::meeting_audio_stop_shell_payload("meeting-1", &result);

        let response = super::audio_sidecar_result_response_line(
            "meeting-stop-failed",
            Instant::now(),
            payload,
            &result,
        );
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioSidecarWriteFailed");
        assert_eq!(response["fallbackReason"], "meeting stop transport failed");
        assert_eq!(response["payload"]["stopped"], false);
        assert_eq!(response["payload"]["sidecar"]["reason"], "transportFailure");
        assert_eq!(
            response["payload"]["sidecarStatus"]["errorCode"],
            "audioSidecarWriteFailed"
        );
    }

    #[test]
    fn meeting_audio_status_failure_is_not_reported_as_normal_inactive() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: false,
            error_code: Some("audioSidecarResponseTimeout".to_string()),
            fallback_reason: Some("meeting status timed out".to_string()),
            payload: json!({
                "active": false,
                "meetingCaptureId": "meeting-1",
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: Some(9876),
        };
        let payload = super::meeting_audio_status_shell_payload("meeting-1", &result);

        assert_eq!(payload["active"], false);
        assert_eq!(payload["reason"], "audioSidecarResponseTimeout");
        assert_ne!(payload["reason"], "meetingCaptureSourceInactive");
        assert_eq!(
            payload["sidecarStatus"]["errorCode"],
            "audioSidecarResponseTimeout"
        );

        let response = super::audio_sidecar_result_response_line(
            "meeting-status-failed",
            Instant::now(),
            payload,
            &result,
        );
        let response: serde_json::Value = serde_json::from_str(response.trim()).unwrap();
        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioSidecarResponseTimeout");
        assert_eq!(response["fallbackReason"], "meeting status timed out");
    }

    #[test]
    fn meeting_audio_status_preserves_normal_inactive_sidecar_reason() {
        let result = crate::audio_sidecar_client::AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "active": false,
                "meetingCaptureId": "meeting-1",
                "reason": "noActiveMeetingCapture",
            }),
            executable_available: true,
            executable_path_hash: Some("hash".to_string()),
            pid: None,
        };

        let payload = super::meeting_audio_status_shell_payload("meeting-1", &result);

        assert_eq!(payload["active"], false);
        assert_eq!(payload["reason"], "noActiveMeetingCapture");
        assert!(payload["sidecarStatus"]["errorCode"].is_null());
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
        let inject_budget = super::MAX_INJECT_TEXT_BYTES;
        let request_budget = super::MAX_REQUEST_BYTES;
        assert!(inject_budget + 8192 < request_budget);
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
    fn inject_response_payload_reports_deadline_budget() {
        let options = super::InjectTextOptions {
            text: "hello".to_string(),
            expected_foreground_title: "Scriber Hot Path Text Target".to_string(),
            restore_clipboard: true,
            restore_delay_ms: 1500,
            pre_delay_ms: 80,
            pre_delay_mode: "auto".to_string(),
            dispatch: "ctrlV".to_string(),
            max_clipboard_retries: 5,
            clipboard_retry_delay_ms: 5,
            deadline_ms: 2000,
        };

        let payload = super::inject_response_payload(
            &options,
            &["clipboard_set", "paste"],
            80,
            Some(1.0),
            Some(2.0),
            Some(3.0),
            10.0,
            json!({
                "scheduled": true,
                "attempted": false,
                "succeeded": null,
                "skippedReason": "scheduled",
                "errorCode": null,
            }),
            &json!({"available": false}),
            &json!({"available": false}),
        );

        assert_eq!(payload["deadlineMs"], 2000);
        assert_eq!(
            payload["expectedForegroundTitleHash"],
            super::hash_sensitive_identifier("Scriber Hot Path Text Target")
        );
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
