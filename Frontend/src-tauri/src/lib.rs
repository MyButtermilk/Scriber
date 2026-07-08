mod audio_devices;
mod audio_frame_pipe;
mod audio_sidecar_client;
mod native_overlay;
mod redaction;
mod shell_ipc;

use serde::{Deserialize, Serialize};
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
    image::Image,
    menu::{IsMenuItem, Menu, MenuBuilder, MenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, LogicalPosition, Manager, Runtime, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder,
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
#[cfg(windows)]
use windows::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR,
    DWMWA_USE_IMMERSIVE_DARK_MODE,
};
#[cfg(windows)]
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, GlobalFree, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND,
    ERROR_SUCCESS, HANDLE,
};
#[cfg(windows)]
use windows_sys::Win32::System::DataExchange::{
    CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData,
};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
#[cfg(windows)]
use windows_sys::Win32::System::Ole::CF_UNICODETEXT;
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
const BACKEND_START_TIMEOUT_ENV: &str = "SCRIBER_BACKEND_START_TIMEOUT_MS";
const BACKEND_SUPERVISOR_INTERVAL: Duration = Duration::from_secs(2);
const FORCE_MANAGED_BACKEND_ENV: &str = "SCRIBER_FORCE_MANAGED_BACKEND";
const SESSION_TOKEN_ENV: &str = "SCRIBER_SESSION_TOKEN";
const SHELL_IPC_PIPE_ENV: &str = "SCRIBER_SHELL_IPC_PIPE";
const SHELL_IPC_TOKEN_ENV: &str = "SCRIBER_SHELL_IPC_TOKEN";
const SHELL_IPC_API_VERSION_ENV: &str = "SCRIBER_SHELL_IPC_API_VERSION";
const DISABLE_HOTKEYS_ENV: &str = "SCRIBER_DISABLE_HOTKEYS";
const TAURI_GLOBAL_HOTKEY_ENV: &str = "SCRIBER_TAURI_GLOBAL_HOTKEY";
const SINGLE_INSTANCE_MUTEX_NAME: &str = "Local\\ScriberDesktopSingleInstance";
const AUTOSTART_REGISTRY_SUBKEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const AUTOSTART_REGISTRY_VALUE: &str = "Scriber";
const AUTOSTART_USER_CHOICE_FILE: &str = "desktop-autostart-user-choice";
const AUTOSTART_DEFAULT_ENV: &str = "SCRIBER_DESKTOP_AUTOSTART_DEFAULT";
const SHELL_MENU_SMOKE_ACTIONS_ENV: &str = "SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTIONS";
const SHELL_MENU_SMOKE_TRIGGER_FILE_ENV: &str = "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE";
const SHELL_MENU_SMOKE_TRIGGER_TIMEOUT_MS_ENV: &str =
    "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_TIMEOUT_MS";
const SHELL_MENU_SMOKE_ACTION_DELAY_MS_ENV: &str = "SCRIBER_TAURI_SMOKE_SHELL_MENU_ACTION_DELAY_MS";
const HOTKEY_DISPATCH_DEBOUNCE: Duration = Duration::from_millis(250);
const NATIVE_DEVICE_OBSERVE_ONLY_LOG_EVERY_EVENTS: u64 = 1000;
const NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL: Duration = Duration::from_secs(900);
const MAIN_WINDOW_LABEL: &str = "main";
const TRAY_ID: &str = "scriber-tray";
const TRAY_PANEL_LABEL: &str = "tray-panel";
const TRAY_STATUS_EVENT: &str = "scriber-tray-status";
const TRAY_NAVIGATE_EVENT: &str = "scriber-navigate";
const TRAY_PANEL_WIDTH: f64 = 386.0;
const TRAY_PANEL_HEIGHT: f64 = 620.0;
const TRAY_PANEL_MARGIN: f64 = 14.0;
const MENU_ITEM_SHOW_WINDOW: &str = "scriber-show-window";
const MENU_ITEM_START_LIVE: &str = "scriber-start-live";
const MENU_ITEM_YOUTUBE: &str = "scriber-open-youtube";
const MENU_ITEM_FILE: &str = "scriber-open-file";
const MENU_ITEM_SETTINGS: &str = "scriber-open-settings";
const MENU_ITEM_INSTALL_UPDATE: &str = "scriber-install-update";
const MENU_ITEM_RESTART_BACKEND: &str = "scriber-restart-backend";
const MENU_ITEM_REFRESH_RECENT: &str = "scriber-refresh-recent";
const MENU_ITEM_QUIT: &str = "scriber-quit";
const MENU_ITEM_COPY_TRANSCRIPT_PREFIX: &str = "scriber-copy-transcript-";
#[allow(dead_code)]
const MENU_RECENT_TRANSCRIPTS: &str = "scriber-recent-transcripts";
#[allow(dead_code)]
const MENU_ITEM_EMPTY_RECENT: &str = "scriber-empty-recent";
const MIN_MAIN_WINDOW_VISIBLE_PX: i32 = 96;
const TRAY_RECENT_TRANSCRIPT_LIMIT: usize = 5;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Default)]
struct TrayState {
    inner: Mutex<TrayStatusInner>,
}

#[derive(Debug, Clone)]
struct TrayStatusInner {
    recording_active: bool,
    recording_mode: String,
    update_available: bool,
    update_installing: bool,
    update_version: Option<String>,
    update_message: String,
}

