from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_history_scroll_baseline import (
    CdpClient,
    connect_to_browser,
    find_free_port,
    resolve_browser_path,
    start_browser,
    start_vite,
    terminate_process as terminate_process_parent,
    transcript_item,
    wait_http,
)


ROUTE_EXPECTATIONS: dict[str, list[str]] = {
    "/": ["Live Transcription", "Recent Recordings"],
    "/youtube": ["Youtube Transcription", "Recent Videos"],
    "/file": ["Import File", "Recent Files"],
    "/debug": [
        "Debug Console",
        "ui-debug-sample.log",
        "Debug console sample error",
        "Copy visible",
        "Support bundle",
        "Auto scroll",
        "Newest first",
    ],
    "/settings": ["Settings", "Transcription Settings", "API Configuration"],
    "/transcript/mic-00001": ["Synthetic Recording 00002", "Summary", "Transcript"],
    "/transcript/youtube-processing-smoke": [
        "Synthetic YouTube Processing",
        "Synthetic completed summary after YouTube processing.",
        "Transcript",
    ],
}


def terminate_process_tree(process: Any) -> None:
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_process_parent(process)
        return
    terminate_process_parent(process)


class FrontendSmokeBackend:
    def __init__(self, *, port: int, item_count: int) -> None:
        self.port = port
        self.item_count = item_count
        self.runner: web.AppRunner | None = None
        self.request_log: list[dict[str, Any]] = []
        self.settings = self._default_settings()
        self.websockets: set[web.WebSocketResponse] = set()
        self.session_token_required = False
        self.session_token = "smoke-session-token"
        self.transcript_detail_counts: dict[str, int] = {}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        @web.middleware
        async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
            if request.method == "OPTIONS":
                response: web.StreamResponse = web.Response()
            elif self._requires_session_token(request) and not self._has_valid_session_token(request):
                response = web.json_response({"message": "Session token required"}, status=401)
            else:
                response = await handler(request)

            origin = request.headers.get("Origin")
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Scriber-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            return response

        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/settings", self.get_settings)
        app.router.add_put("/api/settings", self.put_settings)
        app.router.add_get("/api/autostart", self.autostart)
        app.router.add_post("/api/autostart", self.autostart)
        app.router.add_get("/api/microphones", self.microphones)
        app.router.add_post("/api/microphones/refresh", self.microphones)
        app.router.add_get("/api/runtime", self.runtime)
        app.router.add_post("/api/runtime/frontend-ready", self.frontend_ready)
        app.router.add_get("/api/onnx/models", self.local_models)
        app.router.add_get("/api/nemo/models", self.local_models)
        app.router.add_get("/api/youtube/search", self.youtube_search)
        app.router.add_get("/api/youtube/video", self.youtube_video)
        app.router.add_get("/api/youtube/thumbnail", self.youtube_thumbnail)
        app.router.add_post("/api/youtube/transcribe", self.youtube_transcribe)
        app.router.add_post("/api/file/transcribe", self.file_transcribe)
        app.router.add_get("/api/runtime/logs", self.runtime_logs)
        app.router.add_post("/api/runtime/support-bundle", self.support_bundle)
        app.router.add_get("/api/transcripts", self.transcripts)
        app.router.add_get("/api/transcripts/{transcript_id}", self.transcript_detail)
        app.router.add_delete("/api/transcripts/{transcript_id}", self.delete_transcript)
        app.router.add_post("/api/transcripts/{transcript_id}/cancel", self.ok_response)
        app.router.add_post("/api/transcripts/{transcript_id}/summarize", self.summarize_transcript)
        app.router.add_get("/ws", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()

    def _requires_session_token(self, request: web.Request) -> bool:
        if not self.session_token_required:
            return False
        if request.path == "/api/health":
            return False
        return request.path == "/ws" or request.path.startswith("/api/")

    def _has_valid_session_token(self, request: web.Request) -> bool:
        return (
            request.query.get("scriberToken") == self.session_token
            or request.headers.get("X-Scriber-Token") == self.session_token
        )

    async def close(self) -> None:
        for ws in tuple(self.websockets):
            with suppress(Exception):
                await ws.close()
        self.websockets.clear()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def broadcast_history_updated(self) -> None:
        stale: list[web.WebSocketResponse] = []
        for ws in tuple(self.websockets):
            try:
                await ws.send_json({"apiVersion": "1", "type": "history_updated"})
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.websockets.discard(ws)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "ready": True,
                "apiVersion": "1",
                "runtimeMode": "frontend-browser-smoke",
                "workerVersion": "0.1.0",
            }
        )

    async def runtime(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "ready": True,
                "apiVersion": "1",
                "runtimeMode": "frontend-browser-smoke",
                "featureFlags": {"sessionTokenRequired": self.session_token_required},
            }
        )

    async def frontend_ready(self, request: web.Request) -> web.Response:
        return web.json_response({"apiVersion": "1", "ready": True})

    async def get_settings(self, request: web.Request) -> web.Response:
        return web.json_response(self.settings)

    async def put_settings(self, request: web.Request) -> web.Response:
        patch = await request.json()
        if isinstance(patch, dict):
            self.settings.update(patch)
        return web.json_response(self.settings)

    async def autostart(self, request: web.Request) -> web.Response:
        return web.json_response({"enabled": False, "available": False})

    async def microphones(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "devices": [
                    {"deviceId": "default", "label": "Default Microphone"},
                    {"deviceId": "usb-smoke-mic", "label": "USB Smoke Microphone"},
                ],
                "favoriteMicRestored": False,
            }
        )

    async def local_models(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "available": False,
                "message": "Local model downloads are disabled in frontend smoke.",
                "models": [],
                "currentModel": "",
                "quantization": "int8",
            }
        )

    async def youtube_search(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "items": [
                    {
                        "videoId": "video-smoke-1",
                        "title": "Synthetic YouTube Result",
                        "channelTitle": "Smoke Channel",
                        "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                        "duration": "04:20",
                        "publishedAt": "2026-06-01T12:00:00Z",
                        "viewCount": 1234,
                        "likeCount": 56,
                    }
                ]
            }
        )

    async def youtube_video(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "videoId": "video-smoke-1",
                "title": "Synthetic YouTube URL Result",
                "channelTitle": "Smoke Channel",
                "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                "duration": "04:20",
                "publishedAt": "2026-06-01T12:00:00Z",
                "viewCount": 1234,
                "likeCount": 56,
            }
        )

    async def youtube_thumbnail(self, request: web.Request) -> web.Response:
        self.request_log.append({"path": "/api/youtube/thumbnail", "url": request.query.get("url", "")})
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90" viewBox="0 0 160 90">'
            '<rect width="160" height="90" fill="#2f6fed"/>'
            '<circle cx="80" cy="45" r="24" fill="#ffffff" opacity="0.9"/>'
            '<path d="M73 32v26l24-13z" fill="#2f6fed"/>'
            "</svg>"
        )
        return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml")

    async def youtube_transcribe(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "id": "youtube-queued-smoke",
                "message": "Synthetic transcription queued.",
            }
        )

    async def file_transcribe(self, request: web.Request) -> web.Response:
        self.request_log.append({"path": "/api/file/transcribe"})
        with suppress(Exception):
            await request.post()
        return web.json_response(
            {
                "id": "file-upload-smoke",
                "title": "Synthetic File Upload",
                "date": "Today, 12:03",
                "duration": "00:01",
                "status": "processing",
                "type": "file",
                "language": "auto",
                "step": "Queued",
            }
        )

    async def runtime_logs(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "apiVersion": "1",
                "items": [
                    {
                        "source": "ui-debug-sample.log",
                        "line": 1,
                        "level": "INFO",
                        "message": "Debug console sample info",
                        "timestamp": "12:01:00.100",
                        "timestampMs": None,
                        "component": "web_api",
                    },
                    {
                        "source": "ui-debug-sample.log",
                        "line": 2,
                        "level": "WARNING",
                        "message": "Debug console sample warning",
                        "timestamp": "12:01:01.200",
                        "timestampMs": None,
                        "component": "web_api",
                    },
                    {
                        "source": "ui-debug-sample.log",
                        "line": 3,
                        "level": "ERROR",
                        "message": "Debug console sample error OPENAI_API_KEY=[REDACTED]",
                        "timestamp": "12:01:02.300",
                        "timestampMs": None,
                        "component": "web_api",
                    },
                ],
                "sources": ["ui-debug-sample.log"],
                "limit": 900,
                "truncated": False,
            }
        )

    async def support_bundle(self, request: web.Request) -> web.Response:
        return web.Response(
            body=b"PK\x03\x04synthetic-support-bundle",
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="synthetic-support-bundle.zip"'},
        )

    async def transcripts(self, request: web.Request) -> web.Response:
        transcript_type = request.query.get("type", "mic").strip() or "mic"
        offset = max(0, int(request.query.get("offset", "0") or "0"))
        limit = max(1, min(100, int(request.query.get("limit", "50") or "50")))
        query = (request.query.get("q", "") or "").strip().lower()

        indexes: list[int] = list(range(self.item_count))
        if query:
            indexes = [
                index
                for index in indexes
                if query in transcript_item(transcript_type, index)["title"].lower()
            ]

        total = len(indexes)
        page_indexes = indexes[offset : offset + limit]
        items = [transcript_item(transcript_type, index) for index in page_indexes]
        self.request_log.append(
            {
                "path": "/api/transcripts",
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

    async def transcript_detail(self, request: web.Request) -> web.Response:
        transcript_id = request.match_info["transcript_id"]
        if transcript_id == "youtube-processing-smoke":
            count = self.transcript_detail_counts.get(transcript_id, 0) + 1
            self.transcript_detail_counts[transcript_id] = count
            created_at = "2026-06-01T12:00:00Z"
            if count == 1:
                asyncio.get_running_loop().call_later(
                    0.2,
                    lambda: asyncio.create_task(self.broadcast_history_updated()),
                )
                return web.json_response(
                    {
                        "id": transcript_id,
                        "title": "Synthetic YouTube Processing",
                        "date": "Today, 12:00",
                        "duration": "04:20",
                        "status": "processing",
                        "type": "youtube",
                        "language": "auto",
                        "step": "Download complete",
                        "sourceUrl": "https://www.youtube.com/watch?v=0wEjbSYNUM8",
                        "channel": "Smoke Channel",
                        "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                        "content": "",
                        "summary": "",
                        "summaryStatus": "pending",
                        "summaryError": "",
                        "summaryUpdatedAt": "",
                        "createdAt": created_at,
                        "updatedAt": "2026-06-01T12:00:10Z",
                    }
                )
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic YouTube Processing",
                    "date": "Today, 12:00",
                    "duration": "04:20",
                    "status": "completed",
                    "type": "youtube",
                    "language": "auto",
                    "step": "Completed",
                    "sourceUrl": "https://www.youtube.com/watch?v=0wEjbSYNUM8",
                    "channel": "Smoke Channel",
                    "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                    "content": "Speaker 1: Synthetic completed YouTube transcript.",
                    "summary": "Synthetic completed summary after YouTube processing.",
                    "summaryStatus": "completed",
                    "summaryError": "",
                    "summaryUpdatedAt": "2026-06-01T12:00:30Z",
                    "createdAt": created_at,
                    "updatedAt": "2026-06-01T12:00:30Z",
                }
            )
        kind = transcript_id.split("-", maxsplit=1)[0] if "-" in transcript_id else "mic"
        index = 1
        try:
            index = int(transcript_id.rsplit("-", maxsplit=1)[1])
        except Exception:
            pass
        item = transcript_item(kind, index)
        item.update(
            {
                "content": "Speaker 1: This is a synthetic transcript used by the frontend browser smoke test.",
                "summary": "Synthetic summary for browser smoke.",
                "createdAt": "2026-06-01T12:00:00Z",
                "updatedAt": "2026-06-01T12:05:00Z",
            }
        )
        return web.json_response(item)

    async def delete_transcript(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True})

    async def summarize_transcript(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True, "summary": "Synthetic summary for browser smoke."})

    async def ok_response(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True})

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.websockets.add(ws)
        try:
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
        except ConnectionResetError:
            pass
        finally:
            self.websockets.discard(ws)
        return ws

    @staticmethod
    def _default_settings() -> dict[str, Any]:
        return {
            "hotkey": "ctrl+alt+s",
            "hotkeyRaw": "ctrl+alt+s",
            "mode": "toggle",
            "micDevice": "default",
            "favoriteMic": "",
            "language": "auto",
            "defaultSttService": "soniox",
            "sonioxMode": "realtime",
            "customVocab": "",
            "summarizationPrompt": "",
            "summarizationModel": "gemini-2.5-flash",
            "autoSummarize": False,
            "visualizerBarCount": 45,
            "micAlwaysOn": False,
            "onnxModel": "",
            "nemoModel": "",
            "apiKeys": {},
            "fileUploadLimits": {
                "compressionThresholdBytes": 50 * 1024 * 1024,
                "compressionThresholdLabel": "50MB",
                "providerLabel": "Synthetic",
                "audioMaxLabel": "2GB",
                "rawAudioIngestMaxLabel": "2GB",
                "videoMaxLabel": "2GB",
                "usesDirectProviderLimit": False,
            },
        }


