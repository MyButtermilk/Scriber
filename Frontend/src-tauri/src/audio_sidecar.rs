mod audio_frame_pipe;

use audio_frame_pipe::{AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION};
use serde_json::{json, Value};
use std::{
    env,
    io::{self, BufRead, Write},
    process::ExitCode,
    time::Instant,
};

const SIDECAR_PROTOCOL_VERSION: &str = "1";
const SIDECAR_NAME: &str = "scriber-audio-sidecar";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        Some("--self-test") => write_json_line(&self_test_payload()),
        Some("--stdio") => run_stdio_loop(),
        Some("--help") | Some("-h") => {
            println!("{SIDECAR_NAME} --self-test | --stdio");
            Ok(())
        }
        Some(other) => {
            eprintln!("unsupported argument: {other}");
            Err(())
        }
        None => write_json_line(&self_test_payload()),
    };
    if result.is_ok() {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn run_stdio_loop() -> Result<(), ()> {
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line.map_err(|_| ())?;
        let response = handle_sidecar_request(&line);
        writeln!(stdout, "{response}").map_err(|_| ())?;
        stdout.flush().map_err(|_| ())?;
        if response
            .get("payload")
            .and_then(|payload| payload.get("shutdown"))
            .and_then(Value::as_bool)
            == Some(true)
        {
            break;
        }
    }
    Ok(())
}

fn write_json_line(payload: &Value) -> Result<(), ()> {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{payload}").map_err(|_| ())?;
    stdout.flush().map_err(|_| ())
}

fn handle_sidecar_request(raw: &str) -> Value {
    let started = Instant::now();
    let request = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(map)) => map,
        Ok(_) => {
            return response_payload(
                "",
                false,
                "invalidRequest",
                "request must be an object",
                started,
                json!({}),
            )
        }
        Err(_) => {
            return response_payload(
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
    let protocol_version = request
        .get("protocolVersion")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if protocol_version != SIDECAR_PROTOCOL_VERSION {
        return response_payload(
            request_id,
            false,
            "protocolVersionMismatch",
            "unsupported sidecar protocolVersion",
            started,
            json!({}),
        );
    }

    let command = request
        .get("command")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let payload = request.get("payload").unwrap_or(&Value::Null);
    match command {
        "ping" => response_payload(
            request_id,
            true,
            "",
            "",
            started,
            json!({"pong": true, "sidecar": SIDECAR_NAME}),
        ),
        "capabilities" => {
            response_payload(request_id, true, "", "", started, capabilities_payload())
        }
        "captureStart" => response_payload(
            request_id,
            false,
            "audioCaptureUnavailable",
            "WASAPI capture is not implemented in this sidecar skeleton",
            started,
            json!({
                "sidecar": SIDECAR_NAME,
                "captureAvailable": false,
                "requestedFormat": capture_request_payload(payload),
                "audioFrameProtocol": audio_frame_protocol_payload(),
            }),
        ),
        "captureStop" => response_payload(
            request_id,
            true,
            "",
            "",
            started,
            json!({
                "sidecar": SIDECAR_NAME,
                "stopped": false,
                "streamId": bounded_string(payload, "streamId", "", 96),
                "reason": "noActiveCapture",
            }),
        ),
        "shutdown" => response_payload(
            request_id,
            true,
            "",
            "",
            started,
            json!({"sidecar": SIDECAR_NAME, "shutdown": true}),
        ),
        _ => response_payload(
            request_id,
            false,
            "unknownCommand",
            "unsupported audio sidecar command",
            started,
            json!({}),
        ),
    }
}

fn self_test_payload() -> Value {
    json!({
        "sidecar": SIDECAR_NAME,
        "ok": true,
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "capabilities": capabilities_payload(),
    })
}

fn capabilities_payload() -> Value {
    json!({
        "sidecar": SIDECAR_NAME,
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "commands": ["ping", "capabilities", "captureStart", "captureStop", "shutdown"],
        "captureAvailable": false,
        "captureUnavailableReason": "wasapiCaptureNotImplemented",
        "audioFrameProtocol": audio_frame_protocol_payload(),
    })
}

fn audio_frame_protocol_payload() -> Value {
    json!({
        "magic": "SAF1",
        "version": AUDIO_FRAME_VERSION,
        "headerBytes": AUDIO_FRAME_HEADER_LEN,
        "sampleFormat": "pcm_i16_le",
    })
}

fn capture_request_payload(payload: &Value) -> Value {
    json!({
        "sampleRate": optional_u64(payload, "sampleRate", 16_000, 192_000),
        "channels": optional_u64(payload, "channels", 1, 16),
        "blockSize": optional_u64(payload, "blockSize", 512, 16_384),
        "devicePreference": bounded_string(payload, "devicePreference", "default", 96),
        "prebufferMs": optional_u64(payload, "prebufferMs", 0, 2_000),
    })
}

fn response_payload(
    request_id: &str,
    success: bool,
    error_code: &str,
    fallback_reason: &str,
    started: Instant,
    payload: Value,
) -> Value {
    json!({
        "protocolVersion": SIDECAR_PROTOCOL_VERSION,
        "requestId": request_id,
        "success": success,
        "errorCode": if error_code.is_empty() { Value::Null } else { Value::String(error_code.to_string()) },
        "fallbackReason": if fallback_reason.is_empty() { Value::Null } else { Value::String(fallback_reason.to_string()) },
        "timingsMs": {
            "total": started.elapsed().as_secs_f64() * 1000.0,
        },
        "payload": payload,
    })
}

fn optional_u64(payload: &Value, key: &str, default: u64, max: u64) -> u64 {
    payload
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_u64)
        .unwrap_or(default)
        .min(max)
}

