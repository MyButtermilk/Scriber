# Pipeline Verbesserungen (Vorschlaege)

Ziel: Pipeline robuster machen, speziell bei schnellem Hotkey-Toggling und instabilen STT-Verbindungen.

## Bereits umgesetzt

- Session-ID in allen Live-Mic WebSocket-Events; UI ignoriert stale Events.
- Lock-Scope reduziert (Broadcasts nicht mehr unter dem Lock).
- Emergency-Stop korrigiert (richtige Session-Cleanup).

## Offene Vorschlaege (noch nicht umgesetzt)

- Single-flight Start/Stop: Nur eine Start/Stop-Operation gleichzeitig zulassen; weitere Toggles werden serialisiert oder verworfen.
- Expliziter Zustandsautomat: Idle -> Starting -> Recording -> Stopping -> Transcribing; verhindert widerspruechliche States.
- Start-fertig Signal: Handshake, ob Mic wirklich bereit ist; verhindert Overlay/State-Fehler bei schnellem Stop.
- Hard-Timeout fuer Stop: Wenn STT haengt, nach Timeout hart canceln und UI resetten.
- Bessere Fehlerkategorien: Network/Auth/Audio/Service klar unterscheiden, damit UI bessere Meldungen zeigt.
- Hotkey-Stress-Test: Automatisierter Test (z.B. 10x Start/Stop in kurzer Zeit) zur Race-Condition-Pruefung.