async def install_page_error_capture(cdp: CdpClient) -> None:
    source = r"""
(() => {
  window.__scriberSmoke = { consoleErrors: [], pageErrors: [], unhandledRejections: [] };
  const originalError = console.error.bind(console);
  console.error = (...args) => {
    window.__scriberSmoke.consoleErrors.push(args.map((arg) => String(arg)).join(" "));
    originalError(...args);
  };
  window.addEventListener("error", (event) => {
    window.__scriberSmoke.pageErrors.push(String(event.message || event.error || ""));
  });
  window.addEventListener("unhandledrejection", (event) => {
    window.__scriberSmoke.unhandledRejections.push(String(event.reason || ""));
  });
})();
"""
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})


async def wait_for_route_ready(
    cdp: CdpClient,
    *,
    route: str,
    expected_text: list[str],
    expect_history_virtualized: bool,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    expectation = json.dumps(expected_text)
    expression = f"""
(() => {{
  const expected = {expectation};
  const text = document.body ? document.body.innerText : "";
  const smoke = window.__scriberSmoke || {{}};
  const missing = expected.filter((item) => !text.includes(item));
  const hasOfflineBanner = text.includes("Backend Not Available");
  const hasQueryError = /Could not load|Failed to load|Please retry loading/.test(text);
  const historyRoot = document.querySelector('[data-history-virtualized="true"]');
  const result = {{
    ready: document.readyState === "complete" && missing.length === 0 && !hasOfflineBanner && !hasQueryError && ({str(expect_history_virtualized).lower()} ? !!historyRoot : true),
    route: window.location.pathname,
    missing,
    hasOfflineBanner,
    hasQueryError,
    bodyText: text.slice(0, 1000),
    title: document.title,
    historyVirtualized: !!historyRoot,
    visibleHistoryCards: document.querySelectorAll('.perf-scroll-item').length,
    consoleErrors: smoke.consoleErrors || [],
    pageErrors: smoke.pageErrors || [],
    unhandledRejections: smoke.unhandledRejections || []
  }};
  return result;
}})()
"""
    while time.monotonic() < deadline:
        state = await cdp.evaluate(expression, timeout=5)
        last_state = state or {}
        if last_state.get("ready"):
            return last_state
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for route {route}. Last state: {last_state}")


async def inspect_route(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    route: str,
    timeout_sec: float,
) -> dict[str, Any]:
    expected = ROUTE_EXPECTATIONS[route]
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}{route}"}, timeout=10)
    expect_history_virtualized = route in {"/", "/youtube", "/file"}
    state = await wait_for_route_ready(
        cdp,
        route=route,
        expected_text=expected,
        expect_history_virtualized=expect_history_virtualized,
        timeout_sec=timeout_sec,
    )
    critical_console_errors = [
        message
        for message in state.get("consoleErrors", [])
        if "WebSocket error" not in message and "ResizeObserver loop" not in message
    ]
    ok = (
        (not expect_history_virtualized or bool(state.get("historyVirtualized")))
        and not critical_console_errors
        and not state.get("pageErrors")
        and not state.get("unhandledRejections")
    )
    return {
        "route": route,
        "ok": ok,
        "expectedText": expected,
        "historyVirtualized": bool(state.get("historyVirtualized")),
        "visibleHistoryCards": int(state.get("visibleHistoryCards") or 0),
        "consoleErrors": critical_console_errors,
        "pageErrors": state.get("pageErrors", []),
        "unhandledRejections": state.get("unhandledRejections", []),
    }


