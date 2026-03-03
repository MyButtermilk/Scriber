# Scriber Bug Report

Generated: 2026-01-07
Updated: 2026-02-27 (Re-Validation + 3 neue Findings)

This document contains bugs and issues identified during comprehensive code reviews of the Scriber codebase.

---

## ✅ Re-Validation Sweep (2026-02-27)

Methodik:
- Referenzierte Stellen in `src/` und `Frontend/` erneut geprüft.
- Kurzer Laufzeittest für FTS5-`DELETE WHERE id = ?` durchgeführt (funktioniert mit `UNINDEXED` Spalte).
- Bestehende Einträge in `confirmed open`, `resolved/invalid` und `needs reproduction` eingeteilt.

Status der bisherigen IDs:
- Confirmed open (34): `C1,C2,C3,C4,C7,R2,R3,R5,R7,R9,L1,L2,L4,L6,L7,L8,L9,RL1,RL2,RL3,F1,F2,F4,F5,F6,F7,F8,F9,S1,S2,S3,S4,S5,S6`
- Resolved/invalid (9): `C5,C6,R1,R4,L3,L5,F3,S7,S8`
- Needs reproduction (3): `R6,R8,RL4`

Kurzbegruendung fuer wichtige Re-Klassifizierungen:
- `C5/C6`: `DELETE FROM transcripts_fts WHERE id = ?` funktioniert mit FTS5 `UNINDEXED` Spalte; die beiden Bugs sind in der aktuellen Form nicht reproduzierbar.
- `R1`: `_on_pipeline_done` nimmt inzwischen `_listening_lock`.
- `L3`: Cleanup passiert jetzt explizit in `stop_listening`; der alte Befund ist so nicht mehr aktuell.
- `S7`: `wait_for(shield(task))` ist hier nicht wirkungslos; `shield` verhindert das Timeout-Cancel des inneren Tasks, danach wird bewusst manuell gecancelt.
- `S8`: `_current_rec` wird im aktuellen Code nicht mehr referenziert.

---

## 🔴 Critical (Crashes / Datenverlust)

### C1. Tkinter-Overlay crasht wegen undefiniertem `BAR_COUNT`

**File:** `src/overlay.py:946`

**Issue:** Tkinter-Fallback referenziert `BAR_COUNT`, das nicht definiert ist → `NameError` crasht den gesamten Overlay-Pfad wenn PySide6 nicht installiert ist.

**Fix:** `BAR_COUNT` durch `self.bar_count` oder `Config.VISUALIZER_BAR_COUNT` ersetzen.

---

### C2. PDF-Export crasht bei Sonderzeichen im Titel

**File:** `src/export.py:322`

**Issue:** Titel wird ohne HTML-Escaping an ReportLab `Paragraph` übergeben. Titel die `<`, `>` oder `&` enthalten crashen den PDF-Export mit einem XML-Parse-Error.

**Fix:** `html.escape(title)` vor Übergabe an `Paragraph`.

---

### C3. Nicht-numerische Env-Vars crashen App beim Start

**File:** `src/config.py:62, 70-71`

**Issue:** `int()` Conversion für `MIC_BLOCK_SIZE` und Paste-Delays wirft `ValueError` bei ungültigen Werten (z.B. `SCRIBER_MIC_BLOCK_SIZE=abc`). Crasht die gesamte App beim Start.

**Fix:** `int()` mit try/except umwickeln und auf Default-Wert zurückfallen.

---

### C4. Gemini Safety-Filter crasht Summarization

**File:** `src/summarization.py:273`

**Issue:** `response.text` wirft `ValueError` wenn Gemini wegen Safety-Filtern blockt, statt einer nutzbaren Fehlermeldung.

**Fix:** `response.candidates` prüfen und `finish_reason` auswerten bevor `response.text` aufgerufen wird.

---

### C5. FTS5-Delete scheitert still — Transkripte nie aus Suchindex entfernt

**Status (2026-02-27):** ❌ Invalidiert (nicht reproduzierbar)

**File:** `src/database.py:337`

**Issue:** `DELETE FROM transcripts_fts WHERE id = ?` scheitert still, weil `id` eine `UNINDEXED` FTS5-Spalte ist. Gelöschte Transkripte tauchen weiterhin in der Suche auf.

**Fix:** Löschung über `rowid`-Match: `DELETE FROM transcripts_fts WHERE rowid = (SELECT rowid FROM transcripts WHERE id = ?)`.

