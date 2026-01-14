"""
System tray wrapper for Scriber backend.
Shows an icon in the notification area with options to view logs, open browser, and quit.
"""
import sys
import os
import io
import json
import ctypes
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
from pathlib import Path
from collections import deque

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("Error: pystray and Pillow are required. Run: pip install pystray pillow")
    sys.exit(1)

# Log buffer to store recent logs
MAX_LOG_LINES = 500
log_buffer = deque(maxlen=MAX_LOG_LINES)
log_lock = threading.Lock()

# Single instance lock file
LOCK_FILE = Path(__file__).parent.parent / ".scriber.lock"

# Processes
backend_process = None
frontend_process = None
tray_icon = None

# Watchdog settings
BACKEND_CHECK_INTERVAL = 5  # Check every 5 seconds
watchdog_running = True
watchdog_thread = None


def is_process_running(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if sys.platform == 'win32':
        try:
            # Use tasklist to check if process exists
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        # Unix: check /proc or use kill 0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def acquire_single_instance_lock() -> bool:
    """
    Try to acquire single instance lock.
    Returns True if lock acquired (no other instance running).
    Returns False if another instance is already running.
    """
    try:
        if LOCK_FILE.exists():
            # Read existing PID
            try:
                existing_pid = int(LOCK_FILE.read_text().strip())
                if is_process_running(existing_pid):
                    print(f"Scriber is already running (PID {existing_pid})")
                    return False
                else:
                    # Stale lock file, process no longer exists
                    print(f"Removing stale lock file (PID {existing_pid} not running)")
            except (ValueError, OSError):
                # Invalid lock file content, remove it
                pass

        # Write our PID to lock file
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception as e:
        print(f"Warning: Could not manage lock file: {e}")
        return True  # Allow running if lock management fails


def release_single_instance_lock():
    """Release the single instance lock file."""
    try:
        if LOCK_FILE.exists():
            # Only delete if it contains our PID
            try:
                stored_pid = int(LOCK_FILE.read_text().strip())
                if stored_pid == os.getpid():
                    LOCK_FILE.unlink()
            except (ValueError, OSError):
                pass
    except Exception:
        pass


def load_icon():
    """Load the favicon icon for the system tray (High-DPI compatible)."""
    icon_paths = [
        Path(__file__).parent.parent / "Frontend" / "client" / "public" / "favicon.png",
        Path(__file__).parent.parent / "Frontend" / "client" / "public" / "favicon.ico",
    ]
    
    # Use 256x256 for High-DPI displays (Windows 10/11 scale this down automatically)
    # This prevents pixelation on modern displays with 125%, 150%, 200% scaling
    target_size = 256
    
    for icon_path in icon_paths:
        if icon_path.exists():
            try:
                img = Image.open(icon_path)
                # Convert to RGBA if necessary
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                # Use high-quality LANCZOS resampling
                img = img.resize((target_size, target_size), Image.Resampling.LANCZOS)
                return img
            except Exception:
                pass
    
    # Fallback: create a simple colored icon with gradient
    img = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    # Draw a rounded blue square
    margin = target_size // 8
    draw.rounded_rectangle(
        [margin, margin, target_size - margin, target_size - margin],
        radius=target_size // 6,
        fill=(59, 130, 246, 255)  # Blue color matching theme
    )
    return img


# State icons are cached to avoid regenerating them each time
_icon_cache = {}

# App state tracking
_current_app_state = "idle"  # idle, recording, processing
_state_lock = threading.Lock()


def _add_state_indicator(base_img: Image.Image, state: str) -> Image.Image:
    """Add a colored indicator dot to the icon based on state."""
    if state == "idle":
        return base_img  # No indicator for idle state
    
    # Create a copy to avoid modifying the original
    img = base_img.copy()
    draw = ImageDraw.Draw(img)
    
    # Calculate indicator size and position (bottom-right corner)
    size = img.width
    indicator_radius = size // 6  # ~17% of icon size
    padding = size // 16  # Padding from edges
    
    # Position in bottom-right
    center_x = size - indicator_radius - padding
    center_y = size - indicator_radius - padding
    
    # Choose color based on state
    if state == "recording":
        # Red dot for recording (pulsing would be nice but static is fine for tray)
        fill_color = (220, 38, 38, 255)  # Bright red
        outline_color = (255, 255, 255, 255)  # White outline for visibility
    elif state == "processing":
        # Orange dot for processing/transcribing
        fill_color = (245, 158, 11, 255)  # Amber/orange
        outline_color = (255, 255, 255, 255)
    else:
        return img  # Unknown state, return as-is
    
    # Draw white outline (slightly larger circle)
    outline_radius = indicator_radius + 3
    draw.ellipse(
        [
            center_x - outline_radius,
            center_y - outline_radius,
            center_x + outline_radius,
            center_y + outline_radius,
        ],
        fill=outline_color,
    )
    
    # Draw colored indicator
    draw.ellipse(
        [
            center_x - indicator_radius,
            center_y - indicator_radius,
            center_x + indicator_radius,
            center_y + indicator_radius,
        ],
        fill=fill_color,
    )
    
    return img


def get_state_icon(state: str) -> Image.Image:
    """Get icon for the given state, using cache when possible."""
    global _icon_cache
    
    if state in _icon_cache:
        return _icon_cache[state]
    
    # Load base icon
    base_icon = load_icon()
    
    # Generate state-specific icon
    state_icon = _add_state_indicator(base_icon, state)
    
    # Cache it
    _icon_cache[state] = state_icon
    
    return state_icon


# Log file path
LOG_FILE = Path(__file__).parent.parent / "latest.log"

def write_log(msg: str):
    """Write log message to buffer and file."""
    with log_lock:
        # Write to buffer
        log_buffer.append(msg)
        
        # Write to file
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def read_output(pipe, prefix=""):
    """Read output from subprocess and store in buffer."""
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            line = line.rstrip()
            write_log(f"{prefix}{line}")
            print(line)  # Also print to console if visible
    except Exception:
        pass


def kill_existing_backend():
    """Kill any existing process using port 8765."""
    if sys.platform == 'win32':
        try:
            # Use PowerShell for more reliable port detection
            ps_script = '''
            $conn = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
            if ($conn) {
                $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
                Write-Output "killed"
            }
            '''
            result = subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            if 'killed' in result.stdout.lower():
                write_log("[Tray] Killed existing process on port 8765")
                import time
                time.sleep(1)  # Wait for port to be released
        except Exception as e:
            write_log(f"[Tray] Port cleanup error: {e}")


def start_backend():
    """Start the backend process."""
    global backend_process
    
    # Kill any existing backend first
    kill_existing_backend()
    
    # Clear log file on startup
    try:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
    except Exception:
        pass
        
    write_log("[Tray] Starting backend...")
    
    # Get the python executable from the current environment
    python_exe = sys.executable
    
    # Start the backend
    backend_process = subprocess.Popen(
        [python_exe, "-m", "src.web_api"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).parent.parent),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
    )
    
    # Start thread to read output
    output_thread = threading.Thread(target=read_output, args=(backend_process.stdout,), daemon=True)
    output_thread.start()
    
    write_log("[Tray] Backend process started")


