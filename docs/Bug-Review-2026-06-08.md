# Bug Review 2026-06-08

Last verified: 2026-06-08 after implementation

Scope: static code review plus focused runtime checks for the current hybrid Tauri/Python/React branch. The findings below have been implemented in the same branch and are kept as a traceable bug-fix record.

## Verification Snapshot

- `python -m pytest -q`: passed, 423 tests.
- `npm run check` in `Frontend/`: passed.
- `npm run build` in `Frontend/`: passed.
- `cargo test` in `Frontend/src-tauri/`: passed, 27 tests.
- `python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke-bugfixes-rerun.json`: passed, 6 routes, no critical console errors, no page errors, 4 interaction checks.
- `python scripts\analyze_backend_runtime_dependencies.py --sidecar-dir Frontend\src-tauri\target\release\backend --output tmp\bug-review-runtime-footprint.json --max-scipy-mb 0 --max-onnxruntime-mb 40 --max-total-mb 40`: passed. SciPy absent, ONNXRuntime footprint 33.75 MiB.
- `python -m pytest tests\test_web_api_jobs.py tests\test_web_api_security.py tests\test_backend_runtime_dependency_footprint.py tests\perf\test_media_preparation_smoke_script.py tests\perf\test_frontend_browser_smoke_script.py -q`: passed, 54 tests.
- `python -m py_compile src\web_api.py src\database.py tests\test_web_api_jobs.py tests\test_web_api_security.py`: passed.
- PowerShell parser check for `scripts\build_windows.ps1`: passed.

## Implementation Status

- P1 auto-summary status: fixed. `summaryStatus`, `summaryError`, and `summaryUpdatedAt` are persisted, exposed through transcript payloads, and rendered in the Transcript Detail UI with retry affordance.
- P2 token-required direct browser state: fixed. The frontend now probes a token-protected endpoint after health, shows an explicit desktop session-token message, and suppresses repeated WebSocket reconnects after that state is known.
- P2 stale build evidence: fixed. `scripts\build_windows.ps1` deletes stale media/runtime reports before gate execution and emits `{ ran, path, generatedAt }` status objects instead of unconditional paths.
- P3 file drag/drop stale closure: fixed. `uploadFile` is a dependency-correct callback and `onDrop` depends on it.
- P3 React Query `on401: "throw"` behavior: fixed. Strict mode now rethrows query failures instead of returning `null`.
- P3 frontend smoke gaps: fixed for the frontend smoke scope with concrete coverage for YouTube thumbnail search/URL flows, file drag/drop, Debug Clear/default controls, and token-required direct browser state. Installed NSIS process-window behavior remains covered by installer-specific smokes, not the frontend route smoke.
- P4 aiohttp AppKey warnings: fixed. Controller, HTTP session, and shutdown event app state now use typed `web.AppKey` values.

## Findings

### P1 - Auto-summary failures are hidden behind a completed job

Status: fixed.

Evidence:

- YouTube jobs mark transcription completed before summary generation: `src/web_api.py:2717` and `src/web_api.py:2718`.
- File jobs do the same: `src/web_api.py:3025` and `src/web_api.py:3026`.
- Auto-summary then changes only the transient `step` to `Summarizing...`: `src/web_api.py:2743` and `src/web_api.py:3050`.
- If summarization fails, the exception is logged and `step` is reset to `Completed`: `src/web_api.py:2779`, `src/web_api.py:2780`, `src/web_api.py:3086`, and `src/web_api.py:3087`.

Impact:

If Gemini/OpenAI summarization fails after transcription, the user sees a completed transcript with no explicit summary error. This is especially risky after the observed `MAX_TOKENS` truncation path: the system can look successful while the summary is missing, too short, or only partially recovered.

Recommended fix:

Add persisted summary state separate from transcription state, for example `summaryStatus: idle | pending | completed | failed`, `summaryError`, and `summaryUpdatedAt`. Expose it in transcript detail and history payloads. The UI should show a clear summary failure state with a retry action. Add backend tests for YouTube and file jobs where transcription succeeds but summary fails.

### P2 - Direct browser access to a token-protected managed backend produces a broken app state

Status: fixed.

Evidence:

- The backend serves the frontend catch-all even when a session token is required: `src/web_api.py:5096` and `src/web_api.py:5490`.
- `/api/health` is public, but most local control/API routes require the token through `session_token_middleware`: `src/web_api.py:336`, `src/web_api.py:4388`, and `src/web_api.py:4393`.
- Frontend backend status currently treats a successful `/api/health` response as online: `Frontend/client/src/hooks/use-backend-status.tsx:74`.
- The shared WebSocket opens normally and logs reconnecting errors when the token is absent: `Frontend/client/src/contexts/WebSocketContext.tsx:154` and `Frontend/client/src/contexts/WebSocketContext.tsx:201`.
- Manual Browser check: `http://127.0.0.1:8765/` loaded the Live Mic UI, while `/api/transcripts` and `/api/runtime` returned `401 Session token required`; browser console repeatedly logged `WebSocket error`.

Impact:

Opening the backend URL directly can show a half-working UI instead of an understandable desktop/runtime message. It also makes debugging harder because health says "online" while every useful API/WS call fails.

Recommended fix:

Introduce an explicit frontend auth/runtime state. If the app is not running inside Tauri and the backend requires a session token, show a compact "Open the Scriber desktop app" or "session token required" state instead of loading normal pages. Also stop WebSocket reconnect spam when auth is impossible. The cleanest backend-side option is a token-aware static fallback for managed mode that serves a minimal page when no valid token or Tauri origin is present.

### P2 - Windows build output can point to stale smoke artifacts

Status: fixed.

Evidence:

- `scripts/build_windows.ps1` defines metadata paths for media and runtime reports: `scripts/build_windows.ps1:216` and `scripts/build_windows.ps1:217`.
- The checks only run when their switches are set: `scripts/build_windows.ps1:226` and `scripts/build_windows.ps1:252`.
- The final build result still always includes `mediaPreparationSmoke` and `runtimeDependencyFootprint` paths: `scripts/build_windows.ps1:498` and `scripts/build_windows.ps1:499`.
- In the current checkout, these release-metadata files existed from an older run while the latest local installer build had skipped checks.

Impact:

The build summary can look like it has fresh installer evidence even when the current build did not run the media-preparation or runtime-footprint gates. That is a release-readiness risk because stale artifacts can mask regressions in ffmpeg, ffprobe, ONNXRuntime, or PyInstaller packaging.

Recommended fix:

Record check status explicitly in the build output, for example `{ ran: false, path: null }` when a gate is skipped. Delete stale report files at build start or require their `generatedAt` timestamp to be newer than the build start before reporting them. Add a focused PowerShell/script test that verifies skipped checks cannot surface old artifact paths as current evidence.

### P3 - File drag/drop upload can use stale runtime settings

Status: fixed.

Evidence:

- Upload behavior depends on `compressionThresholdBytes`: `Frontend/client/src/pages/FileTranscribe.tsx:287` and `Frontend/client/src/pages/FileTranscribe.tsx:288`.
- `uploadFile` captures that value: `Frontend/client/src/pages/FileTranscribe.tsx:319` and `Frontend/client/src/pages/FileTranscribe.tsx:332`.
- `onDrop` calls `uploadFile`, but its dependency list only contains `isUploading`: `Frontend/client/src/pages/FileTranscribe.tsx:495`.
- The dropzone consumes `onDrop`: `Frontend/client/src/pages/FileTranscribe.tsx:501`.

Impact:

After settings or backend upload-limit data changes, drag/drop can keep using stale compression thresholds or labels until the component is recreated. This is not necessarily the full root cause of the reported drag/drop issue, but it is a real state-consistency bug in that path.

