from __future__ import annotations

from src.runtime.env_values import env_float, env_int


def test_env_int_falls_back_and_clamps(monkeypatch) -> None:
    monkeypatch.setenv("SCRIBER_TEST_INTEGER", "invalid")
    assert env_int("SCRIBER_TEST_INTEGER", 7, minimum=1, maximum=10) == 7

    monkeypatch.setenv("SCRIBER_TEST_INTEGER", "999")
    assert env_int("SCRIBER_TEST_INTEGER", 7, minimum=1, maximum=10) == 10


def test_env_float_rejects_non_finite_values(monkeypatch) -> None:
    monkeypatch.setenv("SCRIBER_TEST_FLOAT", "nan")
    assert env_float("SCRIBER_TEST_FLOAT", 2.5, minimum=0.1, maximum=10.0) == 2.5

    monkeypatch.setenv("SCRIBER_TEST_FLOAT", "-inf")
    assert env_float("SCRIBER_TEST_FLOAT", 2.5, minimum=0.1, maximum=10.0) == 2.5