impl Default for TrayStatusInner {
    fn default() -> Self {
        Self {
            recording_active: false,
            recording_mode: "idle".to_string(),
            update_available: false,
            update_installing: false,
            update_version: None,
            update_message: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TrayStatus {
    recording_active: bool,
    recording_mode: String,
    update_available: bool,
    update_installing: bool,
    update_version: Option<String>,
    update_message: String,
}

impl From<&TrayStatusInner> for TrayStatus {
    fn from(value: &TrayStatusInner) -> Self {
        Self {
            recording_active: value.recording_active,
            recording_mode: value.recording_mode.clone(),
            update_available: value.update_available,
            update_installing: value.update_installing,
            update_version: value.update_version.clone(),
            update_message: value.update_message.clone(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TrayUpdateStatusInput {
    available: bool,
    installing: bool,
    version: Option<String>,
    message: Option<String>,
}

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

#[derive(Debug)]
#[allow(dead_code)]
struct RecentTranscriptMenuEntry {
    id: String,
    title: String,
    date: String,
    transcript_type: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ShellMenuSmokeAction {
    ShowWindow,
    CopyRecent,
    HotkeyPress,
    HotkeyRelease,
    OverlayInitializing,
    OverlayRecording,
    OverlayTranscribing,
    OverlayHide,
    Quit,
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
    post_processing_hotkey: String,
    mode: String,
    message: String,
    capture_suspended: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DesktopWindowChromeTheme {
    Light,
    Dark,
}

impl DesktopWindowChromeTheme {
    fn parse(value: &str) -> Result<Self, String> {
        match value.trim().to_ascii_lowercase().as_str() {
            "light" => Ok(Self::Light),
            "dark" => Ok(Self::Dark),
            other => Err(format!("unsupported desktop window theme '{other}'")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Light => "light",
            Self::Dark => "dark",
        }
    }
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
    shell_ipc_config: Option<shell_ipc::ShellIpcConfig>,
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

struct NativeDeviceEventsState {
    handle: Mutex<Option<audio_devices::NativeDeviceEventMonitorHandle>>,
}

impl NativeDeviceEventsState {
    fn new() -> Self {
        Self {
            handle: Mutex::new(None),
        }
    }

    fn set_handle(&self, handle: Option<audio_devices::NativeDeviceEventMonitorHandle>) {
        if let Ok(mut state) = self.handle.lock() {
            *state = handle;
        }
    }
}

struct ShellIpcState {
    config: shell_ipc::ShellIpcConfig,
    handle: Mutex<Option<shell_ipc::ShellIpcServerHandle>>,
}

impl ShellIpcState {
    fn new(
        config: shell_ipc::ShellIpcConfig,
        handle: Option<shell_ipc::ShellIpcServerHandle>,
    ) -> Self {
        Self {
            config,
            handle: Mutex::new(handle),
        }
    }

    fn config(&self) -> shell_ipc::ShellIpcConfig {
        self.config.clone()
    }

    fn is_running(&self) -> bool {
        self.handle
            .lock()
            .map(|handle| handle.is_some())
            .unwrap_or(false)
    }
}

struct DesktopHotkeyStateInner {
    registered_hotkey: Option<String>,
    registered_hotkey_id: Option<u32>,
    post_processing_hotkey: Option<String>,
    post_processing_hotkey_id: Option<u32>,
    post_processing_enabled: bool,
    mode: String,
    available: bool,
    message: String,
    last_dispatched_at: Option<Instant>,
    capture_suspended: bool,
}

impl DesktopHotkeyState {
    fn new() -> Self {
        Self {
            inner: Mutex::new(DesktopHotkeyStateInner {
                registered_hotkey: None,
                registered_hotkey_id: None,
                post_processing_hotkey: None,
                post_processing_hotkey_id: None,
                post_processing_enabled: false,
                mode: "toggle".to_string(),
                available: false,
                message: "Global hotkey not initialized".to_string(),
                last_dispatched_at: None,
                capture_suspended: false,
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
                post_processing_hotkey: state.post_processing_hotkey.clone().unwrap_or_default(),
                mode: state.mode.clone(),
                message: state.message.clone(),
                capture_suspended: state.capture_suspended,
            })
            .unwrap_or_else(|_| DesktopHotkeyStatus {
                registered: false,
                available: false,
                hotkey: String::new(),
                post_processing_hotkey: String::new(),
                mode: "toggle".to_string(),
                message: "Global hotkey state lock is poisoned".to_string(),
                capture_suspended: false,
            })
    }

    fn set_registered(
        &self,
        hotkey: String,
        post_processing_hotkey: String,
        post_processing_enabled: bool,
        mode: String,
        message: String,
    ) {
        if let Ok(mut state) = self.inner.lock() {
            let post_processing_enabled =
                post_processing_enabled && !post_processing_hotkey.is_empty() && post_processing_hotkey != hotkey;
            state.registered_hotkey_id = shortcut_id_for_hotkey(&hotkey);
            state.registered_hotkey = Some(hotkey);
            state.post_processing_hotkey_id = if post_processing_enabled {
                shortcut_id_for_hotkey(&post_processing_hotkey)
            } else {
                None
            };
            state.post_processing_hotkey =
                if post_processing_enabled {
                    Some(post_processing_hotkey)
                } else {
                    None
                };
            state.post_processing_enabled = post_processing_enabled;
            state.mode = mode;
            state.available = true;
            state.message = message;
            state.capture_suspended = false;
            state.last_dispatched_at = None;
        }
    }

    fn is_registered_config(
        &self,
        hotkey: &str,
        post_processing_hotkey: &str,
        post_processing_enabled: bool,
        mode: &str,
    ) -> bool {
        self.inner
            .lock()
            .map(|state| {
                state.available
                    && !state.capture_suspended
                    && state.registered_hotkey.as_deref() == Some(hotkey)
                    && state.post_processing_hotkey.as_deref()
                        == if post_processing_enabled
                            && !post_processing_hotkey.is_empty()
                            && post_processing_hotkey != hotkey
                        {
                            Some(post_processing_hotkey)
                        } else {
                            None
                        }
                    && state.mode == mode
            })
            .unwrap_or(false)
    }

    fn set_unregistered(&self, mode: String, available: bool, message: String) {
        if let Ok(mut state) = self.inner.lock() {
            state.registered_hotkey = None;
            state.registered_hotkey_id = None;
            state.post_processing_hotkey = None;
            state.post_processing_hotkey_id = None;
            state.post_processing_enabled = false;
            state.mode = mode;
            state.available = available;
            state.message = message;
            state.last_dispatched_at = None;
        }
    }

    fn is_capture_suspended(&self) -> bool {
        self.inner
            .lock()
            .map(|state| state.capture_suspended)
            .unwrap_or(false)
    }

    fn set_capture_suspended(&self, suspended: bool, message: String) {
        if let Ok(mut state) = self.inner.lock() {
            state.capture_suspended = suspended;
            state.available = true;
            state.message = message;
            if suspended {
                state.registered_hotkey = None;
                state.registered_hotkey_id = None;
                state.post_processing_hotkey = None;
                state.post_processing_hotkey_id = None;
                state.post_processing_enabled = false;
                state.last_dispatched_at = None;
            }
        }
    }

    fn action_for_event(
        &self,
        shortcut_id: u32,
        event_state: ShortcutState,
        now: Instant,
    ) -> Option<&'static str> {
        let mut state = self.inner.lock().ok()?;
        if state.capture_suspended {
            return None;
        }
        let is_primary = state.registered_hotkey_id == Some(shortcut_id);
        let is_post_processing = state.post_processing_enabled
            && state.post_processing_hotkey_id == Some(shortcut_id)
            && state.registered_hotkey_id != Some(shortcut_id);
        if !is_primary && !is_post_processing {
            return None;
        }
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
                if is_post_processing {
                    Some("/api/live-mic/toggle-post-processing")
                } else if state.mode == "push_to_talk" {
                    Some("/api/live-mic/start")
                } else {
                    Some("/api/live-mic/toggle")
                }
            }
            ShortcutState::Released => {
                if is_primary && state.mode == "push_to_talk" {
                    Some("/api/live-mic/stop")
                } else {
                    None
                }
            }
        }
    }
}

#[derive(Default)]
struct NativeDeviceObserveOnlyLogState {
    observed_count: u64,
    last_logged_at: Option<Instant>,
}

impl NativeDeviceObserveOnlyLogState {
    fn maybe_summary(
        &mut self,
        event: &audio_devices::NativeDeviceEvent,
        now: Instant,
    ) -> Option<String> {
        self.observed_count = self.observed_count.saturating_add(1);
        let count_due = self.observed_count == 1
            || self.observed_count % NATIVE_DEVICE_OBSERVE_ONLY_LOG_EVERY_EVENTS == 0;
        let time_due = self
            .last_logged_at
            .map(|last| now.duration_since(last) >= NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL)
            .unwrap_or(true);
        if !count_due && !time_due {
            return None;
        }
        self.last_logged_at = Some(now);
        Some(format!(
            "native device events observed summary mode=observe-only count={} last_kind={} flow={} role={} endpoint_hash={}",
            self.observed_count, event.event_kind, event.flow, event.role, event.endpoint_id_hash
        ))
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

fn poisoned_backend_status() -> BackendStatus {
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

impl BackendManager {
    fn new(shell_ipc_config: Option<shell_ipc::ShellIpcConfig>) -> Self {
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
                shell_ipc_config,
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
        // Phase 1: snapshot under lock, then release before the blocking health check.
        let (port, force_managed) = match self.state.lock() {
            Ok(mut state) => {
                refresh_child_state(&mut state);
                (state.port, force_managed_backend())
            }
            Err(_) => return poisoned_backend_status(),
        };

        // Phase 2: blocking TCP health check outside the lock (up to ~1 s).
        let ready = health_ready(port);

        // Phase 3: relock and decide, guarding against port changes while unlocked.
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            if ready && state.port == port && (!force_managed || state.child.is_some()) {
                state.started_at = None;
                state.message = if state.child.is_some() {
                    "Managed backend is ready".to_string()
                } else {
                    "Attached to existing backend".to_string()
                };
                return status_from_state(&state, true);
            }
            if state.child.is_some() {
                if state.started_at.is_some()
                    && managed_backend_start_timed_out(state.started_at, Instant::now())
                {
                    terminate_managed_child(&mut state);
                    state.message = "Managed backend startup timed out; restarting".to_string();
                } else {
                    state.message = if state.started_at.is_some() {
                        "Managed backend is starting".to_string()
                    } else {
                        "Managed backend is not responding".to_string()
                    };
                    return status_from_state(&state, false);
                }
            }

            let new_port = select_backend_port(state.port);
            return start_managed_backend(&mut state, new_port, "Managed backend process started");
        }
        poisoned_backend_status()
    }

    fn status(&self) -> BackendStatus {
        // Phase 1: snapshot under lock.
        let (port, force_managed) = match self.state.lock() {
            Ok(mut state) => {
                refresh_child_state(&mut state);
                (state.port, force_managed_backend())
            }
            Err(_) => return poisoned_backend_status(),
        };

        // Phase 2: blocking TCP health check outside the lock (up to ~1 s).
        let health = health_ready(port);

        // Phase 3: relock and compute status.
        if let Ok(mut state) = self.state.lock() {
            refresh_child_state(&mut state);
            let ready = health && state.port == port && (!force_managed || state.child.is_some());
            if ready {
                state.started_at = None;
                if state.child.is_none() {
                    state.message = "Attached to existing backend".to_string();
                }
            } else if !ready
                && state.child.is_some()
                && state.started_at.is_some()
                && managed_backend_start_timed_out(state.started_at, Instant::now())
            {
                state.message = "Managed backend startup timed out".to_string();
            } else if !ready && state.child.is_some() && state.started_at.is_none() {
                state.message = "Managed backend is not responding".to_string();
            }
            return status_from_state(&state, ready);
        }
        poisoned_backend_status()
    }

    fn restart(&self) -> Result<BackendStatus, String> {
        // Phase 1: snapshot under lock.
        let port = match self.state.lock() {
            Ok(mut state) => {
                refresh_child_state(&mut state);
                state.port
            }
            Err(_) => return Err("Backend state lock is poisoned".to_string()),
        };

        // Phase 2: blocking TCP health check outside the lock (up to ~1 s).
        let appears_external = !force_managed_backend() && health_ready(port);

        // Phase 3: relock and act.
        let mut state = self
            .state
            .lock()
            .map_err(|_| "Backend state lock is poisoned".to_string())?;
        refresh_child_state(&mut state);
        // Guard: only reject if the port hasn't changed and we still have no managed child.
        if state.child.is_none() && appears_external && state.port == port {
            let message =
                "Cannot restart backend because the current backend is external".to_string();
            state.message = message.clone();
            return Err(message);
        }

        terminate_managed_child(&mut state);
        let new_port = select_backend_port(state.port);
        let status = start_managed_backend(&mut state, new_port, "Managed backend restarted");
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
    let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("backendRestartCommand");
    if stopped > 0 {
        write_shell_log(&format!(
            "stopped {stopped} audio sidecar(s) before backend restart command"
        ));
    }
    manager.restart()
}

#[tauri::command]
fn get_desktop_autostart() -> DesktopAutostartStatus {
    desktop_autostart_status()
}

#[tauri::command]
fn set_desktop_autostart(app: AppHandle, enabled: bool) -> Result<DesktopAutostartStatus, String> {
    set_desktop_autostart_enabled(enabled)?;
    persist_desktop_autostart_user_choice(&app, enabled);
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

#[tauri::command]
fn set_global_hotkey_capture_active(
    app: AppHandle,
    active: bool,
) -> Result<DesktopHotkeyStatus, String> {
    let hotkey_state = app.state::<DesktopHotkeyState>();
    if active {
        app.global_shortcut()
            .unregister_all()
            .map_err(|err| format!("Could not suspend global hotkey capture: {err}"))?;
        let message = "Global hotkey suspended while recording a new shortcut".to_string();
        write_shell_log(&message);
        hotkey_state.set_capture_suspended(true, message);
        return Ok(hotkey_state.status());
    }

    hotkey_state.set_capture_suspended(
        false,
        "Global hotkey capture finished; refreshing registration".to_string(),
    );
    refresh_global_hotkey_for_app(&app)
}

#[tauri::command]
fn set_desktop_window_chrome_theme(app: AppHandle, theme: String) -> Result<(), String> {
    let theme = DesktopWindowChromeTheme::parse(&theme)?;
    let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) else {
        return Err("main window not found".to_string());
    };
    apply_desktop_window_chrome_theme(&window, theme)
}

#[tauri::command]
fn tray_status(app: AppHandle) -> TrayStatus {
    tray_status_for_app(&app)
}

#[tauri::command]
fn set_tray_update_status(
    app: AppHandle,
    status: TrayUpdateStatusInput,
) -> Result<TrayStatus, String> {
    let next = update_tray_status_for_app(&app, |state| {
        state.update_available = status.available;
        state.update_installing = status.installing;
        state.update_version = status
            .version
            .map(|value| sanitize_update_field(&value, 32))
            .filter(|value| !value.is_empty());
        state.update_message = status
            .message
            .map(|value| sanitize_update_field(&value, 96))
            .unwrap_or_default();
    });
    Ok(next)
}

#[tauri::command]
fn set_tray_recording_state(
    app: AppHandle,
    active: bool,
    mode: Option<String>,
) -> Result<TrayStatus, String> {
    let next = update_tray_status_for_app(&app, |state| {
        state.recording_active = active;
        state.recording_mode = normalize_tray_recording_mode(mode.as_deref(), active);
    });
    Ok(next)
}

#[tauri::command]
fn show_tray_panel(app: AppHandle) -> Result<(), String> {
    show_tray_panel_for_app(&app)
}

#[tauri::command]
fn hide_tray_panel(app: AppHandle) -> Result<(), String> {
    hide_tray_panel_for_app(&app)
}

#[tauri::command]
fn tray_action(app: AppHandle, action: String) -> Result<(), String> {
    handle_tray_action(&app, &action)
}

fn apply_desktop_window_chrome_theme<R: Runtime>(
    window: &tauri::WebviewWindow<R>,
    theme: DesktopWindowChromeTheme,
) -> Result<(), String> {
    #[cfg(windows)]
    {
        apply_windows_desktop_window_chrome_theme(window, theme)?;
    }
    #[cfg(not(windows))]
    {
        let _ = (window, theme);
    }
    Ok(())
}

#[cfg(windows)]
fn apply_windows_desktop_window_chrome_theme<R: Runtime>(
    window: &tauri::WebviewWindow<R>,
    theme: DesktopWindowChromeTheme,
) -> Result<(), String> {
    let hwnd = window
        .hwnd()
        .map_err(|err| format!("failed to get main window handle: {err}"))?;
    let use_dark_mode: i32 = if theme == DesktopWindowChromeTheme::Dark {
        1
    } else {
        0
    };
    let (caption_color, text_color, border_color) = desktop_window_chrome_colors(theme);

    unsafe {
        set_dwm_window_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, &use_dark_mode)
            .map_err(|err| format!("failed to set DWM dark mode: {err}"))?;
        set_dwm_window_attribute(hwnd, DWMWA_CAPTION_COLOR, &caption_color)
            .map_err(|err| format!("failed to set DWM caption color: {err}"))?;
        set_dwm_window_attribute(hwnd, DWMWA_TEXT_COLOR, &text_color)
            .map_err(|err| format!("failed to set DWM text color: {err}"))?;
        set_dwm_window_attribute(hwnd, DWMWA_BORDER_COLOR, &border_color)
            .map_err(|err| format!("failed to set DWM border color: {err}"))?;
    }

    write_shell_log(&format!(
        "desktop window chrome theme applied: {}",
        theme.as_str()
    ));
    Ok(())
}

#[cfg(windows)]
unsafe fn set_dwm_window_attribute<T>(
    hwnd: windows::Win32::Foundation::HWND,
    attribute: windows::Win32::Graphics::Dwm::DWMWINDOWATTRIBUTE,
    value: &T,
) -> windows::core::Result<()> {
    unsafe {
        DwmSetWindowAttribute(
            hwnd,
            attribute,
            value as *const T as *const core::ffi::c_void,
            std::mem::size_of::<T>() as u32,
        )
    }
}

#[cfg(windows)]
fn desktop_window_chrome_colors(theme: DesktopWindowChromeTheme) -> (u32, u32, u32) {
    match theme {
        // COLORREF is 0x00bbggrr, so keep the helper in RGB order.
        DesktopWindowChromeTheme::Dark => (
            rgb_to_colorref(31, 34, 40),
            rgb_to_colorref(245, 247, 250),
            rgb_to_colorref(31, 34, 40),
        ),
        DesktopWindowChromeTheme::Light => (
            rgb_to_colorref(229, 231, 235),
            rgb_to_colorref(9, 17, 32),
            rgb_to_colorref(229, 231, 235),
        ),
    }
}

#[cfg(windows)]
fn rgb_to_colorref(red: u8, green: u8, blue: u8) -> u32 {
    u32::from(red) | (u32::from(green) << 8) | (u32::from(blue) << 16)
}

pub fn run() {
    let single_instance_guard = match acquire_single_instance_guard(SINGLE_INSTANCE_MUTEX_NAME) {
        Ok(guard) => guard,
        Err(err) => {
            write_shell_log(&err);
            return;
        }
    };
    let cleaned_sidecars =
        audio_sidecar_client::cleanup_stray_audio_sidecar_processes("shellStartup");
    if cleaned_sidecars > 0 {
        write_shell_log(&format!(
            "cleaned {cleaned_sidecars} stray audio sidecar process(es) during shell startup"
        ));
    }
    let shell_ipc_config = shell_ipc::ShellIpcConfig::new();
    let shell_ipc_handle =
        match shell_ipc::start_shell_ipc_server(shell_ipc_config.clone(), |message| {
            write_shell_log(&message)
        }) {
            Ok(handle) => handle,
            Err(err) => {
                write_shell_log(&format!("shell IPC server unavailable: {err}"));
                None
            }
        };
    let backend_shell_ipc_config = if shell_ipc_handle.is_some() {
        Some(shell_ipc_config.clone())
    } else {
        None
    };
    let backend_manager = BackendManager::new(backend_shell_ipc_config);
    write_shell_log("early backend ensure deferred until Tauri setup completes");

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    handle_global_shortcut_event(app, shortcut, event.state);
                })
                .build(),
        )
        .on_menu_event(|app, event| {
            handle_shell_menu_event(app, event.id().as_ref());
        })
        .manage(single_instance_guard)
        .manage(DesktopHotkeyState::new())
        .manage(TrayState::default())
        .manage(NativeDeviceEventsState::new())
        .manage(ShellIpcState::new(shell_ipc_config, shell_ipc_handle))
        .manage(backend_manager)
        .setup(|app| {
            configure_desktop_shell(app)?;
            native_overlay::set_app_handle(app.handle().clone());
            match native_overlay::create_overlay_window(app) {
                Ok(()) => write_shell_log("native overlay hidden window precreated"),
                Err(err) => write_shell_log(&format!(
                    "native overlay hidden window precreate skipped: {err}"
                )),
            }
            apply_default_desktop_autostart(app.handle());
            let manager = app.state::<BackendManager>();
            manager.set_resource_dir(app.path().resource_dir().ok());
            start_backend_supervisor(app.handle().clone());
            write_shell_log(
                "setup backend ensure and global hotkey registration deferred to supervisor",
            );
            let native_events_handle =
                start_native_device_event_monitor_for_app(app.handle().clone());
            app.state::<NativeDeviceEventsState>()
                .set_handle(native_events_handle);
            start_shell_menu_smoke_actions(app.handle().clone());
            let shell_ipc_state = app.state::<ShellIpcState>();
            write_shell_log(&format!(
                "shell IPC state running={} pipe_hash={}",
                shell_ipc_state.is_running(),
                shell_ipc_state.config().pipe_name_hash()
            ));
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
            refresh_global_hotkey,
            set_global_hotkey_capture_active,
            set_desktop_window_chrome_theme,
            tray_status,
            set_tray_update_status,
            set_tray_recording_state,
            show_tray_panel,
            hide_tray_panel,
            tray_action
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Scriber desktop shell");

    app.run(|_app_handle, event| {
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("shellExit");
            if stopped > 0 {
                write_shell_log(&format!(
                    "stopped {stopped} audio sidecar(s) during shell exit"
                ));
            }
        }
    });
}

fn configure_desktop_shell<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    apply_desktop_window_icon(app.handle());
    install_tray(app)?;
    Ok(())
}

fn apply_default_desktop_autostart<R: Runtime>(app: &AppHandle<R>) {
    if !desktop_autostart_default_enabled() {
        write_shell_log("desktop autostart first-run default skipped by environment");
        return;
    }

    if cfg!(debug_assertions) {
        write_shell_log("desktop autostart first-run default skipped in debug build");
        return;
    }

    match desktop_autostart_user_choice_path(app) {
        Some(choice_path) if choice_path.exists() => {
            write_shell_log("desktop autostart default skipped: user preference exists");
            return;
        }
        Some(_) => {}
        None => {
            write_shell_log("desktop autostart default skipped: app data directory unavailable");
            return;
        }
    }

    match set_desktop_autostart_enabled(true) {
        Ok(()) => {
            write_shell_log("desktop autostart enabled by install default");
        }
        Err(err) => {
            write_shell_log(&format!("desktop autostart install default skipped: {err}"));
        }
    }
}

fn desktop_autostart_default_enabled() -> bool {
    match env::var(AUTOSTART_DEFAULT_ENV) {
        Ok(value) => env_flag_enabled(&value),
        Err(_) => true,
    }
}

fn env_flag_enabled(value: &str) -> bool {
    !matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off" | "disabled"
    )
}

fn desktop_autostart_user_choice_path<R: Runtime>(app: &AppHandle<R>) -> Option<PathBuf> {
    app.path()
        .app_data_dir()
        .ok()
        .map(|dir| dir.join(AUTOSTART_USER_CHOICE_FILE))
}

fn persist_desktop_autostart_user_choice<R: Runtime>(app: &AppHandle<R>, enabled: bool) {
    let Some(path) = desktop_autostart_user_choice_path(app) else {
        write_shell_log(
            "desktop autostart user preference not persisted: app data directory unavailable",
        );
        return;
    };
    if let Some(parent) = path.parent() {
        if let Err(err) = fs::create_dir_all(parent) {
            write_shell_log(&format!(
                "desktop autostart user preference directory failed: {err}"
            ));
            return;
        }
    }
    let value: &[u8] = if enabled { b"enabled\n" } else { b"disabled\n" };
    if let Err(err) = fs::write(path, value) {
        write_shell_log(&format!(
            "desktop autostart user preference write failed: {err}"
        ));
    }
}

fn install_tray<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    let mut tray = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("Scriber")
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if should_show_tray_panel_for_event(&event) {
                if let Err(err) = show_tray_panel_for_app(tray.app_handle()) {
                    write_shell_log(&format!("tray panel show failed from tray click: {err}"));
                }
            }
        });

    tray = tray.icon(tray_icon_image(TrayIconKind::Normal));

    tray.build(app)?;
    Ok(())
}

#[allow(dead_code)]
fn build_tray_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let status = tray_status_for_app(app);
    let live_label = if status.recording_active {
        "Stop Recording"
    } else {
        "Start Live Transcription"
    };
    let live_item = MenuItem::with_id(app, MENU_ITEM_START_LIVE, live_label, true, None::<&str>)?;
    let youtube_item = MenuItem::with_id(
        app,
        MENU_ITEM_YOUTUBE,
        "YouTube Transcription",
        true,
        None::<&str>,
    )?;
    let file_item = MenuItem::with_id(app, MENU_ITEM_FILE, "Transcribe File", true, None::<&str>)?;
    let show_item = MenuItem::with_id(
        app,
        MENU_ITEM_SHOW_WINDOW,
        "Open Main Window",
        true,
        None::<&str>,
    )?;
    let settings_item = MenuItem::with_id(app, MENU_ITEM_SETTINGS, "Settings", true, None::<&str>)?;
    let update_label = match &status.update_version {
        Some(version) if !version.trim().is_empty() => {
            format!("Update available: Scriber {version}")
        }
        _ => "Update available, install now".to_string(),
    };
    let update_item = MenuItem::with_id(
        app,
        MENU_ITEM_INSTALL_UPDATE,
        update_label,
        status.update_available || status.update_installing,
        None::<&str>,
    )?;
    let refresh_item = MenuItem::with_id(
        app,
        MENU_ITEM_REFRESH_RECENT,
        "Refresh Recent Transcripts",
        true,
        None::<&str>,
    )?;
    let restart_item = MenuItem::with_id(
        app,
        MENU_ITEM_RESTART_BACKEND,
        "Restart Backend",
        true,
        None::<&str>,
    )?;
    let quit_item = MenuItem::with_id(app, MENU_ITEM_QUIT, "Quit", true, None::<&str>)?;
    let recent_submenu = build_recent_transcripts_submenu(app)?;

    let builder = MenuBuilder::new(app)
        .item(&live_item)
        .item(&youtube_item)
        .item(&file_item)
        .separator()
        .item(&recent_submenu)
        .item(&refresh_item)
        .item(&show_item)
        .separator()
        .item(&settings_item);

    let builder = if status.update_available || status.update_installing {
        builder.item(&update_item)
    } else {
        builder
    };

    builder
        .separator()
        .item(&restart_item)
        .separator()
        .item(&quit_item)
        .build()
}

#[allow(dead_code)]
fn build_recent_transcripts_submenu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Submenu<R>> {
    let mut recent_items: Vec<MenuItem<R>> = Vec::new();
    if let Some(manager) = app.try_state::<BackendManager>() {
        let status = manager.status();
        if status.ready {
            match fetch_recent_transcripts(&manager.access()) {
                Ok(entries) => {
                    for entry in entries {
                        let label = recent_transcript_label(&entry);
                        let item_id = format!("{MENU_ITEM_COPY_TRANSCRIPT_PREFIX}{}", entry.id);
                        recent_items.push(MenuItem::with_id(
                            app,
                            item_id,
                            label,
                            true,
                            None::<&str>,
                        )?);
                    }
                }
                Err(err) => {
                    write_shell_log(&format!("recent transcripts tray fetch failed: {err}"));
                }
            }
        }
    }

    if recent_items.is_empty() {
        recent_items.push(MenuItem::with_id(
            app,
            MENU_ITEM_EMPTY_RECENT,
            "No completed transcripts",
            false,
            None::<&str>,
        )?);
    }

    let recent_refs: Vec<&dyn IsMenuItem<R>> = recent_items
        .iter()
        .map(|item| item as &dyn IsMenuItem<R>)
        .collect();
    Submenu::with_id_and_items(
        app,
        MENU_RECENT_TRANSCRIPTS,
        "Recent Transcripts",
        true,
        &recent_refs,
    )
}

fn handle_shell_menu_event<R: Runtime>(app: &AppHandle<R>, item_id: &str) {
    if !is_shell_menu_item(item_id) {
        return;
    }

    if let Some(transcript_id) = item_id.strip_prefix(MENU_ITEM_COPY_TRANSCRIPT_PREFIX) {
        copy_recent_transcript_from_shell(app, transcript_id);
        return;
    }

    match item_id {
        MENU_ITEM_SHOW_WINDOW => {
            show_main_window(app);
            let _ = hide_tray_panel_for_app(app);
        }
        MENU_ITEM_START_LIVE => {
            if let Err(err) = toggle_live_recording_from_shell(app) {
                write_shell_log(&format!("live recording tray action failed: {err}"));
            }
        }
        MENU_ITEM_YOUTUBE => {
            let _ = show_main_window_path(app, "/youtube");
            let _ = hide_tray_panel_for_app(app);
        }
        MENU_ITEM_FILE => {
            let _ = show_main_window_path(app, "/file");
            let _ = hide_tray_panel_for_app(app);
        }
        MENU_ITEM_SETTINGS => {
            let _ = show_main_window_path(app, "/settings");
            let _ = hide_tray_panel_for_app(app);
        }
        MENU_ITEM_INSTALL_UPDATE => {
            if let Err(err) = show_tray_panel_for_app(app) {
                write_shell_log(&format!("tray update panel show failed: {err}"));
            }
            if let Err(err) = app.emit(TRAY_STATUS_EVENT, tray_status_for_app(app)) {
                write_shell_log(&format!("tray update status emit failed: {err}"));
            }
        }
        MENU_ITEM_RESTART_BACKEND => restart_backend_from_shell(app),
        MENU_ITEM_REFRESH_RECENT => refresh_tray_menu_for_app(app, "manual refresh"),
        MENU_ITEM_QUIT => {
            let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("shellQuit");
            if stopped > 0 {
                write_shell_log(&format!(
                    "stopped {stopped} audio sidecar(s) before shell quit"
                ));
            }
            app.exit(0);
        }
        _ => {}
    }
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) else {
        write_shell_log("main window focus requested, but the main window was not found");
        return;
    };

    if let Err(err) = ensure_main_window_visible(&window) {
        write_shell_log(&format!("main window visibility check failed: {err}"));
    }
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

fn ensure_main_window_visible<R: Runtime>(window: &WebviewWindow<R>) -> Result<(), String> {
    let monitors = window
        .available_monitors()
        .map_err(|err| format!("available monitor lookup failed: {err}"))?;
    if monitors.is_empty() {
        return Ok(());
    }

    let position = window
        .outer_position()
        .map_err(|err| format!("main window position lookup failed: {err}"))?;
    let size = window
        .outer_size()
        .map_err(|err| format!("main window size lookup failed: {err}"))?;

    let is_visible = monitors.iter().any(|monitor| {
        let work_area = monitor.work_area();
        physical_rect_has_min_visible_area(
            position.x,
            position.y,
            size.width,
            size.height,
            work_area.position.x,
            work_area.position.y,
            work_area.size.width,
            work_area.size.height,
            MIN_MAIN_WINDOW_VISIBLE_PX,
        )
    });
    if is_visible {
        return Ok(());
    }

    let target_monitor = window
        .current_monitor()
        .map_err(|err| format!("current monitor lookup failed: {err}"))?
        .or(window
            .primary_monitor()
            .map_err(|err| format!("primary monitor lookup failed: {err}"))?)
        .or_else(|| monitors.into_iter().next());

    let Some(monitor) = target_monitor else {
        return Ok(());
    };
    let work_area = monitor.work_area();
    let scale = monitor.scale_factor().max(0.25);
    let work_x = work_area.position.x as f64 / scale;
    let work_y = work_area.position.y as f64 / scale;
    let work_width = work_area.size.width as f64 / scale;
    let work_height = work_area.size.height as f64 / scale;
    let window_width = (size.width as f64 / scale).min(work_width);
    let window_height = (size.height as f64 / scale).min(work_height);
    let x = work_x + ((work_width - window_width) / 2.0).max(0.0);
    let y = work_y + ((work_height - window_height) / 2.0).max(0.0);

    window
        .set_position(LogicalPosition::new(x.round(), y.round()))
        .map_err(|err| format!("main window reposition failed: {err}"))?;
    write_shell_log("main window was off-screen and has been moved to a visible monitor");
    Ok(())
}

fn physical_rect_has_min_visible_area(
    window_x: i32,
    window_y: i32,
    window_width: u32,
    window_height: u32,
    work_x: i32,
    work_y: i32,
    work_width: u32,
    work_height: u32,
    min_visible_px: i32,
) -> bool {
    if window_width == 0 || window_height == 0 || work_width == 0 || work_height == 0 {
        return false;
    }
    let minimum = i64::from(min_visible_px.max(1));
    let window_left = i64::from(window_x);
    let window_top = i64::from(window_y);
    let window_right = window_left + i64::from(window_width);
    let window_bottom = window_top + i64::from(window_height);
    let work_left = i64::from(work_x);
    let work_top = i64::from(work_y);
    let work_right = work_left + i64::from(work_width);
    let work_bottom = work_top + i64::from(work_height);
    let visible_width = (window_right.min(work_right) - window_left.max(work_left)).max(0);
    let visible_height = (window_bottom.min(work_bottom) - window_top.max(work_top)).max(0);
    visible_width >= minimum && visible_height >= minimum
}

fn start_shell_menu_smoke_actions<R>(app: AppHandle<R>)
where
    R: Runtime + 'static,
{
    let actions =
        parse_shell_menu_smoke_actions(&env::var(SHELL_MENU_SMOKE_ACTIONS_ENV).unwrap_or_default());
    if actions.is_empty() {
        return;
    }

    let trigger_path = env::var(SHELL_MENU_SMOKE_TRIGGER_FILE_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from);
    let trigger_timeout = env_duration_ms(
        SHELL_MENU_SMOKE_TRIGGER_TIMEOUT_MS_ENV,
        Duration::from_secs(60),
        Duration::from_secs(1),
        Duration::from_secs(300),
    );
    let action_delay = env_duration_ms(
        SHELL_MENU_SMOKE_ACTION_DELAY_MS_ENV,
        Duration::from_millis(250),
        Duration::from_millis(0),
        Duration::from_secs(30),
    );

    if let Err(err) = std::thread::Builder::new()
        .name("shell-menu-smoke".to_string())
        .spawn(move || {
            if let Some(path) = trigger_path.as_ref() {
                write_shell_log(&format!(
                    "shell menu smoke waiting for trigger path_hash={}",
                    redaction::hash_sensitive_identifier(&path.display().to_string())
                ));
                if !wait_for_shell_menu_smoke_trigger(path, trigger_timeout) {
                    write_shell_log("shell menu smoke trigger timed out");
                    return;
                }
                write_shell_log("shell menu smoke trigger observed");
            }

            for action in actions {
                if !action_delay.is_zero() {
                    std::thread::sleep(action_delay);
                }
                match action {
                    ShellMenuSmokeAction::ShowWindow => run_shell_menu_smoke_show_window(&app),
                    ShellMenuSmokeAction::CopyRecent => run_shell_menu_smoke_copy_recent(&app),
                    ShellMenuSmokeAction::HotkeyPress => run_shell_menu_smoke_hotkey_event(
                        &app,
                        ShortcutState::Pressed,
                        "hotkey-press",
                    ),
                    ShellMenuSmokeAction::HotkeyRelease => run_shell_menu_smoke_hotkey_event(
                        &app,
                        ShortcutState::Released,
                        "hotkey-release",
                    ),
                    ShellMenuSmokeAction::OverlayInitializing => {
                        run_shell_menu_smoke_overlay_show("initializing")
                    }
                    ShellMenuSmokeAction::OverlayRecording => {
                        run_shell_menu_smoke_overlay_show("recording")
                    }
                    ShellMenuSmokeAction::OverlayTranscribing => {
                        run_shell_menu_smoke_overlay_show("transcribing")
                    }
                    ShellMenuSmokeAction::OverlayHide => run_shell_menu_smoke_overlay_hide(),
                    ShellMenuSmokeAction::Quit => {
                        run_shell_menu_smoke_quit(&app);
                        break;
                    }
                }
            }
        })
    {
        write_shell_log(&format!("shell menu smoke thread failed: {err}"));
    }
}

fn parse_shell_menu_smoke_actions(raw: &str) -> Vec<ShellMenuSmokeAction> {
    raw.split([',', ';', ' ', '\n', '\r', '\t'])
        .filter_map(|part| match part.trim().to_ascii_lowercase().as_str() {
            "show" | "open" | "open-window" | "show-window" => {
                Some(ShellMenuSmokeAction::ShowWindow)
            }
            "copy-recent" | "copy-recent-transcript" | "recent-copy" => {
                Some(ShellMenuSmokeAction::CopyRecent)
            }
            "hotkey-press" | "push-to-talk-press" | "ptt-press" => {
                Some(ShellMenuSmokeAction::HotkeyPress)
            }
            "hotkey-release" | "push-to-talk-release" | "ptt-release" => {
                Some(ShellMenuSmokeAction::HotkeyRelease)
            }
            "overlay-initializing" | "overlay-preparing" => {
                Some(ShellMenuSmokeAction::OverlayInitializing)
            }
            "overlay-recording" => Some(ShellMenuSmokeAction::OverlayRecording),
            "overlay-transcribing" | "overlay-finalizing" => {
                Some(ShellMenuSmokeAction::OverlayTranscribing)
            }
            "overlay-hide" | "hide-overlay" => Some(ShellMenuSmokeAction::OverlayHide),
            "quit" | "exit" => Some(ShellMenuSmokeAction::Quit),
            _ => None,
        })
        .collect()
}

fn wait_for_shell_menu_smoke_trigger(path: &Path, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if path.exists() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    path.exists()
}

fn run_shell_menu_smoke_show_window<R: Runtime>(app: &AppHandle<R>) {
    let started = Instant::now();
    let mut hide_succeeded = false;
    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
        match window.hide() {
            Ok(()) => {
                hide_succeeded = true;
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(err) => write_shell_log(&format!("shell menu smoke setup hide failed: {err}")),
        }
    } else {
        write_shell_log("shell menu smoke setup hide skipped: main window not found");
    }

    show_main_window(app);
    std::thread::sleep(Duration::from_millis(100));
    let visible = app
        .get_webview_window(MAIN_WINDOW_LABEL)
        .and_then(|window| window.is_visible().ok())
        .unwrap_or(false);
    write_shell_log(&format!(
        "shell menu smoke action show-window completed elapsedMs={} visible={} hideSucceeded={}",
        started.elapsed().as_millis(),
        visible,
        hide_succeeded
    ));
}

fn run_shell_menu_smoke_copy_recent<R: Runtime>(app: &AppHandle<R>) {
    let started = Instant::now();
    let manager = app.state::<BackendManager>();
    let status = manager.ensure_started();
    if !status.ready {
        write_shell_log(&format!(
            "shell menu smoke action copy-recent skipped ready=false message={}",
            status.message
        ));
        return;
    }
    match fetch_recent_transcripts(&manager.access()) {
        Ok(entries) => {
            let Some(entry) = entries.first() else {
                write_shell_log(&format!(
                    "shell menu smoke action copy-recent completed elapsedMs={} copied=false reason=empty",
                    started.elapsed().as_millis()
                ));
                return;
            };
            let copied = copy_recent_transcript_from_shell(app, &entry.id);
            write_shell_log(&format!(
                "shell menu smoke action copy-recent completed elapsedMs={} copied={} transcriptId={}",
                started.elapsed().as_millis(),
                copied,
                entry.id
            ));
        }
        Err(err) => write_shell_log(&format!(
            "shell menu smoke action copy-recent failed elapsedMs={} error={err}",
            started.elapsed().as_millis()
        )),
    }
}

fn run_shell_menu_smoke_hotkey_event<R: Runtime>(
    app: &AppHandle<R>,
    event_state: ShortcutState,
    label: &str,
) {
    let started = Instant::now();
    let status = match refresh_global_hotkey_for_app(app) {
        Ok(status) => status,
        Err(err) => {
            write_shell_log(&format!(
                "shell menu smoke action {label} completed elapsedMs={} mode=unknown path=none dispatched=false posted=false error=refresh_failed:{}",
                started.elapsed().as_millis(),
                sanitize_shell_log_token(&err)
            ));
            return;
        }
    };
    let path = app.try_state::<DesktopHotkeyState>().and_then(|state| {
        shortcut_id_for_hotkey(&status.hotkey).and_then(|shortcut_id| {
            state.action_for_event(shortcut_id, event_state, Instant::now())
        })
    });
    let Some(path) = path else {
        write_shell_log(&format!(
            "shell menu smoke action {label} completed elapsedMs={} mode={} path=none dispatched=false posted=false",
            started.elapsed().as_millis(),
            status.mode
        ));
        return;
    };

    let manager = app.state::<BackendManager>();
    let backend_status = manager.ensure_started();
    let (posted, error) = if backend_status.ready {
        match post_backend_path(&manager.access(), path) {
            Ok(_) => (true, String::new()),
            Err(err) => (false, sanitize_shell_log_token(&err)),
        }
    } else {
        (
            false,
            sanitize_shell_log_token(&format!("backend_not_ready:{}", backend_status.message)),
        )
    };
    write_shell_log(&format!(
        "shell menu smoke action {label} completed elapsedMs={} mode={} path={} dispatched=true posted={} error={}",
        started.elapsed().as_millis(),
        status.mode,
        path,
        posted,
        error
    ));
}

fn run_shell_menu_smoke_overlay_show(mode: &str) {
    let started = Instant::now();
    match native_overlay::handle_shell_command("overlayShow", &json!({ "mode": mode })) {
        Ok(status) => {
            let visible = status
                .get("visible")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let reported_mode = status
                .get("mode")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let available = status
                .get("available")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            write_shell_log(&format!(
                "shell menu smoke action overlay-{mode} completed elapsedMs={} mode={} visible={} available={}",
                started.elapsed().as_millis(),
                sanitize_shell_log_token(reported_mode),
                visible,
                available
            ));
        }
        Err(err) => write_shell_log(&format!(
            "shell menu smoke action overlay-{mode} failed elapsedMs={} error={}",
            started.elapsed().as_millis(),
            sanitize_shell_log_token(&err)
        )),
    }
}

fn run_shell_menu_smoke_overlay_hide() {
    let started = Instant::now();
    match native_overlay::handle_shell_command("overlayHide", &json!({})) {
        Ok(status) => {
            let visible = status
                .get("visible")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let mode = status
                .get("mode")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let available = status
                .get("available")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            write_shell_log(&format!(
                "shell menu smoke action overlay-hide completed elapsedMs={} mode={} visible={} available={}",
                started.elapsed().as_millis(),
                sanitize_shell_log_token(mode),
                visible,
                available
            ));
        }
        Err(err) => write_shell_log(&format!(
            "shell menu smoke action overlay-hide failed elapsedMs={} error={}",
            started.elapsed().as_millis(),
            sanitize_shell_log_token(&err)
        )),
    }
}

fn run_shell_menu_smoke_quit<R: Runtime>(app: &AppHandle<R>) {
    let started = Instant::now();
    let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("shellMenuSmokeQuit");
    write_shell_log(&format!(
        "shell menu smoke action quit completed elapsedMs={} stoppedSidecars={} exitRequested=true",
        started.elapsed().as_millis(),
        stopped
    ));
    app.exit(0);
}

fn restart_backend_from_shell<R: Runtime>(app: &AppHandle<R>) {
    let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("backendRestartMenu");
    if stopped > 0 {
        write_shell_log(&format!(
            "stopped {stopped} audio sidecar(s) before backend restart menu action"
        ));
    }
    let manager = app.state::<BackendManager>();
    match manager.restart() {
        Ok(status) => write_shell_log(&format!(
            "backend restart requested from shell menu; pid={:?} ready={} launch_kind={}",
            status.pid, status.ready, status.launch_kind
        )),
        Err(err) => write_shell_log(&format!("backend restart from shell menu failed: {err}")),
    }
    refresh_tray_menu_for_app(app, "backend restart");
}

fn is_shell_menu_item(item_id: &str) -> bool {
    matches!(
        item_id,
        MENU_ITEM_SHOW_WINDOW
            | MENU_ITEM_START_LIVE
            | MENU_ITEM_YOUTUBE
            | MENU_ITEM_FILE
            | MENU_ITEM_SETTINGS
            | MENU_ITEM_INSTALL_UPDATE
            | MENU_ITEM_RESTART_BACKEND
            | MENU_ITEM_REFRESH_RECENT
            | MENU_ITEM_QUIT
    ) || item_id.starts_with(MENU_ITEM_COPY_TRANSCRIPT_PREFIX)
}

fn refresh_tray_menu_for_app<R: Runtime>(app: &AppHandle<R>, reason: &str) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        write_shell_log(&format!(
            "tray menu refresh skipped ({reason}): tray not found"
        ));
        return;
    };
    if let Err(err) = tray.set_menu(None::<Menu<R>>) {
        write_shell_log(&format!("tray native menu clear failed ({reason}): {err}"));
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TrayIconKind {
    Normal,
    Update,
    Recording,
}

fn tray_icon_image(kind: TrayIconKind) -> Image<'static> {
    let bytes: &'static [u8] = match kind {
        TrayIconKind::Normal => include_bytes!("../icons/tray-normal.rgba"),
        TrayIconKind::Update => include_bytes!("../icons/tray-update.rgba"),
        TrayIconKind::Recording => include_bytes!("../icons/tray-recording.rgba"),
    };
    Image::new(bytes, 32, 32)
}

fn desktop_window_icon_image() -> Image<'static> {
    Image::new(include_bytes!("../icons/window-icon.rgba"), 64, 64)
}

