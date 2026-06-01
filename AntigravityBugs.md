# Implementierungsplan – Kritische Bugfixes & Stabilitätsverbesserungen

Dieser Plan beschreibt drei kritische Fehler, die im Scriber-Python-Backend identifiziert und **gegen den aktuellen Quellcode validiert** wurden.

> **Ergebnis der Überprüfung**: Der ursprüngliche Bug 3 (Dateileck bei `asyncio.CancelledError` in `web_api.py`) ist **bereits behoben**. Die `_schedule_youtube_job()` und `_schedule_file_job()` Runner fangen `CancelledError` bereits ab (Zeilen 1089–1093 und 1122–1126) und setzen den Status auf `"stopped"`. Der YouTube-`finally`-Block (Zeile 2080–2083) löscht `out_dir` bedingungslos, und der File-`finally`-Block (Zeile 2368) prüft `rec.status != "processing"` – was durch den Cancelled-Handler korrekt auf `"stopped"` gesetzt wird.

---

## Fehleranalyse & Lösungsvorschläge

### Bug 1: PortAudio-Zugriffsverletzung (Race Condition im Device Monitor)
* **Status (2026-06-01)**: ✅ **Behoben**. `_enumerate_microphones()` läuft jetzt unter `get_device_guard_lock()`, PortAudio-Refreshes werden während aktiver Streams deferred und nach dem Stop einmalig nachgeholt. Abgedeckt durch `tests/test_device_monitor.py`.
* **Datei**: `src/device_monitor.py`
* **Historisches Problem**: Der Hintergrund-Thread `DeviceMonitor._run()` führte regelmäßig `_refresh_portaudio_cache()` aus, welches PortAudio über `sd._terminate()` / `sd._initialize()` zurücksetzt. Dieser Reset hielt den `_DEVICE_GUARD_LOCK`, aber `_enumerate_microphones()` rief `sd.query_devices()` ursprünglich ohne denselben Lock auf. Wenn ein paralleler Thread `_enumerate_microphones()` aufrief, während PortAudio re-initialisiert wurde, konnte `sd.query_devices()` auf freigegebenen nativen Speicher zugreifen → **Windows Access Violation / Segfault**.

* **Betroffene Aufrufketten**:
  - `DeviceMonitor.get_devices()` (Zeile 308) → `_enumerate_microphones()` (Zeile 312) – wird von Web-API-Routen aufgerufen
  - `DeviceMonitor._refresh_devices()` (Zeile 342) → `_enumerate_microphones()` (Zeile 349) – wird vom Monitor-Thread aufgerufen

* **Umgesetzt**: Der gesamte Rumpf von `_enumerate_microphones()` ist in `with get_device_guard_lock():` gekapselt:

```python
def _enumerate_microphones(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[dict[str, str]]:
    if not HAS_SOUNDDEVICE:
        return [{"deviceId": "default", "label": "Default"}]

    result: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]
    with get_device_guard_lock():          # <-- NEU: Lock gegen PortAudio-Race
        try:
            all_devices = list(sd.query_devices())
        except Exception as exc:
            logger.debug(f"[DeviceMonitor] query_devices failed: {exc}")
            return result

        # ... restlicher Code bleibt innerhalb des with-Blocks ...
```

> **Hinweis**: `_refresh_portaudio_cache()` verwendet denselben Lock (`_DEVICE_GUARD_LOCK`) als `RLock`. Da `_refresh_devices()` erst `_refresh_portaudio_cache()` aufruft (Lock wird gehalten und freigegeben) und dann `_enumerate_microphones()` (Lock wird erneut erworben), entsteht **kein Deadlock**. Die Aufrufe sind sequentiell.

---

### Bug 2: Datenverlust in der Windows-Zwischenablage
* **Datei**: `src/injector.py`
* **Problem**: In `_paste_text()` (Zeile 249) wird `_windows_clipboard_get_text()` aufgerufen, um den bisherigen Clipboard-Inhalt zu sichern. Wenn die Zwischenablage vorübergehend gesperrt ist (z.B. durch eine andere App), schlagen alle 5 `OpenClipboard`-Retries fehl und die Funktion gibt `None` zurück (Zeile 194).

  Im `finally`-Block (Zeile 291–311) wird `previous_text is None` als "Zwischenablage war leer/nicht-textuell" interpretiert → die Wiederherstellung wird übersprungen. Da die Zwischenablage anschließend mit dem diktierten Text **überschrieben** wird (Zeile 266), ist der **ursprüngliche Clipboard-Inhalt des Benutzers unwiederbringlich verloren**.

* **Root Cause**: `None` hat zwei verschiedene Bedeutungen:
  1. Die Zwischenablage enthält keinen Text (korrekt)
  2. Der Zugriff auf die Zwischenablage ist fehlgeschlagen (Bug)

* **Vorschlag**: Einführung eines Sentinel-Objekts zur Unterscheidung:

```python
# Am Anfang der Datei:
_CLIPBOARD_ACCESS_FAILED = object()

def _windows_clipboard_get_text(*, retries: int = 5, delay_secs: float = 0.005) -> str | None | object:
    """..."""
    if sys.platform != "win32":
        return None

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(delay_secs)
            continue
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            kernel32.GlobalLock.restype = wintypes.LPVOID
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    logger.warning("Clipboard access failed after retries – returning sentinel")
    return _CLIPBOARD_ACCESS_FAILED   # <-- NEU: Sentinel statt None
```

