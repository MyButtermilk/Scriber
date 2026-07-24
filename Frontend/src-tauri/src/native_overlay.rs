#[cfg(not(test))]
use serde::Serialize;
use serde_json::{json, Value};
use std::sync::{Mutex, OnceLock};
#[cfg(not(test))]
use std::{sync::mpsc, time::Duration};
#[cfg(not(test))]
use tauri::{Emitter, LogicalPosition, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
#[cfg(all(not(test), windows))]
use windows_sys::Win32::UI::WindowsAndMessaging::{
    IsWindowVisible, ShowWindow, SW_HIDE, SW_SHOWNOACTIVATE,
};

pub const OVERLAY_WINDOW_LABEL: &str = "recording-overlay";
#[cfg(not(test))]
pub const OVERLAY_EVENT: &str = "scriber-overlay-state";

const OVERLAY_WIDTH: f64 = 255.0;
const OVERLAY_HEIGHT: f64 = 78.0;
const OVERLAY_BOTTOM_MARGIN: f64 = 12.0;
#[cfg(not(test))]
const OVERLAY_UI_COMMAND_TIMEOUT: Duration = Duration::from_secs(2);

#[cfg(not(test))]
static OVERLAY_APP_HANDLE: OnceLock<tauri::AppHandle> = OnceLock::new();
static OVERLAY_STATE: OnceLock<Mutex<OverlayState>> = OnceLock::new();
static OVERLAY_MUTATION_LANE: OnceLock<Mutex<()>> = OnceLock::new();

#[derive(Debug, Clone)]
struct OverlayState {
    mode: String,
    visible: bool,
    cursor_events_ignored: bool,
    last_rms: f64,
    window_created: bool,
    renderer_ready: bool,
    #[cfg_attr(test, allow(dead_code))]
    position_initialized: bool,
}

impl Default for OverlayState {
    fn default() -> Self {
        Self {
            mode: "hidden".to_string(),
            visible: false,
            cursor_events_ignored: true,
            last_rms: 0.0,
            window_created: false,
            renderer_ready: false,
            position_initialized: false,
        }
    }
}

#[cfg(not(test))]
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct OverlayEventPayload {
    api_version: &'static str,
    renderer: &'static str,
    mode: String,
    visible: bool,
    rms: Option<f64>,
}

#[cfg(not(test))]
pub fn set_app_handle(app: tauri::AppHandle) {
    let _ = OVERLAY_APP_HANDLE.set(app);
}

#[cfg(test)]
pub fn set_app_handle(_app: tauri::AppHandle) {}

#[cfg(not(test))]
#[allow(dead_code)]
pub fn create_overlay_window(app: &tauri::App) -> tauri::Result<()> {
    if app.get_webview_window(OVERLAY_WINDOW_LABEL).is_some() {
        mark_overlay_window_created();
        return Ok(());
    }

    let window = WebviewWindowBuilder::new(
        app,
        OVERLAY_WINDOW_LABEL,
        // If Windows defers loading a hidden WebView until its first show, render a useful first
        // frame while the listener + native snapshot handshake completes.
        WebviewUrl::App("index.html?overlay=1&overlayMode=initializing".into()),
    )
    .title("Scriber Recording Overlay")
    .inner_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
    .resizable(false)
    .decorations(false)
    .transparent(true)
    .shadow(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .focusable(false)
    .visible(false)
    .build()?;
    window.set_ignore_cursor_events(true)?;
    mark_overlay_cursor_events_ignored(true);
    mark_overlay_window_created();
    mark_overlay_renderer_unready();
    position_overlay_window(&window)?;
    mark_overlay_position_initialized();
    Ok(())
}

#[cfg(test)]
#[allow(dead_code)]
pub fn create_overlay_window(_app: &tauri::App) -> tauri::Result<()> {
    Ok(())
}

#[allow(dead_code)]
pub fn handle_shell_command(command: &str, payload: &Value) -> Result<Value, String> {
    // This entry point is intentionally non-scheduling for code already running on Tauri's UI
    // thread. Off-thread callers (shell IPC, global hotkeys, smoke workers) must use
    // `handle_shell_command_on_ui_thread`; scheduling and synchronously waiting from the UI thread
    // itself would deadlock until the bounded timeout.
    handle_shell_command_now(command, payload)
}

fn overlay_command_requires_ui_thread(command: &str) -> bool {
    matches!(command, "overlayPrepare" | "overlayShow" | "overlayHide")
}

#[cfg(not(test))]
pub fn handle_shell_command_on_ui_thread(command: &str, payload: &Value) -> Result<Value, String> {
    if !overlay_command_requires_ui_thread(command) {
        return handle_shell_command_now(command, payload);
    }
    let app = overlay_app_handle()?;
    let command = command.to_string();
    let payload = payload.clone();
    let (result_tx, result_rx) = mpsc::sync_channel(1);
    app.run_on_main_thread(move || {
        let _ = result_tx.send(handle_shell_command_now(&command, &payload));
    })
    .map_err(|err| format!("overlay UI dispatch failed: {err}"))?;
    result_rx
        .recv_timeout(OVERLAY_UI_COMMAND_TIMEOUT)
        .map_err(|_| "overlay UI dispatch timed out".to_string())?
}

#[cfg(test)]
pub fn handle_shell_command_on_ui_thread(command: &str, payload: &Value) -> Result<Value, String> {
    handle_shell_command_now(command, payload)
}

fn handle_shell_command_now(command: &str, payload: &Value) -> Result<Value, String> {
    // Showing and hiding a WebView is a multi-step state transition. Shell IPC can serve
    // independent clients concurrently, and audio-level updates intentionally stay off the UI
    // scheduler, so keep mutations in one owner-level lane instead of relying on transport order.
    let _mutation_guard = if command == "overlayStatus" {
        None
    } else {
        Some(overlay_mutation_lock())
    };
    match command {
        "overlayPrepare" => prepare_overlay(payload),
        "overlayShow" => show_overlay(payload),
        "overlayHide" => hide_overlay(),
        "overlayAudioLevel" => record_audio_level(payload),
        "overlayStatus" => Ok(status_payload()),
        _ => Err(format!("unsupported overlay command: {command}")),
    }
}

fn show_overlay(payload: &Value) -> Result<Value, String> {
    let mode = normalize_overlay_mode(
        payload
            .get("mode")
            .and_then(Value::as_str)
            .unwrap_or("recording"),
    )?;
    show_overlay_mode(mode)
}

fn prepare_overlay(payload: &Value) -> Result<Value, String> {
    let mode = normalize_overlay_mode(
        payload
            .get("mode")
            .and_then(Value::as_str)
            .unwrap_or("initializing"),
    )?;
    prepare_overlay_mode(mode)
}

#[cfg(not(test))]
fn prepare_overlay_mode(mode: String) -> Result<Value, String> {
    let app = overlay_app_handle()?;
    let window = ensure_overlay_window(&app, &mode)?;
    ensure_overlay_positioned(&window).map_err(|err| format!("overlay position failed: {err}"))?;
    Ok(status_payload())
}

#[cfg(test)]
fn prepare_overlay_mode(_mode: String) -> Result<Value, String> {
    Ok(status_payload())
}

#[cfg(not(test))]
fn show_overlay_mode(mode: String) -> Result<Value, String> {
    let app = overlay_app_handle()?;
    let window = ensure_overlay_window(&app, &mode)?;
    ensure_overlay_positioned(&window).map_err(|err| format!("overlay position failed: {err}"))?;
    set_overlay_cursor_events_ignored(&window, true)?;
    show_overlay_window(&window)?;
    let event_payload = update_state(|state| {
        state.mode = mode.clone();
        state.visible = true;
        if state.mode == "recording" {
            state.last_rms = 0.0;
        }
        OverlayEventPayload {
            api_version: "1",
            renderer: "tauri-webview",
            mode: state.mode.clone(),
            visible: state.visible,
            rms: None,
        }
    });
    app.emit_to(OVERLAY_WINDOW_LABEL, OVERLAY_EVENT, event_payload)
        .map_err(|err| format!("overlay event emit failed: {err}"))?;
    set_overlay_cursor_events_ignored(&window, false)?;
    Ok(status_payload())
}

/// Completes the renderer handshake and returns the authoritative native state.
///
/// The overlay WebView is pre-created while hidden. A hotkey can therefore update native state
/// before React has registered its event listener. Returning the current snapshot after listener
/// registration makes that first transition durable instead of relying on one transient event.
pub fn mark_renderer_ready() -> Value {
    update_state(|state| {
        state.renderer_ready = true;
    });
    status_payload()
}

fn overlay_mutation_lock() -> std::sync::MutexGuard<'static, ()> {
    OVERLAY_MUTATION_LANE
        .get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
fn show_overlay_mode(mode: String) -> Result<Value, String> {
    update_state(|state| {
        state.mode = mode;
        state.visible = true;
    });
    Ok(status_payload())
}

#[cfg(not(test))]
fn hide_overlay() -> Result<Value, String> {
    let app = overlay_app_handle()?;
    if let Some(window) = overlay_window(&app) {
        // Make the stale window harmless before asking the OS to hide it. If this
        // best-effort guard fails, the physical hide remains the required postcondition.
        let _ = set_overlay_cursor_events_ignored(&window, true);
        hide_overlay_window(&window)?;
    } else {
        mark_overlay_cursor_events_ignored(true);
    }
    let event_payload = update_state(|state| {
        state.mode = "hidden".to_string();
        state.visible = false;
        OverlayEventPayload {
            api_version: "1",
            renderer: "tauri-webview",
            mode: state.mode.clone(),
            visible: state.visible,
            rms: None,
        }
    });
    // Physical visibility is already false, so renderer delivery is best effort.
    let _ = app.emit_to(OVERLAY_WINDOW_LABEL, OVERLAY_EVENT, event_payload);
    Ok(status_payload())
}

#[cfg(test)]
fn hide_overlay() -> Result<Value, String> {
    update_state(|state| {
        state.mode = "hidden".to_string();
        state.visible = false;
    });
    Ok(status_payload())
}

#[cfg(not(test))]
fn record_audio_level(payload: &Value) -> Result<Value, String> {
    let rms = payload
        .get("rms")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .clamp(0.0, 1.0);
    let event_payload = update_state(|state| {
        state.last_rms = rms;
        OverlayEventPayload {
            api_version: "1",
            renderer: "tauri-webview",
            mode: state.mode.clone(),
            visible: state.visible,
            rms: Some(rms),
        }
    });
    if let Ok(app) = overlay_app_handle() {
        // The state snapshot remains authoritative even if a renderer is still
        // mounting and cannot receive this best-effort event yet.
        let _ = app.emit_to(OVERLAY_WINDOW_LABEL, OVERLAY_EVENT, event_payload);
    }
    Ok(status_payload())
}

#[cfg(test)]
fn record_audio_level(payload: &Value) -> Result<Value, String> {
    let rms = payload
        .get("rms")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .clamp(0.0, 1.0);
    update_state(|state| {
        state.last_rms = rms;
    });
    Ok(status_payload())
}

#[cfg(not(test))]
fn overlay_app_handle() -> Result<tauri::AppHandle, String> {
    OVERLAY_APP_HANDLE
        .get()
        .cloned()
        .ok_or_else(|| "Tauri overlay app handle is not available".to_string())
}

#[cfg(not(test))]
fn overlay_window(app: &tauri::AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(OVERLAY_WINDOW_LABEL)
}

#[cfg(not(test))]
fn ensure_overlay_window(
    app: &tauri::AppHandle,
    initial_mode: &str,
) -> Result<WebviewWindow, String> {
    if let Some(window) = overlay_window(app) {
        mark_overlay_window_created();
        return Ok(window);
    }

    let safe_mode =
        normalize_overlay_mode(initial_mode).unwrap_or_else(|_| "recording".to_string());
    let window = WebviewWindowBuilder::new(
        app,
        OVERLAY_WINDOW_LABEL,
        WebviewUrl::App(format!("index.html?overlay=1&overlayMode={safe_mode}").into()),
    )
    .title("Scriber Recording Overlay")
    .inner_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
    .resizable(false)
    .decorations(false)
    .transparent(true)
    .shadow(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .focusable(false)
    .visible(false)
    .build()
    .map_err(|err| format!("overlay window create failed: {err}"))?;
    set_overlay_cursor_events_ignored(&window, true)?;
    mark_overlay_window_created();
    mark_overlay_renderer_unready();
    position_overlay_window(&window).map_err(|err| format!("overlay position failed: {err}"))?;
    mark_overlay_position_initialized();
    Ok(window)
}

#[cfg(not(test))]
fn show_overlay_window(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(windows)]
    {
        let hwnd = window
            .hwnd()
            .map_err(|err| format!("overlay window handle lookup failed: {err}"))?;
        unsafe {
            ShowWindow(hwnd.0, SW_SHOWNOACTIVATE);
        }
    }
    #[cfg(not(windows))]
    {
        window
            .show()
            .map_err(|err| format!("overlay show failed: {err}"))?;
    }
    if !overlay_window_is_visible(window)? {
        return Err("overlay show failed: native window remained hidden".to_string());
    }
    Ok(())
}

#[cfg(not(test))]
fn hide_overlay_window(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(windows)]
    {
        let hwnd = window
            .hwnd()
            .map_err(|err| format!("overlay window handle lookup failed: {err}"))?;
        unsafe {
            ShowWindow(hwnd.0, SW_HIDE);
        }
    }
    #[cfg(not(windows))]
    {
        window
            .hide()
            .map_err(|err| format!("overlay hide failed: {err}"))?;
    }
    if overlay_window_is_visible(window)? {
        return Err("overlay hide failed: native window remained visible".to_string());
    }
    Ok(())
}

#[cfg(not(test))]
fn overlay_window_is_visible(window: &WebviewWindow) -> Result<bool, String> {
    #[cfg(windows)]
    {
        let hwnd = window
            .hwnd()
            .map_err(|err| format!("overlay window handle lookup failed: {err}"))?;
        Ok(unsafe { IsWindowVisible(hwnd.0) != 0 })
    }
    #[cfg(not(windows))]
    {
        window
            .is_visible()
            .map_err(|err| format!("overlay visibility lookup failed: {err}"))
    }
}

#[cfg(not(test))]
fn set_overlay_cursor_events_ignored(
    window: &WebviewWindow,
    ignored: bool,
) -> Result<(), String> {
    window
        .set_ignore_cursor_events(ignored)
        .map_err(|err| format!("overlay cursor-event update failed: {err}"))?;
    mark_overlay_cursor_events_ignored(ignored);
    Ok(())
}

#[cfg(not(test))]
fn native_overlay_visibility() -> Option<bool> {
    let Some(app) = OVERLAY_APP_HANDLE.get() else {
        return Some(false);
    };
    let Some(window) = overlay_window(app) else {
        return Some(false);
    };
    overlay_window_is_visible(&window).ok()
}

#[cfg(test)]
fn native_overlay_visibility() -> Option<bool> {
    Some(false)
}

fn mark_overlay_cursor_events_ignored(ignored: bool) {
    update_state(|state| {
        state.cursor_events_ignored = ignored;
    });
}


#[cfg(not(test))]
fn position_overlay_window(window: &WebviewWindow) -> tauri::Result<()> {
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
        let (x, y) = overlay_position_for_work_area(
            f64::from(work_area.position.x) / scale,
            f64::from(work_area.position.y) / scale,
            f64::from(work_area.size.width) / scale,
            f64::from(work_area.size.height) / scale,
            OVERLAY_WIDTH,
            OVERLAY_HEIGHT,
            OVERLAY_BOTTOM_MARGIN,
        );
        window.set_position(LogicalPosition::new(x, y))?;
    }
    Ok(())
}

