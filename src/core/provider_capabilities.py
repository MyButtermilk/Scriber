from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_live_streaming: bool
    supports_direct_file_upload: bool
    injects_immediately_in_live_mode: bool


_DEFAULT = ProviderCapabilities(
    supports_live_streaming=True,
    supports_direct_file_upload=False,
    injects_immediately_in_live_mode=False,
)

_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "soniox": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=True,  # when configured realtime
    ),
    "soniox_async": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
    ),
    "mistral": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=True,
    ),
    "mistral_async": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
    ),
    "assemblyai": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=True,
        injects_immediately_in_live_mode=False,
    ),
    "onnx_local": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
    "nemo_local": ProviderCapabilities(
        supports_live_streaming=True,
        supports_direct_file_upload=False,
        injects_immediately_in_live_mode=False,
    ),
}


def get_capabilities(provider: str) -> ProviderCapabilities:
    key = (provider or "").strip().lower()
    return _CAPABILITIES.get(key, _DEFAULT)


def supports_direct_file_upload(provider: str) -> bool:
    return get_capabilities(provider).supports_direct_file_upload


def injects_immediately_in_live_mode(provider: str) -> bool:
    return get_capabilities(provider).injects_immediately_in_live_mode

