use serde_json::{json, Value};
use std::collections::HashMap;
use std::{
    env,
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, ExitStatus, Stdio},
    sync::{
        mpsc::{self, Receiver, RecvTimeoutError},
        Mutex, MutexGuard, OnceLock,
    },
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
            TH32CS_SNAPPROCESS,
        },
        Threading::{
            GetCurrentProcessId, OpenProcess, QueryFullProcessImageNameW, TerminateProcess,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
        },
    },
};

const AUDIO_SIDECAR_EXE_ENV: &str = "SCRIBER_AUDIO_SIDECAR_EXE";
const AUDIO_SIDECAR_PROTOCOL_VERSION: &str = "1";
const AUDIO_SIDECAR_NAME: &str = "scriber-audio-sidecar";
const SIDECAR_RESPONSE_TIMEOUT: Duration = Duration::from_secs(5);
const SIDECAR_STATUS_RESPONSE_TIMEOUT: Duration = Duration::from_millis(1_500);
const SIDECAR_SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(1_500);
const SIDECAR_SHUTDOWN_POLL_INTERVAL: Duration = Duration::from_millis(25);
const SIDECAR_JSON_LINE_MAX_BYTES: usize = 1024 * 1024;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

struct ActiveAudioSidecar {
    child: Child,
    stdin: ChildStdin,
    response_rx: Receiver<SidecarOutput>,
    path_hash: Option<String>,
    pid: u32,
    started_at: Instant,
}

enum SidecarOutput {
    Line(String),
    Eof,
    ReadError(String),
}

#[derive(Debug, Clone)]
struct SidecarExitInfo {
    status: Option<ExitStatus>,
    killed_after_timeout: bool,
    wait_error: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AudioSidecarCallResult {
    pub success: bool,
    pub error_code: Option<String>,
    pub fallback_reason: Option<String>,
    pub payload: Value,
    pub executable_available: bool,
    pub executable_path_hash: Option<String>,
    pub pid: Option<u32>,
}

pub fn audio_sidecar_executable_available() -> bool {
    find_audio_sidecar_executable().is_some()
}

pub fn call_audio_sidecar_command(command: &str, payload: Value) -> AudioSidecarCallResult {
    match command {
        "captureStart" => return start_audio_sidecar_capture(payload),
        "captureStatus" => return status_audio_sidecar_capture(payload),
        "captureStop" => return stop_audio_sidecar_capture(payload),
        "meetingCaptureStart" => return start_audio_sidecar_meeting_capture(payload),
        "meetingCaptureStatus" => return status_audio_sidecar_meeting_capture(payload),
        "meetingCaptureStop" => return stop_audio_sidecar_meeting_capture(payload),
        "prewarmStart" => return start_audio_sidecar_prewarm(payload),
        "prewarmStatus" => return status_audio_sidecar_prewarm(payload),
        "prewarmStop" => return stop_audio_sidecar_prewarm(payload),
        _ => {}
    }

    let Some(program) = find_audio_sidecar_executable() else {
        return unavailable_result(
            "audioCaptureUnavailable",
            "Rust audio sidecar executable was not found",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": false,
            }),
            None,
            None,
        );
    };
    call_audio_sidecar_command_once(&program, command, payload)
}

pub fn shutdown_all_audio_sidecars(reason: &str) -> usize {
    let mut sessions = lock_active_audio_sidecars();
    let entries: Vec<(String, ActiveAudioSidecar)> = sessions.drain().collect();
    drop(sessions);
    let mut prewarm_sessions = lock_active_audio_prewarm_sidecars();
    let prewarm_entries: Vec<(String, ActiveAudioSidecar)> = prewarm_sessions.drain().collect();
    drop(prewarm_sessions);
    let mut meeting_sessions = lock_active_audio_meeting_sidecars();
    let meeting_entries: Vec<(String, ActiveAudioSidecar)> = meeting_sessions.drain().collect();
    drop(meeting_sessions);

    let mut stopped = 0usize;
    for (stream_id, mut sidecar) in entries {
        stop_sidecar_process(&stream_id, &mut sidecar, reason);
        stopped = stopped.saturating_add(1);
    }
    for (prewarm_id, mut sidecar) in prewarm_entries {
        stop_prewarm_sidecar_process(&prewarm_id, &mut sidecar, reason);
        stopped = stopped.saturating_add(1);
    }
    for (meeting_capture_id, mut sidecar) in meeting_entries {
        stop_meeting_sidecar_process(&meeting_capture_id, &mut sidecar, reason);
        stopped = stopped.saturating_add(1);
    }
    stopped
}

pub fn cleanup_stray_audio_sidecar_processes(reason: &str) -> usize {
    cleanup_stray_audio_sidecar_processes_for(find_audio_sidecar_executable().as_deref(), reason)
}

fn active_audio_sidecars() -> &'static Mutex<HashMap<String, ActiveAudioSidecar>> {
    static ACTIVE_AUDIO_SIDECARS: OnceLock<Mutex<HashMap<String, ActiveAudioSidecar>>> =
        OnceLock::new();
    ACTIVE_AUDIO_SIDECARS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn active_audio_prewarm_sidecars() -> &'static Mutex<HashMap<String, ActiveAudioSidecar>> {
    static ACTIVE_AUDIO_PREWARM_SIDECARS: OnceLock<Mutex<HashMap<String, ActiveAudioSidecar>>> =
        OnceLock::new();
    ACTIVE_AUDIO_PREWARM_SIDECARS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn active_audio_meeting_sidecars() -> &'static Mutex<HashMap<String, ActiveAudioSidecar>> {
    static ACTIVE_AUDIO_MEETING_SIDECARS: OnceLock<Mutex<HashMap<String, ActiveAudioSidecar>>> =
        OnceLock::new();
    ACTIVE_AUDIO_MEETING_SIDECARS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn lock_active_audio_sidecars() -> MutexGuard<'static, HashMap<String, ActiveAudioSidecar>> {
    active_audio_sidecars()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn lock_active_audio_prewarm_sidecars() -> MutexGuard<'static, HashMap<String, ActiveAudioSidecar>>
{
    active_audio_prewarm_sidecars()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn lock_active_audio_meeting_sidecars() -> MutexGuard<'static, HashMap<String, ActiveAudioSidecar>>
{
    active_audio_meeting_sidecars()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn payload_prewarm_id(payload: &Value) -> String {
    payload
        .get("prewarmId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(96)
        .collect()
}

fn start_audio_sidecar_capture(payload: Value) -> AudioSidecarCallResult {
    let prewarm_id = payload_prewarm_id(&payload);
    if !prewarm_id.is_empty() {
        let mut prewarm_sessions = lock_active_audio_prewarm_sidecars();
        if let Some(sidecar) = prewarm_sessions.remove(&prewarm_id) {
            drop(prewarm_sessions);
            return start_audio_sidecar_capture_with_sidecar(sidecar, payload);
        }
        drop(prewarm_sessions);
        let executable_available = audio_sidecar_executable_available();
        return unavailable_result(
            "audioCaptureUnavailable",
            "Rust audio prewarm session was not found for capture adoption",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": executable_available,
                "prewarmAdoptionRequested": true,
            }),
            None,
            None,
        );
    }

    let Some(program) = find_audio_sidecar_executable() else {
        return unavailable_result(
            "audioCaptureUnavailable",
            "Rust audio sidecar executable was not found",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": false,
            }),
            None,
            None,
        );
    };
    start_audio_sidecar_capture_at(&program, payload)
}