async def wait_for_interaction_state(
    cdp: CdpClient,
    *,
    label: str,
    expression: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = await cdp.evaluate(expression, timeout=5)
        last_state = state or {}
        if last_state.get("ok"):
            return last_state
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for interaction '{label}'. Last state: {last_state}")


async def exercise_youtube_interactions(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    start_search = r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Youtube'));
  const button = document.querySelector('button[aria-label="Search YouTube"]');
  if (!input || !button) return { ok: false, reason: 'missing input/button' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'Gestaltung Stiftung');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  button.click();
  return { ok: true };
})()
"""
    search_started = await cdp.evaluate(start_search, timeout=5)
    if not search_started or not search_started.get("ok"):
        raise RuntimeError(f"Could not start YouTube search interaction: {search_started}")

    search_state = await wait_for_interaction_state(
        cdp,
        label="youtube-search-thumbnail",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const thumbnails = Array.from(document.images)
    .filter((img) => img.src.includes('/api/youtube/thumbnail'));
  const loaded = thumbnails.filter((img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
  return {
    ok: text.includes('Synthetic YouTube Result') && loaded.length > 0,
    resultVisible: text.includes('Synthetic YouTube Result'),
    thumbnailCount: thumbnails.length,
    loadedThumbnailCount: loaded.length
  };
})()
""",
    )

    start_url_lookup = r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Youtube'));
  const button = document.querySelector('button[aria-label="Search YouTube"]');
  if (!input || !button) return { ok: false, reason: 'missing input/button' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'https://www.youtube.com/watch?v=0wEjbSYNUM8');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  button.click();
  return { ok: true };
})()
"""
    url_lookup_started = await cdp.evaluate(start_url_lookup, timeout=5)
    if not url_lookup_started or not url_lookup_started.get("ok"):
        raise RuntimeError(f"Could not start YouTube URL lookup interaction: {url_lookup_started}")

    url_state = await wait_for_interaction_state(
        cdp,
        label="youtube-url-thumbnail",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const thumbnails = Array.from(document.images)
    .filter((img) => img.src.includes('/api/youtube/thumbnail'));
  const loaded = thumbnails.filter((img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
  return {
    ok: text.includes('Synthetic YouTube URL Result') && loaded.length > 0,
    resultVisible: text.includes('Synthetic YouTube URL Result'),
    thumbnailCount: thumbnails.length,
    loadedThumbnailCount: loaded.length
  };
})()
""",
    )

    return {"name": "youtube-thumbnails", "ok": True, "search": search_state, "url": url_state}


