#![allow(dead_code)]

use serde_json::{json, Value};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::redaction::hash_sensitive_identifier;

#[cfg(windows)]
use std::{
    ffi::c_void,
    sync::mpsc::{self, RecvTimeoutError, Sender},
    thread::{self, JoinHandle},
};

#[cfg(windows)]
use windows::{
    core::PCWSTR,
    Win32::{
        Foundation::PROPERTYKEY,
        Media::Audio::{
            eAll, eCapture, eCommunications, eConsole, eRender, EDataFlow, ERole, IAudioClient,
            IMMDeviceEnumerator, IMMNotificationClient, IMMNotificationClient_Impl,
            MMDeviceEnumerator, AUDCLNT_SHAREMODE_SHARED, DEVICE_STATE, DEVICE_STATE_ACTIVE,
            WAVEFORMATEX,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL,
            COINIT_MULTITHREADED,
        },
    },
};

const NATIVE_DEVICE_DEBOUNCE: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PassiveAudioProbeOptions {
    pub requested_sample_rate: u32,
    pub requested_channels: u16,
    pub block_size: u32,
    pub device_preference: String,
    pub port_audio_label: String,
    pub native_endpoint_id_hash: String,
}

impl Default for PassiveAudioProbeOptions {
    fn default() -> Self {
        Self {
            requested_sample_rate: 16_000,
            requested_channels: 1,
            block_size: 512,
            device_preference: "default".to_string(),
            port_audio_label: String::new(),
            native_endpoint_id_hash: String::new(),
        }
    }
}

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

pub fn run_passive_audio_probe(options: PassiveAudioProbeOptions) -> Result<Value, String> {
    run_passive_audio_probe_impl(options)
}

#[cfg(not(windows))]
fn run_passive_audio_probe_impl(options: PassiveAudioProbeOptions) -> Result<Value, String> {
    Ok(passive_audio_probe_payload(
        &options,
        false,
        "unsupportedPlatform",
        "WASAPI audio probe is only available on Windows",
        json!({}),
    ))
}

#[cfg(windows)]
fn run_passive_audio_probe_impl(options: PassiveAudioProbeOptions) -> Result<Value, String> {
    let started = Instant::now();
    let com_initialized = match unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }.ok() {
        Ok(()) => true,
        Err(err) => {
            return Ok(passive_audio_probe_payload(
                &options,
                false,
                "comInitializationFailed",
                &format!("{err}"),
                json!({}),
            ));
        }
    };

    let result = (|| -> Result<Value, String> {
        let enumerator: IMMDeviceEnumerator =
            unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                .map_err(|err| format!("MMDeviceEnumerator creation failed: {err}"))?;
        let active_capture_endpoint_count = unsafe {
            enumerator
                .EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)
                .and_then(|collection| collection.GetCount())
                .ok()
        };
        let selected = select_passive_probe_device(&enumerator, &options)?;
        let device = selected.device;
        let endpoint_id_hash = selected.endpoint_id_hash;
        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|err| format!("IAudioClient activation failed: {err}"))?;

        let mut default_period_hns = 0i64;
        let mut minimum_period_hns = 0i64;
        let device_period = unsafe {
            client
                .GetDevicePeriod(Some(&mut default_period_hns), Some(&mut minimum_period_hns))
                .ok()
        };
        let mut stream_initialized = false;
        let mut buffer_frames: Option<u32> = None;
        let mix_format_ptr = unsafe { client.GetMixFormat() }
            .map_err(|err| format!("GetMixFormat failed: {err}"))?;
        let mix_format = unsafe { *mix_format_ptr };
        let initialize_result = unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                0,
                1_000_000,
                0,
                mix_format_ptr,
                None,
            )
        };
        let initialize_error = match initialize_result {
            Ok(()) => {
                stream_initialized = true;
                buffer_frames = unsafe { client.GetBufferSize().ok() };
                None
            }
            Err(err) => Some(format!("{err}")),
        };
        unsafe {
            CoTaskMemFree(Some(mix_format_ptr.cast::<c_void>()));
        }

        let mut payload = passive_audio_probe_payload(
            &options,
            true,
            "",
            "",
            json!({
                "selection": selected.selection_mode.clone(),
                "endpointIdHash": endpoint_id_hash,
                "endpointSelection": passive_endpoint_selection_payload(
                    &options,
                    &endpoint_id_hash,
                    &selected.selection_mode,
                    selected.used_default_endpoint,
                    selected.fallback_reason.clone(),
                ),
                "activeCaptureEndpointCount": active_capture_endpoint_count,
                "mixFormat": wave_format_payload(&mix_format),
                "requestedFormat": requested_format_payload(&options),
                "devicePeriodHns": if device_period.is_some() {
                    json!({
                        "default": default_period_hns,
                        "minimum": minimum_period_hns,
                    })
                } else {
                    Value::Null
                },
                "streamInitialized": stream_initialized,
                "bufferFrames": buffer_frames,
                "initializeError": initialize_error,
                "callbackCount": 0,
                "lastCallbackAgoSeconds": Value::Null,
                "droppedFrameCount": 0,
                "closeStatus": "closed",
                "probeDurationMs": started.elapsed().as_secs_f64() * 1000.0,
            }),
        );
        if let Some(object) = payload.as_object_mut() {
            object.insert(
                "fallbackReason".to_string(),
                selected
                    .fallback_reason
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
        }
        Ok(payload)
    })();

    if com_initialized {
        unsafe {
            CoUninitialize();
        }
    }

    match result {
        Ok(payload) => Ok(payload),
        Err(err) => Ok(passive_audio_probe_payload(
            &options,
            false,
            "probeFailed",
            &err,
            json!({
                "callbackCount": 0,
                "lastCallbackAgoSeconds": Value::Null,
                "droppedFrameCount": 0,
                "closeStatus": "closed",
                "probeDurationMs": started.elapsed().as_secs_f64() * 1000.0,
            }),
        )),
    }
}

