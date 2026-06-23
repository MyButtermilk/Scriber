from __future__ import annotations

import argparse
import asyncio
import base64
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
        "Clear logs",
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
    "/transcript/mic-no-summary-smoke": [
        "Synthetic No Summary Recording",
        "Summarize",
        "Transcript",
    ],
    "/transcript/mic-summary-failed-smoke": [
        "Synthetic Failed Summary Recording",
        "Summary generation failed",
        "Retry Summary",
        "Transcript",
    ],
}

FAST_TAB_SWITCH_SEQUENCE = ["/youtube", "/file", "/settings", "/", "/youtube", "/file", "/"]


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
        self.summarized_transcripts: set[str] = set()
        self.summarize_counts: dict[str, int] = {}
        self.canceled_transcripts: set[str] = set()
        self.cancel_counts: dict[str, int] = {}
        self.settings_patches: list[dict[str, Any]] = []
        self.runtime_logs_deleted = False
        self.support_bundle_count = 0
        self.file_uploads: list[dict[str, Any]] = []
        self.runtime_logs_count = 0
        self.youtube_transcribe_requests: list[dict[str, Any]] = []
        self.autostart_enabled = False
        self.autostart_available = True
        self.autostart_requests: list[dict[str, Any]] = []
        self.deleted_transcript_ids: set[str] = set()

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
        app.router.add_delete("/api/runtime/logs", self.delete_runtime_logs)
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
            self.settings_patches.append(patch)
            self.settings.update(patch)
        return web.json_response(self.settings)

    async def autostart(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            payload = await request.json()
            if isinstance(payload, dict):
                self.autostart_enabled = bool(payload.get("enabled"))
                self.autostart_requests.append({"enabled": self.autostart_enabled})
        return web.json_response(
            {"enabled": self.autostart_enabled, "available": self.autostart_available}
        )

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
                        "url": "https://www.youtube.com/watch?v=video-smoke-1",
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
                "url": "https://www.youtube.com/watch?v=0wEjbSYNUM8",
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
        payload = await request.json()
        if isinstance(payload, dict):
            self.youtube_transcribe_requests.append(payload)
        return web.json_response(
            {
                "success": True,
                "id": "youtube-queued-smoke",
                "title": "Synthetic Queued YouTube Transcription",
                "date": "Today, 12:40",
                "duration": "04:20",
                "status": "processing",
                "type": "youtube",
                "language": "auto",
                "step": "Queued",
                "channel": "Smoke Channel",
                "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                "message": "Synthetic transcription queued.",
            }
        )

    async def file_transcribe(self, request: web.Request) -> web.Response:
        filename = ""
        with suppress(Exception):
            form = await request.post()
            file_field = form.get("file")
            filename = str(getattr(file_field, "filename", "") or "")
        self.file_uploads.append({"filename": filename})
        self.request_log.append({"path": "/api/file/transcribe", "filename": filename})
        if filename == "too-large-smoke.wav":
            return web.json_response({"message": "Synthetic upload limit exceeded"}, status=413)
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
        self.runtime_logs_count += 1
        if self.runtime_logs_deleted:
            return web.json_response(
                {
                    "apiVersion": "1",
                    "items": [],
                    "sources": [],
                    "limit": 900,
                    "truncated": False,
                }
            )

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

    async def delete_runtime_logs(self, request: web.Request) -> web.Response:
        self.runtime_logs_deleted = True
        return web.json_response(
            {
                "apiVersion": "1",
                "ok": True,
                "cleared": 1,
                "failed": 0,
                "clearedSources": ["ui-debug-sample.log"],
                "failures": [],
            }
        )

    async def support_bundle(self, request: web.Request) -> web.Response:
        self.support_bundle_count += 1
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

        indexes: list[int] = [
            index
            for index in range(self.item_count)
            if f"{transcript_type}-{index:05d}" not in self.deleted_transcript_ids
        ]
        if query:
            indexes = [
                index
                for index in indexes
                if query in transcript_item(transcript_type, index)["title"].lower()
            ]

        total = len(indexes)
        page_indexes = indexes[offset : offset + limit]
        items = [transcript_item(transcript_type, index) for index in page_indexes]
        if transcript_type == "file" and not query and offset == 0:
            processing_item = {
                "id": "file-processing-smoke",
                "title": "Synthetic File Processing",
                "date": "Today, 12:50",
                "duration": "",
                "status": "processing",
                "type": "file",
                "language": "auto",
                "channel": "local audio",
                "fileSize": "12 MB",
                "step": "Preparing audio",
            }
            items = [processing_item, *items[: max(0, limit - 1)]]
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
        if transcript_id == "mic-no-summary-smoke":
            summarized = transcript_id in self.summarized_transcripts
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic No Summary Recording",
                    "date": "Today, 12:10",
                    "duration": "02:34",
                    "status": "completed",
                    "type": "mic",
                    "language": "de",
                    "step": "Completed",
                    "content": "Speaker 1: Transcript content that starts without a summary.",
                    "summary": "Synthetic manual summary generated by browser smoke." if summarized else "",
                    "summaryStatus": "completed" if summarized else "idle",
                    "summaryError": "",
                    "summaryUpdatedAt": "2026-06-01T12:11:00Z" if summarized else "",
                    "createdAt": "2026-06-01T12:00:00Z",
                    "updatedAt": "2026-06-01T12:11:00Z",
                }
            )
        if transcript_id == "mic-summary-failed-smoke":
            summarized = transcript_id in self.summarized_transcripts
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic Failed Summary Recording",
                    "date": "Today, 12:20",
                    "duration": "03:21",
                    "status": "completed",
                    "type": "mic",
                    "language": "de",
                    "step": "Completed" if summarized else "Summarization failed",
                    "content": "Speaker 1: Transcript content used for summary retry validation.",
                    "summary": "Synthetic retry summary generated by browser smoke." if summarized else "",
                    "summaryStatus": "completed" if summarized else "failed",
                    "summaryError": "" if summarized else "Synthetic summary provider failed.",
                    "summaryUpdatedAt": "2026-06-01T12:21:00Z" if summarized else "2026-06-01T12:20:30Z",
                    "createdAt": "2026-06-01T12:00:00Z",
                    "updatedAt": "2026-06-01T12:21:00Z",
                }
            )
        if transcript_id == "youtube-cancel-smoke":
            canceled = transcript_id in self.canceled_transcripts
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic Cancel Processing",
                    "date": "Today, 12:30",
                    "duration": "00:00" if canceled else "",
                    "status": "stopped" if canceled else "processing",
                    "type": "youtube",
                    "language": "auto",
                    "step": "Cancelled" if canceled else "Downloading audio",
                    "sourceUrl": "https://www.youtube.com/watch?v=cancel-smoke",
                    "channel": "Smoke Channel",
                    "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                    "content": "",
                    "summary": "",
                    "summaryStatus": "idle",
                    "summaryError": "",
                    "summaryUpdatedAt": "",
                    "createdAt": "2026-06-01T12:30:00Z",
                    "updatedAt": "2026-06-01T12:31:00Z" if canceled else "2026-06-01T12:30:10Z",
                }
            )
        if transcript_id == "youtube-queued-smoke":
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic Queued YouTube Transcription",
                    "date": "Today, 12:40",
                    "duration": "04:20",
                    "status": "processing",
                    "type": "youtube",
                    "language": "auto",
                    "step": "Queued",
                    "sourceUrl": "https://www.youtube.com/watch?v=video-smoke-1",
                    "channel": "Smoke Channel",
                    "thumbnailUrl": f"{self.base_url}/synthetic-thumbnail.svg",
                    "content": "",
                    "summary": "",
                    "summaryStatus": "pending",
                    "summaryError": "",
                    "summaryUpdatedAt": "",
                    "createdAt": "2026-06-01T12:40:00Z",
                    "updatedAt": "2026-06-01T12:40:10Z",
                }
            )
        if transcript_id == "file-processing-smoke":
            return web.json_response(
                {
                    "id": transcript_id,
                    "title": "Synthetic File Processing",
                    "date": "Today, 12:50",
                    "duration": "",
                    "status": "processing",
                    "type": "file",
                    "language": "auto",
                    "step": "Preparing audio",
                    "channel": "local audio",
                    "content": "",
                    "summary": "",
                    "summaryStatus": "pending",
                    "summaryError": "",
                    "summaryUpdatedAt": "",
                    "createdAt": "2026-06-01T12:50:00Z",
                    "updatedAt": "2026-06-01T12:50:10Z",
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
        transcript_id = request.match_info["transcript_id"]
        self.deleted_transcript_ids.add(transcript_id)
        self.request_log.append({"path": "/api/transcripts/{id}", "method": "DELETE", "id": transcript_id})
        return web.json_response({"success": True})

    async def summarize_transcript(self, request: web.Request) -> web.Response:
        transcript_id = request.match_info["transcript_id"]
        self.summarized_transcripts.add(transcript_id)
        self.summarize_counts[transcript_id] = self.summarize_counts.get(transcript_id, 0) + 1
        if transcript_id == "mic-summary-failed-smoke":
            summary = "Synthetic retry summary generated by browser smoke."
        elif transcript_id == "mic-no-summary-smoke":
            summary = "Synthetic manual summary generated by browser smoke."
        else:
            summary = "Synthetic summary for browser smoke."
        return web.json_response({"success": True, "summary": summary})

    async def ok_response(self, request: web.Request) -> web.Response:
        transcript_id = request.match_info.get("transcript_id", "")
        if transcript_id and request.path.endswith("/cancel"):
            self.canceled_transcripts.add(transcript_id)
            self.cancel_counts[transcript_id] = self.cancel_counts.get(transcript_id, 0) + 1
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
            "summarizationModel": "gemini-flash-latest",
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


async def click_page_coordinates(cdp: CdpClient, *, x: float, y: float) -> None:
    await cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=5)
    await cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        timeout=5,
    )
    await cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        timeout=5,
    )


def evidence_path_for_report(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


async def capture_page_screenshot(cdp: CdpClient, *, output_dir: Path, label: str) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in label).strip("-")
    path = output_dir / f"{safe_label or 'screenshot'}.png"
    result = await cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=10)
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise RuntimeError("CDP Page.captureScreenshot did not return image data.")
    path.write_bytes(base64.b64decode(data))
    return evidence_path_for_report(path)


