from __future__ import annotations

import json

from src.core.error_diagnostics import exception_diagnostics


def _exception_from_module(module: str, filename: str, *, depth: int = 0) -> Exception:
    namespace = {"__name__": module}
    code = compile(
        "def fail(depth):\n"
        "    private_transcript = 'PRIVATE_TRANSCRIPT_PROBE'\n"
        "    if depth:\n"
        "        return fail(depth - 1)\n"
        "    raise RuntimeError('PRIVATE_ERROR_MESSAGE_PROBE')\n",
        filename,
        "exec",
    )
    exec(code, namespace)
    try:
        namespace["fail"](depth)
    except Exception as exc:
        return exc
    raise AssertionError("fixture did not raise")


def test_exception_diagnostics_keep_code_locations_without_private_content():
    exc = _exception_from_module("src.pipeline", "C:/PRIVATE_USER/project/src/pipeline.py")

    details = exception_diagnostics(exc)

    assert details == {"error_type": "RuntimeError", "error_stack": ["src.pipeline:fail:5"]}
    serialized = json.dumps(details)
    assert "PRIVATE" not in serialized
    assert "/" not in serialized and "\\" not in serialized


def test_exception_diagnostics_bound_stack_and_causal_chain():
    exc = _exception_from_module(
        "pipecat.pipeline.worker", "/private/site-packages/pipecat/pipeline/worker.py", depth=20
    )
    cause = exc
    for _ in range(8):
        cause.__cause__ = ValueError("PRIVATE_CAUSE")
        cause = cause.__cause__
    cause.__cause__ = exc

    details = exception_diagnostics(exc)

    assert len(details["error_stack"]) == 8
    assert details["error_stack"][-1] == "pipecat.pipeline.worker:fail:5"
    assert details["error_causes"] == [{"error_type": "ValueError"}] * 3
    assert "PRIVATE" not in json.dumps(details)


def test_exception_diagnostics_honor_explicit_cause_and_context_suppression():
    explicit = RuntimeError("private")
    explicit.__cause__ = TimeoutError("private")
    explicit.__context__ = ValueError("private")
    suppressed = RuntimeError("private")
    suppressed.__context__ = ValueError("private")
    suppressed.__suppress_context__ = True
    implicit = RuntimeError("private")
    implicit.__context__ = TimeoutError("private")

    assert exception_diagnostics(explicit)["error_causes"] == [{"error_type": "TimeoutError"}]
    assert "error_causes" not in exception_diagnostics(suppressed)
    assert exception_diagnostics(implicit)["error_causes"] == [{"error_type": "TimeoutError"}]


def test_exception_diagnostics_omit_untrusted_module_or_mismatched_filename():
    for module, filename in (
        ("private_customer", "/private/private_customer.py"),
        ("src.private_customer", "/private/unrelated.py"),
        ("src.private-customer", "/private/src/private-customer.py"),
    ):
        exc = _exception_from_module(module, filename)
        assert exception_diagnostics(exc) == {"error_type": "RuntimeError"}


def test_exception_diagnostics_never_format_exception_or_frame_messages():
    class BrokenError(Exception):
        def __str__(self):
            raise AssertionError("exception message accessed")

    assert exception_diagnostics(BrokenError()) == {"error_type": "BrokenError"}
    assert exception_diagnostics("PRIVATE_PROVIDER_FRAME") == {"error_type": "ProviderErrorFrame"}
    private_type = type("PRIVATE TYPE WITH SPACES", (Exception,), {})
    assert exception_diagnostics(private_type()) == {"error_type": "UnknownError"}
