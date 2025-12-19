"""
Native recording overlay window for system-wide visibility.
Uses PySide6 (Qt) for smooth anti-aliased transparent overlay rendering.
Falls back to tkinter if PySide6 is not available.
"""

import threading
import queue
import time
import math
import os
from typing import Callable, Optional

# Suppress Windows DPI awareness warning (must be set before importing Qt)
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

# Try PySide6 first (best anti-aliasing support)
try:
    from PySide6.QtWidgets import QApplication, QWidget, QLabel
    from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QRectF, QPointF
    from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QFont, QFontMetrics, QLinearGradient, QCursor, QGuiApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False

# Fallback to tkinter
try:
    import tkinter as tk
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from loguru import logger

BAR_COUNT = 64


class QtOverlaySignals(QObject):
    """Signals for thread-safe communication with Qt overlay."""
    show_signal = Signal()
    hide_signal = Signal()
    show_transcribing_signal = Signal()
    audio_signal = Signal(float)
    quit_signal = Signal()


class QtOverlayWindow(QWidget):
    """Qt-based overlay window with smooth anti-aliased rendering and improved visualization."""
    
    stopped = Signal()
    
    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        super().__init__()
        self._on_stop = on_stop
        self._is_recording = False
        self._is_transcribing = False
        self._spinner_angle = 0
        
        # Window setup with shadow margin
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool  # Prevents taskbar icon
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        
        # Size proportions (30% smaller than original)
        self._shadow_margin = 8
        self._pill_w = 280
        self._pill_h = 50
        self.setFixedSize(
            self._pill_w + 2 * self._shadow_margin,
            self._pill_h + 2 * self._shadow_margin,
        )
        
        # Wave buffer with ring buffer (levels = target, display = smoothed)
        self._bar_count = BAR_COUNT
        self._levels = [0.0] * self._bar_count
        self._display = [0.0] * self._bar_count
        self._write_idx = 0
        self._taper = self._build_taper(self._bar_count)
        
        # AGC (Automatic Gain Control) for adaptive level scaling
        self._agc = 0.02
        
        # Stop button hit-test (updated during painting)
        self._stop_center_x = 0.0
        self._stop_center_y = 0.0
        self._stop_radius = 0.0
        self._btn_hover = False
        
        # Drag support
        self._dragging = False
        self._drag_offset = None
        
        self.setMouseTracking(True)
        
        # 60 fps animation timer
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._tick)
        
        # Spinner timer for transcribing mode
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(16)
        self._spinner_timer.timeout.connect(self._update_spinner)
        
        # Fade animation support
        self._pending_hide = False
        self._fade_opacity = 0.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._fade_tick)
        self._fade_target = 0.0
        self._fade_speed = 0.15  # ~7 frames to full fade
        
        self.setWindowOpacity(0.0)
        
    def _build_taper(self, n: int) -> list:
        """Build taper coefficients for waveform edges."""
        if n <= 1:
            return [1.0]
        out = []
        for i in range(n):
            t = i / (n - 1)
            out.append(0.55 + 0.45 * math.sin(math.pi * t))
        return out
    
    def _position_default(self):
        """Position overlay at bottom-center of screen, just above taskbar."""
        try:
            pos = QCursor.pos()
            screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
            if not screen:
                return
            geo = screen.availableGeometry()  # availableGeometry excludes taskbar
            x = int(geo.center().x() - self.width() / 2)
            y = int(geo.bottom() - self.height() - 20)  # 20px above taskbar
            self.move(x, y)
        except Exception:
            # Fallback to bottom center
            screen = QApplication.primaryScreen().availableGeometry()
            x = (screen.width() - self.width()) // 2
            y = screen.height() - self.height() - 20
            self.move(x, y)
    
    def show_recording(self):
        """Show overlay in recording mode with fade in."""
        self._is_recording = True
        self._is_transcribing = False
        self._spinner_timer.stop()
        self._pending_hide = False
        
        # Reset levels
        self._levels = [0.0] * self._bar_count
        self._display = [0.0] * self._bar_count
        self._write_idx = 0
        self._agc = 0.02
        
        self._position_default()
        self.setWindowOpacity(0.0)
        self._fade_opacity = 0.0
        self._fade_target = 1.0
        self.show()
        self.raise_()
        self._anim_timer.start()
        self._fade_timer.start()
        
    def show_transcribing(self):
        """Show overlay in transcribing mode."""
        self._is_recording = False
        self._is_transcribing = True
        self._spinner_angle = 0
        self._anim_timer.stop()
        self._spinner_timer.start()
        self.update()
        
    def hide_overlay(self):
        """Hide overlay with fade out."""
        if not self.isVisible():
            return
        self._pending_hide = True
        self._fade_target = 0.0
        self._fade_timer.start()
        
    def hideEvent(self, event):
        """Handle hide event."""
        self._anim_timer.stop()
        self._spinner_timer.stop()
        self._fade_timer.stop()
        self._is_recording = False
        self._is_transcribing = False
        super().hideEvent(event)
        
    def _fade_tick(self):
        """Handle fade animation."""
        diff = self._fade_target - self._fade_opacity
        if abs(diff) < 0.01:
            self._fade_opacity = self._fade_target
            self._fade_timer.stop()
            if self._pending_hide and self._fade_opacity <= 0.01:
                self._anim_timer.stop()
                self._spinner_timer.stop()
                self.hide()
        else:
            self._fade_opacity += diff * self._fade_speed
        self.setWindowOpacity(self._fade_opacity)
    
    def _update_spinner(self):
        """Update spinner rotation angle."""
        if not self._is_transcribing:
            self._spinner_timer.stop()
            return
        self._spinner_angle = (self._spinner_angle + 6) % 360
        self.update()
        
    def update_audio_level(self, rms: float):
        """Update audio level with AGC and add to ring buffer."""
        if not self._is_recording:
            return
        
        rms = max(0.0, min(1.0, float(rms)))
        
        # Lightweight AGC with slow decay
        self._agc = max(rms, self._agc * 0.995)
        norm = rms / (self._agc + 1e-9)
        
        # Noise gate in normalized domain
        gate = 0.12
        lvl = (norm - gate) / (1.0 - gate)
        lvl = max(0.0, min(float(lvl), 1.0))
        
        # Add to ring buffer
        self._levels[self._write_idx] = lvl
        self._write_idx = (self._write_idx + 1) % self._bar_count
    
    def _tick(self):
        """Animation tick - smooth interpolation."""
        # Smooth interpolation to remove jitter
        alpha = 0.35
        for i in range(self._bar_count):
            self._display[i] += (self._levels[i] - self._display[i]) * alpha
        self.update()
        
    def paintEvent(self, event):
        """Custom paint with anti-aliased graphics."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        
        m = self._shadow_margin
        pill = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)
        radius = pill.height() / 2.0
        
        # Soft shadow (multi-pass)
        shadow_steps = 5
        for i in range(shadow_steps, 0, -1):
            a = int(15 * (i / shadow_steps))
            painter.setBrush(QColor(0, 0, 0, a))
            painter.setPen(Qt.NoPen)
            r = QRectF(pill.left() - i, pill.top() - i, 
                       pill.width() + 2 * i, pill.height() + 2 * i)
            r.translate(0, i * 0.6 + 2)
            painter.drawRoundedRect(r, radius + i, radius + i)
        
        # Background gradient
        grad = QLinearGradient(pill.topLeft(), pill.bottomLeft())
        grad.setColorAt(0.0, QColor(18, 18, 18, 245))
        grad.setColorAt(1.0, QColor(8, 8, 8, 245))
        painter.setBrush(grad)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(pill, radius, radius)
        
        if self._is_recording:
            self._draw_recording_mode(painter, pill)
        elif self._is_transcribing:
            self._draw_transcribing_mode(painter, pill)
    
    def _draw_recording_mode(self, painter: QPainter, pill: QRectF):
        """Draw recording mode: stop button + waveform."""
        # Stop button sized relative to pill
        stop_d = pill.height() * 0.68
        self._stop_radius = stop_d / 2.0
        stop_cx = pill.left() + pill.height() * 0.52
        stop_cy = pill.center().y()
        self._stop_center_x = stop_cx
        self._stop_center_y = stop_cy
        
        stop_rect = QRectF(
            stop_cx - self._stop_radius,
            stop_cy - self._stop_radius,
            stop_d, stop_d
        )
        
        # Stop button
        btn_color = QColor(220, 53, 69) if self._btn_hover else QColor(226, 60, 60)
        painter.setBrush(btn_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(stop_rect)
        
        # White square with rounded corners
        sq = stop_d * 0.30
        sq_rect = QRectF(stop_cx - sq / 2.0, stop_cy - sq / 2.0, sq, sq)
        painter.setBrush(QColor(250, 250, 250))
        painter.drawRoundedRect(sq_rect, 4, 4)
        
        # Waveform area
        left_pad = pill.height() * 1.08
        right_pad = pill.height() * 0.30
        wave = QRectF(
            pill.left() + left_pad,
            pill.top() + pill.height() * 0.22,
            pill.width() - left_pad - right_pad,
            pill.height() * 0.56,
        )
        cy = wave.center().y()
        max_h = wave.height() / 2.0
        
        # Bar sizing
        bar_count = self._bar_count
        gap = 2.0
        bar_w = (wave.width() - gap * (bar_count - 1)) / bar_count
        bar_w = max(1.5, min(bar_w, 5.0))
        
        wave_color = QColor(85, 255, 140)
        pen = QPen(wave_color)
        pen.setCapStyle(Qt.RoundCap)
        pen.setWidthF(bar_w)
        
        dot_threshold = 2.5
        dot_r = max(1.0, bar_w * 0.45)
        
        # Draw bars from ring buffer (oldest left, newest right)
        x0 = wave.left()
        for i in range(bar_count):
            idx = (self._write_idx + i) % bar_count
            lvl = self._display[idx] * self._taper[i]
            h = max_h * lvl
            x = x0 + i * (bar_w + gap) + bar_w / 2.0
            
            if h < dot_threshold:
                # Draw dot for silence/quiet
                painter.setPen(Qt.NoPen)
                painter.setBrush(wave_color)
                painter.drawEllipse(QRectF(x - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))
            else:
                # Draw bar
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawLine(QPointF(x, cy - h), QPointF(x, cy + h))
        
    def _draw_transcribing_mode(self, painter: QPainter, pill: QRectF):
        """Draw transcribing mode: spinner + text, centered in pill."""
        center_y = pill.center().y()
        
        # Calculate total content width for centering
        spinner_size = 20
        text = "Transcribing..."
        spacing = 10
        
        font = QFont("Segoe UI", 12)
        font.setWeight(QFont.Medium)
        painter.setFont(font)
        
        fm = QFontMetrics(font)
        text_width = fm.horizontalAdvance(text)
        total_content_width = spinner_size + spacing + text_width
        
        # Center the content in the pill
        content_start_x = pill.left() + (pill.width() - total_content_width) / 2
        
        # Draw spinning loader
        spinner_cx = content_start_x + spinner_size / 2
        spinner_cy = center_y
        
        # Draw spinner arc
        pen = QPen(QColor(85, 255, 140), 2.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        
        arc_rect = QRectF(
            spinner_cx - spinner_size / 2,
            spinner_cy - spinner_size / 2,
            spinner_size, spinner_size
        )
        
        start_angle = self._spinner_angle * 16
        span_angle = 270 * 16
        painter.drawArc(arc_rect, start_angle, span_angle)
        
        # Draw text
        text_x = content_start_x + spinner_size + spacing
        painter.setPen(QColor(85, 255, 140))
        text_rect = QRectF(text_x, pill.top(), text_width + 10, pill.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
    
    def _is_in_button(self, x: float, y: float) -> bool:
        """Check if coordinates are inside the stop button."""
        dx = x - self._stop_center_x
        dy = y - self._stop_center_y
        return (dx * dx + dy * dy) <= (self._stop_radius * self._stop_radius)
    
    def mousePressEvent(self, event):
        """Handle mouse clicks."""
        pos = event.position()
        if self._is_recording and self._is_in_button(pos.x(), pos.y()):
            if self._on_stop:
                threading.Thread(target=self._on_stop, daemon=True).start()
            return
        
        # Drag from anywhere except the stop button
        self._dragging = True
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
    
    def mouseReleaseEvent(self, event):
        """Handle mouse release."""
        if event.button() == Qt.LeftButton:
            self._dragging = False
    
    def mouseMoveEvent(self, event):
        """Handle mouse movement for hover effects and dragging."""
        pos = event.position()
        
        if self._is_in_button(pos.x(), pos.y()):
            self.setCursor(Qt.PointingHandCursor)
            if not self._btn_hover:
                self._btn_hover = True
                self.update()
        else:
            self.setCursor(Qt.ArrowCursor)
            if self._btn_hover:
                self._btn_hover = False
                self.update()
        
        if self._dragging and self._drag_offset is not None:
            top_left = event.globalPosition().toPoint() - self._drag_offset
            self.move(top_left)
    
    def leaveEvent(self, event):
        """Handle mouse leaving the window."""
        if self._btn_hover:
            self._btn_hover = False
            self.update()


class QtRecordingOverlay:
    """Qt-based recording overlay manager."""
    
    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        self._on_stop = on_stop
        self._app: Optional[QApplication] = None
        self._window: Optional[QtOverlayWindow] = None
        self._thread: Optional[threading.Thread] = None
        self._signals: Optional[QtOverlaySignals] = None
        self._running = False
        self._ready = threading.Event()  # Set when Qt is initialized
        
    def start(self) -> None:
        """Start the overlay in a separate thread."""
        if self._thread and self._thread.is_alive():
            return
            
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_qt, daemon=True)
        self._thread.start()
        # Wait for Qt to initialize (max 2 seconds)
        self._ready.wait(timeout=2.0)
        
    def stop(self) -> None:
        """Stop the overlay."""
        self._running = False
        if self._signals:
            self._signals.quit_signal.emit()
            
    def show(self) -> None:
        """Show the overlay in recording mode."""
        if self._signals:
            self._signals.show_signal.emit()
            
    def show_transcribing(self) -> None:
        """Show overlay in transcribing mode."""
        if self._signals:
            self._signals.show_transcribing_signal.emit()
            
    def hide(self) -> None:
        """Hide the overlay."""
        if self._signals:
            self._signals.hide_signal.emit()
            
    def update_audio_level(self, rms: float) -> None:
        """Update audio level."""
        if self._signals:
            self._signals.audio_signal.emit(rms)
    
    def _run_qt(self) -> None:
        """Run Qt event loop in thread."""
        try:
            self._app = QApplication.instance()
            if self._app is None:
                self._app = QApplication([])
            
            self._window = QtOverlayWindow(on_stop=self._on_stop)
            self._signals = QtOverlaySignals()
            
            # Connect signals with QueuedConnection for thread-safety
            self._signals.show_signal.connect(self._window.show_recording, Qt.QueuedConnection)
            self._signals.show_transcribing_signal.connect(self._window.show_transcribing, Qt.QueuedConnection)
            self._signals.hide_signal.connect(self._window.hide_overlay, Qt.QueuedConnection)
            self._signals.audio_signal.connect(self._window.update_audio_level, Qt.QueuedConnection)
            self._signals.quit_signal.connect(self._app.quit, Qt.QueuedConnection)
            
            # Signal that Qt is ready
            self._ready.set()
            logger.debug("Qt overlay ready")
            
            self._app.exec()
            
        except Exception as e:
            logger.error(f"Qt overlay error: {e}")
        finally:
            self._running = False


# ================================================
# Tkinter Fallback (keeping original implementation)
# ================================================

class TkRecordingOverlay:
    """Tkinter-based fallback overlay."""
    
    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        self._on_stop = on_stop
        self._root: Optional[tk.Tk] = None
        self._thread: Optional[threading.Thread] = None
        self._command_queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = False
        self._is_visible = False
        self._is_transcribing = False
        
        # UI elements
        self._main_canvas: Optional[tk.Canvas] = None
        self._btn_id = None
        self._sq_id = None
        self._canvas_window = None
        self._transcribing_window = None
        self._bar_ids: list = []
        
        self._audio_levels = [0.12] * BAR_COUNT
        
    def start(self) -> None:
        """Start the overlay in a separate thread."""
        if self._thread and self._thread.is_alive():
            return
            
        self._running = True
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        
    def stop(self) -> None:
        """Stop the overlay."""
        self._running = False
        self._command_queue.put(("quit", None))
        
    def show(self) -> None:
        """Show the overlay."""
        self._command_queue.put(("show", None))
        
    def show_transcribing(self) -> None:
        """Show overlay in transcribing mode."""
        self._command_queue.put(("transcribing", None))
        
    def hide(self) -> None:
        """Hide the overlay."""
        self._command_queue.put(("hide", None))
        
    def update_audio_level(self, rms: float) -> None:
        """Update audio level."""
        self._command_queue.put(("audio", rms))
    
    def _run_tk(self) -> None:
        """Run Tkinter event loop in thread."""
        try:
            self._root = tk.Tk()
            self._root.withdraw()
            self._root.overrideredirect(True)
            self._root.attributes('-topmost', True)
            
            try:
                self._root.attributes('-alpha', 0.95)
            except tk.TclError:
                pass
            
            # Window size
            width, height = 400, 72
            self._root.geometry(f"{width}x{height}")
            
            # Create canvas
            self._main_canvas = tk.Canvas(
                self._root, width=width, height=height,
                highlightthickness=0, bg='black'
            )
            self._main_canvas.pack(fill='both', expand=True)
            
            # Draw pill background
            self._main_canvas.create_oval(0, 0, height, height, fill='black', outline='black')
            self._main_canvas.create_oval(width-height, 0, width, height, fill='black', outline='black')
            self._main_canvas.create_rectangle(height//2, 0, width-height//2, height, fill='black', outline='black')
            
            # Stop button
            btn_cx = height // 2
            btn_cy = height // 2
            btn_r = 24
            self._btn_id = self._main_canvas.create_oval(
                btn_cx - btn_r, btn_cy - btn_r,
                btn_cx + btn_r, btn_cy + btn_r,
                fill='#e23c3c', outline='#e23c3c'
            )
            
            # Square inside button
            sq_size = 14
            self._sq_id = self._main_canvas.create_rectangle(
                btn_cx - sq_size // 2, btn_cy - sq_size // 2,
                btn_cx + sq_size // 2, btn_cy + sq_size // 2,
                fill='white', outline='white'
            )
            
            # Waveform frame
            wave_frame = tk.Frame(self._main_canvas, bg='black')
            self._canvas_window = self._main_canvas.create_window(
                height + 10, height // 2,
                window=wave_frame, anchor='w'
            )
            
            # Create waveform bars
            self._wave_canvas = tk.Canvas(
                wave_frame, width=width - height - 40, height=40,
                bg='black', highlightthickness=0
            )
            self._wave_canvas.pack()
            
            bar_width = 3
            gap = 2
            for i in range(BAR_COUNT):
                x = i * (bar_width + gap)
                bar_id = self._wave_canvas.create_rectangle(
                    x, 18, x + bar_width, 22,
                    fill='#55ff8c', outline='#55ff8c'
                )
                self._bar_ids.append(bar_id)
            
            # Transcribing label
            self._transcribing_label = tk.Label(
                self._main_canvas, text="Transcribing...",
                font=('Segoe UI', 12), fg='#55ff8c', bg='black'
            )
            self._transcribing_window = self._main_canvas.create_window(
                width // 2, height // 2,
                window=self._transcribing_label, anchor='center'
            )
            self._main_canvas.itemconfig(self._transcribing_window, state='hidden')
            
            # Bind click
            self._main_canvas.tag_bind(self._btn_id, '<Button-1>', self._on_stop_click)
            self._main_canvas.tag_bind(self._sq_id, '<Button-1>', self._on_stop_click)
            
            # Center on screen
            self._root.update_idletasks()
            screen_w = self._root.winfo_screenwidth()
            screen_h = self._root.winfo_screenheight()
            x = (screen_w - width) // 2
            y = int(screen_h * 0.10)
            self._root.geometry(f"+{x}+{y}")
            
            # Process commands
            self._process_commands()
            self._root.mainloop()
            
        except Exception as e:
            logger.error(f"Tkinter overlay error: {e}")
        finally:
            self._running = False
    
    def _process_commands(self) -> None:
        """Process queued commands."""
        if not self._running or not self._root:
            return
            
        try:
            while True:
                try:
                    cmd, data = self._command_queue.get_nowait()
                    
                    if cmd == "quit":
                        self._root.quit()
                        return
                    elif cmd == "show":
                        self._is_visible = True
                        self._is_transcribing = False
                        self._show_recording_mode()
                        self._root.deiconify()
                    elif cmd == "transcribing":
                        self._is_transcribing = True
                        self._show_transcribing_mode()
                    elif cmd == "hide":
                        self._is_visible = False
                        self._is_transcribing = False
                        self._root.withdraw()
                    elif cmd == "audio":
                        if not self._is_transcribing:
                            rms = float(data) if data else 0
                            normalized = pow(rms, 0.25) * 0.88 + 0.12
                            self._audio_levels = self._audio_levels[1:] + [min(1.0, max(0.12, normalized))]
                            if self._is_visible:
                                self._draw_waveform()
                                
                except queue.Empty:
                    break
                    
        except Exception as e:
            logger.error(f"Command processing error: {e}")
            
        self._root.after(16, self._process_commands)
    
    def _show_recording_mode(self):
        """Show recording button and waveform, hide transcribing label."""
        if self._main_canvas:
            self._main_canvas.itemconfig(self._btn_id, state='normal')
            self._main_canvas.itemconfig(self._sq_id, state='normal')
            self._main_canvas.itemconfig(self._canvas_window, state='normal')
            self._main_canvas.itemconfig(self._transcribing_window, state='hidden')
    
    def _show_transcribing_mode(self):
        """Show transcribing text, hide button and waveform."""
        if self._main_canvas:
            self._main_canvas.itemconfig(self._btn_id, state='hidden')
            self._main_canvas.itemconfig(self._sq_id, state='hidden')
            self._main_canvas.itemconfig(self._canvas_window, state='hidden')
            self._main_canvas.itemconfig(self._transcribing_window, state='normal')
    
    def _draw_waveform(self) -> None:
        """Update waveform visualization."""
        if not self._wave_canvas or not self._bar_ids:
            return
            
        for i, level in enumerate(self._audio_levels):
            if i >= len(self._bar_ids):
                break
            bar_height = max(4, int(level * 36))
            cy = 20
            bar_id = self._bar_ids[i]
            x1, _, x2, _ = self._wave_canvas.coords(bar_id)
            self._wave_canvas.coords(bar_id, x1, cy - bar_height // 2, x2, cy + bar_height // 2)
    
    def _on_stop_click(self, event):
        """Handle stop button click."""
        if self._on_stop:
            threading.Thread(target=self._on_stop, daemon=True).start()


# ================================================
# Public API
# ================================================

class RecordingOverlay:
    """Main overlay class that uses Qt or Tkinter based on availability."""
    
    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        self._on_stop = on_stop
        self._impl: Optional[QtRecordingOverlay | TkRecordingOverlay] = None
        
        if HAS_QT:
            logger.info("Using PySide6 (Qt) for overlay")
            self._impl = QtRecordingOverlay(on_stop=on_stop)
        elif HAS_TK:
            logger.info("Using Tkinter fallback for overlay")
            self._impl = TkRecordingOverlay(on_stop=on_stop)
        else:
            logger.warning("No GUI toolkit available for overlay")
            
    def start(self) -> None:
        """Start the overlay."""
        if self._impl:
            self._impl.start()
            
    def stop(self) -> None:
        """Stop the overlay."""
        if self._impl:
            self._impl.stop()
            
    def show(self) -> None:
        """Show the overlay."""
        if self._impl:
            self._impl.show()
            
    def show_transcribing(self) -> None:
        """Show overlay in transcribing mode."""
        if self._impl:
            self._impl.show_transcribing()
            
    def hide(self) -> None:
        """Hide the overlay."""
        if self._impl:
            self._impl.hide()
            
    def update_audio_level(self, rms: float) -> None:
        """Update audio level."""
        if self._impl:
            self._impl.update_audio_level(rms)


# Global overlay instance
_overlay: Optional[RecordingOverlay] = None


def get_overlay(on_stop: Optional[Callable[[], None]] = None) -> RecordingOverlay:
    """Get or create the global overlay instance."""
    global _overlay
    if _overlay is None:
        _overlay = RecordingOverlay(on_stop=on_stop)
        _overlay.start()
    return _overlay


def show_recording_overlay() -> None:
    """Show the recording overlay."""
    overlay = get_overlay()
    overlay.show()


def show_transcribing_overlay() -> None:
    """Show the overlay in transcribing mode."""
    if _overlay:
        _overlay.show_transcribing()


def hide_recording_overlay() -> None:
    """Hide the recording overlay."""
    if _overlay:
        _overlay.hide()


def update_overlay_audio(rms: float) -> None:
    """Update the overlay audio level."""
    if _overlay:
        _overlay.update_audio_level(rms)
