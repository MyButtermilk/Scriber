import os
import sys

# Ensure project root is on sys.path so `import src` works when running tests
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Make tests deterministic: avoid relying on the current foreground app/window title.
os.environ.setdefault("SCRIBER_INJECT_METHOD", "type")
os.environ.setdefault("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
os.environ["SCRIBER_MIC_ALWAYS_ON"] = "0"

# Never let the automated suite inherit real provider credentials from a
# developer shell or local .env file. Tests that exercise credential handling
# override these deterministic sentinels explicitly.
for provider_key in (
    "SONIOX_API_KEY",
    "MISTRAL_API_KEY",
    "SMALLEST_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "GOOGLE_API_KEY",
    "YOUTUBE_API_KEY",
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "MODEL_API_KEY",
    "CEREBRAS_API_KEY",
    "CELERIS_API_KEY",
    "AZURE_MAI_SPEECH_KEY",
    "GLADIA_API_KEY",
    "GROQ_API_KEY",
    "SPEECHMATICS_API_KEY",
    "MODULATE_API_KEY",
):
    os.environ[provider_key] = "scriber-test-placeholder-never-send"
