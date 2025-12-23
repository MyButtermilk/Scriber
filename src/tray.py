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
from pathlib import Path
from collections import deque

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import pystray
    from PIL import Image
except ImportError:
    print("Error: pystray and Pillow are required. Run: pip install pystray pillow")
    sys.exit(1)

# Log buffer to store recent logs
MAX_LOG_LINES = 500
log_buffer = deque(maxlen=MAX_LOG_LINES)
log_lock = threading.Lock()

# Processes
backend_process = None
frontend_process = None
tray_icon = None


def load_icon():
    """Load the favicon icon for the system tray."""
    icon_paths = [
        Path(__file__).parent.parent / "Frontend" / "client" / "public" / "favicon.png",
        Path(__file__).parent.parent / "Frontend" / "client" / "public" / "favicon.ico",
    ]
    
    for icon_path in icon_paths:
        if icon_path.exists():
            try:
                img = Image.open(icon_path)
                # Resize to typical tray icon size
                img = img.resize((64, 64), Image.Resampling.LANCZOS)
                return img
            except Exception:
                pass
    
    # Fallback: create a simple colored icon
    img = Image.new('RGBA', (64, 64), (59, 130, 246, 255))  # Blue color matching theme
    return img


def read_output(pipe, prefix=""):
    """Read output from subprocess and store in buffer."""
    global log_buffer
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            line = line.rstrip()
            with log_lock:
                log_buffer.append(f"{prefix}{line}")
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
                with log_lock:
                    log_buffer.append("[Tray] Killed existing process on port 8765")
                import time
                time.sleep(1)  # Wait for port to be released
        except Exception as e:
            with log_lock:
                log_buffer.append(f"[Tray] Port cleanup error: {e}")


def start_backend():
    """Start the backend process."""
    global backend_process
    
    # Kill any existing backend first
    kill_existing_backend()
    
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
    
    with log_lock:
        log_buffer.append("[Tray] Backend process started")


def start_frontend():
    """Start the frontend process."""
    global frontend_process
    
    frontend_dir = Path(__file__).parent.parent / "Frontend"
    if not frontend_dir.exists():
        with log_lock:
            log_buffer.append("[Tray] Error: Frontend directory not found")
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
        
        with log_lock:
            log_buffer.append("[Tray] Frontend process started")
    except Exception as e:
        with log_lock:
            log_buffer.append(f"[Tray] Error starting frontend: {e}")


def stop_frontend():
    """Stop the frontend process."""
    global frontend_process
    if frontend_process:
        with log_lock:
            log_buffer.append("[Tray] Stopping frontend...")
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
        with log_lock:
            log_buffer.append("[Tray] Stopping backend...")
        backend_process.terminate()
        try:
            backend_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend_process.kill()
        backend_process = None


