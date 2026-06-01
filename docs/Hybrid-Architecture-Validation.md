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

## 2026-06-02 - NSIS Installer + Real Legacy Data + Upgrade + Uninstall Smoke

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath "C:\Users\Alexander.Immler\Documents\Github\Scriber\Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe" `
  -LegacyDataDir "C:\Users\Alexander.Immler\Documents\Github\Scriber" `
  -VerifyLegacyDataMigration `
  -SimulateUpgrade `
  -VerifyUninstall
```

Result: passed.

Evidence:

- Installer: `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`.
- Temporary install dir: `tmp\installer-smoke\Scriber`.
- Installed app executable: `tmp\installer-smoke\Scriber\scriber-desktop.exe`.
- Test data target: `tmp\installer-smoke\data-78908a0fc6b34873a733802976723e60`.
- First installed runtime mode: `tauri-supervised`.
- First installed launch kind: `sidecar`.
- Legacy source: `C:\Users\Alexander.Immler\Documents\Github\Scriber`.
- Legacy `.env`: copied, 2162 bytes, hash matched.
- Legacy `settings.json`: copied, 944 bytes, hash matched.
- Legacy `transcripts.db`: copied, 24276992 bytes.
- Upgrade simulation: verified.
- Upgrade sentinel: preserved.
- Second installed runtime mode: `tauri-supervised`.
- Second installed launch kind: `sidecar`.
- Second smoke cleanup: verified.
- Silent uninstall: verified.
- Installed app artifacts after uninstall: removed.
- Runtime data directory after uninstall: preserved.
- Uninstall data sentinel: preserved.

Goal coverage:

- Phase 2: proves the installed app starts the packaged Python sidecar without
  Node or a manual Python setup.
- Phase 6: proves first install, upgrade rerun, and silent uninstall behavior
  for the generated NSIS package while preserving existing runtime data.
- Phase 7: adds a real installed-app Windows smoke using the user's actual
  legacy data directory.

Remaining limits:

- This run did not exercise Authenticode signing, updater publication,
  microphone hardware, worker-crash recovery, occupied-port recovery, startup
  timeout recovery, or long stability gates.
- As in the desktop smoke, `transcripts.db` was verified by existence and byte
  size while the backend was active, not by full content hash.
