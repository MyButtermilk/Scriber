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
use tauri::{
    menu::{MenuBuilder, SubmenuBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
#[cfg(windows)]
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, ERROR_SUCCESS, HANDLE,
};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegCreateKeyExW, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW,
    HKEY, HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_READ, KEY_SET_VALUE, REG_OPTION_NON_VOLATILE,
    REG_SZ,
};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{CreateMutexW, ReleaseMutex};

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8765;
const BACKEND_START_TIMEOUT: Duration = Duration::from_secs(30);
const FORCE_MANAGED_BACKEND_ENV: &str = "SCRIBER_FORCE_MANAGED_BACKEND";
const SESSION_TOKEN_ENV: &str = "SCRIBER_SESSION_TOKEN";
const DISABLE_HOTKEYS_ENV: &str = "SCRIBER_DISABLE_HOTKEYS";
const TAURI_GLOBAL_HOTKEY_ENV: &str = "SCRIBER_TAURI_GLOBAL_HOTKEY";
const SINGLE_INSTANCE_MUTEX_NAME: &str = "Local\\ScriberDesktopSingleInstance";
const AUTOSTART_REGISTRY_SUBKEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const AUTOSTART_REGISTRY_VALUE: &str = "Scriber";
const HOTKEY_DISPATCH_DEBOUNCE: Duration = Duration::from_millis(250);
const MAIN_WINDOW_LABEL: &str = "main";
const TRAY_ID: &str = "scriber-tray";
const MENU_ITEM_SHOW_WINDOW: &str = "scriber-show-window";
const MENU_ITEM_RESTART_BACKEND: &str = "scriber-restart-backend";
const MENU_ITEM_QUIT: &str = "scriber-quit";
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

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopAutostartStatus {
    enabled: bool,
    available: bool,
    message: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopHotkeyStatus {
    registered: bool,
    available: bool,
    hotkey: String,
    mode: String,
    message: String,
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

struct DesktopHotkeyState {
    inner: Mutex<DesktopHotkeyStateInner>,
}

struct DesktopHotkeyStateInner {
    registered_hotkey: Option<String>,
    mode: String,
    available: bool,
    message: String,
    last_dispatched_at: Option<Instant>,
}

impl DesktopHotkeyState {
    fn new() -> Self {
        Self {
            inner: Mutex::new(DesktopHotkeyStateInner {
                registered_hotkey: None,
                mode: "toggle".to_string(),
                available: false,
                message: "Global hotkey not initialized".to_string(),
                last_dispatched_at: None,
            }),
        }
    }

    fn status(&self) -> DesktopHotkeyStatus {
        self.inner
            .lock()
            .map(|state| DesktopHotkeyStatus {
                registered: state.registered_hotkey.is_some(),
                available: state.available,
                hotkey: state.registered_hotkey.clone().unwrap_or_default(),
                mode: state.mode.clone(),
                message: state.message.clone(),
            })
            .unwrap_or_else(|_| DesktopHotkeyStatus {
                registered: false,
                available: false,
                hotkey: String::new(),
                mode: "toggle".to_string(),
                message: "Global hotkey state lock is poisoned".to_string(),
            })
    }

    fn set_registered(&self, hotkey: String, mode: String, message: String) {
        if let Ok(mut state) = self.inner.lock() {
            state.registered_hotkey = Some(hotkey);
            state.mode = mode;
            state.available = true;
            state.message = message;
        }
    }

    fn set_unregistered(&self, mode: String, available: bool, message: String) {
        if let Ok(mut state) = self.inner.lock() {
            state.registered_hotkey = None;
            state.mode = mode;
            state.available = available;
            state.message = message;
        }
    }

    fn action_for_event(&self, event_state: ShortcutState, now: Instant) -> Option<&'static str> {
        let mut state = self.inner.lock().ok()?;
        match event_state {
            ShortcutState::Pressed => {
                if state
                    .last_dispatched_at
                    .map(|last| now.duration_since(last) < HOTKEY_DISPATCH_DEBOUNCE)
                    .unwrap_or(false)
                {
                    return None;
                }
                state.last_dispatched_at = Some(now);
                if state.mode == "push_to_talk" {
                    Some("/api/live-mic/start")
                } else {
                    Some("/api/live-mic/toggle")
                }
            }
            ShortcutState::Released => {
                if state.mode == "push_to_talk" {
                    Some("/api/live-mic/stop")
                } else {
                    None
                }
            }
        }
    }
}

#[cfg(windows)]
struct RegistryKey {
    handle: HKEY,
}

#[cfg(windows)]
impl Drop for RegistryKey {
    fn drop(&mut self) {
        unsafe {
            if !self.handle.is_null() {
                let _ = RegCloseKey(self.handle);
            }
        }
    }
}

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

#[tauri::command]
fn get_desktop_autostart() -> DesktopAutostartStatus {
    desktop_autostart_status()
}

#[tauri::command]
fn set_desktop_autostart(enabled: bool) -> Result<DesktopAutostartStatus, String> {
    set_desktop_autostart_enabled(enabled)?;
    Ok(desktop_autostart_status())
}

#[tauri::command]
fn global_hotkey_status(hotkey_state: tauri::State<'_, DesktopHotkeyState>) -> DesktopHotkeyStatus {
    hotkey_state.status()
}

#[tauri::command]
fn refresh_global_hotkey(app: AppHandle) -> Result<DesktopHotkeyStatus, String> {
    refresh_global_hotkey_for_app(&app)
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
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    handle_global_shortcut_event(app, event.state);
                })
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .on_menu_event(|app, event| {
            handle_shell_menu_event(app, event.id().as_ref());
        })
        .manage(single_instance_guard)
        .manage(DesktopHotkeyState::new())
        .manage(BackendManager::new())
        .setup(|app| {
            configure_desktop_shell(app)?;
            let manager = app.state::<BackendManager>();
            manager.set_resource_dir(app.path().resource_dir().ok());
            let _ = manager.ensure_started();
            if let Err(err) = refresh_global_hotkey_for_app(app.handle()) {
                write_shell_log(&format!("global hotkey registration skipped: {err}"));
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_base_url,
            get_backend_access,
            backend_status,
            ensure_backend_running,
            restart_backend,
            get_desktop_autostart,
            set_desktop_autostart,
            global_hotkey_status,
            refresh_global_hotkey
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Scriber desktop shell");
}

fn configure_desktop_shell<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    install_application_menu(app)?;
    install_tray(app)?;
    Ok(())
}

fn install_application_menu<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    let handle = app.handle();
    let app_submenu = SubmenuBuilder::new(handle, "Scriber")
        .text(MENU_ITEM_SHOW_WINDOW, "Open Scriber")
        .text(MENU_ITEM_RESTART_BACKEND, "Restart Backend")
        .separator()
        .text(MENU_ITEM_QUIT, "Quit")
        .build()?;
    let menu = MenuBuilder::new(handle).item(&app_submenu).build()?;
    app.set_menu(menu)?;
    Ok(())
}

