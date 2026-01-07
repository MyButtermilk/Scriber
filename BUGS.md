# Scriber Bug Report

Generated: 2026-01-07
Updated: 2026-01-07 (Re-validated + new findings)

This document contains bugs and issues identified during a comprehensive code review of the Scriber codebase.

---

## 🔴 Critical Bugs

### 1. ~~`_paste_text` silently fails to restore clipboard~~ ✅ FIXED
**File:** `src/injector.py` | **Fix:** Changed early return to pass with proper indentation.

---

### 2. ~~Favorite mic always overrides selected mic~~ ✅ FIXED
**File:** `src/pipeline.py` | **Fix:** Favorite now only overrides when selected is "default" or unavailable.

---

### 20. ~~File/YouTube transcription always uses Soniox direct~~ ✅ FIXED

**Files:** `src/web_api.py`

**Fix:** Now checks `Config.DEFAULT_STT_SERVICE` and uses `transcribe_file_direct` only for Soniox, falls back to `transcribe_file` (pipecat flow) for other services.

---

## 🟠 Medium Issues

### 3. ~~`handleSetFavoriteMic` stale closure~~ ✅ FIXED
**File:** `Frontend/Settings.tsx` | **Fix:** Capture original value before optimistic update.

---

### 4. Missing language support for several STT services

**File:** `src/pipeline.py`

**Issue:** Language parameter not passed to:
- Deepgram (line 645)
- Gladia (line 669)  
- Speechmatics (line 681)
- AWS Transcribe (line 686)

**Impact:** Non-English transcription uses auto-detect or English default.

**Note:** Groq, OpenAI, and Azure already have language support.

---

### 21. ~~AssemblyAI auto-detect never activates (language forced to EN)~~ ✅ FIXED

**File:** `src/pipeline.py`

**Fix:** When `lang` is `None` (auto), the `language` parameter is now omitted entirely, allowing AssemblyAI's multilingual model to auto-detect.

---

### 5. ~~`main.py` race conditions with global state~~ ✅ FIXED

**File:** `src/main.py`

**Fix:** Added `asyncio.Lock` (`_listening_lock`) to protect `start_listening()`, `stop_listening()`, and related operations from concurrent execution when hotkey is pressed rapidly.

---

### 6. ~~RecordingPopup missing WebSocket error handler~~ ✅ FIXED

**File:** `Frontend/components/RecordingPopup.tsx`

**Fix:** Added error case handler + useWebSocket hook. Popup now hides and shows toast on recording errors.

---

### 7. ~~`summarization.py` model name mismatches~~ ✅ NOT A BUG
Model names like `gemini-3-pro-preview`, `gpt-5.2` are valid for 2026.

---

### 8. ~~Overlay bar count static~~ ✅ NOT A BUG
Already reloads on each recording start via `show_recording()`.

---

### 9. ~~Toast import missing in Youtube.tsx~~ ✅ NOT A BUG  
`useToast` is imported at line 11.

---

### 10. ~~Legacy mic IDs without validation~~ ✅ FIXED
Added device existence validation before using legacy numeric IDs.

---

### 22. ~~Tk overlay fallback crashes due to undefined `BAR_COUNT`~~ ✅ FIXED

**File:** `src/overlay.py`

**Fix:** Replaced `BAR_COUNT` with `getattr(Config, 'VISUALIZER_BAR_COUNT', 45)` to use configured value.

---

## 🟡 Minor Issues

### 11. ~~`tray.py` process termination timeout issues~~ ✅ NOT A BUG
`terminate()` sends SIGTERM which IS the graceful shutdown signal. `kill()` is correct fallback.

---

### 12. `_selected_language()` limited language map

**File:** `src/pipeline.py` (lines 378-387)

**Issue:** Only 7 languages supported: EN, DE, FR, ES, IT, PT, NL. Missing common languages: Japanese, Chinese, Korean, Russian, Arabic, Hindi, etc.

**Impact:** Users who select unsupported languages get auto-detect instead.     

---

### 23. ~~Tk UI mic preview ignores name-based device IDs~~ ✅ FIXED

**File:** `src/ui.py`

**Fix:** `_resolve_device()` now looks up devices by name if numeric conversion fails, matching pipeline.py behavior.

---

### 13. ~~Database connection leak potential~~ ✅ NOT A BUG
Thread-local connections are reused by thread pools. Only accumulates if many short-lived threads are created, which doesn't happen in practice.

---