fn bounded_string(payload: &Value, key: &str, default: &str, max_chars: usize) -> String {
    let value = payload
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_str)
        .unwrap_or(default)
        .trim()
        .chars()
        .take(max_chars)
        .collect::<String>();
    if value.is_empty() {
        default.to_string()
    } else {
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidecar_self_test_reports_protocol_and_frame_contract() {
        let payload = self_test_payload();

        assert_eq!(payload["sidecar"], SIDECAR_NAME);
        assert_eq!(payload["ok"], true);
        assert_eq!(payload["protocolVersion"], SIDECAR_PROTOCOL_VERSION);
        assert_eq!(payload["capabilities"]["captureAvailable"], false);
        assert_eq!(
            payload["capabilities"]["audioFrameProtocol"]["sampleFormat"],
            "pcm_i16_le"
        );
    }

    #[test]
    fn sidecar_ping_uses_newline_safe_json_contract() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r1",
            "command": "ping",
            "payload": {}
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], true);
        assert_eq!(response["requestId"], "r1");
        assert_eq!(response["payload"]["pong"], true);
    }

    #[test]
    fn sidecar_capture_start_returns_explicit_unavailable_payload() {
        let request = json!({
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "requestId": "r-capture",
            "command": "captureStart",
            "payload": {
                "sampleRate": 999_999,
                "channels": 99,
                "blockSize": 999_999,
                "devicePreference": "default",
                "prebufferMs": 999_999,
            }
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "audioCaptureUnavailable");
        assert_eq!(
            response["payload"]["requestedFormat"]["sampleRate"],
            192_000
        );
        assert_eq!(response["payload"]["requestedFormat"]["channels"], 16);
        assert_eq!(response["payload"]["requestedFormat"]["prebufferMs"], 2_000);
        assert_eq!(response["payload"]["audioFrameProtocol"]["version"], 1);
    }

    #[test]
    fn sidecar_rejects_protocol_mismatch_before_command_dispatch() {
        let request = json!({
            "protocolVersion": "2",
            "requestId": "r-bad-version",
            "command": "ping",
            "payload": {}
        });

        let response = handle_sidecar_request(&request.to_string());

        assert_eq!(response["success"], false);
        assert_eq!(response["errorCode"], "protocolVersionMismatch");
    }
}
