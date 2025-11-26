import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable

from src.config import Config


class ScriberUI:
    """Tkinter based UI to control the Scriber pipeline."""

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_save_settings: Callable[[], None],
    ):
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_save_settings = on_save_settings

        self.root = tk.Tk()
        self.root.title("Scriber Controller")
        self.root.geometry("420x500")
        self.root.resizable(False, False)

        self.status_var = tk.StringVar(value="Bereit")
        self.hotkey_var = tk.StringVar(value=Config.HOTKEY)
        self.service_var = tk.StringVar(value=Config.DEFAULT_STT_SERVICE)
        self.mode_var = tk.StringVar(value=Config.MODE)
        self.soniox_mode_var = tk.StringVar(value=Config.SONIOX_MODE)
        self.debug_var = tk.BooleanVar(value=Config.DEBUG)
        self.language_var = tk.StringVar(value=Config.LANGUAGE)
        self.mic_device_var = tk.StringVar(value=Config.MIC_DEVICE)
        self.mic_always_on_var = tk.BooleanVar(value=Config.MIC_ALWAYS_ON)
        self.api_key_var = tk.StringVar(value=Config.get_api_key(Config.DEFAULT_STT_SERVICE))
        self.custom_vocab_var = tk.StringVar(value=Config.CUSTOM_VOCAB)

        self._build_controls()

    def _build_controls(self):
        padding_opts = {"padx": 10, "pady": 6}

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **padding_opts)
        ttk.Label(status_frame, text="Status:", width=10).pack(side="left")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="blue").pack(side="left")

        control_frame = ttk.LabelFrame(self.root, text="Steuerung")
        control_frame.pack(fill="x", **padding_opts)
        ttk.Button(control_frame, text="Start", command=self.on_start).pack(side="left", padx=5, pady=8)
        ttk.Button(control_frame, text="Stop", command=self.on_stop).pack(side="left", padx=5, pady=8)

        settings_frame = ttk.LabelFrame(self.root, text="Einstellungen")
        settings_frame.pack(fill="x", **padding_opts)

        ttk.Label(settings_frame, text="STT Service").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        services = list(Config.SERVICE_LABELS.keys())
        service_combo = ttk.Combobox(settings_frame, values=[Config.SERVICE_LABELS[s] for s in services], state="readonly")
        service_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        service_combo.bind("<<ComboboxSelected>>", lambda _: self._on_service_changed(services[service_combo.current()]))
        current_index = services.index(Config.DEFAULT_STT_SERVICE) if Config.DEFAULT_STT_SERVICE in services else 0
        service_combo.current(current_index)

        ttk.Label(settings_frame, text="API Key").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.api_key_var, show="*").grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Hotkey").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.hotkey_var).grid(row=2, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Modus").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        mode_combo = ttk.Combobox(settings_frame, values=["toggle", "push_to_talk"], textvariable=self.mode_var, state="readonly")
        mode_combo.grid(row=3, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Soniox Mode").grid(row=4, column=0, sticky="w", padx=4, pady=4)
        soniox_mode_combo = ttk.Combobox(settings_frame, values=["realtime", "async"], textvariable=self.soniox_mode_var, state="readonly")
        soniox_mode_combo.grid(row=4, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Custom Vocab").grid(row=5, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.custom_vocab_var).grid(row=5, column=1, sticky="ew", padx=4, pady=4)

        ttk.Checkbutton(settings_frame, text="Debug logging", variable=self.debug_var).grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        ttk.Label(settings_frame, text="Sprache / Language").grid(row=7, column=0, sticky="w", padx=4, pady=4)
        languages = [
            ("auto", "🌐 Auto"),
            ("en", "🇺🇸 English"),
            ("de", "🇩🇪 Deutsch"),
            ("fr", "🇫🇷 Français"),
            ("es", "🇪🇸 Español"),
            ("it", "🇮🇹 Italiano"),
            ("pt", "🇵🇹 Português"),
            ("nl", "🇳🇱 Nederlands"),
        ]
        lang_combo = ttk.Combobox(settings_frame, values=[label for _, label in languages], state="readonly")
        lang_combo.grid(row=7, column=1, sticky="ew", padx=4, pady=4)
        try:
            lang_idx = [code for code, _ in languages].index(Config.LANGUAGE)
        except ValueError:
            lang_idx = 0
        lang_combo.current(lang_idx)
        lang_combo.bind("<<ComboboxSelected>>", lambda _: self.language_var.set(languages[lang_combo.current()][0]))
        self.language_var.set(languages[lang_idx][0])

        ttk.Label(settings_frame, text="Microphone").grid(row=8, column=0, sticky="w", padx=4, pady=4)
        devices = self._get_microphones()
        mic_combo = ttk.Combobox(settings_frame, values=[d[1] for d in devices], state="readonly", width=48)
        mic_combo.grid(row=8, column=1, sticky="ew", padx=4, pady=4)
        try:
            mic_idx = [d[0] for d in devices].index(Config.MIC_DEVICE)
        except ValueError:
            mic_idx = 0
        mic_combo.current(mic_idx)
        mic_combo.bind("<<ComboboxSelected>>", lambda _: self.mic_device_var.set(devices[mic_combo.current()][0]))
        self.mic_device_var.set(devices[mic_idx][0])

        ttk.Checkbutton(settings_frame, text="Mic always on (faster start)", variable=self.mic_always_on_var).grid(row=9, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        settings_frame.columnconfigure(1, weight=1)

        ttk.Button(self.root, text="Einstellungen speichern", command=self.on_save_settings).pack(fill="x", padx=12, pady=10)

        info_text = (
            "Hotkey Toggle: Start/Stop.\n"
            "Push-to-Talk: Halten Sie den Hotkey gedrückt, um aufzunehmen."
        )
        ttk.Label(self.root, text=info_text, wraplength=380, foreground="#555").pack(fill="x", padx=12, pady=4)

    def _get_microphones(self):
        try:
            import sounddevice as sd
            devices = []
            default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
            devices.append(("default", "🎙️ System default"))
            seen_labels = set()
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                name = dev.get("name", f"Device {idx}")
                label = f"{name} (id {idx})"
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                prefix = "🎤 "
                if idx == default:
                    label = f"{prefix}{label} • default"
                else:
                    label = f"{prefix}{label}"
                devices.append((str(idx), label))
            return devices
        except Exception:
            return [("default", "🎙️ System default")]

    def _on_service_changed(self, service_name: str):
        self.service_var.set(service_name)
        self.api_key_var.set(Config.get_api_key(service_name))

    def update_status(self, status: str):
        def _setter():
            self.status_var.set(status)
        self.root.after(0, _setter)

    def show_error(self, message: str):
        messagebox.showerror("Scriber", message)

    def run(self):
        self.root.mainloop()