fn start_audio_sidecar_capture_at(program: &Path, payload: Value) -> AudioSidecarCallResult {
    let path_hash = Some(hash_sensitive_identifier(&program.display().to_string()));
    let sidecar = match spawn_audio_sidecar_process(program, path_hash) {
        Ok(sidecar) => sidecar,
        Err(result) => return result,
    };
    start_audio_sidecar_capture_with_sidecar(sidecar, payload)
}

fn start_audio_sidecar_capture_with_sidecar(
    mut sidecar: ActiveAudioSidecar,
    payload: Value,
) -> AudioSidecarCallResult {
    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, "captureStart", payload);
    let path_hash = sidecar.path_hash.clone();

    if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash,
            Some(sidecar.pid),
        );
    }

    let response = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => {
            parse_sidecar_response(&line, &request_id, path_hash.clone(), Some(sidecar.pid))
        }
        Err(result) => {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
            return with_process_identity(result, path_hash, Some(sidecar.pid));
        }
    };

    if !response.success {
        stop_sidecar_process("", &mut sidecar, "captureStartFailed");
        return response;
    }

    let stream_id = response
        .payload
        .get("streamId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if stream_id.is_empty() {
        stop_sidecar_process("", &mut sidecar, "missingStreamId");
        return unavailable_result(
            "audioSidecarMissingStreamId",
            "Rust audio sidecar captureStart succeeded without streamId",
            response.payload,
            path_hash,
            Some(sidecar.pid),
        );
    }

    let replaced = {
        let mut sessions = lock_active_audio_sidecars();
        sessions
            .insert(stream_id.clone(), sidecar)
            .map(|old| vec![(stream_id.clone(), old)])
            .unwrap_or_default()
    };
    for (old_stream_id, mut old_sidecar) in replaced {
        let reason = if old_stream_id == stream_id {
            "duplicateStreamId"
        } else {
            "captureStartReplacedActiveCapture"
        };
        stop_sidecar_process(&old_stream_id, &mut old_sidecar, reason);
    }
    response
}

fn start_audio_sidecar_meeting_capture(payload: Value) -> AudioSidecarCallResult {
    let Some(program) = find_audio_sidecar_executable() else {
        return unavailable_result(
            "meetingCaptureUnavailable",
            "Rust audio sidecar executable was not found",
            json!({"sidecar": AUDIO_SIDECAR_NAME, "sidecarExecutableAvailable": false}),
            None,
            None,
        );
    };
    let path_hash = Some(hash_sensitive_identifier(&program.display().to_string()));
    let mut sidecar = match spawn_audio_sidecar_process(&program, path_hash.clone()) {
        Ok(sidecar) => sidecar,
        Err(result) => return result,
    };
    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, "meetingCaptureStart", payload);
    if let Err(error) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            error,
            json!({"sidecar": AUDIO_SIDECAR_NAME, "meetingCaptureAvailable": false}),
            path_hash,
            Some(sidecar.pid),
        );
    }
    let response = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => {
            parse_sidecar_response(&line, &request_id, path_hash.clone(), Some(sidecar.pid))
        }
        Err(result) => {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
            return with_process_identity(result, path_hash, Some(sidecar.pid));
        }
    };
    if !response.success {
        stop_meeting_sidecar_process("", &mut sidecar, "meetingCaptureStartFailed");
        return response;
    }
    let meeting_capture_id = response
        .payload
        .get("meetingCaptureId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if meeting_capture_id.is_empty() {
        stop_meeting_sidecar_process("", &mut sidecar, "missingMeetingCaptureId");
        return unavailable_result(
            "audioSidecarMissingMeetingCaptureId",
            "Rust audio sidecar meetingCaptureStart succeeded without meetingCaptureId",
            response.payload,
            path_hash,
            Some(sidecar.pid),
        );
    }
    let replaced = {
        let mut sessions = lock_active_audio_meeting_sidecars();
        let replaced: Vec<(String, ActiveAudioSidecar)> = sessions.drain().collect();
        sessions.insert(meeting_capture_id.clone(), sidecar);
        replaced
    };
    for (old_id, mut old_sidecar) in replaced {
        stop_meeting_sidecar_process(&old_id, &mut old_sidecar, "meetingCaptureReplaced");
    }
    response
}

fn meeting_capture_id(payload: &Value) -> String {
    payload
        .get("meetingCaptureId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(96)
        .collect()
}

fn status_audio_sidecar_meeting_capture(payload: Value) -> AudioSidecarCallResult {
    call_active_meeting_sidecar("meetingCaptureStatus", payload, false)
}

fn stop_audio_sidecar_meeting_capture(payload: Value) -> AudioSidecarCallResult {
    call_active_meeting_sidecar("meetingCaptureStop", payload, true)
}

fn call_active_meeting_sidecar(
    command: &str,
    payload: Value,
    remove: bool,
) -> AudioSidecarCallResult {
    let id = meeting_capture_id(&payload);
    if id.is_empty() {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({"active": false, "meetingCaptureId": "", "reason": "missingMeetingCaptureId"}),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }
    let mut sessions = lock_active_audio_meeting_sidecars();
    if !sessions.contains_key(&id) {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({"active": false, "meetingCaptureId": id, "reason": "noActiveMeetingCapture"}),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }
    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, command, json!({"meetingCaptureId": id}));
    let response = {
        let sidecar = sessions.get_mut(&id).unwrap();
        let path_hash = sidecar.path_hash.clone();
        let pid = Some(sidecar.pid);
        if let Err(error) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
            unavailable_result(
                "audioSidecarWriteFailed",
                error,
                json!({"active": false, "meetingCaptureId": id}),
                path_hash,
                pid,
            )
        } else {
            match read_sidecar_response_line_with_timeout(
                &sidecar.response_rx,
                if remove {
                    SIDECAR_RESPONSE_TIMEOUT
                } else {
                    SIDECAR_STATUS_RESPONSE_TIMEOUT
                },
            ) {
                Ok(line) => parse_sidecar_response(&line, &request_id, path_hash, pid),
                Err(result) => result,
            }
        }
    };
    // A meeting status check is observational. Even when the relay has
    // finished, keep the process registered until meetingCaptureStop can join
    // its workers and return the redacted relay/writer diagnostics.
    if remove || !response.success {
        if let Some(mut sidecar) = sessions.remove(&id) {
            let _ = write_sidecar_json_line(&mut sidecar.stdin, &shutdown_request());
            let _ = wait_for_sidecar_exit_or_kill(&mut sidecar.child, SIDECAR_SHUTDOWN_TIMEOUT);
        }
    }
    response
}

