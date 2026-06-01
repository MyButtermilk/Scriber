use serde::Serialize;
use serde_json::Value;
use std::{
    env,
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::Manager;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8765;
const BACKEND_START_TIMEOUT: Duration = Duration::from_secs(30);
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatus {
    base_url: String,
    running: bool,
    ready: bool,
    managed: bool,
    pid: Option<u32>,
    message: String,
    runtime_mode: String,
}

struct BackendState {
    base_url: String,
    port: u16,
    child: Option<Child>,
    job: Option<BackendJob>,
    started_at: Option<Instant>,
    message: String,
}

#[cfg(windows)]
struct BackendJob {
    handle: HANDLE,
}

#[cfg(windows)]
unsafe impl Send for BackendJob {}

#[cfg(windows)]
impl Drop for BackendJob {
    fn drop(&mut self) {
        unsafe {
            if !self.handle.is_null() {
                let _ = CloseHandle(self.handle);
            }
        }
    }
}

#[cfg(not(windows))]
struct BackendJob;

pub struct BackendManager {
    state: Mutex<BackendState>,
}

impl BackendManager {
    fn new() -> Self {
        Self {
            state: Mutex::new(BackendState {
                base_url: base_url(DEFAULT_PORT),
                port: DEFAULT_PORT,
                child: None,
                job: None,
                started_at: None,
                message: "Backend not started".to_string(),
            }),
        }
    }

    fn base_url(&self) -> String {
        self.state
            .lock()
            .map(|state| state.base_url.clone())
            .unwrap_or_else(|_| base_url(DEFAULT_PORT))
    }

    fn ensure_started(&self) -> BackendStatus {
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            if health_ready(state.port) {
                state.message = if state.child.is_some() {
                    "Managed backend is ready".to_string()
                } else {
                    "Attached to existing backend".to_string()
                };
                return status_from_state(&state, true);
            }
            if state.child.is_some() {
                if managed_backend_start_timed_out(state.started_at, Instant::now()) {
                    terminate_managed_child(&mut state);
                    state.message = "Managed backend startup timed out; restarting".to_string();
                } else {
                    state.message = "Managed backend is starting".to_string();
                    return status_from_state(&state, false);
                }
            }

            let port = select_backend_port(state.port);
            return start_managed_backend(&mut state, port, "Managed backend process started");
        }
        BackendStatus {
            base_url: base_url(DEFAULT_PORT),
            running: false,
            ready: false,
            managed: false,
            pid: None,
            message: "Backend state lock is poisoned".to_string(),
            runtime_mode: "tauri-supervised".to_string(),
        }
    }

    fn status(&self) -> BackendStatus {
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            let ready = health_ready(state.port);
            if ready && state.child.is_none() {
                state.message = "Attached to existing backend".to_string();
            } else if !ready
                && state.child.is_some()
                && managed_backend_start_timed_out(state.started_at, Instant::now())
            {
                state.message = "Managed backend startup timed out".to_string();
            }
            return status_from_state(&state, ready);
        }
        BackendStatus {
            base_url: base_url(DEFAULT_PORT),
            running: false,
            ready: false,
            managed: false,
            pid: None,
            message: "Backend state lock is poisoned".to_string(),
            runtime_mode: "tauri-supervised".to_string(),
        }
    }

    fn restart(&self) -> Result<BackendStatus, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "Backend state lock is poisoned".to_string())?;
        refresh_child_state(&mut state);
        if state.child.is_none() && health_ready(state.port) {
            let message =
                "Cannot restart backend because the current backend is external".to_string();
            state.message = message.clone();
            return Err(message);
        }

        terminate_managed_child(&mut state);
        let port = select_backend_port(state.port);
        let status = start_managed_backend(&mut state, port, "Managed backend restarted");
        if status.managed {
            Ok(status)
        } else {
            Err(state.message.clone())
        }
    }
}

impl Drop for BackendManager {
    fn drop(&mut self) {
        if let Ok(mut state) = self.state.lock() {
            terminate_managed_child(&mut state);
        }
    }
}

#[tauri::command]
fn get_backend_base_url(manager: tauri::State<'_, BackendManager>) -> String {
    manager.base_url()
}

#[tauri::command]
fn backend_status(manager: tauri::State<'_, BackendManager>) -> BackendStatus {
    manager.status()
}

#[tauri::command]
fn ensure_backend_running(manager: tauri::State<'_, BackendManager>) -> BackendStatus {
    manager.ensure_started()
}

