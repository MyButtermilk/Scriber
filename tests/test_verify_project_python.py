from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import verify_project_python


@pytest.mark.parametrize(
    ("version", "cache_tag", "installed_pipecat", "expected"),
    [
        ((3, 14, 9), "cpython-314", "9.8.7", 0),
        ((3, 14, 6), "cpython-314", "9.8.7", 5),
        ((3, 14, 9), "cpython-313", "9.8.7", 5),
        ((3, 14, 9), "cpython-314", "9.8.6", 4),
        ((3, 14, 9), "cpython-314", None, 4),
    ],
)
def test_project_python_verifier_checks_runtime_and_locked_dependency(
    monkeypatch, tmp_path, capsys, version, cache_tag, installed_pipecat, expected
) -> None:
    (tmp_path / "requirements-base.txt").write_text("pipecat-ai[openai]==9.8.7\n", encoding="utf-8")
    monkeypatch.setattr(verify_project_python, "__file__", str(tmp_path / "scripts" / "verify_project_python.py"))
    monkeypatch.setattr(
        verify_project_python,
        "sys",
        SimpleNamespace(
            version_info=version,
            version=".".join(map(str, version)),
            implementation=SimpleNamespace(cache_tag=cache_tag),
            stderr=verify_project_python.sys.stderr,
        ),
    )

    def installed_version(name: str) -> str:
        assert name == "pipecat-ai"
        assert expected != 5, "Reject an incompatible interpreter before reading installed dependencies"
        if installed_pipecat is None:
            raise verify_project_python.importlib.metadata.PackageNotFoundError(name)
        return installed_pipecat

    monkeypatch.setattr(verify_project_python.importlib.metadata, "version", installed_version)

    assert verify_project_python.main() == expected
    error = capsys.readouterr().err
    if expected == 0:
        assert error == ""
    elif expected == 5:
        assert "CPython 3.14.9 (cpython-314) is required" in error
    else:
        assert "pipecat-ai 9.8.7 is required" in error
