# Windows-Mikrofon auf 0 %: Loesungsoptionen fuer Scriber

Stand: 19. Februar 2026

## Kurzempfehlung

Die sinnvollste Reihenfolge ist:
1. Sofort in der UI handlungsfaehige Warnungen mit Klick-Pfaden zu Windows liefern.
2. Danach die Warnung technisch auf eine direkte Windows-Endpunktpruefung (Mute/Volume) erweitern.
3. Optional spaeter Auto-Fix als Opt-in.

So gibt es schnell Nutzwert ohne hohes Implementierungsrisiko.

## Problem

Wenn das aktive Windows-Mikrofon auf `0 %` Eingangslautstaerke steht (oder gemutet ist), sieht der Nutzer in Scriber einen scheinbar normalen Startzustand (Mikrofon an, LED aktiv), bekommt aber keinen verwertbaren Audio-Input. Das fuehlt sich wie ein "silent failure" an.

## Ist-Zustand in Scriber

### Backend (Python)

- `src/web_api.py` nutzt aktuell eine reine RMS-Logik:
  - `_update_input_warning()` prueft `_mic_low_rms_threshold`, `_mic_low_rms_clear_threshold` und `_mic_low_rms_warn_after_secs`.
  - Bei dauerhaft niedrigem RMS wird eine generische Warnung gesetzt.
- `_set_input_warning()` broadcastet `input_warning` Events.
- `get_state()` und `status_event` transportieren `inputWarning` als String.

### WebSocket-Contract

- `src/core/ws_contracts.py` definiert `input_warning_event(active, message, session_id)`.
- Es gibt aktuell keine strukturierten Felder wie `code`, `severity` oder `actions`.

### Frontend

- `Frontend/client/src/pages/LiveMic.tsx` zeigt `inputWarning` als reines Text-Banner.
- Keine CTA-Buttons, keine Deep-Links zu Windows-Einstellungen.

### Device-Monitor

- `src/device_monitor.py` nutzt `pycaw` fuer MMDevice-Notifications (z. B. Device-Wechsel).
- Volume/Mute-Callbacks auf Endpunkt-Ebene sind noch nicht implementiert.

## Recherchierte Fakten (extern)

1. Windows nennt Input-Lautstaerke explizit als Troubleshooting-Punkt.  
Quelle: https://support.microsoft.com/en-us/windows/fix-microphone-problems-5f230348-106d-bfa4-1db5-336f35576011

2. `ms-settings:` URIs fuer Sound/Mikrofon sind dokumentiert, u. a.:
- `ms-settings:sound`
- `ms-settings:sound-defaultinputproperties`
- `ms-settings:privacy-microphone`  
Quelle: https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings-app

3. Die URI-Startlogik gilt auch fuer Desktop-Apps.  
Quelle: https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings-app

4. Capture-Endpunktstatus ist per Core Audio direkt abfragbar:
- `GetMasterVolumeLevelScalar()` fuer 0.0 bis 1.0
- `GetMute()` fuer Mute-Status  
Quellen:
- https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nn-endpointvolume-iaudioendpointvolume
- https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolume-getmastervolumelevelscalar
- https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolume-getmute

5. Aenderungen an Volume/Mute koennen eventbasiert empfangen werden (`IAudioEndpointVolumeCallback`).  
Quelle: https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolumecallback-onnotify

6. Default-Inputwechsel koennen via `IMMNotificationClient` erkannt werden.  
Quelle: https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immnotificationclient-ondefaultdevicechanged

## Verbesserungsvorschlag (konkret)

### Phase A: Schnell nutzbar (ohne Windows-COM-Neubau)

Ziel: Aus "unklarer Warnung" wird "konkrete Handlung".

Warncodes (Message bleibt als Fallback):
- `mic_level_very_low`
- `mic_endpoint_muted`
- `mic_endpoint_volume_zero`
- `mic_permission_or_privacy`

CTA-Mapping im Frontend:
- `mic_level_very_low` oder `mic_endpoint_volume_zero` -> `ms-settings:sound-defaultinputproperties`
- `mic_permission_or_privacy` -> `ms-settings:privacy-microphone`
- Fallback/zusatzlich -> `ms-settings:sound`

Rueckwaertskompatibilitaet:
- Bestehendes `message`-Feld beibehalten.
- Neues Feld `code` optional einfuehren.
- `inputWarning` String in `status`/`state` vorerst weiter mitliefern.