async def wait_for_fast_tab_ready(
    cdp: CdpClient,
    *,
    route: str,
    expected_text: list[str],
    timeout_sec: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_sec
    start = time.monotonic()
    samples: list[dict[str, Any]] = []
    expectation = json.dumps(expected_text)
    expression = f"""
(() => {{
  const expected = {expectation};
  const main = document.querySelector('main');
  const mainText = main ? main.innerText : '';
  const bodyText = document.body ? document.body.innerText : '';
  const missing = expected.filter((item) => !bodyText.includes(item));
  return {{
    ready: window.location.pathname === {json.dumps(route)}
      && document.readyState === 'complete'
      && missing.length === 0
      && !bodyText.includes('Backend Not Available')
      && !/Could not load|Failed to load|Please retry loading/.test(bodyText),
    route: window.location.pathname,
    missing,
    mainTextLength: mainText.trim().length,
    bodyTextLength: bodyText.trim().length,
    showingPageLoader: mainText.includes('Loading...'),
    bodyText: bodyText.slice(0, 500),
  }};
}})()
"""
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = await cdp.evaluate(expression, timeout=5)
        last_state = state or {}
        samples.append(
            {
                "elapsedMs": round((time.monotonic() - start) * 1000, 1),
                "route": last_state.get("route"),
                "mainTextLength": int(last_state.get("mainTextLength") or 0),
                "bodyTextLength": int(last_state.get("bodyTextLength") or 0),
                "showingPageLoader": bool(last_state.get("showingPageLoader")),
                "missing": last_state.get("missing", []),
            }
        )
        if last_state.get("ready"):
            return last_state, samples
        await asyncio.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for fast tab route {route}. Last state: {last_state}")


async def exercise_fast_tab_switch(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
    max_route_ms: float,
    evidence_dir: Path,
) -> dict[str, Any]:
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1365, "height": 768, "deviceScaleFactor": 1, "mobile": False},
        timeout=5,
    )
    await cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": False}, timeout=5)
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/",
        expected_text=ROUTE_EXPECTATIONS["/"],
        expect_history_virtualized=True,
        timeout_sec=timeout_sec,
    )
    await asyncio.sleep(0.6)

    transitions: list[dict[str, Any]] = []
    for route in FAST_TAB_SWITCH_SEQUENCE:
        preload_state = await cdp.evaluate(
            f"""
(() => {{
  const route = {json.dumps(route)};
  const links = Array.from(document.querySelectorAll('aside a[href]'));
  const link = links.find((node) => new URL(node.href).pathname === route);
  if (!link) return {{ ok: false, reason: 'missing desktop nav link', route }};
  link.dispatchEvent(new PointerEvent('pointerenter', {{ bubbles: true, cancelable: true }}));
  link.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, cancelable: true }}));
  link.dispatchEvent(new FocusEvent('focus', {{ bubbles: true, cancelable: true }}));
  return {{
    ok: true,
    route,
    label: (link.textContent || '').trim(),
    currentRoute: window.location.pathname,
  }};
}})()
""",
            timeout=5,
        )
        if not preload_state or not preload_state.get("ok"):
            raise RuntimeError(f"Could not warm desktop nav link for {route}: {preload_state}")
        await asyncio.sleep(0.05)

        started = time.monotonic()
        click_state = await cdp.evaluate(
            f"""
(() => {{
  const route = {json.dumps(route)};
  const links = Array.from(document.querySelectorAll('aside a[href]'));
  const link = links.find((node) => new URL(node.href).pathname === route);
  if (!link) return {{ ok: false, reason: 'missing desktop nav link', route }};
  link.click();
  return {{
    ok: true,
    route,
    label: (link.textContent || '').trim(),
    beforeReadyState: document.readyState,
  }};
}})()
""",
            timeout=5,
        )
        if not click_state or not click_state.get("ok"):
            raise RuntimeError(f"Could not click desktop nav link for {route}: {click_state}")

        state, samples = await wait_for_fast_tab_ready(
            cdp,
            route=route,
            expected_text=ROUTE_EXPECTATIONS[route],
            timeout_sec=timeout_sec,
        )
        route_ready_ms = round((time.monotonic() - started) * 1000, 1)
        main_lengths = [int(sample.get("mainTextLength") or 0) for sample in samples]
        blank_sample_count = sum(1 for value in main_lengths if value == 0)
        loading_sample_count = sum(1 for sample in samples if sample.get("showingPageLoader"))
        transitions.append(
            {
                "route": route,
                "label": click_state.get("label"),
                "ok": route_ready_ms <= max_route_ms and blank_sample_count == 0,
                "routeReadyMs": route_ready_ms,
                "sampleCount": len(samples),
                "blankSampleCount": blank_sample_count,
                "loadingSampleCount": loading_sample_count,
                "minMainTextLength": min(main_lengths) if main_lengths else 0,
                "finalMainTextLength": int(state.get("mainTextLength") or 0),
            }
        )

    screenshot_path = await capture_page_screenshot(
        cdp,
        output_dir=evidence_dir,
        label="fast-tab-switch-final",
    )
    return {
        "name": "fast-tab-switch",
        "ok": bool(transitions) and all(item["ok"] for item in transitions),
        "maxRouteReadyMs": max_route_ms,
        "maxObservedRouteReadyMs": max(item["routeReadyMs"] for item in transitions) if transitions else 0,
        "routes": FAST_TAB_SWITCH_SEQUENCE,
        "transitions": transitions,
        "screenshot": screenshot_path,
    }