### 14. ~~`FfmpegAudioFileInput` assertion could crash~~ ✅ MINOR CODE STYLE
Using `assert` is technically fragile with `-O` flag, but nobody runs Python apps with `-O` in practice.

---

### 15. ~~Frontend WebSocket not reconnecting on disconnect~~ ✅ FIXED

**Files:** All 4 page components

**Fix:** Created `use-websocket.ts` hook with:
- Automatic reconnection on disconnect
- Exponential backoff (1s base, max 30s)
- Connection state tracking
- Clean disconnect on unmount

Updated: `LiveMic.tsx`, `FileTranscribe.tsx`, `Youtube.tsx`, `TranscriptDetail.tsx`

---

### 16. ~~`youtube_api.py` statistics parsing can overflow~~ ✅ NOT A BUG
Theoretical only - top video has 14B views, safe integer limit is 9 quadrillion. No real-world impact.

---

### 17. ~~Inconsistent Port Configuration~~ ✅ NOT A BUG
`tray.py` hardcodes 8765, but there's no UI or documentation to change the port. De facto not configurable, so no conflict.

---

### 18. ~~`main.py` is legacy/divergent~~ ✅ TECH DEBT (not a bug)
`main.py` is an alternative Tkinter entry point. Not a bug, just legacy code that could be deprecated.

---

### 19. ~~`youtube_download.py` robustness~~ ✅ NOT A BUG
1. `_require_ffmpeg()` IS called before download (line 63) ✓
2. Exception swallowing in hooks is intentional to prevent UI crashes ✓
3. `final_path` fallback with glob is robust enough ✓

---

## 📝 Summary

### Actually Open Bugs (6):
1. **#4** Missing language support for Deepgram, Gladia, Speechmatics, AWS
2. **#12** Limited LANGUAGE_MAP (7 languages only)
3. **#20** File/YouTube transcription forced to Soniox direct
4. **#21** AssemblyAI auto-detect disabled (forces EN)
5. **#22** Tk overlay fallback `BAR_COUNT` undefined
6. **#23** Tk mic preview ignores name-based device IDs

### Fixed Bugs (7):
- #1 Clipboard restore ✅
- #2 Favorite mic logic ✅
- #3 Settings stale closure ✅
- #5 main.py race conditions ✅
- #6 RecordingPopup error handler ✅
- #10 Legacy mic validation ✅
- #15 WebSocket reconnection ✅

### False Positives Removed (10):
- #7, #8, #9, #11, #13, #14, #16, #17, #18, #19

---

## Files Reviewed

### Backend (16 files)
| File | Lines | Status |
|------|-------|--------|
| `config.py` | 275 | ✓ Clean |
| `pipeline.py` | 1111 | ✓ Bugs #4, #12, #21 open |
| `web_api.py` | 1886 | ✓ Bug #20 open |
| `microphone.py` | 227 | ✓ Clean |
| `overlay.py` | 1047 | ✓ Bug #22 open |
| `injector.py` | 253 | ✓ Fixed |
| `database.py` | 214 | ✓ Clean |
| `tray.py` | 709 | ✓ Clean |
| `summarization.py` | 113 | ✓ Clean |
| `youtube_api.py` | 269 | ✓ Clean |
| `youtube_download.py` | 206 | ✓ Clean |
| `audio_file_input.py` | 162 | ✓ Clean |
| `main.py` | 260 | ✓ Fixed (race conditions) |
| `gemini_transcribe.py` | 55 | ✓ Clean (standalone script) |
| `ui.py` | 931 | ✓ Bug #23 open |
| `__init__.py` | 0 | ✓ Clean |

### Frontend (pages + hooks)
| File | Lines | Status |
|------|-------|--------|
| `Frontend/client/src/pages/LiveMic.tsx` | 445 | ✓ Clean |
| `Frontend/client/src/pages/FileTranscribe.tsx` | 426 | ✓ Clean |
| `Frontend/client/src/pages/Youtube.tsx` | 588 | ✓ Clean |
| `Frontend/client/src/pages/TranscriptDetail.tsx` | 436 | ✓ Clean |
| `Frontend/client/src/pages/Settings.tsx` | 1181 | ✓ Clean |
| `Frontend/client/src/components/RecordingPopup.tsx` | 345 | ✓ Clean |
| `Frontend/client/src/hooks/use-websocket.ts` | 128 | ✓ Clean |

### Other Files (reviewed, no issues found)
- Frontend hooks/lib/components (including all shadcn/ui files), server files, shared schema, and build/config files
- Tests, scripts, docs, and root configs
