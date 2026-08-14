"""Structural contracts for what route modules need from the controller.

Route modules cannot annotate the real ``ScriberWebController``: it lives in
``web_api``, which imports them. Protocols solve that without a cycle, and
structurally — ``ScriberWebController`` satisfies these by having the methods,
with nothing to register and no base class to inherit.

Each domain declares only the slice it uses, so a handler's dependencies are
readable from its own module and a test double only has to implement that
slice. Keep these narrow: a port that grows to mirror the whole controller has
stopped being a contract.
"""

from __future__ import annotations

from typing import Any, Protocol

from aiohttp import web


class BroadcastPort(Protocol):
    """Push an event to every connected WebSocket client."""

    async def broadcast(self, payload: dict[str, Any]) -> None: ...


class RuntimeControllerPort(BroadcastPort, Protocol):
    """State, diagnostics, and frontend telemetry for the runtime routes."""

    def get_health(self) -> dict[str, Any]: ...

    def get_state(self) -> dict[str, Any]: ...

    def get_runtime_info(self) -> dict[str, Any]: ...

    def get_frontend_ready(self) -> dict[str, Any]: ...

    def record_frontend_ready(self, payload: dict[str, Any], request: web.Request) -> dict[str, Any]: ...

    def get_frontend_performance(
        self,
        *,
        after_sequence: int | None = None,
        source_instance_id: str | None = None,
    ) -> dict[str, Any]: ...

    def record_frontend_performance(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def request_frontend_performance_flush(self, source_instance_id: str) -> dict[str, Any] | None: ...

    def get_audio_diagnostics(self) -> dict[str, Any]: ...

    def get_post_processing_diagnostics(self, *, limit: int = 20) -> dict[str, Any]: ...

    def get_hot_path_metrics(self, *, limit: int = 50, include_active: bool = False) -> dict[str, Any]: ...


class OnnxControllerPort(BroadcastPort, Protocol):
    """The ONNX domain only ever announces model and download state."""


class PublicRecordPort(Protocol):
    """A transcript record as the REST layer serialises it."""

    def to_public(self, *, include_content: bool) -> dict[str, Any]: ...


class YoutubeControllerPort(Protocol):
    """Scheduling a YouTube transcription is the domain's only controller call."""

    async def start_youtube_transcription(self, payload: dict[str, Any]) -> PublicRecordPort: ...
