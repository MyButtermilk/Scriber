#![allow(dead_code)]

use serde_json::{json, Value};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(windows)]
use std::{
    sync::mpsc::{self, RecvTimeoutError, Sender},
    thread::{self, JoinHandle},
};

#[cfg(windows)]
use windows::{
    core::PCWSTR,
    Win32::{
        Foundation::PROPERTYKEY,
        Media::Audio::{
            eAll, eCapture, eCommunications, eConsole, eRender, EDataFlow, ERole,
            IMMDeviceEnumerator, IMMNotificationClient, IMMNotificationClient_Impl,
            MMDeviceEnumerator, DEVICE_STATE,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED,
        },
    },
};

const NATIVE_DEVICE_DEBOUNCE: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeDeviceEventsMode {
    Auto,
    Disabled,
    Enabled,
}

pub fn native_device_events_mode_from_env(raw: Option<&str>) -> NativeDeviceEventsMode {
    match raw.unwrap_or("auto").trim().to_ascii_lowercase().as_str() {
        "0" | "false" | "no" | "off" | "disabled" => NativeDeviceEventsMode::Disabled,
        "1" | "true" | "yes" | "on" | "enabled" => NativeDeviceEventsMode::Enabled,
        _ => NativeDeviceEventsMode::Auto,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NativeDeviceEvent {
    pub event_kind: String,
    pub flow: String,
    pub role: String,
    pub endpoint_id_hash: String,
    pub force_portaudio_refresh: bool,
    pub native_timestamp_ms: u64,
}

impl NativeDeviceEvent {
    pub fn new(
        event_kind: impl Into<String>,
        flow: impl Into<String>,
        role: impl Into<String>,
        endpoint_id_hash: impl Into<String>,
    ) -> Self {
        Self {
            event_kind: bounded_hint_string(event_kind.into(), "unknown"),
            flow: normalize_flow(&flow.into()),
            role: bounded_hint_string(role.into(), "unknown").to_ascii_lowercase(),
            endpoint_id_hash: bounded_hint_string(endpoint_id_hash.into(), ""),
            force_portaudio_refresh: true,
            native_timestamp_ms: now_ms(),
        }
    }

    pub fn should_forward(&self) -> bool {
        self.flow != "render"
    }

    pub fn debounce_key(&self) -> String {
        format!(
            "{}:{}:{}:{}",
            self.event_kind, self.flow, self.role, self.endpoint_id_hash
        )
    }

    pub fn to_backend_hint_body(&self) -> Value {
        json!({
            "source": "tauri",
            "eventKind": self.event_kind,
            "flow": self.flow,
            "role": self.role,
            "endpointIdHash": self.endpoint_id_hash,
            "forcePortAudioRefresh": self.force_portaudio_refresh,
            "nativeTimestampMs": self.native_timestamp_ms,
        })
    }
}

pub struct NativeDeviceEventDebouncer {
    debounce: Duration,
    last_key: Option<String>,
    last_emit_at: Option<Instant>,
}

pub struct NativeDeviceEventMonitorHandle {
    #[cfg(windows)]
    stop_tx: Option<Sender<()>>,
    #[cfg(windows)]
    join_handle: Option<JoinHandle<()>>,
}

#[cfg(windows)]
impl Drop for NativeDeviceEventMonitorHandle {
    fn drop(&mut self) {
        if let Some(stop_tx) = self.stop_tx.take() {
            let _ = stop_tx.send(());
        }
        if let Some(join_handle) = self.join_handle.take() {
            let _ = join_handle.join();
        }
    }
}

#[cfg(not(windows))]
impl Drop for NativeDeviceEventMonitorHandle {
    fn drop(&mut self) {}
}

#[cfg(windows)]
pub fn start_native_device_event_monitor<F, L>(
    mode: NativeDeviceEventsMode,
    mut on_event: F,
    mut log: L,
) -> Result<Option<NativeDeviceEventMonitorHandle>, String>
where
    F: FnMut(NativeDeviceEvent) + Send + 'static,
    L: FnMut(String) + Send + 'static,
{
    if mode == NativeDeviceEventsMode::Disabled {
        log("native device event monitor disabled by SCRIBER_NATIVE_DEVICE_EVENTS".to_string());
        return Ok(None);
    }

    let (event_tx, event_rx) = mpsc::channel::<NativeDeviceEvent>();
    let (stop_tx, stop_rx) = mpsc::channel::<()>();
    let join_handle = thread::Builder::new()
        .name("native-device-events".to_string())
        .spawn(move || {
            if let Err(err) =
                run_native_device_event_thread(event_tx, event_rx, stop_rx, &mut on_event, &mut log)
            {
                log(format!("native device event monitor stopped: {err}"));
            }
        })
        .map_err(|err| format!("could not spawn native device event thread: {err}"))?;

    Ok(Some(NativeDeviceEventMonitorHandle {
        stop_tx: Some(stop_tx),
        join_handle: Some(join_handle),
    }))
}

#[cfg(not(windows))]
pub fn start_native_device_event_monitor<F, L>(
    _mode: NativeDeviceEventsMode,
    _on_event: F,
    mut log: L,
) -> Result<Option<NativeDeviceEventMonitorHandle>, String>
where
    F: FnMut(NativeDeviceEvent) + Send + 'static,
    L: FnMut(String) + Send + 'static,
{
    log("native device event monitor unavailable on this platform".to_string());
    Ok(None)
}

#[cfg(windows)]
fn run_native_device_event_thread<F, L>(
    event_tx: Sender<NativeDeviceEvent>,
    event_rx: mpsc::Receiver<NativeDeviceEvent>,
    stop_rx: mpsc::Receiver<()>,
    on_event: &mut F,
    log: &mut L,
) -> Result<(), String>
where
    F: FnMut(NativeDeviceEvent),
    L: FnMut(String),
{
    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|err| format!("COM initialization failed: {err}"))?;

    let result = (|| -> Result<(), String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let client: IMMNotificationClient = AudioEndpointNotificationClient { event_tx }.into();
        unsafe {
            enumerator
                .RegisterEndpointNotificationCallback(&client)
                .map_err(|err| format!("endpoint callback registration failed: {err}"))?;
        }

        log("native device event monitor registered".to_string());
        let mut debouncer = NativeDeviceEventDebouncer::new();
        loop {
            if stop_rx.try_recv().is_ok() {
                break;
            }
            match event_rx.recv_timeout(Duration::from_millis(100)) {
                Ok(event) => {
                    if debouncer.should_emit(&event, Instant::now()) {
                        on_event(event);
                    }
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
        }

        unsafe {
            let _ = enumerator.UnregisterEndpointNotificationCallback(&client);
        }
        log("native device event monitor unregistered".to_string());
        Ok(())
    })();

    unsafe {
        CoUninitialize();
    }
    result
}

#[cfg(windows)]
#[windows::core::implement(IMMNotificationClient)]
struct AudioEndpointNotificationClient {
    event_tx: Sender<NativeDeviceEvent>,
}

#[cfg(windows)]
#[allow(non_snake_case)]
impl IMMNotificationClient_Impl for AudioEndpointNotificationClient_Impl {
    fn OnDeviceStateChanged(
        &self,
        pwstrdeviceid: &PCWSTR,
        _dwnewstate: DEVICE_STATE,
    ) -> windows::core::Result<()> {
        self.send_endpoint_event("device_state_changed", pwstrdeviceid, "unknown", None);
        Ok(())
    }

    fn OnDeviceAdded(&self, pwstrdeviceid: &PCWSTR) -> windows::core::Result<()> {
        self.send_endpoint_event("device_added", pwstrdeviceid, "unknown", None);
        Ok(())
    }

    fn OnDeviceRemoved(&self, pwstrdeviceid: &PCWSTR) -> windows::core::Result<()> {
        self.send_endpoint_event("device_removed", pwstrdeviceid, "unknown", None);
        Ok(())
    }

    fn OnDefaultDeviceChanged(
        &self,
        flow: EDataFlow,
        role: ERole,
        pwstrdefaultdeviceid: &PCWSTR,
    ) -> windows::core::Result<()> {
        self.send_endpoint_event(
            "default_device_changed",
            pwstrdefaultdeviceid,
            role_to_hint(role),
            Some(flow_to_hint(flow)),
        );
        Ok(())
    }

    fn OnPropertyValueChanged(
        &self,
        pwstrdeviceid: &PCWSTR,
        _key: &PROPERTYKEY,
    ) -> windows::core::Result<()> {
        self.send_endpoint_event("property_value_changed", pwstrdeviceid, "unknown", None);
        Ok(())
    }
}

#[cfg(windows)]
impl AudioEndpointNotificationClient_Impl {
    fn send_endpoint_event(
        &self,
        event_kind: &str,
        endpoint_id: &PCWSTR,
        role: &str,
        flow_override: Option<&str>,
    ) {
        let endpoint = pcwstr_to_string(endpoint_id);
        let event = NativeDeviceEvent::new(
            event_kind,
            flow_override.unwrap_or_else(|| flow_from_endpoint_id(&endpoint)),
            role,
            hash_endpoint_id(&endpoint),
        );
        let _ = self.event_tx.send(event);
    }
}

impl NativeDeviceEventDebouncer {
    pub fn new() -> Self {
        Self {
            debounce: NATIVE_DEVICE_DEBOUNCE,
            last_key: None,
            last_emit_at: None,
        }
    }

    pub fn should_emit(&mut self, event: &NativeDeviceEvent, now: Instant) -> bool {
        if !event.should_forward() {
            return false;
        }
        let key = event.debounce_key();
        let duplicate_inside_window = self
            .last_key
            .as_ref()
            .map(|last_key| last_key == &key)
            .unwrap_or(false)
            && self
                .last_emit_at
                .map(|last| now.duration_since(last) < self.debounce)
                .unwrap_or(false);
        if duplicate_inside_window {
            return false;
        }
        self.last_key = Some(key);
        self.last_emit_at = Some(now);
        true
    }
}

fn normalize_flow(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "0" | "render" | "output" => "render".to_string(),
        "1" | "capture" | "input" => "capture".to_string(),
        "2" | "all" => "all".to_string(),
        _ => "unknown".to_string(),
    }
}

fn bounded_hint_string(value: String, default: &str) -> String {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return default.to_string();
    }
    trimmed.chars().take(128).collect()
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

#[cfg(windows)]
fn pcwstr_to_string(value: &PCWSTR) -> String {
    unsafe { value.to_string().unwrap_or_default() }
}

#[cfg(windows)]
fn flow_from_endpoint_id(endpoint_id: &str) -> &'static str {
    let lowered = endpoint_id.to_ascii_lowercase();
    if lowered.contains("{0.0.0.") {
        "render"
    } else if lowered.contains("{0.0.1.") {
        "capture"
    } else {
        "unknown"
    }
}

