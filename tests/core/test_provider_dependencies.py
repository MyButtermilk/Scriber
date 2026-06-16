import pytest

from src.core.error_taxonomy import ErrorCategory
from src.core.provider_errors import provider_user_error
from src.runtime.provider_dependencies import (
    ProviderRuntimeDependencyError,
    import_provider_runtime_module,
)


def test_import_provider_runtime_module_wraps_missing_dependency():
    def fake_import(_module: str):
        raise ModuleNotFoundError("No module named 'deepgram'")

    with pytest.raises(ProviderRuntimeDependencyError) as exc_info:
        import_provider_runtime_module(
            "deepgram",
            "pipecat.services.deepgram.stt",
            import_module=fake_import,
        )

    err = exc_info.value
    assert err.provider == "deepgram"
    assert err.module == "pipecat.services.deepgram.stt"
    assert err.package_hint == "deepgram-sdk"


def test_provider_user_error_maps_missing_runtime_dependency_to_toast_message():
    err = ProviderRuntimeDependencyError(
        provider="deepgram",
        module="pipecat.services.deepgram.stt",
        package_hint="deepgram-sdk",
        cause=ModuleNotFoundError("No module named 'deepgram'"),
    )

    info = provider_user_error(None, err)

    assert info.provider == "deepgram"
    assert info.category is ErrorCategory.CONFIG_INVALID
    assert info.code == "missing_provider_runtime"
    assert "Deepgram runtime is missing" in info.message
    assert info.retryable is False