#[cfg(windows)]
struct PassiveProbeSelectedDevice {
    device: windows::Win32::Media::Audio::IMMDevice,
    endpoint_id_hash: String,
    selection_mode: String,
    used_default_endpoint: bool,
    fallback_reason: Option<String>,
}

#[cfg(windows)]
fn select_passive_probe_device(
    enumerator: &IMMDeviceEnumerator,
    options: &PassiveAudioProbeOptions,
) -> Result<PassiveProbeSelectedDevice, String> {
    let requested_hash = options.native_endpoint_id_hash.trim();
    if !requested_hash.is_empty() {
        let collection = unsafe { enumerator.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE) }
            .map_err(|err| format!("WASAPI probe endpoint enumeration failed: {err}"))?;
        let count = unsafe { collection.GetCount() }
            .map_err(|err| format!("WASAPI probe endpoint count failed: {err}"))?;
        for index in 0..count {
            let device = unsafe { collection.Item(index) }
                .map_err(|err| format!("WASAPI probe endpoint item {index} failed: {err}"))?;
            let endpoint_id = unsafe { device_id_string(&device) };
            let endpoint_hash = hash_endpoint_id(&endpoint_id);
            if endpoint_hash == requested_hash {
                return Ok(PassiveProbeSelectedDevice {
                    device,
                    endpoint_id_hash: requested_hash.to_string(),
                    selection_mode: "nativeEndpointHash".to_string(),
                    used_default_endpoint: false,
                    fallback_reason: None,
                });
            }
        }
        return Err(format!(
            "requested native WASAPI probe endpoint hash was not found: {requested_hash}"
        ));
    }

    if !is_default_device_preference(&options.device_preference) {
        return Err(
            "requested non-default WASAPI probe has no native endpoint hash; refusing default fallback"
                .to_string(),
        );
    }

    let device = unsafe { enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
        .map_err(|err| format!("default capture endpoint unavailable: {err}"))?;
    let endpoint_id = unsafe { device_id_string(&device) };
    let endpoint_id_hash = hash_endpoint_id(&endpoint_id);
    Ok(PassiveProbeSelectedDevice {
        device,
        endpoint_id_hash,
        selection_mode: "default".to_string(),
        used_default_endpoint: true,
        fallback_reason: None,
    })
}

fn passive_audio_probe_payload(
    options: &PassiveAudioProbeOptions,
    available: bool,
    error_code: &str,
    error_message: &str,
    extra: Value,
) -> Value {
    let mut payload = json!({
        "engine": "rust-prototype",
        "probeKind": "wasapi-passive",
        "available": available,
        "errorCode": if error_code.is_empty() { Value::Null } else { Value::String(error_code.to_string()) },
        "errorMessage": if error_message.is_empty() { Value::Null } else { Value::String(error_message.to_string()) },
        "requestedFormat": requested_format_payload(options),
        "selection": "unavailable",
        "endpointIdHash": Value::Null,
        "activeCaptureEndpointCount": Value::Null,
        "mixFormat": Value::Null,
        "devicePeriodHns": Value::Null,
        "streamInitialized": false,
        "bufferFrames": Value::Null,
        "initializeError": Value::Null,
        "callbackCount": 0,
        "lastCallbackAgoSeconds": Value::Null,
        "droppedFrameCount": 0,
        "closeStatus": "closed",
        "fallbackReason": Value::Null,
    });
    if let (Some(target), Some(source)) = (payload.as_object_mut(), extra.as_object()) {
        for (key, value) in source {
            target.insert(key.clone(), value.clone());
        }
    }
    payload
}

