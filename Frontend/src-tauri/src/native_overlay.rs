#[cfg(not(test))]
use serde::Serialize;
use serde_json::{json, Value};
use std::sync::{Mutex, OnceLock};
#[cfg(not(test))]
use tauri::{
    Emitter, LogicalPosition, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};

pub const OVERLAY_WINDOW_LABEL: &str = "recording-overlay";
#[cfg(not(test))]
pub const OVERLAY_EVENT: &str = "scriber-overlay-state";

const OVERLAY_WIDTH: f64 = 255.0;
const OVERLAY_HEIGHT: f64 = 78.0;
const OVERLAY_BOTTOM_MARGIN: f64 = 12.0;

#[cfg(not(test))]
static OVERLAY_APP_HANDLE: OnceLock<tauri::AppHandle> = OnceLock::new();
static OVERLAY_STATE: OnceLock<Mutex<OverlayState>> = OnceLock::new();

#[derive(Debug, Clone)]
struct OverlayState {
    mode: String,
    visible: bool,
    last_rms: f64,
}

impl Default for OverlayState {
    fn default() -> Self {
        Self {
            mode: "hidden".to_string(),
            visible: false,
            last_rms: 0.0,
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
pub fn create_overlay_window(app: &tauri::App) -> tauri::Result<()> {
    if app.get_webview_window(OVERLAY_WINDOW_LABEL).is_some() {
        return Ok(());
    }

    let window = WebviewWindowBuilder::new(
        app,
        OVERLAY_WINDOW_LABEL,
        WebviewUrl::App("index.html?overlay=1".into()),
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
    position_overlay_window(&window)?;
    Ok(())
}

#[cfg(test)]
pub fn create_overlay_window(_app: &tauri::App) -> tauri::Result<()> {
    Ok(())
}

pub fn handle_shell_command(command: &str, payload: &Value) -> Result<Value, String> {
    match command {
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

#[cfg(not(test))]
fn show_overlay_mode(mode: String) -> Result<Value, String> {
    let app = overlay_app_handle()?;
    let window = overlay_window(&app)?;
    position_overlay_window(&window).map_err(|err| format!("overlay position failed: {err}"))?;
    let event_payload = update_state(|state| {
        state.mode = mode.clone();
        state.visible = true;
        OverlayEventPayload {
            api_version: "1",
            renderer: "tauri-webview",
            mode: state.mode.clone(),
            visible: state.visible,
            rms: None,
        }
    });
    window
        .show()
        .map_err(|err| format!("overlay show failed: {err}"))?;
    app.emit_to(OVERLAY_WINDOW_LABEL, OVERLAY_EVENT, event_payload)
        .map_err(|err| format!("overlay event emit failed: {err}"))?;
    Ok(status_payload())
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
    let window = overlay_window(&app)?;
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
    let _ = app.emit_to(OVERLAY_WINDOW_LABEL, OVERLAY_EVENT, event_payload);
    window
        .hide()
        .map_err(|err| format!("overlay hide failed: {err}"))?;
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
fn overlay_window(app: &tauri::AppHandle) -> Result<WebviewWindow, String> {
    app.get_webview_window(OVERLAY_WINDOW_LABEL)
        .ok_or_else(|| "Tauri overlay window is not available".to_string())
}

#[cfg(not(test))]
fn position_overlay_window(window: &WebviewWindow) -> tauri::Result<()> {
    let monitor = window.current_monitor()?.or(window.primary_monitor()?).or_else(|| {
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
    let mut state = state_lock().lock().unwrap();
    update(&mut state)
}

fn status_payload() -> Value {
    let state = state_lock().lock().unwrap().clone();
    json!({
        "renderer": "tauri-webview",
        "windowLabel": OVERLAY_WINDOW_LABEL,
        "available": overlay_runtime_available(),
        "mode": state.mode,
        "visible": state.visible,
        "lastRms": state.last_rms,
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
        assert_eq!(normalize_overlay_mode("transcribing").unwrap(), "transcribing");
        assert!(normalize_overlay_mode("floating").is_err());
    }

    #[test]
    fn overlay_status_is_unavailable_without_app_handle() {
        let status = status_payload();
        assert_eq!(status["renderer"], "tauri-webview");
        assert_eq!(status["available"], false);
    }
}
