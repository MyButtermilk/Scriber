from __future__ import annotations

from pathlib import Path

from scripts.check_backend_runtime_imports import REQUIRED_IMPORTS, check_imports


def test_backend_runtime_import_check_covers_scipy_startup_dependency():
    required_modules = {module for module, _reason in REQUIRED_IMPORTS}

    assert "scipy" in required_modules
    assert "scipy.signal" in required_modules
    assert "pyloudnorm" in required_modules
    assert "src.web_api" in required_modules


def test_standard_requirements_include_scipy():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements-base.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "scipy" in requirements


def test_backend_runtime_import_check_reports_missing_modules():
    def fake_import(module_name: str) -> object:
        if module_name == "missing_module":
            raise ModuleNotFoundError("No module named 'missing_module'")
        return object()

    missing = check_imports(
        [
            ("present_module", "test present"),
            ("missing_module", "test missing"),
        ],
        import_module=fake_import,
    )

    assert missing == [
        {
            "module": "missing_module",
            "reason": "test missing",
            "error": "ModuleNotFoundError: No module named 'missing_module'",
        }
    ]