async def wait_for_settings_patches(
    backend: FrontendSmokeBackend,
    *,
    label: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        patches = list(backend.settings_patches)
        if (
            any(patch.get("summarizationModel") == "gemini-3.5-flash" for patch in patches)
            and any(patch.get("autoSummarize") is True for patch in patches)
            and any(patch.get("language") == "de" for patch in patches)
            and any(patch.get("defaultSttService") == "mistral_async" for patch in patches)
            and any(patch.get("customVocab") == "Scriber, Gemini 3.5, Quality Loop" for patch in patches)
            and any(
                patch.get("summarizationPrompt") == "Bitte fasse Entscheidungen und offene Risiken knapp zusammen."
                for patch in patches
            )
            and any(
                isinstance(patch.get("apiKeys"), dict)
                and patch["apiKeys"].get("googleApiKey") == "smoke-gemini-key"
                for patch in patches
            )
        ):
            return {
                "ok": True,
                "patchCount": len(patches),
                "patches": patches,
            }
        await asyncio.sleep(0.25)
    raise RuntimeError(
        f"Timed out waiting for settings patches during {label}. "
        f"Observed patches: {backend.settings_patches}"
    )


async def fill_settings_textarea(
    cdp: CdpClient,
    *,
    placeholder_includes: str,
    value: str,
    timeout_sec: float,
) -> dict[str, Any]:
    focused = await wait_for_interaction_state(
        cdp,
        label=f"settings-textarea-focus-{placeholder_includes}",
        timeout_sec=timeout_sec,
        expression=f"""
(() => {{
  const needle = {json.dumps(placeholder_includes)};
  const node = Array.from(document.querySelectorAll('textarea'))
    .find((item) => (item.getAttribute('placeholder') || '').includes(needle));
  if (!node) return {{ ok: false, reason: 'missing textarea', needle }};
  node.focus();
  return {{
    ok: document.activeElement === node,
    needle,
    currentValue: node.value || ''
  }};
}})()
""",
    )
    await cdp.call("Input.insertText", {"text": value}, timeout=5)
    blurred = await wait_for_interaction_state(
        cdp,
        label=f"settings-textarea-blur-{placeholder_includes}",
        timeout_sec=timeout_sec,
        expression=f"""
(() => {{
  const needle = {json.dumps(placeholder_includes)};
  const expected = {json.dumps(value)};
  const node = Array.from(document.querySelectorAll('textarea'))
    .find((item) => (item.getAttribute('placeholder') || '').includes(needle));
  if (!node) return {{ ok: false, reason: 'missing textarea', needle }};
  if (node.value !== expected) {{
    return {{ ok: false, reason: 'text not inserted', needle, currentValue: node.value || '' }};
  }}
  node.blur();
  return {{ ok: document.activeElement !== node, needle, value: node.value }};
}})()
""",
    )
    return {"focused": focused, "blurred": blurred}


async def wait_for_favorite_mic_patch(
    backend: FrontendSmokeBackend,
    *,
    expected_device_id: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        patches = list(backend.settings_patches)
        if any(patch.get("favoriteMic") == expected_device_id for patch in patches):
            return {
                "ok": True,
                "expectedDeviceId": expected_device_id,
                "patchCount": len(patches),
                "patches": patches,
            }
        await asyncio.sleep(0.25)
    raise RuntimeError(
        "Timed out waiting for favorite microphone setting patch. "
        f"Expected {expected_device_id}; observed patches: {backend.settings_patches}"
    )


async def exercise_settings_help_links(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    state = await wait_for_interaction_state(
        cdp,
        label="settings-help-links",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const expected = {
    'OpenAI keys': 'https://platform.openai.com/api-keys',
    'Deepgram console': 'https://console.deepgram.com/',
    'AssemblyAI dashboard': 'https://www.assemblyai.com/dashboard',
    'Google AI Studio': 'https://aistudio.google.com/app/apikey',
    'Google Cloud credentials': 'https://console.cloud.google.com/apis/credentials',
    'Soniox console': 'https://console.soniox.com/',
    'Smallest AI console': 'https://app.smallest.ai/',
    'Mistral API keys': 'https://console.mistral.ai/api-keys',
    'Azure MAI Speech resource': 'https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices',
    'Gladia API keys': 'https://app.gladia.io/api-keys',
    'Groq API keys': 'https://console.groq.com/keys',
    'Speechmatics portal': 'https://portal.speechmatics.com/'
  };
  const links = Array.from(document.querySelectorAll('a[target="_blank"][rel~="noreferrer"]'))
    .map((link) => ({
      title: link.getAttribute('title') || '',
      href: link.href,
      text: (link.textContent || '').trim()
    }));
  const missing = [];
  const mismatched = [];
  for (const [title, href] of Object.entries(expected)) {
    const link = links.find((item) => item.title === title);
    if (!link) {
      missing.push(title);
    } else if (link.href !== href) {
      mismatched.push({ title, expected: href, actual: link.href });
    }
  }
  return {
    ok: missing.length === 0 && mismatched.length === 0,
    checkedCount: Object.keys(expected).length,
    linkCount: links.length,
    missing,
    mismatched,
    links
  };
})()
""",
    )
    return {"name": "settings-help-links", "ok": True, "state": state}


async def exercise_settings_favorite_mic(
    cdp: CdpClient,
    *,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    state = await wait_for_interaction_state(
        cdp,
        label="settings-favorite-mic",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const trigger = document.querySelector('button[aria-label="Select input device"]');
  if (!trigger) return { ok: false, reason: 'missing input device dropdown trigger' };
  if (trigger.getAttribute('aria-expanded') !== 'true') {
    trigger.scrollIntoView({ block: 'center', inline: 'center' });
    trigger.click();
    return { ok: false, waitingForDropdown: true };
  }
  const text = document.body ? document.body.innerText : '';
  const removeInput = document.querySelector('input[aria-label="Remove USB Smoke Microphone from favorites"]');
  if (removeInput) {
    return {
      ok: text.includes('Favorite mic will be used automatically when connected'),
      hasRemoveFavoriteInput: true,
      hasFavoriteToast: text.includes('Favorite set'),
      hasFavoriteStatus: text.includes('Favorite mic will be used automatically when connected')
    };
  }
  const favoriteInput = document.querySelector('input[aria-label="Set USB Smoke Microphone as favorite"]');
  if (!favoriteInput) {
    return {
      ok: false,
      reason: 'missing favorite input',
      expanded: trigger.getAttribute('aria-expanded'),
      bodyText: (document.body?.innerText || '').slice(0, 800)
    };
  }
  if (!window.__scriberSmokeFavoriteMicClicked) {
    window.__scriberSmokeFavoriteMicClicked = true;
    favoriteInput.click();
    return { ok: false, waitingForFavoriteSave: true };
  }
  return {
    ok: !!removeInput
      && text.includes('Favorite set')
      && text.includes('Favorite mic will be used automatically when connected'),
    hasRemoveFavoriteInput: !!removeInput,
    hasFavoriteToast: text.includes('Favorite set'),
    hasFavoriteStatus: text.includes('Favorite mic will be used automatically when connected')
  };
})()
""",
    )
    patch = await wait_for_favorite_mic_patch(
        backend,
        expected_device_id="usb-smoke-mic",
        timeout_sec=timeout_sec,
    )
    return {"name": "settings-favorite-mic", "ok": True, "state": state, "patch": patch}


async def exercise_settings_interactions(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    backend.settings_patches.clear()
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/settings"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/settings",
        expected_text=ROUTE_EXPECTATIONS["/settings"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )

    state = await wait_for_interaction_state(
        cdp,
        label="settings-interactions",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const setNativeValue = (node, value) => {
    const prototype = node instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    setter?.call(node, value);
    node.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const clickRadio = (selector) => {
    const node = document.querySelector(selector);
    if (!node) return false;
    node.click();
    return true;
  };
  const clickSwitchInRow = (label) => {
    const row = Array.from(document.querySelectorAll('.settings-control-row'))
      .find((node) => (node.textContent || '').includes(label));
    const control = row?.querySelector('[role="switch"]');
    if (!control) return false;
    if (control.getAttribute('aria-checked') !== 'true') {
      control.click();
    }
    return true;
  };
  const findTextArea = (placeholderIncludes) => Array.from(document.querySelectorAll('textarea'))
      .find((item) => (item.getAttribute('placeholder') || '').includes(placeholderIncludes));
  const customVocabularyArea = findTextArea('Replit, TypeScript');
  const summaryPromptArea = findTextArea('Summarize the following transcript');
  const geminiInput = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.value || '') === '');
  const geminiSection = Array.from(document.querySelectorAll('.space-y-2'))
    .find((node) => (node.textContent || '').includes('Gemini API Key'));
  const geminiKeyInput = geminiSection?.querySelector('input');
  const geminiSaveButton = Array.from(geminiSection?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').includes('Save'));

  const actions = {
    transcription: !!document.querySelector('input[aria-label="Select Mistral Async (Voxtral V2) as transcription model"]'),
    language: !!document.querySelector('input[aria-label="Select German as default transcription language"]'),
    summarizationModel: !!document.querySelector('input[aria-label="Select Gemini 3.5 Flash as summarization model"]'),
    autoSummarize: !!Array.from(document.querySelectorAll('.settings-control-row'))
      .find((node) => (node.textContent || '').includes('Auto-Summarize'))?.querySelector('[role="switch"]'),
    customVocabulary: !!customVocabularyArea,
    summaryPrompt: !!summaryPromptArea,
    geminiKey: !!geminiKeyInput && !!geminiSaveButton
  };
  if (!window.__scriberSmokeSettingsControlsClicked) {
      window.__scriberSmokeSettingsControlsClicked = true;
    clickRadio('input[aria-label="Select Mistral Async (Voxtral V2) as transcription model"]');
    clickRadio('input[aria-label="Select German as default transcription language"]');
    clickRadio('input[aria-label="Select Gemini 3.5 Flash as summarization model"]');
    clickSwitchInRow('Auto-Summarize');
    return { ok: false, waitingForControlSaves: true, actions };
  }
  if (actions.geminiKey && !window.__scriberSmokeGeminiKeySaved) {
    window.__scriberSmokeGeminiKeySaved = true;
    setNativeValue(geminiKeyInput, 'smoke-gemini-key');
    geminiSaveButton.click();
    return { ok: false, waitingForGeminiKeySave: true, actions };
  }

  const text = document.body ? document.body.innerText : '';
  return {
    ok: Object.values(actions).every(Boolean)
      && text.includes('Mistral Async (Voxtral V2)')
      && text.includes('German')
      && text.includes('Gemini 3.5 Flash')
      && text.includes('Saved'),
    actions,
    hasMistralAsync: text.includes('Mistral Async (Voxtral V2)'),
    hasGerman: text.includes('German'),
    hasGemini35: text.includes('Gemini 3.5 Flash'),
    hasSavedToastOrButton: text.includes('Saved')
  };
})()
""",
    )
    help_links = await exercise_settings_help_links(cdp, timeout_sec=timeout_sec)
    favorite_mic = await exercise_settings_favorite_mic(
        cdp,
        backend=backend,
        timeout_sec=timeout_sec,
    )
    custom_vocabulary = await fill_settings_textarea(
        cdp,
        placeholder_includes="Replit, TypeScript",
        value="Scriber, Gemini 3.5, Quality Loop",
        timeout_sec=timeout_sec,
    )
    summary_prompt = await fill_settings_textarea(
        cdp,
        placeholder_includes="Summarize the following transcript",
        value="Bitte fasse Entscheidungen und offene Risiken knapp zusammen.",
        timeout_sec=timeout_sec,
    )
    patches = await wait_for_settings_patches(
        backend,
        label="settings-interactions",
        timeout_sec=timeout_sec,
    )
    return {
        "name": "settings-persistence",
        "ok": True,
        "state": state,
        "helpLinks": help_links,
        "favoriteMic": favorite_mic,
        "customVocabulary": custom_vocabulary,
        "summaryPrompt": summary_prompt,
        "patches": patches,
    }


async def wait_for_settings_desktop_control_effects(
    backend: FrontendSmokeBackend,
    *,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        patches = list(backend.settings_patches)
        autostart_requests = list(backend.autostart_requests)
        if (
            any(patch.get("hotkey") == "Ctrl + Alt + H" for patch in patches)
            and any(patch.get("mode") == "push_to_talk" for patch in patches)
            and any(request.get("enabled") is True for request in autostart_requests)
            and backend.autostart_enabled is True
        ):
            return {
                "ok": True,
                "patchCount": len(patches),
                "patches": patches,
                "autostartRequests": autostart_requests,
                "autostartEnabled": backend.autostart_enabled,
            }
        await asyncio.sleep(0.25)
    raise RuntimeError(
        "Timed out waiting for Settings desktop-control effects. "
        f"Observed patches: {backend.settings_patches}; "
        f"autostart requests: {backend.autostart_requests}; "
        f"autostart enabled: {backend.autostart_enabled}"
    )


async def exercise_settings_desktop_controls(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    backend.settings_patches.clear()
    backend.autostart_requests.clear()
    backend.autostart_enabled = False
    backend.autostart_available = True

    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/settings"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/settings",
        expected_text=ROUTE_EXPECTATIONS["/settings"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )

    hotkey_trigger = await wait_for_interaction_state(
        cdp,
        label="settings-desktop-controls-hotkey-trigger",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const isVisible = (node) => !!(node && (node.offsetWidth || node.offsetHeight || node.getClientRects().length));
  const rows = Array.from(document.querySelectorAll('.settings-control-row')).filter(isVisible);
  const hotkeyRow = rows.find((node) => normalize(node.textContent).includes('Global Hotkey'));
  const hotkeyButton = hotkeyRow?.querySelector('button');
  hotkeyButton?.scrollIntoView({ block: 'center', inline: 'center' });
  const rect = hotkeyButton?.getBoundingClientRect();
  if (!hotkeyButton || !rect || rect.width <= 0 || rect.height <= 0) {
    return { ok: false, reason: 'missing visible hotkey button', hasHotkeyRow: !!hotkeyRow, text: text.slice(0, 1000) };
  }
  if (rect.top < 0 || rect.left < 0 || rect.bottom > window.innerHeight || rect.right > window.innerWidth) {
    return {
      ok: false,
      reason: 'hotkey button outside viewport after scroll',
      rect: { top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      label: normalize(hotkeyButton.textContent),
    };
  }
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    label: normalize(hotkeyButton.textContent),
  };
})()
""",
    )
    await click_page_coordinates(
        cdp,
        x=float(hotkey_trigger["x"]),
        y=float(hotkey_trigger["y"]),
    )

    state = await wait_for_interaction_state(
        cdp,
        label="settings-desktop-controls",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const isVisible = (node) => !!(node && (node.offsetWidth || node.offsetHeight || node.getClientRects().length));
  const rows = Array.from(document.querySelectorAll('.settings-control-row')).filter(isVisible);
  const rowByLabel = (label) => rows.find((node) => normalize(node.textContent).includes(label));
  const hotkeyRow = rowByLabel('Global Hotkey');
  const autostartRow = rowByLabel('Autostart with Windows');
  const hotkeyButton = hotkeyRow?.querySelector('button');
  const hotkeyRect = hotkeyButton?.getBoundingClientRect();
  const hotkeyCenter = hotkeyRect
    ? { x: hotkeyRect.left + hotkeyRect.width / 2, y: hotkeyRect.top + hotkeyRect.height / 2 }
    : null;
  const elementAtHotkeyCenter = hotkeyCenter
    ? document.elementFromPoint(hotkeyCenter.x, hotkeyCenter.y)
    : null;
  const autostartSwitch = autostartRow?.querySelector('[role="switch"]');
  const clickableCards = Array.from(document.querySelectorAll('.cursor-pointer')).filter(isVisible);
  const pushHoldCard = clickableCards.find((node) => normalize(node.textContent).includes('Push and Hold'));
  const dialog = document.querySelector('[role="dialog"]');
  const saveButton = Array.from((dialog || document).querySelectorAll('button'))
    .find((node) => normalize(node.textContent) === 'Save');
  const controls = {
    hotkeyButton: !!hotkeyButton,
    autostartSwitch: !!autostartSwitch,
    pushHoldCard: !!pushHoldCard,
    autostartVisible: !!autostartRow,
  };
  if (!Object.values(controls).every(Boolean)) {
    return { ok: false, reason: 'missing settings desktop controls', controls, text: text.slice(0, 1000) };
  }

  if (!window.__scriberSmokeHotkeySaved) {
    if (!dialog || !text.includes('Record Hotkey')) {
      return {
        ok: false,
        step: 'waiting-for-hotkey-dialog',
        controls,
        hotkeyLabel: normalize(hotkeyButton?.textContent),
        hotkeyRect: hotkeyRect ? {
          top: hotkeyRect.top,
          right: hotkeyRect.right,
          bottom: hotkeyRect.bottom,
          left: hotkeyRect.left,
          width: hotkeyRect.width,
          height: hotkeyRect.height,
        } : null,
        elementAtHotkeyCenter: elementAtHotkeyCenter ? {
          tag: elementAtHotkeyCenter.tagName,
          text: normalize(elementAtHotkeyCenter.textContent).slice(0, 120),
          className: String(elementAtHotkeyCenter.className || '').slice(0, 160),
        } : null,
        activeElement: document.activeElement ? {
          tag: document.activeElement.tagName,
          text: normalize(document.activeElement.textContent).slice(0, 120),
          className: String(document.activeElement.className || '').slice(0, 160),
        } : null,
        dialogCount: document.querySelectorAll('[role="dialog"]').length,
      };
    }
    if (!window.__scriberSmokeHotkeyDispatched) {
      window.__scriberSmokeHotkeyDispatched = true;
      window.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'H',
        code: 'KeyH',
        ctrlKey: true,
        altKey: true,
        bubbles: true,
        cancelable: true,
      }));
      return { ok: false, step: 'captured-hotkey', controls };
    }
    if (!text.includes('Ctrl + Alt + H')) {
      return { ok: false, step: 'waiting-for-hotkey-text', controls, hasHotkey: false };
    }
    if (!saveButton) {
      return { ok: false, step: 'missing-hotkey-save-button', controls };
    }
    window.__scriberSmokeHotkeySaved = true;
    saveButton.click();
    return { ok: false, step: 'saved-hotkey', controls };
  }
  if (dialog || text.includes('Record Hotkey')) {
    return { ok: false, step: 'waiting-for-hotkey-dialog-close', controls };
  }
  if (!window.__scriberSmokeRecordingModeClicked) {
    window.__scriberSmokeRecordingModeClicked = true;
    pushHoldCard.click();
    return { ok: false, step: 'clicked-push-hold', controls };
  }
  const pushHoldSelected = String(pushHoldCard.getAttribute('class') || '').includes('border-primary');
  if (!pushHoldSelected) {
    return { ok: false, step: 'waiting-for-push-hold-selection', controls, pushHoldClass: pushHoldCard.getAttribute('class') };
  }
  if (autostartSwitch.getAttribute('aria-checked') !== 'true') {
    if (!window.__scriberSmokeAutostartClicked) {
      window.__scriberSmokeAutostartClicked = true;
      autostartSwitch.click();
      return { ok: false, step: 'clicked-autostart', controls };
    }
    return { ok: false, step: 'waiting-for-autostart-enabled', controls, checked: autostartSwitch.getAttribute('aria-checked') };
  }
  const desktopUpdateHeading = Array.from(document.querySelectorAll('h2'))
    .find((node) => normalize(node.textContent).includes('Desktop Updates'));
  const desktopUpdateTrigger = desktopUpdateHeading?.closest('button');
  const desktopUpdateButton = Array.from(document.querySelectorAll('button'))
    .find((node) => normalize(node.textContent).includes('Check for updates'));
  if (!desktopUpdateButton) {
    desktopUpdateTrigger?.scrollIntoView({ block: 'center', inline: 'center' });
    if (desktopUpdateTrigger && !window.__scriberSmokeDesktopUpdatesOpened) {
      window.__scriberSmokeDesktopUpdatesOpened = true;
      desktopUpdateTrigger.click();
    }
    return {
      ok: false,
      step: 'waiting-for-desktop-update-button',
      controls,
      hasDesktopUpdateHeading: !!desktopUpdateHeading,
      hasDesktopUpdateTrigger: !!desktopUpdateTrigger,
    };
  }
  if (!window.__scriberSmokeDesktopUpdateClicked) {
    window.__scriberSmokeDesktopUpdateClicked = true;
    window.__scriberSmokeDesktopUpdateStartedAt = performance.now();
    desktopUpdateButton.click();
    return { ok: false, step: 'clicked-desktop-update-check', controls };
  }
  const desktopUpdateElapsedMs = Math.round(performance.now() - (window.__scriberSmokeDesktopUpdateStartedAt || performance.now()));
  const desktopUpdateReady = text.includes('Desktop updates are available in the installed Windows app.');
  if (!desktopUpdateReady) {
    return {
      ok: false,
      step: 'waiting-for-desktop-update-status',
      controls,
      desktopUpdateElapsedMs,
      hasUnavailableStatus: text.includes('Disabled') || text.includes('Unavailable'),
      hasUnavailableMessage: text.includes('Desktop updates are available in the installed Windows app.'),
    };
  }

  return {
    ok: true,
    controls,
    hotkeyVisible: text.includes('Ctrl + Alt + H'),
    pushHoldSelected,
    autostartChecked: autostartSwitch.getAttribute('aria-checked') === 'true',
    desktopUpdate: {
      unavailable: true,
      statusLabel: text.includes('Disabled') ? 'Disabled' : (text.includes('Unavailable') ? 'Unavailable' : ''),
      elapsedMs: desktopUpdateElapsedMs,
      message: 'Desktop updates are available in the installed Windows app.',
    },
  };
})()
""",
    )
    effects = await wait_for_settings_desktop_control_effects(
        backend,
        timeout_sec=timeout_sec,
    )
    return {
        "name": "settings-desktop-controls",
        "ok": True,
        "state": state,
        "effects": effects,
    }


async def exercise_youtube_history_interactions(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    initial_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-initial",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  const text = document.body ? document.body.innerText : '';
  return {
    ok: !!root
      && document.querySelectorAll('.perf-scroll-item').length > 0
      && text.includes('Synthetic Video 00001'),
    view: root?.getAttribute('data-history-view') || '',
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    hasFirstVideo: text.includes('Synthetic Video 00001')
  };
})()
""",
    )

    list_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="List view"]');
  if (!button) return { ok: false, reason: 'missing list view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not list_clicked or not list_clicked.get("ok"):
        raise RuntimeError(f"Could not switch YouTube history to list view: {list_clicked}")

    list_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-list-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'list',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    search_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-search",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Search history'));
  if (!input) return { ok: false, reason: 'missing YouTube history search input' };
  if (input.value !== '00002') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '00002');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForSearch: true };
  }
  const text = document.body ? document.body.innerText : '';
  const query = new URLSearchParams(window.location.search).get('q') || '';
  return {
    ok: query === '00002'
      && text.includes('Synthetic Video 00002')
      && !text.includes('Synthetic Video 00001'),
    query,
    hasTarget: text.includes('Synthetic Video 00002'),
    hasFilteredOutFirst: !text.includes('Synthetic Video 00001'),
    visibleCards: document.querySelectorAll('.perf-scroll-item').length
  };
})()
""",
    )

    clipboard_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-copy",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  if (!window.__scriberSmokeYoutubeClipboardWrites) {
    const writes = [];
    const clipboard = { writeText: async (value) => { writes.push(String(value)); } };
    window.__scriberSmokeYoutubeClipboardWrites = writes;
    let stubbed = false;
    try {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: clipboard
      });
      stubbed = true;
    } catch (error) {
      try {
        Object.defineProperty(Navigator.prototype, 'clipboard', {
          configurable: true,
          get: () => clipboard
        });
        stubbed = true;
      } catch (_fallbackError) {
        window.__scriberSmokeYoutubeClipboardStubError = String(error);
      }
    }
    window.__scriberSmokeYoutubeClipboardStubbed = stubbed;
  }
  const button = document.querySelector('button[aria-label="Copy transcript Synthetic Video 00002"]');
  if (!button) {
    return {
      ok: false,
      reason: 'missing YouTube copy button',
      stubbed: !!window.__scriberSmokeYoutubeClipboardStubbed,
      stubError: window.__scriberSmokeYoutubeClipboardStubError || ''
    };
  }
  if (!window.__scriberSmokeYoutubeCopyClicked) {
    window.__scriberSmokeYoutubeCopyClicked = true;
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeYoutubeClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeYoutubeClipboardWrites || [];
  return {
    ok: writes.some((value) => value.includes('synthetic transcript used by the frontend browser smoke test')),
    writes,
    stubbed: !!window.__scriberSmokeYoutubeClipboardStubbed,
    toastVisible: (document.body?.innerText || '').includes('Transcript copied to clipboard.')
  };
})()
""",
    )

    delete_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-delete",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const button = document.querySelector('button[aria-label="Delete transcript Synthetic Video 00002"]');
  if (!button && !window.__scriberSmokeYoutubeDeleteClicked) {
    return { ok: false, reason: 'missing YouTube delete button' };
  }
  if (button && !window.__scriberSmokeYoutubeDeleteClicked) {
    window.__scriberSmokeYoutubeDeleteClicked = true;
    button.click();
    return { ok: false, waitingForDelete: true };
  }
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('Transcript removed successfully.'),
    hasDeletedToast: text.includes('Transcript removed successfully.')
  };
})()
""",
    )

    clear_search_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-clear-search-before-grid",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Search history'));
  if (!input) return { ok: false, reason: 'missing YouTube history search input' };
  if (input.value !== '') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForClear: true };
  }
  const root = document.querySelector('[data-history-virtualized="true"]');
  const text = document.body ? document.body.innerText : '';
  const query = new URLSearchParams(window.location.search).get('q') || '';
  return {
    ok: query === '' && !!root && text.includes('Synthetic Video 00001'),
    query,
    hasRoot: !!root,
    hasFirstVideo: text.includes('Synthetic Video 00001')
  };
})()
""",
    )

    grid_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="Grid view"]');
  if (!button) return { ok: false, reason: 'missing grid view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not grid_clicked or not grid_clicked.get("ok"):
        raise RuntimeError(f"Could not switch YouTube history to grid view: {grid_clicked}")

    grid_state = await wait_for_interaction_state(
        cdp,
        label="youtube-history-grid-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'grid',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    return {
        "name": "youtube-history-actions",
        "ok": True,
        "initial": initial_state,
        "listView": list_state,
        "search": search_state,
        "copy": clipboard_state,
        "delete": delete_state,
        "clearSearch": clear_search_state,
        "gridView": grid_state,
    }


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


async def exercise_youtube_start_transcription(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    backend.youtube_transcribe_requests.clear()
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/youtube"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/youtube",
        expected_text=ROUTE_EXPECTATIONS["/youtube"],
        expect_history_virtualized=True,
        timeout_sec=timeout_sec,
    )

    search_started = await cdp.evaluate(
        r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Youtube'));
  const button = document.querySelector('button[aria-label="Search YouTube"]');
  if (!input || !button) return { ok: false, reason: 'missing input/button' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'Scriber queued validation');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not search_started or not search_started.get("ok"):
        raise RuntimeError(f"Could not start YouTube transcription search: {search_started}")

    result_state = await wait_for_interaction_state(
        cdp,
        label="youtube-start-result",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const card = document.querySelector('[aria-label="Start transcription for Synthetic YouTube Result"]');
  return {
    ok: !!card && text.includes('Synthetic YouTube Result') && text.includes('Transcribe'),
    hasCard: !!card,
    hasTitle: text.includes('Synthetic YouTube Result'),
    hasTranscribeBadge: text.includes('Transcribe')
  };
})()
""",
    )

    start_clicked = await cdp.evaluate(
        r"""
(() => {
  const card = document.querySelector('[aria-label="Start transcription for Synthetic YouTube Result"]');
  if (!card) return { ok: false, reason: 'missing result card' };
  card.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not start_clicked or not start_clicked.get("ok"):
        raise RuntimeError(f"Could not click YouTube transcription result: {start_clicked}")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and not backend.youtube_transcribe_requests:
        await asyncio.sleep(0.1)
    if not backend.youtube_transcribe_requests:
        raise RuntimeError("YouTube result click did not call the synthetic transcribe endpoint")

    queued_state = await wait_for_interaction_state(
        cdp,
        label="youtube-start-transcription",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: window.location.pathname === '/transcript/youtube-queued-smoke'
      && text.includes('Synthetic Queued YouTube Transcription')
      && text.includes('Queued')
      && text.includes('Elapsed:')
      && text.includes('Stop'),
    route: window.location.pathname,
    hasTitle: text.includes('Synthetic Queued YouTube Transcription'),
    hasQueuedStep: text.includes('Queued'),
    hasElapsed: text.includes('Elapsed:'),
    hasStop: text.includes('Stop')
  };
})()
""",
    )
    queued_state["backendRequestCount"] = len(backend.youtube_transcribe_requests)
    queued_state["backendRequest"] = backend.youtube_transcribe_requests[-1]
    return {
        "name": "youtube-start-transcription",
        "ok": True,
        "result": result_state,
        "queued": queued_state,
    }


async def exercise_file_history_interactions(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    initial_state = await wait_for_interaction_state(
        cdp,
        label="file-history-initial",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  const text = document.body ? document.body.innerText : '';
  return {
    ok: !!root
      && document.querySelectorAll('.perf-scroll-item').length > 0
      && text.includes('Synthetic File 00001')
      && text.includes('Synthetic processes files in-app up to 2GB')
      && text.includes('Video: MP4, MOV, etc. (max 2GB, audio extracted)'),
    view: root?.getAttribute('data-history-view') || '',
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    hasUploadLimitHint: text.includes('Synthetic processes files in-app up to 2GB'),
    hasVideoLimitHint: text.includes('Video: MP4, MOV, etc. (max 2GB, audio extracted)'),
    hasFirstFile: text.includes('Synthetic File 00001')
  };
})()
""",
    )

    processing_queue_state = await wait_for_interaction_state(
        cdp,
        label="file-processing-queue",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const normalizedText = text.toLowerCase();
  const viewButton = document.querySelector('button[aria-label="View transcript Synthetic File Processing"]');
  return {
    ok: normalizedText.includes('processing queue')
      && text.includes('Synthetic File Processing')
      && text.includes('Preparing audio')
      && !!viewButton,
    hasProcessingQueue: normalizedText.includes('processing queue'),
    hasProcessingTitle: text.includes('Synthetic File Processing'),
    hasStep: text.includes('Preparing audio'),
    hasViewButton: !!viewButton
  };
})()
""",
    )

    view_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="View transcript Synthetic File Processing"]');
  if (!button) return { ok: false, reason: 'missing processing view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not view_clicked or not view_clicked.get("ok"):
        raise RuntimeError(f"Could not open file processing transcript: {view_clicked}")

    processing_detail_state = await wait_for_interaction_state(
        cdp,
        label="file-processing-detail",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: window.location.pathname === '/transcript/file-processing-smoke'
      && text.includes('Synthetic File Processing')
      && text.includes('Preparing audio')
      && text.includes('Elapsed:')
      && text.includes('Stop'),
    route: window.location.pathname,
    hasTitle: text.includes('Synthetic File Processing'),
    hasStep: text.includes('Preparing audio'),
    hasElapsed: text.includes('Elapsed:'),
    hasStop: text.includes('Stop')
  };
})()
""",
    )

    returned_to_file = await cdp.evaluate(
        r"""
(() => {
  window.location.href = `${window.location.origin}/file`;
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not returned_to_file or not returned_to_file.get("ok"):
        raise RuntimeError(f"Could not navigate back to file route: {returned_to_file}")
    await wait_for_route_ready(
        cdp,
        route="/file",
        expected_text=ROUTE_EXPECTATIONS["/file"],
        expect_history_virtualized=True,
        timeout_sec=timeout_sec,
    )

    list_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="List view"]');
  if (!button) return { ok: false, reason: 'missing list view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not list_clicked or not list_clicked.get("ok"):
        raise RuntimeError(f"Could not switch file history to list view: {list_clicked}")

    list_state = await wait_for_interaction_state(
        cdp,
        label="file-history-list-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'list',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    search_state = await wait_for_interaction_state(
        cdp,
        label="file-history-search",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Search files'));
  if (!input) return { ok: false, reason: 'missing file search input' };
  if (input.value !== '00002') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '00002');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForSearch: true };
  }
  const text = document.body ? document.body.innerText : '';
  const query = new URLSearchParams(window.location.search).get('q') || '';
  return {
    ok: query === '00002'
      && text.includes('Synthetic File 00002')
      && !text.includes('Synthetic File 00001'),
    query,
    hasTarget: text.includes('Synthetic File 00002'),
    hasFilteredOutFirst: !text.includes('Synthetic File 00001'),
    visibleCards: document.querySelectorAll('.perf-scroll-item').length
  };
})()
""",
    )

    clipboard_state = await wait_for_interaction_state(
        cdp,
        label="file-history-copy",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  if (!window.__scriberSmokeFileClipboardWrites) {
    const writes = [];
    const clipboard = { writeText: async (value) => { writes.push(String(value)); } };
    window.__scriberSmokeFileClipboardWrites = writes;
    let stubbed = false;
    try {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: clipboard
      });
      stubbed = true;
    } catch (error) {
      try {
        Object.defineProperty(Navigator.prototype, 'clipboard', {
          configurable: true,
          get: () => clipboard
        });
        stubbed = true;
      } catch (_fallbackError) {
        window.__scriberSmokeFileClipboardStubError = String(error);
      }
    }
    window.__scriberSmokeFileClipboardStubbed = stubbed;
  }
  const button = document.querySelector('button[aria-label="Copy transcript Synthetic File 00002"]');
  if (!button) {
    return {
      ok: false,
      reason: 'missing file copy button',
      stubbed: !!window.__scriberSmokeFileClipboardStubbed,
      stubError: window.__scriberSmokeFileClipboardStubError || ''
    };
  }
  if (!window.__scriberSmokeFileCopyClicked) {
    window.__scriberSmokeFileCopyClicked = true;
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeFileClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeFileClipboardWrites || [];
  return {
    ok: writes.some((value) => value.includes('synthetic transcript used by the frontend browser smoke test')),
    writes,
    stubbed: !!window.__scriberSmokeFileClipboardStubbed,
    toastVisible: (document.body?.innerText || '').includes('Transcript copied to clipboard.')
  };
})()
""",
    )

    delete_state = await wait_for_interaction_state(
        cdp,
        label="file-history-delete",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const button = document.querySelector('button[aria-label="Delete transcript Synthetic File 00002"]');
  if (!button && !window.__scriberSmokeFileDeleteClicked) {
    return { ok: false, reason: 'missing file delete button' };
  }
  if (button && !window.__scriberSmokeFileDeleteClicked) {
    window.__scriberSmokeFileDeleteClicked = true;
    button.click();
    return { ok: false, waitingForDelete: true };
  }
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('Transcript removed successfully.'),
    hasDeletedToast: text.includes('Transcript removed successfully.')
  };
})()
""",
    )

    clear_search_state = await wait_for_interaction_state(
        cdp,
        label="file-history-clear-search-before-grid",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Search files'));
  if (!input) return { ok: false, reason: 'missing file history search input' };
  if (input.value !== '') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForClear: true };
  }
  const root = document.querySelector('[data-history-virtualized="true"]');
  const text = document.body ? document.body.innerText : '';
  const query = new URLSearchParams(window.location.search).get('q') || '';
  return {
    ok: query === '' && !!root && text.includes('Synthetic File 00001'),
    query,
    hasRoot: !!root,
    hasFirstFile: text.includes('Synthetic File 00001')
  };
})()
""",
    )

    grid_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="Grid view"]');
  if (!button) return { ok: false, reason: 'missing grid view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not grid_clicked or not grid_clicked.get("ok"):
        raise RuntimeError(f"Could not switch file history to grid view: {grid_clicked}")

    grid_state = await wait_for_interaction_state(
        cdp,
        label="file-history-grid-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'grid',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    return {
        "name": "file-history-actions",
        "ok": True,
        "initial": initial_state,
        "processingQueue": processing_queue_state,
        "processingDetail": processing_detail_state,
        "listView": list_state,
        "search": search_state,
        "copy": clipboard_state,
        "delete": delete_state,
        "clearSearch": clear_search_state,
        "gridView": grid_state,
    }


async def exercise_file_upload_error_interaction(cdp: CdpClient, *, timeout_sec: float) -> dict[str, Any]:
    drop_started = await cdp.evaluate(
        r"""
