"""Local ONNX model management routes.

Second domain lifted out of ``web_api.create_app``, following the shape of
:mod:`src.api.runtime_routes`. ``src.onnx_stt`` stays behind function-local
imports so listing models never drags onnxruntime into a process that only
serves cloud providers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from loguru import logger

from src.api.controller_port import OnnxControllerPort
from src.config import Config


@dataclass
class OnnxRoutesService:
    """Dependencies the ONNX domain needs from the surrounding app."""

    controller: OnnxControllerPort
    # Download progress is broadcast from the worker thread through
    # call_soon_threadsafe. asyncio keeps only a weak reference to the tasks
    # that creates, so they are held here until they finish.
    broadcast_tasks: set[asyncio.Task] = field(default_factory=set)


APP_ONNX_SERVICE: web.AppKey[OnnxRoutesService] = web.AppKey(
    "onnx_routes_service",
    OnnxRoutesService,
)


def _service(request: web.Request) -> OnnxRoutesService:
    return request.app[APP_ONNX_SERVICE]


async def list_models(request: web.Request) -> web.Response:
    """List available ONNX models with download status."""
    try:

        def _load_onnx_models() -> dict[str, Any]:
            from src.onnx_stt import is_onnx_available, list_available_models

            if not is_onnx_available():
                return {
                    "available": False,
                    "message": "onnx-asr library not installed. Run: pip install onnx-asr[cpu,hub]",
                    "models": [],
                }

            models = list_available_models(quantization=Config.ONNX_QUANTIZATION)
            return {
                "available": True,
                "models": models,
                "currentModel": Config.ONNX_MODEL,
                "quantization": Config.ONNX_QUANTIZATION,
            }

        payload = await asyncio.to_thread(_load_onnx_models)
        return web.json_response(payload)
    except ImportError as e:
        return web.json_response(
            {
                "available": False,
                "message": str(e),
                "models": [],
            }
        )
    except Exception as e:
        logger.exception("Failed to list ONNX models")
        return web.json_response({"message": str(e)}, status=500)


async def model_status(request: web.Request) -> web.Response:
    """Get status of a specific ONNX model."""
    model_id = request.match_info.get("model_id", "")
    if not model_id:
        return web.json_response({"message": "Missing model ID"}, status=400)
    quantization = request.query.get("quantization") or Config.ONNX_QUANTIZATION

    try:

        def load_status() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            from src.onnx_stt import get_model_info, get_model_status

            info = get_model_info(model_id)
            if not info:
                return None, None
            return info, get_model_status(model_id, quantization=quantization)

        info, status = await asyncio.to_thread(load_status)
        if not info:
            return web.json_response({"message": "Unknown model"}, status=404)
        assert status is not None

        return web.json_response(
            {
                "id": model_id,
                "name": info["name"],
                "description": info["description"],
                "languages": info["languages"],
                "runtime": info.get("runtime", "onnx_asr"),
                "hfRepo": info.get("hf_repo", ""),
                "hfRepoByQuantization": info.get("hf_repo_by_quantization", {}),
                "localDirName": info.get("local_dir_name", ""),
                "sizeMb": info["size_mb"],
                "sizeMbByQuantization": info.get("size_mb_by_quantization", {}),
                "supportedQuantizations": info.get("supported_quantizations", ["int8", "fp32"]),
                "downloaded": status["downloaded"],
                "status": status["status"],
                "progress": status["progress"],
                "message": status["message"],
            }
        )
    except Exception as e:
        return web.json_response({"message": str(e)}, status=500)


async def download_model_route(request: web.Request) -> web.Response:
    """Download an ONNX model from Hugging Face."""
    service = _service(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    model_id = body.get("modelId", "")
    quantization = body.get("quantization") or Config.ONNX_QUANTIZATION
    if not model_id:
        return web.json_response({"message": "Missing modelId"}, status=400)

    try:
        from src.onnx_stt import download_model, get_model_status

        def download_preflight() -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
            from src.onnx_stt import get_model_info, is_model_downloading

            info = get_model_info(model_id)
            if not info:
                return None, None, False
            status = get_model_status(model_id, quantization=quantization)
            return info, status, is_model_downloading(model_id)

        info, status, downloading = await asyncio.to_thread(download_preflight)
        if not info:
            return web.json_response({"message": "Unknown model"}, status=404)

        assert status is not None
        if status.get("downloaded"):
            return web.json_response(
                {
                    "success": True,
                    "message": "Model already downloaded",
                    "modelId": model_id,
                }
            )

        if downloading:
            return web.json_response(
                {
                    "success": False,
                    "message": "Download already in progress",
                    "modelId": model_id,
                },
                status=409,
            )

        controller = service.controller
        loop = asyncio.get_running_loop()

        def on_progress(progress: float, message: str) -> None:
            status_value = "downloading"
            if progress < 0:
                status_value = "error"
            elif progress >= 100:
                status_value = "ready"

            payload = {
                "type": "onnx_download_progress",
                "modelId": model_id,
                "quantization": quantization,
                "progress": progress,
                "status": status_value,
                "message": message,
            }

            def schedule_broadcast() -> None:
                task = asyncio.ensure_future(controller.broadcast(payload))
                service.broadcast_tasks.add(task)
                task.add_done_callback(service.broadcast_tasks.discard)

            loop.call_soon_threadsafe(schedule_broadcast)

        logger.info(f"Starting ONNX model download: {model_id}")
        success = await download_model(model_id, quantization=quantization, on_progress=on_progress)

        final_status = await asyncio.to_thread(
            get_model_status,
            model_id,
            quantization=quantization,
        )
        await controller.broadcast(
            {
                "type": "onnx_download_progress",
                "modelId": model_id,
                "quantization": quantization,
                "progress": final_status.get("progress", 0.0),
                "status": final_status.get("status", "error" if not success else "ready"),
                "message": final_status.get("message", ""),
            }
        )

        if success:
            return web.json_response(
                {
                    "success": True,
                    "message": "Model downloaded successfully",
                    "modelId": model_id,
                    "quantization": quantization,
                }
            )
        return web.json_response(
            {
                "success": False,
                "message": "Download failed",
                "modelId": model_id,
                "quantization": quantization,
            },
            status=500,
        )

    except ValueError as e:
        return web.json_response({"message": str(e)}, status=400)
    except Exception as e:
        logger.exception(f"Failed to download model {model_id}")
        return web.json_response({"message": str(e)}, status=500)


async def delete_model_route(request: web.Request) -> web.Response:
    """Delete a downloaded ONNX model from cache."""
    model_id = request.match_info.get("model_id", "")
    if not model_id:
        return web.json_response({"message": "Missing model ID"}, status=400)
    quantization = request.query.get("quantization") or Config.ONNX_QUANTIZATION

    try:

        def delete_local_model() -> tuple[str, bool]:
            from src.onnx_stt import delete_model, get_model_info, is_model_downloading

            info = get_model_info(model_id)
            if not info:
                return "unknown", False
            if is_model_downloading(model_id):
                return "downloading", False
            return "deleted", delete_model(model_id, quantization=quantization)

        delete_state, success = await asyncio.to_thread(delete_local_model)
        if delete_state == "unknown":
            return web.json_response({"message": "Unknown model"}, status=404)
        if delete_state == "downloading":
            return web.json_response(
                {"message": "Cannot delete a model while it is downloading"},
                status=409,
            )

        if success:
            logger.info(f"Deleted ONNX model: {model_id}")
            await _service(request).controller.broadcast(
                {
                    "type": "onnx_models_updated",
                    "modelId": model_id,
                }
            )
            return web.json_response(
                {
                    "success": True,
                    "message": "Model deleted",
                    "modelId": model_id,
                }
            )
        else:
            return web.json_response(
                {
                    "success": False,
                    "message": "Model not found in cache",
                    "modelId": model_id,
                },
                status=404,
            )

    except ValueError as e:
        return web.json_response({"message": str(e)}, status=400)
    except Exception as e:
        logger.exception(f"Failed to delete model {model_id}")
        return web.json_response({"message": str(e)}, status=500)


def register_onnx_routes(app: web.Application, *, controller: OnnxControllerPort) -> None:
    """Register the ONNX model domain without web_api closure coupling."""

    app[APP_ONNX_SERVICE] = OnnxRoutesService(controller=controller)

    app.router.add_get("/api/onnx/models", list_models)
    app.router.add_get("/api/onnx/models/{model_id}", model_status)
    app.router.add_post("/api/onnx/download", download_model_route)
    app.router.add_delete("/api/onnx/models/{model_id}", delete_model_route)
