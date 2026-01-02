# UI/UX Improvement Proposals

This document collects UI/UX ideas for Scriber (Web UI + tray/overlay) and groups them by impact/effort to help prioritize work.

## UX goals (north stars)

- Setup in under 2 minutes (mic + STT provider + hotkey)
- Recording state is always unambiguous (what is happening right now?)
- History is fast to scan, search, and act on (copy/export/delete/summarize)
- Errors are actionable (what happened, why, how to fix)
- Accessibility-first defaults (keyboard, focus, contrast, reduced motion)

---

## Priority 1: High-impact, low-effort (quick wins)

### 1) Setup status banner (first-run + ongoing)

Show a persistent, non-intrusive banner when the app is "not ready" (e.g., missing API key, no mic selected, hotkey conflict, backend offline).

- Checklist style: "1/3 completed" with direct links into Settings
- One-click "Test configuration" that runs lightweight checks (mic list, settings endpoint reachable)

**Impact:** lower bounce rate + fewer "it doesn't work" moments  
**Effort:** low

### 2) Global recording state indicator + controls

Make recording state visible across all routes, not just inside `LiveMic`.

- Header pill: `Recording - 00:32` + Stop button
- Clear state taxonomy: `Stopped / Recording / Processing / Error`
- Optional "minimized" mode when user navigates away

**Impact:** reduces uncertainty and accidental recordings  
**Effort:** low/medium

### 3) Destructive actions: confirm + undo

Current list actions rely heavily on hover. Improve safety and discoverability:

- Replace hover-only delete with overflow menu (`...`) on each card
- Add "Undo" toast for deletes (soft-delete client-side; commit after timeout)
- Keyboard accessible actions (Enter opens, Del prompts delete)

**Impact:** prevents accidental data loss, improves accessibility  
**Effort:** low

### 4) Empty/error states: always actionable

Standardize empty and error views across pages:

- Empty history: "Record your first transcript" + Start button + hotkey hint
- Backend offline: show status + retry + "Open logs" link (tray)
- Missing permission/device: explain and link to Settings

**Impact:** fewer dead ends  
**Effort:** low

### 5) Transcript cards: surface the "why it matters"

Improve scanability by showing consistent metadata and status:

- Duration, language, source (mic/youtube/file), created time, provider (optional)
- Status badges: processing/ready/failed with clear next action
- Quick actions: Copy / Open / Summarize (context menu)

**Impact:** faster navigation and confidence  
**Effort:** low/medium

---

## Priority 2: Medium effort (workflow polish)

### 6) History: filters + saved views

Make transcript browsing scale beyond a handful of items:

- Filters: source, date range, status, language, provider
- Saved views: "Today", "Last 7 days", "Failed", "Favorites"
- Highlight matches in titles/snippets

**Impact:** scales to large transcript libraries  
**Effort:** medium

### 7) Transcript detail: editing + export designed for "reuse"

Turn Transcript Detail into a "finalize and use it" screen:

- Simple in-place edit mode (title + body)
- Export: `.txt`, `.md`, `.docx` (or copy as Markdown)
- "Copy summary" / "Copy transcript" with clear feedback and formatting options

**Impact:** makes Scriber feel like a tool, not just a viewer  
**Effort:** medium

### 8) Summarization UX: progress + control

Summarization is valuable but needs trust-building UI:

- Show "Summarizing..." state with elapsed time + cancel (if supported)
- Allow "Regenerate" with model choice and prompt preset
- Display a compact summary format by default (bullets + action items)

**Impact:** higher perceived quality + repeat usage  
**Effort:** medium

### 9) YouTube + file flows: queue + progress

Improve feedback and reduce waiting uncertainty:

- Drag & drop for files + upload/progress indicator + cancel
- YouTube: "queued / downloading / transcribing / done" pipeline UI
- Allow multiple items queued (even if processed sequentially)

**Impact:** better long-running task UX  
**Effort:** medium

### 10) Settings IA: "Simple" vs "Advanced"

Settings are powerful but dense; reduce cognitive load:

- Simple mode: mic, hotkey, default provider, language, auto-summarize
- Advanced: per-provider keys + provider-specific knobs (vocab, modes, etc.)
- Add a "Test key" action per provider (even a basic ping/validation helps)

**Impact:** faster successful configuration  
**Effort:** medium

---

## Priority 3: Larger bets (differentiating UX)

### 11) Command palette (Ctrl/Cmd+K)

Global actions and navigation:

- Start/Stop recording, open last transcript, search transcripts, open settings
- Reduce pointer travel and make the app "feel fast"

**Impact:** power-user delight  
**Effort:** medium/high

### 12) Safer injection UX (type/paste/auto)

When injecting text into other apps, UX must prevent surprises:

- "Preview bubble" that shows what will be injected next
- "Pause injection" toggle + clear state indicator
- "Safe mode": never inject while focused window is unknown/blacklisted

**Impact:** trust and safety in the core workflow  
**Effort:** high

---

## Desktop tray + overlay ideas

- Overlay: show clear REC indicator + timer + audio level, with a compact "minimized" mode
- Overlay: drag to reposition, remember last position, optional opacity slider
- Tray menu: show live status (Stopped/Recording/Processing) + last transcript quick actions
- Notifications: optional system toast on start/stop and when a transcript finishes

---

## Accessibility checklist (baseline)

- Visible focus rings on all interactive elements
- Full keyboard navigation (including card actions and menus)
- Respect `prefers-reduced-motion` for page transitions/animations
- Ensure contrast is readable in both themes and across neumorphic surfaces