---

### C6. FTS5-Sync erzeugt Duplikate bei Updates

**Status (2026-02-27):** ❌ Invalidiert (nicht reproduzierbar)

**File:** `src/database.py:54`

**Issue:** `_sync_fts_row` versucht DELETE über `UNINDEXED` Spalte `id` (gleicher Bug wie C5). Bei jedem Update wird ein neuer FTS-Eintrag hinzugefügt, aber der alte nicht entfernt → Duplikate im Suchindex.

**Fix:** Wie C5 — über `rowid` löschen.

---

### C7. `getQueryFn` schluckt alle Errors — React Query `isError` permanent `false`

**File:** `Frontend/client/src/lib/queryClient.ts:50-54`

**Issue:** Default `getQueryFn` fängt **alle** Errors (inkl. Programmier-Fehler) und gibt `null as T` zurück. `isError` ist nie `true`, Retries sind deaktiviert, Error-Boundaries nutzlos.

**Fix:** Nur erwartete Netzwerk-Fehler abfangen, Rest weiterwerfen. Oder Error direkt werfen statt `null`.

---

## 🟠 Race Conditions / Concurrency

### R1. `_on_pipeline_done` mutiert State ohne Lock

**Status (2026-02-27):** ✅ Bereits gefixt

**File:** `src/web_api.py:1373-1452`

**Issue:** Done-Callback mutiert `_active_provider` und `_current` ohne `_listening_lock` zu erwerben. Race mit gleichzeitigem `stop_listening` kann zu inkonsistentem State führen.

**Fix:** `_listening_lock` in `_on_pipeline_done` erwerben.

---

### R2. `TranscriptRecord`-Felder ohne Lock gelesen

**File:** `src/web_api.py:1059, 1363, 1169`

**Issue:** Mutable Felder von `TranscriptRecord` werden außerhalb von `_current_lock` gelesen während andere Threads sie beschreiben.

**Fix:** Alle Zugriffe auf `_current` unter `_current_lock` ausführen.

---

### R3. `_load_transcripts_from_db` mutiert shared State aus Thread

**File:** `src/web_api.py:584, 1121, 4221`

**Issue:** Läuft in `asyncio.to_thread` und mutiert `_history` / `_history_by_id` während der Event-Loop-Thread gleichzeitig darauf liest. Keine Synchronisation.

**Fix:** Daten im Thread laden, aber Mutation im Event-Loop-Thread via `call_soon_threadsafe` ausführen.

---

### R4. `toggle_listening` TOCTOU-Race

**Status (2026-02-27):** ✅ Weitgehend mitigiert

**File:** `src/web_api.py:2523-2531`

**Issue:** `_is_stopping` und `_is_listening` werden ohne Lock gelesen. Gleichzeitige Aufrufe per Hotkey und HTTP-API können beide durchkommen → doppelter Start oder doppelter Stop.

**Fix:** `_listening_lock` erwerben bevor Flags geprüft werden.

---

### R5. Pipeline `is_active` Boolean ohne Synchronisation

**File:** `src/pipeline.py:1047, 1591`

**Issue:** Einfaches Boolean `is_active` ohne Lock oder Atomic. Concurrent `start()` / `stop()` Aufrufe können beide durch die Guard-Condition schlüpfen.

**Fix:** `asyncio.Lock` verwenden oder `asyncio.Event`.

---

### R6. Singleton VAD/SmartTurn halten mutablen State zwischen Sessions

**Status (2026-02-27):** ⏳ Needs reproduction

**File:** `src/pipeline.py:65-103`

**Issue:** Gecachte Singleton-Instanzen von VAD und SmartTurn halten internen mutablen State, der zwischen Pipeline-Sessions durchsickert. Kann zu fehlerhafter Erkennung führen.

**Fix:** State bei jedem Session-Start explizit zurücksetzen.

---

### R7. `genai.configure()` setzt globalen API-Key

**File:** `src/summarization.py:257`

**Issue:** `genai.configure(api_key=...)` setzt den Key global. Bei parallelen Summarization-Aufrufen (z.B. Auto-Summarize für mehrere Jobs gleichzeitig) überschreiben sich die Keys gegenseitig.

**Fix:** Pro-Request Client-Instanz statt globale Konfiguration verwenden.

---

### R8. `_active_capture_channel` ohne Thread-Synchronisation

**Status (2026-02-27):** ⏳ Needs reproduction

**File:** `src/microphone.py:299`