fn apply_desktop_window_icon<R: Runtime>(app: &AppHandle<R>) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) else {
        write_shell_log("desktop window icon skipped: main window not found");
        return;
    };
    if let Err(err) = window.set_icon(desktop_window_icon_image()) {
        write_shell_log(&format!("desktop window icon update failed: {err}"));
    }
}

fn tray_icon_kind(status: &TrayStatus) -> TrayIconKind {
    if status.recording_active {
        return TrayIconKind::Recording;
    }
    if status.update_available || status.update_installing {
        return TrayIconKind::Update;
    }
    TrayIconKind::Normal
}

fn tray_tooltip(status: &TrayStatus) -> String {
    if status.recording_active {
        return "Scriber is recording".to_string();
    }
    if status.update_installing {
        return "Scriber is installing an update".to_string();
    }
    if status.update_available {
        if let Some(version) = status.update_version.as_deref() {
            if !version.trim().is_empty() {
                return format!("Scriber {version} is ready to install");
            }
        }
        return "Scriber update is ready to install".to_string();
    }
    "Scriber".to_string()
}

fn tray_status_for_app<R: Runtime>(app: &AppHandle<R>) -> TrayStatus {
    let Some(state) = app.try_state::<TrayState>() else {
        return TrayStatus::from(&TrayStatusInner::default());
    };
    let state = state.inner.lock().unwrap();
    TrayStatus::from(&*state)
}

