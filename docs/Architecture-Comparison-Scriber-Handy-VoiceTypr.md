# Architekturvergleich: Scriber vs Handy vs VoiceTypr

Stand: 2026-02-09  
Ziel: Architekturentscheidungen der drei Projekte vergleichen und daraus konkrete, spaeter umsetzbare Verbesserungsoptionen fuer Scriber ableiten (ohne aktuelle Codeaenderung).

## Beste Option je Projekt (Kurzfassung)

| Projekt | Aktuell staerkste Option | Warum relevant fuer Scriber |
|---|---|---|
| **Scriber** | Sehr breite STT-Provider-Unterstuetzung + flexible Mic-Aufloesung inkl. Favorit und Name-Normalisierung (`src/pipeline.py:577`, `src/audio_devices.py:43`, `src/audio_devices.py:197`) | Gute Basis fuer unterschiedliche Nutzer-Setups und Provider-Wechsel |
| **Handy** | Stark typisierte Settings + klare Manager-Architektur + generierte TS-Bindings (`tmp_handy_repo/src-tauri/src/settings.rs:246`, `tmp_handy_repo/src-tauri/src/lib.rs:118`, `tmp_handy_repo/src-tauri/src/lib.rs:240`) | Wartbarkeit, weniger Drift zwischen Frontend und Backend |
| **VoiceTypr** | Robustheit im Runtime-Flow: State Machine, Device-Watcher, Event-Routing, explizite Caches (`tmp_voicetypr_repo/src-tauri/src/state_machine.rs:24`, `tmp_voicetypr_repo/src-tauri/src/audio/device_watcher.rs:50`, `tmp_voicetypr_repo/src/lib/EventCoordinator.ts:16`, `tmp_voicetypr_repo/src-tauri/src/commands/audio.rs:204`) | Stabileres Verhalten bei Race Conditions, Disconnects und Multi-Window-Events |

## Vergleichstabelle mit "Best Option"