**Issue:** Wird aus dem PortAudio-Callback-Thread geschrieben, aber aus dem asyncio-Thread gelesen. Keine Synchronisation → theoretisch torn reads.

**Fix:** `threading.Lock` oder `queue.Queue` für Thread-sichere Kommunikation.

---

### R9. Single-Instance Lock-File TOCTOU-Race

**File:** `src/tray.py:73-99`

**Issue:** Check-then-write auf Lock-File hat TOCTOU-Race. Zwei gleichzeitige Starts können beide den Lock erwerben.

**Fix:** `os.open()` mit `O_CREAT | O_EXCL` für atomare Lock-File-Erstellung, oder `msvcrt.locking()` auf Windows.

---

## 🟡 Logik-Fehler

### L1. `delete_transcript` bricht laufenden Task nicht ab

**File:** `src/web_api.py:3643-3662`

**Issue:** Löscht den DB-Eintrag, aber der laufende Background-Task wird nicht gecancelt. Wenn der Task fertig wird, speichert er das Transkript erneut in die DB → "Zombie-Transkript".

**Fix:** Task via `asyncio.Task.cancel()` stoppen bevor DB-Eintrag gelöscht wird.

---

### L2. YouTube-Download-Error bestraft falschen Circuit Breaker

**File:** `src/web_api.py:1764-1784`

**Issue:** `YouTubeDownloadError` wird gegen den STT-Provider-Circuit-Breaker gezählt. Ein yt-dlp Fehler bestraft einen unbeteiligten Service (z.B. Soniox).

**Fix:** Download-Fehler separat behandeln, nicht an Circuit Breaker weiterleiten.

---

### L3. `stop_listening` setzt `_pipeline_task = None` zu früh

**Status (2026-02-27):** ✅ Bereits gefixt

**File:** `src/web_api.py:1376, 2397-2398`

**Issue:** `_pipeline_task` wird auf `None` gesetzt **bevor** die Pipeline stoppt. Der Guard in `_on_pipeline_done` prüft `_pipeline_task is not None` → Cleanup läuft nie.

**Fix:** `_pipeline_task` erst in `_on_pipeline_done` auf `None` setzen, nicht in `stop_listening`.

---

### L4. Falsche "Heute"/"Gestern"-Labels durch Timezone-Mismatch

**File:** `src/web_api.py:331-338, 439`

**Issue:** `_format_date_label` mischt timezone-aware UTC-Timestamps mit timezone-naiven lokalen Datetimes. Ergibt falsche "Heute"/"Gestern"-Zuordnungen, besonders um Mitternacht.

**Fix:** Alle Timestamps konsistent als UTC verarbeiten und erst für die Anzeige in lokale Zeit konvertieren.

---

### L5. Toter Code: doppelter WAV-Fallback

**Status (2026-02-27):** ✅ Outdated (so nicht mehr vorhanden)

**File:** `src/pipeline.py:228-237`

**Issue:** Doppelter WAV-Fallback-Pfad in `_transcribe_async` wird nie erreicht, weil `_encode_audio` intern bereits zurückfällt.

**Fix:** Toten Code entfernen.

---

### L6. Default-Argument zur Import-Zeit evaluiert

**File:** `src/pipeline.py:811`

**Issue:** `Config.DEFAULT_STT_SERVICE` als Default-Argument wird einmal zur Import-Zeit evaluiert. Runtime-Änderungen über Settings-API werden ignoriert.

**Fix:** `None` als Default verwenden und `Config.DEFAULT_STT_SERVICE` im Funktionskörper auflösen.

---

### L7. YouTube-Download Glob-Fallback kann falsche Datei zurückgeben

**File:** `src/youtube_download.py:291-293`

**Issue:** Glob-Fallback gibt die erste Audio-Datei im Download-Verzeichnis zurück. Kann ein Überbleibsel eines vorherigen Downloads sein.

**Fix:** Nach Datei mit dem erwarteten Video-ID-Prefix suchen.

---

### L8. Auto-Switch auf Summary überschreibt User-Interaktion

**File:** `Frontend/client/src/pages/TranscriptDetail.tsx:483`

**Issue:** `useEffect` wechselt automatisch zum Summary-Accordion-Tab bei jedem `transcript.summary`-Change. Überschreibt manuell gewählte Tab-Auswahl.

**Fix:** Nur beim initialen Summary-Load switchen (z.B. via Ref-Flag).

---

### L9. Endlos-Polling für gelöschte Transkripte