def start_frontend():
    """Start the frontend process."""
    global frontend_process
    
    frontend_dir = Path(__file__).parent.parent / "Frontend"
    if not frontend_dir.exists():
        write_log("[Tray] Error: Frontend directory not found")
        return

    # Use shell=True for npm on Windows
    # Pass VITE_BACKEND_URL in environment
    env = os.environ.copy()
    env["VITE_BACKEND_URL"] = "http://127.0.0.1:8765"
    
    try:
        frontend_process = subprocess.Popen(
            "npm run dev:client",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(frontend_dir),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        
        # Start thread to read output
        output_thread = threading.Thread(target=read_output, args=(frontend_process.stdout, "[Frontend] "), daemon=True)
        output_thread.start()
        
        write_log("[Tray] Frontend process started")
    except Exception as e:
        write_log(f"[Tray] Error starting frontend: {e}")


def stop_frontend():
    """Stop the frontend process."""
    global frontend_process
    if frontend_process:
        write_log("[Tray] Stopping frontend...")
        # Since we use shell=True, we need to kill the process group on Windows
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(frontend_process.pid)], creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            frontend_process.terminate()
        frontend_process = None


def stop_backend():
    """Stop the backend process."""
    global backend_process
    if backend_process:
        write_log("[Tray] Stopping backend...")
        backend_process.terminate()
        try:
            backend_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend_process.kill()
        backend_process = None


