"""Measure main-thread work of synthetic typing and streaming in a built frontend.

Serves an already built ``Frontend/dist/public`` (``vite build --outDir ...``)
against the synthetic smoke backend and drives one interaction in headless
Chromium. Timing runs report in-page elapsed time per event (dispatch through
handlers, React commit and the following task: input/event processing, not
time to displayed pixels) and CDP ``ScriptDuration`` per event (main-thread
script CPU over the measurement window). One separate instrumented run, never
mixed with timing, reports React commits plus committed fiber changes.
Compare two builds of different revisions with identical arguments. Timings
are informational CPU measurements, not installed WebView2 latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.measure_history_scroll_baseline import (  # noqa: E402
    connect_to_browser,
    find_free_port,
    resolve_browser_path,
    start_browser,
    terminate_process,
)
from scripts.smoke_frontend_browser import FrontendSmokeBackend  # noqa: E402

# A minimal DevTools hook: React reports each commit, and committed fibers whose
# memoizedProps/memoizedState identity changed since the previous commit are
# counted. This approximates committed fiber changes; it is not an exact count
# of component function invocations or of DOM mutations.
FIBER_CHANGE_COUNTER = r"""
(() => {
  const stats = { commits: 0, componentFiberChanges: 0, hostFiberChanges: 0 };
  const seen = new WeakMap();
  const componentTags = new Set([0, 1, 11, 14, 15]);
  window.__scriberCountFiberChanges = false;
  window.__scriberFiberStats = stats;
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    supportsFiber: true,
    renderers: new Map(),
    inject(renderer) { const id = this.renderers.size + 1; this.renderers.set(id, renderer); return id; },
    onScheduleFiberRoot() {},
    onCommitFiberUnmount() {},
    onPostCommitFiberRoot() {},
    checkDCE() {},
    onCommitFiberRoot(_id, root) {
      const counting = window.__scriberCountFiberChanges;
      if (counting) stats.commits += 1;
      const stack = [root.current];
      while (stack.length) {
        const fiber = stack.pop();
        const previous = seen.get(fiber) || (fiber.alternate && seen.get(fiber.alternate));
        if (counting && (!previous || previous.p !== fiber.memoizedProps || previous.s !== fiber.memoizedState)) {
          if (componentTags.has(fiber.tag)) stats.componentFiberChanges += 1;
          else if (fiber.tag === 5) stats.hostFiberChanges += 1;
        }
        const record = { p: fiber.memoizedProps, s: fiber.memoizedState };
        seen.set(fiber, record);
        if (fiber.alternate) seen.set(fiber.alternate, record);
        if (fiber.sibling) stack.push(fiber.sibling);
        if (fiber.child) stack.push(fiber.child);
      }
    },
  };
})();
"""

SOCKET_CAPTURE = r"""
(() => {
  const NativeWebSocket = window.WebSocket;
  window.__scriberSockets = [];
  window.WebSocket = class extends NativeWebSocket {
    constructor(...args) { super(...args); window.__scriberSockets.push(this); }
  };
})();
"""

INTERACTION = r"""
(async ({ scenario, events, selector, placeholder }) => {
  const tick = () => new Promise((resolve) => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => resolve();
    channel.port2.postMessage(0);
  });
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
  const samples = [];
  if (scenario === "live-mic-interim") {
    const socket = window.__scriberSockets.filter((ws) => ws.readyState === 1).at(-1);
    const deliver = (payload) => socket.onmessage(new MessageEvent("message", { data: JSON.stringify(payload) }));
    deliver({ apiVersion: "1", type: "session_started", sessionId: "benchmark", session: {} });
    deliver({ apiVersion: "1", type: "status", sessionId: "benchmark", status: "Listening", listening: true, recordingState: "recording" });
    await tick(); await frame();
    window.__scriberCountFiberChanges = true;
    let text = "";
    for (let index = 0; index < events; index += 1) {
      text += ` word${index % 10}`;
      const started = performance.now();
      deliver({ apiVersion: "1", type: "transcript", sessionId: "benchmark", text: text.trim(), isFinal: false });
      await tick(); await tick();
      samples.push(performance.now() - started);
      await frame();
    }
  } else {
    const field = selector
      ? document.querySelector(selector)
      : [...document.querySelectorAll("textarea,input")].find((node) => (node.placeholder || "").startsWith(placeholder));
    field.scrollIntoView({ block: "center" });
    field.focus();
    const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setValue = Object.getOwnPropertyDescriptor(prototype, "value").set;
    await frame();
    window.__scriberCountFiberChanges = true;
    for (let index = 0; index < events; index += 1) {
      const started = performance.now();
      setValue.call(field, field.value + String.fromCharCode(97 + (index % 26)));
      field.dispatchEvent(new Event("input", { bubbles: true }));
      await tick();
      samples.push(performance.now() - started);
      await frame();
    }
  }
  window.__scriberCountFiberChanges = false;
  return { samples, stats: window.__scriberFiberStats || null };
})
"""

SCENARIOS: dict[str, dict[str, str]] = {
    "settings-vocabulary": {"route": "/settings", "placeholder": "Enter terms, one per line"},
    "settings-summary-prompt": {"route": "/settings", "placeholder": "Summarize the key points"},
    "settings-custom-model": {"route": "/settings", "placeholder": "author/model"},
    "live-mic-interim": {"route": "/"},
    "live-mic-history-search": {"route": "/", "selector": 'input[aria-label="Search recording history"]'},
    "file-history-search": {"route": "/file", "selector": 'input[aria-label="Search file transcript history"]'},
    "youtube-history-search": {
        "route": "/youtube",
        "selector": 'input[aria-label="Search YouTube transcript history"]',
    },
    "meeting-notes": {"route": "/meetings/meeting-smoke-1", "selector": '[data-testid="meeting-workspace-note"]'},
    "meeting-chat": {
        "route": "/meetings/meeting-smoke-1",
        "click": '[data-testid="meeting-workspace-tab-chat"]',
        "selector": 'textarea[placeholder^="What did we decide"]',
    },
}


def synthetic_meeting(backend: FrontendSmokeBackend, segment_count: int) -> dict[str, Any]:
    meeting = backend._synthetic_meeting("Benchmark meeting")
    meeting["segments"] = [
        {
            "id": f"segment-{index:05d}",
            "meetingId": meeting["id"],
            "revision": "live",
            "source": "system" if index % 2 else "microphone",
            "speakerId": None,
            "speakerLabel": "Speaker 1" if index % 2 else "You",
            "startMs": index * 4_000,
            "endMs": index * 4_000 + 3_500,
            "durationMs": 3_500,
            "text": f"Synthetic sentence {index} with ordinary words for layout.",
            "confidence": None,
            "alignmentQuality": "provider_segment",
            "isFinal": True,
            "sequence": index,
            "createdAt": meeting["createdAt"],
            "editVersion": 0,
            "editedAt": None,
        }
        for index in range(segment_count)
    ]
    return meeting


async def serve_dist(dist: Path, port: int) -> web.AppRunner:
    index = (dist / "index.html").read_bytes()

    async def handle(request: web.Request) -> web.StreamResponse:
        candidate = (dist / request.path.lstrip("/")).resolve()
        if candidate.is_file() and dist in candidate.parents:
            return web.FileResponse(candidate)
        return web.Response(body=index, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def wait_until(cdp: Any, expression: str, timeout_sec: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        if await cdp.evaluate(expression):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {expression}")


async def script_seconds(cdp: Any) -> float:
    metrics = (await cdp.call("Performance.getMetrics")).get("metrics", [])
    return next((float(item["value"]) for item in metrics if item["name"] == "ScriptDuration"), 0.0)


async def run_once(args: argparse.Namespace, *, count_fiber_changes: bool) -> dict[str, Any]:
    scenario = SCENARIOS[args.scenario]
    backend = FrontendSmokeBackend(port=find_free_port(), item_count=args.items)
    if args.scenario.startswith("meeting-"):
        backend.meeting = synthetic_meeting(backend, args.segments)
        # Notes are typed during a live Meeting; questions are asked afterwards.
        backend.meeting["state"] = "recording" if args.scenario == "meeting-notes" else "ready"
    await backend.start()
    frontend_port = find_free_port()
    static = await serve_dist(Path(args.dist).resolve(), frontend_port)
    debug_port = find_free_port()
    browser = None
    cdp = None
    with tempfile.TemporaryDirectory(prefix="scriber-interaction-", ignore_cleanup_errors=True) as profile:
        try:
            browser = start_browser(resolve_browser_path(args.browser), debug_port, Path(profile), headed=False)
            cdp = await connect_to_browser(debug_port)
            await cdp.call("Performance.enable", {"timeDomain": "threadTicks"})
            preload = f"window.__SCRIBER_BACKEND_URL__ = {json.dumps(backend.base_url)};" + SOCKET_CAPTURE
            if count_fiber_changes:
                preload += FIBER_CHANGE_COUNTER
            await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": preload})
            await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{frontend_port}{scenario['route']}"})
            if "click" in scenario:
                target = f"document.querySelector({json.dumps(scenario['click'])})"
                await wait_until(cdp, f"(() => Boolean({target}))()")
                await cdp.evaluate(f"(() => {{ {target}.click(); return true; }})()")
            if args.scenario == "live-mic-interim":
                ready = "document.querySelector('.perf-scroll-item') && window.__scriberSockets.some((ws) => ws.readyState === 1)"
            elif "selector" in scenario:
                ready = f"document.querySelector({json.dumps(scenario['selector'])})"
            else:
                ready = (
                    "[...document.querySelectorAll('textarea,input')]"
                    f".some((node) => (node.placeholder || '').startsWith({json.dumps(scenario['placeholder'])}))"
                )
            await wait_until(cdp, f"(() => Boolean({ready}))()")
            await asyncio.sleep(args.settle_sec)
            before = await script_seconds(cdp)
            options = {
                "scenario": args.scenario,
                "events": args.events,
                "selector": scenario.get("selector", ""),
                "placeholder": scenario.get("placeholder", ""),
            }
            result = await cdp.evaluate(f"{INTERACTION}({json.dumps(options)})", timeout=300)
            after = await script_seconds(cdp)
        finally:
            if cdp is not None:
                await cdp.close()
            if browser is not None:
                terminate_process(browser)
            await static.cleanup()
            await backend.close()
    samples = sorted(result["samples"])
    run: dict[str, Any] = {
        "medianMs": round(statistics.median(samples), 3),
        "p95Ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
        "scriptMsPerEvent": round((after - before) * 1000 / args.events, 3),
    }
    if count_fiber_changes and result["stats"]:
        stats = result["stats"]
        run = {
            "commitsPerEvent": round(stats["commits"] / args.events, 2),
            "componentFiberChangesPerEvent": round(stats["componentFiberChanges"] / args.events, 2),
            "hostFiberChangesPerEvent": round(stats["hostFiberChanges"] / args.events, 2),
        }
    return run


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    runs = [await run_once(args, count_fiber_changes=False) for _ in range(args.runs)]
    return {
        "scenario": args.scenario,
        "dist": str(Path(args.dist).resolve()),
        "eventsPerRun": args.events,
        "runs": runs,
        "medianOfRunMediansMs": round(statistics.median(run["medianMs"] for run in runs), 3),
        "medianScriptMsPerEvent": round(statistics.median(run["scriptMsPerEvent"] for run in runs), 3),
        "fiberChanges": await run_once(args, count_fiber_changes=True),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, help="Built frontend directory containing index.html")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="settings-vocabulary")
    parser.add_argument("--browser", default="")
    parser.add_argument("--events", type=int, default=60)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--items", type=int, default=240)
    parser.add_argument("--segments", type=int, default=400)
    parser.add_argument("--settle-sec", type=float, default=1.5)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = asyncio.run(benchmark(args))
    text = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