def show_logs(icon, item):
    """Show logs in a simple window using Tkinter."""
    def run_logs_window():
        try:
            import tkinter as tk
            from tkinter import scrolledtext
            
            paused = [False]  # Pause refresh when selecting
            last_log_count = [0]
            
            def on_mouse_down(e):
                paused[0] = True
            
            def on_mouse_up(e):
                paused[0] = False
            
            def refresh_logs():
                if paused[0]:
                    root.after(100, refresh_logs)
                    return
                
                with log_lock:
                    current_count = len(log_buffer)
                    # Only update if there are new logs
                    if current_count != last_log_count[0]:
                        # Save scroll position
                        at_bottom = text_widget.yview()[1] >= 0.99
                        
                        text_widget.config(state=tk.NORMAL)
                        text_widget.delete(1.0, tk.END)
                        for line in log_buffer:
                            text_widget.insert(tk.END, line + "\n")
                        text_widget.config(state=tk.DISABLED)
                        
                        last_log_count[0] = current_count
                        
                        if at_bottom:
                            text_widget.see(tk.END)
                
                root.after(1000, refresh_logs)
            
            def copy_selection(e=None):
                try:
                    # Enable widget temporarily to get selection
                    text_widget.config(state=tk.NORMAL)
                    selected = text_widget.get(tk.SEL_FIRST, tk.SEL_LAST)
                    text_widget.config(state=tk.DISABLED)
                    root.clipboard_clear()
                    root.clipboard_append(selected)
                    return "break"
                except tk.TclError:
                    pass
            
            def select_all(e=None):
                text_widget.config(state=tk.NORMAL)
                text_widget.tag_add(tk.SEL, "1.0", tk.END)
                text_widget.config(state=tk.DISABLED)
                return "break"
            
            root = tk.Tk()
            root.title("Scriber Backend Logs")
            root.geometry("900x500")
            root.configure(bg="#1a1a2e")
            
            # Add menu bar
            menubar = tk.Menu(root)
            edit_menu = tk.Menu(menubar, tearoff=0)
            edit_menu.add_command(label="Copy", command=copy_selection, accelerator="Ctrl+C")
            edit_menu.add_command(label="Select All", command=select_all, accelerator="Ctrl+A")
            menubar.add_cascade(label="Edit", menu=edit_menu)
            root.config(menu=menubar)
            
            # Create text widget - DISABLED prevents editing but allows selection
            text_widget = scrolledtext.ScrolledText(
                root,
                wrap=tk.WORD,
                font=("Consolas", 10),
                bg="#1a1a2e",
                fg="#e0e0e0",
                insertbackground="#ffffff",
                selectbackground="#3B82F6",
                selectforeground="#ffffff",
                cursor="arrow",
            )
            text_widget.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            text_widget.config(state=tk.DISABLED)
            
            # Bind mouse events to pause refresh during selection
            text_widget.bind("<Button-1>", on_mouse_down)
            text_widget.bind("<ButtonRelease-1>", on_mouse_up)
            text_widget.bind("<B1-Motion>", lambda e: None)  # Allow drag selection
            
            # Bind keyboard shortcuts
            root.bind('<Control-c>', copy_selection)
            root.bind('<Control-a>', select_all)
            
            refresh_logs()
            root.mainloop()
        except Exception as e:
            print(f"Error showing logs: {e}")
    
    # Run in separate thread to not block tray
    threading.Thread(target=run_logs_window, daemon=True).start()


def open_browser(icon, item):
    """Open the web UI in browser."""
    webbrowser.open("http://localhost:5000")


def restart_backend(icon, item):
    """Restart both backend and frontend processes."""
    with log_lock:
        log_buffer.append("[Tray] Restarting all services...")
    stop_backend()
    stop_frontend()
    import time
    time.sleep(1)
    start_backend()
    start_frontend()
    with log_lock:
        log_buffer.append("[Tray] All services restarted")


def get_recent_transcripts():
    """Fetch recent completed transcripts from the database."""
    try:
        from src.database import load_all_transcripts, _DB_PATH

        with log_lock:
            log_buffer.append(f"[Tray] Loading from database: {_DB_PATH}")

        # Load all transcripts from database (already sorted by created_at DESC)
        all_transcripts = load_all_transcripts()

        with log_lock:
            log_buffer.append(f"[Tray] Database returned {len(all_transcripts)} total transcripts")
            for i, t in enumerate(all_transcripts[:10]):  # Log first 10 for debugging
                log_buffer.append(f"[Tray]   #{i}: type={t.get('type')!r}, status={t.get('status')!r}, title={t.get('title', '')[:30]!r}")

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

        with log_lock:
            log_buffer.append(f"[Tray] Found {len(recent_transcripts)} completed transcripts for menu")

        return recent_transcripts
    except Exception as e:
        # Log unexpected errors
        import traceback
        with log_lock:
            log_buffer.append(f"[Tray] Database fetch error: {type(e).__name__}: {e}")
            log_buffer.append(f"[Tray] Traceback: {traceback.format_exc()}")
        return []


