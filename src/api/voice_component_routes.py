"""Optional local voice component routes.

Tenth domain lifted out of ``web_api.create_app``.

Two optional local components live here: the speaker-recognition model behind
the Voice Library, and the local diarization component behind speaker
separation. Both are downloaded on demand, both can be deleted, and neither is
required for the app to run.

What makes this a domain rather than six status endpoints is the opt-in. Voice
Library processing is biometric, so the user's consent gates it -- and the
consent flag is durable and cross-process, which means it can be withdrawn from
another Scriber window while a download is mid-flight. Every mutation here
therefore re-checks consent after the step that could outlive it, and deletes
what it just installed if consent is gone. That is the invariant this module
exists to hold.

The enrollment routes still live in ``web_api`` because they carry the audio
admission concern, which needs its own owner first. Until they move, the two
locks that serialise voice mutations stay with composition and are handed to
this domain per request; they move here with enrollment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from loguru import logger

from src.api.app_keys import APP_HTTP_SESSION
from src.config import Config
from src.core.rest_contracts import REST_API_VERSION
from src.runtime.cancellation import await_with_delayed_cancellation
from src.runtime.support_bundle import redact_text


@dataclass(frozen=True)
class VoiceLibraryDeps:
    """What the Voice Library routes mutate, resolved per request.

    Per request rather than at registration for the same reason the durable
    Meeting import bundle is: composition supplies these as loose controller
    attributes that suites replace after the app exists, and the two locks are
    created lazily by the controller that owns them.
    """

    speaker_model: Any
    meeting_store: Any
    persist_settings: Callable[[], None]
    download_lock: asyncio.Lock
    mutation_lock: asyncio.Lock


VoiceLibraryProvider = Callable[[], VoiceLibraryDeps]
DiarizerProvider = Callable[[], Any]

APP_VOICE_LIBRARY_DEPS: web.AppKey[VoiceLibraryProvider] = web.AppKey("voice_library_deps_provider")
APP_DIARIZER: web.AppKey[DiarizerProvider] = web.AppKey("diarizer_provider")


def _deps(request: web.Request) -> VoiceLibraryDeps:
    return request.app[APP_VOICE_LIBRARY_DEPS]()


def _diarizer(request: web.Request) -> Any:
    return request.app[APP_DIARIZER]()


async def _voice_library_enabled(deps: VoiceLibraryDeps) -> bool:
    """Answer the durable, cross-process consent gate.

    ``Config`` alone is this process's view. The store's flag is the one another
    Scriber window can flip, so a store that publishes it wins over the
    in-process copy; a store that does not is simply not consulted.
    """
    if not Config.VOICEPRINT_LIBRARY_OPT_IN:
        return False
    durable = getattr(deps.meeting_store, "speaker_library_enabled", None)
    if not callable(durable):
        return True
    return bool(await asyncio.to_thread(durable))


async def _diarizer_status(diarizer: Any) -> dict[str, Any]:
    status_async = getattr(diarizer, "status_async", None)
    return await status_async() if callable(status_async) else diarizer.status()


def _diarization_payload(status: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": REST_API_VERSION,
        "enabled": bool(Config.SPEAKER_DIARIZATION_FALLBACK_ENABLED),
        **extra,
        **status,
    }


async def speaker_model_status(request: web.Request) -> web.Response:
    deps = _deps(request)
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "optedIn": bool(Config.VOICEPRINT_LIBRARY_OPT_IN),
            **deps.speaker_model.status(),
        }
    )


async def download_speaker_model(request: web.Request) -> web.Response:
    """Install the Voice Library model, honouring an opt-out at every step.

    Consent is checked three times on purpose: before starting, after staging
    but before the atomic replace, and again after it. The last check is the one
    that matters -- the replace runs in an executor that cancellation cannot
    interrupt, so another process can withdraw consent while it is in flight.
    The model is then deleted rather than left behind.
    """
    deps = _deps(request)
    if not Config.VOICEPRINT_LIBRARY_OPT_IN:
        return web.json_response(
            {"message": "Confirm the Voice Library biometric-processing opt-in first."}, status=409
        )
    if not await _voice_library_enabled(deps):
        return web.json_response(
            {"message": "Voice Library was turned off before the download started."},
            status=409,
        )
    staged = None
    try:
        async with deps.download_lock:
            staged = await deps.speaker_model.stage_download(request.app[APP_HTTP_SESSION])
            async with deps.mutation_lock:
                if not await _voice_library_enabled(deps):
                    return web.json_response(
                        {"message": "Voice Library was turned off while the local download was running."},
                        status=409,
                    )
                status, promotion_cancel = await await_with_delayed_cancellation(
                    asyncio.to_thread(deps.speaker_model.promote_staged, staged)
                )
                staged = None
                enabled_after_promotion, post_check_cancel = await await_with_delayed_cancellation(
                    _voice_library_enabled(deps)
                )
                pending_cancel = promotion_cancel or post_check_cancel
                if not enabled_after_promotion:
                    _, delete_cancel = await await_with_delayed_cancellation(
                        asyncio.to_thread(deps.speaker_model.delete)
                    )
                    pending_cancel = pending_cancel or delete_cancel
                    if pending_cancel is not None:
                        raise pending_cancel
                    return web.json_response(
                        {"message": "Voice Library was turned off while the local download was finishing."},
                        status=409,
                    )
                if pending_cancel is not None:
                    raise pending_cancel
        return web.json_response({"apiVersion": REST_API_VERSION, **status})
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=502)
    finally:
        if staged is not None:
            try:
                await asyncio.to_thread(deps.speaker_model.discard_staged, staged)
            except OSError:
                logger.warning("Voice Library staged model cleanup failed")


async def delete_speaker_library(request: web.Request) -> web.Response:
    """Erase every voiceprint, the model behind them, and the consent flag."""
    deps = _deps(request)

    async def delete_all_voice_data() -> int:
        deleted = await asyncio.to_thread(deps.meeting_store.delete_all_speaker_profiles)
        await asyncio.to_thread(deps.speaker_model.delete)
        Config.set_voiceprint_library_opt_in(False)
        deps.persist_settings()
        return deleted

    async with deps.mutation_lock:
        # Withdrawing consent has to finish even if the caller goes away: a
        # half-erased Voice Library is exactly what the user asked not to keep.
        deleted_profiles, pending_cancel = await await_with_delayed_cancellation(delete_all_voice_data())
        if pending_cancel is not None:
            raise pending_cancel
    return web.json_response({"apiVersion": REST_API_VERSION, "deleted": True, "deletedProfiles": deleted_profiles})


async def diarization_component_status(request: web.Request) -> web.Response:
    return web.json_response(_diarization_payload(await _diarizer_status(_diarizer(request))))


async def install_diarization_component(request: web.Request) -> web.Response:
    diarizer = _diarizer(request)
    try:
        status = await diarizer.install(request.app[APP_HTTP_SESSION])
    except (OSError, RuntimeError, ValueError) as exc:
        return web.json_response(
            {"message": redact_text(str(exc))[:240] or "Local diarization install failed."},
            status=502,
        )
    return web.json_response(_diarization_payload(status))


async def delete_diarization_component(request: web.Request) -> web.Response:
    diarizer = _diarizer(request)
    delete_async = getattr(diarizer, "delete_async", None)
    if callable(delete_async):
        deleted = await delete_async()
    else:
        await asyncio.to_thread(diarizer.delete)
        deleted = True
    if not deleted:
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "deleted": False,
                "message": "Local speaker separation is currently in use.",
            },
            status=409,
        )
    return web.json_response(_diarization_payload(await _diarizer_status(diarizer), deleted=True))


def register_voice_component_routes(
    app: web.Application,
    *,
    voice_library: VoiceLibraryProvider,
    diarizer: DiarizerProvider,
) -> None:
    """Register the optional voice component domain.

    Two providers rather than one bundle: the Voice Library routes and the
    diarization routes share nothing but ``Config``, and a single bundle would
    make the model status endpoint fail on a composition that never built a
    diarizer. Each route resolves only what it actually uses.
    """

    app[APP_VOICE_LIBRARY_DEPS] = voice_library
    app[APP_DIARIZER] = diarizer

    app.router.add_get("/api/meetings/speaker-model", speaker_model_status)
    app.router.add_post("/api/meetings/speaker-model", download_speaker_model)
    app.router.add_delete("/api/meetings/speaker-library", delete_speaker_library)
    app.router.add_get("/api/meetings/diarization-component", diarization_component_status)
    app.router.add_post("/api/meetings/diarization-component", install_diarization_component)
    app.router.add_delete("/api/meetings/diarization-component", delete_diarization_component)
