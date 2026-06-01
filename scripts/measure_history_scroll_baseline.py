from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "Frontend"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(((pct / 100.0) * len(ordered) + 0.999999) - 1)))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3),
        "p50": round(percentile(values, 50.0), 3),
        "p95": round(percentile(values, 95.0), 3),
        "max": round(max(values), 3),
    }


def wait_http(url: str, *, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= int(response.status) < 500:
                    return
        except Exception as exc:  # pragma: no cover - exercised by external process waits
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def browser_candidates(requested: str = "") -> list[str]:
    if requested:
        return [requested]

    candidates: list[str] = []
    if sys.platform.startswith("win"):
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for root in program_files:
            if not root:
                continue
            candidates.extend(
                [
                    str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                    str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                ]
            )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )

    candidates.extend(
        name
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "msedge",
            "microsoft-edge",
            "chrome",
        )
        if shutil.which(name)
    )
    return candidates


def resolve_browser_path(requested: str = "") -> str:
    for candidate in browser_candidates(requested):
        path = shutil.which(candidate) or candidate
        if Path(path).exists() or shutil.which(path):
            return path
    raise RuntimeError(
        "No Chrome/Edge/Chromium executable found. Pass --browser with an explicit path."
    )


def npm_executable() -> str:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("npm was not found on PATH.")
    return npm


def process_creation_flags() -> int:
    if sys.platform.startswith("win"):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def transcript_item(transcript_type: str, index: int) -> dict[str, Any]:
    title_prefix = {
        "mic": "Synthetic Recording",
        "file": "Synthetic File",
        "youtube": "Synthetic Video",
    }.get(transcript_type, "Synthetic Transcript")
    duration = f"{1 + (index % 58):02d}:{(index * 7) % 60:02d}"
    item: dict[str, Any] = {
        "id": f"{transcript_type}-{index:05d}",
        "title": f"{title_prefix} {index + 1:05d}",
        "date": "2026-06-01",
        "duration": duration,
        "status": "completed",
        "type": transcript_type,
        "language": "de",
        "channel": "Benchmark Channel",
        "fileSize": "1 MB",
        "step": "Done",
    }
    if transcript_type == "youtube":
        item.update(
            {
                "channelTitle": "Benchmark Channel",
                "thumbnailUrl": "",
                "viewCount": index * 10,
                "likeCount": index,
            }
        )
    return item


class MockBackend:
    def __init__(self, *, port: int, item_count: int) -> None:
        self.port = port
        self.item_count = item_count
        self.runner: web.AppRunner | None = None
        self.request_log: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reset_log(self) -> None:
        self.request_log.clear()

    async def start(self) -> None:
        @web.middleware
        async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
            if request.method == "OPTIONS":
                response: web.StreamResponse = web.Response()
            else:
                response = await handler(request)

            origin = request.headers.get("Origin")
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Scriber-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            return response

        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/settings", self.settings)
        app.router.add_get("/api/transcripts", self.transcripts)
        app.router.add_get("/ws", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "ready": True,
                "apiVersion": "1",
                "runtimeMode": "history-scroll-baseline",
            }
        )

    async def settings(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "fileUploadLimits": {
                    "compressionThresholdBytes": 50 * 1024 * 1024,
                    "compressionThresholdLabel": "50MB",
                    "providerLabel": "Synthetic",
                    "audioMaxLabel": "2GB",
                    "rawAudioIngestMaxLabel": "2GB",
                    "videoMaxLabel": "2GB",
                    "usesDirectProviderLimit": False,
                }
            }
        )

    async def transcripts(self, request: web.Request) -> web.Response:
        transcript_type = request.query.get("type", "mic").strip() or "mic"
        offset = max(0, int(request.query.get("offset", "0") or "0"))
        limit = max(1, min(100, int(request.query.get("limit", "50") or "50")))
        query = (request.query.get("q", "") or "").strip().lower()

        matching_indexes = range(self.item_count)
        if query:
            matching_indexes = [
                index
                for index in matching_indexes
                if query in transcript_item(transcript_type, index)["title"].lower()
            ]
        else:
            matching_indexes = list(matching_indexes)

        total = len(matching_indexes)
        page_indexes = matching_indexes[offset : offset + limit]
        items = [transcript_item(transcript_type, index) for index in page_indexes]
        self.request_log.append(
            {
                "type": transcript_type,
                "offset": offset,
                "limit": limit,
                "returned": len(items),
                "total": total,
            }
        )
        return web.json_response(
            {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "hasMore": offset + len(items) < total,
            }
        )

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(
            {
                "apiVersion": "1",
                "type": "state",
                "listening": False,
                "status": "Stopped",
                "current": None,
                "backgroundProcessing": False,
                "recordingState": "idle",
                "transcribing": False,
            }
        )
        async for message in ws:
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
        return ws