(() => {
  const dropzone = document.querySelector('[aria-label="Upload file for transcription"]');
  if (!dropzone) return { ok: false, reason: 'missing dropzone' };
  const file = new File([new Uint8Array([82, 73, 70, 70])], 'too-large-smoke.wav', { type: 'audio/wav' });
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
        raise RuntimeError(f"Could not dispatch file upload error interaction: {drop_started}")

    state = await wait_for_interaction_state(
        cdp,
        label="file-upload-error",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: window.location.pathname === '/file'
      && text.includes('Upload failed')
      && text.includes('Synthetic upload limit exceeded'),
    route: window.location.pathname,
    hasUploadFailed: text.includes('Upload failed'),
    hasProviderError: text.includes('Synthetic upload limit exceeded')
  };
})()
""",
    )
    return {"name": "file-upload-error", "ok": True, "state": state}


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


async def exercise_debug_console_interaction(
    cdp: CdpClient,
    *,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    backend.support_bundle_count = 0
    initial = await cdp.evaluate(
        r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const stickyRoot = document.querySelector('.sticky');
  const dateInput = document.querySelector('input[type="date"]');
  const newestSwitch = document.querySelector('[aria-label="Show newest logs first"]');
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
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

    refresh_before = backend.runtime_logs_count
    refresh_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.getAttribute('aria-label') || '') === 'Refresh logs');
  if (!button) return { ok: false, reason: 'missing refresh button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not refresh_started or not refresh_started.get("ok"):
        raise RuntimeError(f"Could not click debug refresh button: {refresh_started}")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and backend.runtime_logs_count <= refresh_before:
        await asyncio.sleep(0.1)
    if backend.runtime_logs_count <= refresh_before:
        raise RuntimeError(
            "Debug refresh button did not request runtime logs. "
            f"Before={refresh_before}, after={backend.runtime_logs_count}"
        )

    refresh_state = await wait_for_interaction_state(
        cdp,
        label="debug-refresh-controls-refresh",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('3 of 3 entries') && text.includes('Debug console sample error'),
    hasAllEntries: text.includes('3 of 3 entries'),
    hasErrorLog: text.includes('Debug console sample error')
  };
})()
""",
    )
    refresh_state["runtimeLogsBefore"] = refresh_before
    refresh_state["runtimeLogsAfter"] = backend.runtime_logs_count

    toggles_off = await wait_for_interaction_state(
        cdp,
        label="debug-refresh-controls-toggles-off",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const autoRefresh = document.querySelector('[aria-label="Toggle auto refresh"]');
  const autoScroll = document.querySelector('[aria-label="Toggle auto scroll"]');
  const newest = document.querySelector('[aria-label="Show newest logs first"]');
  if (!autoRefresh || !autoScroll || !newest) {
    return { ok: false, reason: 'missing debug switches' };
  }
  if (!window.__scriberSmokeDebugTogglesOffClicked) {
    window.__scriberSmokeDebugTogglesOffClicked = true;
    if (autoRefresh.getAttribute('aria-checked') === 'true') autoRefresh.click();
    if (autoScroll.getAttribute('aria-checked') === 'true') autoScroll.click();
    if (newest.getAttribute('aria-checked') === 'true') newest.click();
    return { ok: false, waitingForToggleState: true };
  }
  return {
    ok: autoRefresh.getAttribute('aria-checked') === 'false'
      && autoScroll.getAttribute('aria-checked') === 'false'
      && newest.getAttribute('aria-checked') === 'false',
    autoRefresh: autoRefresh.getAttribute('aria-checked'),
    autoScroll: autoScroll.getAttribute('aria-checked'),
    newestFirst: newest.getAttribute('aria-checked')
  };
})()
""",
    )

    toggles_on = await wait_for_interaction_state(
        cdp,
        label="debug-refresh-controls-toggles-on",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const autoRefresh = document.querySelector('[aria-label="Toggle auto refresh"]');
  const autoScroll = document.querySelector('[aria-label="Toggle auto scroll"]');
  const newest = document.querySelector('[aria-label="Show newest logs first"]');
  if (!autoRefresh || !autoScroll || !newest) {
    return { ok: false, reason: 'missing debug switches' };
  }
  if (!window.__scriberSmokeDebugTogglesOnClicked) {
    window.__scriberSmokeDebugTogglesOnClicked = true;
    if (autoRefresh.getAttribute('aria-checked') !== 'true') autoRefresh.click();
    if (autoScroll.getAttribute('aria-checked') !== 'true') autoScroll.click();
    if (newest.getAttribute('aria-checked') !== 'true') newest.click();
    return { ok: false, waitingForToggleState: true };
  }
  return {
    ok: autoRefresh.getAttribute('aria-checked') === 'true'
      && autoScroll.getAttribute('aria-checked') === 'true'
      && newest.getAttribute('aria-checked') === 'true',
    autoRefresh: autoRefresh.getAttribute('aria-checked'),
    autoScroll: autoScroll.getAttribute('aria-checked'),
    newestFirst: newest.getAttribute('aria-checked')
  };
})()
""",
    )

    filter_state = await wait_for_interaction_state(
        cdp,
        label="debug-refresh-controls-filter",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = document.querySelector('input[aria-label="Filter logs"]');
  if (!input) return { ok: false, reason: 'missing filter input' };
  if (input.value !== 'warning') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, 'warning');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForFilter: true };
  }
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('1 of 3 entries')
      && text.includes('Debug console sample warning')
      && !text.includes('Debug console sample error'),
    hasOneEntry: text.includes('1 of 3 entries'),
    hasWarningLog: text.includes('Debug console sample warning'),
    errorHidden: !text.includes('Debug console sample error')
  };
})()
""",
    )

    reset_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.textContent || '').includes('Reset filters'));
  if (!button) return { ok: false, reason: 'missing reset filters button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not reset_started or not reset_started.get("ok"):
        raise RuntimeError(f"Could not reset debug filters: {reset_started}")

    reset_state = await wait_for_interaction_state(
        cdp,
        label="debug-refresh-controls-reset",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = document.querySelector('input[aria-label="Filter logs"]');
  const text = document.body ? document.body.innerText : '';
  return {
    ok: input
      && input.value === ''
      && text.includes('3 of 3 entries')
      && text.includes('Debug console sample error')
      && text.includes('Debug console sample warning'),
    filterValue: input ? input.value : null,
    hasAllEntries: text.includes('3 of 3 entries'),
    hasErrorLog: text.includes('Debug console sample error'),
    hasWarningLog: text.includes('Debug console sample warning')
  };
})()
""",
    )
    refresh_controls = {
        "refresh": refresh_state,
        "togglesOff": toggles_off,
        "togglesOn": toggles_on,
        "filter": filter_state,
        "reset": reset_state,
    }

    spy_setup = await cdp.evaluate(
        r"""
(() => {
  const writes = [];
  const downloads = [];
  const objectUrls = [];
  const clipboard = { writeText: async (value) => { writes.push(String(value)); } };
  window.__scriberSmokeDebugClipboardWrites = writes;
  window.__scriberSmokeDebugDownloads = downloads;
  window.__scriberSmokeDebugObjectUrls = objectUrls;
  let clipboardStubbed = false;
  try {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: clipboard
    });
    clipboardStubbed = true;
  } catch (error) {
    try {
      Object.defineProperty(Navigator.prototype, 'clipboard', {
        configurable: true,
        get: () => clipboard
      });
      clipboardStubbed = true;
    } catch (_fallbackError) {
      window.__scriberSmokeDebugClipboardStubError = String(error);
    }
  }
  window.__scriberSmokeDebugOriginalCreateObjectURL = URL.createObjectURL;
  window.__scriberSmokeDebugOriginalRevokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = (blob) => {
    objectUrls.push({ type: String(blob?.type || ''), size: Number(blob?.size || 0) });
    return 'blob:scriber-debug-support-bundle';
  };
  URL.revokeObjectURL = (url) => {
    window.__scriberSmokeDebugRevokedUrl = String(url);
  };
  if (!window.__scriberSmokeDebugOriginalAnchorClick) {
    window.__scriberSmokeDebugOriginalAnchorClick = HTMLAnchorElement.prototype.click;
  }
  HTMLAnchorElement.prototype.click = function() {
    downloads.push({
      href: String(this.href || ''),
      download: String(this.download || '')
    });
  };
  return { ok: true, clipboardStubbed };
})()
""",
        timeout=5,
    )
    if not spy_setup or not spy_setup.get("ok"):
        raise RuntimeError(f"Could not install debug console spies: {spy_setup}")

    copy_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.getAttribute('aria-label') || '') === 'Copy visible logs');
  if (!button) return { ok: false, reason: 'missing copy button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not copy_started or not copy_started.get("ok"):
        raise RuntimeError(f"Could not click debug copy button: {copy_started}")

    copied = await wait_for_interaction_state(
        cdp,
        label="debug-copy-visible",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const writes = window.__scriberSmokeDebugClipboardWrites || [];
  const text = document.body ? document.body.innerText : '';
  const copiedText = writes.join('\n');
  return {
    ok: writes.length === 1
      && copiedText.includes('Debug console sample error OPENAI_API_KEY=[REDACTED]')
      && copiedText.includes('ui-debug-sample.log:3')
      && text.includes('Copied 3 visible log entries.'),
    writes,
    hasRedactedSecret: copiedText.includes('OPENAI_API_KEY=[REDACTED]'),
    hasActionStatus: text.includes('Copied 3 visible log entries.')
  };
})()
""",
    )

    support_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.getAttribute('aria-label') || '') === 'Download support bundle');
  if (!button) return { ok: false, reason: 'missing support bundle button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not support_started or not support_started.get("ok"):
        raise RuntimeError(f"Could not click debug support bundle button: {support_started}")

    support = await wait_for_interaction_state(
        cdp,
        label="debug-support-bundle",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const downloads = window.__scriberSmokeDebugDownloads || [];
  const objectUrls = window.__scriberSmokeDebugObjectUrls || [];
  const text = document.body ? document.body.innerText : '';
  return {
    ok: downloads.some((item) => String(item.download || '').endsWith('.zip'))
      && objectUrls.some((item) => item.type === 'application/zip' && item.size > 0)
      && text.includes('Support bundle downloaded as'),
    downloads,
    objectUrls,
    revokedUrl: window.__scriberSmokeDebugRevokedUrl || '',
    hasActionStatus: text.includes('Support bundle downloaded as')
  };
})()
""",
    )
    support["backendPostCount"] = backend.support_bundle_count
    if backend.support_bundle_count < 1:
        raise RuntimeError("Support bundle action did not call synthetic backend")

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
    return {
        "name": "debug-console-actions",
        "ok": True,
        "initial": initial,
        "refreshControls": refresh_controls,
        "spySetup": spy_setup,
        "copied": copied,
        "supportBundle": support,
        "cleared": cleared,
    }


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