fn update_tray_status_for_app<R: Runtime>(
    app: &AppHandle<R>,
    update: impl FnOnce(&mut TrayStatusInner),
) -> TrayStatus {
    let status = {
        let state = app.state::<TrayState>();
        let mut inner = state.inner.lock().unwrap();
        update(&mut inner);
        TrayStatus::from(&*inner)
    };
    refresh_tray_visuals_for_app(app, &status);
    refresh_tray_menu_for_app(app, "tray status");
    emit_tray_status_for_app(app, &status);
    status
}

fn refresh_tray_visuals_for_app<R: Runtime>(app: &AppHandle<R>, status: &TrayStatus) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return;
    };
    if let Err(err) = tray.set_icon(Some(tray_icon_image(tray_icon_kind(status)))) {
        write_shell_log(&format!("tray icon refresh failed: {err}"));
    }
    let tooltip = tray_tooltip(status);
    if let Err(err) = tray.set_tooltip(Some(&tooltip)) {
        write_shell_log(&format!("tray tooltip refresh failed: {err}"));
    }
}

fn emit_tray_status_for_app<R: Runtime>(app: &AppHandle<R>, status: &TrayStatus) {
    let _ = app.emit(TRAY_STATUS_EVENT, status.clone());
    let _ = app.emit_to(TRAY_PANEL_LABEL, TRAY_STATUS_EVENT, status.clone());
}