class CdpClient:
    def __init__(self, session: Any, ws: Any) -> None:
        self.session = session
        self.ws = ws
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.receiver_task = asyncio.create_task(self._receiver())

    @classmethod
    async def connect(cls, websocket_url: str) -> "CdpClient":
        import aiohttp

        session = aiohttp.ClientSession()
        ws = await session.ws_connect(websocket_url, heartbeat=20)
        return cls(session, ws)

    async def close(self) -> None:
        self.receiver_task.cancel()
        await self.ws.close()
        await self.session.close()

    async def _receiver(self) -> None:
        async for message in self.ws:
            if message.type != WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            response_id = payload.get("id")
            if response_id in self.pending:
                future = self.pending.pop(response_id)
                if not future.done():
                    future.set_result(payload)

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
        self.next_id += 1
        message_id = self.next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[message_id] = future
        await self.ws.send_json({"id": message_id, "method": method, "params": params or {}})
        response = await asyncio.wait_for(future, timeout=timeout)
        if "error" in response:
            raise RuntimeError(f"CDP {method} failed: {response['error']}")
        return response.get("result", {})

    async def evaluate(self, expression: str, *, timeout: float = 20.0) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 5,
        )
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise RuntimeError(str(value))
        return value.get("value")


def read_json_url(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


async def connect_to_browser(debug_port: int) -> CdpClient:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pages = read_json_url(f"http://127.0.0.1:{debug_port}/json/list")
            page = next((item for item in pages if item.get("type") == "page"), None)
            if page and page.get("webSocketDebuggerUrl"):
                cdp = await CdpClient.connect(page["webSocketDebuggerUrl"])
                await cdp.call("Page.enable")
                await cdp.call("Runtime.enable")
                return cdp
        except Exception as exc:  # pragma: no cover - exercised by external browser startup
            last_error = exc
            await asyncio.sleep(0.2)
    raise RuntimeError(f"Timed out connecting to browser CDP: {last_error}")


async def wait_for_history_ready(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    expression = """
(() => {
  const text = document.body ? document.body.innerText : "";
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ready: !!root && document.querySelectorAll('.perf-scroll-item').length > 0,
    virtualized: !!root,
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    failed: /Could not load|Backend Not Available|No recordings match|No files match|No videos match/.test(text),
    bodyText: text.slice(0, 500)
  };
})()
"""
    while time.monotonic() < deadline:
        state = await cdp.evaluate(expression, timeout=5)
        last_state = state or {}
        if last_state.get("ready"):
            return last_state
        if last_state.get("failed"):
            raise RuntimeError(f"History page failed to load: {last_state.get('bodyText', '')}")
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for virtualized history list. Last state: {last_state}")


async def measure_scroll(cdp: CdpClient, args: argparse.Namespace) -> dict[str, Any]:
    config = {
        "maxSteps": args.max_steps,
        "stepPx": args.step_px,
        "intervalMs": args.interval_ms,
        "stableBottomSteps": args.stable_bottom_steps,
        "settleMs": args.settle_ms,
    }
    expression = f"""
(async () => {{
  const config = {json.dumps(config)};
  return await new Promise((resolve) => {{
    let running = true;
    let frameCount = 0;
    let longFrameCount = 0;
    let maxFrameGapMs = 0;
    let lastFrame = performance.now();
    const visibleSamples = [];

    function visibleCards() {{
      return document.querySelectorAll('.perf-scroll-item').length;
    }}

    function scrollRoot() {{
      return document.querySelector('[data-app-scroll-container]') ||
        document.scrollingElement ||
        document.documentElement;
    }}

    function frame(now) {{
      const gap = now - lastFrame;
      if (frameCount > 0) {{
        maxFrameGapMs = Math.max(maxFrameGapMs, gap);
        if (gap > 50) longFrameCount += 1;
      }}
      lastFrame = now;
      frameCount += 1;
      if (running) requestAnimationFrame(frame);
    }}

    let steps = 0;
    let stableAtBottom = 0;
    let lastHeight = 0;
    const started = performance.now();
    requestAnimationFrame(frame);

    function step() {{
      const root = scrollRoot();
      root.scrollTop += config.stepPx;
      const height = root.scrollHeight;
      const viewportHeight = root.clientHeight || window.innerHeight;
      const atBottom = Math.ceil(root.scrollTop + viewportHeight) >= height - 4;
      const cards = visibleCards();
      visibleSamples.push(cards);
      if (atBottom && Math.abs(height - lastHeight) < 2) {{
        stableAtBottom += 1;
      }} else {{
        stableAtBottom = 0;
      }}
      lastHeight = height;
      steps += 1;

      if (steps >= config.maxSteps || stableAtBottom >= config.stableBottomSteps) {{
        setTimeout(() => {{
          running = false;
          const durationMs = performance.now() - started;
          const visibleNow = visibleCards();
          visibleSamples.push(visibleNow);
          resolve({{
            durationMs,
            steps,
            frameCount,
            longFrameCount,
            maxFrameGapMs,
            visibleCards: visibleNow,
            maxVisibleCards: Math.max(...visibleSamples),
            meanVisibleCards: visibleSamples.reduce((a, b) => a + b, 0) / Math.max(1, visibleSamples.length),
            scrollY: scrollRoot().scrollTop,
            scrollHeight: scrollRoot().scrollHeight,
            virtualized: !!document.querySelector('[data-history-virtualized="true"]')
          }});
        }}, config.settleMs);
        return;
      }}

      setTimeout(step, config.intervalMs);
    }}

    step();
  }});
}})()
"""
    return await cdp.evaluate(expression, timeout=max(30, args.max_steps * max(1, args.interval_ms) / 1000 + 15))


async def run_scenario(
    cdp: CdpClient,
    backend: MockBackend,
    *,
    frontend_base_url: str,
    route: str,
    view: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    backend.reset_log()
    separator = "&" if "?" in route else "?"
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}{route}{separator}view={view}"}, timeout=10)
    await wait_for_history_ready(cdp, timeout_sec=args.page_timeout_sec)
    scroll = await measure_scroll(cdp, args)
    request_log = list(backend.request_log)
    returned_items = sum(int(entry["returned"]) for entry in request_log)
    offsets = [int(entry["offset"]) for entry in request_log]
    max_visible_cards = int(round(float(scroll.get("maxVisibleCards") or 0)))
    ok = (
        bool(scroll.get("virtualized"))
        and max_visible_cards < args.items
        and len(request_log) >= 1
        and returned_items > 0
    )
    return {
        "route": route,
        "view": view,
        "ok": ok,
        "itemCount": args.items,
        "durationMs": round(float(scroll.get("durationMs") or 0), 3),
        "steps": int(scroll.get("steps") or 0),
        "frameCount": int(scroll.get("frameCount") or 0),
        "longFrameCount": int(scroll.get("longFrameCount") or 0),
        "maxFrameGapMs": round(float(scroll.get("maxFrameGapMs") or 0), 3),
        "visibleCards": int(round(float(scroll.get("visibleCards") or 0))),
        "maxVisibleCards": max_visible_cards,
        "meanVisibleCards": round(float(scroll.get("meanVisibleCards") or 0), 3),
        "scrollY": round(float(scroll.get("scrollY") or 0), 3),
        "scrollHeight": round(float(scroll.get("scrollHeight") or 0), 3),
        "apiRequestCount": len(request_log),
        "apiOffsets": offsets,
        "returnedItems": returned_items,
        "virtualized": bool(scroll.get("virtualized")),
    }


def start_vite(frontend_port: int, backend_url: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["VITE_BACKEND_URL"] = backend_url
    env["BROWSER"] = "none"
    return subprocess.Popen(
        [
            npm_executable(),
            "run",
            "dev:client",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
        ],
        cwd=FRONTEND_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=process_creation_flags(),
    )


def start_browser(browser_path: str, debug_port: int, profile_dir: Path, *, headed: bool) -> subprocess.Popen[str]:
    args = [
        browser_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
    ]
    if not headed:
        args.append("--headless=new")
    if not sys.platform.startswith("win"):
        args.append("--no-sandbox")
    args.append("about:blank")
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=process_creation_flags(),
    )


async def run_browser_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    backend_port = find_free_port()
    frontend_port = find_free_port()
    debug_port = find_free_port()
    backend = MockBackend(port=backend_port, item_count=args.items)
    vite: subprocess.Popen[str] | None = None
    browser: subprocess.Popen[str] | None = None
    cdp: CdpClient | None = None

    with tempfile.TemporaryDirectory(prefix="scriber-history-scroll-") as temp_dir:
        profile_dir = Path(temp_dir) / "browser-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        await backend.start()
        try:
            vite = start_vite(frontend_port, backend.base_url)
            wait_http(f"http://127.0.0.1:{frontend_port}/", timeout_sec=args.startup_timeout_sec)

            browser_path = resolve_browser_path(args.browser)
            browser = start_browser(browser_path, debug_port, profile_dir, headed=args.headed)
            cdp = await connect_to_browser(debug_port)

            frontend_base_url = f"http://127.0.0.1:{frontend_port}"
            scenarios: list[dict[str, Any]] = []
            for route in args.routes:
                for view in args.views:
                    scenarios.append(
                        await run_scenario(
                            cdp,
                            backend,
                            frontend_base_url=frontend_base_url,
                            route=route,
                            view=view,
                            args=args,
                        )
                    )
        finally:
            if cdp:
                await cdp.close()
            if browser:
                terminate_process(browser)
            if vite:
                terminate_process(vite)
            await backend.close()

    return build_result(args, scenarios)


def build_result(args: argparse.Namespace, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item["durationMs"]) for item in scenarios]
    frame_gaps = [float(item["maxFrameGapMs"]) for item in scenarios]
    max_visible_cards = max((int(item["maxVisibleCards"]) for item in scenarios), default=0)
    total_requests = sum(int(item["apiRequestCount"]) for item in scenarios)
    ok = bool(scenarios) and all(bool(item["ok"]) for item in scenarios)
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": ok,
        "summary": {
            "scenarioCount": len(scenarios),
            "itemCount": args.items,
            "routes": args.routes,
            "views": args.views,
            "durationMs": summarize(durations),
            "maxFrameGapMs": summarize(frame_gaps),
            "maxVisibleCards": max_visible_cards,
            "totalApiRequests": total_requests,
            "virtualized": all(bool(item.get("virtualized")) for item in scenarios),
        },
        "scenarios": scenarios,
    }


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure frontend transcript history scrolling with synthetic paginated data."
    )
    parser.add_argument("--items", type=int, default=2000)
    parser.add_argument("--routes", default="/")
    parser.add_argument("--views", default="list,grid")
    parser.add_argument("--browser", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--startup-timeout-sec", type=float, default=30.0)
    parser.add_argument("--page-timeout-sec", type=float, default=20.0)
    parser.add_argument("--max-steps", type=int, default=700)
    parser.add_argument("--step-px", type=int, default=700)
    parser.add_argument("--interval-ms", type=int, default=16)
    parser.add_argument("--stable-bottom-steps", type=int, default=45)
    parser.add_argument("--settle-ms", type=int, default=250)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    args.items = max(1, int(args.items))
    args.routes = parse_csv(args.routes)
    args.views = parse_csv(args.views)
    if not args.routes:
        args.routes = ["/"]
    if not args.views:
        args.views = ["list"]
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.validate_only:
        result = build_result(
            args,
            [
                {
                    "route": route,
                    "view": view,
                    "ok": True,
                    "itemCount": args.items,
                    "durationMs": 0.0,
                    "steps": 0,
                    "frameCount": 0,
                    "longFrameCount": 0,
                    "maxFrameGapMs": 0.0,
                    "visibleCards": 0,
                    "maxVisibleCards": 0,
                    "meanVisibleCards": 0.0,
                    "scrollY": 0.0,
                    "scrollHeight": 0.0,
                    "apiRequestCount": 0,
                    "apiOffsets": [],
                    "returnedItems": 0,
                    "virtualized": True,
                    "validateOnly": True,
                }
                for route in args.routes
                for view in args.views
            ],
        )
    else:
        result = asyncio.run(run_browser_benchmark(args))
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