**File:** `Frontend/client/src/pages/TranscriptDetail.tsx:340`

**Issue:** `refetchInterval` gibt `1500` zurück wenn `data` null/undefined ist. Bei gelöschten oder nicht-existierenden Transkripten → Endlos-Polling alle 1.5 Sekunden.

**Fix:** Nur pollen wenn `data?.status` ein aktiver Job-Status ist (`queued`, `transcribing`).

---

## 🔵 Resource Leaks

### RL1. Registry-Key-Handle Leak in `get_autostart`

**File:** `src/web_api.py:3325-3334`

**Issue:** Registry-Key-Handle wird nicht geschlossen wenn `QueryValueEx` eine unerwartete Exception wirft (nicht `FileNotFoundError`).

**Fix:** `try/finally` mit `winreg.CloseKey()` oder `with`-Statement verwenden.

---

### RL2. Registry-Key-Handle Leak in `set_autostart`

**File:** `src/web_api.py:3353-3366`

**Issue:** Gleicher Leak — Handle wird nicht geschlossen bei Exception in `SetValueEx` / `DeleteValue`.

**Fix:** Wie RL1.

---

### RL3. Thread-lokale DB-Connections ohne Cleanup

**File:** `src/database.py:66-84`

**Issue:** Thread-lokale Connections akkumulieren in `_all_connections` ohne Cleanup bei Thread-Exit. Bei vielen kurzlebigen Threads → File-Handle-Leak.

**Fix:** `weakref.ref` Callback bei Thread-Exit oder explizite `close_all_connections()` Funktion.

---

### RL4. `aiohttp.ClientSession` wird geschlossen während Processor noch zugreift

**Status (2026-02-27):** ⏳ Needs reproduction

**File:** `src/pipeline.py:1052-1160`

**Issue:** `aiohttp.ClientSession` wird via `async with` geschlossen, aber der Async-Processor hält noch eine Referenz. Bei Cancellation → `ClientSession is closed` Errors.

**Fix:** Session erst schließen nachdem Processor explizit gestoppt und joined wurde.

---

## 🟣 Frontend-Bugs

### F1. `queryFn` prüft nicht `res.ok` — HTTP-Fehler werden verschluckt

**Files:** `Frontend/client/src/pages/LiveMic.tsx:441`, `Youtube.tsx:322`, `FileTranscribe.tsx:252`

**Issue:** Fetch-Response wird nicht auf `res.ok` geprüft. HTTP 4xx/5xx werden als leere Daten behandelt, `isError` triggert nie.

**Fix:** `if (!res.ok) throw new Error(...)` nach jedem `fetch()`.

---

### F2. `useEffect` in `use-toast.ts` re-subscribt bei jedem Dispatch

**File:** `Frontend/client/src/hooks/use-toast.ts:182`

**Issue:** `state` in der Dependency-Array des `useEffect` → Listener wird bei jedem Toast un/re-subscribed statt einmal beim Mount.

**Fix:** `state` aus Dependencies entfernen, Listener einmal beim Mount registrieren.

---

### F3. Stale Closure in Settings `handleWsMessage`

**Status (2026-02-27):** ❌ Aktuell nicht reproduzierbar

**File:** `Frontend/client/src/pages/Settings.tsx:921`

**Issue:** `handleWsMessage` fängt `selectedDeviceId` via Closure. Wenn eine WebSocket-Message zwischen Render und Effect-Commit ankommt, wird ein staler Wert verwendet.

**Fix:** `useRef` für `selectedDeviceId` verwenden oder `useCallback` mit korrekten Dependencies.

---

### F4. `responseErrorMessage` konsumiert Body doppelt

**File:** `Frontend/client/src/lib/request-errors.ts:59-71`

**Issue:** Versucht erst `res.json()`, dann `res.text()` auf derselben Response. Der zweite Read schlägt fehl weil der Stream bereits consumed ist.

**Fix:** Body einmal als Text lesen, dann JSON-Parse versuchen.

---

### F5. WebSocket Context Value ohne `useMemo`

**File:** `Frontend/client/src/contexts/WebSocketContext.tsx:152-157`

**Issue:** Context `value`-Objekt wird bei jedem Render neu erstellt → alle Consumer re-rendern unnötig.

**Fix:** `useMemo` für das value-Objekt.

---

### F6. Fehlende `clearReconnectTimeout` vor neuem Connect