fn show_tray_panel_for_app<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let window = if let Some(window) = app.get_webview_window(TRAY_PANEL_LABEL) {
        window
    } else {
        WebviewWindowBuilder::new(
            app,
            TRAY_PANEL_LABEL,
            WebviewUrl::App("index.html?tray=1".into()),
        )
        .title("Scriber Tray")
        .inner_size(TRAY_PANEL_WIDTH, TRAY_PANEL_HEIGHT)
        .min_inner_size(TRAY_PANEL_WIDTH, TRAY_PANEL_HEIGHT)
        .max_inner_size(TRAY_PANEL_WIDTH, TRAY_PANEL_HEIGHT)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .build()
        .map_err(|err| format!("tray panel create failed: {err}"))?
    };

    position_tray_panel_window(&window)
        .map_err(|err| format!("tray panel position failed: {err}"))?;
    window
        .show()
        .map_err(|err| format!("tray panel show failed: {err}"))?;
    window
        .set_focus()
        .map_err(|err| format!("tray panel focus failed: {err}"))?;
    emit_tray_status_for_app(app, &tray_status_for_app(app));
    Ok(())
}

fn hide_tray_panel_for_app<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(TRAY_PANEL_LABEL) {
        window
            .hide()
            .map_err(|err| format!("tray panel hide failed: {err}"))?;
    }
    Ok(())
}

fn position_tray_panel_window<R: Runtime>(window: &WebviewWindow<R>) -> tauri::Result<()> {
    let monitor = window
        .current_monitor()?
        .or(window.primary_monitor()?)
        .or_else(|| {
            window
                .available_monitors()
                .ok()
                .and_then(|monitors| monitors.into_iter().next())
        });
    if let Some(monitor) = monitor {
        let work_area = monitor.work_area();
        let scale = monitor.scale_factor().max(0.25);
        let (x, y) = tray_panel_position_for_work_area(
            f64::from(work_area.position.x) / scale,
            f64::from(work_area.position.y) / scale,
            f64::from(work_area.size.width) / scale,
            f64::from(work_area.size.height) / scale,
            TRAY_PANEL_WIDTH,
            TRAY_PANEL_HEIGHT,
            TRAY_PANEL_MARGIN,
        );
        window.set_position(LogicalPosition::new(x, y))?;
    }
    Ok(())
}

fn tray_panel_position_for_work_area(
    work_x: f64,
    work_y: f64,
    work_width: f64,
    work_height: f64,
    panel_width: f64,
    panel_height: f64,
    margin: f64,
) -> (f64, f64) {
    let x = work_x + (work_width - panel_width - margin).max(0.0);
    let y = work_y + (work_height - panel_height - margin).max(0.0);
    (x.round(), y.round())
}

fn show_main_window_path<R: Runtime>(app: &AppHandle<R>, path: &str) -> Result<(), String> {
    show_main_window(app);
    app.emit_to(
        MAIN_WINDOW_LABEL,
        TRAY_NAVIGATE_EVENT,
        json!({ "path": path }),
    )
    .map_err(|err| format!("main window navigation event failed: {err}"))
}

fn handle_tray_action<R: Runtime>(app: &AppHandle<R>, action: &str) -> Result<(), String> {
    match action.trim() {
        "toggle_live" | "start_live" => toggle_live_recording_from_shell(app),
        "open_youtube" => {
            show_main_window_path(app, "/youtube")?;
            hide_tray_panel_for_app(app)
        }
        "open_file" => {
            show_main_window_path(app, "/file")?;
            hide_tray_panel_for_app(app)
        }
        "open_settings" => {
            show_main_window_path(app, "/settings")?;
            hide_tray_panel_for_app(app)
        }
        "open_recent" | "show_window" => {
            show_main_window(app);
            hide_tray_panel_for_app(app)
        }
        "restart_backend" => {
            restart_backend_from_shell(app);
            hide_tray_panel_for_app(app)
        }
        "restart_app" => {
            let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("shellRestartApp");
            if stopped > 0 {
                write_shell_log(&format!(
                    "stopped {stopped} audio sidecar(s) before shell app restart"
                ));
            }
            write_shell_log("application restart requested from tray panel");
            app.request_restart();
            Ok(())
        }
        "quit" => {
            let stopped = audio_sidecar_client::shutdown_all_audio_sidecars("shellQuit");
            if stopped > 0 {
                write_shell_log(&format!(
                    "stopped {stopped} audio sidecar(s) before shell quit"
                ));
            }
            app.exit(0);
            Ok(())
        }
        "hide" => hide_tray_panel_for_app(app),
        copy_action if copy_action.starts_with("copy_transcript:") => {
            let transcript_id = copy_action
                .strip_prefix("copy_transcript:")
                .unwrap_or_default()
                .trim();
            if copy_recent_transcript_from_shell(app, transcript_id) {
                Ok(())
            } else {
                Err("could not copy transcript".to_string())
            }
        }
        other => Err(format!("unsupported tray action: {other}")),
    }
}

fn toggle_live_recording_from_shell<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let manager = app.state::<BackendManager>();
    let backend_status = manager.ensure_started();
    if !backend_status.ready {
        return Err(format!(
            "backend is not ready for live recording: {}",
            backend_status.message
        ));
    }

    let current = tray_status_for_app(app);
    let (path, active, mode) = if current.recording_active {
        ("/api/live-mic/stop", false, "transcribing")
    } else {
        ("/api/live-mic/start", true, "initializing")
    };
    request_backend_json(&manager.access(), "POST", path)?;
    update_tray_status_for_app(app, |state| {
        state.recording_active = active;
        state.recording_mode = mode.to_string();
    });
    Ok(())
}

fn normalize_tray_recording_mode(raw: Option<&str>, active: bool) -> String {
    let mode = raw.unwrap_or_default().trim().to_ascii_lowercase();
    match mode.as_str() {
        "initializing" | "recording" | "transcribing" | "idle" | "hidden" => mode,
        _ if active => "recording".to_string(),
        _ => "idle".to_string(),
    }
}

fn sanitize_update_field(value: &str, max_chars: usize) -> String {
    let collapsed = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if max_chars == 0 || collapsed.chars().count() <= max_chars {
        return collapsed;
    }
    if max_chars <= 3 {
        return ".".repeat(max_chars);
    }
    let mut truncated = collapsed
        .chars()
        .take(max_chars.saturating_sub(3))
        .collect::<String>();
    truncated.push_str("...");
    truncated
}

