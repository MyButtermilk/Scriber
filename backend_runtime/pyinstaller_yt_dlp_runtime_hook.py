"""Early yt-dlp boundary for the frozen Scriber backend."""

from __future__ import annotations

import os
import sys


# This environment boundary is cheap and applies to every frozen invocation.
# Importing yt-dlp itself is intentionally deferred during normal startup.
os.environ["YTDLP_NO_PLUGINS"] = "1"

if "--installer-youtube-holdout-probe" in sys.argv:
    from backend_runtime.yt_dlp_policy import apply_youtube_only_runtime_policy

    apply_youtube_only_runtime_policy()