def copy_transcript_to_clipboard(transcript_id, label):
    """Fetch and copy transcript content to clipboard from database."""
    def action(icon, item):
        with log_lock:
            log_buffer.append(f"[Tray] === CLICKED MENU ITEM: {label[:40]} ===")
            log_buffer.append(f"[Tray] Transcript ID: {transcript_id}")

        try:
            # Fetch full content from database
            from src.database import get_transcript

            with log_lock:
                log_buffer.append(f"[Tray] Fetching from database...")

            transcript = get_transcript(transcript_id)

            with log_lock:
                log_buffer.append(f"[Tray] Database result: {transcript is not None}")

            if not transcript:
                with log_lock:
                    log_buffer.append(f"[Tray] Transcript not found: {label[:40]}")
                return

            content = transcript.get("content", "")

            with log_lock:
                log_buffer.append(f"[Tray] Content length: {len(content)}")

            if not content:
                with log_lock:
                    log_buffer.append(f"[Tray] No content found for: {label[:40]}")
                return

            with log_lock:
                log_buffer.append(f"[Tray] Starting clipboard copy...")

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
                        
                        with log_lock:
                            log_buffer.append(f"[Tray] ✓ Copied to clipboard (Unicode): {label[:40]}...")
                    
                    except Exception:
                        # If anything went wrong and we still own the memory, free it
                        if h_mem:
                            kernel32.GlobalFree(h_mem)
                        raise

                except Exception as win_err:
                    with log_lock:
                        log_buffer.append(f"[Tray] Windows clipboard error: {type(win_err).__name__}: {win_err}")
            else:
                # Fallback for non-Windows (macOS/Linux)
                cmd = ['pbcopy'] if sys.platform == 'darwin' else ['xclip', '-selection', 'clipboard']
                process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                process.communicate(content.encode('utf-8'))
                with log_lock:
                    log_buffer.append(f"[Tray] ✓ Copied to clipboard: {label[:40]}...")

        except Exception as e:
            with log_lock:
                log_buffer.append(f"[Tray] Error copying transcript: {type(e).__name__}: {e}")

    return action


def get_transcripts_menu():
    """Generate dynamic submenu for recent transcripts."""
    transcripts = get_recent_transcripts()

    with log_lock:
        log_buffer.append(f"[Tray] Building menu with {len(transcripts)} transcripts")

    if not transcripts:
        return [pystray.MenuItem("No recent recordings", lambda i, t: None, enabled=False)]

    items = []
    for t in transcripts:
        # Format: [Date Time] Preview...
        date = t.get("date", "")
        # Try to parse date to be shorter if needed, but the API formatted one is usually "Today, H:M"

        preview = t.get("preview") or t.get("title", "Untitled")
        label = f"[{date}] {preview}"

        # Ensure label isn't too long
        if len(label) > 60:
            label = label[:57] + "..."

        transcript_id = t.get("id")
        with log_lock:
            log_buffer.append(f"[Tray] Menu item: {label[:40]} (ID: {transcript_id})")

        items.append(pystray.MenuItem(label, copy_transcript_to_clipboard(transcript_id, label)))

    return items


def restart_app(icon, item):
    """Restart the entire tray application."""
    def restart():
        with log_lock:
            log_buffer.append("[Tray] Restarting entire application...")

        stop_backend()
        stop_frontend()

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
    # Run shutdown in a separate thread so it doesn't block the UI
    def shutdown():
        stop_backend()
        stop_frontend()
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
        with log_lock:
            log_buffer.append("[Tray] Logs copied to clipboard")
    except Exception as e:
        with log_lock:
            log_buffer.append(f"[Tray] Error copying logs: {e}")


def create_menu():
    """Create the tray menu dynamically (called each time menu is opened)."""
    return pystray.Menu(
        pystray.MenuItem("Open Scriber", open_browser, default=True),
        pystray.MenuItem("View Logs", show_logs),
        pystray.MenuItem("Copy Logs", copy_all_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Recent Recordings", pystray.Menu(get_transcripts_menu)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart Backend", restart_backend),
        pystray.MenuItem("Restart App", restart_app),
        pystray.MenuItem("Quit", quit_app),
    )


def setup_tray():
    """Setup the system tray icon."""
    global tray_icon

    icon_image = load_icon()

    tray_icon = pystray.Icon(
        "Scriber",
        icon_image,
        "Scriber Backend",
        create_menu(),  # Create the initial menu
    )

    return tray_icon


def main():
    """Main entry point."""
    # Start processes
    start_backend()
    start_frontend()
    
    # Setup and run tray icon
    icon = setup_tray()
    
    with log_lock:
        log_buffer.append("[Tray] System tray icon active")
        log_buffer.append("[Tray] Right-click the icon for options")
    
    try:
        icon.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_backend()


if __name__ == "__main__":
    main()
