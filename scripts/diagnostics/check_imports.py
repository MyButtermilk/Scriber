from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

IMPORT_PROBES = (
    ("pipecat.services.soniox.stt", "SonioxSTTService"),
    ("pipecat.services.assemblyai.stt", "AssemblyAISTTService"),
    ("pipecat.services.google.stt", "GoogleSTTService"),
    ("pipecat.services.elevenlabs.stt", "ElevenLabsSTTService"),
    ("pipecat.services.deepgram.stt", "DeepgramSTTService"),
    ("pipecat.services.openai.stt", "OpenAISTTService"),
    ("src.azure_mai_stt", "AzureMaiTranscribeSTTService"),
    ("pipecat.services.gladia.stt", "GladiaSTTService"),
    ("pipecat.services.groq.stt", "GroqSTTService"),
    ("pipecat.services.speechmatics.stt", "SpeechmaticsSTTService"),
    ("pipecat.services.aws.stt", "AWSTranscribeSTTService"),
)

for module_name, symbol_name in IMPORT_PROBES:
    try:
        module = importlib.import_module(module_name)
        getattr(module, symbol_name)
    except (AttributeError, ImportError) as error:
        print(f"{symbol_name} NOT found: {error}")
    else:
        print(f"{symbol_name} found")