fn install_tray<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    let handle = app.handle();
    let tray_menu = MenuBuilder::new(handle)
        .text(MENU_ITEM_SHOW_WINDOW, "Open Scriber")
        .text(MENU_ITEM_RESTART_BACKEND, "Restart Backend")
        .separator()
        .text(MENU_ITEM_QUIT, "Quit")
        .build()?;
    let mut tray = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("Scriber")
        .menu(&tray_menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if should_show_window_for_tray_event(&event) {
                show_main_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }

    tray.build(app)?;
    Ok(())
}

fn handle_shell_menu_event<R: Runtime>(app: &AppHandle<R>, item_id: &str) {
    if !is_shell_menu_item(item_id) {
        return;
    }

    match item_id {
        MENU_ITEM_SHOW_WINDOW => show_main_window(app),
        MENU_ITEM_RESTART_BACKEND => restart_backend_from_shell(app),
        MENU_ITEM_QUIT => app.exit(0),
        _ => {}
    }
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) else {
        write_shell_log("main window focus requested, but the main window was not found");
        return;
    };

    if let Err(err) = window.show() {
        write_shell_log(&format!("main window show failed: {err}"));
    }
    if let Err(err) = window.unminimize() {
        write_shell_log(&format!("main window unminimize failed: {err}"));
    }
    if let Err(err) = window.set_focus() {
        write_shell_log(&format!("main window focus failed: {err}"));
    }
}