async def exercise_transcript_cancel_action(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    transcript_id = "youtube-cancel-smoke"
    backend.canceled_transcripts.discard(transcript_id)
    backend.cancel_counts.pop(transcript_id, None)
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/{transcript_id}"}, timeout=10)
    initial_state = await wait_for_route_ready(
        cdp,
        route=f"/transcript/{transcript_id}",
        expected_text=[
            "Synthetic Cancel Processing",
            "Downloading audio",
            "Elapsed:",
            "Stop",
        ],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )

    stop_started = await cdp.evaluate(
        r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.textContent || '').trim() === 'Stop');
  if (!button) return { ok: false, reason: 'missing stop button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not stop_started or not stop_started.get("ok"):
        raise RuntimeError(f"Could not click transcript Stop button: {stop_started}")

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and backend.cancel_counts.get(transcript_id, 0) < 1:
        await asyncio.sleep(0.1)
    if backend.cancel_counts.get(transcript_id, 0) < 1:
        raise RuntimeError("Transcript Stop button did not call the synthetic cancel endpoint")

    stopped_state = await wait_for_interaction_state(
        cdp,
        label="transcript-cancel-action",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const stopButtonStillVisible = Array.from(document.querySelectorAll('button'))
    .some((node) => (node.textContent || '').trim() === 'Stop');
  return {
    ok: text.includes('Task cancellation requested.')
      && text.includes('Synthetic Cancel Processing')
      && !text.includes('Downloading audio')
      && !text.includes('Elapsed:')
      && !stopButtonStillVisible,
    route: window.location.pathname,
    hasToast: text.includes('Task cancellation requested.'),
    hasTitle: text.includes('Synthetic Cancel Processing'),
    processingBannerHidden: !text.includes('Downloading audio') && !text.includes('Elapsed:'),
    stopButtonStillVisible
  };
})()
""",
    )
    stopped_state["backendCancelCount"] = backend.cancel_counts.get(transcript_id, 0)
    return {
        "name": "transcript-cancel-action",
        "ok": True,
        "initial": initial_state,
        "stopped": stopped_state,
    }


async def exercise_transcript_detail_actions(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/mic-00001"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/transcript/mic-00001",
        expected_text=ROUTE_EXPECTATIONS["/transcript/mic-00001"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )

    setup_state = await cdp.evaluate(
        r"""