fn start_audio_sidecar_prewarm(payload: Value) -> AudioSidecarCallResult {
    let Some(program) = find_audio_sidecar_executable() else {
        return unavailable_result(
            "audioPrewarmUnavailable",
            "Rust audio sidecar executable was not found",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": false,
            }),
            None,
            None,
        );
    };
    start_audio_sidecar_prewarm_at(&program, payload)
}

fn start_audio_sidecar_prewarm_at(program: &Path, payload: Value) -> AudioSidecarCallResult {
    let path_hash = Some(hash_sensitive_identifier(&program.display().to_string()));
    let mut sidecar = match spawn_audio_sidecar_process(program, path_hash.clone()) {
        Ok(sidecar) => sidecar,
        Err(result) => return result,
    };
    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, "prewarmStart", payload);

    if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash,
            Some(sidecar.pid),
        );
    }

    let response = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => {
            parse_sidecar_response(&line, &request_id, path_hash.clone(), Some(sidecar.pid))
        }
        Err(result) => {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
            return with_process_identity(result, path_hash, Some(sidecar.pid));
        }
    };

    if !response.success {
        stop_prewarm_sidecar_process("", &mut sidecar, "prewarmStartFailed");
        return response;
    }

    let prewarm_id = response
        .payload
        .get("prewarmId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if prewarm_id.is_empty() {
        stop_prewarm_sidecar_process("", &mut sidecar, "missingPrewarmId");
        return unavailable_result(
            "audioSidecarMissingPrewarmId",
            "Rust audio sidecar prewarmStart succeeded without prewarmId",
            response.payload,
            path_hash,
            Some(sidecar.pid),
        );
    }

    let replaced = {
        let mut sessions = lock_active_audio_prewarm_sidecars();
        let replaced: Vec<(String, ActiveAudioSidecar)> = sessions.drain().collect();
        sessions.insert(prewarm_id.clone(), sidecar);
        replaced
    };
    for (old_prewarm_id, mut old_sidecar) in replaced {
        let reason = if old_prewarm_id == prewarm_id {
            "duplicatePrewarmId"
        } else {
            "prewarmStartReplacedActivePrewarm"
        };
        stop_prewarm_sidecar_process(&old_prewarm_id, &mut old_sidecar, reason);
    }
    response
}

fn stop_audio_sidecar_capture(payload: Value) -> AudioSidecarCallResult {
    let stream_id = payload
        .get("streamId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(96)
        .collect::<String>();
    if stream_id.is_empty() {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "stopped": false,
                "streamId": "",
                "reason": "missingStreamId",
            }),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }

    let mut sessions = lock_active_audio_sidecars();
    let Some(mut sidecar) = sessions.remove(&stream_id) else {
        let executable_available = audio_sidecar_executable_available();
        let reason = if executable_available {
            "noActiveCapture"
        } else {
            "noRustAudioSidecar"
        };
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "stopped": false,
                "streamId": stream_id,
                "reason": reason,
            }),
            executable_available,
            executable_path_hash: None,
            pid: None,
        };
    };
    drop(sessions);

    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(
        &request_id,
        "captureStop",
        json!({
            "streamId": stream_id,
        }),
    );
    let path_hash = sidecar.path_hash.clone();
    let pid = Some(sidecar.pid);
    if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
                "streamId": stream_id,
                "stopped": false,
            }),
            path_hash,
            pid,
        );
    }

    let mut result = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => parse_sidecar_response(&line, &request_id, path_hash.clone(), pid),
        Err(result) => with_process_identity(result, path_hash.clone(), pid),
    };
    let _ = write_sidecar_json_line(&mut sidecar.stdin, &shutdown_request());
    let exit = wait_for_sidecar_exit_or_kill(&mut sidecar.child, SIDECAR_SHUTDOWN_TIMEOUT);
    if let Some(object) = result.payload.as_object_mut() {
        object.insert(
            "sidecarUptimeMs".to_string(),
            json!(sidecar
                .started_at
                .elapsed()
                .as_millis()
                .min(u128::from(u64::MAX)) as u64),
        );
        object.insert(
            "exitStatus".to_string(),
            json!(exit.status.as_ref().and_then(|value| value.code())),
        );
        insert_sidecar_exit_info_fields(object, &exit);
        object.insert("sidecarPid".to_string(), json!(sidecar.pid));
        object.insert("sidecarPathHash".to_string(), json!(path_hash));
    }
    result
}

fn status_audio_sidecar_capture(payload: Value) -> AudioSidecarCallResult {
    let stream_id = payload
        .get("streamId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(96)
        .collect::<String>();
    if stream_id.is_empty() {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({"active": false, "streamId": "", "reason": "missingStreamId"}),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }

    let mut sessions = lock_active_audio_sidecars();
    let Some(sidecar) = sessions.get_mut(&stream_id) else {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({"active": false, "streamId": stream_id, "reason": "noActiveCapture"}),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    };
    let path_hash = sidecar.path_hash.clone();
    let pid = Some(sidecar.pid);
    match sidecar.child.try_wait() {
        Ok(Some(status)) => {
            let _ = sessions.remove(&stream_id);
            return AudioSidecarCallResult {
                success: true,
                error_code: None,
                fallback_reason: None,
                payload: json!({
                    "active": false,
                    "streamId": stream_id,
                    "reason": "captureProcessExited",
                    "exitStatus": status.code(),
                }),
                executable_available: true,
                executable_path_hash: path_hash,
                pid,
            };
        }
        Err(err) => {
            return unavailable_result(
                "audioSidecarStatusFailed",
                format!("Rust audio capture status check failed: {err}"),
                json!({"active": false, "streamId": stream_id, "reason": "captureStatusFailed"}),
                path_hash,
                pid,
            );
        }
        Ok(None) => {}
    }

    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, "captureStatus", json!({"streamId": stream_id}));
    if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        let _ = sessions.remove(&stream_id);
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({"active": false, "streamId": stream_id, "reason": "captureStatusWriteFailed"}),
            path_hash,
            pid,
        );
    }
    match read_sidecar_response_line_with_timeout(
        &sidecar.response_rx,
        SIDECAR_STATUS_RESPONSE_TIMEOUT,
    ) {
        Ok(line) => parse_sidecar_response(&line, &request_id, path_hash, pid),
        Err(result) => {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
            let _ = sessions.remove(&stream_id);
            with_process_identity(result, path_hash, pid)
        }
    }
}