async def exercise_file_drop_interaction(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    drop_started = await cdp.evaluate(
        r"""
(() => {
  const dropzone = document.querySelector('[aria-label="Upload file for transcription"]');
  if (!dropzone) return { ok: false, reason: 'missing dropzone' };
  const file = new File([new Uint8Array([82, 73, 70, 70])], 'smoke.wav', { type: 'audio/wav' });
  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);
  dropzone.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer }));
  dropzone.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer }));
  dropzone.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not drop_started or not drop_started.get("ok"):
        raise RuntimeError(f"Could not dispatch file drop interaction: {drop_started}")

    state = await wait_for_interaction_state(
        cdp,
        label="file-drop-upload",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: window.location.pathname === '/transcript/file-upload-smoke' && text.includes('Summary') && text.includes('Transcript'),
    route: window.location.pathname,
    hasSummary: text.includes('Summary'),
    hasTranscript: text.includes('Transcript')
  };
})()
""",
    )
    return {"name": "file-drag-drop", "ok": True, "state": state}


async def exercise_debug_console_interaction(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    initial = await cdp.evaluate(
        r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const stickyRoot = document.querySelector('.sticky');
  const dateInput = document.querySelector('input[type="date"]');
  const newestSwitch = document.querySelector('[aria-label="Show newest logs first"]');
  const today = new Date().toISOString().slice(0, 10);
  return {
    ok: text.includes('Debug console sample error'),
    sticky: stickyRoot ? getComputedStyle(stickyRoot).position === 'sticky' : false,
    dateFilterToday: dateInput ? dateInput.value === today : false,
    newestFirst: newestSwitch ? newestSwitch.getAttribute('aria-checked') === 'true' : false,
    hasErrorLog: text.includes('Debug console sample error')
  };
})()
""",
        timeout=5,
    )
    if not initial or not initial.get("ok"):
        raise RuntimeError(f"Debug console did not render sample logs: {initial}")

    clear_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.textContent || '').includes('Clear view'));
  if (!button) return { ok: false, reason: 'missing clear button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not clear_started or not clear_started.get("ok"):
        raise RuntimeError(f"Could not click debug clear button: {clear_started}")

    cleared = await wait_for_interaction_state(
        cdp,
        label="debug-clear",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('No matching log entries.') && text.includes('Cleared') && !text.includes('Debug console sample error'),
    hasEmptyState: text.includes('No matching log entries.'),
    hasActionStatus: text.includes('Cleared'),
    errorLogStillVisible: text.includes('Debug console sample error')
  };
})()
""",
    )
    return {"name": "debug-clear", "ok": True, "initial": initial, "cleared": cleared}