| Thema | Scriber (Ist) | Handy | VoiceTypr | Beste Option | Verbesserungsansatz fuer Scriber |
|---|---|---|---|---|---|
| **1. Mikrofon-Liste / Duplikate** | Dedup ueber aktive Host-API + Name-Normalisierung + Filter (`src/audio_devices.py:91`, `src/audio_devices.py:141`, `src/audio_devices.py:197`, `src/web_api.py:1559`) | Listet ueber `cpal::default_host()` (einfach, stabil) (`tmp_handy_repo/src-tauri/src/audio_toolkit/audio/device.rs:10`) | Ebenfalls `cpal::default_host()` (`tmp_voicetypr_repo/src-tauri/src/audio/recorder.rs:468`) | **Scriber** (wegen dedup + Favoriten-Matching) | Beibehalten. Optional: kleines Hardware-Regression-Set (Dock an/ab, USB an/ab) automatisieren, damit die Duplikat-Qualitaet langfristig stabil bleibt. |
| **2. Favorite-Mic / Fallback** | Favorit wird priorisiert, sonst selected/default (`src/pipeline.py:577`, `src/web_api.py:1364`) | Selected/Default + clamshell-spezifische Auswahl (`tmp_handy_repo/src-tauri/src/managers/audio.rs:186`) | Selected + Auto-Reset auf Default bei Device-Removal ausserhalb Recording (`tmp_voicetypr_repo/src-tauri/src/audio/device_watcher.rs:115`) | **Hybrid (Scriber + VoiceTypr)** | Favorite-Logik aus Scriber behalten, aber mit Hintergrund-Device-Watcher ergaenzen (Disconnect-Handling nicht nur beim naechsten Start). |
| **3. Device-Change-Watcher** | Kein dedizierter Watcher fuer Mic-Liste/Fallback im laufenden Betrieb | Kein vergleichbarer dedizierter Device-Watcher gefunden | Deferred gestarteter Watcher mit Permission/Onboarding-Gating (`tmp_voicetypr_repo/src-tauri/src/audio/device_watcher.rs:13`) | **VoiceTypr** | In Scriber einen schlanken `DeviceWatcher` einfuehren: Device-Liste diffen, UI benachrichtigen, wenn Favorite/Selected verschwindet auf `default` fallen (recording-aware). |
| **4. Recording-State-Modell** | Mehrere Flags in zentralem Controller (`src/web_api.py:407`, `src/web_api.py:408`) | Manager mit klaren Modes/State (`tmp_handy_repo/src-tauri/src/managers/audio.rs:110`) | Explizite State-Machine + validierte Transitionen + Fallback (`tmp_voicetypr_repo/src-tauri/src/state_machine.rs:40`, `tmp_voicetypr_repo/src-tauri/src/state/unified_state.rs:72`) | **VoiceTypr** | `RecordingStateMachine` in Scriber einfuehren, Flags ersetzen, ungueltige Transitionen aktiv blocken/loggen. |
| **5. Settings-Datenmodell & Migration** | Globale `Config` + `.env` Persistenz (`src/config.py:31`, `src/config.py:316`) | Typisierte `AppSettings` + Defaults + Merge alter Settings (`tmp_handy_repo/src-tauri/src/settings.rs:246`, `tmp_handy_repo/src-tauri/src/settings.rs:661`) | Typisierte Settings + Legacy-Migration fuer einzelne Keys (`tmp_voicetypr_repo/src-tauri/src/commands/settings.rs:41`, `tmp_voicetypr_repo/src-tauri/src/commands/settings.rs:74`) | **Handy** | Bei Scriber ein versioniertes Settings-Objekt einfuehren (z. B. dataclass + schema version), `.env` nur noch fuer Secrets/Overrides nutzen. |
| **6. Frontend Settings-Architektur** | Sehr grosse Seite mit vielen lokalen States/Fetches (`Frontend/client/src/pages/Settings.tsx:79`, `Frontend/client/src/pages/Settings.tsx:172`) | Zentraler Zustandsspeicher (Zustand), optimistische Updates + Rollback (`tmp_handy_repo/src/stores/settingsStore.ts:132`, `tmp_handy_repo/src/stores/settingsStore.ts:245`) | SettingsContext mit optimistischen Updates + Events (`tmp_voicetypr_repo/src/contexts/SettingsContext.tsx:16`, `tmp_voicetypr_repo/src/contexts/SettingsContext.tsx:44`) | **Handy** | Settings in Scriber aus `Settings.tsx` in Store/Hooks auslagern (`useSettingsStore`, `useAudioDevices`, `useApiKeys`) und UI-Komponenten schlank halten. |
| **7. Event-Routing (Multi-Window / Overlay)** | WebSocket + direkte Overlay-Callbacks im Controller (`src/web_api.py:588`) | Weniger ausgepraegtes Event-Routing | Zentrales EventCoordinator-Routing mit Regeln + Cleanup (`tmp_voicetypr_repo/src/lib/EventCoordinator.ts:50`, `tmp_voicetypr_repo/src/lib/EventCoordinator.ts:143`) | **VoiceTypr** | In Scriber Event-Bus-Regeln definieren (z. B. was in Overlay vs Main geht), um doppelte/inkonsistente Event-Handler zu vermeiden. |
| **8. Laufzeit-Caches** | Analyzer-/Transkript-Optimierungen vorhanden (`src/pipeline.py:956`, `src/web_api.py:457`) | Teilweise Manager-intern | Explizite RecordingConfig- und License-Caches mit Invalidation (`tmp_voicetypr_repo/src-tauri/src/commands/audio.rs:204`, `tmp_voicetypr_repo/src-tauri/src/commands/audio.rs:466`, `tmp_voicetypr_repo/src-tauri/src/state/app_state.rs:56`) | **VoiceTypr** | In Scriber klaren Cache-Layer definieren (TTL + Invalidation-Hooks bei Settings-Aenderung), statt impliziter Streuung. |
| **9. API/Command-Vertrag Frontend<->Backend** | Dynamische REST-JSON-Vertraege (`src/web_api.py:2357`) | Generierte TS-Bindings aus Backend-Commands (`tmp_handy_repo/src-tauri/src/lib.rs:240`, `tmp_handy_repo/src-tauri/src/lib.rs:328`) | Commands ohne generierte TS-Typen | **Handy** | Fuer Scriber API-Schema zentral typisieren (z. B. gemeinsames TypeScript-Schema/OpenAPI), um Drift bei Settings/Responses zu reduzieren. |
| **10. Testtiefe** | Gute Kern-Tests inkl. Mic-Resolution/Favoriten (`tests/test_microphone_device_resolution.py:67`, `tests/test_microphone_device_resolution.py:142`) | Eher punktuelle Inline-Tests (`tmp_handy_repo/src-tauri/src/managers/model.rs:1097`) | Breites Backend- + Frontend-Testset inkl. Regression/Error Events (`tmp_voicetypr_repo/src-tauri/src/tests/regression_tests.rs`, `tmp_voicetypr_repo/src-tauri/src/tests/error_event_tests.rs`, `tmp_voicetypr_repo/src/components/tabs/SettingsTab.test.tsx`) | **VoiceTypr** | Scriber um Integrations-Tests fuer Device-Disconnect, Race-Conditions und Settings-Drift erweitern (Backend + Frontend). |
| **11. Provider-Erweiterbarkeit** | Viele Provider, aber Erweiterung an mehreren Stellen (`src/config.py:73`, `src/config.py:90`, `src/pipeline.py:737`) | Eher fokussierter Scope | Mehrere Engines, aber zentrale `commands/audio.rs` sehr gross (`tmp_voicetypr_repo/src-tauri/src/commands/audio.rs:648`) | **Scriber (Funktionalitaetsbreite)** | Registry-Pattern fuer STT-Provider einfuehren (ein Ort fuer Label/API-Key/Factory), damit neue Provider mit weniger Touchpoints integrierbar sind. |

