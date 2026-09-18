from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "scriber-backend.spec"


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"))


def _data_filter():
    function = next(
        node
        for node in _spec_tree().body
        if isinstance(node, ast.FunctionDef) and node.name == "exclude_development_datas"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SPEC), "exec"), namespace)
    return namespace[function.name]


@pytest.mark.parametrize("separator", ("/", "\\"))
def test_post_analysis_filter_removes_only_upstream_development_data(separator) -> None:
    removed = [
        "nltk/test/wordnet.doctest",
        "nltk/test/nested/grammar.doctest",
        "pipecat/cli/templates/client/react-vite/package.json.jinja2",
        "pipecat/cli/templates/server/Dockerfile.jinja2",
    ]
    retained = [
        "nltk_data/tokenizers/punkt_tab/english/abbrev_types.txt",
        "nltk_data/tokenizers/punkt_tab/german/ortho_context.tab",
        "nltk_data/tokenizers/punkt_tab/README",
        "pipecat/audio/vad/data/silero_vad.onnx",
        "pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx",
        "nltk-3.9.4.dist-info/LICENSE.txt",
        "pipecat_ai-1.5.0.dist-info/licenses/LICENSE",
        "nltk/testing-runtime/file.dat",
        "pipecat/client/runtime.dat",
        "onnx_asr/preprocessors/nemo.onnx",
    ]
    entries = [(path.replace("/", separator), "source", "DATA") for path in removed + retained]
    result = _data_filter()(entries)
    assert result == entries[len(removed) :]
    assert len(entries) == len(removed) + len(retained)


def test_development_data_filter_runs_after_hooks_and_before_runtime_punkt_validation() -> None:
    calls = [node for node in _spec_tree().body if isinstance(node, (ast.Assign, ast.Expr))]
    analysis_index = next(
        index
        for index, node in enumerate(calls)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "Analysis"
    )
    filter_index = next(
        index
        for index, node in enumerate(calls)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "exclude_development_datas"
    )
    assert analysis_index < filter_index
    assert ast.unparse(calls[filter_index].targets[0]) == "a.datas[:]"


def test_analysis_excludes_optional_typechecker_plugins_before_their_native_dependency_graph() -> None:
    analysis = next(
        node
        for node in ast.walk(_spec_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Analysis"
    )
    exclusions = next(keyword.value for keyword in analysis.keywords if keyword.arg == "excludes")
    names = {item.value for item in exclusions.elts if isinstance(item, ast.Constant)}
    assert {"mypy", "mypyc", "pydantic.mypy", "pydantic.v1.mypy"} <= names
    assert names.isdisjoint({"pydantic", "pydantic_core", "typing_extensions", "mypy_extensions"})


def test_runtime_providers_work_with_optional_typechecker_imports_unavailable() -> None:
    probe = """
import importlib.abc
import json
import sys

blocked = ('mypy', 'mypyc', 'pydantic.mypy', 'pydantic.v1.mypy')
attempts = []
class NoTypechecking(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + '.') for root in blocked):
            attempts.append(fullname)
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoTypechecking())
from scripts.check_backend_runtime_imports import check_imports, check_provider_initialization_matrix
failures = check_imports() + check_provider_initialization_matrix()
assert not failures, failures
assert not attempts, attempts
print(json.dumps({'failures': failures, 'blockedImportsAttempted': attempts}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, text=True, capture_output=True, timeout=90, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"failures": [], "blockedImportsAttempted": []}
