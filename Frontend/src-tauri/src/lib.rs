use serde::Serialize;
use serde_json::{json, Value};
use std::{
    env,
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::Manager;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{CreateMutexW, ReleaseMutex};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8765;
const BACKEND_START_TIMEOUT: Duration = Duration::from_secs(30);
const FORCE_MANAGED_BACKEND_ENV: &str = "SCRIBER_FORCE_MANAGED_BACKEND";
const SESSION_TOKEN_ENV: &str = "SCRIBER_SESSION_TOKEN";
const SINGLE_INSTANCE_MUTEX_NAME: &str = "Local\\ScriberDesktopSingleInstance";
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
    launch_kind: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendAccess {
    base_url: String,
    session_token: String,
}

struct BackendState {
    base_url: String,
    port: u16,
    child: Option<Child>,
    job: Option<BackendJob>,
    started_at: Option<Instant>,
    message: String,
    launch_kind: String,
    resource_dir: Option<PathBuf>,
    session_token: String,
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

#[cfg(windows)]
pub struct SingleInstanceGuard {
    handle: HANDLE,
}

#[cfg(windows)]
unsafe impl Send for SingleInstanceGuard {}

#[cfg(windows)]
unsafe impl Sync for SingleInstanceGuard {}

#[cfg(windows)]
impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        unsafe {
            if !self.handle.is_null() {
                let _ = ReleaseMutex(self.handle);
                let _ = CloseHandle(self.handle);
            }
        }
    }
}

#[cfg(not(windows))]
pub struct SingleInstanceGuard;

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
                launch_kind: "none".to_string(),
                resource_dir: None,
                session_token: resolve_session_token(),
            }),
        }
    }

    fn set_resource_dir(&self, resource_dir: Option<PathBuf>) {
        if let Ok(mut state) = self.state.lock() {
            state.resource_dir = resource_dir;
        }
    }

    fn base_url(&self) -> String {
        self.state
            .lock()
            .map(|state| state.base_url.clone())
            .unwrap_or_else(|_| base_url(DEFAULT_PORT))
    }

    fn access(&self) -> BackendAccess {
        self.state
            .lock()
            .map(|state| BackendAccess {
                base_url: state.base_url.clone(),
                session_token: state.session_token.clone(),
            })
            .unwrap_or_else(|_| BackendAccess {
                base_url: base_url(DEFAULT_PORT),
                session_token: String::new(),
            })
    }

    fn ensure_started(&self) -> BackendStatus {
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            let force_managed = force_managed_backend();
            if health_ready(state.port) && (!force_managed || state.child.is_some()) {
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
            launch_kind: "unknown".to_string(),
        }
    }

    fn status(&self) -> BackendStatus {
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            let force_managed = force_managed_backend();
            let ready = health_ready(state.port) && (!force_managed || state.child.is_some());
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
            launch_kind: "unknown".to_string(),
        }
    }

    fn restart(&self) -> Result<BackendStatus, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "Backend state lock is poisoned".to_string())?;
        refresh_child_state(&mut state);
        if state.child.is_none() && health_ready(state.port) && !force_managed_backend() {
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
fn get_backend_access(manager: tauri::State<'_, BackendManager>) -> BackendAccess {
    manager.access()
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
    let single_instance_guard = match acquire_single_instance_guard(SINGLE_INSTANCE_MUTEX_NAME) {
        Ok(guard) => guard,
        Err(err) => {
            write_shell_log(&err);
            return;
        }
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(single_instance_guard)
        .manage(BackendManager::new())
        .setup(|app| {
            let manager = app.state::<BackendManager>();
            manager.set_resource_dir(app.path().resource_dir().ok());
            let _ = manager.ensure_started();
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_base_url,
            get_backend_access,
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
        launch_kind: if ready && state.child.is_none() {
            "external".to_string()
        } else {
            state.launch_kind.clone()
        },
    }
}

fn refresh_child_state(state: &mut BackendState) {
    if let Some(child) = state.child.as_mut() {
        match child.try_wait() {
            Ok(Some(status)) => {
                let pid = child.id();
                let launch_kind = state.launch_kind.clone();
                write_backend_exit_metadata(pid, &launch_kind, &status.to_string());
                state.message = format!("Managed backend exited with {status}");
                state.child = None;
                state.job = None;
                state.started_at = None;
                state.launch_kind = "none".to_string();
            }
            Ok(None) => {}
            Err(err) => {
                let pid = child.id();
                let launch_kind = state.launch_kind.clone();
                write_backend_exit_metadata(pid, &launch_kind, &format!("inspect failed: {err}"));
                state.message = format!("Failed to inspect backend process: {err}");
                state.child = None;
                state.job = None;
                state.started_at = None;
                state.launch_kind = "none".to_string();
            }
        }
    }
}

fn terminate_managed_child(state: &mut BackendState) {
    if let Some(mut child) = state.child.take() {
        write_shell_log(&format!("terminating managed backend pid={}", child.id()));
        let _ = child.kill();
        let _ = child.wait();
    }
    state.job = None;
    state.started_at = None;
    state.launch_kind = "none".to_string();
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
    match spawn_backend(port, state.resource_dir.as_deref(), &state.session_token) {
        Ok((child, launch_kind)) => {
            let (job, job_warning) = attach_child_to_kill_job(&child);
            state.message = match job_warning {
                Some(warning) => format!("{message} ({launch_kind}); {warning}"),
                None => format!("{message} ({launch_kind})"),
            };
            state.job = job;
            state.started_at = Some(Instant::now());
            state.launch_kind = launch_kind;
            state.child = Some(child);
        }
        Err(err) => {
            state.message = format!("Failed to start backend: {err}");
            state.job = None;
            state.started_at = None;
            state.child = None;
            state.launch_kind = "none".to_string();
        }
    }
    status_from_state(state, health_ready(state.port))
}

fn managed_backend_start_timed_out(started_at: Option<Instant>, now: Instant) -> bool {
    started_at
        .map(|started_at| now.duration_since(started_at) >= BACKEND_START_TIMEOUT)
        .unwrap_or(false)
}

fn force_managed_backend() -> bool {
    env::var(FORCE_MANAGED_BACKEND_ENV)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes"
            )
        })
        .unwrap_or(false)
}

