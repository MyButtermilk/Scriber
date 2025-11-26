import asyncio
import signal
import sys
import threading
from typing import Optional

from loguru import logger

try:
    import keyboard
    HAS_KEYBOARD = True
except Exception as e:
    logger.warning(f"Keyboard hotkeys not available: {e}")
    keyboard = None
    HAS_KEYBOARD = False

from src.config import Config
from src.pipeline import ScriberPipeline
from src.ui import ScriberUI

loop: Optional[asyncio.AbstractEventLoop] = None
pipeline: Optional[ScriberPipeline] = None
pipeline_task: Optional[asyncio.Task] = None
ptt_task: Optional[asyncio.Future] = None
is_listening: bool = False
ui: Optional[ScriberUI] = None

def handle_status(status: str):
    if ui:
        ui.update_status(status)

def _ensure_loop():
    global loop
    if loop is None:
        loop = asyncio.new_event_loop()
    return loop

def _run_event_loop():
    asyncio.set_event_loop(_ensure_loop())
    loop.run_forever()

def _start_background_loop():
    thread = threading.Thread(target=_run_event_loop, daemon=True)
    thread.start()
    return thread

def _on_pipeline_done(task: asyncio.Task):
    global is_listening, pipeline, pipeline_task
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(f"Pipeline error: {exc}")
        handle_status("Error")
    is_listening = False
    pipeline = None
    pipeline_task = None

async def start_listening():
    global pipeline, pipeline_task, is_listening
    if is_listening:
        return
    try:
        pipeline = ScriberPipeline(service_name=Config.DEFAULT_STT_SERVICE, on_status_change=handle_status)
        pipeline_task = asyncio.create_task(pipeline.start())
        pipeline_task.add_done_callback(_on_pipeline_done)
        is_listening = True
        handle_status("Listening")
    except (ValueError, ImportError) as e:
        logger.error(f"Configuration error: {e}")
        handle_status(f"Error: {e}")
    except Exception as e:
        logger.error(f"Failed to start listening: {e}")
        handle_status("Error")

async def stop_listening():
    global pipeline, pipeline_task, is_listening
    if not is_listening:
        return
    try:
        if pipeline:
            await pipeline.stop()
        if pipeline_task:
            pipeline_task.cancel()
            try:
                await pipeline_task
            except asyncio.CancelledError:
                pass
        handle_status("Stopped")
    finally:
        is_listening = False
        pipeline = None
        pipeline_task = None

async def toggle_listening():
    if is_listening:
        await stop_listening()
    else:
        await start_listening()

async def _ptt_loop():
    last_state = False
    while True:
        try:
            is_pressed = keyboard.is_pressed(Config.HOTKEY)
            if is_pressed and not last_state:
                await start_listening()
            elif not is_pressed and last_state:
                await stop_listening()
            last_state = is_pressed
        except Exception:
            pass
        await asyncio.sleep(0.05)

def register_hotkey():
    global ptt_task
    if not HAS_KEYBOARD:
        logger.warning("Hotkeys disabled (keyboard module missing or headless env).")
        return

    # Some keyboard builds lack internal hotkey sets; create stubs to avoid attribute errors.
    try:
        listener = getattr(keyboard, "_listener", None)
        if listener:
            if not hasattr(listener, "blocking_hotkeys"):
                listener.blocking_hotkeys = set()
            if not hasattr(listener, "nonblocking_hotkeys"):
                listener.nonblocking_hotkeys = set()
            if not hasattr(listener, "nonblocking_keys_pressed"):
                listener.nonblocking_keys_pressed = set()
    except Exception:
        logger.warning("Keyboard listener is missing; hotkeys may be unavailable.")
        return

    if not hasattr(keyboard, "add_hotkey") or not hasattr(keyboard, "clear_all_hotkeys"):
        logger.warning("Keyboard hotkey methods unavailable; skipping hotkey registration.")
        return

    if ptt_task and not ptt_task.cancelled():
        ptt_task.cancel()
        ptt_task = None

    try:
        keyboard.clear_all_hotkeys()
        if Config.MODE == "push_to_talk":
            ptt_task = asyncio.run_coroutine_threadsafe(_ptt_loop(), loop)
            logger.info(f"Push-to-Talk active: {Config.HOTKEY}")
        else:
            keyboard.add_hotkey(Config.HOTKEY, lambda: asyncio.run_coroutine_threadsafe(toggle_listening(), loop))
            logger.info(f"Hotkey registered: {Config.HOTKEY} (Toggle)")
    except Exception as exc:
        logger.error(f"Failed to register hotkey: {exc}")

def save_settings():
    Config.set_default_service(ui.service_var.get())
    Config.set_api_key(ui.service_var.get(), ui.api_key_var.get())
    Config.set_hotkey(ui.hotkey_var.get())
    Config.set_mode(ui.mode_var.get())
    Config.set_soniox_mode(ui.soniox_mode_var.get())
    Config.set_debug(ui.debug_var.get())
    Config.CUSTOM_VOCAB = ui.custom_vocab_var.get().strip()
    # Persist current settings to .env so they are remembered.
    Config.persist_to_env_file(".env")

    register_hotkey()
    ui.update_status("Settings saved")

async def shutdown():
    if ptt_task:
        ptt_task.cancel()
    await stop_listening()
    if loop and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

def start_from_ui():
    save_settings()
    asyncio.run_coroutine_threadsafe(start_listening(), loop)

def stop_from_ui():
    asyncio.run_coroutine_threadsafe(stop_listening(), loop)

def _handle_exit():
    logger.info("Exiting application...")
    future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
    try:
        future.result(timeout=2)
    except Exception:
        pass
    if ui:
        ui.root.quit()

def main():
    global ui, loop

    # Avoid duplicate log lines if main is invoked multiple times; start with a clean sink list.
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.info("Scriber - Voice Dictation")

    loop = _ensure_loop()
    _start_background_loop()

    ui = ScriberUI(
        on_start=start_from_ui,
        on_stop=stop_from_ui,
        on_save_settings=save_settings,
    )
    register_hotkey()

    signal.signal(signal.SIGINT, lambda *_: _handle_exit())
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, lambda *_: _handle_exit())

    ui.root.protocol("WM_DELETE_WINDOW", _handle_exit)
    ui.run()

if __name__ == "__main__":
    main()
