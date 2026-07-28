# Python Runtime A/B v1

This profile compares installed, frozen Scriber builds. It is deliberately
separate from the normal AutoResearch baseline and champion state.

`A13` is the CPython 3.13.14 comparison anchor. `O0` and `O1` use the exact same
official CPython 3.14.6 binary family with `PYTHON_JIT=0` and `PYTHON_JIT=1`.
`C0`/`C1` use one ClangCL + ThinLTO + upstream-PGO family, and `T0`/`T1` use
one otherwise-identical tail-call-interpreter family. `K0` is a one-time
ClangCL calibration build without PGO, tail calls, or JIT and can never ship.

Promotion is fail-closed. Evidence must bind the source commit, dependency and
runtime locks, host profile, complete desktop/backend/audio hashes, Python
identity, compiler, DLL, tail-call status, and both requested and observed JIT
state. The evaluator requires two FullLocal clean-install/reboot blocks on each
of the pinned Intel and AMD hosts. A release candidate must beat both `A13` and
`O0` in every block, with the configured practical threshold and a bootstrap
95% confidence interval wholly on the winning side. All latency, resource, and
error guardrails also apply.

If no optimized candidate passes, `O0` wins. Missing or malformed measurements
are never converted to zero and never count as evidence.