fn stop_audio_sidecar_prewarm(payload: Value) -> AudioSidecarCallResult {
    let prewarm_id = payload
        .get("prewarmId")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .chars()
        .take(96)
        .collect::<String>();
    if prewarm_id.is_empty() {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "stopped": false,
                "prewarmId": "",
                "reason": "missingPrewarmId",
            }),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }

    let mut sessions = lock_active_audio_prewarm_sidecars();
    let Some(mut sidecar) = sessions.remove(&prewarm_id) else {
        let executable_available = audio_sidecar_executable_available();
        let reason = if executable_available {
            "noActivePrewarm"
        } else {
            "noRustAudioSidecar"
        };
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "stopped": false,
                "prewarmId": prewarm_id,
                "reason": reason,
            }),
            executable_available,
            executable_path_hash: None,
            pid: None,
        };
    };
    drop(sessions);

    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(
        &request_id,
        "prewarmStop",
        json!({
            "prewarmId": prewarm_id,
        }),
    );
    let path_hash = sidecar.path_hash.clone();
    let pid = Some(sidecar.pid);
    if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
                "prewarmId": prewarm_id,
                "stopped": false,
            }),
            path_hash,
            pid,
        );
    }

    let mut result = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => parse_sidecar_response(&line, &request_id, path_hash.clone(), pid),
        Err(result) => with_process_identity(result, path_hash.clone(), pid),
    };
    let _ = write_sidecar_json_line(&mut sidecar.stdin, &shutdown_request());
    let exit = wait_for_sidecar_exit_or_kill(&mut sidecar.child, SIDECAR_SHUTDOWN_TIMEOUT);
    if let Some(object) = result.payload.as_object_mut() {
        object.insert(
            "sidecarUptimeMs".to_string(),
            json!(sidecar
                .started_at
                .elapsed()
                .as_millis()
                .min(u128::from(u64::MAX)) as u64),
        );
        object.insert(
            "exitStatus".to_string(),
            json!(exit.status.as_ref().and_then(|value| value.code())),
        );
        insert_sidecar_exit_info_fields(object, &exit);
        object.insert("sidecarPid".to_string(), json!(sidecar.pid));
        object.insert("sidecarPathHash".to_string(), json!(path_hash));
    }
    result
}

fn status_audio_sidecar_prewarm(payload: Value) -> AudioSidecarCallResult {
    let prewarm_id = payload_prewarm_id(&payload);
    if prewarm_id.is_empty() {
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "active": false,
                "prewarmId": "",
                "reason": "missingPrewarmId",
            }),
            executable_available: audio_sidecar_executable_available(),
            executable_path_hash: None,
            pid: None,
        };
    }

    let mut sessions = lock_active_audio_prewarm_sidecars();
    if !sessions.contains_key(&prewarm_id) {
        let executable_available = audio_sidecar_executable_available();
        let reason = if executable_available {
            "noActivePrewarm"
        } else {
            "noRustAudioSidecar"
        };
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "active": false,
                "prewarmId": prewarm_id,
                "reason": reason,
            }),
            executable_available,
            executable_path_hash: None,
            pid: None,
        };
    }

    let exited = {
        let sidecar = sessions.get_mut(&prewarm_id).unwrap();
        match sidecar.child.try_wait() {
            Ok(Some(status)) => Some((status, sidecar.path_hash.clone(), sidecar.pid)),
            Ok(None) => None,
            Err(err) => {
                let path_hash = sidecar.path_hash.clone();
                let pid = sidecar.pid;
                let mut result = unavailable_result(
                    "audioSidecarStatusFailed",
                    format!("Rust audio prewarm status check failed: {err}"),
                    json!({
                        "sidecar": AUDIO_SIDECAR_NAME,
                        "active": false,
                        "prewarmId": prewarm_id,
                        "reason": "prewarmStatusFailed",
                    }),
                    path_hash,
                    Some(pid),
                );
                if let Some(object) = result.payload.as_object_mut() {
                    object.insert("sidecarPid".to_string(), json!(pid));
                }
                return result;
            }
        }
    };
    if let Some((status, path_hash, pid)) = exited {
        let _ = sessions.remove(&prewarm_id);
        return AudioSidecarCallResult {
            success: true,
            error_code: None,
            fallback_reason: None,
            payload: json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "active": false,
                "prewarmId": prewarm_id,
                "reason": "prewarmProcessExited",
                "exitStatus": status.code(),
                "sidecarPid": pid,
                "sidecarPathHash": path_hash,
            }),
            executable_available: true,
            executable_path_hash: path_hash,
            pid: Some(pid),
        };
    }

    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(
        &request_id,
        "prewarmStatus",
        json!({
            "prewarmId": prewarm_id,
        }),
    );

    let mut remove_after_status_error = false;
    let response = {
        let sidecar = sessions.get_mut(&prewarm_id).unwrap();
        let path_hash = sidecar.path_hash.clone();
        let pid = Some(sidecar.pid);
        if let Err(err) = write_sidecar_json_line(&mut sidecar.stdin, &request) {
            let _ = sidecar.child.kill();
            let _ = sidecar.child.wait();
            remove_after_status_error = true;
            unavailable_result(
                "audioSidecarWriteFailed",
                err,
                json!({
                    "sidecar": AUDIO_SIDECAR_NAME,
                    "active": false,
                    "prewarmId": prewarm_id,
                    "reason": "prewarmStatusWriteFailed",
                }),
                path_hash,
                pid,
            )
        } else {
            match read_sidecar_response_line_with_timeout(
                &sidecar.response_rx,
                SIDECAR_STATUS_RESPONSE_TIMEOUT,
            ) {
                Ok(line) => parse_sidecar_response(&line, &request_id, path_hash.clone(), pid),
                Err(result) => {
                    let _ = sidecar.child.kill();
                    let _ = sidecar.child.wait();
                    remove_after_status_error = true;
                    with_process_identity(result, path_hash, pid)
                }
            }
        }
    };

    if remove_after_status_error {
        let _ = sessions.remove(&prewarm_id);
    }

    if response.success
        && !response
            .payload
            .get("active")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    {
        if let Some(mut sidecar) = sessions.remove(&prewarm_id) {
            stop_prewarm_sidecar_process(&prewarm_id, &mut sidecar, "prewarmStatusInactive");
        }
    }
    response
}