async def exercise_transcript_processing_refresh(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    state = await wait_for_interaction_state(
        cdp,
        label="transcript-processing-refresh",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('Synthetic completed summary after YouTube processing.')
      && !text.includes('Download complete')
      && !text.includes('Elapsed:'),
    hasCompletedSummary: text.includes('Synthetic completed summary after YouTube processing.'),
    hasStaleDownloadStep: text.includes('Download complete'),
    hasProcessingElapsed: text.includes('Elapsed:'),
    route: window.location.pathname
  };
})()
""",
    )
    return {"name": "transcript-processing-refresh", "ok": True, "state": state}


async def inspect_token_required_browser_state(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/"}, timeout=10)
    state = await wait_for_interaction_state(
        cdp,
        label="token-required-browser-state",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const smoke = window.__scriberSmoke || {};
  const websocketErrors = (smoke.consoleErrors || [])
    .filter((message) => String(message).includes('WebSocket error'));
  return {
    ok: text.includes('Backend Not Available') && text.includes('desktop session token') && websocketErrors.length <= 2,
    hasOfflineBanner: text.includes('Backend Not Available'),
    hasTokenMessage: text.includes('desktop session token'),
    websocketErrorCount: websocketErrors.length,
    consoleErrors: smoke.consoleErrors || []
  };
})()
""",
    )
    return {"name": "token-required-browser-state", "ok": True, "state": state}


