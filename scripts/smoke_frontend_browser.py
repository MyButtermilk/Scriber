from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import wave
from contextlib import suppress
from datetime import datetime, timezone
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
    "/youtube": ["YouTube transcription", "Recent videos"],
    "/file": ["File transcription", "Recent files"],
    "/meetings": ["Meeting workspace", "Meetings", "Start meeting"],
    "/debug": [
        "Debug Console",
        "ui-debug-sample.log",
        "Debug console sample error",
        "Clear logs",
        "Auto scroll",
        "Newest first",
    ],
    "/settings": ["Settings", "Speech-to-text provider", "API keys"],
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

FAST_TAB_SWITCH_SEQUENCE = [
    "/youtube", "/file", "/meetings", "/settings", "/", "/youtube", "/meetings", "/file", "/"
]

PRIMARY_TAB_SHELLS = [
    ("/", "live-mic"),
    ("/meetings", "meetings"),
    ("/youtube", "youtube"),
    ("/file", "file"),
    ("/debug", "console"),
    ("/settings", "settings"),
]


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
        self.settings_get_count = 0
        self.runtime_logs_deleted = False
        self.support_bundle_count = 0
        self.file_uploads: list[dict[str, Any]] = []
        self.runtime_logs_count = 0
        self.youtube_search_requests: list[str] = []
        self.youtube_transcribe_requests: list[dict[str, Any]] = []
        self.autostart_enabled = False
        self.autostart_available = True
        self.autostart_requests: list[dict[str, Any]] = []
        self.deleted_transcript_ids: set[str] = set()
        self.processing_started_at = datetime.now(timezone.utc).isoformat()
        self.meeting: dict[str, Any] | None = None
        self.meeting_requests: list[str] = []
        self.meeting_exports: list[str] = []
        self.meeting_email_exports: list[str] = []
        self.meeting_deliveries: list[dict[str, Any]] = []
        self.meeting_start_payload: dict[str, Any] = {}
        self.meeting_imports: dict[str, dict[str, Any]] = {}
        self.diarization_component_installed = False
        self.speaker_profiles = [
            {"id": "profile-smoke-a", "displayName": "Speaker a1b2c3", "sampleCount": 4,
             "isNamed": False, "enrolled": False, "enrollmentSampleCount": 0,
             "enrolledAt": "", "createdAt": "2026-06-01T10:00:00Z", "updatedAt": "2026-06-01T10:00:00Z"},
            {"id": "profile-smoke-b", "displayName": "Grace Hopper", "sampleCount": 7,
             "isNamed": True, "enrolled": False, "enrollmentSampleCount": 0,
             "enrolledAt": "", "createdAt": "2026-06-01T10:00:00Z", "updatedAt": "2026-06-01T10:00:00Z"},
        ]
        self.outlook_connected = False
        self.outlook_synced = False
        self.meeting_detection_candidate: dict[str, Any] | None = {
            "detectionId": "detection-smoke-1", "label": "Zoom meeting detected",
            "source": "windowAndRenderSession", "detectedAt": datetime.now(timezone.utc).isoformat(),
            "calendarEvent": None,
        }

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
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
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
        app.router.add_get("/api/meetings", self.meetings)
        app.router.add_get("/api/meeting-imports", self.list_meeting_imports)
        app.router.add_post("/api/meeting-imports", self.create_meeting_import)
        app.router.add_get("/api/meeting-imports/{import_id}", self.get_meeting_import)
        app.router.add_put("/api/meeting-imports/{import_id}/content", self.upload_meeting_import)
        app.router.add_delete("/api/meeting-imports/{import_id}", self.cancel_meeting_import)
        app.router.add_post("/api/meetings/import", self.import_meeting)
        app.router.add_post("/api/meetings", self.start_meeting)
        app.router.add_get("/api/meetings/capabilities", self.meeting_capabilities)
        app.router.add_get("/api/meetings/audio-devices", self.meeting_audio_devices)
        app.router.add_post("/api/meetings/device-test", self.meeting_device_test)
        app.router.add_get("/api/meeting-profiles", self.meeting_profiles)
        app.router.add_get("/api/meetings/profiles", self.meeting_profiles)
        app.router.add_get("/api/meetings/speaker-profiles", self.meeting_speaker_profiles)
        app.router.add_post("/api/meetings/speaker-profiles/enroll", self.enroll_meeting_speaker_profile)
        app.router.add_patch("/api/meetings/speaker-profiles/{profile_id}", self.patch_meeting_speaker_profile)
        app.router.add_delete("/api/meetings/speaker-profiles/{profile_id}", self.delete_meeting_speaker_profile)
        app.router.add_get("/api/meetings/speaker-model", self.meeting_speaker_model)
        app.router.add_get("/api/meetings/diarization-component", self.diarization_component)
        app.router.add_post("/api/meetings/diarization-component", self.install_diarization_component)
        app.router.add_get("/api/meetings/detection", self.meeting_detection)
        app.router.add_post("/api/meetings/detection/dismiss", self.dismiss_meeting_detection)
        app.router.add_get("/api/meetings/{meeting_id}/audio", self.meeting_audio)
        app.router.add_get("/api/meetings/{meeting_id}/audio/{source}", self.meeting_audio)
        app.router.add_get("/api/meetings/{meeting_id}", self.meeting_detail)
        app.router.add_delete("/api/meetings/{meeting_id}", self.delete_meeting)
        app.router.add_post("/api/meetings/{meeting_id}/chat", self.meeting_chat)
        app.router.add_post("/api/meetings/{meeting_id}/deliveries/preview", self.meeting_delivery_preview)
        app.router.add_get("/api/meetings/{meeting_id}/deliveries", self.meeting_delivery_list)
        app.router.add_post("/api/meetings/{meeting_id}/deliveries", self.meeting_delivery)
        app.router.add_put("/api/meetings/{meeting_id}/notes", self.meeting_note)
        app.router.add_patch("/api/meetings/{meeting_id}/action-items/{item_id}", self.meeting_action_item)
        app.router.add_patch("/api/meetings/{meeting_id}/speakers/{speaker_id}", self.meeting_speaker)
        app.router.add_patch("/api/meetings/{meeting_id}/segments/{segment_id}", self.meeting_segment_edit)
        app.router.add_post("/api/meetings/{meeting_id}/segments/{segment_id}/undo", self.meeting_segment_undo)
        app.router.add_get("/api/meetings/{meeting_id}/export/{format}", self.meeting_export)
        app.router.add_get("/api/meetings/{meeting_id}/email-preview", self.meeting_email_preview)
        app.router.add_get("/api/meetings/{meeting_id}/export-email", self.meeting_export_email)
        app.router.add_post("/api/meetings/{meeting_id}/{action}", self.meeting_action)
        app.router.add_get("/api/calendar/outlook/status", self.outlook_status)
        app.router.add_post("/api/calendar/outlook/connect", self.outlook_connect)
        app.router.add_post("/api/calendar/outlook/sync", self.outlook_sync)
        app.router.add_delete("/api/calendar/outlook", self.outlook_disconnect)
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

    async def broadcast_event(self, payload: dict[str, Any]) -> None:
        stale: list[web.WebSocketResponse] = []
        for ws in tuple(self.websockets):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.websockets.discard(ws)

    async def broadcast_meeting_reconnect_cycle(self, meeting_id: str) -> None:
        await asyncio.sleep(0.25)
        base = {
            "apiVersion": "1",
            "type": "meeting_live_status",
            "meetingId": meeting_id,
            "source": "system",
            "reconnectCount": 1,
        }
        await self.broadcast_event({**base, "status": "reconnecting"})
        await asyncio.sleep(0.4)
        await self.broadcast_event({**base, "status": "recovered"})

    async def disconnect_websockets(self) -> set[web.WebSocketResponse]:
        disconnected = set(self.websockets)
        for ws in disconnected:
            with suppress(Exception):
                await ws.close(code=1012, message=b"frontend smoke reconnect")
        return disconnected

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
        self.settings_get_count += 1
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
        self.youtube_search_requests.append(request.query.get("q", ""))
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

    async def meetings(self, request: web.Request) -> web.Response:
        items = [self._meeting_summary()] if self.meeting else []
        active = self._meeting_summary() if self.meeting and self.meeting["state"] in {
            "starting", "recording", "paused", "stopping", "finalizing", "analyzing"
        } else None
        return web.json_response({
            "apiVersion": "1", "items": items, "total": len(items),
            "limit": 50, "offset": 0, "activeMeeting": active,
        })

    async def diarization_component(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "available": True, "enabled": True,
            "installed": self.diarization_component_installed,
            "engine": "sherpa-onnx", "version": "1.13.3",
            "segmentationModel": "pyannote-segmentation-3.0-int8",
            "embeddingModel": "3D-Speaker ERes2Net",
            "byteSize": 73_831_494 if self.diarization_component_installed else 0,
            "license": "Apache-2.0 runtime; model licenses are stored with the component",
        })

    async def install_diarization_component(self, request: web.Request) -> web.Response:
        self.diarization_component_installed = True
        return await self.diarization_component(request)

    async def import_meeting(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        filename = "Imported meeting"
        async for field in reader:
            if field.name == "file":
                filename = Path(field.filename or filename).stem
                while await field.read_chunk():
                    pass
                break
        self.meeting = self._synthetic_meeting(filename)
        self.meeting["state"] = "finalizing"
        self.meeting["captureMetadata"]["captureKind"] = "meeting-file-import"
        self.meeting_requests.append("import")
        return web.json_response(self._meeting_summary(), status=202)

    async def create_meeting_import(self, request: web.Request) -> web.Response:
        payload = await request.json()
        import_id = f"import-smoke-{len(self.meeting_imports) + 1}"
        job = {
            "apiVersion": "1", "id": import_id, "state": "created",
            "originalFilename": payload.get("filename", "Imported meeting.webm"),
            "title": payload.get("title", "Imported meeting"), "language": payload.get("language", "auto"),
            "profileId": payload.get("profileId", "soniox-balanced"),
            "expectedBytes": payload.get("byteSize"), "receivedBytes": 0,
            "progress": 0, "status": "Created", "meetingId": None,
            "cancelRequested": False, "errorCode": "", "errorMessage": "",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "uploadUrl": f"/api/meeting-imports/{import_id}/content",
        }
        self.meeting_imports[import_id] = job
        return web.json_response(job, status=201)

    async def list_meeting_imports(self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(50, int(request.query.get("limit", "24"))))
        except ValueError:
            return web.json_response({"message": "invalid limit"}, status=400)
        items = list(reversed(list(self.meeting_imports.values())))[:limit]
        return web.json_response({
            "apiVersion": "1", "items": items, "total": len(items), "limit": limit,
        })

    async def get_meeting_import(self, request: web.Request) -> web.Response:
        job = self.meeting_imports.get(request.match_info["import_id"])
        return web.json_response(job or {"message": "not found"}, status=200 if job else 404)

    async def upload_meeting_import(self, request: web.Request) -> web.Response:
        import_id = request.match_info["import_id"]
        job = self.meeting_imports[import_id]
        received = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            received += len(chunk)
        job.update({"state": "received", "status": "Upload safely stored", "progress": 0.86, "receivedBytes": received})
        return web.json_response(job, status=202)

    async def cancel_meeting_import(self, request: web.Request) -> web.Response:
        job = self.meeting_imports.get(request.match_info["import_id"])
        if job is None:
            return web.json_response({"message": "not found"}, status=404)
        job.update({"state": "canceled", "status": "Canceled", "cancelRequested": True})
        await self.broadcast_event({
            "apiVersion": "1", "type": "meeting_import_progress",
            "importId": job["id"], "phase": "canceled", "progress": 0,
            "status": "Meeting import canceled", "receivedBytes": job["receivedBytes"],
            "expectedBytes": job["expectedBytes"],
        })
        return web.json_response(job)

    async def meeting_capabilities(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "platform": "windows", "shellIpcAvailable": True,
            "nativeMeetingCapture": True, "liveMicBusy": False,
            "activeMeeting": None, "sources": ["microphone", "system"],
            "requiresPermissionConfirmation": False,
            "longSession": {
                "targetDurationSeconds": 18_000,
                "checkpointIntervalSeconds": 30,
                "requiredFreeBytes": 6 * 1024**3,
                "availableFreeBytes": 7 * 1024**3,
                "estimatedCaptureSeconds": 55_924,
                "storageReady": True,
            },
        })

    def _meeting_summary(self) -> dict[str, Any]:
        assert self.meeting is not None
        return {key: value for key, value in self.meeting.items() if key not in {
            "apiVersion", "segments", "speakers", "notes", "actionItems", "outputs",
            "outputVersions", "audioGaps", "audioAssets",
        }}

    def _synthetic_meeting(self, title: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "apiVersion": "1", "id": "meeting-smoke-1", "title": title or "Synthetic meeting",
            "state": "recording", "language": "auto", "liveProvider": "soniox",
            "finalProvider": "soniox_async", "analysisModel": "gemini-flash-latest",
            "aecEnabled": True, "voiceLibraryEnabled": False, "consentConfirmed": False,
            "origin": "captured",
            "transcriptEditVersion": 0,
            "startedAt": now, "endedAt": None, "createdAt": now, "updatedAt": now,
            "errorCode": "", "errorMessage": "", "captureMetadata": {
                "aecActive": True,
                "calendarEvent": {
                    "participants": [
                        {"name": "Morgan Example", "address": "morgan@example.com"},
                        {"name": "Riley Example", "address": "riley@example.com"},
                    ],
                },
                "aecMetrics": {
                    "measurement": "render-active-raw-to-clean-energy-ratio",
                    "renderActiveFrames": 1800,
                    "renderActiveDurationMs": 18000,
                    "echoReductionDb": 8.4,
                },
            },
            "audioRetentionDays": 0, "segments": [], "speakers": [], "notes": [],
            "actionItems": [], "outputs": [], "outputVersions": [], "audioGaps": [],
            "audioAssets": [],
        }

    async def start_meeting(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.meeting_start_payload = dict(payload)
        self.meeting = self._synthetic_meeting(str(payload.get("title", "")))
        self.meeting_requests.append("start")
        asyncio.create_task(self.broadcast_meeting_reconnect_cycle(self.meeting["id"]))
        return web.json_response(self._meeting_summary())

    async def meeting_detail(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        return web.json_response(self.meeting)

    async def meeting_audio(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        sample_rate = 16_000
        frames = bytearray()
        for index in range(sample_rate * 10):
            sample = int(32767 * 0.08 * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(frames)
        return web.Response(
            body=output.getvalue(), content_type="audio/wav",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"},
        )

    async def delete_meeting(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        meeting_id = self.meeting["id"]
        self.meeting = None
        self.meeting_requests.append("delete")
        return web.json_response({"apiVersion": "1", "success": True, "id": meeting_id})

    async def meeting_action(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        action = request.match_info["action"]
        self.meeting_requests.append(action)
        if action == "pause":
            self.meeting["state"] = "paused"
        elif action == "resume":
            self.meeting["state"] = "recording"
            self.meeting["errorCode"] = ""
            self.meeting["errorMessage"] = ""
        elif action == "stop":
            self.meeting["state"] = "ready"
            self.meeting["endedAt"] = datetime.now(timezone.utc).isoformat()
            self.meeting["audioAssets"] = [
                {
                    "id": f"asset-{kind}", "meetingId": self.meeting["id"],
                    "kind": kind, "relativePath": f"final/{kind}.opus", "codec": "opus",
                    "sampleRate": 16000, "channels": 1, "durationMs": 10000,
                    "byteSize": 32000, "sha256": "a" * 64,
                    "trackManifestVersion": 2,
                    "trackManifest": [{
                        "source": source, "codec": "opus", "sampleRate": 16000,
                        "channels": 1, "sampleCount": 160000, "durationMs": 10000,
                        "timelineOriginMs": 0, "pcmSha256": "b" * 64,
                        "equalityVerified": True,
                    }],
                    "equalityVerified": True, "createdAt": datetime.now(timezone.utc).isoformat(),
                }
                for kind, source in (
                    ("playback_mix", "mixed"),
                    ("playback_microphone", "microphone"),
                    ("playback_system", "system"),
                )
            ]
            self.meeting["segments"] = [
                {
                    "id": "seg-smoke-001", "meetingId": self.meeting["id"],
                    "revision": "canonical", "source": "microphone", "speakerId": "speaker-smoke-1",
                    "speakerLabel": "Alex", "startMs": 0, "endMs": 4200,
                    "durationMs": 4200,
                    "text": "We decided to launch the meeting workspace on Friday.",
                    "confidence": 0.98, "isFinal": True, "sequence": 1,
                    "editVersion": 0, "editedAt": None,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "id": "seg-smoke-002", "meetingId": self.meeting["id"],
                    "revision": "canonical", "source": "system", "speakerId": "speaker-smoke-2",
                    "speakerLabel": "Morgan", "startMs": 5000, "endMs": 8200,
                    "durationMs": 3200,
                    "text": "Customer approval remains open before release.",
                    "confidence": 0.96, "isFinal": True, "sequence": 2,
                    "editVersion": 0, "editedAt": None,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                },
            ]
            self.meeting["speakers"] = [{
                "id": "speaker-smoke-1", "meetingId": self.meeting["id"], "label": "Speaker 1",
                "displayName": "Alex", "sourceHint": "microphone", "profileId": None,
                "confidence": 0.98, "createdAt": self.meeting["createdAt"],
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }, {
                "id": "speaker-smoke-2", "meetingId": self.meeting["id"], "label": "Speaker 2",
                "displayName": "Morgan", "sourceHint": "system", "profileId": None,
                "confidence": 0.96, "createdAt": self.meeting["createdAt"],
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }]
        elif action == "analyze":
            analysis = {
                "executiveSummary": "The team approved a Friday launch.",
                "topics": [{"text": "Launch readiness", "segmentIds": ["seg-smoke-001"]}],
                "decisions": [{"text": "Launch on Friday", "segmentIds": ["seg-smoke-001"]}],
                "openQuestions": [{"text": "Who owns release monitoring?", "segmentIds": ["seg-smoke-001"]}],
            }
            output = {
                "id": "output-smoke-1", "kind": "analysis", "schemaVersion": "MeetingAnalysisV1",
                "version": 1, "supersedesId": None, "transcriptRevision": "canonical",
                "transcriptEditVersion": self.meeting["transcriptEditVersion"],
                "provider": "synthetic", "status": "completed", "payload": analysis,
                "errorMessage": "", "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            self.meeting["outputs"] = [output]
            self.meeting["outputVersions"] = []
            self.meeting["actionItems"] = [{
                "id": "action-smoke-1", "meetingId": self.meeting["id"],
                "text": "Prepare the release checklist", "owner": "Alex", "dueDate": None,
                "status": "open", "segmentIds": ["seg-smoke-001"], "userModified": False,
                "createdAt": self.meeting["createdAt"], "updatedAt": datetime.now(timezone.utc).isoformat(),
            }]
        else:
            return web.json_response({"message": "Unsupported meeting action"}, status=400)
        self.meeting["updatedAt"] = datetime.now(timezone.utc).isoformat()
        return web.json_response(self._meeting_summary())

    async def meeting_chat(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.meeting_requests.append("chat")
        return web.json_response({
            "apiVersion": "1", "message": {
                "content": f"Friday was selected for launch. Question: {payload.get('question', '')}",
                "citations": ["seg-smoke-001"],
            },
        })

    async def meeting_export(self, request: web.Request) -> web.Response:
        export_format = request.match_info["format"]
        self.meeting_exports.append(export_format)
        return web.Response(
            text="# Synthetic meeting export\n", content_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="meeting-smoke.{export_format}"'},
        )

    async def meeting_email_preview(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        return web.json_response({
            "apiVersion": "1",
            "recipients": [
                {"name": "Morgan Example", "address": "morgan@example.com"},
                {"name": "Riley Example", "address": "riley@example.com"},
            ],
            "subject": "Meeting follow-up: Browser smoke product sync",
            "body": (
                "Hello,\n\nHere is the follow-up for Browser smoke product sync.\n\n"
                "Summary\nThe team approved a Friday launch.\n\n"
                "Action items\n- Prepare the release checklist [Alex]\n\nBest regards"
            ),
        })

    async def meeting_export_email(self, request: web.Request) -> web.Response:
        if not self.meeting or request.match_info["meeting_id"] != self.meeting["id"]:
            return web.json_response({"message": "Meeting not found"}, status=404)
        attachment = request.query.get("attachment", "")
        self.meeting_email_exports.append(attachment or "body")
        body = (
            "Subject: Meeting follow-up: Browser smoke product sync\r\n"
            "To: Morgan Example <morgan@example.com>, Riley Example <riley@example.com>\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "Synthetic Outlook-compatible meeting draft."
        ).encode("utf-8")
        return web.Response(
            body=body,
            content_type="message/rfc822",
            headers={"Content-Disposition": 'attachment; filename="Browser smoke product sync - email draft.eml"'},
        )

    async def meeting_delivery_preview(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if payload.get("url") != "https://automation.example/meeting":
            return web.json_response({"message": "Unexpected smoke webhook URL"}, status=400)
        return web.json_response({
            "apiVersion": "1", "target": payload["url"], "previewHash": "preview-smoke-hash",
            "byteSize": 512, "payload": {
                "event": "meeting.ready", "meeting": {"title": "Browser smoke product sync"},
                "segments": [{"id": "seg-smoke-001"}], "notes": [],
            },
        })

    async def meeting_delivery(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if payload.get("confirmed") is not True or payload.get("previewHash") != "preview-smoke-hash":
            return web.json_response({"message": "Preview confirmation required"}, status=409)
        delivery = {
            "id": "delivery-smoke-1", "target": "https://automation.example/meeting",
            "status": "delivered", "attemptCount": 1,
        }
        self.meeting_deliveries.append(delivery)
        self.meeting_requests.append("webhook")
        return web.json_response({"apiVersion": "1", "delivery": delivery}, status=201)

    async def meeting_delivery_list(self, request: web.Request) -> web.Response:
        return web.json_response({"apiVersion": "1", "items": self.meeting_deliveries})

    async def meeting_note(self, request: web.Request) -> web.Response:
        payload = await request.json()
        note = {
            "id": str(payload.get("id") or "workspace"), "meetingId": request.match_info["meeting_id"],
            "body": str(payload.get("body") or ""), "atMs": None,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        assert self.meeting is not None
        self.meeting["notes"] = [note]
        self.meeting_requests.append("note")
        return web.json_response({"apiVersion": "1", **note})

    async def meeting_action_item(self, request: web.Request) -> web.Response:
        payload = await request.json()
        assert self.meeting is not None
        item = next(value for value in self.meeting["actionItems"] if value["id"] == request.match_info["item_id"])
        item.update(payload)
        item["userModified"] = True
        item["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self.meeting_requests.append("action-item")
        return web.json_response({"apiVersion": "1", **item})

    async def meeting_speaker(self, request: web.Request) -> web.Response:
        payload = await request.json()
        assert self.meeting is not None
        speaker = next(value for value in self.meeting["speakers"] if value["id"] == request.match_info["speaker_id"])
        speaker["displayName"] = str(payload.get("displayName") or speaker["displayName"])
        self.meeting_requests.append("speaker")
        return web.json_response({"apiVersion": "1", "success": True})

    async def meeting_segment_edit(self, request: web.Request) -> web.Response:
        payload = await request.json()
        assert self.meeting is not None
        expected_version = int(payload.get("expectedEditVersion", -1))
        if expected_version != self.meeting["transcriptEditVersion"]:
            return web.json_response({"message": "Transcript changed since it was loaded"}, status=409)
        segment = next(value for value in self.meeting["segments"] if value["id"] == request.match_info["segment_id"])
        previous_text = str(segment["text"])
        segment["text"] = str(payload.get("text") or "").strip()
        segment["editVersion"] = int(segment.get("editVersion", 0)) + 1
        segment["editedAt"] = datetime.now(timezone.utc).isoformat()
        self.meeting["transcriptEditVersion"] += 1
        segment["smokePreviousText"] = previous_text
        self.meeting_requests.append("segment-edit")
        return web.json_response({
            "apiVersion": "1", "meetingId": self.meeting["id"],
            "segment": {key: value for key, value in segment.items() if key != "smokePreviousText"},
            "transcriptEditVersion": self.meeting["transcriptEditVersion"],
            "outputsStale": True,
        })

    async def meeting_segment_undo(self, request: web.Request) -> web.Response:
        payload = await request.json()
        assert self.meeting is not None
        expected_version = int(payload.get("expectedEditVersion", -1))
        if expected_version != self.meeting["transcriptEditVersion"]:
            return web.json_response({"message": "Transcript changed since it was loaded"}, status=409)
        segment = next(value for value in self.meeting["segments"] if value["id"] == request.match_info["segment_id"])
        segment["text"] = str(segment.pop("smokePreviousText", segment["text"]))
        segment["editVersion"] = int(segment.get("editVersion", 0)) + 1
        segment["editedAt"] = datetime.now(timezone.utc).isoformat()
        self.meeting["transcriptEditVersion"] += 1
        self.meeting_requests.append("segment-undo")
        return web.json_response({
            "apiVersion": "1", "meetingId": self.meeting["id"], "segment": segment,
            "transcriptEditVersion": self.meeting["transcriptEditVersion"],
            "outputsStale": True,
        })

    async def meeting_profiles(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "defaultProfileId": "soniox-balanced",
            "profiles": [{
                "id": "soniox-balanced", "name": "Soniox live + final",
                "description": "Live captions during the meeting, followed by a second transcription of the complete saved audio.",
                "transcriptionMode": "live_final",
                "liveProvider": "soniox", "finalProvider": "soniox_async",
                "livePreviewAvailable": True, "livePreviewWarning": "",
                "analysisModel": "gemini-flash-latest", "language": "auto",
                "stages": [
                    {"id": "live", "label": "During the meeting", "provider": "Soniox Realtime", "model": "stt-rt-v5", "purpose": "Immediate captions for microphone and system audio."},
                    {"id": "final", "label": "After stopping", "provider": "Soniox Async", "model": "stt-async-v5", "purpose": "Retranscribes the complete durable audio with speaker diarization."},
                    {"id": "analysis", "label": "Summary and actions", "provider": "Gemini", "model": "gemini-flash-latest", "purpose": "Creates the cited summary, decisions, questions, and action items."},
                ],
                "aecEnabled": True, "voiceLibraryEnabled": False,
                "smartTurnEnabled": True, "autoAnalyze": True,
                "audioRetentionDays": 0, "available": True,
                "costEstimate": {
                    "currency": "USD", "pricingUpdatedAt": "2026-07-13",
                    "audioTrackAssumption": 2, "livePreviewPerMeetingHour": 0.24,
                    "livePerMeetingHour": 0.24, "finalPerMeetingHour": 0.20,
                    "singleTrackFinalPerAudioHour": 0.10,
                    "totalPerMeetingHour": 0.44,
                    "estimateKind": "published-list-price",
                    "sources": [{"label": "Soniox pricing", "url": "https://soniox.com/pricing"}],
                    "assumption": "Two captured audio tracks with live preview and a final pass.",
                },
                "fiveHourSupported": True,
                "fiveHourReason": "Bounded WebM/Opus upload derivative.",
                "maxDurationSeconds": 18_000,
                "unavailableReason": "",
            }],
            "providerCapabilities": {
                "soniox": {"live": True, "timestamps": True, "liveDiarization": True,
                    "batchDiarization": False, "local": False, "maxDurationSeconds": 18_000,
                    "structuredTokens": True, "fiveHourSupported": True,
                    "fiveHourReason": "Bounded WebM/Opus upload derivative."},
                "soniox_async": {"live": False, "timestamps": True, "liveDiarization": False,
                    "batchDiarization": True, "local": False, "maxDurationSeconds": 18_000,
                    "structuredTokens": True, "fiveHourSupported": True,
                    "fiveHourReason": "Bounded WebM/Opus upload derivative."},
            },
        })

    async def meeting_audio_devices(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "available": True, "reason": "",
            "source": "rust-wasapi", "partial": False,
            "capture": [
                {"endpointIdHash": "a" * 32, "friendlyName": "USB Smoke Microphone", "isDefault": True, "defaultRoles": ["console"]},
                {"endpointIdHash": "c" * 32, "friendlyName": "Conference Camera Microphone", "isDefault": False, "defaultRoles": []},
                {"endpointIdHash": "d" * 32, "friendlyName": "Laptop Microphone Array", "isDefault": False, "defaultRoles": []},
            ],
            "render": [{"endpointIdHash": "b" * 32, "friendlyName": "Smoke Speakers", "isDefault": True, "defaultRoles": ["console"]}],
        })

    async def meeting_device_test(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if payload.get("microphoneNativeEndpointIdHash") != "a" * 32 or payload.get("renderNativeEndpointIdHash") != "b" * 32:
            return web.json_response({"message": "Explicit synthetic routes required"}, status=400)
        self.meeting_requests.append("device-test")
        source = lambda rms, peak: {
            "frames": 150, "audioFrames": 24_000, "rms": rms, "peak": peak,
            "active": True, "errorCode": "",
        }
        return web.json_response({
            "apiVersion": "1", "available": True, "durationMs": 3_000,
            "aecActive": True, "testTonePlayed": True,
            "sources": {
                "microphone": source(0.18, 0.52),
                "system": source(0.24, 0.68),
                "mic_clean": source(0.12, 0.44),
            },
            "audioPersisted": False, "audioSentToProvider": False,
        })

    async def meeting_speaker_profiles(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "enabled": True, "items": self.speaker_profiles,
            "message": "Voice Library is local and opt-in; embeddings are excluded from this response.",
        })

    async def patch_meeting_speaker_profile(self, request: web.Request) -> web.Response:
        payload = await request.json()
        profile = next(
            (item for item in self.speaker_profiles if item["id"] == request.match_info["profile_id"]),
            None,
        )
        if profile is None:
            return web.json_response({"message": "Speaker profile not found"}, status=404)
        profile["displayName"] = str(payload.get("displayName") or profile["displayName"])
        profile["isNamed"] = True
        profile["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self.meeting_requests.append("speaker-profile-rename")
        return web.json_response({"apiVersion": "1", **profile})

    async def enroll_meeting_speaker_profile(self, request: web.Request) -> web.Response:
        payload = await request.json()
        display_name = " ".join(str(payload.get("displayName") or "").split())
        if not display_name:
            return web.json_response({"message": "Enter the speaker's name first."}, status=400)
        microphone_hash = str(payload.get("microphoneNativeEndpointIdHash") or "")
        if microphone_hash not in {"", "a" * 32}:
            return web.json_response({"message": "Choose a valid microphone."}, status=400)
        now = datetime.now(timezone.utc).isoformat()
        profile = {
            "id": f"profile-smoke-enrolled-{len(self.speaker_profiles) + 1}",
            "displayName": display_name,
            "sampleCount": 1,
            "isNamed": True,
            "enrolled": True,
            "enrollmentSampleCount": 1,
            "enrolledAt": now,
            "createdAt": now,
            "updatedAt": now,
        }
        self.speaker_profiles.append(profile)
        self.meeting_requests.append("speaker-profile-enroll")
        return web.json_response({
            "apiVersion": "1",
            "profile": profile,
            "capture": {"durationMs": 8_000, "rms": 0.12, "peak": 0.48, "quality": 0.88},
            "audioPersisted": False,
            "audioSentToProvider": False,
        }, status=201)

    async def delete_meeting_speaker_profile(self, request: web.Request) -> web.Response:
        profile_id = request.match_info["profile_id"]
        previous = len(self.speaker_profiles)
        self.speaker_profiles = [item for item in self.speaker_profiles if item["id"] != profile_id]
        if len(self.speaker_profiles) == previous:
            return web.json_response({"message": "Speaker profile not found"}, status=404)
        self.meeting_requests.append("speaker-profile-delete")
        return web.json_response({"apiVersion": "1", "success": True})

    async def meeting_speaker_model(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "optedIn": True, "installed": True,
            "model": "wespeaker-voxceleb-resnet34-LM", "revision": "smoke",
            "byteSize": 26632299, "expectedByteSize": 26632299, "sha256": "c" * 64,
            "license": "optional local model",
        })

    async def meeting_detection(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "available": True,
            "detection": self.meeting_detection_candidate,
        })

    async def dismiss_meeting_detection(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if not self.meeting_detection_candidate or payload.get("detectionId") != self.meeting_detection_candidate["detectionId"]:
            return web.json_response({"message": "Meeting detection not found"}, status=404)
        self.meeting_detection_candidate = None
        self.meeting_requests.append("dismiss-detection")
        return web.json_response({"apiVersion": "1", "dismissed": True})

    async def outlook_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "apiVersion": "1", "configured": True, "connected": self.outlook_connected,
            "scopes": ["User.Read", "Calendars.Read", "offline_access"],
            "lastSyncAt": "2026-06-01T11:45:00Z" if self.outlook_synced else "",
            "lastError": "",
            "nextEvent": ({
                "id": "outlook-smoke-event", "subject": "Architecture review",
                "start_at": "2026-06-02T09:00:00Z", "end_at": "2026-06-02T10:00:00Z",
                "join_url": "https://teams.microsoft.com/l/meetup-join/smoke",
                "organizer": {"name": "Ada Lovelace", "address": "ada@example.com"},
                "participants": [{"name": "Grace Hopper", "address": "grace@example.com"}],
            } if self.outlook_synced else None),
        })

    async def outlook_connect(self, request: web.Request) -> web.Response:
        self.outlook_connected = True
        self.meeting_requests.append("outlook-connect")
        return web.json_response({
            "apiVersion": "1", "authorizationUrl": "https://login.microsoftonline.com/smoke",
            "expiresIn": 600, "redirectUri": f"{self.base_url}/api/calendar/outlook/callback",
        })

    async def outlook_sync(self, request: web.Request) -> web.Response:
        if not self.outlook_connected:
            return web.json_response({"message": "Outlook is not connected"}, status=409)
        self.outlook_synced = True
        self.meeting_requests.append("outlook-sync")
        return web.json_response({"apiVersion": "1", "changed": 1})

    async def outlook_disconnect(self, request: web.Request) -> web.Response:
        self.outlook_connected = False
        self.outlook_synced = False
        self.meeting_requests.append("outlook-disconnect")
        return web.json_response({"apiVersion": "1", "disconnected": True})

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
        count = self.transcript_detail_counts.get(transcript_id, 0) + 1
        self.transcript_detail_counts[transcript_id] = count
        if transcript_id == "youtube-processing-smoke":
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
                        "processingStartedAt": self.processing_started_at,
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
                    "processingStartedAt": self.processing_started_at,
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
                    "processingStartedAt": self.processing_started_at,
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
                    "processingStartedAt": self.processing_started_at,
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
        except BaseException as exc:
            if args.evidence_dir:
                failure_path = Path(args.evidence_dir).resolve() / "browser-smoke-error.json"
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                failure_path.write_text(
                    json.dumps({
                        "errorType": exc.__class__.__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            raise
        finally:
            self.websockets.discard(ws)
        return ws

    @staticmethod
    def _default_settings() -> dict[str, Any]:
        return {
            "hotkey": "Ctrl + Shift + D",
            "hotkeyRaw": "ctrl+shift+d",
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
            "postProcessingEnabled": True,
            "postProcessingHotkey": "Ctrl + Shift + F",
            "postProcessingHotkeyRaw": "ctrl+shift+f",
            "meetingHotkey": "Ctrl + Shift + M",
            "meetingHotkeyRaw": "ctrl+shift+m",
            "meetingFinalProvider": "soniox_async",
            "meetingAnalysisModel": "gemini-flash-latest",
            "meetingSmartTurnEnabled": True,
            "meetingAutoAnalyze": True,
            "meetingAecEnabled": True,
            "meetingAudioRetentionDays": 0,
            "voiceprintLibraryOptIn": True,
            "visualizerBarCount": 45,
            "micAlwaysOn": False,
            "onnxModel": "",
            "apiKeys": {
                "soniox": "smoke-initial-soniox-key",
                "mistral": "smoke-initial-mistral-key",
                "assemblyai": "smoke-initial-assemblyai-key",
                "deepgram": "smoke-initial-deepgram-key",
                "googleApiKey": "smoke-initial-gemini-key",
                "openrouter": "smoke-initial-openrouter-key",
            },
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
    const stack = event.error && typeof event.error.stack === "string"
      ? event.error.stack
      : "";
    window.__scriberSmoke.pageErrors.push(
      stack || String(event.message || event.error || ""),
    );
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
  const normalizedText = text.toLocaleLowerCase();
  const smoke = window.__scriberSmoke || {{}};
  const missing = expected.filter((item) => !normalizedText.includes(String(item).toLocaleLowerCase()));
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


async def click_visible_target(
    cdp: CdpClient,
    *,
    label: str,
    selector: str,
    timeout_sec: float,
    text: str | None = None,
    exact_text: bool = True,
    prefer_last: bool = False,
) -> dict[str, Any]:
    """Click a rendered, enabled target through CDP after proving it is hit-testable."""
    probe_expression = f"""
(() => {{
  const selector = {json.dumps(selector)};
  const expectedText = {json.dumps(text)};
  const exactText = {json.dumps(exact_text)};
  const preferLast = {json.dumps(prefer_last)};
  const candidates = Array.from(document.querySelectorAll(selector)).filter((node) => {{
    const nodeText = (node.textContent || '').trim();
    const matchesText = expectedText === null
      || (exactText ? nodeText === expectedText : nodeText.includes(expectedText));
    const style = window.getComputedStyle(node);
    const rendered = node.getClientRects().length > 0
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && style.visibility !== 'collapse'
      && style.opacity !== '0';
    const enabled = !node.disabled
      && node.getAttribute('aria-disabled') !== 'true'
      && !node.hasAttribute('data-disabled');
    return matchesText && rendered && enabled;
  }});
  const node = preferLast ? candidates.at(-1) : candidates.at(0);
  if (!node) {{
    return {{
      ok: false,
      reason: 'missing rendered and enabled target',
      selector,
      expectedText,
      candidates: Array.from(document.querySelectorAll(selector))
        .map((candidate) => (candidate.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 30),
    }};
  }}
  node.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'instant' }});
  const rect = node.getBoundingClientRect();
  const left = Math.max(0, rect.left);
  const right = Math.min(window.innerWidth, rect.right);
  const top = Math.max(0, rect.top);
  const bottom = Math.min(window.innerHeight, rect.bottom);
  if (right <= left || bottom <= top) {{
    return {{
      ok: false,
      reason: 'target has no visible viewport area',
      selector,
      expectedText,
      rect: {{ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }},
      viewport: {{ width: window.innerWidth, height: window.innerHeight }},
    }};
  }}
  const x = left + (right - left) / 2;
  const y = top + (bottom - top) / 2;
  const hit = document.elementFromPoint(x, y);
  const hitMatches = !!hit && (hit === node || node.contains(hit));
  return {{
    ok: hitMatches,
    reason: hitMatches ? '' : 'target center is covered by another element',
    selector,
    expectedText,
    x,
    y,
    targetTag: node.tagName,
    targetText: (node.textContent || '').trim().slice(0, 160),
    hitTag: hit?.tagName || '',
    hitText: (hit?.textContent || '').trim().slice(0, 160),
  }};
}})()
"""
    target = await wait_for_interaction_state(
        cdp,
        label=label,
        timeout_sec=timeout_sec,
        expression=probe_expression,
    )
    # Radix dialogs and menus animate their transform on entry. A target can be
    # hit-testable for one frame and still move before the injected pointer
    # reaches it. Require a stable center just like Playwright's actionability
    # check so the smoke represents a real user click rather than a race.
    previous = target
    for _attempt in range(6):
        await asyncio.sleep(0.05)
        current = await wait_for_interaction_state(
            cdp,
            label=f"{label}-stable",
            timeout_sec=timeout_sec,
            expression=probe_expression,
        )
        if (
            abs(float(current["x"]) - float(previous["x"])) <= 0.5
            and abs(float(current["y"]) - float(previous["y"])) <= 0.5
        ):
            target = current
            break
        previous = current
        target = current
    await click_page_coordinates(cdp, x=float(target["x"]), y=float(target["y"]))
    return target


async def click_visible_button(
    cdp: CdpClient,
    *,
    label: str,
    timeout_sec: float,
    selector: str = "button",
    exact_text: bool = True,
    prefer_last: bool = True,
) -> dict[str, Any]:
    return await click_visible_target(
        cdp,
        label=f"button-{label}",
        selector=selector,
        timeout_sec=timeout_sec,
        text=label,
        exact_text=exact_text,
        prefer_last=prefer_last,
    )


async def set_file_input_files(
    cdp: CdpClient,
    *,
    label: str,
    selector: str,
    files: list[Path],
    timeout_sec: float,
) -> None:
    """Use Chromium's native file-input path instead of an untrusted synthetic change event."""
    await wait_for_interaction_state(
        cdp,
        label=label,
        timeout_sec=timeout_sec,
        expression=f"(() => ({{ ok: !!document.querySelector({json.dumps(selector)}) }}))()",
    )
    remote = await cdp.call(
        "Runtime.evaluate",
        {
            "expression": f"document.querySelector({json.dumps(selector)})",
            "returnByValue": False,
        },
        timeout=5,
    )
    object_id = str(remote.get("result", {}).get("objectId") or "")
    if not object_id:
        raise RuntimeError(f"Could not resolve file input '{label}' through CDP DOM")
    try:
        await cdp.call(
            "DOM.setFileInputFiles",
            {"files": [str(path.resolve()) for path in files], "objectId": object_id},
            timeout=5,
        )
    finally:
        await cdp.call("Runtime.releaseObject", {"objectId": object_id}, timeout=5)


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
    await cdp.evaluate(
        r"""
(async () => {
  window.scrollTo({ left: 0, top: 0, behavior: 'instant' });
  document.documentElement.scrollLeft = 0;
  if (document.body) document.body.scrollLeft = 0;
  const appScroller = document.querySelector('[data-app-scroll-container="true"]');
  let ancestor = appScroller?.parentElement || null;
  while (ancestor) {
    ancestor.scrollLeft = 0;
    ancestor.scrollTop = 0;
    ancestor = ancestor.parentElement;
  }
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return { scrollX: window.scrollX, scrollY: window.scrollY };
})()
""",
        timeout=5,
    )
    result = await cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=10)
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise RuntimeError("CDP Page.captureScreenshot did not return image data.")
    path.write_bytes(base64.b64decode(data))
    return evidence_path_for_report(path)


async def exercise_dark_boot_shell(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
    screenshot_dir: Path | None,
) -> dict[str, Any]:
    await cdp.call("Network.enable", timeout=5)
    await cdp.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": 'window.localStorage.setItem("scriber-theme", "dark");'},
        timeout=5,
    )
    await cdp.call("Network.setBlockedURLs", {"urls": ["*/src/main.tsx*"]}, timeout=5)
    try:
        await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/"}, timeout=10)
        state = await wait_for_interaction_state(
            cdp,
            label="dark-boot-shell",
            timeout_sec=timeout_sec,
            expression=r"""
(() => {
  if (!document.body) return { ok: false, reason: 'body-not-ready' };
  const shell = document.querySelector('.boot-shell');
  const darkLogo = document.querySelector('.boot-logo-dark');
  const lightLogo = document.querySelector('.boot-logo-light');
  const darkLogoStyle = darkLogo ? getComputedStyle(darkLogo) : null;
  const lightLogoStyle = lightLogo ? getComputedStyle(lightLogo) : null;
  const background = getComputedStyle(document.body).backgroundColor;
  const foreground = shell ? getComputedStyle(shell).color : '';
  const darkClass = document.documentElement.classList.contains('dark');
  return {
    ok: !!shell && darkClass && darkLogoStyle?.display !== 'none' && lightLogoStyle?.display === 'none'
      && darkLogo?.complete && Number(darkLogo?.naturalWidth || 0) > 0,
    darkClass,
    darkLogoVisible: darkLogoStyle?.display !== 'none',
    lightLogoHidden: lightLogoStyle?.display === 'none',
    darkLogoLoaded: !!darkLogo?.complete && Number(darkLogo?.naturalWidth || 0) > 0,
    background,
    foreground,
  };
})()
""",
        )
        screenshot = None
        if screenshot_dir is not None:
            screenshot = await capture_page_screenshot(
                cdp, output_dir=screenshot_dir, label="dark-boot-shell"
            )
        return {"name": "dark-boot-shell", "ok": True, "state": state, "screenshot": screenshot}
    finally:
        await cdp.call("Network.setBlockedURLs", {"urls": []}, timeout=5)
        await cdp.call("Page.navigate", {"url": "about:blank"}, timeout=5)


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
  const normalizedBodyText = bodyText.toLocaleLowerCase();
  const missing = expected.filter((item) => !normalizedBodyText.includes(String(item).toLocaleLowerCase()));
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


async def exercise_meeting_end_to_end(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    screenshots: list[str] = []
    import_fixture_dir = tempfile.TemporaryDirectory(prefix="scriber-meeting-browser-smoke-")
    customer_import_path = Path(import_fixture_dir.name) / "Customer interview.webm"
    durable_import_path = Path(import_fixture_dir.name) / "Durable interview.webm"
    customer_import_path.write_bytes(bytes(4096))
    durable_import_path.write_bytes(bytes(8192))
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/meetings"}, timeout=10)
    await wait_for_route_ready(
        cdp, route="/meetings", expected_text=ROUTE_EXPECTATIONS["/meetings"],
        expect_history_virtualized=False, timeout_sec=timeout_sec,
    )
    await set_file_input_files(
        cdp,
        label="meeting-import-input",
        selector='input[aria-label="Import meeting recording"]',
        files=[customer_import_path],
        timeout_sec=timeout_sec,
    )
    import_dialog = await wait_for_interaction_state(
        cdp, label="meeting-import-dialog", timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const dialog = document.querySelector('[role="dialog"]');
  return {
    ok: !!dialog && text.includes('Import a meeting recording')
      && text.includes('Customer interview.webm')
      && text.includes('Final transcript setting')
      && text.includes('Speaker names')
      && text.includes('Included'),
    hasDialog: !!dialog,
    text: dialog?.innerText.slice(0, 900) || '',
  };
})()
""",
    )
    import_cancelled = await click_visible_button(
        cdp,
        label="Cancel",
        selector='[role="dialog"] button',
        timeout_sec=timeout_sec,
        prefer_last=False,
    )
    await wait_for_interaction_state(
        cdp, label="meeting-import-dialog-closed", timeout_sec=timeout_sec,
        expression="(() => ({ ok: !document.body.innerText.includes('Import a meeting recording') }))()",
    )
    await set_file_input_files(
        cdp,
        label="durable-meeting-import-input",
        selector='input[aria-label="Import meeting recording"]',
        files=[durable_import_path],
        timeout_sec=timeout_sec,
    )
    await wait_for_interaction_state(
        cdp, label="durable-import-dialog-ready", timeout_sec=timeout_sec,
        expression="(() => ({ ok: document.body.innerText.includes('Durable interview.webm') }))()",
    )
    upload_clicked = await click_visible_button(
        cdp,
        label="Import recording",
        selector='[role="dialog"] button',
        timeout_sec=timeout_sec,
        prefer_last=False,
    )
    durable_import_state = await wait_for_interaction_state(
        cdp, label="durable-import-upload-committed", timeout_sec=timeout_sec,
        expression="(() => ({ ok: document.body.innerText.includes('Upload safely stored'), text: document.querySelector('[role=dialog]')?.innerText.slice(0, 900) || '' }))()",
    )
    cancel_upload = await click_visible_button(
        cdp,
        label="Cancel import",
        selector='[role="dialog"] button',
        timeout_sec=timeout_sec,
        prefer_last=False,
    )
    await wait_for_interaction_state(
        cdp, label="durable-import-canceled", timeout_sec=timeout_sec,
        expression="(() => ({ ok: document.body.innerText.includes('Meeting import canceled') || document.body.innerText.includes('Cancel') }))()",
    )
    await wait_for_interaction_state(
        cdp,
        label="durable-import-dialog-closed",
        timeout_sec=timeout_sec,
        expression="(() => ({ ok: !document.body.innerText.includes('Import a meeting recording') }))()",
    )
    import_fixture_dir.cleanup()
    prepared = await cdp.evaluate(
        r"""
(() => {
  const title = document.querySelector('#meeting-title');
  const microphone = document.querySelector('#meeting-microphone');
  const render = document.querySelector('#meeting-render');
  if (!title || !microphone || !render) return { ok: false, reason: 'missing start controls' };
  if (microphone.options.length !== 4) {
    return { ok: false, reason: `expected Windows default plus 3 microphones, got ${microphone.options.length}` };
  }
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  valueSetter.call(title, 'Browser smoke product sync');
  title.dispatchEvent(new Event('input', { bubbles: true }));
  const selectSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
  selectSetter.call(microphone, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  microphone.dispatchEvent(new Event('change', { bubbles: true }));
  selectSetter.call(render, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
  render.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not prepared or not prepared.get("ok"):
        raise RuntimeError(f"Could not prepare meeting start: {prepared}")

    async def click_button(label: str) -> dict[str, Any]:
        return await click_visible_button(
            cdp,
            label=label,
            timeout_sec=timeout_sec,
            prefer_last=True,
        )

    async def wait_text(label: str, expected: str) -> dict[str, Any]:
        return await wait_for_interaction_state(
            cdp, label=label, timeout_sec=timeout_sec,
            expression=f"""
(() => {{
  const text = document.body ? document.body.innerText : '';
  return {{ ok: text.includes({json.dumps(expected)}), route: window.location.pathname, bodyText: text.slice(0, 1200) }};
}})()
""",
        )

    async def wait_button(label: str) -> dict[str, Any]:
        return await wait_for_interaction_state(
            cdp, label=f"meeting-button-{label}", timeout_sec=timeout_sec,
            expression=f"""
(() => {{
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.textContent || '').trim() === {json.dumps(label)} && !node.disabled);
  return {{ ok: !!button, route: window.location.pathname }};
}})()
""",
        )

    await wait_text("meeting-five-hour-readiness", "Ready for a long meeting")
    await wait_text("meeting-final-stt-five-hour", "Up to 5:00:00")
    await wait_text("meeting-detection-visible", "Zoom meeting detected")
    await click_button("Dismiss")
    await wait_for_interaction_state(
        cdp, label="meeting-detection-dismissed", timeout_sec=timeout_sec,
        expression="(() => ({ ok: !document.body.innerText.includes('Zoom meeting detected') }))()",
    )
    await wait_button("Test microphone and playback")
    await click_button("Test microphone and playback")
    await wait_text("meeting-device-test", "Speaker sound played")
    if screenshot_dir is not None:
        await cdp.evaluate(
            r"""
(() => {
  const heading = Array.from(document.querySelectorAll('h2'))
    .find((node) => (node.textContent || '').includes('Ready the room'));
  const scroller = heading?.closest('.overflow-y-auto');
  if (scroller) scroller.scrollTop = 0;
  window.scrollTo(0, 0);
  return { ok: !!heading, resetNestedScroller: !!scroller };
})()
""",
            timeout=5,
        )
        await asyncio.sleep(0.15)
        screenshots.append(await capture_page_screenshot(
            cdp, output_dir=screenshot_dir, label="meeting-start-readiness"
        ))
        await cdp.evaluate(
            r"""
(() => {
  const button = Array.from(document.querySelectorAll('button'))
    .find((node) => (node.textContent || '').includes('Test microphone and playback'));
  button?.closest('.rounded-2xl')?.scrollIntoView({ block: 'center' });
  return { ok: !!button };
})()
""",
            timeout=5,
        )
        await asyncio.sleep(0.15)
        screenshots.append(await capture_page_screenshot(
            cdp, output_dir=screenshot_dir, label="meeting-start-device-test"
        ))
    await wait_button("Start meeting")
    await click_button("Start meeting")
    await wait_text("meeting-live-reconnecting", "live text is back")
    await wait_text("meeting-live-recovered", "final transcript will be created from saved audio")
    if screenshot_dir is not None:
        screenshots.append(await capture_page_screenshot(
            cdp, output_dir=screenshot_dir, label="meeting-live-recovered"
        ))

    assert backend.meeting is not None
    backend.meeting["state"] = "interrupted"
    backend.meeting["errorCode"] = "process_interrupted"
    backend.meeting["errorMessage"] = "Scriber stopped before the meeting workflow completed."
    disconnected_sockets = await backend.disconnect_websockets()
    await wait_text("meeting-backend-restart-interrupted", "Resume capture")
    reconnected_after_crash = any(ws not in disconnected_sockets for ws in backend.websockets)
    if screenshot_dir is not None:
        screenshots.append(await capture_page_screenshot(
            cdp, output_dir=screenshot_dir, label="meeting-backend-restart-interrupted"
        ))
    await click_button("Resume capture")
    await wait_button("Pause")

    await wait_button("Pause")
    await click_button("Pause")
    await wait_button("Resume")
    await click_button("Resume")
    await wait_button("Pause")
    await click_button("Stop")
    await wait_text("meeting-finalized", "We decided to launch the meeting workspace on Friday.")
    await click_button("System on")
    await wait_text("meeting-system-muted", "System muted")
    await click_button("System muted")
    await wait_text("meeting-system-unmuted", "System on")
    audio_playback = await cdp.evaluate(
        r"""
(async () => {
  const audio = document.querySelector('audio');
  if (!audio) return { ok: false, reason: 'missing meeting audio player' };
  try {
    if (audio.readyState < 2) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('audio load timeout')), 3000);
        audio.addEventListener('canplay', () => { clearTimeout(timer); resolve(); }, { once: true });
        audio.load();
      });
    }
    await audio.play();
    await new Promise((resolve) => {
      const started = performance.now();
      const poll = () => {
        if (audio.currentTime > 0.02 || performance.now() - started > 3000) resolve();
        else setTimeout(poll, 50);
      };
      poll();
    });
    const advanced = audio.currentTime > 0.02;
    audio.pause();
    const state = {
      ok: audio.duration > 0 && advanced && !audio.error,
      readyState: audio.readyState,
      duration: audio.duration,
      advanced,
    };
    return state;
  } catch (error) {
    return { ok: false, reason: String(error) };
  }
})()
""",
        timeout=5,
        user_gesture=True,
    )
    if not audio_playback or not audio_playback.get("ok"):
        raise RuntimeError(f"Meeting audio playback failed: {audio_playback}")

    await click_button("Overview")
    await wait_text("meeting-analysis-available", "Create meeting brief")
    await click_button("Create meeting brief")
    await wait_text("meeting-analysis-ready", "The team approved a Friday launch.")
    import_inbox_state = await cdp.evaluate(
        r"""
(() => {
  const body = document.body?.innerText || '';
  const hasError = body.includes('Imports could not be loaded.');
  const hasInbox = body.includes('Imports') && body.includes('Continues after you restart Scriber');
  return { ok: hasInbox && !hasError, hasInbox, hasError };
})()
""",
        timeout=5,
    )
    if not import_inbox_state or not import_inbox_state.get("ok"):
        raise RuntimeError(f"Meeting import inbox did not recover: {import_inbox_state}")
    if screenshot_dir is not None:
        screenshots.append(await capture_page_screenshot(
            cdp, output_dir=screenshot_dir, label="meeting-overview-analysis"
        ))

    await click_button("Action items")
    action_changed = await click_visible_target(
        cdp,
        label="meeting-complete-action-item",
        selector='button[aria-label="Complete action item"]',
        timeout_sec=timeout_sec,
    )
    action_deadline = time.monotonic() + timeout_sec
    while time.monotonic() < action_deadline and "action-item" not in backend.meeting_requests:
        await asyncio.sleep(0.1)
    if "action-item" not in backend.meeting_requests:
        raise RuntimeError(f"Action item edit did not reach backend: {backend.meeting_requests}")

    await click_button("Transcript")
    search_prepared = await cdp.evaluate(
        r"""
(() => {
  const input = document.querySelector('input[aria-label="Search this meeting transcript"]');
  if (!input) return { ok: false, reason: 'missing transcript search input' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'Customer approval');
  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'Customer approval' }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not search_prepared or not search_prepared.get("ok"):
        raise RuntimeError(f"Could not search the meeting transcript: {search_prepared}")
    transcript_search = await wait_for_interaction_state(
        cdp,
        label="meeting-transcript-search",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body?.innerText || '';
  const marks = Array.from(document.querySelectorAll('mark')).map((node) => node.textContent || '');
  const target = document.querySelector('button[aria-label="Play transcript segment from 0:05 to 0:08"]');
  return {
    ok: text.includes('1 of 2 parts')
      && text.includes('Customer approval remains open before release.')
      && !text.includes('We decided to launch the meeting workspace on Friday.')
      && marks.some((value) => value.toLowerCase().includes('customer approval'))
      && !!target,
    marks,
    targetTitle: target?.getAttribute('title') || ''
  };
})()
""",
    )
    seek_clicked = await click_visible_target(
        cdp,
        label="meeting-timestamped-transcript-result",
        selector='button[aria-label="Play transcript segment from 0:05 to 0:08"]',
        timeout_sec=timeout_sec,
    )
    transcript_seek = await wait_for_interaction_state(
        cdp,
        label="meeting-transcript-seek",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const audio = document.querySelector('audio');
  return {
    ok: !!audio && audio.currentTime >= 4.9 && audio.currentTime < 6.5 && !audio.error,
    currentTime: audio?.currentTime || 0,
    source: audio?.getAttribute('src') || '',
    paused: audio?.paused
  };
})()
""",
    )
    edit_opened = await click_visible_button(
        cdp,
        label="Edit",
        timeout_sec=timeout_sec,
        prefer_last=False,
    )
    edit_prepared = await cdp.evaluate(
        r"""
(() => {
  const textarea = document.querySelector('textarea[aria-label^="Edit transcript for"]');
  if (!textarea) return { ok: false, reason: 'missing transcript correction textarea' };
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
  setter?.call(textarea, 'Customer approval is confirmed before release.');
  textarea.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not edit_prepared or not edit_prepared.get("ok"):
        raise RuntimeError(f"Could not prepare transcript correction: {edit_prepared}")
    await click_button("Save correction")
    transcript_correction = await wait_for_interaction_state(
        cdp,
        label="meeting-transcript-correction",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body?.innerText || '';
  return {
    ok: text.includes('Customer approval is confirmed before release.')
      && text.includes('Transcript corrected after this brief was generated')
      && text.includes('Edited'),
    hasCorrection: text.includes('Customer approval is confirmed before release.'),
    hasStaleBriefWarning: text.includes('Transcript corrected after this brief was generated'),
    hasEditedBadge: text.includes('Edited')
  };
})()
""",
    )
    await click_button("Undo latest")
    transcript_undo = await wait_for_interaction_state(
        cdp,
        label="meeting-transcript-correction-undo",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body?.innerText || '';
  return {
    ok: text.includes('Customer approval remains open before release.')
      && !text.includes('Customer approval is confirmed before release.'),
    restoredOriginal: text.includes('Customer approval remains open before release.')
  };
})()
""",
    )
    speaker_changed = await cdp.evaluate(
        r"""
(() => {
  const input = document.querySelector('input[aria-label="Rename Alex"]');
  if (!input) return { ok: false, reason: 'missing speaker rename control' };
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  valueSetter.call(input, 'Alex Morgan');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not speaker_changed or not speaker_changed.get("ok"):
        raise RuntimeError(f"Could not rename meeting speaker: {speaker_changed}")

    await click_button("Notes")
    note_changed = await cdp.evaluate(
        r"""
(() => {
  const textarea = document.querySelector('main section textarea[placeholder*="Capture decisions"]');
  if (!textarea) return { ok: false, reason: 'missing meeting notes control' };
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  valueSetter.call(textarea, 'Customer approval is still required.');
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not note_changed or not note_changed.get("ok"):
        raise RuntimeError(f"Could not edit meeting notes: {note_changed}")
    edit_deadline = time.monotonic() + timeout_sec
    while time.monotonic() < edit_deadline and not {"action-item", "speaker", "note"}.issubset(backend.meeting_requests):
        await asyncio.sleep(0.1)
    if not {"action-item", "speaker", "note"}.issubset(backend.meeting_requests):
        raise RuntimeError(f"Meeting edits did not reach backend: {backend.meeting_requests}")

    await click_button("Ask meeting")
    chat_prepared = await cdp.evaluate(
        r"""
(() => {
  const textarea = document.querySelector('textarea[placeholder*="What did we decide"]');
  if (!textarea) return { ok: false, reason: 'missing chat textarea' };
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  valueSetter.call(textarea, 'When is the launch?');
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not chat_prepared or not chat_prepared.get("ok"):
        raise RuntimeError(f"Could not prepare meeting chat: {chat_prepared}")
    await click_button("Ask meeting")
    await wait_text("meeting-chat-answer", "Friday was selected for launch.")

    await cdp.evaluate(
        r"""
(() => {
  window.__scriberSmokeDownloads = [];
  if (!window.__scriberSmokeOriginalAnchorClick) {
    window.__scriberSmokeOriginalAnchorClick = HTMLAnchorElement.prototype.click;
  }
  HTMLAnchorElement.prototype.click = function () {
    if (this.download) {
      window.__scriberSmokeDownloads.push({ name: String(this.download), href: String(this.href) });
      return;
    }
    return window.__scriberSmokeOriginalAnchorClick.call(this);
  };
  return { ok: true };
})()
""",
        timeout=5,
    )
    for export_index, export_format in enumerate(("JSON", "MD", "PDF", "DOCX"), start=1):
        await click_visible_button(
            cdp,
            label="Save or share",
            selector="button",
            timeout_sec=timeout_sec,
            exact_text=False,
            prefer_last=False,
        )
        await click_visible_target(
            cdp,
            label=f"meeting-export-item-{export_format.lower()}",
            selector=f'[aria-label="Export meeting as {export_format}"]',
            timeout_sec=timeout_sec,
        )
        await wait_for_interaction_state(
            cdp,
            label=f"meeting-export-download-{export_format.lower()}",
            timeout_sec=timeout_sec,
            expression=f"(() => ({{ ok: (window.__scriberSmokeDownloads || []).length >= {export_index} }}))()",
        )
    export_state = await cdp.evaluate(
        r"""
(() => {
  const downloads = Array.from(window.__scriberSmokeDownloads || []);
  if (window.__scriberSmokeOriginalAnchorClick) {
    HTMLAnchorElement.prototype.click = window.__scriberSmokeOriginalAnchorClick;
    delete window.__scriberSmokeOriginalAnchorClick;
  }
  delete window.__scriberSmokeDownloads;
  return {
    ok: downloads.length === 4
      && ['.json', '.md', '.pdf', '.docx'].every((suffix) =>
        downloads.some((item) => item.name.toLowerCase().endsWith(suffix))),
    downloads,
  };
})()
""",
        timeout=5,
    )
    if not export_state or not export_state.get("ok"):
        raise RuntimeError(f"Meeting exports failed: {export_state}")

    await click_visible_button(
        cdp,
        label="Save or share",
        selector="button",
        timeout_sec=timeout_sec,
        exact_text=False,
        prefer_last=False,
    )
    await click_visible_target(
        cdp,
        label="meeting-email-export-item",
        selector='[role="menuitem"]',
        timeout_sec=timeout_sec,
        text="Create email draft",
        exact_text=True,
    )
    await wait_text("meeting-email-preview", "Meeting follow-up: Browser smoke product sync")
    await wait_for_interaction_state(
        cdp,
        label="meeting-email-draft-controls",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const markdown = dialog?.querySelector('input[name="meeting-email-attachment"][value="md"]');
  const compose = Array.from(dialog?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').includes('Open email with summary'));
  return {
    ok: !!dialog && !!markdown && !!compose,
    hasDialog: !!dialog,
    inputs: Array.from(dialog?.querySelectorAll('input') || []).map((node) => ({ name: node.name, value: node.value })),
    buttons: Array.from(dialog?.querySelectorAll('button') || []).map((node) => (node.textContent || '').trim()),
    text: (dialog?.innerText || '').slice(0, 1200),
  };
})()
""",
    )
    await click_visible_target(
        cdp,
        label="meeting-email-markdown-attachment",
        selector='[role="dialog"] label',
        timeout_sec=timeout_sec,
        text="Markdown",
        exact_text=True,
    )
    await wait_for_interaction_state(
        cdp,
        label="meeting-email-markdown-selected",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const markdown = dialog?.querySelector('input[name="meeting-email-attachment"][value="md"]');
  const downloadButton = Array.from(dialog?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').includes('Save email draft + MD') && !node.disabled);
  return { ok: !!markdown?.checked && !!downloadButton };
})()
""",
    )
    await cdp.evaluate(
        r"""
(() => {
  window.__scriberSmokeEmailDownloads = [];
  if (!window.__scriberSmokeEmailOriginalAnchorClick) {
    window.__scriberSmokeEmailOriginalAnchorClick = HTMLAnchorElement.prototype.click;
  }
  HTMLAnchorElement.prototype.click = function () {
    if (this.download) {
      window.__scriberSmokeEmailDownloads.push({ name: String(this.download), href: String(this.href) });
      return;
    }
    return window.__scriberSmokeEmailOriginalAnchorClick.call(this);
  };
  return { ok: true };
})()
""",
        timeout=5,
    )
    try:
        await click_visible_button(
            cdp,
            label="Save email draft + MD",
            selector='[role="dialog"] button',
            timeout_sec=timeout_sec,
            exact_text=False,
            prefer_last=False,
        )
        email_export_state = await wait_for_interaction_state(
            cdp,
            label="meeting-email-draft-download",
            timeout_sec=timeout_sec,
            expression=r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const downloads = Array.from(window.__scriberSmokeEmailDownloads || []);
  const compose = Array.from(dialog?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').includes('Open email with summary'));
  return {
    ok: downloads.length === 1
      && downloads[0].name.toLowerCase().endsWith('.eml'),
    downloads,
    dialogOpen: !!dialog,
    hasComposeAction: !!compose,
    recipientsVisible: (dialog?.textContent || '').includes('morgan@example.com')
      && (dialog?.textContent || '').includes('riley@example.com')
  };
})()
""",
        )
    finally:
        await cdp.evaluate(
            r"""
(() => {
  if (window.__scriberSmokeEmailOriginalAnchorClick) {
    HTMLAnchorElement.prototype.click = window.__scriberSmokeEmailOriginalAnchorClick;
  }
  delete window.__scriberSmokeEmailOriginalAnchorClick;
  delete window.__scriberSmokeEmailDownloads;
  return { ok: true };
})()
""",
            timeout=5,
        )
    if not email_export_state or not email_export_state.get("ok"):
        raise RuntimeError(f"Meeting email draft failed: {email_export_state}")
    if email_export_state.get("dialogOpen"):
        await click_visible_button(
            cdp,
            label="Close",
            selector='[role="dialog"] button',
            timeout_sec=timeout_sec,
            prefer_last=False,
        )
    await wait_for_interaction_state(
        cdp,
        label="meeting-email-dialog-closed",
        timeout_sec=timeout_sec,
        expression="(() => ({ ok: !document.querySelector('[role=dialog]') }))()",
    )

    delivery_panel = await wait_for_interaction_state(
        cdp,
        label="meeting-delivery-panel",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const summary = Array.from(document.querySelectorAll('summary'))
    .find((node) => (node.textContent || '').includes('Delivery & integrations'));
  const details = summary?.closest('details');
  return { ok: !!summary && !!details, open: !!details?.open };
})()
""",
    )
    if not delivery_panel.get("open"):
        await click_visible_target(
            cdp,
            label="meeting-delivery-panel-toggle",
            selector="summary",
            timeout_sec=timeout_sec,
            text="Delivery & integrations",
            exact_text=False,
        )
    delivery_panel = await wait_for_interaction_state(
        cdp,
        label="meeting-delivery-panel-open",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const summary = Array.from(document.querySelectorAll('summary'))
    .find((node) => (node.textContent || '').includes('Delivery & integrations'));
  const details = summary?.closest('details');
  return { ok: !!details?.open };
})()
""",
    )

    webhook_prepared = await cdp.evaluate(
        r"""
(() => {
  const url = document.querySelector('#meeting-webhook-url');
  const secret = document.querySelector('#meeting-webhook-secret');
  if (!url || !secret) return { ok: false, reason: 'missing webhook controls' };
  const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  valueSetter.call(url, 'https://automation.example/meeting');
  url.dispatchEvent(new Event('input', { bubbles: true }));
  valueSetter.call(secret, 'smoke-signing-secret');
  secret.dispatchEvent(new Event('input', { bubbles: true }));
  return { ok: true };
})()
""",
        timeout=5,
    )
    if not webhook_prepared or not webhook_prepared.get("ok"):
        raise RuntimeError(f"Could not prepare meeting webhook: {webhook_prepared}")
    await wait_button("Preview payload")
    await click_button("Preview payload")
    await wait_text("meeting-webhook-preview", "512 B")
    confirmation = await click_visible_target(
        cdp,
        label="meeting-webhook-confirmation",
        selector="label",
        timeout_sec=timeout_sec,
        text="I reviewed this target and payload.",
        exact_text=True,
    )
    await wait_button("Send webhook")
    await click_button("Send webhook")
    await wait_text("meeting-webhook-delivered", "delivered")

    delete_opened = await click_visible_target(
        cdp,
        label="meeting-delete-control",
        selector='button[aria-label^="Delete "]',
        timeout_sec=timeout_sec,
    )
    await wait_text("meeting-delete-confirmation", "This cannot be undone")
    await click_button("Delete meeting")
    await wait_text("meeting-delete-complete", "Your first meeting will appear here")

    expected_requests = ["dismiss-detection", "device-test", "start", "resume", "pause", "resume", "stop", "analyze", "action-item", "segment-edit", "segment-undo", "speaker", "note", "chat", "webhook", "delete"]
    start_payload_ok = (
        backend.meeting_start_payload.get("microphoneNativeEndpointIdHash") == "a" * 32
        and backend.meeting_start_payload.get("renderNativeEndpointIdHash") == "b" * 32
        and backend.meeting_start_payload.get("liveProvider") == "soniox"
        and backend.meeting_start_payload.get("finalProvider") == "soniox_async"
    )
    ok = (
        backend.meeting_requests == expected_requests
        and backend.meeting_exports == ["json", "md", "pdf", "docx"]
        and backend.meeting_email_exports == ["md"]
        and start_payload_ok
        and reconnected_after_crash
        and bool(audio_playback and audio_playback.get("ok"))
        and bool(transcript_search and transcript_search.get("ok"))
        and bool(transcript_seek and transcript_seek.get("ok"))
        and bool(transcript_correction and transcript_correction.get("ok"))
        and bool(transcript_undo and transcript_undo.get("ok"))
        and bool(import_dialog and import_dialog.get("ok"))
        and bool(durable_import_state and durable_import_state.get("ok"))
        and bool(import_inbox_state and import_inbox_state.get("ok"))
    )
    return {
        "name": "meeting-end-to-end", "ok": ok,
        "requests": list(backend.meeting_requests),
        "exports": list(backend.meeting_exports),
        "emailExports": list(backend.meeting_email_exports),
        "startPayloadOk": start_payload_ok,
        "startPayload": dict(backend.meeting_start_payload),
        "backendCrashRecovery": {
            "ok": reconnected_after_crash,
            "disconnectedSocketCount": len(disconnected_sockets),
            "reconnected": reconnected_after_crash,
        },
        "audioPlayback": audio_playback,
        "transcriptSearch": transcript_search,
        "transcriptSeek": transcript_seek,
        "transcriptCorrection": transcript_correction,
        "transcriptUndo": transcript_undo,
        "meetingImportDialog": import_dialog,
        "durableMeetingImport": durable_import_state,
        "meetingImportInboxRecovery": import_inbox_state,
        "screenshots": screenshots,
        "exportState": export_state,
        "emailExportState": email_export_state,
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
            and any(
                isinstance(patch.get("apiKeys"), dict)
                and patch["apiKeys"].get("openrouter") == "smoke-openrouter-key"
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


async def save_settings_credential(
    cdp: CdpClient,
    *,
    credential_id: str,
    value: str,
    timeout_sec: float,
) -> dict[str, Any]:
    credential_json = json.dumps(credential_id)
    value_json = json.dumps(value)
    state = await wait_for_interaction_state(
        cdp,
        label=f"settings-credential-{credential_id}",
        timeout_sec=timeout_sec,
        expression=f"""
(() => {{
  const credentialId = {credential_json};
  const expectedValue = {value_json};
  const marker = `__scriberSmokeCredential${{credentialId.replace(/[^a-z0-9]/gi, '')}}`;
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) {{
    const trigger = document.querySelector(`[data-credential-id="${{credentialId}}"]`);
    if (!trigger) return {{ ok: false, reason: 'missing credential trigger', credentialId }};
    trigger.click();
    return {{ ok: false, opening: true, credentialId }};
  }}

  const title = dialog.querySelector('[role="heading"]')?.textContent?.trim()
    || dialog.querySelector('h1,h2,h3')?.textContent?.trim()
    || '';
  if (title !== credentialId) {{
    return {{ ok: false, reason: 'wrong credential dialog', credentialId, title }};
  }}

  const input = dialog.querySelector('input');
  const saveButton = Array.from(dialog.querySelectorAll('button'))
    .find((node) => ['Save', 'Saved'].includes((node.textContent || '').trim()));
  if (!input || !saveButton) {{
    return {{ ok: false, reason: 'missing credential controls', credentialId }};
  }}

  const phase = window[marker] || '';
  if (input.value !== expectedValue) {{
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, expectedValue);
    input.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: expectedValue }}));
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    window[marker] = 'value';
    return {{ ok: false, valueEntered: true, credentialId }};
  }}

  const buttonText = (saveButton.textContent || '').trim();
  if (phase === 'value' && buttonText === 'Save') {{
    saveButton.click();
    window[marker] = 'saving';
    return {{ ok: false, saving: true, credentialId }};
  }}
  return {{
    ok: phase === 'saving' && buttonText === 'Saved',
    credentialId,
    buttonText,
    valueMatches: input.value === expectedValue
  }};
}})()
""",
    )
    await cdp.evaluate(
        r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const closeButton = Array.from(dialog?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').trim() === 'Close');
  closeButton?.click();
  return { ok: !!closeButton };
})()
""",
        timeout=5,
    )
    await wait_for_interaction_state(
        cdp,
        label=f"settings-credential-close-{credential_id}",
        timeout_sec=timeout_sec,
        expression="(() => ({ ok: !document.querySelector('[role=\"dialog\"]') }))()",
    )
    return state


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
    expected_links = (
        ("OpenAI", "OpenAI keys", "https://platform.openai.com/api-keys"),
        ("Deepgram", "Deepgram console", "https://console.deepgram.com/"),
        ("AssemblyAI", "AssemblyAI dashboard", "https://www.assemblyai.com/dashboard"),
        ("Gemini", "Google AI Studio", "https://aistudio.google.com/app/apikey"),
        ("OpenRouter", "OpenRouter keys", "https://openrouter.ai/settings/keys"),
        ("Google Cloud", "Google Cloud credentials", "https://console.cloud.google.com/apis/credentials"),
        ("Soniox", "Soniox console", "https://console.soniox.com/"),
        ("Smallest AI", "Smallest AI console", "https://app.smallest.ai/"),
        ("Mistral", "Mistral API keys", "https://console.mistral.ai/api-keys"),
        ("Azure", "Azure MAI Speech resource", "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices"),
        ("Gladia", "Gladia API keys", "https://app.gladia.io/api-keys"),
        ("Groq", "Groq API keys", "https://console.groq.com/keys"),
        ("Speechmatics", "Speechmatics portal", "https://portal.speechmatics.com/"),
    )
    results: list[dict[str, Any]] = []
    for credential_id, title, href in expected_links:
        state = await wait_for_interaction_state(
            cdp,
            label=f"settings-help-link-{credential_id}",
            timeout_sec=timeout_sec,
            expression=f"""
(() => {{
  const credentialId = {json.dumps(credential_id)};
  const expectedTitle = {json.dumps(title)};
  const expectedHref = {json.dumps(href)};
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) {{
    const trigger = document.querySelector(`[data-credential-id="${{credentialId}}"]`);
    if (!trigger) return {{ ok: false, reason: 'missing credential trigger', credentialId }};
    trigger.click();
    return {{ ok: false, opening: true, credentialId }};
  }}
  const link = Array.from(dialog.querySelectorAll('a[target="_blank"]'))
    .find((node) => (node.getAttribute('title') || '') === expectedTitle);
  return {{
    ok: !!link && link.href === expectedHref,
    credentialId,
    expectedTitle,
    expectedHref,
    actualHref: link?.href || '',
    availableTitles: Array.from(dialog.querySelectorAll('a')).map((node) => node.getAttribute('title') || '')
  }};
}})()
""",
        )
        results.append(state)
        await cdp.evaluate(
            r"""
(() => {
  const dialog = document.querySelector('[role="dialog"]');
  const closeButton = Array.from(dialog?.querySelectorAll('button') || [])
    .find((node) => (node.textContent || '').trim() === 'Close');
  closeButton?.click();
  return { ok: !!closeButton };
})()
""",
            timeout=5,
        )
        await wait_for_interaction_state(
            cdp,
            label=f"settings-help-link-close-{credential_id}",
            timeout_sec=timeout_sec,
            expression="(() => ({ ok: !document.querySelector('[role=\"dialog\"]') }))()",
        )
    return {
        "name": "settings-help-links",
        "ok": True,
        "checkedCount": len(results),
        "results": results,
    }


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
      ok: true,
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
    ok: !!removeInput,
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


async def exercise_meeting_settings(
    cdp: CdpClient,
    *,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    state = await wait_for_interaction_state(
        cdp,
        label="meeting-settings",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const section = document.querySelector('#settings-meetings');
  if (!section) return { ok: false, reason: 'missing meeting settings section' };
  const finalTrigger = section.querySelector('button[aria-label="Final meeting transcription model"]');
  const analysisTrigger = section.querySelector('button[aria-label="Meeting summary model"]');
  const retentionTrigger = section.querySelector('button[aria-label="Default meeting audio retention"]');
  const smartTurn = section.querySelector('[role="switch"][aria-label="Keep meeting live sentences together across short pauses"]');
  const aec = section.querySelector('[role="switch"][aria-label="Reduce speaker echo in meetings"]');
  const autoAnalyze = section.querySelector('[role="switch"][aria-label="Automatically analyze completed meetings"]');
  if (!finalTrigger || !analysisTrigger || !retentionTrigger || !smartTurn || !aec || !autoAnalyze) {
    return { ok: false, reason: 'missing meeting pipeline control' };
  }

  const stage = Number(window.__scriberMeetingSettingsStage || 0);
  if (stage === 0) {
    finalTrigger.click();
    window.__scriberMeetingSettingsStage = 1;
    return { ok: false, waiting: 'final-options' };
  }
  if (stage === 1) {
    const option = Array.from(document.querySelectorAll('[role="option"]'))
      .find((node) => (node.textContent || '').includes('AssemblyAI'));
    if (!option) return { ok: false, waiting: 'assemblyai-option' };
    option.click();
    window.__scriberMeetingSettingsStage = 2;
    return { ok: false, waiting: 'final-save' };
  }
  if (stage === 2) {
    if (!(finalTrigger.textContent || '').includes('AssemblyAI')) return { ok: false, waiting: 'assemblyai-selection' };
    analysisTrigger.click();
    window.__scriberMeetingSettingsStage = 3;
    return { ok: false, waiting: 'analysis-options' };
  }
  if (stage === 3) {
    const option = Array.from(document.querySelectorAll('[role="option"]'))
      .find((node) => (node.textContent || '').trim() === 'Gemini 3.5 Flash');
    if (!option) return { ok: false, waiting: 'gemini-option' };
    option.click();
    window.__scriberMeetingSettingsStage = 4;
    return { ok: false, waiting: 'analysis-save' };
  }
  if (stage === 4) {
    if (!(analysisTrigger.textContent || '').includes('Gemini 3.5 Flash')) return { ok: false, waiting: 'gemini-selection' };
    for (const control of [smartTurn, aec, autoAnalyze]) {
      if (control.getAttribute('aria-checked') === 'true') control.click();
    }
    retentionTrigger.click();
    window.__scriberMeetingSettingsStage = 5;
    return { ok: false, waiting: 'retention-options' };
  }
  if (stage === 5) {
    const option = Array.from(document.querySelectorAll('[role="option"]'))
      .find((node) => (node.textContent || '').trim() === '30 days');
    if (!option) return { ok: false, waiting: 'retention-option' };
    option.click();
    window.__scriberMeetingSettingsStage = 6;
    return { ok: false, waiting: 'retention-save' };
  }

  const text = section.textContent || '';
  return {
    ok: (finalTrigger.textContent || '').includes('AssemblyAI')
      && (analysisTrigger.textContent || '').includes('Gemini 3.5 Flash')
      && (retentionTrigger.textContent || '').includes('30 days')
      && smartTurn.getAttribute('aria-checked') === 'false'
      && aec.getAttribute('aria-checked') === 'false'
      && autoAnalyze.getAttribute('aria-checked') === 'false'
      && text.includes('Includes speaker names and exact timing.')
      && text.includes('Local speaker separation')
      && text.includes('Protected every 30 seconds.'),
    finalModel: finalTrigger.textContent,
    analysisModel: analysisTrigger.textContent,
    retention: retentionTrigger.textContent,
    smartTurn: smartTurn.getAttribute('aria-checked'),
    aec: aec.getAttribute('aria-checked'),
    autoAnalyze: autoAnalyze.getAttribute('aria-checked')
  };
})()
""",
    )
    deadline = time.monotonic() + timeout_sec
    required = {
        "meetingFinalProvider": "assemblyai",
        "meetingAnalysisModel": "gemini-3.5-flash",
        "meetingSmartTurnEnabled": False,
        "meetingAecEnabled": False,
        "meetingAutoAnalyze": False,
        "meetingAudioRetentionDays": 30,
    }
    while time.monotonic() < deadline:
        merged: dict[str, Any] = {}
        for patch in backend.settings_patches:
            merged.update(patch)
        if all(merged.get(key) == value for key, value in required.items()):
            return {"name": "meeting-settings", "ok": True, "state": state, "saved": required}
        await asyncio.sleep(0.05)
    raise RuntimeError(
        "Meeting settings did not persist all selections. "
        f"Observed patches: {backend.settings_patches}"
    )


async def exercise_meeting_identity_settings(
    cdp: CdpClient,
    *,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    state = await wait_for_interaction_state(
        cdp,
        label="meeting-identity-settings",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const section = document.querySelector('#settings-meetings');
  if (!section) return { ok: false, reason: 'missing meeting settings section' };
  const text = section.textContent || '';
  const stage = Number(window.__scriberMeetingIdentityStage || 0);
  const setNativeValue = (node, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(node, value);
    node.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  };
  if (stage === 0) {
    const addVoice = Array.from(section.querySelectorAll('button'))
      .find((node) => (node.textContent || '').trim() === 'Add voice');
    if (!addVoice || addVoice.disabled) return { ok: false, waiting: 'add-voice' };
    addVoice.scrollIntoView({ block: 'center' });
    addVoice.click();
    window.__scriberMeetingIdentityStage = 1;
    return { ok: false, waiting: 'voice-enrollment-dialog' };
  }
  if (stage === 1) {
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((node) => (node.textContent || '').includes('Teach Scriber a voice'));
    const input = dialog?.querySelector('#voice-enrollment-name');
    if (!dialog || !input) return { ok: false, waiting: 'voice-enrollment-name' };
    setNativeValue(input, 'Katherine Johnson');
    window.__scriberMeetingIdentityStage = 2;
    return { ok: false, waiting: 'voice-enrollment-name-state' };
  }
  if (stage === 2) {
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((node) => (node.textContent || '').includes('Teach Scriber a voice'));
    const record = Array.from(dialog?.querySelectorAll('button') || [])
      .find((node) => (node.textContent || '').includes('Record 8-second sample'));
    if (!dialog || !record || record.disabled) return { ok: false, waiting: 'voice-enrollment-record' };
    record.click();
    window.__scriberMeetingIdentityStage = 3;
    return { ok: false, waiting: 'voice-enrollment-result' };
  }
  if (stage === 3) {
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((node) => (node.textContent || '').includes('Katherine Johnson is ready'));
    const done = Array.from(dialog?.querySelectorAll('button') || [])
      .find((node) => (node.textContent || '').trim() === 'Done');
    if (!dialog || !done) return { ok: false, waiting: 'voice-enrollment-success' };
    done.click();
    window.__scriberMeetingIdentityStage = 4;
    return { ok: false, waiting: 'enrolled-profile-list' };
  }
  if (stage === 4) {
    if (!text.includes('Katherine Johnson')) return { ok: false, waiting: 'enrolled-profile' };
    const profile = Array.from(section.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Speaker a1b2c3'));
    if (!profile) return { ok: false, waiting: 'anonymous-profile' };
    profile.scrollIntoView({ block: 'center' });
    profile.click();
    window.__scriberMeetingIdentityStage = 5;
    return { ok: false, waiting: 'profile-editor' };
  }
  if (stage === 5) {
    const input = section.querySelector('input[aria-label="Name saved speaker Speaker a1b2c3"]');
    const save = Array.from(section.querySelectorAll('button')).find((node) => (node.textContent || '').trim() === 'Save');
    if (!input || !save) return { ok: false, waiting: 'profile-editor-controls' };
    setNativeValue(input, 'Ada Lovelace');
    save.click();
    window.__scriberMeetingIdentityStage = 6;
    return { ok: false, waiting: 'profile-rename' };
  }
  if (stage === 6) {
    if (!text.includes('Ada Lovelace')) return { ok: false, waiting: 'renamed-profile' };
    const remove = section.querySelector('button[aria-label="Delete saved speaker Grace Hopper"]');
    if (!remove) return { ok: false, waiting: 'profile-delete-control' };
    remove.click();
    window.__scriberMeetingIdentityStage = 7;
    return { ok: false, waiting: 'profile-delete' };
  }
  if (stage === 7) {
    const dialog = document.querySelector('[role="alertdialog"]');
    const confirm = Array.from(dialog?.querySelectorAll('button') || [])
      .find((node) => (node.textContent || '').trim() === 'Delete speaker');
    if (!dialog || !confirm) return { ok: false, waiting: 'profile-delete-confirmation' };
    confirm.click();
    window.__scriberMeetingIdentityStage = 8;
    return { ok: false, waiting: 'profile-delete-request' };
  }
  if (stage === 8) {
    if (text.includes('Grace Hopper')) return { ok: false, waiting: 'deleted-profile-disappear' };
    const connect = Array.from(section.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Connect Outlook'));
    if (!connect || connect.disabled) return { ok: false, waiting: 'outlook-connect' };
    connect.scrollIntoView({ block: 'center' });
    connect.click();
    window.__scriberMeetingIdentityStage = 9;
    return { ok: false, waiting: 'outlook-connected' };
  }
  if (stage === 9) {
    if (!text.includes('Outlook is connected')) return { ok: false, waiting: 'connected-status' };
    const sync = Array.from(section.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Sync now'));
    if (!sync) return { ok: false, waiting: 'outlook-sync' };
    sync.click();
    window.__scriberMeetingIdentityStage = 10;
    return { ok: false, waiting: 'outlook-event' };
  }
  if (stage === 10) {
    if (!text.includes('Architecture review')) return { ok: false, waiting: 'synced-event' };
    const disconnect = Array.from(section.querySelectorAll('button'))
      .find((node) => (node.textContent || '').includes('Disconnect Outlook'));
    if (!disconnect) return { ok: false, waiting: 'outlook-disconnect' };
    disconnect.click();
    window.__scriberMeetingIdentityStage = 11;
    return { ok: false, waiting: 'outlook-disconnect-confirmation' };
  }
  if (stage === 11) {
    const dialog = Array.from(document.querySelectorAll('[role="alertdialog"]'))
      .find((node) => (node.textContent || '').includes('Disconnect Outlook?'));
    const confirm = Array.from(dialog?.querySelectorAll('button') || [])
      .find((node) => (node.textContent || '').trim() === 'Disconnect Outlook');
    if (!dialog || !confirm || confirm.disabled) {
      return { ok: false, waiting: 'outlook-disconnect-confirmation' };
    }
    confirm.click();
    window.__scriberMeetingIdentityStage = 12;
    return { ok: false, waiting: 'outlook-disconnected' };
  }
  return {
    ok: text.includes('Ada Lovelace')
      && text.includes('Katherine Johnson')
      && !text.includes('Grace Hopper')
      && text.includes('Outlook is ready to connect')
      && text.includes('2 saved speakers'),
    hasNamedProfile: text.includes('Ada Lovelace'),
    hasEnrolledProfile: text.includes('Katherine Johnson'),
    profileCount: text.includes('2 saved speakers'),
    outlookDisconnected: text.includes('Outlook is ready to connect')
  };
})()
""",
    )
    required_requests = {
        "speaker-profile-enroll", "speaker-profile-rename", "speaker-profile-delete",
        "outlook-connect", "outlook-sync", "outlook-disconnect",
    }
    missing = required_requests.difference(backend.meeting_requests)
    if missing:
        raise RuntimeError(f"Meeting identity Settings requests missing: {sorted(missing)}")
    return {
        "name": "meeting-identity-settings",
        "ok": True,
        "state": state,
        "requests": sorted(required_requests),
    }


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

    mistral_credential = await save_settings_credential(
        cdp,
        credential_id="Mistral",
        value="smoke-mistral-key",
        timeout_sec=timeout_sec,
    )
    gemini_credential = await save_settings_credential(
        cdp,
        credential_id="Gemini",
        value="smoke-gemini-key",
        timeout_sec=timeout_sec,
    )
    openrouter_credential = await save_settings_credential(
        cdp,
        credential_id="OpenRouter",
        value="smoke-openrouter-key",
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
  const findChoice = (label) => Array.from(document.querySelectorAll('button[role="radio"]'))
    .find((node) => (node.getAttribute('title') || '').startsWith(`${label}:`));
  const clickChoice = (label) => {
    const node = findChoice(label);
    if (!node) return false;
    node.click();
    return true;
  };
  const findSwitchInSetting = (label) => {
    const labelNode = Array.from(document.querySelectorAll('label'))
      .find((node) => (node.textContent || '').trim() === label);
    const row = labelNode?.parentElement?.parentElement;
    return row?.querySelector('[role="switch"]');
  };
  const clickSwitchInSetting = (label) => {
    const control = findSwitchInSetting(label);
    if (!control) return false;
    if (control.getAttribute('aria-checked') !== 'true') {
      control.click();
    }
    return true;
  };
  const findTextArea = (placeholderIncludes) => Array.from(document.querySelectorAll('textarea'))
      .find((item) => (item.getAttribute('placeholder') || '').includes(placeholderIncludes));
  const customVocabularyArea = findTextArea('Enter terms, one per line');
  const summaryPromptArea = findTextArea('Summarize the key points');

  const actions = {
    transcription: !!findChoice('Mistral Batch'),
    language: !!document.querySelector('input[aria-label="Select German as default transcription language"]'),
    summarizationModel: !!findChoice('Gemini 3.5 Flash'),
    openRouterSummaryModels: !!findChoice('MiniMax M3 Nitro') && !!findChoice('GLM 5.2 Nitro'),
    autoSummarize: !!findSwitchInSetting('Auto-summarize'),
    customVocabulary: !!customVocabularyArea,
    summaryPrompt: !!summaryPromptArea,
    geminiKey: !!document.querySelector('[data-credential-id="Gemini"]'),
    openRouterKey: !!document.querySelector('[data-credential-id="OpenRouter"]')
  };
  if (!window.__scriberSmokeSettingsControlsClicked) {
    window.__scriberSmokeSettingsControlsClicked = true;
    clickChoice('Mistral Batch');
    document.querySelector('input[aria-label="Select German as default transcription language"]')?.click();
    clickChoice('Gemini 3.5 Flash');
    clickSwitchInSetting('Auto-summarize');
    return { ok: false, waitingForControlSaves: true, actions };
  }

  const text = document.body ? document.body.innerText : '';
  return {
    ok: Object.values(actions).every(Boolean)
      && text.includes('Mistral Batch')
      && text.includes('German')
      && text.includes('Gemini 3.5 Flash')
      && text.includes('MiniMax M3 Nitro')
      && text.includes('GLM 5.2 Nitro'),
    actions,
    hasMistralAsync: text.includes('Mistral Batch'),
    hasGerman: text.includes('German'),
    hasGemini35: text.includes('Gemini 3.5 Flash'),
    hasOpenRouterSummaries: text.includes('MiniMax M3 Nitro') && text.includes('GLM 5.2 Nitro')
  };
})()
""",
    )
    meeting_settings = await exercise_meeting_settings(
        cdp,
        backend=backend,
        timeout_sec=timeout_sec,
    )
    meeting_identity_settings = await exercise_meeting_identity_settings(
        cdp,
        backend=backend,
        timeout_sec=timeout_sec,
    )
    help_links = await exercise_settings_help_links(cdp, timeout_sec=timeout_sec)
    favorite_mic = await exercise_settings_favorite_mic(
        cdp,
        backend=backend,
        timeout_sec=timeout_sec,
    )
    custom_vocabulary = await fill_settings_textarea(
        cdp,
        placeholder_includes="Enter terms, one per line",
        value="Scriber, Gemini 3.5, Quality Loop",
        timeout_sec=timeout_sec,
    )
    summary_prompt = await fill_settings_textarea(
        cdp,
        placeholder_includes="Summarize the key points",
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
        "meetingSettings": meeting_settings,
        "meetingIdentitySettings": meeting_identity_settings,
        "mistralCredential": mistral_credential,
        "geminiCredential": gemini_credential,
        "openRouterCredential": openrouter_credential,
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
  const settingLineFor = (label) => {
    const labelNode = Array.from(document.querySelectorAll('label'))
      .filter(isVisible)
      .find((node) => normalize(node.textContent).toLowerCase() === label.toLowerCase());
    return labelNode?.closest('div.grid') || null;
  };
  const hotkeyRow = settingLineFor('Global hotkey');
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
  const settingLineFor = (label) => {
    const labelNode = Array.from(document.querySelectorAll('label'))
      .filter(isVisible)
      .find((node) => normalize(node.textContent).toLowerCase() === label.toLowerCase());
    return labelNode?.closest('div.grid') || null;
  };
  const hotkeyRow = settingLineFor('Global hotkey');
  const autostartRow = settingLineFor('Start with Windows');
  const hotkeyButton = hotkeyRow?.querySelector('button');
  const hotkeyRect = hotkeyButton?.getBoundingClientRect();
  const hotkeyCenter = hotkeyRect
    ? { x: hotkeyRect.left + hotkeyRect.width / 2, y: hotkeyRect.top + hotkeyRect.height / 2 }
    : null;
  const elementAtHotkeyCenter = hotkeyCenter
    ? document.elementFromPoint(hotkeyCenter.x, hotkeyCenter.y)
    : null;
  const autostartSwitch = autostartRow?.querySelector('[role="switch"]');
  const pushHoldCard = Array.from(document.querySelectorAll('button'))
    .filter(isVisible)
    .find((node) => normalize(node.textContent) === 'Push-to-talk');
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
    if (!dialog || !normalize(dialog.textContent).includes('Record hotkey')) {
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
  if (dialog || text.includes('Record hotkey')) {
    return { ok: false, step: 'waiting-for-hotkey-dialog-close', controls };
  }
  if (!window.__scriberSmokeRecordingModeClicked) {
    window.__scriberSmokeRecordingModeClicked = true;
    pushHoldCard.click();
    return { ok: false, step: 'clicked-push-hold', controls };
  }
  const pushHoldSelected = pushHoldCard.getAttribute('data-state') === 'on';
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
    .find((node) => normalize(node.textContent).includes('Update app'));
  const desktopUpdateTrigger = null;
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
  const input = document.querySelector('input[aria-label="Search YouTube transcript history"]');
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
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeYoutubeClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeYoutubeClipboardWrites || [];
  return {
    ok: writes.length === 1
      && writes[0].includes('synthetic transcript used by the frontend browser smoke test'),
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
  const input = document.querySelector('input[aria-label="Search YouTube transcript history"]');
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
  const input = document.querySelector('#youtube-source-search');
  const button = document.querySelector('button[aria-label="Find video"]');
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
  const input = document.querySelector('#youtube-source-search');
  const button = document.querySelector('button[aria-label="Find video"]');
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
    backend.youtube_search_requests.clear()
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
  const input = document.querySelector('#youtube-source-search');
  const button = document.querySelector('button[aria-label="Find video"]');
  if (!input || !button) return { ok: false, reason: 'missing input/button' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'Scriber queued validation');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  button.click();
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
    if len(backend.youtube_search_requests) != 1:
        raise RuntimeError(
            "YouTube search double click issued "
            f"{len(backend.youtube_search_requests)} backend requests instead of one"
        )

    start_clicked = await cdp.evaluate(
        r"""
(() => {
  const card = document.querySelector('[aria-label="Start transcription for Synthetic YouTube Result"]');
  if (!card) return { ok: false, reason: 'missing result card' };
  card.click();
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
    await asyncio.sleep(0.1)
    if len(backend.youtube_transcribe_requests) != 1:
        raise RuntimeError(
            "YouTube transcription double click issued "
            f"{len(backend.youtube_transcribe_requests)} backend requests instead of one"
        )

    queued_state = await wait_for_interaction_state(
        cdp,
        label="youtube-start-transcription",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const elapsed = (/Elapsed:\s*([0-9:]+)/.exec(text) || [])[1] || '';
  return {
    ok: window.location.pathname === '/transcript/youtube-queued-smoke'
      && text.includes('Synthetic Queued YouTube Transcription')
      && text.includes('Queued')
      && text.includes('Elapsed:')
      && elapsed.split(':').length === 2
      && text.includes('Stop'),
    route: window.location.pathname,
    hasTitle: text.includes('Synthetic Queued YouTube Transcription'),
    hasQueuedStep: text.includes('Queued'),
    hasElapsed: text.includes('Elapsed:'),
    hasStop: text.includes('Stop'),
    elapsed
  };
})()
""",
    )
    queued_state["backendRequestCount"] = len(backend.youtube_transcribe_requests)
    queued_state["searchRequestCount"] = len(backend.youtube_search_requests)
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
      && text.includes('video up to 2GB'),
    view: root?.getAttribute('data-history-view') || '',
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    hasUploadLimitHint: text.includes('Synthetic processes files in-app up to 2GB'),
    hasVideoLimitHint: text.includes('video up to 2GB'),
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
  const elapsed = (/Elapsed:\s*([0-9:]+)/.exec(text) || [])[1] || '';
  return {
    ok: window.location.pathname === '/transcript/file-processing-smoke'
      && text.includes('Synthetic File Processing')
      && text.includes('Preparing audio')
      && text.includes('Elapsed:')
      && elapsed.split(':').length === 2
      && text.includes('Stop'),
    route: window.location.pathname,
    hasTitle: text.includes('Synthetic File Processing'),
    hasStep: text.includes('Preparing audio'),
    hasElapsed: text.includes('Elapsed:'),
    hasStop: text.includes('Stop'),
    elapsed
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
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeFileClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeFileClipboardWrites || [];
  return {
    ok: writes.length === 1
      && writes[0].includes('synthetic transcript used by the frontend browser smoke test'),
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
    ok: text.includes('of 3') && text.includes('Debug console sample error'),
    hasAllEntries: text.includes('of 3'),
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
    ok: /1\s+VISIBLE\s+of 3 logs/i.test(text)
      && text.includes('Debug console sample warning')
      && !text.includes('Debug console sample error'),
    hasOneEntry: /1\s+VISIBLE\s+of 3 logs/i.test(text),
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
    .find((node) => (node.textContent || '').trim() === 'Reset');
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
      && text.includes('of 3')
      && text.includes('Debug console sample error')
      && text.includes('Debug console sample warning'),
    filterValue: input ? input.value : null,
    hasAllEntries: text.includes('of 3'),
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
    ok: text.includes('No matching events') && text.includes('Cleared') && !text.includes('Debug console sample error'),
    hasEmptyState: text.includes('No matching events'),
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
    elapsed_state = await wait_for_interaction_state(
        cdp,
        label="transcript-current-attempt-elapsed",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const elapsed = (/Elapsed:\s*([0-9:]+)/.exec(text) || [])[1] || '';
  return {
    ok: elapsed.split(':').length === 2,
    elapsed,
    hasLegacyCreatedAtOverflow: elapsed.split(':').length > 2
  };
})()
""",
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
        "elapsed": elapsed_state,
        "stopped": stopped_state,
    }


async def exercise_transcript_detail_actions(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    backend: FrontendSmokeBackend,
    timeout_sec: float,
) -> dict[str, Any]:
    backend.settings_get_count = 0
    await cdp.call("Page.navigate", {"url": f"{frontend_base_url}/transcript/mic-00001"}, timeout=10)
    await wait_for_route_ready(
        cdp,
        route="/transcript/mic-00001",
        expected_text=ROUTE_EXPECTATIONS["/transcript/mic-00001"],
        expect_history_virtualized=False,
        timeout_sec=timeout_sec,
    )
    await asyncio.sleep(0.1)
    settings_request_state = {
        # One request is the app-wide bootstrap. The transcript detail must not
        # add a second request for a mic transcript.
        "ok": backend.settings_get_count == 1,
        "settingsGetCount": backend.settings_get_count,
        "bootstrapOnly": backend.settings_get_count == 1,
    }
    if not settings_request_state["ok"]:
        raise RuntimeError(
            "Mic transcript detail added settings traffic beyond bootstrap: "
            f"{backend.settings_get_count} total request(s)"
        )
    detail_requests_before_generic_update = backend.transcript_detail_counts.get("mic-00001", 0)
    await backend.broadcast_history_updated()
    deadline = time.monotonic() + timeout_sec
    while (
        time.monotonic() < deadline
        and backend.transcript_detail_counts.get("mic-00001", 0)
        <= detail_requests_before_generic_update
    ):
        await asyncio.sleep(0.05)
    detail_requests_after_generic_update = backend.transcript_detail_counts.get("mic-00001", 0)
    generic_refresh_state = {
        "ok": detail_requests_after_generic_update > detail_requests_before_generic_update,
        "before": detail_requests_before_generic_update,
        "after": detail_requests_after_generic_update,
    }
    if not generic_refresh_state["ok"]:
        raise RuntimeError(
            "Generic history_updated event did not refresh the active transcript detail"
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

    copy_failure_state = await wait_for_interaction_state(
        cdp,
        label="transcript-detail-copy-failure",
        timeout_sec=timeout_sec,
        expression=r"""
(() => {
  if (!window.__scriberSmokeDetailCopyFailureStarted) {
    const failingClipboard = { writeText: async () => { throw new Error('synthetic clipboard denial'); } };
    try {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: failingClipboard
      });
    } catch (_error) {
      Object.defineProperty(Navigator.prototype, 'clipboard', {
        configurable: true,
        get: () => failingClipboard
      });
    }
    const button = Array.from(document.querySelectorAll('button'))
      .find((node) => {
        const label = node.textContent || '';
        return label.includes('Copy Transcript') || label.includes('Copied!');
      });
    if (!button) return { ok: false, reason: 'missing transcript copy button' };
    window.__scriberSmokeDetailCopyFailureStarted = true;
    button.click();
    return { ok: false, waitingForFailureToast: true };
  }
  const text = document.body ? document.body.innerText : '';
  return {
    ok: text.includes('Copy failed') && text.includes('could not access the clipboard'),
    hasFailureToast: text.includes('Copy failed'),
    hasFailureDescription: text.includes('could not access the clipboard')
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
        "micSettingsRequests": settings_request_state,
        "genericHistoryRefresh": generic_refresh_state,
        "spySetup": setup_state,
        "copy": copy_state,
        "copyFailure": copy_failure_state,
        "exportPdf": export_pdf_state,
        "exportDocx": export_docx_state,
        "summarize": summarize_state,
        "retrySummary": retry_state,
    }


async def exercise_rapid_theme_change(
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
    await cdp.call(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "prefers-reduced-motion", "value": "no-preference"}]},
        timeout=5,
    )
    initial = await cdp.evaluate(
        r"""
(() => {
  const toggle = document.querySelector('button[role="switch"][aria-label^="Switch to"]');
  if (!toggle) return { ok: false, reason: 'missing theme toggle' };
  window.__scriberSmokeOriginalStartViewTransition = document.startViewTransition;
  Object.defineProperty(document, 'startViewTransition', {
    configurable: true,
    value: undefined
  });
  const startedDark = document.documentElement.classList.contains('dark');
  window.__scriberSmokeThemeStartedDark = startedDark;
  toggle.click();
  return { ok: true, startedDark };
})()
""",
        timeout=5,
    )
    if not initial or not initial.get("ok"):
        raise RuntimeError(f"Could not start rapid theme-change smoke: {initial}")

    await cdp.call(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
        timeout=5,
    )
    selection = await wait_for_interaction_state(
        cdp,
        label="rapid-theme-change-selection",
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
  const trigger = document.querySelector('button[aria-label="Theme mode options"]');
  if (!trigger) return { ok: false, reason: 'missing theme options trigger' };
  if (!window.__scriberSmokeThemeMenuOpened) {
    window.__scriberSmokeThemeMenuOpened = true;
    activate(trigger);
    return { ok: false, waitingForMenu: true };
  }
  const target = window.__scriberSmokeThemeStartedDark ? 'Dark' : 'Light';
  const item = Array.from(document.querySelectorAll('[role="menuitem"]'))
    .find((node) => (node.textContent || '').includes(target));
  if (!item) return { ok: false, waitingForItem: target };
  activate(item);
  return { ok: true, target };
})()
""",
    )

    await asyncio.sleep(0.8)
    final_state = await cdp.evaluate(
        r"""
(() => {
  const startedDark = !!window.__scriberSmokeThemeStartedDark;
  const finalDark = document.documentElement.classList.contains('dark');
  const storedTheme = window.localStorage.getItem('scriber-theme') || '';
  const expectedStoredTheme = startedDark ? 'dark' : 'light';
  const revealActive = document.documentElement.dataset.themeRevealActive === 'true';
  const overlays = document.querySelectorAll('.theme-reveal-overlay').length;
  const original = window.__scriberSmokeOriginalStartViewTransition;
  if (original) {
    Object.defineProperty(document, 'startViewTransition', {
      configurable: true,
      value: original
    });
  } else {
    delete document.startViewTransition;
  }
  return {
    ok: finalDark === startedDark
      && storedTheme === expectedStoredTheme
      && !revealActive
      && overlays === 0,
    startedDark,
    finalDark,
    storedTheme,
    expectedStoredTheme,
    revealActive,
    overlays
  };
})()
""",
        timeout=5,
    )
    await cdp.call("Emulation.setEmulatedMedia", {"features": []}, timeout=5)
    if not final_state or not final_state.get("ok"):
        raise RuntimeError(f"Rapid theme change resolved to stale state: {final_state}")
    return {
        "name": "rapid-theme-change",
        "ok": True,
        "initial": initial,
        "selection": selection,
        "final": final_state,
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
  const scroll = root?.closest('[data-app-scroll-container]');
  const text = document.body ? document.body.innerText : '';
  const visibleTitles = Array.from(document.querySelectorAll('.perf-scroll-item'))
    .slice(0, 5)
    .map((node) => (node.textContent || '').trim().slice(0, 120));
  return {
    ok: !!root
      && document.querySelectorAll('.perf-scroll-item').length > 0
      && text.includes('Synthetic Recording 00001'),
    view: root?.getAttribute('data-history-view') || '',
    visibleCards: document.querySelectorAll('.perf-scroll-item').length,
    hasFirstRecording: text.includes('Synthetic Recording 00001'),
    scrollTop: scroll?.scrollTop || 0,
    visibleTitles
  };
})()
""",
    )

    history_requests_before_reconnect = sum(
        1
        for entry in backend.request_log
        if entry.get("path") == "/api/transcripts" and entry.get("type") == "mic"
    )
    disconnected_sockets = await backend.disconnect_websockets()
    reconnect_deadline = time.monotonic() + timeout_sec
    reconnect_state: dict[str, Any] = {}
    while time.monotonic() < reconnect_deadline:
        history_requests_after_reconnect = sum(
            1
            for entry in backend.request_log
            if entry.get("path") == "/api/transcripts" and entry.get("type") == "mic"
        )
        has_reconnected_socket = any(ws not in disconnected_sockets for ws in backend.websockets)
        reconnect_state = {
            "ok": has_reconnected_socket and history_requests_after_reconnect > history_requests_before_reconnect,
            "disconnectedSockets": len(disconnected_sockets),
            "hasReconnectedSocket": has_reconnected_socket,
            "historyRequestsBefore": history_requests_before_reconnect,
            "historyRequestsAfter": history_requests_after_reconnect,
        }
        if reconnect_state["ok"]:
            break
        await asyncio.sleep(0.1)
    if not reconnect_state.get("ok"):
        raise RuntimeError(f"Transcript history did not refresh after WebSocket reconnect: {reconnect_state}")

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
    button.click();
    return {
      ok: false,
      waitingForCopy: true,
      stubbed: !!window.__scriberSmokeClipboardStubbed
    };
  }
  const writes = window.__scriberSmokeClipboardWrites || [];
  return {
    ok: writes.length === 1
      && writes[0].includes('synthetic transcript used by the frontend browser smoke test'),
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
        "reconnect": reconnect_state,
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
      && text.includes('Debug console')
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
      && text.includes('Speech-to-text provider')
      && text.includes('API keys')
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
      && text.includes('YouTube transcription')
      && text.includes('Recent videos')
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
      const wrappingLabel = node.closest('label');
      if (wrappingLabel) {{
        const labelRect = wrappingLabel.getBoundingClientRect();
        if (labelRect.width >= 44 && labelRect.height >= 44) return false;
      }}
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


async def exercise_desktop_page_shell_layouts(
    cdp: CdpClient,
    *,
    frontend_base_url: str,
    timeout_sec: float,
    screenshot_dir: Path | None,
) -> dict[str, Any]:
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 2048, "height": 1252, "deviceScaleFactor": 1, "mobile": False},
        timeout=5,
    )
    await cdp.call(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": False},
        timeout=5,
    )

    results: list[dict[str, Any]] = []
    screenshots: list[str] = []
    try:
        for route, shell_id in PRIMARY_TAB_SHELLS:
            await cdp.call("Page.navigate", {"url": f"{frontend_base_url}{route}"}, timeout=10)
            await wait_for_route_ready(
                cdp,
                route=route,
                expected_text=ROUTE_EXPECTATIONS[route],
                expect_history_virtualized=route in {"/", "/youtube", "/file"},
                timeout_sec=timeout_sec,
            )
            await cdp.evaluate(
                r"""
(() => {
  window.scrollTo(0, 0);
  document.querySelectorAll('[data-app-scroll-container], aside').forEach((node) => {
    node.scrollTop = 0;
    node.scrollLeft = 0;
  });
  return { ok: true };
})()
""",
                timeout=5,
            )
            # Let route-level queries and lazy modules settle before measuring or
            # capturing evidence; an initial paint that disappears is not a pass.
            await asyncio.sleep(0.35)
            state = await cdp.evaluate(
                f"""
(() => {{
  const route = {json.dumps(route)};
  const shellId = {json.dumps(shell_id)};
  const shell = Array.from(document.querySelectorAll('[data-page-shell]'))
    .find((node) => node.getAttribute('data-page-shell') === shellId);
  const scrollContainer = document.querySelector('[data-app-scroll-container="true"]');
  const desktopSidebar = document.querySelector('aside');
  const sidebarStyle = desktopSidebar ? getComputedStyle(desktopSidebar) : null;
  const smoke = window.__scriberSmoke || {{}};
  if (!shell || !scrollContainer) {{
    return {{
      ok: false,
      route,
      shellId,
      reason: !shell ? 'missing data-page-shell hook' : 'missing app scroll container',
      bodyText: (document.body?.innerText || '').slice(0, 500),
      consoleErrors: smoke.consoleErrors || [],
      pageErrors: smoke.pageErrors || [],
      unhandledRejections: smoke.unhandledRejections || []
    }};
  }}

  const round = (value) => Math.round(value * 100) / 100;
  const shellRect = shell.getBoundingClientRect();
  const containerRect = scrollContainer.getBoundingClientRect();
  const style = getComputedStyle(shell);
  const paddingLeft = parseFloat(style.paddingLeft) || 0;
  const paddingRight = parseFloat(style.paddingRight) || 0;
  const computedMaxWidth = parseFloat(style.maxWidth);
  const containerContentRight = containerRect.left + scrollContainer.clientWidth;
  const leftGutter = shellRect.left - containerRect.left;
  const rightGutter = containerContentRight - shellRect.right;
  const containerCenter = containerRect.left + scrollContainer.clientWidth / 2;
  const shellCenter = shellRect.left + shellRect.width / 2;
  const availableSlack = scrollContainer.clientWidth - shellRect.width;
  const maxWidthReached = Number.isFinite(computedMaxWidth)
    && Math.abs(shellRect.width - computedMaxWidth) <= 2;

  return {{
    ok: shellRect.width > 0
      && scrollContainer.clientWidth > 0
      && leftGutter >= -2
      && rightGutter >= -2
      && sidebarStyle?.display !== 'none',
    route,
    shellId,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    rectWidth: round(shellRect.width),
    contentWidth: round(shellRect.width - paddingLeft - paddingRight),
    paddingLeft: round(paddingLeft),
    paddingRight: round(paddingRight),
    computedMaxWidth: Number.isFinite(computedMaxWidth) ? round(computedMaxWidth) : null,
    maxWidthReached,
    containerClientWidth: scrollContainer.clientWidth,
    availableSlack: round(availableSlack),
    leftGutter: round(leftGutter),
    rightGutter: round(rightGutter),
    gutterImbalance: round(Math.abs(leftGutter - rightGutter)),
    centerDelta: round(Math.abs(shellCenter - containerCenter)),
    desktopSidebarDisplay: sidebarStyle?.display || ''
  }};
}})()
""",
                timeout=5,
            )
            results.append(state or {
                "ok": False,
                "route": route,
                "shellId": shell_id,
                "reason": "layout measurement returned no state",
            })
            if screenshot_dir is not None:
                screenshots.append(await capture_page_screenshot(
                    cdp,
                    output_dir=screenshot_dir,
                    label=f"desktop-shell-{shell_id}",
                ))
    finally:
        await cdp.call("Emulation.clearDeviceMetricsOverride", timeout=5)
        await cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": False}, timeout=5)

    measured = [
        item
        for item in results
        if all(isinstance(item.get(key), (int, float)) for key in (
            "rectWidth", "contentWidth", "paddingLeft", "paddingRight",
            "gutterImbalance", "centerDelta", "availableSlack",
        ))
    ]

    def spread(key: str) -> float:
        values = [float(item[key]) for item in measured]
        return round(max(values) - min(values), 2) if values else 999_999.0

    max_width_spread = spread("rectWidth")
    max_content_width_spread = spread("contentWidth")
    max_padding_spread = max(spread("paddingLeft"), spread("paddingRight"))
    max_gutter_imbalance = max(
        (float(item["gutterImbalance"]) for item in measured),
        default=999_999.0,
    )
    max_center_delta = max(
        (float(item["centerDelta"]) for item in measured),
        default=999_999.0,
    )
    live = next((item for item in measured if item.get("route") == "/"), None)
    meetings = next((item for item in measured if item.get("route") == "/meetings"), None)
    meeting_at_most_live = bool(
        live
        and meetings
        and float(meetings["rectWidth"]) <= float(live["rectWidth"]) + 1
    )
    max_width_reached = len(measured) == len(PRIMARY_TAB_SHELLS) and all(
        item.get("maxWidthReached") is True and float(item["availableSlack"]) >= 96
        for item in measured
    )
    ok = (
        len(results) == len(PRIMARY_TAB_SHELLS)
        and len(measured) == len(PRIMARY_TAB_SHELLS)
        and all(item.get("ok") for item in results)
        and max_width_spread <= 2
        and max_content_width_spread <= 2
        and max_padding_spread <= 2
        and max_gutter_imbalance <= 2
        and max_center_delta <= 2
        and meeting_at_most_live
        and max_width_reached
    )
    return {
        "name": "desktop-page-shell-layouts",
        "ok": ok,
        "viewport": {"width": 2048, "height": 1252, "deviceScaleFactor": 1},
        "routes": [route for route, _shell_id in PRIMARY_TAB_SHELLS],
        "routeCount": len(results),
        "maxWidthSpread": max_width_spread,
        "maxContentWidthSpread": max_content_width_spread,
        "maxPaddingSpread": max_padding_spread,
        "maxGutterImbalance": max_gutter_imbalance,
        "maxCenterDelta": max_center_delta,
        "meetingAtMostLive": meeting_at_most_live,
        "maxWidthReached": max_width_reached,
        "results": results,
        "screenshots": screenshots,
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
            screenshot_dir = Path(args.evidence_dir).resolve() if args.evidence_dir else None
            dark_boot_check = await exercise_dark_boot_shell(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
                screenshot_dir=screenshot_dir,
            )
            routes = [route for route in args.routes if route in ROUTE_EXPECTATIONS]
            scenarios = []
            command_palette_check: dict[str, Any] | None = None
            transcript_detail_actions_check: dict[str, Any] | None = None
            transcript_cancel_check: dict[str, Any] | None = None
            rapid_theme_change_check: dict[str, Any] | None = None
            fast_tab_switch_check: dict[str, Any] | None = None
            desktop_page_shell_layouts_check: dict[str, Any] | None = None
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
                if screenshot_dir and route in {"/", "/youtube", "/file"}:
                    await cdp.evaluate(
                        r"""
(() => {
  window.scrollTo(0, 0);
  document.querySelectorAll('[data-app-scroll-container], aside').forEach((node) => {
    node.scrollTop = 0;
  });
  return { ok: true };
})()
""",
                        timeout=5,
                    )
                    # Capture the settled surface rather than the route-entry fade.
                    await asyncio.sleep(0.9)
                    screenshot_label = {
                        "/": "live-transcription",
                        "/youtube": "youtube-transcription",
                        "/file": "file-transcription",
                    }[route]
                    scenario["screenshot"] = await capture_page_screenshot(
                        cdp,
                        output_dir=screenshot_dir,
                        label=screenshot_label,
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
                elif route == "/meetings":
                    interaction_checks.append(
                        await exercise_meeting_end_to_end(
                            cdp,
                            frontend_base_url=frontend_base_url,
                            backend=backend,
                            timeout_sec=args.page_timeout_sec,
                            screenshot_dir=Path(args.evidence_dir).resolve() if args.evidence_dir else None,
                        )
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
                backend=backend,
                timeout_sec=args.page_timeout_sec,
            )

            transcript_cancel_check = await exercise_transcript_cancel_action(
                cdp,
                frontend_base_url=frontend_base_url,
                backend=backend,
                timeout_sec=args.page_timeout_sec,
            )

            rapid_theme_change_check = await exercise_rapid_theme_change(
                cdp,
                frontend_base_url=frontend_base_url,
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

            desktop_page_shell_layouts_check = await exercise_desktop_page_shell_layouts(
                cdp,
                frontend_base_url=frontend_base_url,
                timeout_sec=args.page_timeout_sec,
                screenshot_dir=screenshot_dir,
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
        and bool(dark_boot_check and dark_boot_check.get("ok"))
        and bool(command_palette_check and command_palette_check.get("ok"))
        and bool(transcript_detail_actions_check and transcript_detail_actions_check.get("ok"))
        and bool(transcript_cancel_check and transcript_cancel_check.get("ok"))
        and bool(rapid_theme_change_check and rapid_theme_change_check.get("ok"))
        and (fast_tab_switch_check is None or bool(fast_tab_switch_check.get("ok")))
        and bool(desktop_page_shell_layouts_check and desktop_page_shell_layouts_check.get("ok"))
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
    if dark_boot_check:
        interaction_checks.append(dark_boot_check)
    if token_required_check:
        interaction_checks.append(token_required_check)
    if command_palette_check:
        interaction_checks.append(command_palette_check)
    if transcript_detail_actions_check:
        interaction_checks.append(transcript_detail_actions_check)
    if transcript_cancel_check:
        interaction_checks.append(transcript_cancel_check)
    if rapid_theme_change_check:
        interaction_checks.append(rapid_theme_change_check)
    if fast_tab_switch_check:
        interaction_checks.append(fast_tab_switch_check)
    if desktop_page_shell_layouts_check:
        interaction_checks.append(desktop_page_shell_layouts_check)
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
        "darkBootCheck": dark_boot_check,
        "commandPaletteCheck": command_palette_check,
        "transcriptDetailActionsCheck": transcript_detail_actions_check,
        "transcriptCancelCheck": transcript_cancel_check,
        "rapidThemeChangeCheck": rapid_theme_change_check,
        "fastTabSwitchCheck": fast_tab_switch_check,
        "desktopPageShellLayoutsCheck": desktop_page_shell_layouts_check,
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
    dark_boot_check = {
        "name": "dark-boot-shell",
        "ok": True,
        "state": {"darkClass": True, "darkLogoVisible": True, "lightLogoHidden": True},
        "screenshot": None,
        "validateOnly": True,
    }
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
                {"name": "meeting-end-to-end", "ok": True}
            ] if route == "/meetings" else [
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
    desktop_page_shell_layouts_check = {
        "name": "desktop-page-shell-layouts",
        "ok": True,
        "viewport": {"width": 2048, "height": 1252, "deviceScaleFactor": 1},
        "routes": [route for route, _shell_id in PRIMARY_TAB_SHELLS],
        "routeCount": len(PRIMARY_TAB_SHELLS),
        "maxWidthSpread": 0,
        "maxContentWidthSpread": 0,
        "maxPaddingSpread": 0,
        "maxGutterImbalance": 0,
        "maxCenterDelta": 0,
        "meetingAtMostLive": True,
        "maxWidthReached": True,
        "results": [
            {
                "route": route,
                "shellId": shell_id,
                "ok": True,
                "viewportWidth": 2048,
                "viewportHeight": 1252,
                "rectWidth": 1320,
                "contentWidth": 1272,
                "paddingLeft": 24,
                "paddingRight": 24,
                "computedMaxWidth": 1320,
                "maxWidthReached": True,
                "containerClientWidth": 1768,
                "availableSlack": 448,
                "leftGutter": 224,
                "rightGutter": 224,
                "gutterImbalance": 0,
                "centerDelta": 0,
                "desktopSidebarDisplay": "flex",
                "validateOnly": True,
            }
            for route, shell_id in PRIMARY_TAB_SHELLS
        ],
        "screenshots": [],
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
                + 8
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
                "rapid-theme-change",
                *(["fast-tab-switch"] if fast_tab_switch_check else []),
                "desktop-page-shell-layouts",
                "mobile-navigation",
                "mobile-route-layouts",
                "token-required-browser-state",
            ],
            "validateOnly": True,
        },
        "scenarios": scenarios,
        "darkBootCheck": dark_boot_check,
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
        "rapidThemeChangeCheck": {
            "name": "rapid-theme-change",
            "ok": True,
            "validateOnly": True,
        },
        "fastTabSwitchCheck": fast_tab_switch_check,
        "desktopPageShellLayoutsCheck": desktop_page_shell_layouts_check,
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
