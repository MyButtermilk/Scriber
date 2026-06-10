use serde_json::{json, Value};
use std::{
    env,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
use uuid::Uuid;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const AUDIO_SIDECAR_EXE_ENV: &str = "SCRIBER_AUDIO_SIDECAR_EXE";
const AUDIO_SIDECAR_PROTOCOL_VERSION: &str = "1";
const AUDIO_SIDECAR_NAME: &str = "scriber-audio-sidecar";
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

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
    call_audio_sidecar_command_at(&program, command, payload)
}

fn call_audio_sidecar_command_at(
    program: &Path,
    command: &str,
    payload: Value,
) -> AudioSidecarCallResult {
    let request_id = Uuid::new_v4().simple().to_string();
    let request = json!({
        "protocolVersion": AUDIO_SIDECAR_PROTOCOL_VERSION,
        "requestId": request_id,
        "command": command,
        "payload": payload,
    });
    let shutdown = json!({
        "protocolVersion": AUDIO_SIDECAR_PROTOCOL_VERSION,
        "requestId": Uuid::new_v4().simple().to_string(),
        "command": "shutdown",
        "payload": {},
    });
    let path_hash = Some(hash_sensitive_identifier(&program.display().to_string()));

    let mut process = Command::new(program);
    process
        .arg("--stdio")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_child_console_window(&mut process);

    let mut child = match process.spawn() {
        Ok(child) => child,
        Err(err) => {
            return unavailable_result(
                "audioSidecarSpawnFailed",
                format!("Rust audio sidecar spawn failed: {err}"),
                json!({
                    "sidecar": AUDIO_SIDECAR_NAME,
                    "sidecarExecutableAvailable": true,
                }),
                path_hash,
                None,
            )
        }
    };
    let pid = Some(child.id());

    let write_result = (|| -> Result<(), String> {
        let stdin = child
            .stdin
            .as_mut()
            .ok_or_else(|| "audio sidecar stdin unavailable".to_string())?;
        writeln!(stdin, "{request}")
            .map_err(|err| format!("sidecar request write failed: {err}"))?;
        writeln!(stdin, "{shutdown}")
            .map_err(|err| format!("sidecar shutdown write failed: {err}"))?;
        stdin
            .flush()
            .map_err(|err| format!("sidecar stdin flush failed: {err}"))?;
        Ok(())
    })();
    drop(child.stdin.take());

    if let Err(err) = write_result {
        let _ = child.kill();
        let _ = child.wait();
        return unavailable_result(
            "audioSidecarWriteFailed",
            err,
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
            }),
            path_hash,
            pid,
        );
    }

    let mut stdout = String::new();
    if let Some(mut pipe) = child.stdout.take() {
        if let Err(err) = pipe.read_to_string(&mut stdout) {
            let _ = child.kill();
            let _ = child.wait();
            return unavailable_result(
                "audioSidecarReadFailed",
                format!("Rust audio sidecar read failed: {err}"),
                json!({
                    "sidecar": AUDIO_SIDECAR_NAME,
                    "sidecarExecutableAvailable": true,
                }),
                path_hash,
                pid,
            );
        }
    }
    let status = child.wait().ok();
    let Some(first_line) = stdout.lines().find(|line| !line.trim().is_empty()) else {
        return unavailable_result(
            "audioSidecarEmptyResponse",
            "Rust audio sidecar returned no response",
            json!({
                "sidecar": AUDIO_SIDECAR_NAME,
                "sidecarExecutableAvailable": true,
                "exitStatus": status.as_ref().and_then(|value| value.code()),
            }),
            path_hash,
            pid,
        );
    };
    parse_sidecar_response(first_line, &request_id, path_hash, pid)
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
    let mut dirs = Vec::new();
    if let Ok(exe) = env::current_exe() {
        if let Some(parent) = exe.parent() {
            push_unique_dir(&mut dirs, parent.to_path_buf());
            push_unique_dir(&mut dirs, parent.join("audio-sidecar"));
        }
    }
    if let Ok(current_dir) = env::current_dir() {
        push_unique_dir(&mut dirs, current_dir);
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
    fn audio_sidecar_executable_lookup_uses_allowlisted_names() {
        let dir = unique_test_dir("lookup");
        let executable = dir.join(audio_sidecar_executable_names()[0]);
        fs::write(&executable, b"test").unwrap();
        let rejected = dir.join("cmd.exe");
        fs::write(rejected, b"test").unwrap();

        let found =
            find_audio_sidecar_executable_in_dirs(&[dir.clone()], audio_sidecar_executable_names());

        assert_eq!(found, Some(executable));
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
        assert_eq!(result.executable_available, true);
        assert_eq!(result.pid, Some(123));
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
}