fn resolve_session_token() -> String {
    env::var(SESSION_TOKEN_ENV)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string())
}

#[cfg(windows)]
fn acquire_single_instance_guard(name: &str) -> Result<SingleInstanceGuard, String> {
    let wide_name = wide_null(name);
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 1, wide_name.as_ptr());
        if handle.is_null() {
            return Err(format!(
                "single-instance mutex creation failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            return Err("another Scriber desktop instance is already running".to_string());
        }

        Ok(SingleInstanceGuard { handle })
    }
}

#[cfg(not(windows))]
fn acquire_single_instance_guard(_name: &str) -> Result<SingleInstanceGuard, String> {
    Ok(SingleInstanceGuard)
}

#[cfg(windows)]
fn wide_null(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

struct BackendCommandSpec {
    program: PathBuf,
    args: Vec<String>,
    working_dir: PathBuf,
    launch_kind: String,
}

fn spawn_backend(
    port: u16,
    resource_dir: Option<&Path>,
    session_token: &str,
) -> Result<(Child, String), String> {
    let spec = resolve_backend_command(resource_dir)?;
    let data_dir = scriber_data_dir();
    fs::create_dir_all(&data_dir)
        .map_err(|err| format!("Could not create Scriber data directory: {err}"))?;
    write_shell_log_to_dir(
        &data_dir,
        &format!(
            "starting backend launch_kind={} program={} port={} data_dir={}",
            spec.launch_kind,
            spec.program.display(),
            port,
            data_dir.display()
        ),
    );
    let log_path = data_dir.join("logs").join("tauri-backend.log");
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

    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .current_dir(&spec.working_dir)
        .env("SCRIBER_WEB_HOST", DEFAULT_HOST)
        .env("SCRIBER_WEB_PORT", port.to_string())
        .env("SCRIBER_RUNTIME_MODE", "tauri-supervised")
        .env("SCRIBER_BACKEND_LAUNCH_KIND", &spec.launch_kind)
        .env(SESSION_TOKEN_ENV, session_token)
        .env("SCRIBER_LOG_STDERR", "1")
        .env("SCRIBER_DATA_DIR", &data_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    hide_child_console_window(&mut command);
    match command.spawn() {
        Ok(child) => {
            write_shell_log_to_dir(
                &data_dir,
                &format!(
                    "backend started pid={} launch_kind={} backend_log={}",
                    child.id(),
                    spec.launch_kind,
                    log_path.display()
                ),
            );
            Ok((child, spec.launch_kind))
        }
        Err(err) => {
            write_shell_log_to_dir(
                &data_dir,
                &format!(
                    "backend spawn failed program={} error={err}",
                    spec.program.display()
                ),
            );
            Err(format!("Could not spawn {:?}: {err}", spec.program))
        }
    }
}

fn resolve_backend_command(resource_dir: Option<&Path>) -> Result<BackendCommandSpec, String> {
    if let Some(program) = find_backend_executable(resource_dir)? {
        let working_dir = program
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
        return Ok(BackendCommandSpec {
            program,
            args: Vec::new(),
            working_dir,
            launch_kind: "sidecar".to_string(),
        });
    }

    let repo_root =
        find_repo_root().ok_or_else(|| "Could not locate Scriber repository root".to_string())?;
    Ok(BackendCommandSpec {
        program: find_python(&repo_root),
        args: vec!["-m".to_string(), "src.web_api".to_string()],
        working_dir: repo_root,
        launch_kind: "python-module".to_string(),
    })
}

fn find_backend_executable(resource_dir: Option<&Path>) -> Result<Option<PathBuf>, String> {
    if let Ok(raw) = env::var("SCRIBER_BACKEND_EXE") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            let path = absolute_path(trimmed);
            if path.is_file() {
                return Ok(Some(path));
            }
            return Err(format!(
                "SCRIBER_BACKEND_EXE does not exist: {}",
                path.display()
            ));
        }
    }

    Ok(find_backend_executable_in_dirs(
        &backend_executable_dirs(resource_dir),
        backend_executable_names(),
    ))
}

