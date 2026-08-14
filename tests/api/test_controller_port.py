"""Guard the domain-local route ports against controller drift.

The ports are structural, so nothing forces ScriberWebController to keep
matching them. mypy does not catch it either:
web_api is outside the typechecked tranche, so the register_*_routes call
sites are unchecked. Renaming a controller method would leave the ports
compiling happily and fail at runtime on the first request.

These tests compare the two directly.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Protocol, get_type_hints

import pytest

from src.api.device_routes import DeviceControllerPort
from src.api.local_polishing_routes import LocalPolishingControllerPort
from src.api.onnx_routes import OnnxControllerPort
from src.api.outlook_calendar_routes import OutlookCalendarPort
from src.api.runtime_routes import RuntimeControllerPort
from src.api.settings_routes import SettingsControllerPort
from src.api.transcript_routes import TranscriptsControllerPort, TranscriptViewPort
from src.api.youtube_routes import PublicRecordPort, YoutubeControllerPort
from src.outlook_calendar import OutlookCalendarService
from src.web_api import ScriberWebController, TranscriptRecord, TranscriptView

PORTS = [
    RuntimeControllerPort,
    OnnxControllerPort,
    YoutubeControllerPort,
    TranscriptsControllerPort,
    SettingsControllerPort,
    LocalPolishingControllerPort,
    DeviceControllerPort,
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
    declared = inspect.signature(PublicRecordPort.to_public)
    actual = inspect.signature(TranscriptRecord.to_public)
    assert list(declared.parameters) == list(actual.parameters)
    assert get_type_hints(TranscriptRecord.to_public)["include_content"] is bool


def test_transcript_view_satisfies_the_view_port():
    """transcript_view's return value is typed through this port."""
    declared = {name for name in get_type_hints(TranscriptViewPort)}
    actual = {field.name for field in dataclasses.fields(TranscriptView)}
    assert declared <= actual, f"TranscriptViewPort names fields TranscriptView lacks: {declared - actual}"


def test_outlook_calendar_port_matches_its_collaborator():
    """This port names the calendar, not the controller, so it is checked apart."""
    for name, declared in _declared_methods(OutlookCalendarPort).items():
        actual = getattr(OutlookCalendarService, name, None)
        assert actual is not None, f"OutlookCalendarPort requires a missing {name}"
        assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(declared), (
            f"{name}: async-ness differs between OutlookCalendarPort and OutlookCalendarService"
        )
        expected_params = list(inspect.signature(declared).parameters.values())[1:]
        actual_params = list(inspect.signature(actual).parameters.values())[1:]
        assert [(p.name, p.kind) for p in expected_params] == [(p.name, p.kind) for p in actual_params], (
            f"{name}: parameter names/kinds differ from OutlookCalendarService"
        )


def test_outlook_calendar_port_declares_the_pending_property():
    """A property is not collected as a method, so it is asserted directly."""
    assert isinstance(inspect.getattr_static(OutlookCalendarService, "authorization_pending"), property), (
        "authorization_pending is no longer a property on OutlookCalendarService"
    )


def test_ports_stay_narrow():
    """A port that mirrors the whole controller has stopped being a contract."""
    runtime_members = _declared_methods(RuntimeControllerPort)
    controller_methods = {
        name for name, value in vars(ScriberWebController).items() if not name.startswith("_") and callable(value)
    }
    assert len(runtime_members) < len(controller_methods) / 2, (
        "RuntimeControllerPort now covers more than half the controller's public surface; split it before adding more."
    )
