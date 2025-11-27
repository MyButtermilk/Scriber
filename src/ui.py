import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable
from pathlib import Path
import urllib.request

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
        self.root.geometry("640x620")
        self.root.minsize(520, 560)
        self.root.resizable(True, True)
        self.style = ttk.Style()

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
        self.flag_images = {}

        self._build_controls()

    def _update_soniox_mode_visibility(self, service_name: str):
        visible = service_name == "soniox"
        if visible:
            self.soniox_mode_label.grid()
            self.soniox_mode_combo.grid()
        else:
            self.soniox_mode_label.grid_remove()
            self.soniox_mode_combo.grid_remove()

    def _load_flag_images(self, languages):
        flags_dir = Path(__file__).resolve().parent / "assets" / "flags"
        flags_dir.mkdir(parents=True, exist_ok=True)
        for lang_code, _, flag_code in languages:
            cc = flag_code or lang_code
            if cc is None or cc == "auto":
                continue
            target = flags_dir / f"{cc}.png"
            if not target.exists():
                url = f"https://flagcdn.com/24x18/{cc}.png"
                try:
                    urllib.request.urlretrieve(url, target)
                except Exception:
                    continue
            try:
                self.flag_images[flag_code] = tk.PhotoImage(file=str(target))
            except Exception:
                # fallback: keep no image; text will be used
                pass

    def _autosave(self):
        try:
            self.on_save_settings()
        except Exception:
            pass

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
        # Show Soniox once; async is selected via the Soniox mode dropdown.
        services = [s for s in Config.SERVICE_LABELS.keys() if s != "soniox_async"]
        service_labels = [Config.SERVICE_LABELS[s] for s in services]
        service_combo = ttk.Combobox(settings_frame, values=service_labels, state="readonly")
        service_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        service_combo.bind("<<ComboboxSelected>>", lambda _: (self._on_service_changed(services[service_combo.current()]), self._autosave()))
        initial_service = Config.DEFAULT_STT_SERVICE if Config.DEFAULT_STT_SERVICE != "soniox_async" else "soniox"
        if Config.DEFAULT_STT_SERVICE == "soniox_async":
            self.soniox_mode_var.set("async")
        current_index = services.index(initial_service) if initial_service in services else 0
        service_combo.current(current_index)
        # Ensure service var reflects normalized initial value
        self.service_var.set(initial_service)

        ttk.Label(settings_frame, text="API Key").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.api_key_var, show="*").grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Hotkey").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.hotkey_var).grid(row=2, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(settings_frame, text="Modus").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        mode_combo = ttk.Combobox(settings_frame, values=["toggle", "push_to_talk"], textvariable=self.mode_var, state="readonly")
        mode_combo.grid(row=3, column=1, sticky="ew", padx=4, pady=4)
        mode_combo.bind("<<ComboboxSelected>>", lambda _: self._autosave())

        self.soniox_mode_label = ttk.Label(settings_frame, text="Soniox Mode")
        self.soniox_mode_label.grid(row=4, column=0, sticky="w", padx=4, pady=4)
        soniox_mode_combo = ttk.Combobox(settings_frame, values=["realtime", "async"], textvariable=self.soniox_mode_var, state="readonly")
        self.soniox_mode_combo = soniox_mode_combo
        soniox_mode_combo.grid(row=4, column=1, sticky="ew", padx=4, pady=4)
        soniox_mode_combo.bind("<<ComboboxSelected>>", lambda _: self._autosave())
        self._update_soniox_mode_visibility(initial_service)

        ttk.Label(settings_frame, text="Custom Vocab").grid(row=5, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(settings_frame, textvariable=self.custom_vocab_var).grid(row=5, column=1, sticky="ew", padx=4, pady=4)

        ttk.Checkbutton(settings_frame, text="Debug logging", variable=self.debug_var, command=self._autosave).grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        ttk.Label(settings_frame, text="Sprache / Language").grid(row=7, column=0, sticky="w", padx=4, pady=4)
        languages = [
            ("auto", "🌐 Auto", None),
            ("en", "English", "us"),
            ("de", "Deutsch", "de"),
            ("fr", "Français", "fr"),
            ("es", "Español", "es"),
            ("it", "Italiano", "it"),
            ("pt", "Português", "pt"),
            ("nl", "Nederlands", "nl"),
        ]
        self._load_flag_images(languages)
        # Left-align menubutton label by overriding layout to stick west.
        self.style.layout(
            "Lang.TMenubutton",
            [
                (
                    "Menubutton.border",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Menubutton.padding",
                                {
                                    "sticky": "nswe",
                                    "children": [
                                        (
                                            "Menubutton.focus",
                                            {
                                                "sticky": "nswe",
                                                "children": [
                                                    ("Menubutton.label", {"sticky": "w"})
                                                ],
                                            },
                                        )
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        self.style.configure("Lang.TMenubutton", padding=(6, 2))
        self.language_button = ttk.Menubutton(settings_frame, text="", direction="below", style="Lang.TMenubutton")
        lang_menu = tk.Menu(self.language_button, tearoff=False)

        def _set_language(code: str, label: str, flag_code: str):
            self.language_var.set(code)
            if code == "auto":
                self.language_button.config(text="🌐 Auto", image="", compound=None)
            else:
                img = self.flag_images.get(flag_code)
                self.language_button.config(text=label, image=img, compound="left" if img else None)
            self._autosave()

        for code, label, flag_code in languages:
            img = self.flag_images.get(flag_code)
            lang_menu.add_radiobutton(
                label=label,
                image=img,
                compound="left" if img else None,
                value=code,
                variable=self.language_var,
                command=lambda c=code, l=label, f=flag_code: _set_language(c, l, f),
            )

        self.language_button["menu"] = lang_menu
        self.language_button.grid(row=7, column=1, sticky="ew", padx=4, pady=4)

        # Initialize displayed language
        try:
            initial = next(item for item in languages if item[0] == Config.LANGUAGE)
        except StopIteration:
            initial = languages[0]
        self.language_var.set(initial[0])
        if initial[0] == "auto":
            self.language_button.config(text="🌐 Auto", image="", compound=None)
        else:
            init_img = self.flag_images.get(initial[2])
            self.language_button.config(text=initial[1], image=init_img, compound="left" if init_img else None)

        ttk.Label(settings_frame, text="Microphone").grid(row=8, column=0, sticky="w", padx=4, pady=4)
        devices = self._get_microphones()
        max_label_len = max((len(d[1]) for d in devices), default=20)
        mic_width = max(120, max_label_len + 6)  # allow long labels to stay visible; expands further with window resize
        mic_combo = ttk.Combobox(settings_frame, values=[d[1] for d in devices], state="readonly", width=mic_width)
        mic_combo.grid(row=8, column=1, sticky="ew", padx=4, pady=4)
        try:
            mic_idx = [d[0] for d in devices].index(Config.MIC_DEVICE)
        except ValueError:
            mic_idx = 0
        mic_combo.current(mic_idx)
        mic_combo.bind("<<ComboboxSelected>>", lambda _: (self.mic_device_var.set(devices[mic_combo.current()][0]), self._autosave()))
        self.mic_device_var.set(devices[mic_idx][0])

        ttk.Checkbutton(settings_frame, text="Mic always on (faster start)", variable=self.mic_always_on_var, command=self._autosave).grid(row=9, column=0, columnspan=2, sticky="w", padx=4, pady=4)

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
            import re
            from difflib import SequenceMatcher
            devices = []
            default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
            devices.append(("default", "🎙️ System default"))
            seen = []  # preserve order with metadata: {"idx": int, "name": str, "norm": str, "is_default": bool}

            def _canonical(name: str) -> str:
                """Normalize device labels so host-specific prefixes/suffixes don't duplicate entries."""
                cleaned = name.lower()
                cleaned = re.sub(r"\(id\s*\d+\)", "", cleaned, flags=re.IGNORECASE)         # drop trailing id markers
                cleaned = re.sub(r"\(\s*\d+\s*[-–—]?\s*", "(", cleaned)                      # strip numeric prefixes inside parens e.g. "(4- insta360"
                cleaned = re.sub(r"^\s*\d+\s*[-: ]\s*", "", cleaned)                         # strip leading "4-" style host id
                cleaned = re.sub(r"\bwith hap\b", "", cleaned)                               # drop driver suffix noise
                cleaned = re.sub(r"\b2nd\b", "", cleaned)                                    # drop duplicated output marker
                cleaned = re.sub(r"[^a-z]+", " ", cleaned)                                   # collapse punctuation/duplicates
                cleaned = " ".join(cleaned.split())
                return cleaned.strip()

            def _find_duplicate(norm: str) -> int:
                for i, entry in enumerate(seen):
                    other = entry["norm"]
                    ratio = SequenceMatcher(None, norm, other).ratio()
                    if ratio >= 0.9 or norm.startswith(other) or other.startswith(norm):
                        return i
                return -1

            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                name = dev.get("name", f"Device {idx}")
                norm = _canonical(name)
                is_default = idx == default
                dup_idx = _find_duplicate(norm)
                if dup_idx == -1:
                    seen.append({"idx": idx, "name": name, "norm": norm, "is_default": is_default})
                else:
                    # Prefer the default device or, if neither default, keep the first occurrence.
                    if is_default and not seen[dup_idx]["is_default"]:
                        seen[dup_idx] = {"idx": idx, "name": name, "norm": norm, "is_default": is_default}

            for entry in seen:
                idx = entry["idx"]
                name = entry["name"]
                label = f"🎤 {name} (id {idx})"
                if entry["is_default"]:
                    label = f"{label} • default"
                devices.append((str(idx), label))
            return devices
        except Exception:
            return [("default", "🎙️ System default")]

    def _on_service_changed(self, service_name: str):
        self.service_var.set(service_name)
        self.api_key_var.set(Config.get_api_key(service_name))
        self._update_soniox_mode_visibility(service_name)

    def update_status(self, status: str):
        def _setter():
            self.status_var.set(status)
        self.root.after(0, _setter)

    def show_error(self, message: str):
        messagebox.showerror("Scriber", message)

    def run(self):
        self.root.mainloop()
