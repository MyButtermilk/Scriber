from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

DWMWA_CLOAKED = 14


def qpc_frequency() -> int:
    value = ctypes.c_longlong()
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(value)):
        return 1_000_000_000
    return int(value.value)


def qpc_ticks() -> int:
    value = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(value)):
        return time.perf_counter_ns()
    return int(value.value)


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_rect(hwnd: int) -> dict[str, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"left": 0, "top": 0, "right": 0, "bottom": 0, "width": 0, "height": 0}
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "right": int(rect.right),
        "bottom": int(rect.bottom),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def is_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_int(0)
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.c_uint(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and bool(cloaked.value)


def hwnd_hash(hwnd: int) -> str:
    if not hwnd:
        return ""
    return hashlib.sha256(str(hwnd).encode("ascii", errors="replace")).hexdigest()[:8]


def window_process_id(hwnd: int) -> int:
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(
        wintypes.HWND(hwnd),
        ctypes.byref(process_id),
    )
    return int(process_id.value)


def observe_window(hwnd: int) -> dict[str, Any] | None:
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    title = window_text(hwnd)
    rect = window_rect(hwnd)
    return {
        "hwndHash": hwnd_hash(hwnd),
        "pid": window_process_id(hwnd),
        "title": title,
        "visible": bool(user32.IsWindowVisible(wintypes.HWND(hwnd))),
        "minimized": bool(user32.IsIconic(wintypes.HWND(hwnd))),
        "cloaked": is_cloaked(hwnd),
        "rect": rect,
        "validScreenArea": rect["width"] > 0 and rect["height"] > 0,
        "qpcTicks": qpc_ticks(),
    }


def observe_windows(title_contains: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        title = window_text(hwnd)
        if title_contains.lower() not in title.lower():
            return True
        observed = observe_window(int(hwnd))
        if observed is not None:
            matches.append(observed)
        return True

    user32.EnumWindows(enum_proc, 0)
    return matches


def write_json(path_value: str, value: dict[str, Any]) -> None:
    if not path_value:
        return
    path = Path(path_value).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe the real Scriber overlay window.")
    parser.add_argument("--title-contains", default="Scriber Recording Overlay")
    parser.add_argument("--timeout-sec", type=float, default=5.0)
    parser.add_argument("--poll-sec", type=float, default=0.02)
    parser.add_argument("--expected-pid", type=int, default=0)
    parser.add_argument("--expected-hwnd", type=int, default=0)
    parser.add_argument("--ready-output", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    expected_mode = args.expected_pid > 0 or args.expected_hwnd > 0
    ready_observation = observe_window(args.expected_hwnd) if args.expected_hwnd > 0 else None
    ready_ok = bool(
        not expected_mode
        or (
            args.expected_pid > 0
            and args.expected_hwnd > 0
            and ready_observation is not None
            and ready_observation.get("pid") == args.expected_pid
            and args.title_contains.lower()
            in str(ready_observation.get("title") or "").lower()
            and not ready_observation.get("visible")
        )
    )
    ready = {
        "schemaVersion": 1,
        "ok": ready_ok,
        "endpoint": "overlay_observer_ready",
        "observerPid": os.getpid(),
        "qpcFrequency": qpc_frequency(),
        "readyQpcTicks": qpc_ticks(),
        "expectedPid": args.expected_pid if expected_mode else None,
        "expectedHwndHash": hwnd_hash(args.expected_hwnd) if expected_mode else "",
        "targetPresent": ready_observation is not None if expected_mode else True,
        "hiddenAtReady": (
            not bool(ready_observation.get("visible"))
            if ready_observation is not None
            else not expected_mode
        ),
    }
    write_json(args.ready_output, ready)

    deadline = time.monotonic() + max(0.1, args.timeout_sec)
    observations: list[dict[str, Any]] = []
    first_visible: dict[str, Any] | None = None
    while ready_ok and time.monotonic() < deadline:
        if expected_mode:
            expected_observation = observe_window(args.expected_hwnd)
            observations = [expected_observation] if expected_observation is not None else []
        else:
            observations = observe_windows(args.title_contains)
        first_visible = next(
            (
                item
                for item in observations
                if item.get("pid") == args.expected_pid or not expected_mode
                if item.get("hwndHash") == hwnd_hash(args.expected_hwnd) or not expected_mode
                if item["visible"]
                and not item["minimized"]
                and not item["cloaked"]
                and item["validScreenArea"]
            ),
            None,
        )
        if first_visible:
            break
        time.sleep(max(0.005, args.poll_sec))

    result = {
        "schemaVersion": 1,
        "ok": first_visible is not None,
        "reason": (
            None
            if first_visible is not None
            else "expected_overlay_target_not_ready"
            if not ready_ok
            else "expected_overlay_visible_frame_not_observed"
        ),
        "endpoint": "overlay_first_visible_frame",
        "qpcFrequency": qpc_frequency(),
        "observerReady": ready,
        "expectedPid": args.expected_pid if expected_mode else None,
        "expectedHwndHash": hwnd_hash(args.expected_hwnd) if expected_mode else "",
        "firstVisible": first_visible,
        "observations": observations,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    write_json(args.output, result)
    print(payload)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
