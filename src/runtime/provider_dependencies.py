from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class ProviderRuntimeDependency:
    provider: str
    module: str
    package_hint: str
    reason: str


class ProviderRuntimeDependencyError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        module: str,
        package_hint: str,
        cause: BaseException,
    ) -> None:
        self.provider = provider
        self.module = module
        self.package_hint = package_hint
        self.cause = cause
        super().__init__(
            "Provider runtime dependency missing: "
            f"provider={provider} module={module} package={package_hint}. "
            f"Cause: {type(cause).__name__}: {cause}"
        )


STANDARD_PROVIDER_RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("pipecat.services.soniox.stt", "Soniox realtime STT provider"),
    ("src.assemblyai_async_stt", "AssemblyAI async STT adapter"),
    ("pipecat.services.assemblyai.stt", "AssemblyAI realtime STT provider"),
    ("src.mistral_stt", "Mistral realtime and async STT adapters"),
    ("src.smallest_stt", "Smallest AI realtime and async STT adapters"),
    (
        "src.cloud_async_stt",
        "Deepgram, Gladia, OpenAI, Speechmatics, and Gemini async STT adapters",
    ),
    ("src.azure_mai_stt", "Microsoft MAI Transcribe adapter"),
    ("pipecat.services.google.stt", "Google Cloud STT provider"),
    ("pipecat.services.elevenlabs.stt", "ElevenLabs STT provider"),
    ("pipecat.services.deepgram.stt", "Deepgram STT provider"),
    ("pipecat.services.openai.stt", "OpenAI realtime and batch STT provider"),
    ("pipecat.services.gladia.stt", "Gladia STT provider"),
    ("pipecat.services.groq.stt", "Groq STT provider"),
    ("pipecat.services.speechmatics.stt", "Speechmatics STT provider"),
)


_PROVIDER_DEPENDENCIES: dict[str, tuple[ProviderRuntimeDependency, ...]] = {
    "soniox": (
        ProviderRuntimeDependency(
            "soniox",
            "pipecat.services.soniox.stt",
            "websockets",
            "Soniox realtime WebSocket transcription",
        ),
    ),
    "smallest": (
        ProviderRuntimeDependency(
            "smallest",
            "src.smallest_stt",
            "websockets",
            "Smallest AI realtime WebSocket transcription",
        ),
    ),
    "assemblyai_realtime": (
        ProviderRuntimeDependency(
            "assemblyai_realtime",
            "pipecat.services.assemblyai.stt",
            "pipecat-ai[silero]==1.5.0",
            "AssemblyAI Universal-3.5 Pro realtime transcription",
        ),
    ),
    "elevenlabs": (
        ProviderRuntimeDependency(
            "elevenlabs",
            "pipecat.services.elevenlabs.stt",
            "websockets",
            "ElevenLabs realtime WebSocket transcription",
        ),
    ),
    "gladia": (
        ProviderRuntimeDependency(
            "gladia",
            "pipecat.services.gladia.stt",
            "websockets",
            "Gladia realtime WebSocket transcription",
        ),
    ),
    "deepgram": (
        ProviderRuntimeDependency(
            "deepgram",
            "pipecat.services.deepgram.stt",
            "deepgram-sdk",
            "Deepgram realtime transcription SDK",
        ),
    ),
    "deepgram_async": (
        ProviderRuntimeDependency(
            "deepgram_async",
            "src.cloud_async_stt",
            "requirements-base.txt",
            "Deepgram pre-recorded transcription adapter",
        ),
    ),
    "openai": (
        ProviderRuntimeDependency(
            "openai",
            "pipecat.services.openai.stt",
            "openai",
            "OpenAI realtime transcription SDK",
        ),
    ),
    "openai_async": (
        ProviderRuntimeDependency(
            "openai_async",
            "src.cloud_async_stt",
            "requirements-base.txt",
            "OpenAI audio transcription adapter",
        ),
    ),
    "gemini_stt": (
        ProviderRuntimeDependency(
            "gemini_stt",
            "src.cloud_async_stt",
            "requirements-base.txt",
            "Gemini API audio transcription adapter",
        ),
    ),
    "groq": (
        ProviderRuntimeDependency(
            "groq",
            "pipecat.services.groq.stt",
            "groq",
            "Groq Pipecat SDK path",
        ),
    ),
    "google": (
        ProviderRuntimeDependency(
            "google",
            "pipecat.services.google.stt",
            "google-cloud-speech",
            "Google Cloud Speech SDK",
        ),
    ),
    "speechmatics": (
        ProviderRuntimeDependency(
            "speechmatics",
            "pipecat.services.speechmatics.stt",
            "speechmatics-rt",
            "Speechmatics realtime SDK",
        ),
    ),
    "gladia_async": (
        ProviderRuntimeDependency(
            "gladia_async",
            "src.cloud_async_stt",
            "requirements-base.txt",
            "Gladia pre-recorded transcription adapter",
        ),
    ),
    "speechmatics_async": (
        ProviderRuntimeDependency(
            "speechmatics_async",
            "src.cloud_async_stt",
            "requirements-base.txt",
            "Speechmatics batch transcription adapter",
        ),
    ),
}


def _normalize_provider(provider: str) -> str:
    return (provider or "").strip().lower().replace("-", "_")


def _dependency_for(provider: str, module: str) -> ProviderRuntimeDependency:
    normalized = _normalize_provider(provider)
    dependencies = _PROVIDER_DEPENDENCIES.get(normalized, ())
    for dependency in dependencies:
        if dependency.module == module:
            return dependency
    if dependencies:
        return dependencies[0]
    return ProviderRuntimeDependency(
        normalized,
        module,
        "requirements-base.txt",
        "standard provider runtime",
    )


def _looks_like_missing_runtime(exc: BaseException) -> bool:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return True
    text = str(exc).lower()
    return "missing module:" in text or "pip install pipecat-ai[" in text


def import_provider_runtime_module(
    provider: str,
    module: str,
    *,
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> ModuleType:
    dependency = _dependency_for(provider, module)
    try:
        return import_module(module)
    except ProviderRuntimeDependencyError:
        raise
    except Exception as exc:
        if _looks_like_missing_runtime(exc):
            raise ProviderRuntimeDependencyError(
                provider=dependency.provider,
                module=dependency.module,
                package_hint=dependency.package_hint,
                cause=exc,
            ) from exc
        raise


def require_provider_runtime(
    provider: str,
    *,
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> None:
    normalized = _normalize_provider(provider)
    for dependency in _PROVIDER_DEPENDENCIES.get(normalized, ()):
        import_provider_runtime_module(
            dependency.provider,
            dependency.module,
            import_module=import_module,
        )
