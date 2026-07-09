from __future__ import annotations

import argparse
import ctypes
import json
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


def observe_windows(title_contains: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        title = window_text(hwnd)
        if title_contains.lower() not in title.lower():
            return True
        rect = window_rect(hwnd)
        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        cloaked = is_cloaked(hwnd)
        matches.append(
            {
                "hwndHash": hash(str(hwnd)) & 0xFFFFFFFF,
                "title": title,
                "visible": visible,
                "minimized": minimized,
                "cloaked": cloaked,
                "rect": rect,
                "validScreenArea": rect["width"] > 0 and rect["height"] > 0,
                "qpcTicks": qpc_ticks(),
            }
        )
        return True

    user32.EnumWindows(enum_proc, 0)
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe the real Scriber overlay window.")
    parser.add_argument("--title-contains", default="Scriber Recording Overlay")
    parser.add_argument("--timeout-sec", type=float, default=5.0)
    parser.add_argument("--poll-sec", type=float, default=0.02)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    deadline = time.monotonic() + max(0.1, args.timeout_sec)
    observations: list[dict[str, Any]] = []
    first_visible: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        observations = observe_windows(args.title_contains)
        first_visible = next(
            (
                item
                for item in observations
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
        "endpoint": "overlay_first_visible_frame",
        "qpcFrequency": qpc_frequency(),
        "firstVisible": first_visible,
        "observations": observations,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        path = Path(args.output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
