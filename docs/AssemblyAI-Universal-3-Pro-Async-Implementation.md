# AssemblyAI Universal-3-Pro Async Implementation

## Zielbild
Scriber nutzt AssemblyAI nur noch im **Async/Pre-recorded-Modus** (kein WebSocket-Streaming).

- Provider-ID bleibt: `assemblyai`
- Modell: `speech_models: ["universal-3-pro"]`
- **Keyterms Prompting immer aktiv**, wenn `customVocab` gültige Begriffe enthält
- Live Mic: `speaker_labels=false`
- File/YouTube: `speaker_labels=true`

## Verhalten nach Workflow

## Live Mic (`/api/live-mic/start`)
- Pipeline nutzt `AssemblyAIUniversal3ProAsyncProcessor`
- Audio wird lokal gepuffert und erst beim Stop/End hochgeladen
- Ergebnis kommt als **finales Transkript nach Stop**
- Keine Interim-Transkripte

## File + YouTube (`/api/file/transcribe`, `/api/youtube/...`)
- Direkter Upload zur Pre-recorded API
- Diarization aktiv (`speaker_labels=true`)
- `utterances` werden auf Scriber-Format gemappt:
  - `[Speaker 1]: ...`
  - `[Speaker 2]: ...`

## Payload-Beispiele

## Live Mic (Async final bei Stop)
```json
{
  "audio_url": "https://assemblyai-upload-url",
  "speech_models": ["universal-3-pro"],
  "speaker_labels": false,
  "keyterms_prompt": ["Scriber", "Pipecat"],
  "language_detection": true
}
```

## File/YouTube (mit Speaker Labels)
```json
{
  "audio_url": "https://assemblyai-upload-url",
  "speech_models": ["universal-3-pro"],
  "speaker_labels": true,
  "keyterms_prompt": ["Scriber", "Pipecat"],
  "language_code": "de"
}
```

## Sprachregeln (U3-Pro)
Unterstützte manuelle Sprachen:
- `en`
- `es`
- `pt`
- `fr`
- `de`
- `it`

Regeln:
- `Config.LANGUAGE == "auto"` -> `language_detection=true`
- Unterstützte manuelle Sprache -> `language_code=<lang>`
- Nicht unterstützte manuelle Sprache (z. B. `nl`) -> Fallback auf `language_detection=true` + Warn-Log

## Keyterms Prompting
Quelle: `Config.CUSTOM_VOCAB`

Sanitizing:
- Split per Komma/Newline/Semikolon
- Trim + Dedupe (case-insensitive)
- Maximal 1000 Einträge
- Maximal 6 Wörter pro Eintrag
- Ungültige Einträge werden verworfen

Wenn keine gültigen Begriffe übrig bleiben:
- `keyterms_prompt` wird nicht gesendet
- Warnung wird geloggt
