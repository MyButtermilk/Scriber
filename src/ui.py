import tkinter as tk
import customtkinter as ctk
from typing import Any, Callable, Mapping, Optional, Sequence
from pathlib import Path
import urllib.request
import threading
import time
import math
import re
from PIL import Image
import os

from src.config import Config
from loguru import logger

# Configure CustomTkinter defaults
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("dark-blue")

# Constants for UI
COLOR_PRIMARY = "#2563eb" # Blue-600
COLOR_PRIMARY_HOVER = "#1d4ed8" # Blue-700
COLOR_DANGER = "#ef4444" # Red-500
COLOR_DANGER_HOVER = "#dc2626" # Red-600
COLOR_WARNING = "#f59e0b" # Amber-500
COLOR_SIDEBAR = "#1e293b" # Slate-800
COLOR_BG = "#0f172a" # Slate-900 (Darker bg if possible, but ctk controls theme)

_DEVICE_NAME_PREFIX_RE = re.compile(r"\((\d+)\s*-\s*", re.IGNORECASE)


def _normalize_device_name(name: str) -> str:
    if not name:
        return ""
    return _DEVICE_NAME_PREFIX_RE.sub("(", name).strip().lower()

class AudioVisualizer(ctk.CTkFrame):
    """Responsive mic visualizer with smooth bars + peak hold.

    Accepts either a single RMS float (0..1) or a dict containing precomputed
    band levels (0..1). All drawing happens on the Tk thread; callers should
    only push new targets.
    """

    GRADIENT_COLORS = [
        "#3b82f6",  # blue
        "#06b6d4",  # cyan
        "#22c55e",  # green
        "#a3e635",  # lime
        "#fbbf24",  # amber
        "#fb7185",  # rose
    ]

    def __init__(self, master, width: int = 320, height: int = 72, bars: int = 24, **kwargs):
        super().__init__(master, width=width, height=height, fg_color="transparent", **kwargs)

        self.n_bars = int(bars)
        self.width = int(width)
        self.height = int(height)
        self.gap = 3
        self.padding_y = 8
        self.bar_width = (self.width - (self.n_bars - 1) * self.gap) / self.n_bars

        self._target_rms_level = 0.0
        self._target_bands: Optional[list[float]] = None
        self._target_peak: Optional[float] = None
        self._last_input_ts = 0.0
        self._phase = 0.0

        self._current = [0.0] * self.n_bars
        self._peaks = [0.0] * self.n_bars

        self.canvas = tk.Canvas(
            self,
            width=self.width,
            height=self.height,
            highlightthickness=0,
            bg=COLOR_BG,
            bd=0,
            relief="flat",
        )
        self.canvas.pack(fill="both", expand=True)

        # Baseline + subtle grid
        self.canvas.create_line(0, self.height - 1, self.width, self.height - 1, fill="#111827")

        self.bar_ids: list[int] = []
        self.peak_ids: list[int] = []

        for i in range(self.n_bars):
            x0 = i * (self.bar_width + self.gap)
            x1 = x0 + self.bar_width
            bar = self.canvas.create_rectangle(x0, self.height - 2, x1, self.height, fill=self.GRADIENT_COLORS[0], outline="")
            peak = self.canvas.create_rectangle(x0, self.height - 4, x1, self.height - 2, fill="", outline="")
            self.bar_ids.append(bar)
            self.peak_ids.append(peak)

        self.after(50, self._animate)

    @staticmethod
    def _clamp01(value: float) -> float:
        return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value

    @classmethod
    def _color_for(cls, level: float) -> str:
        level = cls._clamp01(level)
        idx = int(level * (len(cls.GRADIENT_COLORS) - 1))
        return cls.GRADIENT_COLORS[idx]

    @staticmethod
    def _rms_to_level(rms: float) -> float:
        rms = max(0.0, float(rms))
        db = 20.0 * math.log10(rms + 1e-6)  # ~[-120..0]
        # Voice-friendly mapping: -60dB => 0, -15dB => 1
        return AudioVisualizer._clamp01((db + 60.0) / 45.0)

    def push(self, level: float | Mapping[str, Any]):
        """Thread-safe-ish: updates only target numbers (no Tk calls)."""
        now = time.monotonic()
        self._last_input_ts = now

        if isinstance(level, Mapping):
            rms = float(level.get("rms", 0.0) or 0.0)
            self._target_rms_level = self._rms_to_level(rms)
            bands = level.get("bands")
            if isinstance(bands, Sequence):
                self._target_bands = self._fit_bands([float(x) for x in bands])
            else:
                self._target_bands = None
            peak = level.get("peak")
            self._target_peak = float(peak) if peak is not None else None
            return

        self._target_rms_level = self._rms_to_level(float(level))
        self._target_bands = None
        self._target_peak = None

    def _fit_bands(self, bands: Sequence[float]) -> list[float]:
        if not bands:
            return [0.0] * self.n_bars
        if len(bands) == self.n_bars:
            return [self._clamp01(v) for v in bands]
        # Resample (linear index) to match bar count.
        src_n = len(bands)
        out: list[float] = []
        for i in range(self.n_bars):
            src_i = int((i / max(1, self.n_bars - 1)) * (src_n - 1))
            out.append(self._clamp01(float(bands[src_i])))
        return out

    def _synthetic_bands(self, level: float) -> list[float]:
        # Smooth, non-random "wave" so it still looks premium without spectrum data.
        level = self._clamp01(level)
        base = 0.02
        amp = 0.10 + 0.90 * level
        bands: list[float] = []
        for i in range(self.n_bars):
            w = 0.55 + 0.45 * math.sin(self._phase + i * 0.45)
            center = 1.0 - abs(i - (self.n_bars - 1) / 2) / ((self.n_bars - 1) / 2 + 1e-6)
            bands.append(self._clamp01(base + amp * (0.25 + 0.75 * w) * (0.55 + 0.45 * center)))
        return bands

    def _animate(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        self._phase += 0.18
        if self._target_bands is not None:
            gain = self._target_rms_level ** 0.7 if self._target_rms_level > 0.0 else 0.0
            target = [min(1.0, t * gain) for t in self._target_bands]
        else:
            target = self._synthetic_bands(self._target_rms_level)

        # If we haven't received an update recently, fall back to a subtle idle pulse.
        idle = (time.monotonic() - self._last_input_ts) > 0.8
        if idle:
            idle_amp = 0.06 + 0.04 * math.sin(self._phase * 0.6)
            target = [min(1.0, t * 0.25 + idle_amp * 0.35) for t in target]

        for i in range(self.n_bars):
            t = self._clamp01(float(target[i]))
            cur = self._current[i]
            # Fast attack, slower release.
            k = 0.55 if t > cur else 0.18
            cur = cur + (t - cur) * k
            self._current[i] = cur

            # Peak hold.
            peak = self._peaks[i]
            if cur >= peak:
                peak = cur
            else:
                peak *= 0.965
            self._peaks[i] = peak

            x0 = i * (self.bar_width + self.gap)
            x1 = x0 + self.bar_width
            max_h = max(1.0, self.height - self.padding_y)
            bar_h = max(2.0, cur * max_h)
            y1 = self.height
            y0 = y1 - bar_h

            self.canvas.coords(self.bar_ids[i], x0, y0, x1, y1)
            self.canvas.itemconfig(self.bar_ids[i], fill=self._color_for(cur))

            peak_y = max(2.0, y1 - (peak * max_h) - 2.0)
            self.canvas.coords(self.peak_ids[i], x0, peak_y, x1, peak_y + 2.0)
            self.canvas.itemconfig(self.peak_ids[i], fill="#f97316" if peak > 0.75 else "#eab308")

        self.after(30, self._animate)


class MicPreviewStream:
    """Lightweight mic monitor stream for UI visualization (no STT required)."""

    def __init__(
        self,
        on_audio_visual: Callable[[Mapping[str, Any]], None],
        *,
        bars: int = 24,
        sample_rate: int = 16000,
        block_size: int = 512,
    ) -> None:
        self._on_audio_visual = on_audio_visual
        self._bars = int(bars)
        self._preferred_sample_rate = int(sample_rate)
        self._block_size = int(block_size)

        self._lock = threading.Lock()
        self._stream = None
        self._active = False
        self._last_open_attempt = 0.0
        self._open_retry_secs = 1.0
        self._last_open_warning = 0.0

        # Cached FFT layout (rebuilt when frames/sample_rate change)
        self._fft_size: Optional[int] = None
        self._fft_sr: Optional[int] = None
        self._fft_window = None
        self._band_slices: Optional[list[tuple[int, int]]] = None

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True

        self._open_stream()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            stream = self._stream
            self._stream = None

        if stream is None:
            return

        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def restart(self) -> None:
        with self._lock:
            should_run = self._active
        self.stop()
        if should_run:
            self.start()

    def tick(self) -> None:
        """Retry opening the stream if it is desired but currently unavailable."""
        with self._lock:
            active = self._active
            has_stream = self._stream is not None
        if not active or has_stream:
            return
        now = time.monotonic()
        if (now - self._last_open_attempt) < self._open_retry_secs:
            return
        self._open_stream()

    def _resolve_device(self) -> Optional[int]:
        dev = getattr(Config, "MIC_DEVICE", "default")
        if dev in (None, "", "default"):
            return None
        # Try numeric ID first
        try:
            return int(dev)
        except (ValueError, TypeError):
            pass
        target = str(dev).strip()
        target_norm = _normalize_device_name(target)
        # Try to find device by name
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    name = d.get("name")
                    if name == target or (target_norm and _normalize_device_name(name) == target_norm):
                        return i
        except Exception:
            pass
        return None

    def _open_stream(self) -> None:
        self._last_open_attempt = time.monotonic()
        try:
            import sounddevice as sd
        except Exception as exc:
            logger.warning(f"Mic preview disabled (sounddevice import failed): {exc}")
            return

        device_index = self._resolve_device()
        sample_rate = self._preferred_sample_rate

        # Some devices reject 16k; fall back to device default if needed.
        fallback_sample_rate = None
        try:
            if device_index is not None:
                info = sd.query_devices(device=device_index, kind="input")
                fallback_sample_rate = int(info.get("default_samplerate") or 0) or None
        except Exception:
            fallback_sample_rate = None

        for sr in [sample_rate, fallback_sample_rate]:
            if sr is None:
                continue
            try:
                stream = sd.InputStream(
                    samplerate=int(sr),
                    channels=1,
                    dtype="float32",
                    blocksize=self._block_size,
                    device=device_index,
                    callback=self._callback,
                )
                stream.start()
                with self._lock:
                    if not self._active:
                        stream.stop()
                        stream.close()
                        return
                    self._stream = stream
                return
            except Exception as exc:
                logger.debug(f"Mic preview stream open failed (sr={sr}, device={device_index}): {exc}")

        now = time.monotonic()
        if (now - self._last_open_warning) > 5.0:
            self._last_open_warning = now
            logger.warning("Mic preview stream could not be started (device busy or unsupported format).")

    def _ensure_fft_layout(self, *, fft_size: int, sample_rate: int):
        if self._fft_size == fft_size and self._fft_sr == sample_rate and self._band_slices is not None and self._fft_window is not None:
            return

        import numpy as np

        self._fft_size = int(fft_size)
        self._fft_sr = int(sample_rate)
        self._fft_window = np.hanning(self._fft_size).astype(np.float32)

        freqs = np.fft.rfftfreq(self._fft_size, d=1.0 / self._fft_sr)
        min_hz = 70.0
        max_hz = min(8000.0, float(self._fft_sr) / 2.0)
        edges = np.logspace(np.log10(min_hz), np.log10(max_hz), self._bars + 1)
        idx_edges = np.searchsorted(freqs, edges)

        slices: list[tuple[int, int]] = []
        for i in range(self._bars):
            start = int(idx_edges[i])
            end = int(idx_edges[i + 1])
            if end <= start:
                end = start + 1
            slices.append((start, end))
        self._band_slices = slices

    def _callback(self, indata, frames, time_info, status):
        if status:
            # Non-fatal (under/overruns etc.)
            logger.debug(f"Mic preview status: {status}")

        with self._lock:
            if not self._active:
                return
            stream = self._stream
        if stream is None:
            return

        try:
            import numpy as np

            mono = indata
            if getattr(mono, "ndim", 1) > 1:
                mono = mono.mean(axis=1)
            mono = mono.astype(np.float32, copy=False)

            rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
            peak = float(np.max(np.abs(mono))) if mono.size else 0.0

            try:
                sr = int(getattr(stream, "samplerate", None) or self._preferred_sample_rate)
            except Exception:
                sr = self._preferred_sample_rate

            self._ensure_fft_layout(fft_size=int(mono.shape[0]), sample_rate=sr)

            windowed = mono * self._fft_window
            mag = np.abs(np.fft.rfft(windowed)).astype(np.float32)
            if mag.size:
                mag[0] = 0.0  # drop DC

            bands: list[float] = []
            max_band = 0.0
            for start, end in (self._band_slices or []):
                v = float(np.mean(mag[start:end])) if end > start else 0.0
                # Mild compression for nicer visuals.
                v = float(np.log1p(v * 12.0))
                bands.append(v)
                if v > max_band:
                    max_band = v

            if max_band > 0:
                bands = [float(b / max_band) for b in bands]
            else:
                bands = [0.0 for _ in range(self._bars)]

            self._on_audio_visual({"rms": rms, "peak": peak, "bands": bands})
        except Exception:
            # Never raise from an audio callback.
            return

class ScriberUI(ctk.CTk):
    """Premium CustomTkinter UI V2 for Scriber."""

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_save_settings: Callable[[], None],
    ):
        super().__init__()

        self.on_start = on_start
        self.on_stop = on_stop
        self.on_save_settings = on_save_settings

        # Basic Window Setup
        self.title("Scriber")
        self.geometry("900x700")
        self.minsize(800, 600)
        self.configure(fg_color=COLOR_BG)

        # Thread-safe update buffers (pipeline/audio callbacks may come from other threads)
        self._pending_lock = threading.Lock()
        self._pending_status: Optional[str] = None
        self._pending_audio: Optional[float | Mapping[str, Any]] = None
        self._mic_preview: Optional[MicPreviewStream] = None

        # Variables
        self.status_var = ctk.StringVar(value="Ready")
        self.hotkey_var = ctk.StringVar(value=Config.HOTKEY)
        self.service_var = ctk.StringVar(value=Config.DEFAULT_STT_SERVICE)
        self.mode_var = ctk.StringVar(value=Config.MODE)
        self.soniox_mode_var = ctk.StringVar(value=Config.SONIOX_MODE)
        self.debug_var = ctk.BooleanVar(value=Config.DEBUG)
        self.language_var = ctk.StringVar(value=Config.LANGUAGE)
        self.mic_device_var = ctk.StringVar(value=Config.MIC_DEVICE)
        self.mic_always_on_var = ctk.BooleanVar(value=Config.MIC_ALWAYS_ON)
        self.api_key_var = ctk.StringVar(value=Config.get_api_key(Config.DEFAULT_STT_SERVICE))
        self.custom_vocab_var = ctk.StringVar(value=Config.CUSTOM_VOCAB)
        
        # Resources
        self.icons = {}
        self._load_icons()
        
        # Fonts
        self.font_hero = ("Inter", 24, "bold")
        self.font_header = ("Inter", 18, "bold")
        self.font_main = ("Inter", 14)
        self.font_small = ("Inter", 12)

        # Layout Configuration
        self.grid_columnconfigure(1, weight=1) # Content Area
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_content_area()

        # Select Dashboard by default
        self._select_nav("dashboard")

        # Start a mic-preview stream so the visualizer reacts even before STT starts.
        try:
            bars = getattr(self.visualizer, "n_bars", 24)
            self._mic_preview = MicPreviewStream(
                on_audio_visual=self.update_amplitude,
                bars=bars,
                sample_rate=getattr(Config, "SAMPLE_RATE", 16000),
            )
            self._mic_preview.start()
        except Exception as exc:
            logger.debug(f"Mic preview init failed: {exc}")
            self._mic_preview = None

        self.after(33, self._ui_tick)

    def _load_icons(self):
        """Load or download icons."""
        assets_dir = Path(__file__).parent / "assets" / "icons"
        assets_dir.mkdir(parents=True, exist_ok=True)
        
        # Map: name -> url (using material icons or similar reliable source)
        # Using a reliable CDN for white material icons
        base_url = "https://img.icons8.com/ios-filled/50/ffffff"
        icon_map = {
            "home": f"{base_url}/home.png",
            "settings": f"{base_url}/settings.png",
            "info": f"{base_url}/info.png",
            "mic": f"{base_url}/microphone.png",
            "save": f"{base_url}/save.png",
            "play": f"{base_url}/play.png",
            "stop": f"{base_url}/stop.png",
        }

        for name, url in icon_map.items():
            path = assets_dir / f"{name}.png"
            if not path.exists():
                try:
                    # Fake user agent to avoid some CDN blocks
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req) as response, open(path, 'wb') as out_file:
                        out_file.write(response.read())
                except Exception as e:
                    print(f"Failed to download icon {name}: {e}")
                    continue
            
            try:
                # Store as CTkImage
                # Size 24x24 for nav, maybe bigger for others
                pil_img = Image.open(path)
                self.icons[name] = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(24, 24))
                # Create a larger version for Hero
                self.icons[f"{name}_lg"] = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(48, 48))
            except Exception:
                pass

    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(4, weight=1) # Spacer

        # App Title
        ctk.CTkLabel(self.sidebar, text="Scriber", font=("Inter", 22, "bold")).grid(row=0, column=0, padx=20, pady=(20, 10))

        # Nav Buttons
        self.nav_btn_dashboard = self._create_nav_button("Dashboard", "home", lambda: self._select_nav("dashboard"), 1)
        self.nav_btn_settings = self._create_nav_button("Settings", "settings", lambda: self._select_nav("settings"), 2)
        self.nav_btn_about = self._create_nav_button("About", "info", lambda: self._select_nav("about"), 3)

        # Bottom
        ctk.CTkLabel(self.sidebar, text="v1.0", font=self.font_small, text_color="gray50").grid(row=5, column=0, pady=20)

    def _create_nav_button(self, text, icon_name, command, row):
        btn = ctk.CTkButton(
            self.sidebar, 
            corner_radius=0, 
            height=50, 
            border_spacing=10, 
            text=f"  {text}",
            fg_color="transparent", 
            text_color=("gray10", "gray90"), 
            hover_color=("gray70", "gray30"),
            image=self.icons.get(icon_name), 
            anchor="w", 
            command=command,
            font=self.font_main
        )
        btn.grid(row=row, column=0, sticky="ew")
        return btn

    def _build_content_area(self):
        # Create Frames for each view
        self.frame_dashboard = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_settings = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent") # Scrollable for settings
        self.frame_about = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")

        # Layout Dashboard
        self.frame_dashboard.grid_columnconfigure(0, weight=1)
        self._build_dashboard()
        self._build_settings()
        self._build_about()

    def _build_dashboard(self):
        # Center Content
        container = ctk.CTkFrame(self.frame_dashboard, fg_color="transparent")
        container.grid(row=0, column=0, sticky="ns")
        
        # Spacer
        ctk.CTkLabel(container, text="").pack(pady=(60, 20))

        # Hero Button
        self.hero_btn = ctk.CTkButton(
            container,
            text="",
            width=160,
            height=160,
            corner_radius=80,
            image=self.icons.get("mic_lg"),
            fg_color=COLOR_PRIMARY,
            hover_color=COLOR_PRIMARY_HOVER,
            command=self._on_action_click
        )
        self.hero_btn.pack(pady=20)

        # Status
        self.lbl_status = ctk.CTkLabel(container, textvariable=self.status_var, font=self.font_hero)
        self.lbl_status.pack(pady=10)

        # Visualizer
        self.visualizer = AudioVisualizer(container, width=320, height=72, bars=24)
        self.visualizer.pack(pady=(10, 30))

        # Mic Selection (Quick)
        mic_frame = ctk.CTkFrame(container, fg_color=("gray85", "gray20"), corner_radius=10)
        mic_frame.pack(pady=30, padx=20, fill="x")
        
        ctk.CTkLabel(mic_frame, text="Microphone Source", font=self.font_main, text_color="gray60").pack(pady=(10, 5))
        
        devices = self._get_microphones()
        dev_names = [d[1] for d in devices]
        
        self.mic_combo = ctk.CTkOptionMenu(
            mic_frame, 
            values=dev_names, 
            command=self._on_mic_change,
            width=300,
            font=self.font_main,
            fg_color=("gray90", "gray25"),
            button_color=("gray80", "gray30"),
            text_color=("gray10", "gray90")
        )
        self.mic_combo.pack(pady=(0, 15), padx=15)

        # Init mic selection
        current_mic = Config.MIC_DEVICE
        favorite_mic = getattr(Config, "FAVORITE_MIC", "") or ""
        if favorite_mic and devices:
            def _label_to_name(label: str) -> str:
                suffix = " (Default)"
                return label[:-len(suffix)] if label.endswith(suffix) else label

            favorite_id = next((d[0] for d in devices if _label_to_name(d[1]) == favorite_mic), None)
            current_available = any(d[0] == current_mic for d in devices)
            if favorite_id and (current_mic in ("default", "", None) or not current_available):
                current_mic = favorite_id

        display = next((d[1] for d in devices if d[0] == current_mic), dev_names[0] if dev_names else "Default")
        self.mic_device_var.set(current_mic)
        self.mic_combo.set(display)

    def _build_settings(self):
        # Helper to create sections
        def add_section(title):
            l = ctk.CTkLabel(self.frame_settings, text=title, font=self.font_header, anchor="w")
            l.pack(fill="x", pady=(20, 10), padx=20)
            f = ctk.CTkFrame(self.frame_settings)
            f.pack(fill="x", padx=20, pady=0)
            f.grid_columnconfigure(1, weight=1)
            return f

        # --- Service ---
        s_frame = add_section("Transcription Service")
        
        ctk.CTkLabel(s_frame, text="Provider", font=self.font_main).grid(row=0, column=0, padx=15, pady=15, sticky="w")
        services = [s for s in Config.SERVICE_LABELS.keys() if s != "soniox_async"]
        labels = [Config.SERVICE_LABELS[s] for s in services]
        self.combo_service = ctk.CTkOptionMenu(s_frame, values=labels, command=self._on_service_change_ui, font=self.font_main)
        self.combo_service.grid(row=0, column=1, padx=15, pady=15, sticky="ew")
        
        # Init Service
        init_svc = Config.DEFAULT_STT_SERVICE if Config.DEFAULT_STT_SERVICE != "soniox_async" else "soniox"
        self.combo_service.set(Config.SERVICE_LABELS.get(init_svc, init_svc))

        self.lbl_soniox = ctk.CTkLabel(s_frame, text="Mode", font=self.font_main)
        self.seg_soniox = ctk.CTkSegmentedButton(s_frame, values=["realtime", "async"], variable=self.soniox_mode_var, font=self.font_main)
        # Visibility handled later

        ctk.CTkLabel(s_frame, text="API Key", font=self.font_main).grid(row=2, column=0, padx=15, pady=15, sticky="w")
        ctk.CTkEntry(s_frame, textvariable=self.api_key_var, show="*", font=self.font_main).grid(row=2, column=1, padx=15, pady=15, sticky="ew")

        # --- Behavior ---
        b_frame = add_section("Behavior")

        ctk.CTkLabel(b_frame, text="Activation", font=self.font_main).grid(row=0, column=0, padx=15, pady=15, sticky="w")
        ctk.CTkSegmentedButton(b_frame, values=["toggle", "push_to_talk"], variable=self.mode_var, font=self.font_main).grid(row=0, column=1, padx=15, pady=15, sticky="ew")
        
        ctk.CTkLabel(b_frame, text="Hotkey", font=self.font_main).grid(row=1, column=0, padx=15, pady=15, sticky="w")
        ctk.CTkEntry(b_frame, textvariable=self.hotkey_var, font=self.font_main).grid(row=1, column=1, padx=15, pady=15, sticky="ew")
        
        ctk.CTkSwitch(b_frame, text="Keep Microphone Alive (Faster)", variable=self.mic_always_on_var, font=self.font_main).grid(row=2, column=0, columnspan=2, padx=15, pady=15, sticky="w")

        # --- General ---
        g_frame = add_section("General")
        
        ctk.CTkLabel(g_frame, text="Language", font=self.font_main).grid(row=0, column=0, padx=15, pady=15, sticky="w")
        langs = [("auto", "🌐 Auto"), ("en", "🇺🇸 English"), ("de", "🇩🇪 Deutsch"), ("fr", "🇫🇷 Français"), ("es", "🇪🇸 Español"), ("it", "🇮🇹 Italiano")]
        self.lang_map = {l[1]: l[0] for l in langs}
        rev_map = {l[0]: l[1] for l in langs}
        self.combo_lang = ctk.CTkOptionMenu(g_frame, values=[l[1] for l in langs], command=self._on_lang_change, font=self.font_main)
        self.combo_lang.grid(row=0, column=1, padx=15, pady=15, sticky="ew")
        self.combo_lang.set(rev_map.get(Config.LANGUAGE, "🌐 Auto"))

        ctk.CTkLabel(g_frame, text="Custom Vocab", font=self.font_main).grid(row=1, column=0, padx=15, pady=15, sticky="w")
        ctk.CTkEntry(g_frame, textvariable=self.custom_vocab_var, placeholder_text="Name, Brand, App...", font=self.font_main).grid(row=1, column=1, padx=15, pady=15, sticky="ew")

        ctk.CTkSwitch(g_frame, text="Debug Logging", variable=self.debug_var, font=self.font_main).grid(row=2, column=0, columnspan=2, padx=15, pady=15, sticky="w")

        # Save
        ctk.CTkButton(self.frame_settings, text="Save Settings", command=self.on_save_settings, height=40, font=self.font_header, image=self.icons.get("save")).pack(fill="x", padx=20, pady=30)

        self._update_soniox_visibility(init_svc)

    def _build_about(self):
        f = self.frame_about
        ctk.CTkLabel(f, text="Scriber", font=("Inter", 32, "bold")).pack(pady=(100, 10))
        ctk.CTkLabel(f, text="The AI-Driven Voice Dictation Tool", font=("Inter", 16)).pack(pady=5)
        ctk.CTkLabel(f, text="Version 1.0.0", font=self.font_small, text_color="gray50").pack(pady=20)

    # --- Logic ---

    def _select_nav(self, name):
        # Reset buttons to transparent
        for btn in [self.nav_btn_dashboard, self.nav_btn_settings, self.nav_btn_about]:
            btn.configure(fg_color="transparent")
        
        # Highlight selected
        if name == "dashboard":
            self.nav_btn_dashboard.configure(fg_color=("gray75", "gray25"))
            self._show_frame(self.frame_dashboard)
        elif name == "settings":
            self.nav_btn_settings.configure(fg_color=("gray75", "gray25"))
            self._show_frame(self.frame_settings)
        elif name == "about":
            self.nav_btn_about.configure(fg_color=("gray75", "gray25"))
            self._show_frame(self.frame_about)

    def _show_frame(self, frame):
        self.frame_dashboard.grid_forget()
        self.frame_settings.grid_forget()
        self.frame_about.grid_forget()
        
        frame.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)

    def _on_action_click(self):
        status = self.status_var.get()
        if "Listening" in status or "Transcribing" in status:
            self.on_stop()
        else:
            self.on_start()

    def update_status(self, status: str):
        with self._pending_lock:
            self._pending_status = status

    def update_amplitude(self, level: float | Mapping[str, Any]):
        with self._pending_lock:
            self._pending_audio = level

    def set_mic_preview_enabled(self, enabled: bool) -> None:
        preview = getattr(self, "_mic_preview", None)
        if not preview:
            return
        if enabled:
            preview.start()
        else:
            preview.stop()

    def _apply_status_ui(self, status: str) -> None:
        self.status_var.set(status)

        busy = any(word in status for word in ("Listening", "Transcribing", "Stopping"))
        self.set_mic_preview_enabled(not busy)

        if "Listening" in status:
            self.hero_btn.configure(
                fg_color=COLOR_DANGER,
                hover_color=COLOR_DANGER_HOVER,
                image=self.icons.get("stop_lg", self.icons.get("mic_lg")),
            )
            self.lbl_status.configure(text_color=COLOR_DANGER)
        elif "Transcribing" in status or "Stopping" in status:
            self.hero_btn.configure(fg_color=COLOR_WARNING, hover_color=COLOR_WARNING)
            self.lbl_status.configure(text_color=COLOR_WARNING)
        else:
            self.hero_btn.configure(fg_color=COLOR_PRIMARY, hover_color=COLOR_PRIMARY_HOVER, image=self.icons.get("mic_lg"))
            self.lbl_status.configure(text_color=("gray20", "gray90"))

    def _ui_tick(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        with self._pending_lock:
            status = self._pending_status
            audio = self._pending_audio
            self._pending_status = None
            self._pending_audio = None

        if status is not None:
            try:
                self._apply_status_ui(status)
            except Exception:
                pass

        if audio is not None and hasattr(self, "visualizer"):
            try:
                self.visualizer.push(audio)
            except Exception:
                pass

        if self._mic_preview:
            try:
                self._mic_preview.tick()
            except Exception:
                pass

        self.after(33, self._ui_tick)

    def _get_microphones(self):
        try:
            import sounddevice as sd
            devices = []
            default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
            seen = set()
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) <= 0: continue
                name = dev.get("name", f"Dev {idx}")
                if name in seen: continue
                seen.add(name)
                lbl = f"{name} (Default)" if idx == default else name
                devices.append((str(idx), lbl))
            return devices
        except: return [("default", "Default Mic")]

    def _on_mic_change(self, choice):
        devs = self._get_microphones()
        for i, l in devs:
            if l == choice:
                self.mic_device_var.set(i)
                self.on_save_settings()
                if self._mic_preview:
                    self._mic_preview.restart()
                break

    def _on_service_change_ui(self, choice):
        key = next((k for k, v in Config.SERVICE_LABELS.items() if v == choice), "soniox")
        self.service_var.set(key)
        self.api_key_var.set(Config.get_api_key(key))
        self._update_soniox_visibility(key)
        self.on_save_settings()

    def _on_lang_change(self, choice):
        self.language_var.set(self.lang_map.get(choice, "auto"))
        self.on_save_settings()

    def _update_soniox_visibility(self, key):
        if key == "soniox":
            self.lbl_soniox.grid(row=1, column=0, padx=15, pady=15, sticky="w")
            self.seg_soniox.grid(row=1, column=1, padx=15, pady=15, sticky="ew")
        else:
            self.lbl_soniox.grid_forget()
            self.seg_soniox.grid_forget()
            
    def show_error(self, msg):
        from tkinter import messagebox
        messagebox.showerror("Error", msg)

    def run(self):
        self.mainloop()
    
    def quit(self):
        try:
            if self._mic_preview:
                self._mic_preview.stop()
        except Exception:
            pass
        self.destroy()