fn find_backend_executable_in_dirs(dirs: &[PathBuf], names: &[&str]) -> Option<PathBuf> {
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

fn backend_executable_dirs(resource_dir: Option<&Path>) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Ok(raw) = env::var("SCRIBER_BACKEND_DIR") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            push_unique_dir(&mut dirs, absolute_path(trimmed));
        }
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            push_unique_dir(&mut dirs, exe_dir.to_path_buf());
            push_unique_dir(&mut dirs, exe_dir.join("backend"));
            push_unique_dir(&mut dirs, exe_dir.join("binaries"));
        }
    }
    if let Some(resource_dir) = resource_dir {
        push_unique_dir(&mut dirs, resource_dir.to_path_buf());
        push_unique_dir(&mut dirs, resource_dir.join("backend"));
        push_unique_dir(&mut dirs, resource_dir.join("binaries"));
    }
    dirs
}

fn push_unique_dir(dirs: &mut Vec<PathBuf>, dir: PathBuf) {
    if !dirs.iter().any(|existing| existing == &dir) {
        dirs.push(dir);
    }
}

#[cfg(windows)]
fn backend_executable_names() -> &'static [&'static str] {
    &[
        "scriber-backend.exe",
        "scriber-backend-x86_64-pc-windows-msvc.exe",
    ]
}

#[cfg(not(windows))]
fn backend_executable_names() -> &'static [&'static str] {
    &[
        "scriber-backend",
        "scriber-backend-x86_64-unknown-linux-gnu",
        "scriber-backend-aarch64-apple-darwin",
        "scriber-backend-x86_64-apple-darwin",
    ]
}

fn scriber_data_dir() -> PathBuf {
    if let Ok(raw) = env::var("SCRIBER_DATA_DIR") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return absolute_path(trimmed);
        }
    }

    #[cfg(windows)]
    {
        if let Ok(local_app_data) = env::var("LOCALAPPDATA") {
            let trimmed = local_app_data.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed).join("Scriber");
            }
        }
        if let Ok(app_data) = env::var("APPDATA") {
            let trimmed = app_data.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed).join("Scriber");
            }
        }
    }

    #[cfg(not(windows))]
    {
        if let Ok(xdg_data_home) = env::var("XDG_DATA_HOME") {
            let trimmed = xdg_data_home.trim();
            if !trimmed.is_empty() {
                return PathBuf::from(trimmed).join("scriber");
            }
        }
        if let Ok(home) = env::var("HOME") {
            let trimmed = home.trim();
            if !trimmed.is_empty() {
                #[cfg(target_os = "macos")]
                {
                    return PathBuf::from(trimmed)
                        .join("Library")
                        .join("Application Support")
                        .join("Scriber");
                }
                #[cfg(not(target_os = "macos"))]
                {
                    return PathBuf::from(trimmed)
                        .join(".local")
                        .join("share")
                        .join("scriber");
                }
            }
        }
    }

    env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("scriber-data")
}