def watchdog_loop():
    """Monitor backend process and restart if it crashes."""
    global backend_process, watchdog_running
    import time
    import urllib.request
    
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3  # Restart after 3 failed health checks
    
    while watchdog_running:
        time.sleep(BACKEND_CHECK_INTERVAL)
        
        if not watchdog_running:
            break
        
        # Check if backend process is still running
        if backend_process is not None and backend_process.poll() is not None:
            # Process has exited
            exit_code = backend_process.returncode
            write_log(f"[Watchdog] Backend process exited with code {exit_code}, restarting...")
            start_backend()
            consecutive_failures = 0
            continue
        
        # Also check via health endpoint (in case process is running but unresponsive)
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/api/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
        except Exception:
            consecutive_failures += 1
        
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            write_log(f"[Watchdog] Backend unresponsive ({consecutive_failures} failures), restarting...")
            stop_backend()
            time.sleep(1)
            start_backend()
            consecutive_failures = 0


# State monitor settings
STATE_CHECK_INTERVAL = 1  # Check state every 1 second
state_monitor_running = True
state_monitor_thread = None


def update_tray_icon_for_state(new_state: str) -> None:
    """Update the tray icon to reflect the current app state."""
    global _current_app_state, tray_icon
    
    with _state_lock:
        if _current_app_state == new_state:
            return  # No change
        
        old_state = _current_app_state
        _current_app_state = new_state
    
    if tray_icon is None:
        return
    
    try:
        new_icon = get_state_icon(new_state)
        tray_icon.icon = new_icon
        
        # Update tooltip to reflect state
        if new_state == "recording":
            tray_icon.title = "Scriber - Recording..."
        elif new_state == "processing":
            tray_icon.title = "Scriber - Processing..."
        else:
            tray_icon.title = "Scriber"
        
        if old_state != new_state:
            write_log(f"[Tray] Icon state changed: {old_state} -> {new_state}")
    except Exception as e:
        write_log(f"[Tray] Failed to update icon: {e}")