async def run_browser_smoke(args: argparse.Namespace) -> dict[str, Any]:
    backend_port = find_free_port()
    frontend_port = find_free_port()
    debug_port = find_free_port()
    backend = FrontendSmokeBackend(port=backend_port, item_count=args.items)
    vite = None
    browser = None
    cdp: CdpClient | None = None

    await backend.start()
    with tempfile.TemporaryDirectory(prefix="scriber-frontend-browser-", ignore_cleanup_errors=True) as temp_dir:
        profile_dir = Path(temp_dir) / "browser-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            vite = start_vite(frontend_port, backend.base_url)
            wait_http(f"http://127.0.0.1:{frontend_port}/", timeout_sec=args.startup_timeout_sec)

            browser_path = resolve_browser_path(args.browser)
            browser = start_browser(browser_path, debug_port, profile_dir, headed=args.headed)
            cdp = await connect_to_browser(debug_port)
            await install_page_error_capture(cdp)

            frontend_base_url = f"http://127.0.0.1:{frontend_port}"
            routes = [route for route in args.routes if route in ROUTE_EXPECTATIONS]
            scenarios = []
            token_required_check: dict[str, Any] | None = None
            for route in routes:
                scenario = await inspect_route(
                    cdp,
                    frontend_base_url=frontend_base_url,
                    route=route,
                    timeout_sec=args.page_timeout_sec,
                )
                interaction_checks: list[dict[str, Any]] = []
                if route == "/youtube":
                    interaction_checks.append(
                        await exercise_youtube_interactions(cdp, timeout_sec=args.page_timeout_sec)
                    )
                elif route == "/file":
                    interaction_checks.append(
                        await exercise_file_drop_interaction(cdp, timeout_sec=args.page_timeout_sec)
                    )
                elif route == "/debug":
                    interaction_checks.append(
                        await exercise_debug_console_interaction(cdp, timeout_sec=args.page_timeout_sec)
                    )
                elif route == "/transcript/youtube-processing-smoke":
                    interaction_checks.append(
                        await exercise_transcript_processing_refresh(cdp, timeout_sec=args.page_timeout_sec)
                    )
                if interaction_checks:
                    scenario["interactionChecks"] = interaction_checks
                    scenario["ok"] = bool(scenario["ok"]) and all(item.get("ok") for item in interaction_checks)
                scenarios.append(scenario)

            backend.session_token_required = True
            token_required_check = await inspect_token_required_browser_state(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
            )
        finally:
            if cdp:
                with suppress(Exception):
                    await cdp.call("Page.navigate", {"url": "about:blank"}, timeout=2)
                await cdp.close()
                await asyncio.sleep(0.1)
            await backend.close()
            await asyncio.sleep(0.1)
            if browser:
                terminate_process_tree(browser)
            if vite:
                terminate_process_tree(vite)

    ok = bool(scenarios) and all(item["ok"] for item in scenarios) and bool(token_required_check and token_required_check.get("ok"))
    virtualized_routes = [
        item["route"]
        for item in scenarios
        if item["route"] in {"/", "/youtube", "/file"} and item["historyVirtualized"]
    ]
    interaction_checks = [
        check
        for item in scenarios
        for check in item.get("interactionChecks", [])
    ]
    if token_required_check:
        interaction_checks.append(token_required_check)
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": ok,
        "summary": {
            "routeCount": len(scenarios),
            "routes": [item["route"] for item in scenarios],
            "virtualizedHistoryRoutes": virtualized_routes,
            "criticalConsoleErrorCount": sum(len(item["consoleErrors"]) for item in scenarios),
            "pageErrorCount": sum(len(item["pageErrors"]) for item in scenarios),
            "unhandledRejectionCount": sum(len(item["unhandledRejections"]) for item in scenarios),
            "interactionCheckCount": len(interaction_checks),
            "interactionChecks": [item.get("name", "") for item in interaction_checks],
        },
        "scenarios": scenarios,
        "tokenRequiredCheck": token_required_check,
    }