fn restart_backend_from_shell<R: Runtime>(app: &AppHandle<R>) {
    let manager = app.state::<BackendManager>();
    match manager.restart() {
        Ok(status) => write_shell_log(&format!(
            "backend restart requested from shell menu; pid={:?} ready={} launch_kind={}",
            status.pid, status.ready, status.launch_kind
        )),
        Err(err) => write_shell_log(&format!("backend restart from shell menu failed: {err}")),
    }
}

fn is_shell_menu_item(item_id: &str) -> bool {
    matches!(
        item_id,
        MENU_ITEM_SHOW_WINDOW | MENU_ITEM_RESTART_BACKEND | MENU_ITEM_QUIT
    )
}

fn should_show_window_for_tray_event(event: &TrayIconEvent) -> bool {
    match event {
        TrayIconEvent::Click {
            button,
            button_state,
            ..
        } => should_show_window_for_tray_click(*button, Some(*button_state)),
        TrayIconEvent::DoubleClick { button, .. } => {
            should_show_window_for_tray_click(*button, None)
        }
        _ => false,
    }
}

fn should_show_window_for_tray_click(
    button: MouseButton,
    button_state: Option<MouseButtonState>,
) -> bool {
    button == MouseButton::Left
        && button_state
            .map(|state| state == MouseButtonState::Up)
            .unwrap_or(true)
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

#[cfg(windows)]
fn desktop_autostart_status() -> DesktopAutostartStatus {
    let expected_command = match env::current_exe() {
        Ok(exe) => autostart_command_for_exe(&exe),
        Err(err) => {
            return DesktopAutostartStatus {
                enabled: false,
                available: true,
                message: format!("Could not resolve current desktop executable: {err}"),
            };
        }
    };

    match read_autostart_value(AUTOSTART_REGISTRY_SUBKEY, AUTOSTART_REGISTRY_VALUE) {
        Ok(Some(command)) => {
            let enabled = autostart_commands_match(&command, &expected_command);
            DesktopAutostartStatus {
                enabled,
                available: true,
                message: if enabled {
                    "Desktop autostart is enabled".to_string()
                } else {
                    "Desktop autostart points to a different Scriber command".to_string()
                },
            }
        }
        Ok(None) => DesktopAutostartStatus {
            enabled: false,
            available: true,
            message: "Desktop autostart is disabled".to_string(),
        },
        Err(err) => DesktopAutostartStatus {
            enabled: false,
            available: true,
            message: err,
        },
    }
}

#[cfg(not(windows))]
fn desktop_autostart_status() -> DesktopAutostartStatus {
    DesktopAutostartStatus {
        enabled: false,
        available: false,
        message: "Desktop autostart is only available on Windows".to_string(),
    }
}

#[cfg(windows)]
fn set_desktop_autostart_enabled(enabled: bool) -> Result<(), String> {
    let key = create_registry_key(AUTOSTART_REGISTRY_SUBKEY, KEY_SET_VALUE)?;
    let value_name = wide_null(AUTOSTART_REGISTRY_VALUE);
    unsafe {
        if enabled {
            let command =
                autostart_command_for_exe(&env::current_exe().map_err(|err| {
                    format!("Could not resolve current desktop executable: {err}")
                })?);
            let data = wide_null(&command);
            let bytes = data.len() * std::mem::size_of::<u16>();
            let status = RegSetValueExW(
                key.handle,
                value_name.as_ptr(),
                0,
                REG_SZ,
                data.as_ptr() as *const u8,
                bytes as u32,
            );
            if status != ERROR_SUCCESS {
                return Err(format_registry_error("set desktop autostart", status));
            }
        } else {
            let status = RegDeleteValueW(key.handle, value_name.as_ptr());
            if status != ERROR_SUCCESS && status != ERROR_FILE_NOT_FOUND {
                return Err(format_registry_error("disable desktop autostart", status));
            }
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn set_desktop_autostart_enabled(_enabled: bool) -> Result<(), String> {
    Err("Desktop autostart is only available on Windows".to_string())
}

#[cfg(windows)]
fn read_autostart_value(subkey: &str, value_name: &str) -> Result<Option<String>, String> {
    let key = match open_registry_key(subkey, KEY_READ | KEY_QUERY_VALUE) {
        Ok(key) => key,
        Err(err) => return Err(err),
    };
    let value_name = wide_null(value_name);
    unsafe {
        let mut value_type = 0;
        let mut bytes = 0;
        let status = RegQueryValueExW(
            key.handle,
            value_name.as_ptr(),
            std::ptr::null(),
            &mut value_type,
            std::ptr::null_mut(),
            &mut bytes,
        );
        if status == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        if status != ERROR_SUCCESS {
            return Err(format_registry_error("read desktop autostart", status));
        }
        if value_type != REG_SZ {
            return Ok(Some(String::new()));
        }

        let mut data = vec![0u16; bytes as usize / std::mem::size_of::<u16>()];
        let status = RegQueryValueExW(
            key.handle,
            value_name.as_ptr(),
            std::ptr::null(),
            &mut value_type,
            data.as_mut_ptr() as *mut u8,
            &mut bytes,
        );
        if status != ERROR_SUCCESS {
            return Err(format_registry_error("read desktop autostart", status));
        }
        while data.last() == Some(&0) {
            data.pop();
        }
        Ok(Some(String::from_utf16_lossy(&data)))
    }
}

#[cfg(windows)]
fn open_registry_key(subkey: &str, access: u32) -> Result<RegistryKey, String> {
    let subkey = wide_null(subkey);
    let mut handle: HKEY = std::ptr::null_mut();
    unsafe {
        let status = RegOpenKeyExW(HKEY_CURRENT_USER, subkey.as_ptr(), 0, access, &mut handle);
        if status != ERROR_SUCCESS {
            return Err(format_registry_error("open desktop autostart key", status));
        }
    }
    Ok(RegistryKey { handle })
}

#[cfg(windows)]
fn create_registry_key(subkey: &str, access: u32) -> Result<RegistryKey, String> {
    let subkey = wide_null(subkey);
    let mut handle: HKEY = std::ptr::null_mut();
    unsafe {
        let status = RegCreateKeyExW(
            HKEY_CURRENT_USER,
            subkey.as_ptr(),
            0,
            std::ptr::null(),
            REG_OPTION_NON_VOLATILE,
            access,
            std::ptr::null(),
            &mut handle,
            std::ptr::null_mut(),
        );
        if status != ERROR_SUCCESS {
            return Err(format_registry_error(
                "create desktop autostart key",
                status,
            ));
        }
    }
    Ok(RegistryKey { handle })
}

fn autostart_command_for_exe(exe: &Path) -> String {
    format!("\"{}\"", exe.display())
}

fn autostart_commands_match(configured: &str, expected: &str) -> bool {
    normalize_autostart_command(configured) == normalize_autostart_command(expected)
}

fn normalize_autostart_command(command: &str) -> String {
    command.trim().trim_matches('"').trim().to_ascii_lowercase()
}

#[cfg(windows)]
fn format_registry_error(operation: &str, status: u32) -> String {
    format!(
        "Could not {operation}: {}",
        std::io::Error::from_raw_os_error(status as i32)
    )
}

fn refresh_global_hotkey_for_app(app: &AppHandle) -> Result<DesktopHotkeyStatus, String> {
    let hotkey_state = app.state::<DesktopHotkeyState>();
    if !tauri_global_hotkey_enabled() {
        let _ = app.global_shortcut().unregister_all();
        hotkey_state.set_unregistered(
            "toggle".to_string(),
            false,
            format!("Global hotkey disabled via {TAURI_GLOBAL_HOTKEY_ENV}"),
        );
        return Ok(hotkey_state.status());
    }

    let manager = app.state::<BackendManager>();
    let status = manager.ensure_started();
    if !status.ready {
        let message = format!(
            "Backend is not ready for global hotkey registration: {}",
            status.message
        );
        hotkey_state.set_unregistered("toggle".to_string(), true, message.clone());
        return Err(message);
    }

    let access = manager.access();
    let config = fetch_backend_hotkey_config(&access)?;
    if config.hotkey.is_empty() {
        let _ = app.global_shortcut().unregister_all();
        hotkey_state.set_unregistered(config.mode, true, "No global hotkey configured".to_string());
        return Ok(hotkey_state.status());
    }

    app.global_shortcut()
        .unregister_all()
        .map_err(|err| format!("Could not clear previous global shortcuts: {err}"))?;
    app.global_shortcut()
        .register(config.hotkey.as_str())
        .map_err(|err| {
            format!(
                "Could not register global hotkey '{}': {err}",
                config.hotkey
            )
        })?;

    let message = format!(
        "Global hotkey registered: {} ({})",
        config.hotkey, config.mode
    );
    write_shell_log(&message);
    hotkey_state.set_registered(config.hotkey, config.mode, message);
    Ok(hotkey_state.status())
}

fn handle_global_shortcut_event(app: &AppHandle, event_state: ShortcutState) {
    let Some(path) = app
        .try_state::<DesktopHotkeyState>()
        .and_then(|state| state.action_for_event(event_state, Instant::now()))
    else {
        return;
    };

    let app_handle = app.clone();
    std::thread::spawn(move || {
        let Some(manager) = app_handle.try_state::<BackendManager>() else {
            write_shell_log("global hotkey ignored because backend manager is unavailable");
            return;
        };
        let status = manager.ensure_started();
        if !status.ready {
            write_shell_log(&format!(
                "global hotkey ignored because backend is not ready: {}",
                status.message
            ));
            return;
        }
        let access = manager.access();
        if let Err(err) = post_backend_path(&access, path) {
            write_shell_log(&format!("global hotkey action failed path={path}: {err}"));
        }
    });
}

fn tauri_global_hotkey_enabled() -> bool {
    env::var(TAURI_GLOBAL_HOTKEY_ENV)
        .map(|value| {
            !matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "0" | "false" | "no" | "off"
            )
        })
        .unwrap_or(true)
}

struct BackendHotkeyConfig {
    hotkey: String,
    mode: String,
}

fn fetch_backend_hotkey_config(access: &BackendAccess) -> Result<BackendHotkeyConfig, String> {
    let value = request_backend_json(access, "GET", "/api/settings")?;
    let raw_hotkey = value
        .get("hotkeyRaw")
        .or_else(|| value.get("hotkey"))
        .and_then(Value::as_str)
        .unwrap_or("ctrl+alt+s");
    let raw_mode = value
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("toggle");

    Ok(BackendHotkeyConfig {
        hotkey: normalize_global_shortcut(raw_hotkey),
        mode: normalize_hotkey_mode(raw_mode),
    })
}

fn normalize_hotkey_mode(mode: &str) -> String {
    if mode.trim().eq_ignore_ascii_case("push_to_talk") {
        "push_to_talk".to_string()
    } else {
        "toggle".to_string()
    }
}

fn normalize_global_shortcut(hotkey: &str) -> String {
    hotkey
        .split('+')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(|part| part.to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join("+")
}

fn post_backend_path(access: &BackendAccess, path: &str) -> Result<Value, String> {
    request_backend_json(access, "POST", path)
}

fn request_backend_json(access: &BackendAccess, method: &str, path: &str) -> Result<Value, String> {
    let (host, port) = parse_loopback_backend_url(&access.base_url)?;
    let addr = SocketAddr::from((host, port));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|err| format!("could not connect to backend: {err}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));

    let token_header = if access.session_token.is_empty() {
        String::new()
    } else {
        format!("X-Scriber-Token: {}\r\n", access.session_token)
    };
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {DEFAULT_HOST}:{port}\r\n{token_header}Content-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|err| format!("could not write backend request: {err}"))?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|err| format!("could not read backend response: {err}"))?;
    let (status, body) = split_http_response(&response)?;
    if !status.starts_with("HTTP/1.1 2") && !status.starts_with("HTTP/1.0 2") {
        return Err(format!("backend returned {status}"));
    }
    serde_json::from_str::<Value>(body)
        .map_err(|err| format!("backend returned invalid JSON: {err}"))
}

fn split_http_response(response: &str) -> Result<(&str, &str), String> {
    let Some((head, body)) = response.split_once("\r\n\r\n") else {
        return Err("backend returned malformed HTTP response".to_string());
    };
    let status = head.lines().next().unwrap_or_default();
    Ok((status, body))
}

fn parse_loopback_backend_url(base_url: &str) -> Result<([u8; 4], u16), String> {
    let trimmed = base_url.trim().trim_end_matches('/');
    let rest = trimmed
        .strip_prefix("http://")
        .ok_or_else(|| format!("unsupported backend URL: {base_url}"))?;
    let Some((host, port_raw)) = rest.rsplit_once(':') else {
        return Err(format!("backend URL has no port: {base_url}"));
    };
    if host != DEFAULT_HOST && host != "localhost" {
        return Err(format!("backend URL is not loopback: {base_url}"));
    }
    let port = port_raw
        .parse::<u16>()
        .map_err(|err| format!("backend URL has invalid port: {err}"))?;
    Ok(([127, 0, 0, 1], port))
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
        .env(DISABLE_HOTKEYS_ENV, "1")
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
        acquire_single_instance_guard, autostart_command_for_exe, autostart_commands_match,
        backend_executable_names, find_backend_executable_in_dirs, health_response_ready,
        is_shell_menu_item, managed_backend_start_timed_out, normalize_global_shortcut,
        normalize_hotkey_mode, parse_loopback_backend_url, resolve_session_token,
        should_show_window_for_tray_click, split_http_response, BACKEND_START_TIMEOUT,
        MENU_ITEM_QUIT, MENU_ITEM_RESTART_BACKEND, MENU_ITEM_SHOW_WINDOW, SESSION_TOKEN_ENV,
    };
    use std::{
        fs,
        path::PathBuf,
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };
    use tauri::tray::{MouseButton, MouseButtonState};

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

    #[test]
    fn autostart_command_quotes_executable_path() {
        let exe = PathBuf::from(r"C:\Program Files\Scriber\scriber-desktop.exe");

        assert_eq!(
            autostart_command_for_exe(&exe),
            r#""C:\Program Files\Scriber\scriber-desktop.exe""#
        );
    }

    #[test]
    fn autostart_command_match_accepts_quoted_and_unquoted_path() {
        assert!(autostart_commands_match(
            r#"C:\Program Files\Scriber\scriber-desktop.exe"#,
            r#""C:\Program Files\Scriber\scriber-desktop.exe""#
        ));
    }

    #[test]
    fn autostart_command_match_rejects_legacy_tray_command() {
        assert!(!autostart_commands_match(
            r#""C:\Python313\python.exe" "C:\Scriber\src\tray.py""#,
            r#""C:\Program Files\Scriber\scriber-desktop.exe""#
        ));
    }

    #[test]
    fn normalize_global_shortcut_matches_tauri_syntax() {
        assert_eq!(normalize_global_shortcut("Ctrl + Alt + S"), "ctrl+alt+s");
        assert_eq!(normalize_global_shortcut("ctrl+shift+s"), "ctrl+shift+s");
    }

    #[test]
    fn normalize_hotkey_mode_falls_back_to_toggle() {
        assert_eq!(normalize_hotkey_mode("push_to_talk"), "push_to_talk");
        assert_eq!(normalize_hotkey_mode("toggle"), "toggle");
        assert_eq!(normalize_hotkey_mode("unexpected"), "toggle");
    }

    #[test]
    fn parse_loopback_backend_url_accepts_localhost_and_default_host() {
        assert_eq!(
            parse_loopback_backend_url("http://127.0.0.1:8765").unwrap(),
            ([127, 0, 0, 1], 8765)
        );
        assert_eq!(
            parse_loopback_backend_url("http://localhost:9999/").unwrap(),
            ([127, 0, 0, 1], 9999)
        );
    }

    #[test]
    fn parse_loopback_backend_url_rejects_non_loopback_host() {
        assert!(parse_loopback_backend_url("http://example.com:8765").is_err());
    }

    #[test]
    fn split_http_response_returns_status_and_body() {
        let response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true}";
        let (status, body) = split_http_response(response).unwrap();

        assert_eq!(status, "HTTP/1.1 200 OK");
        assert_eq!(body, "{\"ok\":true}");
    }

    #[test]
    fn shell_menu_item_filter_accepts_only_owned_items() {
        assert!(is_shell_menu_item(MENU_ITEM_SHOW_WINDOW));
        assert!(is_shell_menu_item(MENU_ITEM_RESTART_BACKEND));
        assert!(is_shell_menu_item(MENU_ITEM_QUIT));
        assert!(!is_shell_menu_item("copy"));
    }

    #[test]
    fn tray_left_click_reopens_main_window() {
        assert!(should_show_window_for_tray_click(
            MouseButton::Left,
            Some(MouseButtonState::Up)
        ));
        assert!(should_show_window_for_tray_click(MouseButton::Left, None));
        assert!(!should_show_window_for_tray_click(
            MouseButton::Left,
            Some(MouseButtonState::Down)
        ));
        assert!(!should_show_window_for_tray_click(
            MouseButton::Right,
            Some(MouseButtonState::Up)
        ));
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