fn fetch_recent_transcripts(
    access: &BackendAccess,
) -> Result<Vec<RecentTranscriptMenuEntry>, String> {
    let value = request_backend_json(access, "GET", "/api/transcripts?limit=20&offset=0")?;
    recent_transcripts_from_value(&value)
}

fn recent_transcripts_from_value(value: &Value) -> Result<Vec<RecentTranscriptMenuEntry>, String> {
    let items = value
        .get("items")
        .and_then(Value::as_array)
        .ok_or_else(|| "backend transcript list did not include items".to_string())?;
    let mut entries = Vec::new();

    for item in items {
        let status = value_string(item, "status");
        if !status.eq_ignore_ascii_case("completed") {
            continue;
        }
        let id = value_string(item, "id");
        if id.is_empty() || !is_safe_transcript_id(&id) {
            continue;
        }
        entries.push(RecentTranscriptMenuEntry {
            id,
            title: value_string(item, "title"),
            date: value_string(item, "date"),
            transcript_type: value_string(item, "type"),
        });
        if entries.len() >= TRAY_RECENT_TRANSCRIPT_LIMIT {
            break;
        }
    }

    Ok(entries)
}

fn copy_recent_transcript_from_shell<R: Runtime>(app: &AppHandle<R>, transcript_id: &str) -> bool {
    if !is_safe_transcript_id(transcript_id) {
        write_shell_log("recent transcript copy skipped: invalid transcript id");
        return false;
    }
    let manager = app.state::<BackendManager>();
    let status = manager.ensure_started();
    if !status.ready {
        write_shell_log(&format!(
            "recent transcript copy skipped because backend is not ready: {}",
            status.message
        ));
        return false;
    }
    let path = format!("/api/transcripts/{transcript_id}");
    let value = match request_backend_json(&manager.access(), "GET", &path) {
        Ok(value) => value,
        Err(err) => {
            write_shell_log(&format!("recent transcript copy fetch failed: {err}"));
            return false;
        }
    };
    let content = value_string(&value, "content");
    if content.trim().is_empty() {
        write_shell_log("recent transcript copy skipped: transcript content is empty");
        return false;
    }
    match copy_text_to_clipboard(&content) {
        Ok(()) => {
            write_shell_log(&format!(
                "recent transcript copied to clipboard: {transcript_id}"
            ));
            true
        }
        Err(err) => {
            write_shell_log(&format!("recent transcript clipboard copy failed: {err}"));
            false
        }
    }
}

fn value_string(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn is_safe_transcript_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

#[allow(dead_code)]
fn recent_transcript_label(entry: &RecentTranscriptMenuEntry) -> String {
    let kind = match entry.transcript_type.as_str() {
        "youtube" => "YouTube",
        "file" => "File",
        "mic" => "Mic",
        _ => "Transcript",
    };
    let title = sanitize_menu_label(&entry.title, "Untitled transcript", 48);
    let date = sanitize_menu_label(&entry.date, "", 22);
    if date.is_empty() {
        format!("{kind}: {title}")
    } else {
        format!("{kind}: {title} ({date})")
    }
}

#[allow(dead_code)]
fn sanitize_menu_label(value: &str, fallback: &str, max_chars: usize) -> String {
    let mut collapsed = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        collapsed = fallback.to_string();
    }
    if max_chars == 0 {
        return String::new();
    }
    if collapsed.chars().count() <= max_chars {
        return collapsed;
    }
    if max_chars <= 3 {
        return ".".repeat(max_chars);
    }
    let mut truncated = collapsed
        .chars()
        .take(max_chars.saturating_sub(3))
        .collect::<String>();
    truncated.push_str("...");
    truncated
}

