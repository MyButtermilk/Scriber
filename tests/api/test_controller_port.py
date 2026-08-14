"""Guard the route ports against controller drift.

The ports in src.api.controller_port are structural, so nothing forces
ScriberWebController to keep matching them. mypy does not catch it either:
web_api is outside the typechecked tranche, so the register_*_routes call
sites are unchecked. Renaming a controller method would leave the ports
compiling happily and fail at runtime on the first request.

These tests compare the two directly.
"""

from __future__ import annotations

import inspect
from typing import Protocol, get_type_hints

import pytest

from src.api import controller_port
from src.web_api import ScriberWebController, TranscriptRecord

PORTS = [
    controller_port.BroadcastPort,
    controller_port.RuntimeControllerPort,
    controller_port.OnnxControllerPort,
    controller_port.YoutubeControllerPort,
]


def _declared_methods(port: type) -> dict[str, object]:
    """Every method a Protocol requires, including inherited ones."""
    members: dict[str, object] = {}
    for klass in reversed(port.__mro__):
        if klass in (object, Protocol) or not getattr(klass, "_is_protocol", False):
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_") and callable(value):
                members[name] = value
    return members


@pytest.mark.parametrize("port", PORTS, ids=lambda p: p.__name__)
def test_controller_implements_every_port_member(port):
    missing = [name for name in _declared_methods(port) if not hasattr(ScriberWebController, name)]
    assert not missing, f"{port.__name__} requires methods ScriberWebController does not have: {missing}"


@pytest.mark.parametrize("port", PORTS, ids=lambda p: p.__name__)
def test_port_signatures_match_the_controller(port):
    for name, declared in _declared_methods(port).items():
        actual = getattr(ScriberWebController, name)

        assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(declared), (
            f"{name}: async-ness differs between {port.__name__} and ScriberWebController"
        )

        expected_params = list(inspect.signature(declared).parameters.values())[1:]
        actual_params = list(inspect.signature(actual).parameters.values())[1:]
        assert [(p.name, p.kind) for p in expected_params] == [(p.name, p.kind) for p in actual_params], (
            f"{name}: parameter names/kinds differ between {port.__name__} and ScriberWebController"
        )
        for expected, real in zip(expected_params, actual_params, strict=True):
            if expected.default is not inspect.Parameter.empty:
                assert real.default == expected.default, f"{name}: default for '{expected.name}' differs"


def test_transcript_record_satisfies_the_public_record_port():
    """start_youtube_transcription's return value is typed through this port."""
    declared = inspect.signature(controller_port.PublicRecordPort.to_public)
    actual = inspect.signature(TranscriptRecord.to_public)
    assert list(declared.parameters) == list(actual.parameters)
    assert get_type_hints(TranscriptRecord.to_public)["include_content"] is bool


def test_ports_stay_narrow():
    """A port that mirrors the whole controller has stopped being a contract."""
    runtime_members = _declared_methods(controller_port.RuntimeControllerPort)
    controller_methods = {
        name for name, value in vars(ScriberWebController).items() if not name.startswith("_") and callable(value)
    }
    assert len(runtime_members) < len(controller_methods) / 2, (
        "RuntimeControllerPort now covers more than half the controller's public surface; split it before adding more."
    )