fn call_audio_sidecar_command_once(
    program: &Path,
    command: &str,
    payload: Value,
) -> AudioSidecarCallResult {
    let request_id = Uuid::new_v4().simple().to_string();
    let request = sidecar_request(&request_id, command, payload);
    let shutdown = shutdown_request();
    let path_hash = Some(hash_sensitive_identifier(&program.display().to_string()));
    let mut sidecar = match spawn_audio_sidecar_process(program, path_hash.clone()) {
        Ok(sidecar) => sidecar,
        Err(result) => return result,
    };

    let write_result = (|| -> Result<(), String> {
        write_sidecar_json_line(&mut sidecar.stdin, &request)?;
        write_sidecar_json_line(&mut sidecar.stdin, &shutdown)?;
        Ok(())
    })();

    if let Err(err) = write_result {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash,
            Some(sidecar.pid),
        );
    }

    let response = match read_sidecar_response_line(&sidecar.response_rx) {
        Ok(line) => {
            parse_sidecar_response(&line, &request_id, path_hash.clone(), Some(sidecar.pid))
        }
        Err(result) => with_process_identity(result, path_hash.clone(), Some(sidecar.pid)),
    };
    let exit = wait_for_sidecar_exit_or_kill(&mut sidecar.child, SIDECAR_SHUTDOWN_TIMEOUT);
    if response.error_code.as_deref() == Some("audioSidecarEmptyResponse") {
        return with_exit_status(response, &exit);
    }
    response
}

fn spawn_audio_sidecar_process(
    program: &Path,
    path_hash: Option<String>,
) -> Result<ActiveAudioSidecar, AudioSidecarCallResult> {
    let mut process = Command::new(program);
    process
        .arg("--stdio")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_child_console_window(&mut process);

    let mut child = process.spawn().map_err(|err| {
        unavailable_result(
            "audioSidecarSpawnFailed",
            format!("Rust audio sidecar spawn failed: {err}"),
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash.clone(),
            None,
        )
    })?;
    let pid = child.id();
    let Some(stdin) = child.stdin.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(unavailable_result(
            "audioSidecarPipeUnavailable",
            "Rust audio sidecar stdin was unavailable",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash.clone(),
            Some(pid),
        ));
    };
    let Some(stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(unavailable_result(
            "audioSidecarPipeUnavailable",
            "Rust audio sidecar stdout was unavailable",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash.clone(),
            Some(pid),
        ));
    };
    let (response_tx, response_rx) = mpsc::channel();
    thread::Builder::new()
        .name("scriber-audio-sidecar-stdout".to_string())
        .spawn(move || {
            let mut stdout = BufReader::new(stdout);
            let mut line = String::new();
            loop {
                line.clear();
                match read_json_line_limited(&mut stdout, &mut line) {
                    Ok(0) => {
                        let _ = response_tx.send(SidecarOutput::Eof);
                        break;
                    }
                    Ok(_) if line.trim().is_empty() => continue,
                    Ok(_) => {
                        if response_tx
                            .send(SidecarOutput::Line(std::mem::take(&mut line)))
                            .is_err()
                        {
                            break;
                        }
                    }
                    Err(err) => {
                        let _ = response_tx.send(SidecarOutput::ReadError(err.to_string()));
                        break;
                    }
                }
            }
        })
        .map_err(|err| {
            let _ = child.kill();
            let _ = child.wait();
            unavailable_result(
                "audioSidecarReaderSpawnFailed",
                format!("Rust audio sidecar stdout reader failed to start: {err}"),
                json!({
                    "sidecar": AUDIO_SIDECAR_NAME,
                    "sidecarExecutableAvailable": true,
                }),
                path_hash.clone(),
                Some(pid),
            )
        })?;
    Ok(ActiveAudioSidecar {
        child,
        stdin,
        response_rx,
        path_hash,
        pid,
        started_at: Instant::now(),
    })
}

fn read_json_line_limited<R: BufRead>(reader: &mut R, line: &mut String) -> io::Result<usize> {
    line.clear();
    let bytes_read = reader
        .take((SIDECAR_JSON_LINE_MAX_BYTES + 1) as u64)
        .read_line(line)?;
    if bytes_read > SIDECAR_JSON_LINE_MAX_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "audio sidecar response line exceeded limit",
        ));
    }
    Ok(bytes_read)
}

fn sidecar_request(request_id: &str, command: &str, payload: Value) -> Value {
    json!({
        "protocolVersion": AUDIO_SIDECAR_PROTOCOL_VERSION,
        "requestId": request_id,
        "command": command,
        "payload": payload,
    })
}

fn shutdown_request() -> Value {
    sidecar_request(&Uuid::new_v4().simple().to_string(), "shutdown", json!({}))
}

fn write_sidecar_json_line(stdin: &mut ChildStdin, value: &Value) -> Result<(), String> {
    writeln!(stdin, "{value}").map_err(|err| format!("sidecar request write failed: {err}"))?;
    stdin
        .flush()
        .map_err(|err| format!("sidecar stdin flush failed: {err}"))
}

fn read_sidecar_response_line(
    response_rx: &Receiver<SidecarOutput>,
) -> Result<String, AudioSidecarCallResult> {
    read_sidecar_response_line_with_timeout(response_rx, SIDECAR_RESPONSE_TIMEOUT)
}

fn read_sidecar_response_line_with_timeout(
    response_rx: &Receiver<SidecarOutput>,
    timeout: Duration,
) -> Result<String, AudioSidecarCallResult> {
    match response_rx.recv_timeout(timeout) {
        Ok(SidecarOutput::Line(line)) => Ok(line),
        Ok(SidecarOutput::Eof) | Err(RecvTimeoutError::Disconnected) => Err(unavailable_result(
            "audioSidecarEmptyResponse",
            "Rust audio sidecar returned no response",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            None,
            None,
        )),
        Ok(SidecarOutput::ReadError(err)) => Err(unavailable_result(
            "audioSidecarReadFailed",
            format!("Rust audio sidecar read failed: {err}"),
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            None,
            None,
        )),
        Err(RecvTimeoutError::Timeout) => Err(unavailable_result(
            "audioSidecarResponseTimeout",
            "Rust audio sidecar response timed out",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
                "responseTimeoutMs": timeout.as_millis().min(u128::from(u64::MAX)) as u64,
            }),
            None,
            None,
        )),
    }
}

