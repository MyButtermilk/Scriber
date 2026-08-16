"""Real-browser File upload smoke against the production aiohttp composition.

Unlike ``smoke_frontend_browser.py`` this narrow vertical slice does not use
``FrontendSmokeBackend``. It runs the React/Vite page in Chrome, posts through
``src.web_api.create_app``, and verifies the exact durable JobStore row. The
provider worker is deliberately held at the queued boundary: this is ingest
E2E evidence, not external-provider or installed-Tauri evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_history_scroll_baseline import (  # noqa: E402
    CdpClient,
    connect_to_browser,
    find_free_port,
    resolve_browser_path,
    start_browser,
    start_vite,
    wait_http,
)
from scripts.smoke_frontend_browser import (  # noqa: E402
    install_page_error_capture,
    set_file_input_files,
    terminate_process_tree,
    wait_for_interaction_state,
)
from src import web_api  # noqa: E402
from src.data.job_store import JobStore  # noqa: E402


def _write_fixture(path: Path) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 1_600)


async def _start_real_backend(
    *,
    port: int,
    data_root: Path,
) -> tuple[web.AppRunner, web_api.ScriberWebController, JobStore]:
    os.environ["SCRIBER_DATA_DIR"] = str(data_root)
    web_api.db._close_all_connections()
    web_api.db._DB_PATH = data_root / "transcripts.db"
    store = JobStore(db_path=data_root / "jobs.db")
    controller = web_api.ScriberWebController(asyncio.get_running_loop(), job_store=store)
    controller._downloads_dir = data_root / "downloads"
    controller._select_available_provider = lambda: "assemblyai"
    controller._schedule_file_job = lambda *_args, **_kwargs: None
    web_api._validate_provider_ready = lambda _provider: None
    web_api._probe_media_duration_seconds = lambda _path: 0.1

    runner = web.AppRunner(web_api.create_app(controller))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, controller, store


async def _probe_real_websocket(
    cdp: CdpClient,
    *,
    backend_url: str,
    timeout_sec: float,
) -> dict[str, Any]:
    websocket_url = backend_url.replace("http://", "ws://", 1) + "/ws"
    result = await cdp.evaluate(
        f"""
new Promise((resolve) => {{
  const socket = new WebSocket({json.dumps(websocket_url)});
  let initialType = "";
  const finish = (value) => {{
    clearTimeout(timer);
    socket.close();
    resolve(value);
  }};
  const timer = setTimeout(
    () => finish({{ ok: false, initialType, pong: false, error: "timeout" }}),
    5000,
  );
  socket.onerror = () => finish({{ ok: false, initialType, pong: false, error: "socket" }});
  socket.onmessage = (event) => {{
    if (!initialType) {{
      try {{
        initialType = JSON.parse(event.data).type || "";
      }} catch (_error) {{
        finish({{ ok: false, initialType: "", pong: false, error: "initial-json" }});
        return;
      }}
      socket.send("ping");
      return;
    }}
    finish({{
      ok: initialType === "state" && event.data === "pong",
      initialType,
      pong: event.data === "pong",
      error: "",
    }});
  }};
}})
""",
        timeout=timeout_sec,
    )
    return dict(result) if isinstance(result, dict) else {"ok": False, "error": "invalid-result"}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    backend_port = find_free_port()
    frontend_port = find_free_port()
    debug_port = find_free_port()
    vite = None
    browser = None
    cdp: CdpClient | None = None
    runner: web.AppRunner | None = None

    with tempfile.TemporaryDirectory(prefix="scriber-real-file-browser-", ignore_cleanup_errors=True) as temp:
        temp_root = Path(temp)
        data_root = temp_root / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        fixture = temp_root / "real-browser.wav"
        profile = temp_root / "browser-profile"
        profile.mkdir()
        _write_fixture(fixture)

        runner, controller, store = await _start_real_backend(port=backend_port, data_root=data_root)
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}"
        try:
            vite = start_vite(frontend_port, backend_url)
            wait_http(f"{frontend_url}/", timeout_sec=args.startup_timeout_sec)
            browser = start_browser(
                resolve_browser_path(args.browser),
                debug_port,
                profile,
                headed=args.headed,
            )
            cdp = await connect_to_browser(debug_port)
            await install_page_error_capture(cdp)
            await cdp.call("Page.navigate", {"url": f"{frontend_url}/file"}, timeout=10)
            await wait_for_interaction_state(
                cdp,
                label="real-file-page",
                timeout_sec=args.page_timeout_sec,
                expression=r"""