def state_monitor_loop():
    """Monitor backend state and update tray icon accordingly."""
    global state_monitor_running
    import time
    import urllib.request
    import json as json_module
    
    # Separate counters for different checks
    state_check_cycle = 0
    
    while state_monitor_running:
        time.sleep(STATE_CHECK_INTERVAL)
        
        if not state_monitor_running:
            break
        
        state_check_cycle += 1
        new_state = "idle"  # Default
        
        try:
            # Always check the main state endpoint (fast, lightweight)
            req = urllib.request.Request("http://127.0.0.1:8765/api/state", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json_module.loads(resp.read().decode('utf-8'))
                    
                    # Determine state based on response
                    is_listening = data.get("listening", False)
                    status = data.get("status", "")
                    current = data.get("current")
                    
                    if is_listening:
                        # Currently recording (highest priority)
                        new_state = "recording"
                    elif current and current.get("status") == "processing":
                        # Recording stopped, but transcription in progress
                        new_state = "processing"
                    elif status.lower() in ("transcribing", "processing"):
                        new_state = "processing"
            
            # Every 3 seconds, also check for background transcription tasks
            # This catches YouTube/file transcriptions that run in background
            if new_state == "idle" and state_check_cycle % 3 == 0:
                try:
                    req = urllib.request.Request(
                        "http://127.0.0.1:8765/api/transcripts?limit=5", 
                        method="GET"
                    )
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        if resp.status == 200:
                            transcripts_data = json_module.loads(resp.read().decode('utf-8'))
                            items = transcripts_data.get("items", [])
                            
                            # Check if any recent transcript is still processing
                            for item in items:
                                if item.get("status") == "processing":
                                    new_state = "processing"
                                    break
                except Exception:
                    pass  # Non-critical, just skip this check
            
            update_tray_icon_for_state(new_state)
            
        except urllib.error.URLError:
            # Backend not reachable, show idle state
            update_tray_icon_for_state("idle")
        except Exception:
            # Don't log every failed state check to avoid log spam
            pass


# Global state for log window
log_window_ref = None

def show_logs(icon, item):
    """Launch the separate log viewer process."""
    try:
        python_exe = sys.executable
        script_path = Path(__file__).parent / "log_viewer.py"
        
        # Launch as new independent process
        subprocess.Popen(
            [python_exe, str(script_path)],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            close_fds=True
        )
    except Exception as e:
        write_log(f"[Tray] Failed to start log viewer: {e}")


def open_browser(icon, item):
    """Open the web UI in browser."""
    webbrowser.open("http://localhost:5000")


def restart_backend(icon, item):
    """Restart both backend and frontend processes."""
    write_log("[Tray] Restarting all services...")
    stop_backend()
    stop_frontend()
    import time
    time.sleep(1)
    start_backend()
    start_frontend()
    write_log("[Tray] All services restarted")


def get_recent_transcripts():
    """Fetch recent completed transcripts from the database."""
    try:
        from src.database import load_all_transcripts, _DB_PATH

        write_log(f"[Tray] Loading from database: {_DB_PATH}")

        # Load all transcripts from database (already sorted by created_at DESC)
        all_transcripts = load_all_transcripts()

        write_log(f"[Tray] Database returned {len(all_transcripts)} total transcripts")
        for i, t in enumerate(all_transcripts[:10]):  # Log first 10 for debugging
            write_log(f"[Tray]   #{i}: type={t.get('type')!r}, status={t.get('status')!r}, title={t.get('title', '')[:30]!r}")

        # Filter for completed recordings (any type) and limit to 5
        recent_transcripts = []
        for t in all_transcripts:
            # Include all completed recordings (mic, youtube, file)
            if t.get("status") == "completed":
                # Add preview field for menu display
                content = t.get("content", "")
                if content:
                    # Create preview from first 5 words of content
                    words = content.split()[:5]
                    preview = " ".join(words)
                    if len(words) >= 5:
                        preview += "..."
                    t["preview"] = preview.replace("\n", " ").strip()
                else:
                    t["preview"] = t.get("title", "Untitled")

                recent_transcripts.append(t)

                if len(recent_transcripts) >= 5:
                    break

        write_log(f"[Tray] Found {len(recent_transcripts)} completed transcripts for menu")

        return recent_transcripts
    except Exception as e:
        # Log unexpected errors
        import traceback
        write_log(f"[Tray] Database fetch error: {type(e).__name__}: {e}")
        write_log(f"[Tray] Traceback: {traceback.format_exc()}")
        return []


def copy_transcript_to_clipboard(transcript_id, label):
    """Fetch and copy transcript content to clipboard from database."""
    def action(icon, item):
        write_log(f"[Tray] === CLICKED MENU ITEM: {label[:40]} ===")
        write_log(f"[Tray] Transcript ID: {transcript_id}")

        try:
            # Fetch full content from database
            from src.database import get_transcript

            write_log(f"[Tray] Fetching from database...")

            transcript = get_transcript(transcript_id)

            write_log(f"[Tray] Database result: {transcript is not None}")

            if not transcript:
                write_log(f"[Tray] Transcript not found: {label[:40]}")
                return

            content = transcript.get("content", "")

            write_log(f"[Tray] Content length: {len(content)}")

            if not content:
                write_log(f"[Tray] No content found for: {label[:40]}")
                return

            write_log(f"[Tray] Starting clipboard copy...")

            # Use clipboard copy methods
            if sys.platform == 'win32':
                try:
                    # Use Win32 API directly for proper Unicode support
                    import ctypes
                    from ctypes import wintypes
                    
                    # Windows API constants
                    CF_UNICODETEXT = 13
                    GMEM_MOVEABLE = 0x0002
                    
                    # Load required functions
                    user32 = ctypes.windll.user32
                    kernel32 = ctypes.windll.kernel32
                    
                    # Set up function signatures
                    user32.OpenClipboard.argtypes = [wintypes.HWND]
                    user32.OpenClipboard.restype = wintypes.BOOL
                    user32.CloseClipboard.argtypes = []
                    user32.CloseClipboard.restype = wintypes.BOOL
                    user32.EmptyClipboard.argtypes = []
                    user32.EmptyClipboard.restype = wintypes.BOOL
                    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
                    user32.SetClipboardData.restype = wintypes.HANDLE
                    
                    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
                    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
                    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
                    kernel32.GlobalLock.restype = wintypes.LPVOID
                    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
                    kernel32.GlobalUnlock.restype = wintypes.BOOL
                    
                    # Encode as UTF-16 (Windows native Unicode format)
                    text_utf16 = content.encode('utf-16-le') + b'\x00\x00'  # Null-terminated
                    
                    # Allocate global memory
                    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(text_utf16))
                    if not h_mem:
                        raise ctypes.WinError()
                    
                    try:
                        # Lock memory and copy data
                        ptr = kernel32.GlobalLock(h_mem)
                        if not ptr:
                            raise ctypes.WinError()
                        
                        try:
                            ctypes.memmove(ptr, text_utf16, len(text_utf16))
                        finally:
                            kernel32.GlobalUnlock(h_mem)
                        
                        # Open clipboard and set data
                        if not user32.OpenClipboard(None):
                            raise ctypes.WinError()
                        
                        try:
                            user32.EmptyClipboard()
                            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                                raise ctypes.WinError()
                            # Memory now owned by clipboard, don't free it
                            h_mem = None
                        finally:
                            user32.CloseClipboard()
                        
                        write_log(f"[Tray] ✓ Copied to clipboard (Unicode): {label[:40]}...")
                    
                    except Exception:
                        # If anything went wrong and we still own the memory, free it
                        if h_mem:
                            kernel32.GlobalFree(h_mem)
                        raise

                except Exception as win_err:
                    write_log(f"[Tray] Windows clipboard error: {type(win_err).__name__}: {win_err}")
            else:
                # Fallback for non-Windows (macOS/Linux)
                cmd = ['pbcopy'] if sys.platform == 'darwin' else ['xclip', '-selection', 'clipboard']
                process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                process.communicate(content.encode('utf-8'))
                write_log(f"[Tray] ✓ Copied to clipboard: {label[:40]}...")

        except Exception as e:
            write_log(f"[Tray] Error copying transcript: {type(e).__name__}: {e}")

    return action


