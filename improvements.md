# Scriber Improvements
Updated: 2026-01-07

This document lists further improvement ideas based on the current codebase review.

---

## 🔴 High Impact (Product + Reliability)
1. ~~Unify file/youtube transcription path to respect the selected STT provider~~ ✅ DONE
2. ~~Add user-visible cancel/stop for file and YouTube transcription tasks (both backend cancel + UI action).~~ ✅ DONE
3. Centralize microphone device resolution so web UI and Tk UI behave identically (single helper used by pipeline + mic preview).
4. Add lightweight transcript export endpoints (txt/markdown/json) and wire the UI "Export" button.
5. Introduce retry/backoff policies per provider (network and rate limit errors) with clear user messages.
6. **NEW**: Extend language support to Deepgram, Gladia, Speechmatics, AWS (Bug #4).
7. **NEW**: Expand LANGUAGE_MAP beyond 7 supported languages (Bug #12).

---

## 🟠 Backend/Platform
1. Add DB indexes for `created_at`, `status`, and `type` to keep transcript list queries fast as history grows.
2. Persist summaries in DB via a dedicated update method (avoid silent loss if process exits after in-memory update).
3. Add MIME sniffing for uploads (match extension + file header) to reduce "wrong file type" failures.
4. Implement a background cleanup job for old downloads/temp folders and oversized artifacts.
5. Standardize error payloads across all endpoints (consistent `{message, code, details}` shape).
6. **NEW**: Make port configurable in `tray.py` (currently hardcoded to 8765).
7. **NEW**: Add graceful shutdown hook to close all DB connections cleanly.
8. **NEW**: Add health-check endpoint with detailed status (DB, STT service availability, disk space).

---

## 🟡 Frontend/UX
1. Virtualize long transcript lists (e.g., 1000+ items) to avoid slow renders.
2. Add a "queue" view for pending/processing items with real-time ETA and progress.
3. Make live transcript UI resilient to huge text (collapse / "show more" / incremental rendering).
4. Expose backend status in header (green/amber/red) using the existing health check hook.
5. Offer per-session language override in Live Mic (UI control + payload into start endpoint).
6. **NEW**: Add keyboard shortcuts for common actions (start/stop recording, copy transcript).
7. **NEW**: Add dark/light theme persistence across sessions.
8. **NEW**: Implement toast for successful operations (copy, export, save settings).
9. **NEW**: Add bulk delete/export for transcript management.
10. **NEW**: Show selected microphone name in Live Mic header for confirmation.

---

## 🔒 Security & Privacy
1. **NEW**: Encrypt API keys at rest in `.env` / settings.json (use OS keychain or encrypted store).
2. **NEW**: Add option to auto-delete transcripts after X days.
3. **NEW**: Sanitize/redact PII from transcripts on export (optional toggle).
4. **NEW**: Add session timeout for web UI when running on network (not localhost).
5. **NEW**: Audit logging for sensitive operations (API key changes, transcript deletions).

---

## ⚡ Performance
1. **NEW**: Lazy-load STT service imports (already done for most, verify all paths).
2. **NEW**: Use streaming JSON for large transcript lists instead of loading all into memory.
3. **NEW**: Cache FFmpeg probe results to avoid repeated startup checks.
4. **NEW**: Profile overlay rendering on low-end hardware and optimize if needed.
5. **NEW**: Use WebSocket binary frames for audio level updates (reduce JSON overhead).

---

## ♿ Accessibility
1. **NEW**: Add ARIA labels to all interactive elements in the frontend.
2. **NEW**: Ensure keyboard navigation works across all pages.
3. **NEW**: Add screen reader announcements for recording state changes.
4. **NEW**: Increase minimum touch target sizes for mobile (if ever targeted).
5. **NEW**: Provide high-contrast mode option.

---

## 🧪 Developer Experience
1. Add integration tests for file/youtube flows across multiple STT providers (mocked providers).
2. Add unit tests for device resolution, language mapping, and upload validation.
3. Provide a single "dev all" command that starts backend + frontend + tray with logs.
4. Document provider-specific capabilities (language support, diarization, file size limits) in README.
5. **NEW**: Add pre-commit hooks for linting (ruff/eslint) and type checking.
6. **NEW**: Set up CI pipeline with test coverage reporting.
7. **NEW**: Add Playwright E2E tests for critical user journeys.
8. **NEW**: Create a CONTRIBUTING.md with setup instructions and coding standards.

---

## 🗑️ Tech Debt / Maintenance
1. Split `web_api.py` into smaller modules (routing, controller, helpers) for clearer ownership.
2. Move config defaults into a typed settings object and validate on startup.
3. Replace ad-hoc globals with a small app state container for easier testing.
4. Reduce duplicate UI logic between Tk and Web (shared constants for bar count, colors, etc.).
5. Add a migration plan for legacy Tk UI if it will be deprecated.
6. **NEW**: Remove or update `main.py` (legacy Tkinter entry point) - diverges from web_api.
7. **NEW**: Consolidate progress hook error handling (currently swallowed silently in youtube_download).
8. **NEW**: Add type hints to all public functions in core modules.
9. **NEW**: Extract common WebSocket message types into shared constants.
10. **NEW**: Unify hotkey handling between main.py and web_api.py.

---

## 💡 Future Features
1. ~~Add speaker diarization labels to transcript UI~~ ✅ DONE (Backend parsing + Frontend badging)
2. **NEW**: Real-time translation mode (transcribe + translate via LLM).
3. **NEW**: Voice commands to control recording ("Scriber, stop recording").
4. **NEW**: Integration with note-taking apps (Notion, Obsidian, OneNote).
5. **NEW**: Mobile companion app for remote recording control.
6. **NEW**: Batch transcription mode for folders of audio files.
7. **NEW**: Scheduled recording (start at specific time).
8. **NEW**: Audio playback with synchronized transcript highlighting.
9. **NEW**: Custom vocabulary UI in settings (currently only via .env).
10. **NEW**: Multi-user support with separate transcript libraries.

