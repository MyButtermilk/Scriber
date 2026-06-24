from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.process_utils import terminate_process


SEGMENTS_BY_REQUIREMENT = {
    "hotkey_to_recording_state": "hotkey_received_to_mic_ready_ms",
    "hotkey_to_first_audio_frame": "hotkey_received_to_first_audio_frame_ms",
    "stop_to_text_injection": "stop_requested_to_first_paste_ms",
}
STOP_TO_TEXT_SEGMENT = "stop_requested_to_first_paste_ms"
TEXT_BEFORE_STOP_SEGMENT = "first_paste_to_stop_requested_ms"
PROVIDER_TRANSCRIPT_SEGMENT = "hotkey_received_to_first_final_token_ms"
AUDIBLE_AUDIO_SEGMENT = "hotkey_received_to_first_audible_audio_frame_ms"
STOP_TO_LAST_CHUNK_SEGMENT = "stop_requested_to_last_chunk_sent_ms"
STOP_TO_PROVIDER_FINAL_SEGMENT = "stop_requested_to_provider_final_received_ms"
LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT = "last_chunk_sent_to_provider_final_received_ms"
PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT = "provider_final_received_to_clipboard_set_ms"
CLIPBOARD_SET_TO_PASTE_SEGMENT = "clipboard_set_to_paste_ms"
PASTE_TO_FIRST_PASTE_SEGMENT = "paste_to_first_paste_ms"
RUST_AUDIO_ACTIVE_ENGINE = "rust-wasapi"
RUST_AUDIO_FRAME_SOURCE = "rust-frame-pipe"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(((pct / 100.0) * len(ordered) + 0.999999) - 1)))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "meanMs": 0.0, "p50Ms": 0.0, "p95Ms": 0.0, "maxMs": 0.0}
    return {
        "count": len(values),
        "meanMs": round(statistics.fmean(values), 3),
        "p50Ms": round(percentile(values, 50.0), 3),
        "p95Ms": round(percentile(values, 95.0), 3),
        "maxMs": round(max(values), 3),
    }


def iteration_text_target_path(raw_path: str, index: int, total_iterations: int) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if total_iterations <= 1:
        return path
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem}.iteration-{index}{suffix}")


def short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:8]


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
    buffer = ctypes.create_unicode_buffer(max(2, length + 1))
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, len(buffer))
    return buffer.value


def _window_class(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), buffer, len(buffer))
    return buffer.value


def foreground_window_snapshot(expected_title: str = "") -> dict[str, Any]:
    if sys.platform != "win32":
        return {
            "available": False,
            "ok": False,
            "reason": "not_windows",
            "matchesExpectedTitle": False,
        }

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    if not hwnd:
        return {
            "available": True,
            "ok": False,
            "reason": "no_foreground_window",
            "matchesExpectedTitle": False,
        }

    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    title = _window_text(hwnd)
    class_name = _window_class(hwnd)
    matches = bool(expected_title and title == expected_title)
    return {
        "available": True,
        "ok": matches,
        "matchesExpectedTitle": matches,
        "hwndHash": short_hash(hwnd),
        "titleHash": short_hash(title),
        "titleLength": len(title),
        "classHash": short_hash(class_name),
        "processIdHash": short_hash(pid.value),
    }


def try_focus_text_target_window(title: str) -> bool:
    if sys.platform != "win32" or not title:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = int(user32.FindWindowW(None, title))
    if not hwnd:
        return False
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    user32.AttachThreadInput.restype = ctypes.c_bool
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.restype = ctypes.c_bool
    user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
    user32.SetActiveWindow.restype = ctypes.c_void_p
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    sw_restore = 9
    current_thread = int(kernel32.GetCurrentThreadId())
    target_thread = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None))
    foreground_hwnd = int(user32.GetForegroundWindow() or 0)
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground_hwnd), None))
        if foreground_hwnd
        else 0
    )
    attached_threads: list[int] = []
    for thread_id in {target_thread, foreground_thread}:
        if thread_id and thread_id != current_thread:
            if user32.AttachThreadInput(current_thread, thread_id, True):
                attached_threads.append(thread_id)
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), sw_restore)
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        user32.SetActiveWindow(ctypes.c_void_p(hwnd))
        focused = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
        return focused or bool(foreground_window_snapshot(title).get("ok"))
    finally:
        for thread_id in attached_threads:
            user32.AttachThreadInput(current_thread, thread_id, False)