fn sanitize_shell_log_token(value: &str) -> String {
    value
        .chars()
        .take(180)
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | ':' | '/' | '=') {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

#[cfg(windows)]
fn copy_text_to_clipboard(text: &str) -> Result<(), String> {
    let wide: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
    let byte_len = wide.len() * std::mem::size_of::<u16>();
    unsafe {
        let handle = GlobalAlloc(GMEM_MOVEABLE, byte_len);
        if handle.is_null() {
            return Err("could not allocate clipboard memory".to_string());
        }
        let locked = GlobalLock(handle) as *mut u16;
        if locked.is_null() {
            let _ = GlobalFree(handle);
            return Err("could not lock clipboard memory".to_string());
        }
        std::ptr::copy_nonoverlapping(wide.as_ptr(), locked, wide.len());
        let _ = GlobalUnlock(handle);

        if OpenClipboard(std::ptr::null_mut()) == 0 {
            let _ = GlobalFree(handle);
            return Err("could not open clipboard".to_string());
        }
        if EmptyClipboard() == 0 {
            let _ = CloseClipboard();
            let _ = GlobalFree(handle);
            return Err("could not empty clipboard".to_string());
        }
        if SetClipboardData(CF_UNICODETEXT as u32, handle).is_null() {
            let _ = CloseClipboard();
            let _ = GlobalFree(handle);
            return Err("could not set clipboard data".to_string());
        }
        let _ = CloseClipboard();
    }
    Ok(())
}

#[cfg(not(windows))]
fn copy_text_to_clipboard(_text: &str) -> Result<(), String> {
    Err("clipboard copy is only implemented on Windows".to_string())
}

fn should_show_tray_panel_for_event(event: &TrayIconEvent) -> bool {
    match event {
        TrayIconEvent::Click {
            button,
            button_state,
            ..
        } => should_show_tray_panel_for_click(*button, Some(*button_state)),
        TrayIconEvent::DoubleClick { button, .. } => {
            should_show_tray_panel_for_click(*button, None)
        }
        _ => false,
    }
}

fn should_show_tray_panel_for_click(
    button: MouseButton,
    button_state: Option<MouseButtonState>,
) -> bool {
    matches!(button, MouseButton::Left | MouseButton::Right)
        && button_state
            .map(|state| state == MouseButtonState::Up)
            .unwrap_or(true)
}

#[cfg(test)]
fn should_show_window_for_tray_click(
    button: MouseButton,
    button_state: Option<MouseButtonState>,
) -> bool {
    should_show_tray_panel_for_click(button, button_state)
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
    match spawn_backend(
        port,
        state.resource_dir.as_deref(),
        &state.session_token,
        state.shell_ipc_config.as_ref(),
    ) {
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
    // A freshly spawned backend is never immediately ready; readiness is confirmed on
    // the next supervisor tick, outside the lock, via health_ready().
    status_from_state(state, false)
}

fn managed_backend_start_timed_out(started_at: Option<Instant>, now: Instant) -> bool {
    started_at
        .map(|started_at| now.duration_since(started_at) >= backend_start_timeout())
        .unwrap_or(false)
}

fn backend_start_timeout() -> Duration {
    env::var(BACKEND_START_TIMEOUT_ENV)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .map(Duration::from_millis)
        .unwrap_or(BACKEND_START_TIMEOUT)
}

fn env_duration_ms(
    name: &str,
    default: Duration,
    minimum: Duration,
    maximum: Duration,
) -> Duration {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(Duration::from_millis)
        .map(|duration| duration.clamp(minimum, maximum))
        .unwrap_or(default)
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

fn shell_ipc_env_pairs(
    shell_ipc_config: Option<&shell_ipc::ShellIpcConfig>,
) -> Vec<(&'static str, String)> {
    shell_ipc_config
        .map(|config| {
            vec![
                (SHELL_IPC_PIPE_ENV, config.pipe_name.clone()),
                (SHELL_IPC_TOKEN_ENV, config.token.clone()),
                (SHELL_IPC_API_VERSION_ENV, "1".to_string()),
            ]
        })
        .unwrap_or_default()
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

fn refresh_global_hotkey_for_app<R: Runtime>(
    app: &AppHandle<R>,
) -> Result<DesktopHotkeyStatus, String> {
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

    if hotkey_state.is_capture_suspended() {
        let _ = app.global_shortcut().unregister_all();
        hotkey_state.set_capture_suspended(
            true,
            "Global hotkey suspended while recording a new shortcut".to_string(),
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

    if hotkey_state.is_registered_config(
        &config.hotkey,
        &config.post_processing_hotkey,
        config.post_processing_enabled,
        &config.mode,
    ) {
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
    let post_hotkey_registered = config.post_processing_enabled
        && !config.post_processing_hotkey.is_empty()
        && config.post_processing_hotkey != config.hotkey;
    if post_hotkey_registered {
        app.global_shortcut()
            .register(config.post_processing_hotkey.as_str())
            .map_err(|err| {
                format!(
                    "Could not register post-processing hotkey '{}': {err}",
                    config.post_processing_hotkey
                )
            })?;
    }

    let message = format!(
        "Global hotkey registered: {} ({}){}",
        config.hotkey,
        config.mode,
        if post_hotkey_registered {
            format!(", post-processing: {}", config.post_processing_hotkey)
        } else {
            String::new()
        }
    );
    write_shell_log(&message);
    hotkey_state.set_registered(
        config.hotkey,
        if post_hotkey_registered {
            config.post_processing_hotkey
        } else {
            String::new()
        },
        post_hotkey_registered,
        config.mode,
        message,
    );
    Ok(hotkey_state.status())
}

fn handle_global_shortcut_event(app: &AppHandle, shortcut: &Shortcut, event_state: ShortcutState) {
    let Some(path) = app
        .try_state::<DesktopHotkeyState>()
        .and_then(|state| state.action_for_event(shortcut.id(), event_state, Instant::now()))
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

fn start_backend_supervisor(app: AppHandle) {
    std::thread::spawn(move || {
        let mut hotkey_refreshed_after_ready = false;
        let mut tray_refreshed_after_ready = false;
        loop {
            let Some(manager) = app.try_state::<BackendManager>() else {
                break;
            };
            let status = manager.ensure_started();
            if should_refresh_hotkey_after_backend_ready(status.ready, hotkey_refreshed_after_ready)
            {
                match refresh_global_hotkey_for_app(&app) {
                    Ok(_) => {
                        hotkey_refreshed_after_ready = true;
                    }
                    Err(err) => {
                        hotkey_refreshed_after_ready = true;
                        write_shell_log(&format!(
                            "global hotkey registration after backend ready skipped: {err}"
                        ));
                    }
                }
            }
            if status.ready && !tray_refreshed_after_ready {
                refresh_tray_menu_for_app(&app, "backend ready");
                tray_refreshed_after_ready = true;
            } else if !status.ready {
                hotkey_refreshed_after_ready = false;
                tray_refreshed_after_ready = false;
            }
            std::thread::sleep(BACKEND_SUPERVISOR_INTERVAL);
        }
    });
}

fn start_native_device_event_monitor_for_app(
    app: AppHandle,
) -> Option<audio_devices::NativeDeviceEventMonitorHandle> {
    let raw_mode = env::var("SCRIBER_NATIVE_DEVICE_EVENTS").ok();
    let mode = audio_devices::native_device_events_mode_from_env(raw_mode.as_deref());
    let post_hints = mode == audio_devices::NativeDeviceEventsMode::Enabled;
    let app_for_event = app.clone();
    let mut observe_only_log_state = NativeDeviceObserveOnlyLogState::default();
    let on_event = move |event: audio_devices::NativeDeviceEvent| {
        if !post_hints {
            if let Some(message) = observe_only_log_state.maybe_summary(&event, Instant::now()) {
                write_shell_log(&message);
            }
            return;
        }

        let access = app_for_event.state::<BackendManager>().access();
        let body = event.to_backend_hint_body();
        match request_backend_json_with_body(
            &access,
            "POST",
            "/api/microphones/refresh",
            Some(&body),
        ) {
            Ok(_) => {
                audio_devices::record_native_device_event_post_result(&event, true, None);
                write_shell_log(&format!(
                    "native device event posted kind={} flow={} role={} endpoint_hash={}",
                    event.event_kind, event.flow, event.role, event.endpoint_id_hash
                ));
            }
            Err(err) => {
                audio_devices::record_native_device_event_post_result(
                    &event,
                    false,
                    Some(err.to_string()),
                );
                write_shell_log(&format!(
                    "native device event post failed kind={} flow={} error={err}",
                    event.event_kind, event.flow
                ));
            }
        }
    };
    let log = |message: String| write_shell_log(&message);
    match audio_devices::start_native_device_event_monitor(mode, on_event, log) {
        Ok(handle) => handle,
        Err(err) => {
            write_shell_log(&format!("native device event monitor unavailable: {err}"));
            None
        }
    }
}

fn should_refresh_hotkey_after_backend_ready(
    backend_ready: bool,
    hotkey_refreshed_after_ready: bool,
) -> bool {
    backend_ready && !hotkey_refreshed_after_ready
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

fn shortcut_id_for_hotkey(hotkey: &str) -> Option<u32> {
    hotkey
        .parse::<Shortcut>()
        .ok()
        .map(|shortcut| shortcut.id())
}

struct BackendHotkeyConfig {
    hotkey: String,
    post_processing_hotkey: String,
    post_processing_enabled: bool,
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
    let raw_post_processing_hotkey = value
        .get("postProcessingHotkeyRaw")
        .or_else(|| value.get("postProcessingHotkey"))
        .and_then(Value::as_str)
        .unwrap_or("ctrl+shift+p");
    let post_processing_enabled = value
        .get("postProcessingEnabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);

    Ok(BackendHotkeyConfig {
        hotkey: normalize_global_shortcut(raw_hotkey),
        post_processing_hotkey: normalize_global_shortcut(raw_post_processing_hotkey),
        post_processing_enabled,
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
    request_backend_json_with_body(access, method, path, None)
}

fn request_backend_json_with_body(
    access: &BackendAccess,
    method: &str,
    path: &str,
    body: Option<&Value>,
) -> Result<Value, String> {
    let (host, port) = parse_loopback_backend_url(&access.base_url)?;
    let addr = SocketAddr::from((host, port));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|err| format!("could not connect to backend: {err}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));

    let body_text = body.map(Value::to_string).unwrap_or_default();
    let request = build_backend_http_request(method, path, port, &access.session_token, &body_text);
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

fn build_backend_http_request(
    method: &str,
    path: &str,
    port: u16,
    session_token: &str,
    body: &str,
) -> String {
    let token_header = if session_token.is_empty() {
        String::new()
    } else {
        format!("X-Scriber-Token: {session_token}\r\n")
    };
    let content_type_header = if body.is_empty() {
        String::new()
    } else {
        "Content-Type: application/json\r\n".to_string()
    };
    format!(
        "{method} {path} HTTP/1.1\r\nHost: {DEFAULT_HOST}:{port}\r\n{token_header}{content_type_header}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.as_bytes().len()
    )
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
    shell_ipc_config: Option<&shell_ipc::ShellIpcConfig>,
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
    for (name, value) in shell_ipc_env_pairs(shell_ipc_config) {
        command.env(name, value);
    }
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
            if !is_allowed_backend_executable_name(&path) {
                return Err(format!(
                    "SCRIBER_BACKEND_EXE must point to a Scriber backend sidecar executable named one of: {}",
                    backend_executable_names().join(", ")
                ));
            }
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

fn is_allowed_backend_executable_name(path: &Path) -> bool {
    let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    backend_executable_names()
        .iter()
        .any(|allowed| file_name.eq_ignore_ascii_case(allowed))
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
        backend_executable_names, backend_start_timeout, build_backend_http_request,
        desktop_autostart_default_enabled, env_duration_ms, env_flag_enabled,
        find_backend_executable, find_backend_executable_in_dirs, health_response_ready,
        is_safe_transcript_id, is_shell_menu_item, managed_backend_start_timed_out,
        normalize_global_shortcut, normalize_hotkey_mode, parse_loopback_backend_url,
        parse_shell_menu_smoke_actions, recent_transcript_label, recent_transcripts_from_value,
        resolve_session_token, sanitize_menu_label, shell_ipc, shell_ipc_env_pairs,
        shortcut_id_for_hotkey, should_refresh_hotkey_after_backend_ready,
        should_show_window_for_tray_click, split_http_response, DesktopHotkeyState,
        NativeDeviceObserveOnlyLogState, RecentTranscriptMenuEntry, ShellMenuSmokeAction,
        AUTOSTART_DEFAULT_ENV, BACKEND_START_TIMEOUT, BACKEND_START_TIMEOUT_ENV,
        HOTKEY_DISPATCH_DEBOUNCE, MENU_ITEM_COPY_TRANSCRIPT_PREFIX, MENU_ITEM_QUIT,
        MENU_ITEM_REFRESH_RECENT, MENU_ITEM_RESTART_BACKEND, MENU_ITEM_SHOW_WINDOW,
        NATIVE_DEVICE_OBSERVE_ONLY_LOG_EVERY_EVENTS, NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL,
        SESSION_TOKEN_ENV, SHELL_IPC_API_VERSION_ENV, SHELL_IPC_PIPE_ENV, SHELL_IPC_TOKEN_ENV,
        TRAY_RECENT_TRANSCRIPT_LIMIT,
    };
    use std::{
        fs,
        path::PathBuf,
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };
    use tauri::tray::{MouseButton, MouseButtonState};
    use tauri_plugin_global_shortcut::ShortcutState;

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
    fn hotkey_registration_retries_once_after_backend_ready() {
        assert!(!should_refresh_hotkey_after_backend_ready(false, false));
        assert!(!should_refresh_hotkey_after_backend_ready(true, true));
        assert!(should_refresh_hotkey_after_backend_ready(true, false));
    }

    #[test]
    fn desktop_window_chrome_theme_parser_accepts_light_and_dark() {
        assert_eq!(
            super::DesktopWindowChromeTheme::parse("light").unwrap(),
            super::DesktopWindowChromeTheme::Light
        );
        assert_eq!(
            super::DesktopWindowChromeTheme::parse(" DARK ").unwrap(),
            super::DesktopWindowChromeTheme::Dark
        );
        assert!(super::DesktopWindowChromeTheme::parse("sepia").is_err());
    }

    #[test]
    fn window_visibility_accepts_window_with_enough_visible_area() {
        assert!(super::physical_rect_has_min_visible_area(
            1800, 900, 900, 700, 0, 0, 1920, 1080, 96
        ));
        assert!(super::physical_rect_has_min_visible_area(
            0, 0, 900, 700, 0, 0, 1920, 1080, 96
        ));
    }

    #[test]
    fn window_visibility_rejects_offscreen_or_tiny_slivers() {
        assert!(!super::physical_rect_has_min_visible_area(
            2200, 100, 900, 700, 0, 0, 1920, 1080, 96
        ));
        assert!(!super::physical_rect_has_min_visible_area(
            1900, 100, 900, 700, 0, 0, 1920, 1080, 96
        ));
    }

    #[cfg(windows)]
    #[test]
    fn rgb_to_colorref_uses_windows_bgr_order() {
        assert_eq!(super::rgb_to_colorref(0x11, 0x22, 0x33), 0x00332211);
    }

    #[cfg(windows)]
    #[test]
    fn light_window_chrome_matches_light_app_shell() {
        let (caption_color, _text_color, border_color) =
            super::desktop_window_chrome_colors(super::DesktopWindowChromeTheme::Light);
        let app_shell_light = super::rgb_to_colorref(0xe5, 0xe7, 0xeb);
        assert_eq!(caption_color, app_shell_light);
        assert_eq!(border_color, app_shell_light);
    }

    #[test]
    fn backend_start_timeout_can_be_overridden_for_smoke_tests() {
        let previous = std::env::var(BACKEND_START_TIMEOUT_ENV).ok();
        std::env::set_var(BACKEND_START_TIMEOUT_ENV, "1250");

        assert_eq!(backend_start_timeout(), Duration::from_millis(1250));

        match previous {
            Some(value) => std::env::set_var(BACKEND_START_TIMEOUT_ENV, value),
            None => std::env::remove_var(BACKEND_START_TIMEOUT_ENV),
        }
    }

    #[test]
    fn env_duration_ms_clamps_smoke_values() {
        const TEST_ENV: &str = "SCRIBER_TEST_DURATION_MS";
        let previous = std::env::var(TEST_ENV).ok();

        std::env::set_var(TEST_ENV, "50");
        assert_eq!(
            env_duration_ms(
                TEST_ENV,
                Duration::from_millis(500),
                Duration::from_millis(100),
                Duration::from_millis(1000),
            ),
            Duration::from_millis(100)
        );

        std::env::set_var(TEST_ENV, "1500");
        assert_eq!(
            env_duration_ms(
                TEST_ENV,
                Duration::from_millis(500),
                Duration::from_millis(100),
                Duration::from_millis(1000),
            ),
            Duration::from_millis(1000)
        );

        match previous {
            Some(value) => std::env::set_var(TEST_ENV, value),
            None => std::env::remove_var(TEST_ENV),
        }
    }

    #[test]
    fn shell_menu_smoke_actions_accept_only_safe_smoke_commands() {
        assert_eq!(
            parse_shell_menu_smoke_actions(
                "show-window, unknown; copy-recent hotkey-press hotkey-release overlay-initializing overlay-recording overlay-transcribing overlay-hide quit open",
            ),
            vec![
                ShellMenuSmokeAction::ShowWindow,
                ShellMenuSmokeAction::CopyRecent,
                ShellMenuSmokeAction::HotkeyPress,
                ShellMenuSmokeAction::HotkeyRelease,
                ShellMenuSmokeAction::OverlayInitializing,
                ShellMenuSmokeAction::OverlayRecording,
                ShellMenuSmokeAction::OverlayTranscribing,
                ShellMenuSmokeAction::OverlayHide,
                ShellMenuSmokeAction::Quit,
                ShellMenuSmokeAction::ShowWindow,
            ]
        );
        assert!(parse_shell_menu_smoke_actions("copy-transcript secret").is_empty());
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
    fn backend_executable_override_rejects_unapproved_name() {
        let previous = std::env::var("SCRIBER_BACKEND_EXE").ok();
        let dir = unique_test_dir("sidecar-override");
        fs::create_dir_all(&dir).unwrap();
        let executable = dir.join("not-scriber-backend.exe");
        fs::write(&executable, b"test").unwrap();
        std::env::set_var("SCRIBER_BACKEND_EXE", &executable);

        let result = find_backend_executable(None);

        assert!(matches!(
            result,
            Err(message) if message.contains("must point to a Scriber backend sidecar executable")
        ));
        match previous {
            Some(value) => std::env::set_var("SCRIBER_BACKEND_EXE", value),
            None => std::env::remove_var("SCRIBER_BACKEND_EXE"),
        }
        let _ = fs::remove_dir_all(dir);
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
    fn desktop_autostart_default_is_enabled_unless_disabled_by_environment() {
        let previous = std::env::var(AUTOSTART_DEFAULT_ENV).ok();

        std::env::remove_var(AUTOSTART_DEFAULT_ENV);
        assert!(desktop_autostart_default_enabled());

        std::env::set_var(AUTOSTART_DEFAULT_ENV, "0");
        assert!(!desktop_autostart_default_enabled());

        std::env::set_var(AUTOSTART_DEFAULT_ENV, "false");
        assert!(!desktop_autostart_default_enabled());

        std::env::set_var(AUTOSTART_DEFAULT_ENV, "1");
        assert!(desktop_autostart_default_enabled());

        match previous {
            Some(value) => std::env::set_var(AUTOSTART_DEFAULT_ENV, value),
            None => std::env::remove_var(AUTOSTART_DEFAULT_ENV),
        }
    }

    #[test]
    fn env_flag_enabled_accepts_common_negative_values() {
        for value in ["0", "false", "no", "off", "disabled"] {
            assert!(!env_flag_enabled(value));
        }
        for value in ["", "1", "true", "yes", "enabled"] {
            assert!(env_flag_enabled(value));
        }
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
    fn desktop_hotkey_toggle_dispatches_backend_toggle_only_on_press() {
        let state = DesktopHotkeyState::new();
        let primary_id = shortcut_id_for_hotkey("ctrl+alt+s").unwrap();
        state.set_registered(
            "ctrl+alt+s".to_string(),
            String::new(),
            false,
            "toggle".to_string(),
            "registered".to_string(),
        );
        let now = Instant::now();

        assert_eq!(
            state.action_for_event(primary_id, ShortcutState::Pressed, now),
            Some("/api/live-mic/toggle")
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Pressed,
                now + HOTKEY_DISPATCH_DEBOUNCE - Duration::from_millis(1)
            ),
            None
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Released,
                now + HOTKEY_DISPATCH_DEBOUNCE
            ),
            None
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Pressed,
                now + HOTKEY_DISPATCH_DEBOUNCE + Duration::from_millis(1)
            ),
            Some("/api/live-mic/toggle")
        );
    }

    #[test]
    fn desktop_hotkey_capture_suspension_blocks_dispatch() {
        let state = DesktopHotkeyState::new();
        let primary_id = shortcut_id_for_hotkey("ctrl+alt+s").unwrap();
        state.set_registered(
            "ctrl+alt+s".to_string(),
            String::new(),
            false,
            "toggle".to_string(),
            "registered".to_string(),
        );
        let now = Instant::now();

        state.set_capture_suspended(true, "suspended".to_string());
        let suspended_status = state.status();
        assert!(suspended_status.capture_suspended);
        assert!(!suspended_status.registered);
        assert_eq!(
            state.action_for_event(primary_id, ShortcutState::Pressed, now),
            None
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Released,
                now + Duration::from_millis(25)
            ),
            None
        );

        state.set_capture_suspended(false, "resuming".to_string());
        state.set_registered(
            "ctrl+alt+s".to_string(),
            String::new(),
            false,
            "toggle".to_string(),
            "registered".to_string(),
        );
        assert!(!state.status().capture_suspended);
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Pressed,
                now + HOTKEY_DISPATCH_DEBOUNCE + Duration::from_millis(1)
            ),
            Some("/api/live-mic/toggle")
        );
    }

    #[test]
    fn desktop_hotkey_state_detects_already_registered_config() {
        let state = DesktopHotkeyState::new();
        assert!(!state.is_registered_config("ctrl+alt+s", "", false, "toggle"));

        state.set_registered(
            "ctrl+alt+s".to_string(),
            "ctrl+shift+p".to_string(),
            true,
            "toggle".to_string(),
            "registered".to_string(),
        );
        assert!(state.is_registered_config("ctrl+alt+s", "ctrl+shift+p", true, "toggle"));
        assert!(!state.is_registered_config("ctrl+shift+s", "ctrl+shift+p", true, "toggle"));
        assert!(!state.is_registered_config("ctrl+alt+s", "ctrl+shift+x", true, "toggle"));
        assert!(!state.is_registered_config("ctrl+alt+s", "ctrl+shift+p", true, "push_to_talk"));

        state.set_capture_suspended(true, "suspended".to_string());
        assert!(!state.is_registered_config("ctrl+alt+s", "ctrl+shift+p", true, "toggle"));
    }

    #[test]
    fn desktop_hotkey_push_to_talk_maps_press_and_release_to_backend_endpoints() {
        let state = DesktopHotkeyState::new();
        let primary_id = shortcut_id_for_hotkey("ctrl+alt+s").unwrap();
        state.set_registered(
            "ctrl+alt+s".to_string(),
            String::new(),
            false,
            "push_to_talk".to_string(),
            "registered".to_string(),
        );
        let now = Instant::now();

        assert_eq!(
            state.action_for_event(primary_id, ShortcutState::Pressed, now),
            Some("/api/live-mic/start")
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Released,
                now + Duration::from_millis(25)
            ),
            Some("/api/live-mic/stop")
        );
    }

    #[test]
    fn desktop_hotkey_post_processing_maps_to_dedicated_toggle() {
        let state = DesktopHotkeyState::new();
        let primary_id = shortcut_id_for_hotkey("ctrl+alt+s").unwrap();
        let post_id = shortcut_id_for_hotkey("ctrl+shift+p").unwrap();
        state.set_registered(
            "ctrl+alt+s".to_string(),
            "ctrl+shift+p".to_string(),
            true,
            "push_to_talk".to_string(),
            "registered".to_string(),
        );
        let now = Instant::now();

        assert_eq!(
            state.action_for_event(post_id, ShortcutState::Pressed, now),
            Some("/api/live-mic/toggle-post-processing")
        );
        assert_eq!(
            state.action_for_event(
                post_id,
                ShortcutState::Released,
                now + Duration::from_millis(25)
            ),
            None
        );
        assert_eq!(
            state.action_for_event(
                primary_id,
                ShortcutState::Pressed,
                now + HOTKEY_DISPATCH_DEBOUNCE + Duration::from_millis(1),
            ),
            Some("/api/live-mic/start")
        );
    }

    #[test]
    fn desktop_hotkey_primary_wins_when_post_processing_hotkey_matches_primary() {
        let state = DesktopHotkeyState::new();
        let primary_id = shortcut_id_for_hotkey("ctrl+alt+s").unwrap();
        state.set_registered(
            "ctrl+alt+s".to_string(),
            "ctrl+alt+s".to_string(),
            true,
            "toggle".to_string(),
            "registered".to_string(),
        );
        let status = state.status();
        assert_eq!(status.post_processing_hotkey, "");
        assert_eq!(
            state.action_for_event(primary_id, ShortcutState::Pressed, Instant::now()),
            Some("/api/live-mic/toggle")
        );
    }

    #[test]
    fn native_device_observe_only_log_state_summarizes_noisy_events() {
        let mut state = NativeDeviceObserveOnlyLogState::default();
        let event = super::audio_devices::NativeDeviceEvent::new(
            "property_value_changed",
            "capture",
            "unknown",
            "hash",
        );
        let start = Instant::now();

        let first = state.maybe_summary(&event, start).unwrap();
        assert!(first.contains("count=1"));
        assert!(first.contains("last_kind=property_value_changed"));

        assert!(state
            .maybe_summary(
                &event,
                start + NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL - Duration::from_millis(1)
            )
            .is_none());
        let by_interval = state
            .maybe_summary(
                &event,
                start + NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL + Duration::from_millis(1),
            )
            .unwrap();
        assert!(by_interval.contains("count=3"));

        for i in 4..NATIVE_DEVICE_OBSERVE_ONLY_LOG_EVERY_EVENTS {
            assert!(state
                .maybe_summary(
                    &event,
                    start + NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL + Duration::from_millis(i),
                )
                .is_none());
        }
        let by_count = state
            .maybe_summary(
                &event,
                start
                    + NATIVE_DEVICE_OBSERVE_ONLY_LOG_INTERVAL
                    + Duration::from_millis(NATIVE_DEVICE_OBSERVE_ONLY_LOG_EVERY_EVENTS),
            )
            .unwrap();
        assert!(by_count.contains("count=1000"));
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
    fn backend_http_request_without_body_uses_zero_content_length() {
        let request = build_backend_http_request("POST", "/api/live-mic/toggle", 8765, "", "");

        assert!(request.starts_with("POST /api/live-mic/toggle HTTP/1.1\r\n"));
        assert!(request.contains("Host: 127.0.0.1:8765\r\n"));
        assert!(request.contains("Content-Length: 0\r\n"));
        assert!(!request.contains("Content-Type: application/json\r\n"));
        assert!(request.ends_with("\r\n\r\n"));
    }

    #[test]
    fn backend_http_request_with_json_body_includes_token_and_length() {
        let body = r#"{"source":"tauri","flow":"capture"}"#;
        let request =
            build_backend_http_request("POST", "/api/microphones/refresh", 8765, "secret", body);

        assert!(request.contains("X-Scriber-Token: secret\r\n"));
        assert!(request.contains("Content-Type: application/json\r\n"));
        assert!(request.contains(&format!("Content-Length: {}\r\n", body.as_bytes().len())));
        assert!(request.ends_with(body));
    }

    #[test]
    fn shell_ipc_env_pairs_are_added_only_when_configured() {
        assert!(shell_ipc_env_pairs(None).is_empty());

        let config = shell_ipc::ShellIpcConfig {
            pipe_name: r"\\.\pipe\scriber-shell-test".to_string(),
            token: "secret-token".to_string(),
        };
        let pairs = shell_ipc_env_pairs(Some(&config));

        assert_eq!(pairs.len(), 3);
        assert!(pairs.contains(&(SHELL_IPC_PIPE_ENV, config.pipe_name.clone())));
        assert!(pairs.contains(&(SHELL_IPC_TOKEN_ENV, config.token.clone())));
        assert!(pairs.contains(&(SHELL_IPC_API_VERSION_ENV, "1".to_string())));
    }

    #[test]
    fn shell_menu_item_filter_accepts_only_owned_items() {
        assert!(is_shell_menu_item(MENU_ITEM_SHOW_WINDOW));
        assert!(is_shell_menu_item(MENU_ITEM_RESTART_BACKEND));
        assert!(is_shell_menu_item(MENU_ITEM_REFRESH_RECENT));
        assert!(is_shell_menu_item(MENU_ITEM_QUIT));
        assert!(is_shell_menu_item(&format!(
            "{MENU_ITEM_COPY_TRANSCRIPT_PREFIX}mic-00001"
        )));
        assert!(!is_shell_menu_item("copy"));
    }

    #[test]
    fn recent_transcript_menu_labels_are_short_and_stable() {
        let entry = RecentTranscriptMenuEntry {
            id: "mic-00001".to_string(),
            title: "A very long transcript title with a lot of extra whitespace".to_string(),
            date: "Today, 15:26".to_string(),
            transcript_type: "mic".to_string(),
        };

        let label = recent_transcript_label(&entry);

        assert!(label.starts_with("Mic: A very long transcript title"));
        assert!(label.ends_with("(Today, 15:26)"));
        assert_eq!(
            sanitize_menu_label("  one\n two\tthree  ", "fallback", 20),
            "one two three"
        );
        assert_eq!(sanitize_menu_label("", "fallback", 20), "fallback");
    }

    #[test]
    fn recent_transcript_entries_filter_invalid_ids_status_and_limit() {
        let mut items = vec![
            serde_json::json!({
                "id": "mic-failed",
                "status": "failed",
                "title": "Failed transcript",
                "date": "Today",
                "type": "mic"
            }),
            serde_json::json!({
                "id": "../secret",
                "status": "completed",
                "title": "Path traversal",
                "date": "Today",
                "type": "mic"
            }),
            serde_json::json!({
                "id": "bad?id=1",
                "status": "completed",
                "title": "Query id",
                "date": "Today",
                "type": "mic"
            }),
        ];
        for index in 0..(TRAY_RECENT_TRANSCRIPT_LIMIT + 2) {
            items.push(serde_json::json!({
                "id": format!("mic-{index:05}"),
                "status": "completed",
                "title": format!("Transcript {index}"),
                "date": "Today",
                "type": "mic"
            }));
        }
        let value = serde_json::json!({ "items": items });

        let entries = recent_transcripts_from_value(&value).expect("entries");

        assert_eq!(entries.len(), TRAY_RECENT_TRANSCRIPT_LIMIT);
        assert!(entries.iter().all(|entry| is_safe_transcript_id(&entry.id)));
        assert_eq!(entries[0].id, "mic-00000");
        assert_eq!(
            entries.last().map(|entry| entry.id.as_str()),
            Some("mic-00004")
        );
        assert!(!entries.iter().any(|entry| entry.id == "mic-failed"));
    }

    #[test]
    fn recent_transcript_entries_reject_missing_items_array() {
        let err = recent_transcripts_from_value(&serde_json::json!({"items": null}))
            .expect_err("missing items must be rejected");

        assert_eq!(err, "backend transcript list did not include items");
    }

    #[test]
    fn transcript_ids_allow_only_safe_path_characters() {
        assert!(is_safe_transcript_id("mic-00001"));
        assert!(is_safe_transcript_id(
            "550e8400-e29b-41d4-a716-446655440000"
        ));
        assert!(!is_safe_transcript_id(""));
        assert!(!is_safe_transcript_id("../secret"));
        assert!(!is_safe_transcript_id("bad?id=1"));
    }

    #[test]
    fn tray_click_opens_custom_panel() {
        assert!(should_show_window_for_tray_click(
            MouseButton::Left,
            Some(MouseButtonState::Up)
        ));
        assert!(should_show_window_for_tray_click(MouseButton::Left, None));
        assert!(!should_show_window_for_tray_click(
            MouseButton::Left,
            Some(MouseButtonState::Down)
        ));
        assert!(should_show_window_for_tray_click(
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