(() => {
  const writes = [];
  const opened = [];
  const clipboard = { writeText: async (value) => { writes.push(String(value)); } };
  window.__scriberSmokeDetailClipboardWrites = writes;
  window.__scriberSmokeOpenedUrls = opened;
  window.__scriberSmokeOriginalOpen = window.open;
  window.open = (url, target) => {
    opened.push({ url: String(url), target: String(target || '') });
    return null;
  };
  let clipboardStubbed = false;
  try {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: clipboard
    });
    clipboardStubbed = true;
  } catch (error) {
    try {
      Object.defineProperty(Navigator.prototype, 'clipboard', {
        configurable: true,
        get: () => clipboard
      });
      clipboardStubbed = true;
    } catch (_fallbackError) {
      window.__scriberSmokeDetailClipboardStubError = String(error);
    }
  }
  window.__scriberSmokeDetailClipboardStubbed = clipboardStubbed;
  return { ok: true, clipboardStubbed };
})()
""",
        timeout=5,
    )
    if not setup_state or not setup_state.get("ok"):
        raise RuntimeError(f"Could not install transcript detail action spies: {setup_state}")

    copy_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-copy",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  if (!window.__scriberSmokeDetailCopyStarted) {
    const transcriptButton = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Copy Transcript'));
    const summaryButton = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Copy Summary'));
    if (!transcriptButton || !summaryButton) {
      return {
        ok: false,
        reason: 'missing copy buttons',
        hasTranscriptButton: !!transcriptButton,
        hasSummaryButton: !!summaryButton
      };
    }
    window.__scriberSmokeDetailCopyStarted = true;
    transcriptButton.click();
    summaryButton.click();
    return { ok: false, waitingForCopy: true };
  }
  const writes = window.__scriberSmokeDetailClipboardWrites || [];
  return {
    ok: writes.some((value) => value.includes('synthetic transcript used by the frontend browser smoke test'))
      && writes.some((value) => value.includes('Synthetic summary for browser smoke.')),
    writes,
    clipboardStubbed: !!window.__scriberSmokeDetailClipboardStubbed,
    toastVisible: (document.body?.innerText || '').includes('Copied to Clipboard')
  };
})()
""",
    )

    export_pdf_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-export-pdf",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const activate = (node) => {
    node.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 }));
    node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
    node.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 }));
    node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0 }));
    node.click();
  };
  const opened = window.__scriberSmokeOpenedUrls || [];
  if (!window.__scriberSmokePdfExportClicked) {
    const exportButton = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Export'));
    if (!exportButton) return { ok: false, reason: 'missing export button' };
    window.__scriberSmokePdfExportClicked = true;
    activate(exportButton);
    return { ok: false, waitingForMenu: true };
  }
  if (!window.__scriberSmokePdfMenuClicked) {
    const pdfItem = Array.from(document.querySelectorAll('[role="menuitem"]'))
      .find((node) => (node.textContent || '').includes('Export as PDF'));
    if (!pdfItem) return { ok: false, reason: 'missing pdf export menu item' };
    window.__scriberSmokePdfMenuClicked = true;
    activate(pdfItem);
    return { ok: false, waitingForPdfOpen: true };
  }
  return {
    ok: opened.some((item) => String(item.url).includes('/api/transcripts/mic-00001/export/pdf')),
    opened
  };
})()
""",
    )

    export_docx_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-export-docx",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const activate = (node) => {
    node.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 }));
    node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
    node.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 }));
    node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0 }));
    node.click();
  };
  const opened = window.__scriberSmokeOpenedUrls || [];
  if (!window.__scriberSmokeDocxExportClicked) {
    const exportButton = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Export'));
    if (!exportButton) return { ok: false, reason: 'missing export button' };
    window.__scriberSmokeDocxExportClicked = true;
    activate(exportButton);
    return { ok: false, waitingForMenu: true };
  }
  if (!window.__scriberSmokeDocxMenuClicked) {
    const docxItem = Array.from(document.querySelectorAll('[role="menuitem"]'))
      .find((node) => (node.textContent || '').includes('Export as DOCX'));
    if (!docxItem) return { ok: false, reason: 'missing docx export menu item' };
    window.__scriberSmokeDocxMenuClicked = true;
    activate(docxItem);
    return { ok: false, waitingForDocxOpen: true };
  }
  return {
    ok: opened.some((item) => String(item.url).includes('/api/transcripts/mic-00001/export/docx')),
    opened
  };
})()
""",
    )

    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/mic-no-summary-smoke"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/transcript/mic-no-summary-smoke",
        expected_text=ROUTE_EXPECTATIONS["/transcript/mic-no-summary-smoke"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )
    summarize_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-summarize",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  if (!window.__scriberSmokeSummarizeClicked) {
    const button = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').trim() === 'Summarize');
    if (!button) {
      return {
        ok: false,
        reason: 'missing summarize ready state',
        hasButton: !!button,
        hasGeneratedSummary: text.includes('Synthetic manual summary generated by browser smoke.')
      };
    }
    window.__scriberSmokeSummarizeClicked = true;
    button.click();
    return { ok: false, waitingForSummary: true };
  }
  return {
    ok: text.includes('Synthetic manual summary generated by browser smoke.')
      && text.includes('Summary generated'),
    hasGeneratedSummary: text.includes('Synthetic manual summary generated by browser smoke.'),
    hasToast: text.includes('Summary generated')
  };
})()
""",
    )

    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/mic-summary-failed-smoke"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/transcript/mic-summary-failed-smoke",
        expected_text=ROUTE_EXPECTATIONS["/transcript/mic-summary-failed-smoke"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )
    retry_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-retry-summary",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  if (!window.__scriberSmokeRetrySummaryClicked) {
    const button = Array.from(document.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Retry Summary'));
    if (!button || !text.includes('Summary generation failed')) {
      return {
        ok: false,
        reason: 'missing retry summary ready state',
        hasButton: !!button,
        hasFailedState: text.includes('Summary generation failed')
      };
    }
    window.__scriberSmokeRetrySummaryClicked = true;
    button.click();
    return { ok: false, waitingForRetrySummary: true };
  }
  return {
    ok: text.includes('Synthetic retry summary generated by browser smoke.')
      && text.includes('Summary generated')
      && !text.includes('Summary generation failed'),
    hasRetrySummary: text.includes('Synthetic retry summary generated by browser smoke.'),
    failedStateCleared: !text.includes('Summary generation failed'),
    hasToast: text.includes('Summary generated')
  };
})()
""",
    )

    return {
        "name": "transcript-detail-actions",
        "ok": True,
        "spySetup": setup_state,
        "copy": copy_state,
        "exportPdf": export_pdf_state,
        "exportDocx": export_docx_state,
        "summarize": summarize_state,
        "retrySummary": retry_state,
    }


async def exercise_history_interactions(
    cdp: CdpClient,
    *,
    backend: FrontendSmokeBackend,
    frontend_base_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    initial_state = await wait_for_interaction_state(
        cdp,
        label="history-initial",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  const text = document.body ? document.body.innerText : '';
  return {
    ok: !!root
      && document.querySelectorAll('.perf-scroll-item').length > 0
      && text.includes('Synthetic Recording 00001'),
    view: root?.getAttribute('data-history-view') || '',
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    hasFirstRecording: text.includes('Synthetic Recording 00001')
  };
})()
""",
    )

    list_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="List view"]');
  if (!button) return { ok: false, reason: 'missing list view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not list_clicked or not list_clicked.get("ok"):
        raise RuntimeError(f"Could not switch history to list view: {list_clicked}")

    list_state = await wait_for_interaction_state(
        cdp,
        label="history-list-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'list',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    search_state = await wait_for_interaction_state(
        cdp,
        label="history-search",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const input = Array.from(document.querySelectorAll('input'))
    .find((node) => (node.getAttribute('placeholder') || '').includes('Search recordings'));
  if (!input) return { ok: false, reason: 'missing recording search input' };
  if (input.value !== '00002') {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '00002');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: false, waitingForSearch: true };
  }
  const text = document.body ? document.body.innerText : '';
  const query = new URLSearchParams(window.location.search).get('q') || '';
  return {
    ok: query === '00002'
      && text.includes('Synthetic Recording 00002')
      && !text.includes('Synthetic Recording 00001'),
    query,
    hasTarget: text.includes('Synthetic Recording 00002'),
    hasFilteredOutFirst: !text.includes('Synthetic Recording 00001'),
    visibleCards: document.querySelectorAll('.perf-scroll-item').length
  };
})()
""",
    )

    clipboard_state = await wait_for_interaction_state(
        cdp,
        label="history-copy",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  if (!window.__scriberSmokeClipboardWrites) {
    const writes = [];
    const clipboard = { writeText: async (value) => { writes.push(String(value)); } };
    window.__scriberSmokeClipboardWrites = writes;
    let stubbed = false;
    try {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: clipboard
      });
      stubbed = true;
    } catch (error) {
      try {
        Object.defineProperty(Navigator.prototype, 'clipboard', {
          configurable: true,
          get: () => clipboard
        });
        stubbed = true;
      } catch (_fallbackError) {
        window.__scriberSmokeClipboardStubError = String(error);
      }
    }
    window.__scriberSmokeClipboardStubbed = stubbed;
  }
  const button = document.querySelector('button[aria-label="Copy transcript Synthetic Recording 00002"]');
  if (!button) {
    return {
      ok: false,
      reason: 'missing copy button',
      stubbed: !!window.__scriberSmokeClipboardStubbed,
      stubError: window.__scriberSmokeClipboardStubError || ''
    };
  }
  if (!window.__scriberSmokeCopyClicked) {
    window.__scriberSmokeCopyClicked = true;
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeClipboardWrites || [];
  return {
    ok: writes.some((value) => value.includes('synthetic transcript used by the frontend browser smoke test')),
    writes,
    stubbed: !!window.__scriberSmokeClipboardStubbed,
    toastVisible: (document.body?.innerText || '').includes('Transcript copied to clipboard.')
  };
})()
""",
    )

    grid_clicked = await cdp.evaluate(
        r"""