Anpassung in `_paste_text()`:

```python
def _paste_text(text: str, *, skip_clipboard_restore: bool = False) -> bool:
    # ...
    previous_text = None if skip_clipboard_restore else _windows_clipboard_get_text()

    # NEU: Abbruch wenn Clipboard-Zugriff fehlgeschlagen
    if previous_text is _CLIPBOARD_ACCESS_FAILED:
        logger.warning("Clipboard access failed – aborting paste to protect user data")
        return False

    if not _windows_clipboard_set_text(text):
        return False
    # ... Rest bleibt gleich ...
```

---

### Bug 3: Verwaiste Subprozesse (`yt-dlp` / `ffmpeg`) bei Task-Abbruch
* **Datei**: `src/youtube_download.py`
* **Problem**: An mehreren Stellen werden `asyncio.create_subprocess_exec()`-Subprozesse gestartet und auf `proc.communicate()` gewartet:
  - Zeile 76–91: `_has_video_stream()` startet `ffprobe`
  - Zeile 104–122: `_extract_audio_track()` startet `ffmpeg`
  - Zeile 332–337: Subprocess-Fallback startet `yt-dlp`

  Wird ein Task abgebrochen (Timeout, User-Abbruch), wirft `proc.communicate()` eine `CancelledError`. Der Subprozess wird **nicht** automatisch beendet – er läuft als Zombie-Prozess weiter und verbraucht CPU, Bandbreite und Festplattenspeicher.

* **Vorschlag**: Jede `proc.communicate()`-Stelle in ein `try/except CancelledError`-Muster einwickeln:

```python
# Beispiel für _extract_audio_track() (gleiche Absicherung an allen 3 Stellen):
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", str(source_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "64k",
        str(target_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
```

---

## ~~Ehemaliger Bug 3 (ENTFERNT): Dateileck bei asyncio.CancelledError in web_api.py~~

> **Hinweis**: Dieser Bug war im ursprünglichen Plan enthalten, wurde aber bei der Gegenprüfung als **bereits korrekt implementiert** erkannt. Details:
> - `_schedule_youtube_job._runner()` fängt `CancelledError` ab (Zeile 1089) und setzt `rec.status = "stopped"` (Zeile 1091)
> - `_schedule_file_job._runner()` fängt `CancelledError` ab (Zeile 1122) und setzt `rec.status = "stopped"` (Zeile 1124)
> - Die `finally`-Blöcke in `_run_youtube_transcription` (Zeile 2080–2083) und `_run_file_transcription` (Zeile 2368) löschen temporäre Dateien korrekt, da `rec.status` niemals bei `"processing"` stehen bleibt.

---

## Vorgeschlagene Dateiänderungen

### Component: Device Monitoring

#### [MODIFY] `src/device_monitor.py`
- ✅ `_enumerate_microphones()`: Gesamten Rumpf ab `sd.query_devices()` unter `with get_device_guard_lock():` gestellt
- ✅ PortAudio-Refresh bei aktivem Stream wird nicht wiederholt alle 0,5s rescheduled, sondern bis idle deferred

---

### Component: Text Injection

#### [MODIFY] `src/injector.py`
- Neues Modul-Level Sentinel `_CLIPBOARD_ACCESS_FAILED` hinzufügen
- `_windows_clipboard_get_text()` (Zeile 163–194): Rückgabe von `_CLIPBOARD_ACCESS_FAILED` statt `None` bei fehlgeschlagenem Zugriff
- `_paste_text()` (Zeile 249–311): Prüfung auf `_CLIPBOARD_ACCESS_FAILED` vor dem Überschreiben der Zwischenablage; bei Zugriffsfehler Abbruch statt stilles Datenlöschen

---

### Component: YouTube Download

#### [MODIFY] `src/youtube_download.py`
- `_has_video_stream()` (Zeile 76–91): `proc.communicate()` in `try/except CancelledError` einwickeln, bei Abbruch `proc.kill()` aufrufen
- `_extract_audio_track()` (Zeile 104–128): Gleiches Muster
- Subprocess-Fallback (Zeile 332–337): Gleiches Muster

---

## Verifikationsplan

### Automatisierte Tests
- Gesamte Testsuite: `venv\Scripts\pytest`
- Spezifisch: `pytest tests/test_injector_paste.py` (Clipboard-Sentinel-Logik)
- Spezifisch: `pytest tests/test_web_api_reliability.py` (wird nach Device-Monitor-Fix stabil ohne Segfault durchlaufen)

### Manuelle Verifikation
1. **Zwischenablage**: Großen Text in die Zwischenablage kopieren → Diktatfunktion starten → verifizieren, dass Clipboard-Inhalt nach Beendigung erhalten bleibt
2. **Subprozess-Cleanup**: YouTube-Download starten → Task abbrechen → im Task-Manager prüfen, ob alle `yt-dlp.exe`/`ffmpeg.exe`-Instanzen sofort beendet wurden