(() => ({
  ok: !!document.querySelector('input[type="file"]')
    && (document.body?.innerText || '').includes('File transcription')
}))()
""",
            )
            websocket_state = await _probe_real_websocket(
                cdp,
                backend_url=backend_url,
                timeout_sec=args.page_timeout_sec,
            )
            await set_file_input_files(
                cdp,
                label="real-file-input",
                selector='input[type="file"]',
                files=[fixture],
                timeout_sec=args.page_timeout_sec,
            )
            browser_state = await wait_for_interaction_state(
                cdp,
                label="real-file-queued",
                timeout_sec=args.page_timeout_sec,
                expression=r"""
(() => {
  const text = document.body?.innerText || '';
  const smoke = window.__scriberSmoke || {};
  return {
    ok: window.location.pathname.startsWith('/transcript/')
      && text.includes('real-browser.wav')
      && text.includes('Queued'),
    route: window.location.pathname,
    hasTitle: text.includes('real-browser.wav'),
    hasQueuedState: text.includes('Queued'),
    consoleErrors: smoke.consoleErrors || [],
    pageErrors: smoke.pageErrors || [],
    unhandledRejections: smoke.unhandledRejections || []
  };
})()
""",
            )

            transcript_id = str(browser_state["route"]).rsplit("/", 1)[-1]
            job = store.get(transcript_id)
            source_path = Path(str(job.payload.get("path") or "")) if job is not None else Path()
            route_handler_module = next(
                (
                    route.handler.__module__
                    for route in runner.app.router.routes()
                    if route.method == "POST" and route.resource.canonical == "/api/file/transcribe"
                ),
                "",
            )
            websocket_handler_module = next(
                (
                    route.handler.__module__
                    for route in runner.app.router.routes()
                    if route.method == "GET" and route.resource.canonical == "/ws"
                ),
                "",
            )
            result = {
                "schemaVersion": 1,
                "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ok": bool(
                    job is not None
                    and job.id == transcript_id
                    and job.transcript_id == transcript_id
                    and job.payload.get("executionRoute", {}).get("provider") == "assemblyai"
                    and source_path.is_file()
                    and browser_state.get("ok") is True
                    and route_handler_module == "src.api.file_transcription_routes"
                    and websocket_state.get("ok") is True
                    and websocket_handler_module == "src.api.websocket_routes"
                    and browser_state.get("consoleErrors") == []
                    and browser_state.get("pageErrors") == []
                    and browser_state.get("unhandledRejections") == []
                ),
                "boundary": {
                    "realReactViteChrome": True,
                    "realPythonCreateApp": True,
                    "realFileRoute": True,
                    "realJobStore": True,
                    "realWebSocketRoute": True,
                    "providerWorkerHeldAtQueuedBoundary": True,
                    "installedTauri": False,
                    "externalProvider": False,
                },
                "browser": browser_state,
                "websocket": websocket_state,
                "durableJob": {
                    "idMatchesTranscript": bool(job and job.id == transcript_id == job.transcript_id),
                    "status": job.status.value if job else "missing",
                    "provider": job.payload.get("executionRoute", {}).get("provider") if job else "",
                    "sourceExists": source_path.is_file(),
                },
                "routeHandlerModule": route_handler_module,
                "websocketHandlerModule": websocket_handler_module,
                "controllerType": type(controller).__name__,
            }
        finally:
            if cdp is not None:
                with suppress(Exception):
                    await cdp.call("Page.navigate", {"url": "about:blank"}, timeout=2)
                await cdp.close()
            if browser is not None:
                terminate_process_tree(browser)
            if vite is not None:
                terminate_process_tree(vite)
            if runner is not None:
                await runner.cleanup()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--startup-timeout-sec", type=float, default=30.0)
    parser.add_argument("--page-timeout-sec", type=float, default=30.0)
    parser.add_argument("--output", default="tmp/real-file-browser-smoke.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = asyncio.run(run(args))
    output = json.dumps(result, indent=2, ensure_ascii=False)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