def get_transcripts_menu():
    """Generate dynamic submenu for recent transcripts."""
    transcripts = get_recent_transcripts()

    write_log(f"[Tray] Building menu with {len(transcripts)} transcripts")

    if not transcripts:
        return [pystray.MenuItem("No recent recordings", lambda i, t: None, enabled=False)]

    items = []
    for t in transcripts:
        # Choose prefix based on transcript type
        t_type = t.get("type", "mic")
        if t_type == "youtube":
            prefix = "🎬\ufe0f"
        elif t_type == "file":
            prefix = "📁\ufe0f"
        else:  # mic or default
            prefix = "🎤\ufe0f"
        
        # Format: [Type] [Date] Preview...
        date = t.get("date", "")
        preview = t.get("preview") or t.get("title", "Untitled")
        label = f"{prefix} [{date}] {preview}"

        # Ensure label isn't too long
        if len(label) > 60:
            label = label[:57] + "..."

        transcript_id = t.get("id")
        write_log(f"[Tray] Menu item: {label[:40]} (ID: {transcript_id})")

        items.append(pystray.MenuItem(label, copy_transcript_to_clipboard(transcript_id, label)))

    # Add hint at bottom
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Click to copy transcript", lambda i, t: None, enabled=False))

    return items


def restart_app(icon, item):
    """Restart the entire tray application."""
    def restart():
        write_log("[Tray] Restarting entire application...")

        stop_backend()
        stop_frontend()

        # Release lock before starting new instance
        release_single_instance_lock()

        # Get the Python executable and script path
        python_exe = sys.executable
        script_path = str(Path(__file__).resolve())

        # Start new instance
        subprocess.Popen([python_exe, script_path])

        # Stop current instance
        icon.stop()
        os._exit(0)

    threading.Thread(target=restart, daemon=True).start()