(() => {
  const button = document.querySelector('button[aria-label="Grid view"]');
  if (!button) return { ok: false, reason: 'missing grid view button' };
  button.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not grid_clicked or not grid_clicked.get("ok"):
        raise RuntimeError(f"Could not switch history to grid view: {grid_clicked}")

    grid_state = await wait_for_interaction_state(
        cdp,
        label="history-grid-view",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const root = document.querySelector('[data-history-virtualized="true"]');
  return {
    ok: root?.getAttribute('data-history-view') === 'grid',
    view: root?.getAttribute('data-history-view') || ''
  };
})()
""",
    )

    delete_state = await wait_for_interaction_state(
        cdp,
        label="history-delete",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  if (!window.__scriberSmokeHistoryDeleteClicked) {
    const button = document.querySelector('button[aria-label="Delete transcript Synthetic Recording 00002"]');
    if (!button || !text.includes('Synthetic Recording 00002')) {
      return {
        ok: false,
        reason: 'missing visible delete target',
        hasDeleteButton: !!button,
        hasDeleteTarget: text.includes('Synthetic Recording 00002')
      };
    }
    window.__scriberSmokeHistoryDeleteClicked = true;
    button.click();
    return { ok: false, waitingForDelete: true };
  }
  return {
    ok: text.includes('Deleted')
      && !text.includes('Synthetic Recording 00002'),
    hasDeletedToast: text.includes('Deleted'),
    targetRemoved: !text.includes('Synthetic Recording 00002'),
    visibleCards: document.querySelectorAll('.perf-scroll-item').length
  };
})()
""",
    )
    delete_state["backend"] = {
        "deletedTranscriptIds": sorted(backend.deleted_transcript_ids),
        "targetDeleted": "mic-00001" in backend.deleted_transcript_ids,
        "unrelatedControlDeleted": "mic-00002" in backend.deleted_transcript_ids,
    }
    if not delete_state["backend"]["targetDeleted"] or delete_state["backend"]["unrelatedControlDeleted"]:
        raise RuntimeError(f"Unexpected backend delete state: {delete_state['backend']}")

    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/mic-00001"}, timeout=10)
    detail_state = await wait_for_interaction_state(
        cdp,
        label="history-detail-nav",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    ok: window.location.pathname === '/transcript/mic-00001'
      && text.includes('Synthetic Recording 00002')
      && text.includes('Synthetic summary for browser smoke.'),
    route: window.location.pathname,
    hasTitle: text.includes('Synthetic Recording 00002'),
    hasSummary: text.includes('Synthetic summary for browser smoke.')
  };
})()
""",
    )

    return {
        "name": "history-search-copy-navigation",
        "ok": True,
        "initial": initial_state,
        "listView": list_state,
        "search": search_state,
        "copy": clipboard_state,
        "gridView": grid_state,
        "delete": delete_state,
        "detail": detail_state,
    }


async def exercise_command_palette(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/",
        expected_text=ROUTE_EXPECTATIONS["/"],
        expect_history_virtualized=True,
        timeout_sec=timeout_sec,
    )

    open_palette = await cdp.evaluate(
        r"""
(() => {
  const eventInit = { key: 'k', code: 'KeyK', ctrlKey: true, bubbles: true, cancelable: true };
  document.dispatchEvent(new KeyboardEvent('keydown', eventInit));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not open_palette or not open_palette.get("ok"):
        raise RuntimeError(f"Could not dispatch command palette shortcut: {open_palette}")

    initial_state = await wait_for_interaction_state(
        cdp,
        label="command-palette-open",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const text = dialog ? dialog.innerText : '';
  return {
    ok: !!dialog
      && text.includes('Aktionen')
      && text.includes('Navigation')
      && text.includes('Aufnahme starten')
      && text.includes('Debug-Konsole'),
    hasDialog: !!dialog,
    text: text.slice(0, 600)
  };
})()
""",
    )

    debug_clicked = await cdp.evaluate(
        r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const item = Array.from((dialog || document).querySelectorAll('[cmdk-item]'))
    .find((node) => (node.textContent || '').includes('Debug-Konsole'));
  if (!item) return { ok: false, reason: 'missing debug item' };
  item.dispatchEvent(new PointerEvent('pointermove', { bubbles: true }));
  item.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not debug_clicked or not debug_clicked.get("ok"):
        raise RuntimeError(f"Could not click Debug-Konsole command palette item: {debug_clicked}")

    debug_state = await wait_for_interaction_state(
        cdp,
        label="command-palette-debug-nav",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const dialog = document.querySelector('[role="dialog"]');
  return {
    ok: window.location.pathname === '/debug'
      && text.includes('Debug Console')
      && !dialog,
    route: window.location.pathname,
    paletteClosed: !dialog
  };
})()
""",
    )

    reopen_palette = await cdp.evaluate(
        r"""
(() => {
  document.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'k',
    code: 'KeyK',
    ctrlKey: true,
    bubbles: true,
    cancelable: true
  }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not reopen_palette or not reopen_palette.get("ok"):
        raise RuntimeError(f"Could not reopen command palette: {reopen_palette}")

    transcript_search_state = await wait_for_interaction_state(
        cdp,
        label="command-palette-transcript-loaded",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const input = dialog ? dialog.querySelector('input') : null;
  if (!dialog || !input) return { ok: false, hasDialog: !!dialog };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'Synthetic Recording 00003');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  const text = dialog.innerText;
  return {
    ok: text.includes('Transkripte') && text.includes('Synthetic Recording 00003'),
    hasTranscriptGroup: text.includes('Transkripte'),
    hasTargetTranscript: text.includes('Synthetic Recording 00003')
  };
})()
""",
    )

    transcript_clicked = await cdp.evaluate(
        r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const item = Array.from((dialog || document).querySelectorAll('[cmdk-item]'))
    .find((node) => (node.textContent || '').includes('Synthetic Recording 00003'));
  if (!item) return { ok: false, reason: 'missing transcript item' };
  item.dispatchEvent(new PointerEvent('pointermove', { bubbles: true }));
  item.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not transcript_clicked or not transcript_clicked.get("ok"):
        raise RuntimeError(f"Could not click transcript command palette item: {transcript_clicked}")

    transcript_state = await wait_for_interaction_state(
        cdp,
        label="command-palette-transcript-nav",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const dialog = document.querySelector('[role="dialog"]');
  return {
    ok: window.location.pathname === '/transcript/mic-00002'
      && text.includes('Synthetic Recording 00003')
      && text.includes('Summary')
      && !dialog,
    route: window.location.pathname,
    paletteClosed: !dialog
  };
})()
""",
    )

    return {
        "name": "command-palette",
        "ok": True,
        "initial": initial_state,
        "debug": debug_state,
        "transcriptSearch": transcript_search_state,
        "transcript": transcript_state,
    }


async def exercise_mobile_navigation(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
        timeout=5,
    )
    await cdp.call(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": True, "configuration": "mobile"},
        timeout=5,
    )
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/",
        expected_text=ROUTE_EXPECTATIONS["/"],
        expect_history_virtualized=True,
        timeout_sec=timeout_sec,
    )

    header_state = await cdp.evaluate(
        r"""
(() => {
  const trigger = document.querySelector('button[aria-label="Open navigation"]');
  const desktopSidebar = document.querySelector('aside');
  const triggerStyle = trigger ? getComputedStyle(trigger) : null;
  const sidebarStyle = desktopSidebar ? getComputedStyle(desktopSidebar) : null;
  return {
    ok: !!trigger
      && triggerStyle.display !== 'none'
      && trigger.getBoundingClientRect().width >= 44
      && trigger.getBoundingClientRect().height >= 44
      && (!desktopSidebar || sidebarStyle.display === 'none'),
    triggerVisible: !!trigger && triggerStyle.display !== 'none',
    triggerWidth: trigger ? Math.round(trigger.getBoundingClientRect().width) : 0,
    triggerHeight: trigger ? Math.round(trigger.getBoundingClientRect().height) : 0,
    desktopSidebarDisplay: sidebarStyle ? sidebarStyle.display : '',
    viewportWidth: window.innerWidth
  };
})()
""",
        timeout=5,
    )
    if not header_state or not header_state.get("ok"):
        raise RuntimeError(f"Mobile navigation header was not usable: {header_state}")

    open_sheet = await cdp.evaluate(
        r"""
(() => {
  const trigger = document.querySelector('button[aria-label="Open navigation"]');
  if (!trigger) return { ok: false, reason: 'missing trigger' };
  trigger.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not open_sheet or not open_sheet.get("ok"):
        raise RuntimeError(f"Could not open mobile navigation sheet: {open_sheet}")

    sheet_state = await wait_for_interaction_state(
        cdp,
        label="mobile-navigation-sheet",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const links = Array.from((dialog || document).querySelectorAll('a'))
    .map((link) => (link.textContent || '').trim())
    .filter(Boolean);
  return {
    ok: !!dialog
      && ['Live Mic', 'YouTube', 'File', 'Console', 'Settings'].every((label) => links.includes(label)),
    hasDialog: !!dialog,
    links
  };
})()
""",
    )

    settings_clicked = await cdp.evaluate(
        r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const link = Array.from((dialog || document).querySelectorAll('a'))
    .find((node) => (node.textContent || '').trim() === 'Settings');
  if (!link) return { ok: false, reason: 'missing settings link' };
  link.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not settings_clicked or not settings_clicked.get("ok"):
        raise RuntimeError(f"Could not click mobile Settings nav item: {settings_clicked}")

    settings_state = await wait_for_interaction_state(
        cdp,
        label="mobile-navigation-settings",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const dialog = document.querySelector('[role="dialog"]');
  return {
    ok: window.location.pathname === '/settings'
      && text.includes('Settings')
      && text.includes('Transcription Settings')
      && !dialog,
    route: window.location.pathname,
    sheetClosed: !dialog
  };
})()
""",
    )

    reopen_sheet = await cdp.evaluate(
        r"""
(() => {
  const trigger = document.querySelector('button[aria-label="Open navigation"]');
  if (!trigger) return { ok: false, reason: 'missing trigger after settings nav' };
  trigger.click();
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not reopen_sheet or not reopen_sheet.get("ok"):
        raise RuntimeError(f"Could not reopen mobile navigation sheet: {reopen_sheet}")

    youtube_clicked = await wait_for_interaction_state(
        cdp,
        label="mobile-navigation-youtube-click",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const link = Array.from((dialog || document).querySelectorAll('a'))
    .find((node) => (node.textContent || '').trim() === 'YouTube');
  if (!dialog || !link) return { ok: false, hasDialog: !!dialog };
  link.click();
  return { ok: true };
})()
""",
    )

    youtube_state = await wait_for_interaction_state(
        cdp,
        label="mobile-navigation-youtube",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const dialog = document.querySelector('[role="dialog"]');
  return {
    ok: window.location.pathname === '/youtube'
      && text.includes('Youtube Transcription')
      && text.includes('Recent Videos')
      && !dialog,
    route: window.location.pathname,
    sheetClosed: !dialog
  };
})()
""",
    )

    await cdp.call("Emulation.clearDeviceMetricsOverride", timeout=5)
    await cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": False}, timeout=5)
    return {
        "name": "mobile-navigation",
        "ok": True,
        "header": header_state,
        "sheet": sheet_state,
        "settings": settings_state,
        "youtubeClick": youtube_clicked,
        "youtube": youtube_state,
    }


async def exercise_mobile_route_layouts(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    routes: list[str],
    timeout_sec: float,
) -> dict[str, Any]:
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
        timeout=5,
    )
    await cdp.call(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": True, "configuration": "mobile"},
        timeout=5,
    )

    results: list[dict[str, Any]] = []
    for route in routes:
        await cdp.call("Page.navigate", {"url": f"{frontend_base_url}{route}"}, timeout=10)
        excluded_expected_text = {
            "/debug": {"Copy visible", "Clear logs", "Support bundle"},
            "/transcript/mic-no-summary-smoke": {"Summarize"},
            "/transcript/mic-summary-failed-smoke": {"Summary generation failed", "Retry Summary"},
        }.get(route, set())
        expected_text = [
            value
            for value in ROUTE_EXPECTATIONS[route]
            if value not in excluded_expected_text
        ]
        await wait_for_route_ready(
            cdp,
            route=route,
            expected_text=expected_text,
            expect_history_virtualized=route in {"/", "/youtube", "/file"},
            timeout_sec=timeout_sec,
        )
        state = await cdp.evaluate(
            f"""