#[cfg(windows)]
fn flow_to_hint(flow: EDataFlow) -> &'static str {
    if flow == eCapture {
        "capture"
    } else if flow == eRender {
        "render"
    } else if flow == eAll {
        "all"
    } else {
        "unknown"
    }
}

#[cfg(windows)]
fn role_to_hint(role: ERole) -> &'static str {
    if role == eConsole {
        "console"
    } else if role == eCommunications {
        "communications"
    } else {
        "unknown"
    }
}

fn hash_endpoint_id(endpoint_id: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in endpoint_id.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

#[cfg(test)]
mod tests {
    use super::{
        native_device_events_mode_from_env, NativeDeviceEvent, NativeDeviceEventDebouncer,
        NativeDeviceEventsMode,
    };
    use std::time::{Duration, Instant};

    #[test]
    fn native_device_events_mode_accepts_documented_values() {
        assert_eq!(
            native_device_events_mode_from_env(None),
            NativeDeviceEventsMode::Auto
        );
        assert_eq!(
            native_device_events_mode_from_env(Some("auto")),
            NativeDeviceEventsMode::Auto
        );
        assert_eq!(
            native_device_events_mode_from_env(Some("0")),
            NativeDeviceEventsMode::Disabled
        );
        assert_eq!(
            native_device_events_mode_from_env(Some("false")),
            NativeDeviceEventsMode::Disabled
        );
        assert_eq!(
            native_device_events_mode_from_env(Some("1")),
            NativeDeviceEventsMode::Enabled
        );
        assert_eq!(
            native_device_events_mode_from_env(Some("enabled")),
            NativeDeviceEventsMode::Enabled
        );
    }

    #[test]
    fn native_device_event_filters_render_flow() {
        let render = NativeDeviceEvent::new("device_added", "0", "console", "hash");
        let capture = NativeDeviceEvent::new("device_added", "1", "console", "hash");

        assert!(!render.should_forward());
        assert!(capture.should_forward());
    }

    #[test]
    fn native_device_event_body_matches_backend_hint_contract() {
        let mut event =
            NativeDeviceEvent::new("default_device_changed", "capture", "console", "abc");
        event.native_timestamp_ms = 1234;

        let body = event.to_backend_hint_body();

        assert_eq!(body["source"], "tauri");
        assert_eq!(body["eventKind"], "default_device_changed");
        assert_eq!(body["flow"], "capture");
        assert_eq!(body["role"], "console");
        assert_eq!(body["endpointIdHash"], "abc");
        assert_eq!(body["forcePortAudioRefresh"], true);
        assert_eq!(body["nativeTimestampMs"], 1234);
    }

    #[test]
    fn native_device_event_debouncer_suppresses_immediate_duplicates() {
        let mut debouncer = NativeDeviceEventDebouncer::new();
        let now = Instant::now();
        let event = NativeDeviceEvent::new("device_added", "capture", "console", "abc");

        assert!(debouncer.should_emit(&event, now));
        assert!(!debouncer.should_emit(&event, now + Duration::from_millis(100)));
        assert!(debouncer.should_emit(&event, now + Duration::from_millis(600)));
    }

    #[test]
    fn native_device_event_debouncer_allows_distinct_events_inside_window() {
        let mut debouncer = NativeDeviceEventDebouncer::new();
        let now = Instant::now();
        let first = NativeDeviceEvent::new("device_added", "capture", "console", "abc");
        let second = NativeDeviceEvent::new("device_removed", "capture", "console", "abc");

        assert!(debouncer.should_emit(&first, now));
        assert!(debouncer.should_emit(&second, now + Duration::from_millis(100)));
    }

    #[test]
    fn endpoint_hash_is_stable_and_redacted() {
        let raw = r"SWD\MMDEVAPI\{0.0.1.00000000}.{capture-device}";
        let hashed = super::hash_endpoint_id(raw);

        assert_eq!(hashed, super::hash_endpoint_id(raw));
        assert_ne!(hashed, raw);
        assert_eq!(hashed.len(), 16);
    }

    #[cfg(windows)]
    #[test]
    fn endpoint_id_flow_hint_matches_windows_mmdevice_pattern() {
        assert_eq!(
            super::flow_from_endpoint_id(r"SWD\MMDEVAPI\{0.0.0.00000000}.{render-device}"),
            "render"
        );
        assert_eq!(
            super::flow_from_endpoint_id(r"SWD\MMDEVAPI\{0.0.1.00000000}.{capture-device}"),
            "capture"
        );
        assert_eq!(super::flow_from_endpoint_id("unknown"), "unknown");
    }
}