fn timestamp_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

fn write_shell_log(message: &str) {
    let data_dir = scriber_data_dir();
    write_shell_log_to_dir(&data_dir, message);
}

fn write_shell_log_to_dir(data_dir: &Path, message: &str) {
    let log_dir = data_dir.join("logs");
    if fs::create_dir_all(&log_dir).is_err() {
        return;
    }
    let path = log_dir.join("tauri-shell.log");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{} {}", timestamp_millis(), message);
    }
}

fn write_backend_exit_metadata(pid: u32, launch_kind: &str, status: &str) {
    let data_dir = scriber_data_dir();
    let log_dir = data_dir.join("logs");
    if fs::create_dir_all(&log_dir).is_err() {
        return;
    }
    let payload = json!({
        "timestampMs": timestamp_millis(),
        "event": "managed_backend_exit",
        "pid": pid,
        "launchKind": launch_kind,
        "status": status,
    });
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("backend-crash-metadata.jsonl"))
    {
        let _ = writeln!(file, "{payload}");
    }
    write_shell_log_to_dir(
        &data_dir,
        &format!("managed backend exited pid={pid} launch_kind={launch_kind} status={status}"),
    );
}

fn absolute_path(raw: &str) -> PathBuf {
    let path = PathBuf::from(raw);
    if path.is_absolute() {
        path
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
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
    use super::{
        acquire_single_instance_guard, backend_executable_names, find_backend_executable_in_dirs,
        health_response_ready, managed_backend_start_timed_out, resolve_session_token,
        BACKEND_START_TIMEOUT, SESSION_TOKEN_ENV,
    };
    use std::{
        fs,
        path::PathBuf,
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };

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

    #[test]
    fn resolve_session_token_prefers_environment_and_can_generate() {
        let previous = std::env::var(SESSION_TOKEN_ENV).ok();

        std::env::set_var(SESSION_TOKEN_ENV, "known-token");
        assert_eq!(resolve_session_token(), "known-token");

        std::env::remove_var(SESSION_TOKEN_ENV);
        assert!(resolve_session_token().len() >= 32);

        match previous {
            Some(value) => std::env::set_var(SESSION_TOKEN_ENV, value),
            None => std::env::remove_var(SESSION_TOKEN_ENV),
        }
    }

    #[cfg(windows)]
    #[test]
    fn single_instance_guard_blocks_second_acquisition() {
        let name = format!("Local\\ScriberDesktopTest-{}", unique_test_id());
        let first = acquire_single_instance_guard(&name).unwrap();

        let second = acquire_single_instance_guard(&name);
        assert!(matches!(second, Err(message) if message.contains("already running")));

        drop(first);
        let third = acquire_single_instance_guard(&name);
        assert!(third.is_ok());
    }

    #[test]
    fn find_backend_executable_in_dirs_finds_sidecar_name() {
        let dir = unique_test_dir("sidecar");
        fs::create_dir_all(&dir).unwrap();
        let sidecar = dir.join(backend_executable_names()[0]);
        fs::write(&sidecar, b"test").unwrap();

        let found = find_backend_executable_in_dirs(&[dir.clone()], backend_executable_names());

        assert_eq!(found, Some(sidecar));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn find_backend_executable_in_dirs_prefers_earlier_directory() {
        let first = unique_test_dir("sidecar-first");
        let second = unique_test_dir("sidecar-second");
        fs::create_dir_all(&first).unwrap();
        fs::create_dir_all(&second).unwrap();
        let first_sidecar = first.join(backend_executable_names()[0]);
        let second_sidecar = second.join(backend_executable_names()[0]);
        fs::write(&first_sidecar, b"first").unwrap();
        fs::write(&second_sidecar, b"second").unwrap();

        let found = find_backend_executable_in_dirs(
            &[first.clone(), second.clone()],
            backend_executable_names(),
        );

        assert_eq!(found, Some(first_sidecar));
        let _ = fs::remove_dir_all(first);
        let _ = fs::remove_dir_all(second);
    }

    fn unique_test_dir(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!("scriber-{label}-{}", unique_test_id()))
    }

    fn unique_test_id() -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("{}-{nanos}", std::process::id())
    }
}
