import os
import sys

# Ensure project root is on sys.path so `import src` works when running tests
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Make tests deterministic: avoid relying on the current foreground app/window title.
os.environ.setdefault("SCRIBER_INJECT_METHOD", "type")