def record_text_target_focus(
    sample: dict[str, Any],
    info: dict[str, Any] | None,
    phase: str,
    *,
    timeout_sec: float = 0.75,
    poll_sec: float = 0.05,
) -> bool:
    if not info:
        return True
    title = str(info.get("title") or "")
    deadline = time.monotonic() + max(0.0, timeout_sec)
    latest: dict[str, Any] | None = None
    while True:
        focus_requested = try_focus_text_target_window(title)
        latest = foreground_window_snapshot(title)
        latest = {
            "phase": phase,
            "ok": bool(latest.get("ok")),
            "focusRequested": focus_requested,
            "expectedTitleHash": short_hash(title),
            **latest,
        }
        if latest["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, poll_sec))

    sample.setdefault("textTargetFocus", []).append(latest)
    return bool(latest and latest.get("ok"))


def sleep_with_text_target_focus(
    sample: dict[str, Any],
    info: dict[str, Any] | None,
    seconds: float,
    phase: str,
    *,
    poll_sec: float = 0.2,
) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    index = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(max(0.01, poll_sec), remaining))
        index += 1
        record_text_target_focus(
            sample,
            info,
            f"{phase}:{index}",
            timeout_sec=0.05,
            poll_sec=0.01,
        )


def start_text_target_focus_keeper(
    sample: dict[str, Any],
    info: dict[str, Any] | None,
    *,
    interval_sec: float = 0.15,
) -> tuple[threading.Event, threading.Thread] | None:
    if not info:
        return None
    stop_event = threading.Event()

    def _run() -> None:
        index = 0
        while not stop_event.wait(max(0.05, interval_sec)):
            index += 1
            record_text_target_focus(
                sample,
                info,
                f"focus_keeper:{index}",
                timeout_sec=0.05,
                poll_sec=0.01,
            )

    thread = threading.Thread(
        target=_run,
        name="scriber-text-target-focus-keeper",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def stop_text_target_focus_keeper(
    keeper: tuple[threading.Event, threading.Thread] | None,
) -> None:
    if keeper is None:
        return
    stop_event, thread = keeper
    stop_event.set()
    thread.join(timeout=1.0)


def powershell_text_target_command() -> str:
    return r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$output = [Environment]::GetEnvironmentVariable('SCRIBER_TEXT_TARGET_OUTPUT')
$title = [Environment]::GetEnvironmentVariable('SCRIBER_TEXT_TARGET_TITLE')
if ([string]::IsNullOrWhiteSpace($title)) {
  $title = 'Scriber Hot Path Text Target'
}

[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = $title
$form.Width = 760
$form.Height = 320
$form.StartPosition = 'Manual'
$form.Left = 80
$form.Top = 80
$form.TopMost = $true

$text = New-Object System.Windows.Forms.TextBox
$text.Multiline = $true
$text.AcceptsReturn = $true
$text.AcceptsTab = $true
$text.ScrollBars = 'Vertical'
$text.Dock = 'Fill'
$text.Font = New-Object System.Drawing.Font('Segoe UI', 12)
$form.Controls.Add($text)

$save = {
  try {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($output, $text.Text, $encoding)
  } catch {
  }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
  & $save
  $form.Activate()
  $text.Focus()
})
$timer.Start()

$form.Add_Shown({
  $form.Activate()
  $text.Focus()
})
$form.Add_FormClosed({ & $save })
[System.Windows.Forms.Application]::Run($form)
""".strip()


def launch_text_target(args: argparse.Namespace, index: int) -> tuple[dict[str, Any] | None, subprocess.Popen | None]:
    if not args.text_target_file:
        return None, None

    target_path = iteration_text_target_path(args.text_target_file, index, args.iterations)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("", encoding="utf-8")
    if sys.platform != "win32":
        raise RuntimeError("Text target window is only supported on Windows")

    env = os.environ.copy()
    env["SCRIBER_TEXT_TARGET_OUTPUT"] = str(target_path)
    env["SCRIBER_TEXT_TARGET_TITLE"] = args.text_target_title
    proc = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-Sta",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            powershell_text_target_command(),
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(args.text_target_settle_sec)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"Text target window exited early with code {proc.returncode}: {stderr}")

    return {
        "path": str(target_path),
        "pid": proc.pid,
        "title": args.text_target_title,
    }, proc


def read_text_target_length(info: dict[str, Any] | None) -> int:
    if not info or not info.get("path"):
        return 0
    path = Path(str(info["path"]))
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8-sig", errors="replace").strip())


def wait_for_text_target_capture(
    info: dict[str, Any] | None,
    *,
    started_at: float,
    timeout_sec: float,
    poll_sec: float = 0.2,
) -> tuple[int, float | None]:
    if not info or not info.get("path"):
        return 0, None
    deadline = time.monotonic() + max(0.0, timeout_sec)
    captured = 0
    while True:
        captured = read_text_target_length(info)
        if captured > 0:
            return captured, round((time.monotonic() - started_at) * 1000, 3)
        if time.monotonic() >= deadline:
            return captured, None
        time.sleep(max(0.01, poll_sec))


def start_speech_prompt(args: argparse.Namespace) -> tuple[dict[str, Any] | None, subprocess.Popen | None]:
    text = (args.speech_prompt_text or "").strip()
    if not text:
        return None, None
    if sys.platform != "win32":
        return {"started": False, "error": "speech prompt is only supported on Windows"}, None

    env = os.environ.copy()
    env["SCRIBER_RECORDING_PROMPT_TEXT"] = text
    command = (
        "$voice = New-Object -ComObject SAPI.SpVoice; "
        "$voice.Speak([string]$env:SCRIBER_RECORDING_PROMPT_TEXT) | Out-Null"
    )
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {"started": True, "pid": proc.pid, "chars": len(text)}, proc


def requirement_values(
    samples: list[dict[str, Any]],
    requirement: str,
    segment_name: str,
) -> tuple[list[float], dict[str, Any]]:
    if requirement != "stop_to_text_injection":
        return [
            float((sample.get("segments") or {}).get(segment_name))
            for sample in samples
            if segment_name in (sample.get("segments") or {})
        ], {"sourceSegments": [segment_name]}

    values: list[float] = []
    after_stop_samples = 0
    already_injected_samples = 0
    provider_transcript_samples = 0
    provider_transcript_values: list[float] = []
    audible_audio_samples = 0
    audible_audio_values: list[float] = []
    stop_to_last_chunk_values: list[float] = []
    stop_to_provider_final_values: list[float] = []
    provider_finalize_values: list[float] = []
    provider_to_clipboard_values: list[float] = []
    clipboard_to_paste_values: list[float] = []
    paste_to_callback_values: list[float] = []
    for sample in samples:
        segments = sample.get("segments") or {}
        if AUDIBLE_AUDIO_SEGMENT in segments:
            audible_audio_samples += 1
            audible_audio_values.append(float(segments[AUDIBLE_AUDIO_SEGMENT]))
        if PROVIDER_TRANSCRIPT_SEGMENT in segments:
            provider_transcript_samples += 1
            provider_transcript_values.append(float(segments[PROVIDER_TRANSCRIPT_SEGMENT]))
        if STOP_TO_TEXT_SEGMENT in segments:
            values.append(float(segments[STOP_TO_TEXT_SEGMENT]))
            after_stop_samples += 1
        elif TEXT_BEFORE_STOP_SEGMENT in segments:
            # Real-time providers can inject text before the user stops recording.
            # In that case the stop-to-text wait is measured as zero, not missing.
            values.append(0.0)
            already_injected_samples += 1
        if STOP_TO_LAST_CHUNK_SEGMENT in segments:
            stop_to_last_chunk_values.append(float(segments[STOP_TO_LAST_CHUNK_SEGMENT]))
        if STOP_TO_PROVIDER_FINAL_SEGMENT in segments:
            stop_to_provider_final_values.append(float(segments[STOP_TO_PROVIDER_FINAL_SEGMENT]))
        if LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT in segments:
            provider_finalize_values.append(float(segments[LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT]))
        if PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT in segments:
            provider_to_clipboard_values.append(float(segments[PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT]))
        if CLIPBOARD_SET_TO_PASTE_SEGMENT in segments:
            clipboard_to_paste_values.append(float(segments[CLIPBOARD_SET_TO_PASTE_SEGMENT]))
        if PASTE_TO_FIRST_PASTE_SEGMENT in segments:
            paste_to_callback_values.append(float(segments[PASTE_TO_FIRST_PASTE_SEGMENT]))

    return values, {
        "sourceSegments": [STOP_TO_TEXT_SEGMENT, TEXT_BEFORE_STOP_SEGMENT],
        "diagnosticSegments": [
            PROVIDER_TRANSCRIPT_SEGMENT,
            AUDIBLE_AUDIO_SEGMENT,
            STOP_TO_LAST_CHUNK_SEGMENT,
            STOP_TO_PROVIDER_FINAL_SEGMENT,
            LAST_CHUNK_TO_PROVIDER_FINAL_SEGMENT,
            PROVIDER_FINAL_TO_CLIPBOARD_SET_SEGMENT,
            CLIPBOARD_SET_TO_PASTE_SEGMENT,
            PASTE_TO_FIRST_PASTE_SEGMENT,
        ],
        "afterStopInjectionSamples": after_stop_samples,
        "alreadyInjectedBeforeStopSamples": already_injected_samples,
        "audibleAudioSamples": audible_audio_samples,
        "audibleAudioDurations": summarize(audible_audio_values),
        "providerTranscriptSamples": provider_transcript_samples,
        "providerTranscriptDurations": summarize(provider_transcript_values),
        "lastChunkSentSamples": len(stop_to_last_chunk_values),
        "lastChunkSentDurations": summarize(stop_to_last_chunk_values),
        "stopToProviderFinalSamples": len(stop_to_provider_final_values),
        "stopToProviderFinalDurations": summarize(stop_to_provider_final_values),
        "providerFinalizeSamples": len(provider_finalize_values),
        "providerFinalizeDurations": summarize(provider_finalize_values),
        "providerToClipboardSamples": len(provider_to_clipboard_values),
        "providerToClipboardDurations": summarize(provider_to_clipboard_values),
        "clipboardToPasteSamples": len(clipboard_to_paste_values),
        "clipboardToPasteDurations": summarize(clipboard_to_paste_values),
        "pasteCallbackSamples": len(paste_to_callback_values),
        "pasteCallbackDurations": summarize(paste_to_callback_values),
    }


def text_target_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    target_samples = [
        sample.get("textTarget") or {}
        for sample in samples
        if sample.get("textTarget") is not None
    ]
    focus_checks = [
        check
        for sample in samples
        for check in (sample.get("textTargetFocus") or [])
        if sample.get("textTarget") is not None
    ]
    focus_errors = sum(1 for check in focus_checks if not check.get("ok"))
    captured_chars = [
        int(target.get("capturedChars") or 0)
        for target in target_samples
        if int(target.get("capturedChars") or 0) > 0
    ]
    capture_elapsed_values = [
        float(target.get("captureElapsedMs"))
        for target in target_samples
        if target.get("captureElapsedMs") is not None
    ]
    return {
        "configuredSamples": len(target_samples),
        "capturedSamples": len(captured_chars),
        "capturedChars": captured_chars,
        "maxCapturedChars": max(captured_chars) if captured_chars else 0,
        "captureElapsedDurations": summarize(capture_elapsed_values),
        "focusChecks": len(focus_checks),
        "focusErrors": focus_errors,
        "focusOk": len(target_samples) <= 0 or (bool(focus_checks) and focus_errors == 0),
    }


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class BackendClient:
    def __init__(self, base_url: str, token: str = "", timeout_sec: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_sec = timeout_sec

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Scriber-Token"] = self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise HttpError(exc.code, payload) from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request_json("GET", path)

    def post(self, path: str) -> dict[str, Any]:
        return self.request_json("POST", path, {})


def wait_for_state(
    client: BackendClient,
    predicate: Any,
    *,
    timeout_sec: float,
    poll_interval_sec: float = 0.2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = client.get("/api/state")
        if predicate(last_state):
            return last_state
        if last_state.get("recordingState") == "failed":
            return last_state
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"Timed out waiting for state. Last state: {last_state}")


def latest_metric_for_session(client: BackendClient, session_id: str) -> dict[str, Any] | None:
    metrics = client.get("/api/metrics/hot-path?limit=200")
    for item in metrics.get("items", []) or []:
        if item.get("sessionId") == session_id:
            return item
    return None


def wait_for_metric(client: BackendClient, session_id: str, *, timeout_sec: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        metric = latest_metric_for_session(client, session_id)
        if metric:
            return metric
        time.sleep(0.5)
    return None


def run_one_iteration(client: BackendClient, args: argparse.Namespace, index: int) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "iteration": index,
        "ok": False,
        "sessionId": "",
        "segments": {},
        "error": "",
    }
    started = False
    text_target_proc: subprocess.Popen | None = None
    text_target_focus_keeper: tuple[threading.Event, threading.Thread] | None = None
    speech_proc: subprocess.Popen | None = None
    try:
        text_target, text_target_proc = launch_text_target(args, index)
        if text_target:
            sample["textTarget"] = text_target
            record_text_target_focus(sample, text_target, "target_launched")
            text_target_focus_keeper = start_text_target_focus_keeper(sample, text_target)

        state = client.post("/api/live-mic/start")
        started = True
        session_id = str(state.get("sessionId") or state.get("current", {}).get("id") or "")
        sample["sessionId"] = session_id

        state = wait_for_state(
            client,
            lambda value: value.get("recordingState") == "recording" or value.get("listening") is True,
            timeout_sec=args.start_timeout_sec,
        )
        if state.get("recordingState") == "failed":
            raise RuntimeError(str(state.get("status") or "Recording failed during startup."))
        if not session_id:
            session_id = str(state.get("sessionId") or state.get("current", {}).get("id") or "")
            sample["sessionId"] = session_id
        if not session_id:
            raise RuntimeError("Backend did not expose a live session id.")
        if text_target:
            record_text_target_focus(sample, text_target, "recording_started")

        try:
            sample["audioDiagnosticsDuringRecording"] = client.get(
                "/api/runtime/audio-diagnostics"
            )
        except Exception as exc:
            sample["audioDiagnosticsDuringRecording"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        record_started_at = time.monotonic()
        if args.speech_prompt_text:
            sleep_with_text_target_focus(
                sample,
                text_target,
                min(args.speech_prompt_delay_sec, args.record_seconds),
                "before_speech_prompt",
            )
            speech_prompt, speech_proc = start_speech_prompt(args)
            sample["speechPrompt"] = speech_prompt
            if text_target:
                record_text_target_focus(sample, text_target, "speech_prompt_started")
        elapsed = time.monotonic() - record_started_at
        sleep_with_text_target_focus(
            sample,
            text_target,
            max(0.0, args.record_seconds - elapsed),
            "recording_wait",
        )
        if text_target:
            record_text_target_focus(sample, text_target, "before_stop")
        client.post("/api/live-mic/stop")
        started = False
        wait_for_state(
            client,
            lambda value: value.get("recordingState") == "idle" and not value.get("listening"),
            timeout_sec=args.stop_timeout_sec,
        )

        metric = wait_for_metric(client, session_id, timeout_sec=args.metric_timeout_sec)
        if not metric:
            raise RuntimeError(f"No hot-path metric appeared for session {session_id}.")
        segments = metric.get("segments") or {}
        sample["segments"] = segments
        sample["totalMs"] = metric.get("totalMs", 0.0)
        if "textTarget" in sample:
            captured_chars, capture_elapsed_ms = wait_for_text_target_capture(
                sample["textTarget"],
                started_at=record_started_at,
                timeout_sec=args.text_target_timeout_sec,
            )
            sample["textTarget"]["capturedChars"] = captured_chars
            sample["textTarget"]["captureElapsedMs"] = capture_elapsed_ms
            sample["textTarget"]["captured"] = captured_chars > 0
            record_text_target_focus(sample, text_target, "after_capture")
        sample["focusOk"] = all(
            bool(check.get("ok")) for check in sample.get("textTargetFocus", [])
        )
        sample["ok"] = any(name in segments for name in SEGMENTS_BY_REQUIREMENT.values())
    except Exception as exc:
        sample["error"] = str(exc)
        if started:
            try:
                client.post("/api/live-mic/stop")
            except Exception:
                pass
    finally:
        if speech_proc is not None:
            try:
                speech_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                terminate_process(speech_proc)
        stop_text_target_focus_keeper(text_target_focus_keeper)
        terminate_process(text_target_proc)
    return sample


def provider_transcript_requirement(samples: list[dict[str, Any]]) -> dict[str, Any]:
    transcript_values = [
        float((sample.get("segments") or {}).get(PROVIDER_TRANSCRIPT_SEGMENT))
        for sample in samples
        if PROVIDER_TRANSCRIPT_SEGMENT in (sample.get("segments") or {})
    ]
    audible_values = [
        float((sample.get("segments") or {}).get(AUDIBLE_AUDIO_SEGMENT))
        for sample in samples
        if AUDIBLE_AUDIO_SEGMENT in (sample.get("segments") or {})
    ]
    status = "measured" if transcript_values else "missing_audible_audio"
    if not transcript_values and audible_values:
        status = "missing_provider_transcript"
    return {
        "status": status,
        "segment": PROVIDER_TRANSCRIPT_SEGMENT,
        "providerTranscriptSamples": len(transcript_values),
        "providerTranscriptDurations": summarize(transcript_values),
        "audibleAudioSamples": len(audible_values),
        "audibleAudioDurations": summarize(audible_values),
    }


def rust_audio_requirement(
    samples: list[dict[str, Any]],
    audio_diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    feature_flags = (audio_diagnostics or {}).get("featureFlags") or {}
    requested = str(feature_flags.get("requestedAudioEngine") or "")
    effective = str(feature_flags.get("audioEngine") or "")
    rust_requested = feature_flags.get("rustAudioRequested") is True
    rust_available = feature_flags.get("rustAudioAvailable") is True

    active_captures: list[dict[str, Any]] = []
    fallback_circuits: list[dict[str, Any]] = []
    matching_samples = 0
    mic_always_on_samples = 0
    prewarm_adoption_samples = 0
    raw_prewarm_id_samples = 0
    mid_session_failure_samples = 0
    report_microphone = (audio_diagnostics or {}).get("microphone") or {}
    report_mic_always_on = (
        isinstance(report_microphone, dict)
        and report_microphone.get("micAlwaysOn") is True
    )
    report_circuit = ((audio_diagnostics or {}).get("microphone") or {}).get(
        "rustAudioFallbackCircuit"
    )
    if isinstance(report_circuit, dict):
        fallback_circuits.append(
            {
                "source": "report",
                "open": report_circuit.get("open"),
                "reason": report_circuit.get("reason"),
                "remainingSeconds": report_circuit.get("remainingSeconds"),
            }
        )
    for sample in samples:
        during = sample.get("audioDiagnosticsDuringRecording")
        if not isinstance(during, dict):
            continue
        microphone = during.get("microphone") or {}
        sample_mic_always_on = isinstance(microphone, dict) and microphone.get("micAlwaysOn") is True
        if sample_mic_always_on:
            mic_always_on_samples += 1
        during_circuit = microphone.get("rustAudioFallbackCircuit")
        if isinstance(during_circuit, dict):
            fallback_circuits.append(
                {
                    "source": f"sample:{sample.get('iteration')}",
                    "open": during_circuit.get("open"),
                    "reason": during_circuit.get("reason"),
                    "remainingSeconds": during_circuit.get("remainingSeconds"),
                }
            )
        active = microphone.get("activeCapture") or {}
        if not isinstance(active, dict) or not active:
            continue
        adoption = active.get("rustPrewarmAdoption")
        adoption_is_object = isinstance(adoption, dict)
        adoption_has_hash = False
        adoption_adopted = False
        adoption_has_raw_id = False
        if adoption_is_object:
            adoption_has_hash = bool(adoption.get("prewarmIdHash") or adoption.get("prewarm_idHash"))
            adoption_adopted = adoption.get("adopted") is True
            adoption_has_raw_id = "prewarmId" in adoption or "prewarm_id" in adoption
        source = active.get("source")
        mid_session_values = (
            active.get("midSessionFailureReason"),
            active.get("sourceMidSessionFailureReason"),
            active.get("lastRustAudioMidSessionFailureReason"),
            source.get("midSessionFailureReason") if isinstance(source, dict) else None,
        )
        reader_end_values = (
            active.get("framePipeReaderEndReason"),
            active.get("sourceFramePipeReaderEndReason"),
            source.get("framePipeReaderEndReason") if isinstance(source, dict) else None,
        )
        mid_session_failure = any(str(value or "").strip() for value in mid_session_values)
        unexpected_reader_end = next(
            (
                str(value or "").strip()
                for value in reader_end_values
                if str(value or "").strip() not in {"", "running"}
            ),
            "",
        )
        active_captures.append(
            {
                "engine": active.get("engine"),
                "frameSource": active.get("frameSource"),
                "callbackCount": active.get("callbackCount"),
                "nativeEndpointIdHash": active.get("nativeEndpointIdHash"),
                "requestedPrewarmIdHash": active.get("requestedPrewarmIdHash"),
                "adoptedPrewarm": active.get("adoptedPrewarm"),
                "rustPrewarmAdoption": {
                    "present": adoption_is_object,
                    "adopted": adoption_adopted,
                    "hasPrewarmIdHash": adoption_has_hash,
                    "hasRawPrewarmId": adoption_has_raw_id,
                },
                "engineFallbackReason": active.get("engineFallbackReason"),
                "rustAudioFallbackCircuitOpen": active.get("rustAudioFallbackCircuitOpen"),
                "rustAudioFallbackCircuitReason": active.get("rustAudioFallbackCircuitReason"),
                "midSessionFailure": mid_session_failure,
                "midSessionFailureReason": next(
                    (str(value or "") for value in mid_session_values if str(value or "").strip()),
                    "",
                ),
                "framePipeReaderEndReason": unexpected_reader_end
                or next((str(value or "") for value in reader_end_values if str(value or "").strip()), ""),
            }
        )
        if (
            active.get("engine") == RUST_AUDIO_ACTIVE_ENGINE
            and active.get("frameSource") == RUST_AUDIO_FRAME_SOURCE
        ):
            matching_samples += 1
            if mid_session_failure or unexpected_reader_end:
                mid_session_failure_samples += 1
            if adoption_is_object and adoption_adopted and adoption_has_hash and not adoption_has_raw_id:
                prewarm_adoption_samples += 1
            if adoption_has_raw_id:
                raw_prewarm_id_samples += 1

    status = "measured"
    fallback_circuit_open = any(circuit.get("open") is True for circuit in fallback_circuits)
    prewarm_adoption_required = report_mic_always_on or mic_always_on_samples > 0
    if fallback_circuit_open:
        status = "fallback_circuit_open"
    elif mid_session_failure_samples > 0:
        status = "mid_session_failure"
    elif raw_prewarm_id_samples > 0:
        status = "raw_prewarm_id"
    elif matching_samples <= 0:
        if not rust_requested:
            status = "not_requested"
        elif not rust_available or effective != RUST_AUDIO_ACTIVE_ENGINE:
            status = "unavailable"
        else:
            status = "missing_active_rust_capture"
    elif prewarm_adoption_required and prewarm_adoption_samples < matching_samples:
        status = "missing_prewarm_adoption"

    return {
        "status": status,
        "segment": "audioDiagnosticsDuringRecording.microphone.activeCapture",
        "requestedAudioEngine": requested,
        "audioEngine": effective,
        "rustAudioRequested": rust_requested,
        "rustAudioAvailable": rust_available,
        "matchingSamples": matching_samples,
        "micAlwaysOn": report_mic_always_on or mic_always_on_samples > 0,
        "micAlwaysOnSamples": mic_always_on_samples,
        "prewarmAdoptionRequired": prewarm_adoption_required,
        "prewarmAdoptionSamples": prewarm_adoption_samples,
        "rawPrewarmIdSamples": raw_prewarm_id_samples,
        "midSessionFailureSamples": mid_session_failure_samples,
        "activeCaptureSamples": len(active_captures),
        "activeCaptures": active_captures[:5],
        "fallbackCircuitOpen": fallback_circuit_open,
        "fallbackCircuits": fallback_circuits[:5],
    }


def build_summary(
    samples: list[dict[str, Any]],
    *,
    require_text_target: bool = False,
    require_provider_transcript: bool = False,
    require_rust_audio_engine: bool = False,
    audio_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requirements: dict[str, Any] = {}
    for requirement, segment_name in SEGMENTS_BY_REQUIREMENT.items():
        values, details = requirement_values(samples, requirement, segment_name)
        status = "measured" if values else "missing"
        if requirement == "stop_to_text_injection" and not values:
            if details.get("providerTranscriptSamples", 0):
                status = "missing_injection_after_transcript"
            elif details.get("audibleAudioSamples", 0):
                status = "missing_provider_transcript"
            else:
                status = "missing_audible_audio"
        requirements[requirement] = {
            "status": status,
            "segment": segment_name,
            **details,
            "durations": summarize(values),
        }

    target_summary = text_target_summary(samples)
    if require_text_target:
        target_status = "measured"
        if target_summary["configuredSamples"] <= 0:
            target_status = "missing_target_window"
        elif target_summary["capturedSamples"] <= 0:
            target_status = "missing_target_text"
        elif target_summary["focusErrors"] > 0:
            target_status = "focus_lost"
        elif target_summary["focusChecks"] <= 0:
            target_status = "focus_unverified"
        requirements["text_target_persistence"] = {
            "status": target_status,
            "segment": "textTarget.capturedChars",
            **target_summary,
            "durations": target_summary["captureElapsedDurations"],
        }

    if require_provider_transcript:
        requirements["provider_transcript"] = provider_transcript_requirement(samples)

    if require_rust_audio_engine:
        requirements["rust_audio_engine"] = rust_audio_requirement(samples, audio_diagnostics)

    return {
        "iterations": len(samples),
        "successfulSamples": sum(1 for sample in samples if sample.get("ok")),
        "textTarget": target_summary,
        "requirements": requirements,
        "complete": all(item["status"] == "measured" for item in requirements.values()),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    client = BackendClient(args.base_url, token=args.token, timeout_sec=args.http_timeout_sec)
    health = client.get("/api/health")
    try:
        audio_diagnostics: dict[str, Any] = client.get("/api/runtime/audio-diagnostics")
    except Exception as exc:
        audio_diagnostics = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    samples = [run_one_iteration(client, args, index) for index in range(1, args.iterations + 1)]
    summary = build_summary(
        samples,
        require_text_target=args.require_text_target,
        require_provider_transcript=args.require_provider_transcript,
        require_rust_audio_engine=args.require_rust_audio_engine,
        audio_diagnostics=audio_diagnostics,
    )
    strict_requirements = (
        args.require_text_target
        or args.require_provider_transcript
        or args.require_rust_audio_engine
    )
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url,
        "requested": {
            "iterations": args.iterations,
            "recordSeconds": args.record_seconds,
            "speechPromptText": args.speech_prompt_text,
            "speechPromptDelaySec": args.speech_prompt_delay_sec,
            "requireTextTarget": bool(args.require_text_target),
            "requireProviderTranscript": bool(args.require_provider_transcript),
            "textTargetTitle": args.text_target_title,
            "textTargetSettleSec": args.text_target_settle_sec,
            "textTargetTimeoutSec": args.text_target_timeout_sec,
        },
        "health": {
            "apiVersion": health.get("apiVersion"),
            "runtimeMode": health.get("runtimeMode"),
            "pid": health.get("pid"),
        },
        "audioDiagnostics": audio_diagnostics,
        "ok": summary["successfulSamples"] > 0
        and (summary["complete"] if strict_requirements else True),
        "summary": summary,
        "samples": samples,
    }


def build_validate_result(args: argparse.Namespace) -> dict[str, Any]:
    validate_audio_engine = RUST_AUDIO_ACTIVE_ENGINE if args.require_rust_audio_engine else "python"
    validate_rust_flags = {
        "audioEngine": validate_audio_engine,
        "requestedAudioEngine": validate_audio_engine,
        "rustAudioRequested": bool(args.require_rust_audio_engine),
        "rustAudioAvailable": bool(args.require_rust_audio_engine),
    }
    sample = {
        "iteration": 1,
        "ok": True,
        "sessionId": "validate-session",
        "segments": {
            "hotkey_received_to_mic_ready_ms": 120.0,
            "hotkey_received_to_first_audio_frame_ms": 180.0,
            "hotkey_received_to_first_audible_audio_frame_ms": 190.0,
            "hotkey_received_to_first_final_token_ms": 240.0,
            "hotkey_received_to_first_paste_ms": 260.0,
            "first_paste_to_stop_requested_ms": 350.0,
            "stop_requested_to_session_finished_ms": 90.0,
        },
        "validateOnly": True,
    }
    if args.require_rust_audio_engine:
        sample["audioDiagnosticsDuringRecording"] = {
            "featureFlags": dict(validate_rust_flags),
            "microphone": {
                "activeCapture": {
                    "engine": RUST_AUDIO_ACTIVE_ENGINE,
                    "frameSource": RUST_AUDIO_FRAME_SOURCE,
                    "callbackCount": 12,
                    "nativeEndpointIdHash": "validate-endpoint",
                },
            },
        }
    if args.text_target_file or args.require_text_target:
        sample["textTarget"] = {
            "path": args.text_target_file or "validate-target.txt",
            "pid": 0,
            "title": args.text_target_title,
            "capturedChars": len("Scriber validation prompt"),
            "captureElapsedMs": 35.0,
            "captured": True,
        }
        sample["textTargetFocus"] = [
            {
                "phase": "validate",
                "ok": True,
                "focusRequested": False,
                "expectedTitleHash": short_hash(args.text_target_title),
                "available": False,
                "matchesExpectedTitle": True,
            }
        ]
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url,
        "requested": {
            "iterations": args.iterations,
            "recordSeconds": args.record_seconds,
            "speechPromptText": args.speech_prompt_text,
            "speechPromptDelaySec": args.speech_prompt_delay_sec,
            "requireTextTarget": bool(args.require_text_target),
            "requireProviderTranscript": bool(args.require_provider_transcript),
            "textTargetTitle": args.text_target_title,
            "textTargetSettleSec": args.text_target_settle_sec,
            "textTargetTimeoutSec": args.text_target_timeout_sec,
        },
        "health": {"apiVersion": "validate", "runtimeMode": "validate", "pid": 0},
        "audioDiagnostics": {
            "apiVersion": "validate",
            "runtimeMode": "validate",
            "pid": 0,
            "featureFlags": {
                **validate_rust_flags,
            },
            "provider": {
                "configured": "validate",
                "active": None,
                "sonioxMode": "realtime",
            },
            "microphone": {
                "configuredDevice": "default",
                "favoriteMic": "",
                "favoriteMicConfigured": False,
                "micAlwaysOn": False,
                "idlePrewarmActive": False,
            },
            "textInjection": {
                "method": "auto",
                "disabled": False,
                "pastePreDelayMs": 80,
                "pasteRestoreDelayMs": 1500,
            },
            "runtimeImports": {
                "onnxruntime": {"importable": True, "error": None},
                "pipecat.audio.vad.silero": {"importable": True, "error": None},
            },
        },
        "ok": True,
        "summary": build_summary(
            [sample],
            require_text_target=args.require_text_target,
            require_provider_transcript=args.require_provider_transcript,
            require_rust_audio_engine=args.require_rust_audio_engine,
            audio_diagnostics={"featureFlags": dict(validate_rust_flags)},
        ),
        "samples": [sample],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive live mic start/stop and collect hot-path metrics.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", default="")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--record-seconds", type=float, default=2.0)
    parser.add_argument("--start-timeout-sec", type=float, default=20.0)
    parser.add_argument("--stop-timeout-sec", type=float, default=60.0)
    parser.add_argument("--metric-timeout-sec", type=float, default=10.0)
    parser.add_argument("--http-timeout-sec", type=float, default=10.0)
    parser.add_argument("--text-target-file", default="")
    parser.add_argument("--text-target-title", default="Scriber Hot Path Text Target")
    parser.add_argument("--text-target-settle-sec", type=float, default=1.0)
    parser.add_argument("--text-target-timeout-sec", type=float, default=5.0)
    parser.add_argument("--require-text-target", action="store_true")
    parser.add_argument("--require-provider-transcript", action="store_true")
    parser.add_argument("--require-rust-audio-engine", action="store_true")
    parser.add_argument("--speech-prompt-text", default="")
    parser.add_argument("--speech-prompt-delay-sec", type=float, default=0.5)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    args.iterations = max(1, int(args.iterations))
    args.record_seconds = max(0.1, float(args.record_seconds))
    args.text_target_settle_sec = max(0.1, float(args.text_target_settle_sec))
    args.text_target_timeout_sec = max(0.0, float(args.text_target_timeout_sec))
    args.speech_prompt_delay_sec = max(0.0, float(args.speech_prompt_delay_sec))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(urllib.parse.unquote(output_path)).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    args = parse_args(argv)
    result = build_validate_result(args) if args.validate_only else run_benchmark(args)
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
