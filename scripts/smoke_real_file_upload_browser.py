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
import subprocess
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
    browser_candidates,
    connect_to_browser,
    find_free_port,
    resolve_browser_path,
    start_browser,
    start_vite,
    wait_http,
)
from scripts.smoke_frontend_browser import (  # noqa: E402
    capture_page_screenshot,
    install_page_error_capture,
    set_file_input_files,
    terminate_process_tree,
    wait_for_interaction_state,
)
from src import web_api  # noqa: E402
from src.data.job_store import JobStore  # noqa: E402


async def _exercise_file_recovery(
    cdp: CdpClient, *, frontend_url: str, store: JobStore, fixture: Path, args: argparse.Namespace
) -> dict[str, Any]:
    failed_id = "a" * 32
    pending_title = "Die steuerliche Bewertung von Immobilien für Erbschaft- und Schenkungsteuer"
    failed_title = "Die Beendigung von Arbeitsverhältnissen"
    summary_title = "Frühere Zusammenfassung bleibt erhalten"
    for transcript_id, title, status, summary_status, summary in (
        ("b" * 32, pending_title, "completed", "pending", ""),
        (failed_id, failed_title, "failed", "idle", ""),
        ("c" * 32, summary_title, "completed", "failed", "An earlier summary."),
    ):
        await asyncio.to_thread(
            web_api.db.save_transcript,
            {
                "id": transcript_id,
                "title": title,
                "date": "2026-10-02",
                "duration": "2:17:33",
                "type": "file",
                "status": status,
                "summaryStatus": summary_status,
                "summary": summary,
                "content": "[Error] Connection failed" if status == "failed" else "Synthetic transcript text.",
            },
        )
    await cdp.evaluate("localStorage.setItem('scriber-ui-locale', 'de')")
    await cdp.call("Page.navigate", {"url": f"{frontend_url}/file?view=grid"})
    await wait_for_interaction_state(
        cdp,
        label="file-recovery-actions",
        timeout_sec=args.page_timeout_sec,
        expression=f"({{ok: !!document.querySelector('[data-transcript-retry=\"{failed_id}\"]:enabled')}})",
    )
    layouts = []
    screenshots = []
    for width, dark in ((1280, False), (960, True), (390, False)):
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": 940, "deviceScaleFactor": 1, "mobile": False},
        )
        await cdp.evaluate(f"document.documentElement.classList.toggle('dark', {str(dark).lower()})")
        layout = await wait_for_interaction_state(
            cdp,
            label=f"file-recovery-layout-{width}",
            timeout_sec=args.page_timeout_sec,
            expression=f"""(() => {{
              const cards = [...document.querySelectorAll('.file-history-card')];
              const pending = cards.find(card => card.textContent.includes({json.dumps(pending_title)}));
              const failed = cards.find(card => card.textContent.includes({json.dumps(failed_title)}));
              const summary = cards.find(card => card.textContent.includes({json.dumps(summary_title)}));
              const icon = pending?.querySelector('.file-history-icon')?.getBoundingClientRect();
              const expectedIconSize = 3 * parseFloat(getComputedStyle(document.documentElement).fontSize);
              const badge = pending?.querySelector('[title]')?.getBoundingClientRect();
              const card = pending?.getBoundingClientRect();
              return {{
                ok: icon?.width === expectedIconSize && icon?.height === expectedIconSize
                  && badge?.left >= icon?.right + 10 && badge?.right <= card?.right - 15
                  && !!failed?.querySelector('[data-transcript-retry]:enabled')
                  && !!summary?.querySelector('button[title] .lucide-rotate-ccw')
                  && cards.every(card => card.scrollWidth <= card.clientWidth + 1)
                  && document.documentElement.scrollWidth <= innerWidth + 1,
                width: innerWidth, iconWidth: icon?.width, iconHeight: icon?.height,
                summaryLabel: pending?.querySelector('[title]')?.textContent?.trim(),
                horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1
              }};
            }})()""",
        )
        layouts.append(layout)
        if args.evidence_dir:
            await cdp.evaluate(
                f"""(() => {{
                  const pending = [...document.querySelectorAll('.file-history-card')]
                    .find(card => card.textContent.includes({json.dumps(pending_title)}));
                  pending?.scrollIntoView({{block: 'center', behavior: 'instant'}});
                }})()"""
            )
            # The finite history entry animation must settle before visual QA.
            await asyncio.sleep(0.35)
            screenshots.append(
                await capture_page_screenshot(
                    cdp,
                    output_dir=Path(args.evidence_dir),
                    label=f"file-recovery-{width}-{'dark' if dark else 'light'}",
                    reset_scroll=False,
                )
            )

    await cdp.call("Page.navigate", {"url": f"{frontend_url}/transcript/{failed_id}"})
    await wait_for_interaction_state(
        cdp,
        label="file-detail-retry",
        timeout_sec=args.page_timeout_sec,
        expression=f"""(() => {{
          const button = document.querySelector('[data-transcript-retry="{failed_id}"]:enabled');
          return {{ok: !!button && button.closest('.space-y-2')?.textContent.includes('Transkription fehlgeschlagen')}};
        }})()""",
    )
    if args.evidence_dir:
        await asyncio.sleep(0.35)
        screenshots.append(
            await capture_page_screenshot(
                cdp, output_dir=Path(args.evidence_dir), label="file-recovery-detail", reset_scroll=False
            )
        )
    await cdp.evaluate(f"document.querySelector('[data-transcript-retry=\"{failed_id}\"]').click()")
    await wait_for_interaction_state(
        cdp,
        label="file-retry-dialog",
        timeout_sec=args.page_timeout_sec,
        expression="({ok: !!document.querySelector('[role=dialog]')})",
    )
    await set_file_input_files(
        cdp,
        label="file-retry-original",
        selector='input[type="file"]',
        files=[fixture],
        timeout_sec=args.page_timeout_sec,
    )
    queued = await wait_for_interaction_state(
        cdp,
        label="file-retry-queued",
        timeout_sec=args.page_timeout_sec,
        expression=f"""({{
          ok: location.pathname.startsWith('/transcript/') && !location.pathname.endsWith('{failed_id}')
            && !!document.querySelector('[data-transcript-detail-header]'), route: location.pathname
        }})""",
    )
    retry_id = str(queued["route"]).rsplit("/", 1)[-1]
    retry_job = store.get(retry_id)
    original = await asyncio.to_thread(web_api.db.get_transcript, failed_id)
    source = Path(str(retry_job.payload.get("path") or "")) if retry_job else Path()
    return {
        "ok": all(layout.get("ok") for layout in layouts)
        and bool(retry_job and retry_job.transcript_id == retry_id and source.is_file())
        and bool(original and original["status"] == "failed"),
        "layouts": layouts,
        "newDurableAttempt": bool(retry_job and retry_job.transcript_id == retry_id),
        "originalFailurePreserved": bool(original and original["status"] == "failed"),
        "screenshots": screenshots,
    }


