import sys
import time
import threading
import tkinter as tk
from tkinter import scrolledtext
from pathlib import Path

# Config
LOG_FILE = Path(__file__).parent.parent / "latest.log"
REFRESH_RATE_MS = 1000

class LogViewer:
    def __init__(self, root):
        self.root = root
        self.root.title("Scriber Logs")
        self.root.geometry("1000x600")
        self.root.configure(bg="#1a1a2e")
        
        # State
        self.paused = False
        self.auto_scroll = True
        self.last_pos = 0
        self.filter_level = "ALL"
        self.search_term = ""
        
        self.setup_ui()
        self.start_watching()

    def setup_ui(self):
        # Toolbar
        toolbar = tk.Frame(self.root, bg="#2d2d44", pady=5)
        toolbar.pack(fill=tk.X)
        
        # Filters
        self.filter_buttons = {}
        for lvl, col in [("ALL","#6366f1"), ("ERROR","#ef4444"), ("WARNING","#f59e0b"), ("INFO","#22c55e")]:
            btn = tk.Button(toolbar, text=lvl, command=lambda l=lvl: self.set_filter(l),
                          bg="#2d2d44", fg="white", activebackground=col, relief=tk.RAISED)
            btn.pack(side=tk.LEFT, padx=2)
            self.filter_buttons[lvl] = btn
        self.filter_buttons["ALL"].config(relief=tk.SUNKEN, bg="#3B82F6")
        
        tk.Frame(toolbar, bg="#2d2d44", width=20).pack(side=tk.LEFT)
        
        # Controls
        self.btn_scroll = tk.Button(toolbar, text="⬇ Auto", command=self.toggle_scroll, bg="#22c55e", fg="white")
        self.btn_scroll.pack(side=tk.LEFT, padx=2)
        
        tk.Button(toolbar, text="📋 Copy All", command=self.copy_all, bg="#3B82F6", fg="white").pack(side=tk.LEFT, padx=2)
        
        # Search
        tk.Label(toolbar, text="Find:", bg="#2d2d44", fg="#aaa").pack(side=tk.LEFT, padx=(10,2))
        self.entry_search = tk.Entry(toolbar, bg="#1a1a2e", fg="white", insertbackground="white")
        self.entry_search.pack(side=tk.LEFT, padx=2)
        self.entry_search.bind("<Return>", self.do_search)
        self.entry_search.bind("<KeyRelease>", self.do_search)
        
        # Text Area
        text_frame = tk.Frame(self.root, bg="#1a1a2e")
        text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.text = scrolledtext.ScrolledText(text_frame, bg="#0d1117", fg="#c9d1d9", font=("Consolas", 10))
        self.text.pack(fill=tk.BOTH, expand=True)
        
        # Tags
        self.text.tag_config("ERROR", foreground="#f87171", font=("Consolas", 10, "bold"))
        self.text.tag_config("WARNING", foreground="#fbbf24")
        self.text.tag_config("INFO", foreground="#4ade80")
        self.text.tag_config("DEBUG", foreground="#8b949e")
        self.text.tag_config("SEARCH", background="#854d0e", foreground="#fef3c7")
        self.text.tag_config("HIDDEN", elide=True)
        
        # Status Bar
        self.status_bar = tk.Frame(self.root, bg="#2d2d44", height=25)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_stats = tk.Label(self.status_bar, text="Ready", bg="#2d2d44", fg="#aaa")
        self.lbl_stats.pack(side=tk.LEFT, padx=5)

    def start_watching(self):
        self.root.after(100, self.refresh_file)

    def refresh_file(self):
        if not LOG_FILE.exists():
            self.lbl_stats.config(text="Waiting for log file...")
            self.root.after(1000, self.refresh_file)
            return

        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, 2)
                current_size = f.tell()
                
                if current_size < self.last_pos:
                    # File was truncated/rotated
                    self.last_pos = 0
                    self.text.delete("1.0", tk.END)
                
                if current_size > self.last_pos:
                    f.seek(self.last_pos)
                    new_data = f.read()
                    self.last_pos = current_size
                    
                    self.append_logs(new_data)
        except Exception as e:
            print(f"Error reading log: {e}")

        self.root.after(REFRESH_RATE_MS, self.refresh_file)

    def get_level(self, line):
        if "| ERROR" in line or "[ERROR]" in line: return "ERROR"
        if "| WARNING" in line or "[WARNING]" in line: return "WARNING"
        if "| INFO" in line or "[INFO]" in line: return "INFO"
        if "| DEBUG" in line or "[DEBUG]" in line: return "DEBUG"
        return "INFO"

    def append_logs(self, data):
        self.text.config(state=tk.NORMAL)
        
        for line in data.splitlines():
            line += "\n"
            start = self.text.index(tk.END)
            self.text.insert(tk.END, line)
            
            # Apply color tag
            lvl = self.get_level(line)
            self.text.tag_add(lvl, f"{float(self.text.index(tk.END))-1.0} linestart", tk.END)
            
            # Filter check
            if self.filter_level != "ALL" and lvl != self.filter_level:
                self.text.tag_add("HIDDEN", f"{float(self.text.index(tk.END))-1.0} linestart", tk.END)
            
            # Search check
            if self.search_term and self.search_term not in line.lower():
                self.text.tag_add("HIDDEN", f"{float(self.text.index(tk.END))-1.0} linestart", tk.END)

        self.text.config(state=tk.DISABLED)
        
        if self.auto_scroll:
            self.text.see(tk.END)
            
    def set_filter(self, lvl):
        self.filter_level = lvl
        for l, b in self.filter_buttons.items():
            b.config(relief=tk.SUNKEN if l == lvl else tk.RAISED, bg="#3B82F6" if l == lvl else "#2d2d44")
        self.reapply_filters()

    def do_search(self, event=None):
        self.search_term = self.entry_search.get().lower()
        self.reapply_filters()

    def reapply_filters(self):
        self.text.tag_remove("HIDDEN", "1.0", tk.END)
        self.text.tag_remove("SEARCH", "1.0", tk.END)
        
        # This is a bit heavy for huge logs, but fine for typical usage
        # We iterate through all lines to set HIDDEN tag
        # A more optimized way would be to only hide/unhide, but tkinter text widget doesn't store data separately
        
        # Simple re-parse of visible text is hard without raw data structure.
        # For now, we rely on the tag_add "HIDDEN" logic purely visual.
        # Actually proper re-filtering requires clearing and re-reading or iterating all lines.
        # Let's iterate lines.
        
        num_lines = int(float(self.text.index(tk.END)))
        for i in range(1, num_lines):
            line_text = self.text.get(f"{i}.0", f"{i}.end")
            lvl = self.get_level(line_text)
            
            hide = False
            if self.filter_level != "ALL" and lvl != self.filter_level:
                hide = True
            if self.search_term and self.search_term not in line_text.lower():
                hide = True
            
            if hide:
                self.text.tag_add("HIDDEN", f"{i}.0", f"{i+1}.0")
            
            # Search highlighting
            if self.search_term and self.search_term in line_text.lower():
                 start_idx = line_text.lower().find(self.search_term)
                 while start_idx >= 0:
                     self.text.tag_add("SEARCH", f"{i}.{start_idx}", f"{i}.{start_idx+len(self.search_term)}")
                     start_idx = line_text.lower().find(self.search_term, start_idx + 1)

    def toggle_scroll(self):
        self.auto_scroll = not self.auto_scroll
        self.btn_scroll.config(text="⬇ Auto" if self.auto_scroll else "⏸ Pause", bg="#22c55e" if self.auto_scroll else "#ef4444")

    def copy_all(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.text.get("1.0", tk.END))

if __name__ == "__main__":
    # Enable High DPI support on Windows
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    app = LogViewer(root)
    root.mainloop()