def parse_routes(value: str) -> list[str]:
    routes = [part.strip() for part in value.split(",") if part.strip()]
    return routes or list(ROUTE_EXPECTATIONS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test Scriber React routes in a real browser with a synthetic backend."
    )
    parser.add_argument("--routes", default=",".join(ROUTE_EXPECTATIONS))
    parser.add_argument("--items", type=int, default=120)
    parser.add_argument("--browser", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--startup-timeout-sec", type=float, default=30.0)
    parser.add_argument("--page-timeout-sec", type=float, default=20.0)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default="tmp/frontend-browser-smoke.json")
    args = parser.parse_args(argv)
    args.routes = parse_routes(args.routes)
    args.items = max(1, int(args.items))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def build_validate_result(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = [
        {
            "route": route,
            "ok": True,
            "expectedText": ROUTE_EXPECTATIONS.get(route, []),
            "historyVirtualized": route in {"/", "/youtube", "/file"},
            "visibleHistoryCards": 0,
            "consoleErrors": [],
            "pageErrors": [],
            "unhandledRejections": [],
            "interactionChecks": [
                {"name": "youtube-thumbnails", "ok": True}
            ] if route == "/youtube" else [
                {"name": "file-drag-drop", "ok": True}
            ] if route == "/file" else [
                {"name": "debug-clear", "ok": True}
            ] if route == "/debug" else [
                {"name": "transcript-processing-refresh", "ok": True}
            ] if route == "/transcript/youtube-processing-smoke" else [],
            "validateOnly": True,
        }
        for route in args.routes
        if route in ROUTE_EXPECTATIONS
    ]
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": bool(scenarios),
        "summary": {
            "routeCount": len(scenarios),
            "routes": [item["route"] for item in scenarios],
            "virtualizedHistoryRoutes": [
                item["route"] for item in scenarios if item["historyVirtualized"]
            ],
            "criticalConsoleErrorCount": 0,
            "pageErrorCount": 0,
            "unhandledRejectionCount": 0,
            "interactionCheckCount": sum(len(item.get("interactionChecks", [])) for item in scenarios) + 1,
            "interactionChecks": [
                check["name"]
                for item in scenarios
                for check in item.get("interactionChecks", [])
            ] + ["token-required-browser-state"],
            "validateOnly": True,
        },
        "scenarios": scenarios,
        "tokenRequiredCheck": {
            "name": "token-required-browser-state",
            "ok": True,
            "validateOnly": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.validate_only:
        result = build_validate_result(args)
    else:
        result = asyncio.run(run_browser_smoke(args))
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
