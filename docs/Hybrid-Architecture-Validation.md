# Hybrid Architecture Validation Log

This file records concrete validation evidence for `docs/Hybrid-Architecture-Goal.md`.
It is intentionally separate from the goal text so local goal edits can stay
unmixed with verification results.

## 2026-06-02 - Tauri Sidecar + Real Legacy Data Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_tauri_desktop.ps1 `
  -BackendExePath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\backend\scriber-backend.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -DisableDevFallback `
  -TimeoutSec 90 `
  -BackendHealthTimeoutSec 40
```

Result: passed.

Evidence:

- Release shell: `Frontend\src-tauri\target\release\scriber-desktop.exe`.
- Backend launch kind: `sidecar`.
- Runtime mode: `tauri-supervised`.
- Backend health: ready, API version `1`.
- Backend port: `127.0.0.1:8765`.
- Test data target: `tmp\tauri-smoke-data\7cb1578c03224e00bbb178b9673a219e`.
- Runtime `dataDir` matched the test data target.
- Runtime `downloadsDir` was under the test data target.
- Legacy source: `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
- Legacy `.env`: copied, 2162 bytes, hash matched.
- Legacy `settings.json`: copied, 944 bytes, hash matched.
- Legacy `transcripts.db`: copied, 24276992 bytes.
- Legacy `downloads`: checked, 0 source files.
- Smoke cleanup: verified.

Goal coverage:

- Phase 2: proves the release Tauri shell can supervise a packaged Python
  sidecar and reach the backend health/runtime contracts without dev fallback.
- Phase 6: proves existing local `.env`, `settings.json`, and `transcripts.db`
  are migrated into `SCRIBER_DATA_DIR` without overwriting the source data.
- Phase 7: adds a real Windows desktop smoke run using the user's actual legacy
  data directory instead of only synthetic fixtures.

Remaining limits:

- This run did not exercise the NSIS installer, upgrade, uninstall, Authenticode
  signing, updater publication, microphone hardware, or long stability gates.
- `transcripts.db` was verified by existence and byte size during the running
  backend smoke, not by a full content hash, because the smoke avoids locking or
  reading the live SQLite database more aggressively than necessary.