#[tauri::command]
fn restart_backend(manager: tauri::State<'_, BackendManager>) -> Result<BackendStatus, String> {
    manager.restart()
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(BackendManager::new())
        .setup(|app| {
            let manager = app.state::<BackendManager>();
            let _ = manager.ensure_started();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_base_url,
            backend_status,
            ensure_backend_running,
            restart_backend
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Scriber desktop shell");
}

fn status_from_state(state: &BackendState, ready: bool) -> BackendStatus {
    BackendStatus {
        base_url: state.base_url.clone(),
        running: ready || state.child.is_some(),
        ready,
        managed: state.child.is_some(),
        pid: state.child.as_ref().map(Child::id),
        message: state.message.clone(),
        runtime_mode: "tauri-supervised".to_string(),
    }
}

fn refresh_child_state(state: &mut BackendState) {
    if let Some(child) = state.child.as_mut() {
        match child.try_wait() {
            Ok(Some(status)) => {
                state.message = format!("Managed backend exited with {status}");
                state.child = None;
                state.job = None;
                state.started_at = None;
            }
            Ok(None) => {}
            Err(err) => {
                state.message = format!("Failed to inspect backend process: {err}");
                state.child = None;
                state.job = None;
                state.started_at = None;
            }
        }
    }
}

fn terminate_managed_child(state: &mut BackendState) {
    if let Some(mut child) = state.child.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    state.job = None;
    state.started_at = None;
}

fn select_backend_port(current_port: u16) -> u16 {
    if port_appears_free(current_port) {
        current_port
    } else if port_appears_free(DEFAULT_PORT) {
        DEFAULT_PORT
    } else {
        allocate_loopback_port().unwrap_or(DEFAULT_PORT)
    }
}

fn start_managed_backend(state: &mut BackendState, port: u16, message: &str) -> BackendStatus {
    state.port = port;
    state.base_url = base_url(port);
    match spawn_python_backend(port) {
        Ok(child) => {
            let (job, job_warning) = attach_child_to_kill_job(&child);
            state.message = match job_warning {
                Some(warning) => format!("{message}; {warning}"),
                None => message.to_string(),
            };
            state.job = job;
            state.started_at = Some(Instant::now());
            state.child = Some(child);
        }
        Err(err) => {
            state.message = format!("Failed to start backend: {err}");
            state.job = None;
            state.started_at = None;
            state.child = None;
        }
    }
    status_from_state(state, health_ready(state.port))
}

fn managed_backend_start_timed_out(started_at: Option<Instant>, now: Instant) -> bool {
    started_at
        .map(|started_at| now.duration_since(started_at) >= BACKEND_START_TIMEOUT)
        .unwrap_or(false)
}

fn spawn_python_backend(port: u16) -> Result<Child, String> {
    let repo_root =
        find_repo_root().ok_or_else(|| "Could not locate Scriber repository root".to_string())?;
    let python = find_python(&repo_root);
    let log_path = repo_root.join("tmp").join("tauri-backend.log");
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("Could not create log directory: {err}"))?;
    }
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|err| format!("Could not open backend log: {err}"))?;
    let stderr = stdout
        .try_clone()
        .map_err(|err| format!("Could not clone backend log handle: {err}"))?;

    let mut command = Command::new(&python);
    command
        .arg("-m")
        .arg("src.web_api")
        .current_dir(&repo_root)
        .env("SCRIBER_WEB_HOST", DEFAULT_HOST)
        .env("SCRIBER_WEB_PORT", port.to_string())
        .env("SCRIBER_RUNTIME_MODE", "tauri-supervised")
        .env("SCRIBER_LOG_STDERR", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    hide_child_console_window(&mut command);
    command
        .spawn()
        .map_err(|err| format!("Could not spawn {:?}: {err}", python))
}

#[cfg(windows)]
fn attach_child_to_kill_job(child: &Child) -> (Option<BackendJob>, Option<String>) {
    use std::os::windows::io::AsRawHandle;

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return (
                None,
                Some(format!(
                    "backend kill job unavailable: {}",
                    std::io::Error::last_os_error()
                )),
            );
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let set_ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if set_ok == 0 {
            let err = std::io::Error::last_os_error();
            let _ = CloseHandle(job);
            return (None, Some(format!("backend kill job setup failed: {err}")));
        }

        let process_handle = child.as_raw_handle() as HANDLE;
        if AssignProcessToJobObject(job, process_handle) == 0 {
            let err = std::io::Error::last_os_error();
            let _ = CloseHandle(job);
            return (
                None,
                Some(format!("backend kill job assignment failed: {err}")),
            );
        }

        (Some(BackendJob { handle: job }), None)
    }
}