def quit_app(icon, item):
    """Quit the application."""
    global watchdog_running, state_monitor_running
    # Run shutdown in a separate thread so it doesn't block the UI
    def shutdown():
        global watchdog_running, state_monitor_running
        watchdog_running = False  # Stop watchdog first
        state_monitor_running = False  # Stop state monitor
        stop_backend()
        stop_frontend()
        release_single_instance_lock()
        icon.stop()
        # Force exit to ensure no hanging threads
        os._exit(0)

    threading.Thread(target=shutdown, daemon=True).start()


def copy_all_logs(icon, item):
    """Copy all logs to clipboard."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        with log_lock:
            all_logs = "\n".join(log_buffer)
        root.clipboard_append(all_logs)
        root.update()
        root.destroy()
        write_log("[Tray] Logs copied to clipboard")
    except Exception as e:
        write_log(f"[Tray] Error copying logs: {e}")


def create_menu():
    """Create the tray menu dynamically (called each time menu is opened)."""
    # Check if backend is running for status display
    backend_status = "Running" if backend_process and backend_process.poll() is None else "Stopped"
    frontend_status = "Running" if frontend_process and frontend_process.poll() is None else "Stopped"
    
    return pystray.Menu(
        # Main actions
        pystray.MenuItem("🌐\ufe0f Open Scriber", open_browser, default=True),
        pystray.Menu.SEPARATOR,
        
        # Recent recordings submenu
        pystray.MenuItem("📂\ufe0f Recent Recordings", pystray.Menu(get_transcripts_menu)),
        pystray.Menu.SEPARATOR,
        
        # Logs section
        pystray.MenuItem("📋\ufe0f View Logs", show_logs),
        pystray.MenuItem("📑\ufe0f Copy Logs", copy_all_logs),
        pystray.Menu.SEPARATOR,
        
        # Status display (non-clickable)
        pystray.MenuItem(
            f"Running Status: {backend_status} | {frontend_status}",
            lambda i, t: None,
            enabled=False
        ),
        
        # Restart options
        pystray.MenuItem("🔄\ufe0f Restart Backend", restart_backend),
        pystray.MenuItem("⚡\ufe0f Restart App", restart_app),
        pystray.Menu.SEPARATOR,
        
        # Quit
        pystray.MenuItem("🚪\ufe0f Quit", quit_app),
    )


def setup_tray():
    """Setup the system tray icon."""
    global tray_icon

    icon_image = load_icon()

    # Pass menu as a lambda so it's regenerated each time the menu is opened
    # This enables dynamic menu items (like status updates)
    tray_icon = pystray.Icon(
        "Scriber",
        icon_image,
        "Scriber Backend",
        menu=pystray.Menu(lambda: iter(create_menu())),
    )

    return tray_icon


def main():
    """Main entry point."""
    # Enable High DPI support on Windows
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # Check for single instance
    if not acquire_single_instance_lock():
        print("Another instance of Scriber is already running. Exiting.")
        sys.exit(1)

    global watchdog_running, watchdog_thread, state_monitor_running, state_monitor_thread

    # Start processes
    start_backend()
    start_frontend()
    
    # Start watchdog thread to auto-restart backend if it crashes
    watchdog_running = True
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="BackendWatchdog")
    watchdog_thread.start()
    write_log("[Tray] Backend watchdog started (auto-restart enabled)")
    
    # Start state monitor thread to update tray icon based on app state
    state_monitor_running = True
    state_monitor_thread = threading.Thread(target=state_monitor_loop, daemon=True, name="StateMonitor")
    state_monitor_thread.start()
    write_log("[Tray] State monitor started (dynamic icon updates enabled)")
    
    # Setup and run tray icon
    icon = setup_tray()
    
    write_log("[Tray] System tray icon active")
    write_log("[Tray] Right-click the icon for options")
    
    try:
        icon.run()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop monitor threads
        watchdog_running = False
        state_monitor_running = False
        stop_backend()
        stop_frontend()
        release_single_instance_lock()


if __name__ == "__main__":
    main()
