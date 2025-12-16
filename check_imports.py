try:
    from pipecat.services.soniox.stt import SonioxSTTService
    print("SonioxSTTService found")
except ImportError as e:
    print(f"SonioxSTTService NOT found: {e}")

try:
    from pipecat.services.assemblyai.stt import AssemblyAISTTService
    print("AssemblyAISTTService found")
except ImportError as e:
    print(f"AssemblyAISTTService NOT found: {e}")

try:
    from pipecat.services.google.stt import GoogleSTTService
    print("GoogleSTTService found")
except ImportError as e:
    print(f"GoogleSTTService NOT found: {e}")

try:
    from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
    print("ElevenLabsSTTService found")
except ImportError as e:
    print(f"ElevenLabsSTTService NOT found: {e}")

try:
    from pipecat.services.deepgram.stt import DeepgramSTTService
    print("DeepgramSTTService found")
except ImportError as e:
    print(f"DeepgramSTTService NOT found: {e}")

try:
    from pipecat.services.openai.stt import OpenAISTTService
    print("OpenAISTTService found")
except ImportError as e:
    print(f"OpenAISTTService NOT found: {e}")

try:
    from pipecat.services.azure.stt import AzureSTTService
    print("AzureSTTService found")
except ImportError as e:
    print(f"AzureSTTService NOT found: {e}")

try:
    from pipecat.services.gladia.stt import GladiaSTTService
    print("GladiaSTTService found")
except ImportError as e:
    print(f"GladiaSTTService NOT found: {e}")

try:
    from pipecat.services.groq.stt import GroqSTTService
    print("GroqSTTService found")
except ImportError as e:
    print(f"GroqSTTService NOT found: {e}")

try:
    from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
    print("SpeechmaticsSTTService found")
except ImportError as e:
    print(f"SpeechmaticsSTTService NOT found: {e}")

try:
    from pipecat.services.aws.stt import AWSTranscribeSTTService
    print("AWSTranscribeSTTService found")
except ImportError as e:
    print(f"AWSTranscribeSTTService NOT found: {e}")
