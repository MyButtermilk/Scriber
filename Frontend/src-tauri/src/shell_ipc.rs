use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::Instant,
};
use uuid::Uuid;

const API_VERSION: &str = "1";
const MAX_REQUEST_BYTES: usize = 64 * 1024;
const PIPE_BUFFER_BYTES: u32 = 64 * 1024;

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
                "commands": ["ping", "capabilities"],
                "textInjection": false,
            }),
        ),
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

#[cfg(windows)]
fn run_shell_ipc_server<L>(config: ShellIpcConfig, stop: Arc<AtomicBool>, log: &mut L)
where
    L: FnMut(String),
{
    log(format!(
        "shell IPC server starting pipe={}",
        config.pipe_name
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
    let pipe: HANDLE = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            PIPE_BUFFER_BYTES,
            PIPE_BUFFER_BYTES,
            250,
            ptr::null(),
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
    use windows_sys::Win32::Storage::FileSystem::ReadFile;

    let mut request = Vec::<u8>::new();
    loop {
        let mut buffer = [0u8; 4096];
        let mut bytes_read = 0u32;
        let ok = unsafe {
            ReadFile(
                pipe,
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                &mut bytes_read,
                ptr::null_mut(),
            )
        };
        if ok == 0 || bytes_read == 0 {
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
    use super::{handle_shell_ipc_request, response_line, ShellIpcConfig, API_VERSION};
    use serde_json::json;
    use std::time::Instant;

    #[test]
    fn shell_ipc_config_uses_private_pipe_and_token() {
        let config = ShellIpcConfig::new();

        assert!(config.pipe_name.starts_with(r"\\.\pipe\scriber-shell-"));
        assert!(config.token.len() >= 32);
        assert!(!config.pipe_name.contains(&config.token));
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
        assert_eq!(value["payload"]["textInjection"], false);
        assert_eq!(value["payload"]["commands"][0], "ping");
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
