# Legacy Desktop Fallback Decision

Date: 2026-06-02

## Decision

Keep the legacy Python desktop paths as maintenance-only fallback:

- `python -m src.tray`
- `python -m src.main`
- `src.ui`
- Tk fallback paths in `src.overlay`

The primary desktop runtime is Tauri. New desktop-shell functionality belongs
in `Frontend/src-tauri/` unless it is explicitly backend-owned behavior exposed
through the existing REST/WebSocket API.

## Rationale

The hybrid architecture goal moves shell ownership to Tauri/Rust while Python
keeps STT, Pipecat, providers, SQLite, uploads, exports, jobs, and audio capture
for now. Removing the legacy Python desktop paths immediately would reduce
operational fallback options before two stable Tauri release candidates have
exercised real user installations.

Keeping the legacy paths avoids a forced cutover while the remaining external
release gates are still open:

- physical microphone hardware matrix,
- real Authenticode signing,
- published signed updater metadata,
- production release/update operation.

## Policy

- Tauri is the default and release-target desktop shell.
- Legacy Tkinter and Python tray paths stay available for diagnostics and
  emergency fallback until at least two Tauri release candidates pass the full
  automated installer smoke set and the manual hardware matrix.
- No new user-facing desktop-shell features should be added to legacy Tkinter
  or Python tray code unless needed to keep fallback startup functional.
- Bug fixes are allowed when they preserve fallback viability or shared backend
  behavior.
- Tauri and Python must not own duplicate recording state. Recording state
  remains in the Python backend and is controlled through existing endpoints.
- Autostart and global hotkey ownership in Tauri-managed desktop runtime stays
  with Tauri. Legacy paths may keep their old behavior only for non-Tauri
  launches.

## Removal Gate

Legacy desktop paths can be deprecated for removal only after all of the
following are true:

- two Tauri release candidates have passed the installed smoke matrix,
- the physical microphone hardware matrix is documented as passed,
- support bundle, crash recovery, controlled shutdown, startup timeout,
  default-port conflict, external-backend attach, frontend/WebView readiness,
  upgrade, uninstall, and live recording gates are still green on the release
  candidate build,
- a signed installer and signed updater metadata have been produced by the real
  release process,
- README and AGENTS.md no longer point users or agents to legacy paths as normal
  startup commands.

## Current Status

As of 2026-06-02, keep legacy desktop fallback. The current Tauri installer has
passed the automated installed recovery matrix, but external signing/updater
publication and the physical microphone hardware matrix are not yet complete.
