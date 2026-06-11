from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any
from ctypes import wintypes

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.measure_recording_hot_path_baseline import TEXT_TARGET_WINDOW_FLAG
from scripts.process_utils import terminate_process


def repo_root() -> Path:
    return _REPO_ROOT


def default_output_path() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return repo_root() / "tmp" / "hybrid-baseline" / f"text-injection-smoke-{stamp}.json"


def default_target_path(output_path: Path) -> Path:
    suffix = output_path.suffix
    stem = output_path.name[: -len(suffix)] if suffix else output_path.name
    return output_path.with_name(f"{stem}-target.txt")


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def wait_for_target_text(
    target_path: Path,
    expected_text: str,
    *,
    started_at: float,
    timeout_sec: float,
    poll_sec: float,
) -> tuple[str, float | None]:
    deadline = time.monotonic() + timeout_sec
    last_text = ""
    while time.monotonic() < deadline:
        last_text = read_text(target_path)
        if expected_text in last_text:
            return last_text, round((time.monotonic() - started_at) * 1000, 3)
        time.sleep(poll_sec)
    return last_text, None


def evaluate_result(
    *,
    expected_text: str,
    callback_text: str,
    target_text: str,
    callback_elapsed_ms: float | None,
    target_elapsed_ms: float | None,
    target_error: str = "",
) -> dict[str, Any]:
    callback_verified = bool(callback_text)
    target_verified = expected_text in target_text

    if callback_verified and target_verified:
        status = "passed"
    elif callback_verified and not target_verified:
        status = "callback_without_target_text"
    elif target_verified and not callback_verified:
        status = "target_text_without_callback"
    else:
        status = "failed"

    return {
        "status": status,
        "ok": status == "passed",
        "callbackVerified": callback_verified,
        "targetTextVerified": target_verified,
        "callbackElapsedMs": callback_elapsed_ms,
        "targetTextElapsedMs": target_elapsed_ms,
        "capturedChars": len(target_text),
        "targetError": target_error,
    }


def launch_target_window(target_path: Path, title: str, settle_sec: float) -> subprocess.Popen:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(repo_root() / "scripts" / "measure_recording_hot_path_baseline.py"),
            TEXT_TARGET_WINDOW_FLAG,
            "--target-output",
            str(target_path),
            "--target-title",
            title,
        ],
        cwd=repo_root(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(settle_sec)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"Text target window exited early with code {proc.returncode}: {stderr}")
    return proc