fn stop_sidecar_process(stream_id: &str, sidecar: &mut ActiveAudioSidecar, reason: &str) {
    stop_sidecar_process_with_command(stream_id, "streamId", "captureStop", sidecar, reason);
}

fn stop_prewarm_sidecar_process(prewarm_id: &str, sidecar: &mut ActiveAudioSidecar, reason: &str) {
    stop_sidecar_process_with_command(prewarm_id, "prewarmId", "prewarmStop", sidecar, reason);
}

fn stop_meeting_sidecar_process(
    meeting_capture_id: &str,
    sidecar: &mut ActiveAudioSidecar,
    reason: &str,
) {
    stop_sidecar_process_with_command(
        meeting_capture_id,
        "meetingCaptureId",
        "meetingCaptureStop",
        sidecar,
        reason,
    );
}

fn stop_sidecar_process_with_command(
    session_id: &str,
    id_key: &str,
    stop_command: &str,
    sidecar: &mut ActiveAudioSidecar,
    reason: &str,
) {
    if !session_id.is_empty() {
        let mut stop_payload = serde_json::Map::new();
        stop_payload.insert(id_key.to_string(), json!(session_id));
        stop_payload.insert("reason".to_string(), json!(reason));
        let _ = write_sidecar_json_line(
            &mut sidecar.stdin,
            &sidecar_request(
                &Uuid::new_v4().simple().to_string(),
                stop_command,
                Value::Object(stop_payload),
            ),
        );
    }
    let _ = write_sidecar_json_line(&mut sidecar.stdin, &shutdown_request());
    let _ = wait_for_sidecar_exit_or_kill(&mut sidecar.child, SIDECAR_SHUTDOWN_TIMEOUT);
}

fn with_process_identity(
    mut result: AudioSidecarCallResult,
    path_hash: Option<String>,
    pid: Option<u32>,
) -> AudioSidecarCallResult {
    result.executable_available = path_hash.is_some();
    result.executable_path_hash = path_hash;
    result.pid = pid;
    result
}

fn wait_for_sidecar_exit_or_kill(child: &mut Child, timeout: Duration) -> SidecarExitInfo {
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return SidecarExitInfo {
                    status: Some(status),
                    killed_after_timeout: false,
                    wait_error: None,
                };
            }
            Ok(None) => {
                if started.elapsed() >= timeout {
                    let kill_error = child.kill().err().map(|err| err.to_string());
                    let wait_result = child.wait();
                    return SidecarExitInfo {
                        status: wait_result.ok(),
                        killed_after_timeout: true,
                        wait_error: kill_error,
                    };
                }
                thread::sleep(SIDECAR_SHUTDOWN_POLL_INTERVAL.min(timeout));
            }
            Err(err) => {
                let kill_error = child.kill().err().map(|kill_err| kill_err.to_string());
                let _ = child.wait();
                return SidecarExitInfo {
                    status: None,
                    killed_after_timeout: true,
                    wait_error: Some(match kill_error {
                        Some(kill_err) => format!("{err}; kill failed: {kill_err}"),
                        None => err.to_string(),
                    }),
                };
            }
        }
    }
}

fn insert_sidecar_exit_info_fields(
    object: &mut serde_json::Map<String, Value>,
    exit: &SidecarExitInfo,
) {
    object.insert(
        "sidecarKilledAfterTimeout".to_string(),
        json!(exit.killed_after_timeout),
    );
    object.insert(
        "sidecarWaitError".to_string(),
        json!(exit.wait_error.as_deref()),
    );
}

fn with_exit_status(
    mut result: AudioSidecarCallResult,
    exit: &SidecarExitInfo,
) -> AudioSidecarCallResult {
    if let Some(object) = result.payload.as_object_mut() {
        object.insert(
            "exitStatus".to_string(),
            json!(exit.status.as_ref().and_then(|value| value.code())),
        );
        insert_sidecar_exit_info_fields(object, exit);
    }
    result
}

#[cfg(windows)]
fn cleanup_stray_audio_sidecar_processes_for(program: Option<&Path>, _reason: &str) -> usize {
    let Some(program) = program else {
        return 0;
    };
    let target_path = normalized_process_path(program);
    if target_path.is_empty() {
        return 0;
    }

    let current_pid = unsafe { GetCurrentProcessId() };
    let mut stopped = 0usize;
    for pid in matching_audio_sidecar_process_ids(&target_path) {
        if pid == current_pid {
            continue;
        }
        if terminate_process_by_id(pid) {
            stopped = stopped.saturating_add(1);
        }
    }
    stopped
}

#[cfg(not(windows))]
fn cleanup_stray_audio_sidecar_processes_for(_program: Option<&Path>, _reason: &str) -> usize {
    0
}

#[cfg(windows)]
fn matching_audio_sidecar_process_ids(target_path: &str) -> Vec<u32> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }

    let mut pids = Vec::new();
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut has_entry = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
    while has_entry {
        let exe_name = wide_process_name(&entry.szExeFile);
        if audio_sidecar_executable_names()
            .iter()
            .any(|allowed| exe_name.eq_ignore_ascii_case(allowed))
        {
            if let Some(path) = process_image_path(entry.th32ProcessID) {
                if normalized_process_path(&path) == target_path {
                    pids.push(entry.th32ProcessID);
                }
            }
        }
        has_entry = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
    }
    unsafe {
        let _ = CloseHandle(snapshot);
    }
    pids
}

#[cfg(windows)]
fn process_image_path(pid: u32) -> Option<PathBuf> {
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return None;
    }
    let mut buffer = vec![0u16; 32_768];
    let mut size = buffer.len() as u32;
    let ok = unsafe { QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut size) };
    unsafe {
        let _ = CloseHandle(handle);
    }
    if ok == 0 || size == 0 {
        return None;
    }
    buffer.truncate(size as usize);
    Some(PathBuf::from(String::from_utf16_lossy(&buffer)))
}

#[cfg(windows)]
fn terminate_process_by_id(pid: u32) -> bool {
    let handle = unsafe { OpenProcess(PROCESS_TERMINATE, 0, pid) };
    if handle.is_null() {
        return false;
    }
    let terminated = unsafe { TerminateProcess(handle, 0) } != 0;
    unsafe {
        let _ = CloseHandle(handle);
    }
    terminated
}

#[cfg(windows)]
fn normalized_process_path(path: &Path) -> String {
    path.canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .replace('/', "\\")
        .trim()
        .to_ascii_lowercase()
}

