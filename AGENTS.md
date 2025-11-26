# Repository Guidelines

## Project Structure & Modules
- `src/`: App code. Key modules: `main.py` (entrypoint/UI wiring), `pipeline.py` (STT pipeline orchestration), `injector.py` (text injection), `config.py` (env-driven settings), `microphone.py` (audio input), `ui.py` (Tkinter UI), `gemini_transcribe.py` (Gemini helper).
- `tests/`: Pytest-compatible tests (`test_config.py`, `test_injector.py`) plus `conftest.py` for path setup.
- Root scripts: `start.bat` (Windows bootstrap), `start.sh` (Linux/macOS bootstrap), `check_imports.py` (quick dependency presence check).

## Build, Test, and Development Commands
- `python -m venv venv && venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Unix) — create/activate env.
- `pip install -r requirements.txt` — install runtime + test deps.
- `python -m src.main` — run the app (same as `start.bat`/`start.sh` after setup).
- `pytest` — run tests (auto-discovers under `tests/`).
- `python check_imports.py` — sanity-check STT provider imports.

## Coding Style & Naming
- Python 3.10+, PEP 8 with 4-space indents; prefer type hints (see `main.py`, `pipeline.py`).
- Modules/files lowercase with underscores; classes CapWords; functions/vars snake_case.
- Logging via `loguru`; keep user-facing messages concise.
- Never hard-code secrets; read from `Config`/environment or `.env`.

## Testing Guidelines
- Framework: `pytest` (can extend with `pytest-asyncio`).
- Naming: tests in `tests/`, files `test_*.py`, functions `test_*`.
- Mocks: use `unittest.mock.patch` for GUI/keyboard to avoid side effects.
- Add coverage for new STT providers by exercising `_create_stt_service` branches.

## Commit & Pull Request Guidelines
- Commit messages: short, imperative; optional type prefix (`refactor: ...`).
- PRs: include problem statement, summary, tests run (`pytest`, hotkey/manual checks), note new env vars/hotkeys; screenshots for UI tweaks (`ui.py`).

## Configuration & Security
- Secrets in `.env` (`SONIOX_API_KEY`, `OPENAI_API_KEY`, etc.); never commit `.env`.
- Settings persisted via UI to `.env`: hotkey, STT service/mode (realtime/async), language (or auto), debug flag, custom vocab, mic device, mic always-on, API keys.
- Default hotkey `ctrl+alt+s`, mode `toggle`; update docs when changing defaults.
- When adding providers, update `Config.SERVICE_API_KEY_MAP` and `SERVICE_LABELS`; pass language hints if supported.