def active_window_title() -> str:
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def click_target_window(title: str, x: int, y: int) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"attempted": False, "reason": "not_windows"}
    result: dict[str, Any] = {
        "attempted": True,
        "title": title,
        "requestedX": int(x),
        "requestedY": int(y),
        "activeBefore": active_window_title(),
    }
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            rect = wintypes.RECT()
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            click_x = int(rect.left) + int(x)
            click_y = int(rect.top) + int(y)
            result.update(
                {
                    "hwnd": int(hwnd),
                    "coordinateMode": "window-relative",
                    "windowRect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                }
            )
        else:
            click_x = int(x)
            click_y = int(y)
            result.update({"hwnd": 0, "coordinateMode": "screen"})

        user32.SetCursorPos(click_x, click_y)
        # MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        result.update({"x": click_x, "y": click_y, "activeAfter": active_window_title(), "ok": True})
        return result
    except Exception as exc:
        result.update({"ok": False, "error": str(exc), "activeAfter": active_window_title()})
        return result


def run_injection_smoke(args: argparse.Namespace) -> dict[str, Any]:
    output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path()
    target_path = (
        Path(args.target_file).expanduser().resolve()
        if args.target_file
        else default_target_path(output_path)
    )

    injected_texts: list[str] = []
    callback_elapsed_ms: float | None = None
    started_at = 0.0
    target_proc: subprocess.Popen | None = None
    target_error = ""
    focus_result: dict[str, Any] = {"attempted": False}

    try:
        target_proc = launch_target_window(target_path, args.target_title, args.settle_sec)
        if not args.skip_target_click:
            focus_result = click_target_window(args.target_title, args.click_x, args.click_y)
            time.sleep(args.post_click_settle_sec)

        from src.config import Config
        from src.injector import HAS_GUI, TextInjector

        Config.INJECT_METHOD = args.method
        os.environ["SCRIBER_INJECT_METHOD"] = args.method
        if args.paste_restore_delay_ms >= 0:
            Config.PASTE_RESTORE_DELAY_MS = int(args.paste_restore_delay_ms)
            os.environ["SCRIBER_PASTE_RESTORE_DELAY_MS"] = str(Config.PASTE_RESTORE_DELAY_MS)

        if not HAS_GUI:
            target_error = "GUI injection libraries are unavailable"
        else:
            def on_injected(text: str) -> None:
                nonlocal callback_elapsed_ms
                injected_texts.append(text)
                callback_elapsed_ms = round((time.monotonic() - started_at) * 1000, 3)

            injector = TextInjector(on_injected=on_injected)
            started_at = time.monotonic()
            injector._inject_text(args.text)
    except Exception as exc:
        target_error = str(exc)
        started_at = started_at or time.monotonic()
    finally:
        target_text, target_elapsed_ms = wait_for_target_text(
            target_path,
            args.text,
            started_at=started_at or time.monotonic(),
            timeout_sec=args.timeout_sec,
            poll_sec=args.poll_sec,
        )
        terminate_process(target_proc)

    result = evaluate_result(
        expected_text=args.text,
        callback_text=injected_texts[0] if injected_texts else "",
        target_text=target_text,
        callback_elapsed_ms=callback_elapsed_ms,
        target_elapsed_ms=target_elapsed_ms,
        target_error=target_error,
    )
    result.update(
        {
            "schemaVersion": 1,
            "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": args.method,
            "expectedChars": len(args.text),
            "targetFile": str(target_path),
            "targetTitle": args.target_title,
            "targetFocus": focus_result,
            "shellIpc": shell_ipc_snapshot(),
        }
    )
    return result


def build_validate_result(args: argparse.Namespace) -> dict[str, Any]:
    target_text = args.text
    result = evaluate_result(
        expected_text=args.text,
        callback_text=args.text,
        target_text=target_text,
        callback_elapsed_ms=12.5,
        target_elapsed_ms=35.0,
    )
    result.update(
        {
            "schemaVersion": 1,
            "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": args.method,
            "expectedChars": len(args.text),
            "targetFile": "",
            "targetTitle": args.target_title,
            "targetFocus": {"attempted": False, "validateOnly": True},
            "shellIpc": shell_ipc_snapshot(),
            "validateOnly": True,
        }
    )
    return result


def shell_ipc_snapshot() -> dict[str, Any]:
    try:
        from src.runtime.shell_ipc import diagnostic_snapshot

        snapshot = diagnostic_snapshot()
        return snapshot if isinstance(snapshot, dict) else {}
    except Exception as exc:
        return {
            "available": False,
            "snapshotError": f"{type(exc).__name__}: {exc}",
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test real OS text injection into a safe target window.")
    parser.add_argument("--text", default="Scriber injection smoke target text.")
    parser.add_argument("--method", choices=["paste", "sendinput", "type", "auto", "tauri"], default="paste")
    parser.add_argument("--output", default="")
    parser.add_argument("--target-file", default="")
    parser.add_argument("--target-title", default="Scriber Injection Smoke Target")
    parser.add_argument("--settle-sec", type=float, default=1.0)
    parser.add_argument("--post-click-settle-sec", type=float, default=0.2)
    parser.add_argument("--timeout-sec", type=float, default=5.0)
    parser.add_argument("--poll-sec", type=float, default=0.1)
    parser.add_argument("--click-x", type=int, default=180)
    parser.add_argument("--click-y", type=int, default=160)
    parser.add_argument("--skip-target-click", action="store_true")
    parser.add_argument("--paste-restore-delay-ms", type=int, default=1500)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    args.text = str(args.text or "").strip() or "Scriber injection smoke target text."
    args.settle_sec = max(0.1, float(args.settle_sec))
    args.post_click_settle_sec = max(0.0, float(args.post_click_settle_sec))
    args.timeout_sec = max(0.1, float(args.timeout_sec))
    args.poll_sec = max(0.01, float(args.poll_sec))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(urllib.parse.unquote(output_path)).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = build_validate_result(args) if args.validate_only else run_injection_smoke(args)
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