(() => {{
  const route = {json.dumps(route)};
  const doc = document.documentElement;
  const body = document.body;
  const main = document.querySelector('main') || body;
  const desktopSidebar = document.querySelector('aside');
  const sidebarStyle = desktopSidebar ? getComputedStyle(desktopSidebar) : null;
  const visibleControls = Array.from(document.querySelectorAll('button, a, input, textarea, [role="button"]'))
    .filter((node) => {{
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      const label = (node.textContent || node.getAttribute('aria-label') || '').trim();
      const intersectsViewport = rect.right > 0 && rect.bottom > 0 && rect.left < window.innerWidth && rect.top < window.innerHeight;
      return rect.width > 0
        && rect.height > 0
        && intersectsViewport
        && label !== 'Skip to main content'
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    }});
  const tooSmallControls = visibleControls
    .filter((node) => {{
      const tag = node.tagName.toLowerCase();
      if (tag === 'input' || tag === 'textarea') return false;
      const rect = node.getBoundingClientRect();
      return rect.width < 26 || rect.height < 26;
    }})
    .slice(0, 8)
    .map((node) => {{
      const rect = node.getBoundingClientRect();
      return {{
        tag: node.tagName.toLowerCase(),
        text: (node.textContent || node.getAttribute('aria-label') || '').trim().slice(0, 60),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }};
    }});
  const scrollWidth = Math.max(doc.scrollWidth || 0, body?.scrollWidth || 0);
  const overflowX = Math.max(0, scrollWidth - window.innerWidth);
  const mainTextLength = (main?.innerText || '').trim().length;
  return {{
    ok: overflowX <= 2
      && mainTextLength > 0
      && (!desktopSidebar || sidebarStyle.display === 'none' || route.startsWith('/transcript/'))
      && tooSmallControls.length === 0,
    route,
    viewportWidth: window.innerWidth,
    scrollWidth,
    overflowX,
    mainTextLength,
    desktopSidebarDisplay: sidebarStyle ? sidebarStyle.display : '',
    visibleControlCount: visibleControls.length,
    tooSmallControls
  }};
}})()
""",
            timeout=5,
        )
        if not state or not state.get("ok"):
            raise RuntimeError(f"Mobile route layout check failed for {route}: {state}")
        results.append(state)

    await cdp.call("Emulation.clearDeviceMetricsOverride", timeout=5)
    await cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": False}, timeout=5)
    return {
        "name": "mobile-route-layouts",
        "ok": bool(results) and all(item.get("ok") for item in results),
        "routes": routes,
        "routeCount": len(results),
        "maxOverflowX": max((float(item.get("overflowX") or 0) for item in results), default=0),
        "results": results,
    }


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
            command_palette_check: dict[str, Any] | None = None
            transcript_detail_actions_check: dict[str, Any] | None = None
            transcript_cancel_check: dict[str, Any] | None = None
            fast_tab_switch_check: dict[str, Any] | None = None
            mobile_navigation_check: dict[str, Any] | None = None
            mobile_route_layouts_check: dict[str, Any] | None = None
            token_required_check: dict[str, Any] | None = None
            for route in routes:
                scenario = await inspect_route(
                    cdp,
                    frontend_base_url=frontend_base_url,
                    route=route,
                    timeout_sec=args.page_timeout_sec,
                )
                interaction_checks: list[dict[str, Any]] = []
                if route == "/":
                    interaction_checks.append(
                        await exercise_history_interactions(
                            cdp,
                            backend=backend,
                            frontend_base_url=frontend_base_url,
                            timeout_sec=args.page_timeout_sec,
                        )
                    )
                elif route == "/youtube":
                    interaction_checks.append(
                        await exercise_youtube_history_interactions(cdp, timeout_sec=args.page_timeout_sec)
                    )
                    interaction_checks.append(
                        await exercise_youtube_interactions(cdp, timeout_sec=args.page_timeout_sec)
                    )
                    interaction_checks.append(
                        await exercise_youtube_start_transcription(
                            cdp,
                            frontend_base_url=frontend_base_url,
                            backend=backend,
                            timeout_sec=args.page_timeout_sec,
                        )
                    )
                elif route == "/file":
                    interaction_checks.append(
                        await exercise_file_history_interactions(cdp, timeout_sec=args.page_timeout_sec)
                    )
                    interaction_checks.append(
                        await exercise_file_upload_error_interaction(cdp, timeout_sec=args.page_timeout_sec)
                    )
                    interaction_checks.append(
                        await exercise_file_drop_interaction(cdp, timeout_sec=args.page_timeout_sec)
                    )
                elif route == "/debug":
                    interaction_checks.append(
                        await exercise_debug_console_interaction(
                            cdp,
                            backend=backend,
                            timeout_sec=args.page_timeout_sec,
                        )
                    )
                elif route == "/settings":
                    interaction_checks.append(
                        await exercise_settings_interactions(
                            cdp,
                            frontend_base_url=frontend_base_url,
                            backend=backend,
                            timeout_sec=args.page_timeout_sec,
                        )
                    )
                    interaction_checks.append(
                        await exercise_settings_desktop_controls(
                            cdp,
                            frontend_base_url=frontend_base_url,
                            backend=backend,
                            timeout_sec=args.page_timeout_sec,
                        )
                    )
                elif route == "/transcript/youtube-processing-smoke":
                    interaction_checks.append(
                        await exercise_transcript_processing_refresh(cdp, timeout_sec=args.page_timeout_sec)
                    )
                if interaction_checks:
                    scenario["interactionChecks"] = interaction_checks
                    scenario["ok"] = bool(scenario["ok"]) and all(item.get("ok") for item in interaction_checks)
                scenarios.append(scenario)

            command_palette_check = await exercise_command_palette(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
            )

            transcript_detail_actions_check = await exercise_transcript_detail_actions(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
            )

            transcript_cancel_check = await exercise_transcript_cancel_action(
                cdp,
                frontend_base_url=frontend_base_url,
                backend=backend,
                timeout_sec=args.page_timeout_sec,
            )

            if args.fast_tab_switch:
                output_path = Path(args.output).resolve()
                evidence_dir = (
                    Path(args.evidence_dir).resolve()
                    if args.evidence_dir
                    else output_path.with_suffix("")
                )
                fast_tab_switch_check = await exercise_fast_tab_switch(
                    cdp,
                    frontend_base_url=frontend_base_url,
                    timeout_sec=args.page_timeout_sec,
                    max_route_ms=args.fast_tab_max_ms,
                    evidence_dir=evidence_dir,
                )

            mobile_navigation_check = await exercise_mobile_navigation(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
            )

            mobile_route_layouts_check = await exercise_mobile_route_layouts(
                cdp,
                frontend_base_url=frontend_base_url,
                routes=routes,
                timeout_sec=args.page_timeout_sec,
            )

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

    ok = (
        bool(scenarios)
        and all(item["ok"] for item in scenarios)
        and bool(command_palette_check and command_palette_check.get("ok"))
        and bool(transcript_detail_actions_check and transcript_detail_actions_check.get("ok"))
        and bool(transcript_cancel_check and transcript_cancel_check.get("ok"))
        and (fast_tab_switch_check is None or bool(fast_tab_switch_check.get("ok")))
        and bool(mobile_navigation_check and mobile_navigation_check.get("ok"))
        and bool(mobile_route_layouts_check and mobile_route_layouts_check.get("ok"))
        and bool(token_required_check and token_required_check.get("ok"))
    )
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
    if command_palette_check:
        interaction_checks.append(command_palette_check)
    if transcript_detail_actions_check:
        interaction_checks.append(transcript_detail_actions_check)
    if transcript_cancel_check:
        interaction_checks.append(transcript_cancel_check)
    if fast_tab_switch_check:
        interaction_checks.append(fast_tab_switch_check)
    if mobile_navigation_check:
        interaction_checks.append(mobile_navigation_check)
    if mobile_route_layouts_check:
        interaction_checks.append(mobile_route_layouts_check)
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
        "commandPaletteCheck": command_palette_check,
        "transcriptDetailActionsCheck": transcript_detail_actions_check,
        "transcriptCancelCheck": transcript_cancel_check,
        "fastTabSwitchCheck": fast_tab_switch_check,
        "mobileNavigationCheck": mobile_navigation_check,
        "mobileRouteLayoutsCheck": mobile_route_layouts_check,
        "tokenRequiredCheck": token_required_check,
    }


async def run_browser_smoke_with_clean_shutdown(args: argparse.Namespace) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def handle_loop_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if sys.platform.startswith("win") and isinstance(exc, ConnectionResetError):
            # Chromium/Vite teardown can close the pipe before the proactor transport
            # finishes its connection-lost callback. The smoke result already captures
            # browser/page errors; keep this expected shutdown noise out of evidence.
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handle_loop_exception)
    try:
        return await run_browser_smoke(args)
    finally:
        loop.set_exception_handler(previous_handler)


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
    parser.add_argument("--fast-tab-switch", action="store_true")
    parser.add_argument("--fast-tab-max-ms", type=float, default=2000.0)
    parser.add_argument("--evidence-dir", default="")
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
    valid_routes = [route for route in args.routes if route in ROUTE_EXPECTATIONS]
    fast_tab_switch_check = (
        {
            "name": "fast-tab-switch",
            "ok": True,
            "maxRouteReadyMs": args.fast_tab_max_ms,
            "maxObservedRouteReadyMs": min(args.fast_tab_max_ms, 250.0),
            "routes": FAST_TAB_SWITCH_SEQUENCE,
            "transitions": [
                {
                    "route": route,
                    "ok": True,
                    "routeReadyMs": 100.0,
                    "blankSampleCount": 0,
                    "loadingSampleCount": 0,
                }
                for route in FAST_TAB_SWITCH_SEQUENCE
            ],
            "screenshot": "validate-only",
            "validateOnly": True,
        }
        if args.fast_tab_switch
        else None
    )
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
                {"name": "history-search-copy-navigation", "ok": True}
            ] if route == "/" else [
                {"name": "youtube-history-actions", "ok": True},
                {"name": "youtube-thumbnails", "ok": True},
                {"name": "youtube-start-transcription", "ok": True}
            ] if route == "/youtube" else [
                {"name": "file-history-actions", "ok": True},
                {"name": "file-upload-error", "ok": True},
                {"name": "file-drag-drop", "ok": True}
            ] if route == "/file" else [
                {"name": "debug-console-actions", "ok": True}
            ] if route == "/debug" else [
                {"name": "settings-persistence", "ok": True},
                {"name": "settings-desktop-controls", "ok": True}
            ] if route == "/settings" else [
                {"name": "transcript-processing-refresh", "ok": True}
            ] if route == "/transcript/youtube-processing-smoke" else [],
            "validateOnly": True,
        }
        for route in valid_routes
    ]
    mobile_route_layouts_check = {
        "name": "mobile-route-layouts",
        "ok": True,
        "routes": valid_routes,
        "routeCount": len(valid_routes),
        "maxOverflowX": 0,
        "results": [
            {
                "route": route,
                "ok": True,
                "viewportWidth": 390,
                "scrollWidth": 390,
                "overflowX": 0,
                "mainTextLength": 1,
                "visibleControlCount": 0,
                "tooSmallControls": [],
                "validateOnly": True,
            }
            for route in valid_routes
        ],
        "validateOnly": True,
    }
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
            "interactionCheckCount": (
                sum(len(item.get("interactionChecks", [])) for item in scenarios)
                + 6
                + (1 if fast_tab_switch_check else 0)
            ),
            "interactionChecks": [
                check["name"]
                for item in scenarios
                for check in item.get("interactionChecks", [])
            ] + [
                "command-palette",
                "transcript-detail-actions",
                "transcript-cancel-action",
                *(["fast-tab-switch"] if fast_tab_switch_check else []),
                "mobile-navigation",
                "mobile-route-layouts",
                "token-required-browser-state",
            ],
            "validateOnly": True,
        },
        "scenarios": scenarios,
        "commandPaletteCheck": {
            "name": "command-palette",
            "ok": True,
            "validateOnly": True,
        },
        "transcriptDetailActionsCheck": {
            "name": "transcript-detail-actions",
            "ok": True,
            "validateOnly": True,
        },
        "transcriptCancelCheck": {
            "name": "transcript-cancel-action",
            "ok": True,
            "validateOnly": True,
        },
        "fastTabSwitchCheck": fast_tab_switch_check,
        "mobileNavigationCheck": {
            "name": "mobile-navigation",
            "ok": True,
            "validateOnly": True,
        },
        "mobileRouteLayoutsCheck": mobile_route_layouts_check,
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
        result = asyncio.run(run_browser_smoke_with_clean_shutdown(args))
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
