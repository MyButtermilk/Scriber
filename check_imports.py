try:
    from pipecat.services.soniox import SonioxSTTService
    print("SonioxSTTService found")
except ImportError as e:
    print(f"SonioxSTTService NOT found: {e}")

try:
    from pipecat.services.assemblyai import AssemblyAISTTService
    print("AssemblyAISTTService found")
except ImportError as e:
    print(f"AssemblyAISTTService NOT found: {e}")

try:
    from pipecat.services.google import GoogleSTTService
    print("GoogleSTTService found")
except ImportError as e:
    print(f"GoogleSTTService NOT found: {e}")

try:
    from pipecat.services.elevenlabs import ElevenLabsSTTService
    print("ElevenLabsSTTService found")
except ImportError as e:
    print(f"ElevenLabsSTTService NOT found: {e}")
