"""Shared assertions for domain-local route contracts.

The individual route test modules name their own port and production adapter.
This file contains only the comparison mechanism; it deliberately has no
catalogue of route domains or controller ports.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Protocol, get_type_hints

import pytest


def _declared_protocol_members(port: type) -> tuple[dict[str, object], set[str], set[str]]:
    methods: dict[str, object] = {}
    properties: set[str] = set()
    fields: set[str] = set()
    for klass in reversed(port.__mro__):
        if klass in (object, Protocol) or not getattr(klass, "_is_protocol", False):
            continue
        fields.update(name for name in getattr(klass, "__annotations__", {}) if not name.startswith("_"))
        for name, value in vars(klass).items():
            if name.startswith("_"):
                continue
            if isinstance(value, property):
                properties.add(name)
            elif callable(value):
                methods[name] = value
    return methods, properties, fields


def _parameters(callable_object: object) -> list[inspect.Parameter]:
    parameters = list(inspect.signature(callable_object).parameters.values())
    if parameters and parameters[0].name in {"self", "cls"}:
        return parameters[1:]
    return parameters


def _assert_protocol_contract(
    port: type,
    adapter: type,
    *,
    methods: AbstractSet[str],
    properties: AbstractSet[str] = frozenset(),
    returns: Mapping[str, object] | None = None,
) -> None:
    declared_methods, declared_properties, declared_fields = _declared_protocol_members(port)
    assert set(declared_methods) == set(methods), f"{port.__name__} method surface changed"
    assert declared_properties == set(properties), f"{port.__name__} property surface changed"
    assert not declared_fields, f"{port.__name__} exposes writable fields; use methods or read-only properties"

    expected_returns = dict(returns or {})
    assert expected_returns.keys() <= declared_methods.keys(), (
        f"{port.__name__} return contract names an unknown method"
    )

    for name, declared in declared_methods.items():
        actual = getattr(adapter, name, None)
        assert callable(actual), f"{port.__name__} requires {adapter.__name__}.{name}"
        assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(declared), (
            f"{name}: async-ness differs between {port.__name__} and {adapter.__name__}"
        )

        expected_parameters = _parameters(declared)
        actual_parameters = _parameters(actual)
        expected_shape = [(item.name, item.kind, item.default) for item in expected_parameters]
        actual_shape = [(item.name, item.kind, item.default) for item in actual_parameters]
        assert actual_shape == expected_shape, (
            f"{name}: parameters or defaults differ between {port.__name__} and {adapter.__name__}"
        )
        if name in expected_returns:
            expected_return = expected_returns[name]
            declared_return = get_type_hints(declared).get("return", inspect.Signature.empty)
            actual_return = get_type_hints(actual).get("return", inspect.Signature.empty)
            assert declared_return == expected_return, f"{port.__name__}.{name} return type changed"
            assert actual_return == expected_return, f"{adapter.__name__}.{name} return type changed"

    for name in declared_properties:
        actual = inspect.getattr_static(adapter, name, None)
        assert isinstance(actual, property), f"{port.__name__} requires property {adapter.__name__}.{name}"


class ProtocolContractAssertion(Protocol):
    def __call__(
        self,
        port: type,
        adapter: type,
        *,
        methods: AbstractSet[str],
        properties: AbstractSet[str] = frozenset(),
        returns: Mapping[str, object] | None = None,
    ) -> None: ...


@pytest.fixture
def assert_protocol_contract() -> ProtocolContractAssertion:
    return _assert_protocol_contract