Recommended fix:

Wrap `uploadFile` in `useCallback` with the settings and navigation/toast dependencies it reads, then include `uploadFile` in the `onDrop` dependency list. Add a browser interaction smoke for drag/drop using a synthetic file, not only click-to-select.

### P3 - React Query default `on401: "throw"` still returns `null`

Status: fixed.

Evidence:

- The default query function accepts `on401: "returnNull" | "throw"`: `Frontend/client/src/lib/queryClient.ts:32`.
- It returns `null` for 401 when requested: `Frontend/client/src/lib/queryClient.ts:45`.
- The catch block then returns `null` for all fetch errors anyway: `Frontend/client/src/lib/queryClient.ts:53`.
- The global default is configured as `on401: "throw"`: `Frontend/client/src/lib/queryClient.ts:60`.
- Several route prefetches use the default query function: `Frontend/client/src/pages/LiveMic.tsx:848`, `Frontend/client/src/pages/FileTranscribe.tsx:492`, and `Frontend/client/src/pages/Youtube.tsx:577`.

Impact:

Some API failures are silently converted to `null` despite the configuration saying they should throw. This can hide auth, backend, and data-shape problems during navigation/prefetch and makes behavior inconsistent with explicit query functions.

Recommended fix:

Only swallow errors for the explicit `returnNull` mode. For `throw`, rethrow HTTP and parsing errors after optional logging. Add unit coverage around 401 and non-401 failures.

### P3 - Frontend smoke coverage misses the user-reported interaction classes

Status: fixed for the frontend smoke scope. The browser smoke now covers the listed frontend interaction classes. Installed-app process spawning remains covered by installer-specific smokes, not this frontend route smoke.

Evidence:

- The current synthetic frontend smoke passed basic route checks.
- The recent user-reported issues were more specific: YouTube thumbnails, drag/drop, Debug Clear behavior, sticky debug toolbar behavior, tokenized Tauri runtime behavior, and installed-app process spawning.

Impact:

The current smoke is useful as a route/load gate, but these regressions can pass it. This increases the chance of shipping UI regressions after a clean `npm run build` and green route smoke.

Recommended fix:

Add interaction smokes for:

- YouTube route with a real or locally mocked thumbnail image and assertion that image pixels are not placeholder-only.
- Paste/search flow for a real YouTube URL, including thumbnail proxy response status.
- File drag/drop with a synthetic file.
- Debug console Clear button and newest-first/date-default behavior.
- Token-required desktop mode, verifying the WebView receives a token while direct browser access gets a clear runtime/auth state.

### P4 - aiohttp application keys still produce warnings

Status: fixed.

Evidence:

- Pytest passes, but emits `aiohttp.NotAppKeyWarning` for app keys around `src/web_api.py:4401` and `src/web_api.py:4406`.

Impact:

This is not a functional bug today, but it adds warning noise and can hide more important warnings in CI.

Recommended fix:

Replace plain string keys for aiohttp app state with `web.AppKey` typed keys, and update tests that write `shutdown_event` similarly.

## Fix Order Completed

1. Persist and expose summary status/errors; add retry UI.
2. Fix token-required frontend/runtime handling for direct browser access and WebSocket reconnect behavior.
3. Make Windows build metadata honest about skipped or stale checks.
4. Fix File drag/drop callback dependencies and add an interaction smoke.
5. Correct React Query default error behavior.
6. Expand frontend smoke coverage for thumbnails, debug console interactions, and tokenized Tauri runtime.
7. Clean up aiohttp `AppKey` warnings.

## Explicit Gaps After This Implementation

- No physical microphone hardware matrix was run.
- No real external STT provider network calls were run.
- No installed NSIS app smoke was run during this review.
- The frontend browser smoke validates the no-token browser path and a synthetic token-required backend state. It does not replace installed Tauri WebView/NSIS smokes.
