# Repository Guidelines

## Project Structure & Modules
- `src/`: App code. Key modules: `main.py` (entrypoint/UI wiring), `pipeline.py` (STT pipeline orchestration), `injector.py` (keyboard/GUI text injection), `config.py` (env-driven settings), `microphone.py` (fallback audio input), `ui.py` (Tkinter UI), `gemini_transcribe.py` (Gemini helper).
- `tests/`: Pytest-compatible tests (`test_config.py`, `test_injector.py`) plus `conftest.py` for path setup.
- Root scripts: `start.bat` (Windows bootstrap), `start.sh` (Linux/macOS bootstrap), `check_imports.py` (quick dependency presence check).

## Build, Test, and Development Commands
- `python -m venv venv && venv\\Scripts\\activate` (Windows) or `source venv/bin/activate` (Unix) — create/activate env.
- `pip install -r requirements.txt` — install runtime + test deps.
- `python -m src.main` — run the app (same as `start.bat`/`start.sh` after setup).
- `pytest` — run tests (auto-discovers under `tests/`).
- `python check_imports.py` — sanity-check STT provider imports.

## Coding Style & Naming
- Python 3.10+, PEP 8 with 4-space indents; prefer type hints as seen in `main.py` and `pipeline.py`.
- Modules and files use lowercase with underscores; classes in `CapWords`; functions/vars snake_case.
- Logging via `loguru`; keep user-facing messages concise and actionable.
- Avoid hard-coding secrets; read from `Config`/environment or `.env`.

## Testing Guidelines
- Framework: `pytest` (tests can use `asyncio` + `pytest-asyncio` if expanded).
- Naming: place tests in `tests/`, files start with `test_`, functions `test_*`.
- Mocks: prefer `unittest.mock.patch` for `keyboard`/`pyautogui` to avoid GUI side effects.
- Add coverage for new STT services by exercising `_create_stt_service` branches with env vars.

## Commit & Pull Request Guidelines
- Commit messages in repo trend toward imperative summaries with optional type prefix (`refactor: Improve robustness...`); keep under ~72 chars.
- PRs: include problem statement, summary of changes, and manual/automated test results (`pytest`, manual hotkey check). Link related issue if available and note any new env vars or hotkeys. Screenshots are helpful when UI changes (`ui.py`).

## Configuration & Security
- Secrets loaded from `.env`; keys listed in `README.md`/`start` scripts (`SONIOX_API_KEY`, `OPENAI_API_KEY`, etc.). Never commit `.env`.
- Default hotkey `ctrl+alt+s`, mode `toggle`; adjust via env or UI and ensure docs reflect changes when altering defaults.
- When adding new providers, register key names in `Config.SERVICE_API_KEY_MAP` and surface user-friendly labels in `SERVICE_LABELS`.