#[cfg(not(test))]
fn ensure_overlay_positioned(window: &WebviewWindow) -> tauri::Result<()> {
    let already_positioned = update_state(|state| state.position_initialized);
    if already_positioned {
        return Ok(());
    }
    position_overlay_window(window)?;
    mark_overlay_position_initialized();
    Ok(())
}

#[cfg(not(test))]
fn mark_overlay_position_initialized() {
    update_state(|state| {
        state.position_initialized = true;
    });
}

#[cfg_attr(test, allow(dead_code))]
fn mark_overlay_window_created() {
    update_state(|state| {
        state.window_created = true;
    });
}

#[cfg(not(test))]
fn mark_overlay_renderer_unready() {
    update_state(|state| {
        state.renderer_ready = false;
    });
}

fn overlay_position_for_work_area(
    work_x: f64,
    work_y: f64,
    work_width: f64,
    work_height: f64,
    overlay_width: f64,
    overlay_height: f64,
    bottom_margin: f64,
) -> (f64, f64) {
    let x = work_x + ((work_width - overlay_width) / 2.0).max(0.0);
    let y = work_y + (work_height - overlay_height - bottom_margin).max(0.0);
    (x.round(), y.round())
}

fn normalize_overlay_mode(raw: &str) -> Result<String, String> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "initializing" | "recording" | "transcribing" => Ok(raw.trim().to_ascii_lowercase()),
        other => Err(format!("unsupported overlay mode: {other}")),
    }
}