#[cfg(not(windows))]
fn attach_child_to_kill_job(_child: &Child) -> (Option<BackendJob>, Option<String>) {
    (None, None)
}

#[cfg(windows)]
fn hide_child_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;

    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_child_console_window(_command: &mut Command) {}

fn find_python(repo_root: &Path) -> PathBuf {
    if let Ok(raw) = env::var("SCRIBER_PYTHON") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    let candidates = [
        repo_root.join("venv").join("Scripts").join("python.exe"),
        repo_root.join(".venv").join("Scripts").join("python.exe"),
        repo_root.join("venv").join("bin").join("python"),
        repo_root.join(".venv").join("bin").join("python"),
    ];
    candidates
        .into_iter()
        .find(|candidate| candidate.exists())
        .unwrap_or_else(|| PathBuf::from("python"))
}

fn find_repo_root() -> Option<PathBuf> {
    if let Ok(raw) = env::var("SCRIBER_REPO_ROOT") {
        let path = PathBuf::from(raw);
        if path.join("src").join("web_api.py").exists() {
            return Some(path);
        }
    }

    let current = env::current_dir().ok()?;
    let mut candidates = vec![current.clone()];
    candidates.extend(current.ancestors().map(Path::to_path_buf));
    for candidate in candidates {
        if candidate.join("src").join("web_api.py").exists() {
            return Some(candidate);
        }
        let parent_candidate = candidate
            .parent()
            .and_then(Path::parent)
            .map(Path::to_path_buf);
        if let Some(root) = parent_candidate {
            if root.join("src").join("web_api.py").exists() {
                return Some(root);
            }
        }
    }
    None
}

fn allocate_loopback_port() -> Option<u16> {
    TcpListener::bind((DEFAULT_HOST, 0))
        .ok()
        .and_then(|listener| listener.local_addr().ok().map(|addr| addr.port()))
}

fn port_appears_free(port: u16) -> bool {
    TcpListener::bind((DEFAULT_HOST, port)).is_ok()
}

fn base_url(port: u16) -> String {
    format!("http://{DEFAULT_HOST}:{port}")
}

fn health_ready(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(750)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(750)));
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: {DEFAULT_HOST}:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    health_response_ready(&response)
}

fn health_response_ready(response: &str) -> bool {
    if !response.starts_with("HTTP/1.1 200") && !response.starts_with("HTTP/1.0 200") {
        return false;
    }
    let Some((_, body)) = response.split_once("\r\n\r\n") else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<Value>(body) else {
        return false;
    };
    value.get("ok").and_then(Value::as_bool).unwrap_or(false)
        && value.get("apiVersion").is_some()
        && value.get("runtimeMode").is_some()
}

#[cfg(test)]
mod tests {
    use super::{health_response_ready, managed_backend_start_timed_out, BACKEND_START_TIMEOUT};
    use std::time::{Duration, Instant};

    #[test]
    fn health_response_ready_requires_scriber_contract() {
        let response = concat!(
            "HTTP/1.1 200 OK\r\n",
            "Content-Type: application/json\r\n",
            "\r\n",
            r#"{"ok":true,"apiVersion":"1","runtimeMode":"python-web"}"#
        );

        assert!(health_response_ready(response));
    }

    #[test]
    fn health_response_ready_rejects_generic_ok_payload() {
        let response = concat!(
            "HTTP/1.1 200 OK\r\n",
            "Content-Type: application/json\r\n",
            "\r\n",
            r#"{"ok":true}"#
        );

        assert!(!health_response_ready(response));
    }

    #[test]
    fn health_response_ready_rejects_non_success_status() {
        let response = concat!(
            "HTTP/1.1 503 Service Unavailable\r\n",
            "Content-Type: application/json\r\n",
            "\r\n",
            r#"{"ok":true,"apiVersion":"1","runtimeMode":"python-web"}"#
        );

        assert!(!health_response_ready(response));
    }

    #[test]
    fn managed_backend_start_timeout_waits_for_grace_period() {
        let now = Instant::now();

        assert!(!managed_backend_start_timed_out(None, now));
        assert!(!managed_backend_start_timed_out(
            Some(now - (BACKEND_START_TIMEOUT - Duration::from_secs(1))),
            now
        ));
        assert!(managed_backend_start_timed_out(
            Some(now - BACKEND_START_TIMEOUT),
            now
        ));
    }
}