async def _start_test_browser(args: argparse.Namespace, profile_root: Path) -> tuple[subprocess.Popen[str], CdpClient]:
    primary = resolve_browser_path(args.browser)
    alternatives = [str(Path(path)) for path in browser_candidates() if Path(path).is_file() and path != primary]
    fallback = primary if args.browser or not alternatives else alternatives[0]
    for attempt, executable in enumerate((primary, fallback), start=1):
        profile = profile_root / str(attempt)
        profile.mkdir()
        port = find_free_port()
        browser = start_browser(executable, port, profile, headed=args.headed)
        try:
            cdp = await connect_to_browser(port, timeout_sec=args.startup_timeout_sec)
            return browser, cdp
        except BaseException as exc:
            print(f"Browser startup attempt {attempt}: executable={executable}, exit={browser.poll()}", file=sys.stderr)
            log_path = profile / "browser-process.log"
            if log_path.is_file():
                with log_path.open("rb") as log:
                    log.seek(max(0, log_path.stat().st_size - 8192))
                    print(log.read().decode("utf-8", errors="replace"), file=sys.stderr)
            terminate_process_tree(browser)
            # Retry startup only. Cancellation and every test assertion remain
            # terminal; the caller begins its assertions after this returns.
            if attempt == 2 or not isinstance(exc, Exception):
                raise
    raise AssertionError("Browser startup attempts exhausted")


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
    vite = None
    browser = None
    cdp: CdpClient | None = None
    runner: web.AppRunner | None = None

    with tempfile.TemporaryDirectory(prefix="scriber-real-file-browser-", ignore_cleanup_errors=True) as temp:
        temp_root = Path(temp)
        data_root = temp_root / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        fixture = temp_root / "real-browser.wav"
        second_fixture = temp_root / "second-browser.wav"
        profile = temp_root / "browser-profile"
        profile.mkdir()
        _write_fixture(fixture)
        _write_fixture(second_fixture)

        runner, controller, store = await _start_real_backend(port=backend_port, data_root=data_root)
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}"
        try:
            vite = start_vite(frontend_port, backend_url)
            wait_http(f"{frontend_url}/", timeout_sec=args.startup_timeout_sec)
            browser, cdp = await _start_test_browser(args, profile)
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
                files=[fixture, second_fixture],
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
    ok: window.location.pathname === '/file'
      && text.includes('real-browser.wav')
      && text.includes('second-browser.wav')
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

            jobs = store.list_pending()
            separate_sources = len(jobs) == 2 and len({job.payload.get("path") for job in jobs}) == 2
            await cdp.evaluate("document.querySelector('[aria-label=\"View transcript real-browser.wav\"]').click()")
            detail_state = await wait_for_interaction_state(
                cdp,
                label="real-file-detail",
                timeout_sec=args.page_timeout_sec,
                expression="({ok: location.pathname.startsWith('/transcript/'), route: location.pathname})",
            )
            # CDP sends actual browser input, rather than calling our handler.
            for event_type in ("mousePressed", "mouseReleased"):
                await cdp.call(
                    "Input.dispatchMouseEvent",
                    {"type": event_type, "x": 600, "y": 300, "button": "back", "clickCount": 1},
                )
            back_state = await wait_for_interaction_state(
                cdp,
                label="real-file-mouse-back",
                timeout_sec=args.page_timeout_sec,
                expression="({ok: location.pathname === '/file'})",
            )
            for event_type in ("mousePressed", "mouseReleased"):
                await cdp.call(
                    "Input.dispatchMouseEvent",
                    {"type": event_type, "x": 600, "y": 300, "button": "forward", "clickCount": 1},
                )
            forward_state = await wait_for_interaction_state(
                cdp,
                label="real-file-mouse-forward",
                timeout_sec=args.page_timeout_sec,
                expression=f"({{ok: location.pathname === {json.dumps(detail_state['route'])}}})",
            )

            transcript_id = str(detail_state["route"]).rsplit("/", 1)[-1]
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
            recovery = await _exercise_file_recovery(
                cdp, frontend_url=frontend_url, store=store, fixture=fixture, args=args
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
                    and recovery.get("ok") is True
                    and browser_state.get("ok") is True
                    and separate_sources
                    and back_state.get("ok") is True
                    and forward_state.get("ok") is True
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
                "fileRecovery": recovery,
                "parallelImports": {"jobCount": len(jobs), "separateSources": separate_sources},
                "mouseNavigation": {"back": back_state, "forward": forward_state},
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
    parser.add_argument("--startup-timeout-sec", type=float, default=45.0)
    parser.add_argument("--page-timeout-sec", type=float, default=30.0)
    parser.add_argument("--output", default="tmp/real-file-browser-smoke.json")
    parser.add_argument("--evidence-dir", default="")
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