fn state_lock() -> &'static Mutex<OverlayState> {
    OVERLAY_STATE.get_or_init(|| Mutex::new(OverlayState::default()))
}

fn update_state<T>(update: impl FnOnce(&mut OverlayState) -> T) -> T {
    let mut state = state_lock()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    update(&mut state)
}

fn status_payload() -> Value {
    let state = state_lock()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    json!({
        "renderer": "tauri-webview",
        "windowLabel": OVERLAY_WINDOW_LABEL,
        "available": overlay_runtime_available(),
        "mode": state.mode,
        "requestedVisible": state.visible,
        "visible": state.visible,
        "nativeVisible": native_overlay_visibility(),
        "cursorEventsIgnored": state.cursor_events_ignored,
        "lastRms": state.last_rms,
        "windowCreated": state.window_created,
        "rendererReady": state.renderer_ready,
        "positionInitialized": state.position_initialized,
    })
}

#[cfg(not(test))]
fn overlay_runtime_available() -> bool {
    OVERLAY_APP_HANDLE.get().is_some()
}

#[cfg(test)]
fn overlay_runtime_available() -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overlay_position_centers_above_work_area_bottom() {
        let (x, y) = overlay_position_for_work_area(
            0.0,
            0.0,
            1920.0,
            1040.0,
            OVERLAY_WIDTH,
            OVERLAY_HEIGHT,
            OVERLAY_BOTTOM_MARGIN,
        );
        assert_eq!((x, y), (833.0, 950.0));
    }

    #[test]
    fn overlay_mode_validation_accepts_known_modes() {
        assert_eq!(normalize_overlay_mode("Recording").unwrap(), "recording");
        assert_eq!(
            normalize_overlay_mode("transcribing").unwrap(),
            "transcribing"
        );
        assert!(normalize_overlay_mode("floating").is_err());
    }

    #[test]
    fn overlay_status_is_unavailable_without_app_handle() {
        let status = status_payload();
        assert_eq!(status["renderer"], "tauri-webview");
        assert_eq!(status["available"], false);
    }

    #[test]
    fn only_window_mutations_require_the_tauri_ui_thread() {
        assert!(overlay_command_requires_ui_thread("overlayPrepare"));
        assert!(overlay_command_requires_ui_thread("overlayShow"));
        assert!(overlay_command_requires_ui_thread("overlayHide"));
        assert!(!overlay_command_requires_ui_thread("overlayAudioLevel"));
        assert!(!overlay_command_requires_ui_thread("overlayStatus"));
    }

    #[test]
    fn renderer_ready_handshake_returns_authoritative_snapshot() {
        update_state(|state| {
            state.mode = "recording".to_string();
            state.visible = true;
            state.renderer_ready = false;
        });

        let status = mark_renderer_ready();

        assert_eq!(status["mode"], "recording");
        assert_eq!(status["visible"], true);
        assert_eq!(status["rendererReady"], true);
    }

    #[test]
    fn overlay_mutation_lane_serializes_owner_transitions() {
        use std::{sync::mpsc, thread, time::Duration};

        let first = overlay_mutation_lock();
        let (acquired_tx, acquired_rx) = mpsc::sync_channel(1);
        let waiter = thread::spawn(move || {
            let _second = overlay_mutation_lock();
            let _ = acquired_tx.send(());
        });

        assert!(acquired_rx.recv_timeout(Duration::from_millis(25)).is_err());
        drop(first);
        acquired_rx
            .recv_timeout(Duration::from_millis(250))
            .expect("second overlay mutation should proceed after the owner lane is released");
        waiter.join().unwrap();
    }
}