fn requested_format_payload(options: &PassiveAudioProbeOptions) -> Value {
    json!({
        "sampleRate": options.requested_sample_rate,
        "channels": options.requested_channels,
        "blockSize": options.block_size,
        "devicePreference": bounded_hint_string(options.device_preference.clone(), "default"),
        "portAudioLabel": bounded_hint_string(options.port_audio_label.clone(), ""),
        "nativeEndpointIdHash": if options.native_endpoint_id_hash.trim().is_empty() {
            Value::Null
        } else {
            Value::String(options.native_endpoint_id_hash.clone())
        },
    })
}

fn passive_endpoint_selection_payload(
    options: &PassiveAudioProbeOptions,
    selected_endpoint_id_hash: &str,
    mode: &str,
    used_default_endpoint: bool,
    fallback_reason: Option<String>,
) -> Value {
    json!({
        "mode": mode,
        "requestedDevicePreference": options.device_preference,
        "requestedPortAudioLabel": options.port_audio_label,
        "requestedNativeEndpointIdHash": if options.native_endpoint_id_hash.trim().is_empty() {
            Value::Null
        } else {
            Value::String(options.native_endpoint_id_hash.clone())
        },
        "selectedNativeEndpointIdHash": if selected_endpoint_id_hash.is_empty() {
            Value::Null
        } else {
            Value::String(selected_endpoint_id_hash.to_string())
        },
        "usedDefaultEndpoint": used_default_endpoint,
        "fallbackReason": fallback_reason.map(Value::String).unwrap_or(Value::Null),
    })
}

#[cfg(windows)]
fn wave_format_payload(format: &WAVEFORMATEX) -> Value {
    let format_tag = format.wFormatTag;
    let channels = format.nChannels;
    let sample_rate = format.nSamplesPerSec;
    let average_bytes_per_second = format.nAvgBytesPerSec;
    let block_align = format.nBlockAlign;
    let bits_per_sample = format.wBitsPerSample;
    let extra_size = format.cbSize;
    json!({
        "formatTag": format_tag,
        "channels": channels,
        "sampleRate": sample_rate,
        "averageBytesPerSecond": average_bytes_per_second,
        "blockAlign": block_align,
        "bitsPerSample": bits_per_sample,
        "extraSize": extra_size,
    })
}

#[cfg(windows)]
unsafe fn device_id_string(device: &windows::Win32::Media::Audio::IMMDevice) -> String {
    let id = unsafe { device.GetId() }.ok();
    let Some(id) = id else {
        return String::new();
    };
    let text = unsafe { id.to_string() }.unwrap_or_default();
    unsafe {
        CoTaskMemFree(Some(id.as_ptr().cast::<c_void>()));
    }
    text
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

fn is_default_device_preference(value: &str) -> bool {
    let normalized = value.trim().to_ascii_lowercase();
    normalized.is_empty() || normalized == "default" || normalized == "none"
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
    hash_sensitive_identifier(endpoint_id)
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
    fn passive_audio_probe_payload_uses_redacted_contract_shape() {
        let options = super::PassiveAudioProbeOptions {
            requested_sample_rate: 16_000,
            requested_channels: 1,
            block_size: 512,
            device_preference: "default".to_string(),
            port_audio_label: "Default Mic, Windows WASAPI".to_string(),
            native_endpoint_id_hash: "abc123".to_string(),
        };

        let payload = super::passive_audio_probe_payload(
            &options,
            true,
            "",
            "",
            serde_json::json!({
                "endpointIdHash": "abc123",
                "selection": "default",
            }),
        );

        assert_eq!(payload["engine"], "rust-prototype");
        assert_eq!(payload["probeKind"], "wasapi-passive");
        assert_eq!(payload["available"], true);
        assert_eq!(payload["endpointIdHash"], "abc123");
        assert_eq!(payload["requestedFormat"]["sampleRate"], 16_000);
        assert_eq!(
            payload["requestedFormat"]["portAudioLabel"],
            "Default Mic, Windows WASAPI"
        );
        assert_eq!(payload["requestedFormat"]["nativeEndpointIdHash"], "abc123");
        assert!(payload.get("endpointId").is_none());
    }

    #[cfg(not(windows))]
    #[test]
    fn passive_audio_probe_reports_unsupported_off_windows() {
        let payload =
            super::run_passive_audio_probe(super::PassiveAudioProbeOptions::default()).unwrap();

        assert_eq!(payload["available"], false);
        assert_eq!(payload["errorCode"], "unsupportedPlatform");
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
        assert_eq!(super::hash_endpoint_id(""), "");
        assert_eq!(
            super::hash_endpoint_id(r"SWD\MMDEVAPI\{0.0.1.00000000}.{secret-device-guid}"),
            "e9a658ee3eff25fd"
        );
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
