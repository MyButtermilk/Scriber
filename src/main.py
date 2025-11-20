import asyncio
import sys
import signal
from loguru import logger
import keyboard

from src.config import Config
from src.pipeline import ScriberPipeline

# Global state
pipeline = None
is_listening = False
loop = None

def handle_status(status):
    print(f"STATUS: {status}")

async def start_listening():
    global is_listening, pipeline
    if is_listening:
        return

    logger.info("Starting Listening...")
    if not pipeline:
        pipeline = ScriberPipeline(service_name=Config.DEFAULT_STT_SERVICE, on_status_change=handle_status)

    asyncio.create_task(pipeline.start())
    is_listening = True

async def stop_listening():
    global is_listening, pipeline
    if not is_listening:
        return

    logger.info("Stopping Listening...")
    if pipeline:
        await pipeline.stop()
        # We might want to clear pipeline instance to ensure fresh start
        pipeline = None
    is_listening = False

async def toggle_listening():
    if is_listening:
        await stop_listening()
    else:
        await start_listening()

def on_hotkey_toggle():
    """Called when hotkey is pressed (Toggle Mode)."""
    if loop:
        asyncio.run_coroutine_threadsafe(toggle_listening(), loop)

def on_hotkey_press(e):
    """Called when key is pressed (PTT Mode)."""
    # Note: keyboard.on_press passes an event
    if loop and not is_listening:
         asyncio.run_coroutine_threadsafe(start_listening(), loop)

def on_hotkey_release(e):
    """Called when key is released (PTT Mode)."""
    if loop and is_listening:
         asyncio.run_coroutine_threadsafe(stop_listening(), loop)

async def main():
    global loop, pipeline
    loop = asyncio.get_running_loop()

    logger.info("Scriber - Windows Voice Dictation")
    logger.info(f"Service: {Config.DEFAULT_STT_SERVICE}")
    logger.info(f"Hotkey: {Config.HOTKEY}")
    logger.info(f"Mode: {Config.MODE}")

    # Register Hotkey
    try:
        if Config.MODE == "push_to_talk":
            logger.info("Push-to-Talk Mode enabled. Hold hotkey to speak.")
            # keyboard.add_hotkey detects the combo, but for PTT we need press/release of that combo?
            # Standard hotkeys trigger once.
            # To support PTT for a combo like "ctrl+alt+s" is tricky with `keyboard`.
            # Easier for single keys.
            # But `keyboard.add_hotkey` accepts a callback.
            # We can use `keyboard.is_pressed` loop or hooks.
            # A simple approach for PTT with `keyboard` is hooking the specific key if it's a single key.
            # If it's a combo, "holding a combo" is ambiguous (do you hold s while holding ctrl+alt?).
            # Let's implement a hook that checks `keyboard.is_pressed(Config.HOTKEY)`.

            # Better PTT approach:
            # Use `add_hotkey` to START, and wait for release? No, that blocks.
            # We will stick to the simple logic:
            # If user configured PTT, we assume they might use a single key (e.g. 'f9', 'caps lock', 'space').
            # If they use a combo, we'll try to detect release.

            # However, a robust PTT usually involves:
            # on_press(key) -> if key == HOTKEY -> start
            # on_release(key) -> if key == HOTKEY -> stop

            # Since parsing 'ctrl+alt+s' to key events is complex,
            # we will implement a simplified PTT:
            # Using `keyboard.on_press_key` and `keyboard.on_release_key` requires the scan code or name.
            # If HOTKEY is a complex string, `add_hotkey` is best.
            # Let's try to use a polling loop for PTT as a fallback, or just `is_pressed`.

            # Polling approach for PTT (Robust for combos)
            async def ptt_loop():
                last_state = False
                while True:
                    try:
                        is_pressed = keyboard.is_pressed(Config.HOTKEY)
                        if is_pressed and not last_state:
                            # Pressed
                            await start_listening()
                        elif not is_pressed and last_state:
                            # Released
                            await stop_listening()
                        last_state = is_pressed
                    except Exception:
                        pass
                    await asyncio.sleep(0.05) # 50ms poll

            asyncio.create_task(ptt_loop())

        else:
            # Toggle Mode
            keyboard.add_hotkey(Config.HOTKEY, on_hotkey_toggle)

        logger.info("Hotkey registered successfully.")
    except ImportError:
         logger.error("Keyboard module not functioning (likely Linux/Headless). Hotkey disabled.")
    except Exception as e:
         logger.error(f"Failed to register hotkey: {e}")

    # Keep alive
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Exiting...")
        if pipeline:
            await pipeline.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