Empfohlene Dateien:
- `src/core/ws_contracts.py`
- `src/web_api.py`
- `Frontend/client/src/pages/LiveMic.tsx`
- `tests/contract/test_ws_events.py`
- `tests/test_web_api_lifecycle.py`

### Phase B: Eindeutige Erkennung beim Start

Ziel: 0 %- und Mute-Fall sofort erkennen, bevor der Nutzer lange wartet.

1. Neues Modul `src/windows_mic_health.py` anlegen (Windows-only, auf anderen Plattformen no-op).
2. API-Vorschlag:
```python
class MicEndpointHealth(TypedDict):
    ok: bool
    volume_scalar: float | None
    muted: bool | None
    warning_code: str | None
```
3. In `start_live` vor Pipeline-Start einmal pruefen.
4. Bei `muted is True` oder `volume_scalar <= 0.01` sofort `input_warning` mit passendem Code setzen.

### Phase C: Laufende Aktualisierung waehrend Recording

Ziel: Wenn Nutzer waehrend der Aufnahme in Windows regelt, reagiert Scriber sofort.

Option 1 (empfohlen zuerst):
- Polling alle 1 bis 2 Sekunden (einfach, robust, wenig COM-Komplexitaet).

Option 2 (spaeter):
- `IAudioEndpointVolumeCallback` fuer push-basierte Updates.

Hinweis: Polling ist oft der bessere erste Schritt, weil deutlich einfacher zu stabilisieren.

### Phase D: Optionaler Auto-Fix (Opt-in)

Ziel: Frustfall automatisch reduzieren.

1. Setting: `Auto-heal mic volume when 0 %` (Default aus).
2. Wenn aktiv und Endpunkt bei 0: optional Unmute und optional Volume auf z. B. 35 %.
3. Immer mit sichtbarer Rueckmeldung im UI.

## Vorgeschlagener Event-Contract

Minimaler Ausbau ohne Breaking Change:

```json
{
  "type": "input_warning",
  "active": true,
  "message": "Sehr niedriger Eingangspegel.",
  "code": "mic_endpoint_volume_zero",
  "actions": [
    { "id": "open_input_volume", "label": "Eingangslautstaerke oeffnen", "uri": "ms-settings:sound-defaultinputproperties" }
  ]
}
```

Regeln:
- `message` bleibt Pflicht.
- `code` und `actions` sind optional.
- Unbekannte Felder sollen im Frontend ignoriert werden.

## Akzeptanzkriterien

1. Bei 0 % Lautstaerke erscheint in <= 1 s eine spezifische Warnung mit passendem CTA.
2. Bei Mute erscheint `mic_endpoint_muted` (nicht als generischer RMS-Text).
3. Warnung verschwindet automatisch, wenn Nutzer in Windows korrigiert.
4. Auf Nicht-Windows bleibt Verhalten stabil (kein Exception-Pfad).
5. Bestehende RMS-Warnung bleibt als Fallback erhalten.

## Risiken und Gegenmassnahmen

1. Device-Mapping ist nicht immer 1:1. Gegenmassnahme: zuerst auf Default-Capture-Endpunkt arbeiten, spaeter Mapping verbessern.
2. COM-Callbacks koennen je nach Umgebung fragil sein. Gegenmassnahme: erst Polling einbauen, Callback erst als zweite Ausbaustufe.
3. Manche Audio-Treiber blockieren programmatisches Setzen. Gegenmassnahme: Auto-Fix strikt optional und mit sauberem Error-Handling.

## Scope

- In Scope: Windows Live-Mic Aufnahme im Scriber-Backend/Web-UI.
- Out of Scope: macOS/Linux-spezifische Anpassungen.

## Quellen

- Microsoft Support, Microphone troubleshooting:  
https://support.microsoft.com/en-us/windows/fix-microphone-problems-5f230348-106d-bfa4-1db5-336f35576011
- Windows settings URIs:  
https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings-app
- Core Audio endpoint volume interface:  
https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nn-endpointvolume-iaudioendpointvolume
- `GetMasterVolumeLevelScalar`:  
https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolume-getmastervolumelevelscalar
- `GetMute`:  
https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolume-getmute
- `IAudioEndpointVolumeCallback::OnNotify`:  
https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nf-endpointvolume-iaudioendpointvolumecallback-onnotify
- `IMMNotificationClient::OnDefaultDeviceChanged`:  
https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immnotificationclient-ondefaultdevicechanged