## Priorisierte Verbesserungs-Roadmap fuer Scriber (nur Ideen)

### Phase 1 (niedriges Risiko, hoher Nutzen)

1. **Settings-Store im Frontend extrahieren**  
   Ziel: `Settings.tsx` entlasten, weniger Seiteneffekte, einfachere Tests.
2. **Device-Watcher einbauen (recording-aware)**  
   Ziel: Sauberes Runtime-Handling bei Dock/USB-Disconnect inklusive Favorite-Fallback.
3. **API-Typen zentralisieren**  
   Ziel: Frontend/Backend-Drift reduzieren.

### Phase 2 (mittleres Risiko, sehr hoher Stabilitaetsgewinn)

1. **Recording-State-Machine einfuehren**  
   Ziel: Ungueltige State-Transitions vermeiden, Race-Conditions reduzieren.
2. **Runtime-AppState kapseln**  
   Ziel: Flags/Locks/Caches aus verstreuten Stellen in ein zentrales Modell bringen.

### Phase 3 (strukturelle Verbesserungen)

1. **`src/web_api.py` nach Domainen aufteilen**  
   Vorschlag: `settings_api`, `mic_api`, `transcript_api`, `recording_api`.
2. **STT-Provider-Registry**  
   Ziel: Neue Provider ohne Mehrfachanpassungen in Config + Pipeline.
3. **Explizite Cache-Policies (TTL + Invalidation)**  
   Ziel: Berechenbare Performance bei weniger inkonsistentem Runtime-Verhalten.

## Umsetzungshinweise (wichtig fuer spaeter)

1. Favorite-Mic-Verhalten muss erhalten bleiben (UI-Versprechen: "immer verwenden, wenn verfuegbar").
2. Mic-Dedup-Logik nicht gegen reine Namensfilter vereinfachen; Host-API-Priorisierung ist der Kern gegen Duplikate.
3. Refactoring zuerst in separaten Schritten mit Stabilitaets-Tests (Device-Disconnect, schneller Start/Stop, Hotkey-Stress).

## Groesste Hebel fuer Scriber

1. **State-Machine + Device-Watcher** (Stabilitaet)
2. **Frontend-Store statt monolithischer Settings-Seite** (Wartbarkeit)
3. **Typisierte API-Vertraege + Provider-Registry** (Erweiterbarkeit)