**File:** `Frontend/client/src/contexts/WebSocketContext.tsx:122` / `use-websocket.ts:135`

**Issue:** `connect()` ruft `clearReconnectTimeout` nicht auf bevor ein neuer Socket geöffnet wird. Ein pending Timeout kann eine doppelte WebSocket-Verbindung erzeugen.

**Fix:** `clearReconnectTimeout()` am Anfang von `connect()` aufrufen.

---

### F7. Shared `VIEW_MODE_STORAGE_KEY` über alle Pages

**Files:** `Frontend/client/src/pages/LiveMic.tsx:397`, `Youtube.tsx:260`, `FileTranscribe.tsx:208`

**Issue:** Alle drei Pages verwenden denselben localStorage-Key für den View-Mode. Wechsel auf einer Page beeinflusst die anderen.

**Fix:** Page-spezifische Keys: `scriber_view_mode_livemic`, `scriber_view_mode_youtube`, `scriber_view_mode_file`.

---

### F8. Clipboard-Write nicht awaited

**File:** `Frontend/client/src/pages/TranscriptDetail.tsx:448-469`

**Issue:** `navigator.clipboard.writeText()` Promise wird nicht awaited oder caught. User sieht "Copied!" auch wenn der Clipboard-Write fehlschlägt (z.B. fehlende Permission).

**Fix:** `await` + try/catch mit Fehler-Toast.

---

### F9. Settings `queryFn` gibt `{}` bei Fehler zurück statt zu werfen

**File:** `Frontend/client/src/pages/TranscriptDetail.tsx:314-325`

**Issue:** Bei `!res.ok` wird `{}` zurückgegeben statt ein Error geworfen. React Query kennt keinen Error-State, kein Retry.

**Fix:** `throw new Error(...)` bei `!res.ok`.

---

## ⚪ Sonstige

### S1. Clipboard-Restore-Timer feuert zu früh

**File:** `src/injector.py:291-311`

**Issue:** Der Timer für Clipboard-Restore kann feuern bevor die Ziel-App den Paste-Inhalt konsumiert hat → ursprünglicher Clipboard-Inhalt wird zu früh wiederhergestellt, Paste-Inhalt geht verloren.

**Fix:** Delay erhöhen oder nach Paste-Bestätigung restoren.

---

### S2. Leeres Clipboard wird nach Paste nie wiederhergestellt

**File:** `src/injector.py:181`

**Issue:** `_windows_clipboard_get_text` gibt `None` bei leerem Clipboard zurück. Der Restore-Code prüft `if old_text:` → ein ursprünglich leeres Clipboard wird nach Paste nie wiederhergestellt (bleibt mit Paste-Inhalt gefüllt).

**Fix:** `None` vs leeren String unterscheiden: leeres Clipboard explizit wiederherstellen.

---

### S3. Overlay `_fall`/`_peak` Arrays nicht bei Resize rebuildet

**File:** `src/overlay.py:282-285`

**Issue:** `_fall` und `_peak` Arrays werden in `show_recording()` nicht neu aufgebaut. Wenn die Bar-Anzahl zur Laufzeit steigt → `IndexError`.

**Fix:** Arrays in `show_recording()` auf aktuelle Bar-Anzahl resizen.

---

### S4. Watchdog ohne Startup-Grace-Period

**File:** `src/tray.py:400-440`

**Issue:** Watchdog prüft Backend-Health sofort. Ein langsamer Backend-Start (z.B. große Modelle laden) löst nach 15 Sekunden einen Restart-Loop aus.

**Fix:** Grace-Period von 30-60s beim ersten Start.

---

### S5. `taskkill` in `stop_frontend` ohne Exception-Handling

**File:** `src/tray.py:381`

**Issue:** `taskkill` ist nicht in try/except gewrappt. Wenn der Prozess bereits beendet ist, schlägt `taskkill` fehl und verhindert den restlichen Shutdown.

**Fix:** `try/except subprocess.CalledProcessError`.

---

### S6. `.env`-Pfad relativ statt absolut

**File:** `src/config.py:268`

**Issue:** `persist_to_env_file` schreibt in relativen Pfad `.env` (basierend auf cwd) statt in den Projekt-Root. Wenn die App aus einem anderen Verzeichnis gestartet wird, wird die falsche `.env` aktualisiert.

**Fix:** `pathlib.Path(__file__).parent.parent / ".env"` verwenden (konsistent mit `_JSON_SETTINGS_PATH`).

---

