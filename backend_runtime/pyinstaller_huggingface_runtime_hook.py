"""Force the frozen Hub client onto its supported HTTP download path."""

from __future__ import annotations

import os


# The frozen runtime intentionally omits hf_xet.  Setting this before any Hub
# import also guards against package-metadata-only detection in frozen builds.
os.environ["HF_HUB_DISABLE_XET"] = "1"