#[cfg(windows)]
fn wide_process_name(raw: &[u16]) -> String {
    let len = raw
        .iter()
        .position(|value| *value == 0)
        .unwrap_or(raw.len());
    String::from_utf16_lossy(&raw[..len])
}

fn parse_sidecar_response(
    raw: &str,
    expected_request_id: &str,
    path_hash: Option<String>,
    pid: Option<u32>,
) -> AudioSidecarCallResult {
    let parsed = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(map)) => Value::Object(map),
        Ok(_) => {
            return unavailable_result(
                "audioSidecarInvalidResponse",
                "Rust audio sidecar response was not an object",
                json!({"sidecar": AUDIO_SIDECAR_NAME, "sidecarExecutableAvailable": true}),
                path_hash,
                pid,
            )
        }
        Err(err) => {
            return unavailable_result(
                "audioSidecarInvalidJson",
                format!("Rust audio sidecar returned invalid JSON: {err}"),
                json!({"sidecar": AUDIO_SIDECAR_NAME, "sidecarExecutableAvailable": true}),
                path_hash,
                pid,
            )
        }
    };

    if parsed.get("protocolVersion").and_then(Value::as_str) != Some(AUDIO_SIDECAR_PROTOCOL_VERSION)
    {
        return unavailable_result(
            "audioSidecarProtocolMismatch",
            "Rust audio sidecar protocolVersion mismatch",
            json!({"sidecar": AUDIO_SIDECAR_NAME, "sidecarExecutableAvailable": true}),
            path_hash,
            pid,
        );
    }
    if parsed.get("requestId").and_then(Value::as_str) != Some(expected_request_id) {
        return unavailable_result(
            "audioSidecarRequestMismatch",
            "Rust audio sidecar requestId mismatch",
            json!({"sidecar": AUDIO_SIDECAR_NAME, "sidecarExecutableAvailable": true}),
            path_hash,
            pid,
        );
    }
    let success = parsed
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    AudioSidecarCallResult {
        success,
        error_code: parsed
            .get("errorCode")
            .and_then(Value::as_str)
            .map(str::to_string),
        fallback_reason: parsed
            .get("fallbackReason")
            .and_then(Value::as_str)
            .map(str::to_string),
        payload: parsed.get("payload").cloned().unwrap_or_else(|| json!({})),
        executable_available: true,
        executable_path_hash: path_hash,
        pid,
    }
}

fn unavailable_result(
    code: impl Into<String>,
    reason: impl Into<String>,
    payload: Value,
    executable_path_hash: Option<String>,
    pid: Option<u32>,
) -> AudioSidecarCallResult {
    AudioSidecarCallResult {
        success: false,
        error_code: Some(code.into()),
        fallback_reason: Some(reason.into()),
        payload,
        executable_available: executable_path_hash.is_some(),
        executable_path_hash,
        pid,
    }
}

fn find_audio_sidecar_executable() -> Option<PathBuf> {
    if let Ok(raw) = env::var(AUDIO_SIDECAR_EXE_ENV) {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            let path = PathBuf::from(trimmed);
            if is_allowed_audio_sidecar_executable_name(&path) && path.is_file() {
                return Some(path);
            }
        }
    }
    find_audio_sidecar_executable_in_dirs(
        &audio_sidecar_executable_dirs(),
        audio_sidecar_executable_names(),
    )
}

fn audio_sidecar_executable_dirs() -> Vec<PathBuf> {
    let exe_parent = env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(Path::to_path_buf));
    let current_dir = env::current_dir().ok();
    audio_sidecar_executable_dirs_for(exe_parent.as_deref(), current_dir.as_deref())
}

fn audio_sidecar_executable_dirs_for(
    exe_parent: Option<&Path>,
    current_dir: Option<&Path>,
) -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Some(parent) = exe_parent {
        push_unique_dir(&mut dirs, parent.to_path_buf());
        push_unique_dir(&mut dirs, parent.join("audio-sidecar"));
        push_unique_dir(&mut dirs, parent.join("resources").join("audio-sidecar"));
        push_unique_dir(&mut dirs, parent.join("resources"));
    }
    if let Some(current_dir) = current_dir {
        push_unique_dir(&mut dirs, current_dir.to_path_buf());
        push_unique_dir(&mut dirs, current_dir.join("audio-sidecar"));
        push_unique_dir(
            &mut dirs,
            current_dir.join("resources").join("audio-sidecar"),
        );
    }
    dirs
}