### S7. `asyncio.shield` innerhalb `asyncio.wait_for` wirkungslos

**Status (2026-02-27):** ❌ Invalidiert (aktuelle Nutzung ist absichtlich)

**File:** `src/microphone.py:427`

**Issue:** `asyncio.shield` innerhalb `asyncio.wait_for` heben sich gegenseitig auf. `wait_for` cancelt das inner Future bei Timeout, `shield` soll genau das verhindern → wirkungslos.

**Fix:** Entweder nur `wait_for` oder manuelles Timeout-Management.

---

### S8. `_emergency_stop_pipeline` referenziert undefiniertes `_current_rec`

**Status (2026-02-27):** ✅ Bereits gefixt

**File:** `src/web_api.py` (Bestehender Bug #24)

**Issue:** Emergency stop prüft `self._current_rec`, aber der Controller verwendet `_current`. Wirft `AttributeError` und überspringt Cleanup → Pipeline/Overlay bleibt in stuck State.

**Fix:** `self._current` unter Lock verwenden.

---

## 🆕 Neue Findings (2026-02-27)

### N1. `DELETE /api/transcripts/{id}` ignoriert DB-Delete-Fehler

**Files:** `src/web_api.py:3670`, `src/database.py:334-344`

**Issue:** API loescht aus In-Memory-History und ignoriert den Rueckgabewert von `db.delete_transcript(...)`. Bei DB-Fehlern antwortet der Endpoint trotzdem mit `{"success": true}`.

**Fix:** Rueckgabewert pruefen, bei `False` HTTP 500 liefern und In-Memory-Delete ggf. rollbacken oder konsistent markieren.

---

### N2. Single-Instance-Garantie wird bei Lock-Fehler still deaktiviert

**File:** `src/tray.py:97-99`

**Issue:** `acquire_single_instance_lock()` gibt bei Exception `True` zurueck. Wenn Lock-File-Operationen fehlschlagen (Permissions/IO), koennen mehrere Instanzen gleichzeitig laufen.

**Fix:** Bei Lock-Fehler nicht fail-open laufen; stattdessen Fehler signalisieren und Start abbrechen oder auf OS-level Locking umstellen.

---

### N3. `_load_transcripts_from_db()` dupliziert Historie bei erneutem Aufruf

**File:** `src/web_api.py:1131-1160`

**Issue:** Die Methode appended in `_history`/`_history_by_id`, ohne vorher zu leeren. Ein zweiter Aufruf fuehrt zu doppelten In-Memory-Eintraegen.

**Fix:** Vor Reload `clear()` ausfuehren oder ein dediziertes Merge/Upsert pro `id` nutzen.

---

## 📊 Summary

### Statistik

| Typ | Anzahl |
|-----|--------|
| Confirmed open | 37 |
| Resolved/invalid | 9 |
| Needs reproduction | 3 |
| **Gesamt erfasste Findings** | **49** |

### Prioritäts-Empfehlung

**Sofort fixen:**
1. C2 (PDF-Export Crash bei Sonderzeichen im Titel)
2. C7 (globales Schlucken von Query-Fehlern im Frontend)
3. L1 (Delete ohne Cancel erzeugt Zombie-Transkripte)
4. N1 (Delete-API meldet Success trotz DB-Fehler)
5. C1 (Tkinter-Fallback-Overlay kann hard crashen)

**Zeitnah fixen:**
1. F1 + F4 + F9 (Fehlerbehandlung in Frontend-Requests konsolidieren)
2. L2 (Circuit Breaker darf YouTube-Download-Fehler nicht als STT-Provider-Failure zaehlen)
3. RL1 + RL2 (Registry-Key Handles robust schliessen)
4. S6 (absoluter `.env`-Pfad fuer konsistente Persistenz)
5. N2 (Single-Instance Lock fail-open beheben)

**Backlog:**
- `R6`, `R8`, `RL4` gezielt reproduzieren und dann final klassifizieren.
- Restliche confirmed-open Findings nach User-Impact priorisieren.

---

### Zuvor fixierte Bugs (aus früheren Reviews)

- Clipboard restore ✅
- Favorite mic logic ✅
- Settings stale closure ✅
- main.py race conditions ✅
- RecordingPopup error handler ✅
- Legacy mic validation ✅
- WebSocket reconnection ✅
- File/YouTube Soniox-only fix ✅
- AssemblyAI auto-detect ✅
- Tk mic preview name-based IDs ✅
- Soniox direct transcription cleanup ✅