fn find_audio_sidecar_executable_in_dirs(dirs: &[PathBuf], names: &[&str]) -> Option<PathBuf> {
    for dir in dirs {
        for name in names {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

fn is_allowed_audio_sidecar_executable_name(path: &Path) -> bool {
    let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    audio_sidecar_executable_names()
        .iter()
        .any(|allowed| file_name.eq_ignore_ascii_case(allowed))
}

#[cfg(windows)]
fn audio_sidecar_executable_names() -> &'static [&'static str] {
    &[
        "scriber-audio-sidecar.exe",
        "scriber-audio-sidecar-x86_64-pc-windows-msvc.exe",
    ]
}

#[cfg(not(windows))]
fn audio_sidecar_executable_names() -> &'static [&'static str] {
    &[
        "scriber-audio-sidecar",
        "scriber-audio-sidecar-x86_64-unknown-linux-gnu",
        "scriber-audio-sidecar-aarch64-apple-darwin",
        "scriber-audio-sidecar-x86_64-apple-darwin",
    ]
}

fn push_unique_dir(dirs: &mut Vec<PathBuf>, dir: PathBuf) {
    if !dirs.iter().any(|existing| existing == &dir) {
        dirs.push(dir);
    }
}

fn hash_sensitive_identifier(raw: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in raw.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

#[cfg(windows)]
fn hide_child_console_window(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_child_console_window(_command: &mut Command) {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, time::SystemTime};

    fn unique_test_dir(label: &str) -> PathBuf {
        let mut dir = env::temp_dir();
        let unique = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        dir.push(format!("scriber-audio-sidecar-{label}-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn sidecar_response_reader_rejects_oversized_lines() {
        let mut input = io::Cursor::new(vec![b'x'; SIDECAR_JSON_LINE_MAX_BYTES + 1]);
        let mut line = String::new();

        let error = read_json_line_limited(&mut input, &mut line).unwrap_err();

        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[cfg(windows)]
    fn long_running_test_process() -> Command {
        let mut command = Command::new("cmd");
        command.args(["/C", "ping -n 30 127.0.0.1 >NUL"]);
        command
    }

    #[cfg(not(windows))]
    fn long_running_test_process() -> Command {
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 30"]);
        command
    }

    #[cfg(windows)]
    fn quick_exit_test_process(exit_code: i32) -> Command {
        let mut command = Command::new("cmd");
        command.args(["/C", &format!("exit /B {exit_code}")]);
        command
    }

    #[cfg(not(windows))]
    fn quick_exit_test_process(exit_code: i32) -> Command {
        let mut command = Command::new("sh");
        command.args(["-c", &format!("exit {exit_code}")]);
        command
    }

    fn spawn_test_process(mut command: Command) -> Child {
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_child_console_window(&mut command);
        command.spawn().unwrap()
    }

    #[test]
    fn audio_sidecar_executable_lookup_uses_allowlisted_names() {
        let dir = unique_test_dir("lookup");
        let executable = dir.join(audio_sidecar_executable_names()[0]);
        fs::write(&executable, b"test").unwrap();
        let rejected = dir.join("cmd.exe");
        fs::write(rejected, b"test").unwrap();

        let found = find_audio_sidecar_executable_in_dirs(
            std::slice::from_ref(&dir),
            audio_sidecar_executable_names(),
        );

        assert_eq!(found, Some(executable));
    }

    #[test]
    fn audio_sidecar_executable_dirs_include_installed_resource_dir() {
        let exe_parent = PathBuf::from(r"C:\Program Files\Scriber");
        let dirs = audio_sidecar_executable_dirs_for(Some(&exe_parent), None);

        assert!(dirs.contains(&exe_parent));
        assert!(dirs.contains(&exe_parent.join("resources").join("audio-sidecar")));
    }

    #[test]
    fn audio_sidecar_executable_dirs_prefer_root_binary_over_legacy_resource_dir() {
        let exe_parent = PathBuf::from(r"C:\Program Files\Scriber");
        let dirs = audio_sidecar_executable_dirs_for(Some(&exe_parent), None);

        let root_index = dirs
            .iter()
            .position(|dir| dir == &exe_parent)
            .expect("root directory missing");
        let resource_index = dirs
            .iter()
            .position(|dir| dir == &exe_parent.join("audio-sidecar"))
            .expect("audio-sidecar resource directory missing");

        assert!(root_index < resource_index);
    }

    #[test]
    fn audio_sidecar_unavailable_result_redacts_executable_path() {
        let result = unavailable_result(
            "audioSidecarSpawnFailed",
            "failed",
            json!({}),
            Some(hash_sensitive_identifier(
                r"C:\secret\scriber-audio-sidecar.exe",
            )),
            Some(123),
        );

        assert!(!format!("{result:?}").contains(r"C:\secret"));
        assert!(result.executable_available);
        assert_eq!(result.pid, Some(123));
    }

    #[test]
    fn audio_sidecar_capture_stop_without_stream_id_is_idempotent() {
        let result = call_audio_sidecar_command("captureStop", json!({}));

        assert!(result.success);
        assert_eq!(result.payload["stopped"], false);
        assert_eq!(result.payload["reason"], "missingStreamId");
    }

    #[test]
    fn audio_sidecar_prewarm_stop_without_prewarm_id_is_idempotent() {
        let result = call_audio_sidecar_command("prewarmStop", json!({}));

        assert!(result.success);
        assert_eq!(result.payload["stopped"], false);
        assert_eq!(result.payload["reason"], "missingPrewarmId");
    }

    #[test]
    fn audio_sidecar_prewarm_status_without_prewarm_id_is_idempotent() {
        let result = call_audio_sidecar_command("prewarmStatus", json!({}));

        assert!(result.success);
        assert_eq!(result.payload["active"], false);
        assert_eq!(result.payload["reason"], "missingPrewarmId");
    }

    #[test]
    fn audio_sidecar_shutdown_without_active_sessions_is_noop() {
        assert_eq!(shutdown_all_audio_sidecars("test"), 0);
    }

    #[cfg(windows)]
    #[test]
    fn audio_sidecar_process_name_reads_until_nul() {
        let mut raw = [0u16; 260];
        let text: Vec<u16> = "scriber-audio-sidecar.exe".encode_utf16().collect();
        raw[..text.len()].copy_from_slice(&text);
        raw[text.len()] = 0;
        raw[text.len() + 1] = 'x' as u16;

        assert_eq!(wide_process_name(&raw), "scriber-audio-sidecar.exe");
    }

    #[cfg(windows)]
    #[test]
    fn audio_sidecar_process_path_normalization_is_case_insensitive() {
        let upper = PathBuf::from(r"C:\Program Files\Scriber\scriber-audio-sidecar.exe");
        let lower = PathBuf::from(r"c:/program files/scriber/SCRIBER-AUDIO-SIDECAR.EXE");

        assert_eq!(
            normalized_process_path(&upper),
            normalized_process_path(&lower)
        );
    }

    #[test]
    fn sidecar_wait_reports_clean_process_exit() {
        let mut child = spawn_test_process(quick_exit_test_process(7));

        let exit = wait_for_sidecar_exit_or_kill(&mut child, Duration::from_secs(5));

        assert!(!exit.killed_after_timeout);
        assert_eq!(exit.status.and_then(|status| status.code()), Some(7));
        assert_eq!(exit.wait_error, None);
    }

    #[test]
    fn sidecar_wait_kills_process_after_timeout() {
        let mut child = spawn_test_process(long_running_test_process());

        let exit = wait_for_sidecar_exit_or_kill(&mut child, Duration::from_millis(20));

        assert!(exit.killed_after_timeout);
        assert_eq!(exit.wait_error, None);
        assert!(child.try_wait().unwrap().is_some());
    }

    #[test]
    fn sidecar_response_validation_rejects_request_id_mismatch() {
        let response = json!({
            "protocolVersion": AUDIO_SIDECAR_PROTOCOL_VERSION,
            "requestId": "wrong",
            "success": true,
            "payload": {}
        })
        .to_string();

        let result =
            parse_sidecar_response(&response, "expected", Some("hash".to_string()), Some(1));

        assert!(!result.success);
        assert_eq!(
            result.error_code.as_deref(),
            Some("audioSidecarRequestMismatch")
        );
    }

    #[test]
    fn sidecar_response_read_is_bounded_by_timeout() {
        let (_tx, rx) = mpsc::channel();

        let result = read_sidecar_response_line_with_timeout(&rx, Duration::from_millis(1))
            .expect_err("an unresponsive sidecar must time out");

        assert_eq!(
            result.error_code.as_deref(),
            Some("audioSidecarResponseTimeout")
        );
        assert_eq!(result.payload["responseTimeoutMs"], 1);
    }
}
