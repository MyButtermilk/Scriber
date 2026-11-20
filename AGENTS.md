# Scriber Agent Guide

Scriber is a Windows-first transcription app with a Tauri shell, React frontend,
Python backend and native audio workers. Start with the component you are
changing. Read its editing contracts and run the smallest check that exercises
the changed behavior.

## Working in this repository

- Use `scripts\project-python.cmd` for local Python. It selects the project's
  virtual environment and validates the Pipecat pin. For setup, see the
  [test handbook](docs/TESTING_AND_RELEASE.md#core-test-commands).
- For Windows Rust builds use the configured MSVC toolchain. A missing SDK or
  linker, a denied executable launch, or a Falcon quarantine triggers the
  [endpoint security procedure](#endpoint-security-safe-automation).
- Before local-polishing training, corpus work, evaluation or export anywhere
  in this repo, including `ml/`, read the [PRAXIST input and release boundaries](praxist_task/AGENTS.md).
  For product inference, preserve the raw dictation on failure and keep local
  polishing offline.
- Prefer the owning implementation, tests and CI workflow when older prose
  disagrees. Correct the relevant contract with the change. Add regression
  coverage for changes to lifecycle, provider behavior, packaging or a
  user-visible workflow. Keep temporary investigation output in `tmp/`.
- Keep shared working rules in this file, subsystem contracts in the linked
  guides, and dated measurements in the performance or testing handbook.
- For logs, support bundles, exports and IPC, follow the redaction rules for
  that channel. Keep cookies, session tokens, raw device IDs and voice
  embeddings out of diagnostics. Resolve shell file actions from opaque
  export tokens. [Data and diagnostics](docs/contracts/data-and-diagnostics.md)

## Find the relevant contract

Paths written in code spans in the contract guides are relative to the repository root.

| When changing | Read first | Boundary to preserve |
| --- | --- | --- |
| Backend routes, React navigation, shell IPC or installer ownership | [Repository map](docs/contracts/repository-map.md) and [runtime architecture](docs/ARCHITECTURE.md) | Follow the owning module across process boundaries. |
| Tauri lifecycle, REST, WebSocket, authentication or events | [Desktop and API](docs/contracts/desktop-and-api.md) | Keep privileged operations in the shell and preserve existing clients' authenticated contracts. |
| Outlook meeting identity, participants or delivery | [Outlook and identity](docs/contracts/desktop-and-api.md#outlook-calendar-and-participant-identity) | Resolve recipients from the frozen appointment. [Export tests](tests/test_meeting_export.py) |
| Live mic, prewarm, device changes or audio admission | [Microphone and device handling](docs/contracts/microphone.md) | Verify installed Rust/WASAPI capture, prewarm adoption and provider finalization on the path changed. |
| STT providers, file/YouTube ingest, retries or long audio | [Providers and media](docs/contracts/providers-and-media.md) | Respect the frozen request and paid-call boundary on resume. [Request-shape tests](tests/test_long_file_provider_contracts.py) |
| YouTube sign-in, cookies or extension permissions | [YouTube session handling](docs/contracts/providers-and-media.md#youtube-session-handling) and [extension privacy](docs/CHROME_EXTENSION_PRIVACY.md) | Keep optional session transfer separate from the ordinary video link. |
| Transcript artifacts, jobs, durable state, diagnostics or export | [Data and diagnostics](docs/contracts/data-and-diagnostics.md) | Preserve recovery, user data and redaction on failure. |
| Performance, packaging, cache or installer changes | [Performance contracts](docs/contracts/performance.md) and [packaging handbook](docs/PERFORMANCE_AND_PACKAGING.md) | Limit upgrade cleanup to named artifacts. [Upgrade tests](tests/test_overlay_upgrade_tombstones.py) |
| AutoResearch profiling and benchmark setup | [Profile doctor](doctor.ps1) and [performance handbook](docs/PERFORMANCE_AND_PACKAGING.md) | Select the profile and phase that produced the measurement. |

## Choose a check

Run from the repository root unless a different directory is specified. Use
[Windows quality gates](.github/workflows/quality-gates.yml) for exact setup,
worker staging, Rust formatting, Clippy and full-suite commands.

| Changed component | Local check or CI job |
| --- | --- |
| Python | Run `scripts\project-python.cmd -m pytest` with the relevant test path. Ruff covers `src tests scripts`. For full mypy coverage use the exact directories and files in the [Python typecheck job](.github/workflows/python-full-suite.yml). Pytest, browser integration and typechecking have separate jobs. |
| React / Vite | From `Frontend`, choose `npm run check`, `npm run lint`, `npm test` or `npm run build` for the change. Build once. Use `npm run test:tooling` for the launcher and `npm run test:i18n` for locale changes. [Scripts](Frontend/package.json) |
| Tauri shell | From `Frontend/src-tauri`, run the matching Cargo test with `--locked`. CI runs `cargo test --locked --all-targets`. |
| Audio worker | Even though `audio_sidecar.rs` lives in the shell tree, its binary belongs to [the standalone worker](native/scriber-audio-sidecar/Cargo.toml). Use `cargo test --locked --manifest-path native/scriber-audio-sidecar/Cargo.toml` with the relevant test filter. |
| LocalVQE | Use the worker manifest above with `-p scriber-localvqe` for the pinned upstream reference tests. |
| Chrome extension | From the root, run `node --test browser-extension/chrome/tests/*.test.cjs`. |
| GitHub Actions | Use the pinned `actionlint` command in the [workflow-lint job](.github/workflows/hybrid-pr-checks.yml). |

For CI-equivalent pytest parallelism, use `-n 4 --dist loadfile -ra`. Run the
real Windows host-profile smoke separately and serially. In real-controller
tests, close cached database connections and bind `src.database._DB_PATH` to
a per-test database before constructing the controller.

For installer builds, microphone tests on hardware, real-provider runs or
release work, use the [test and release handbook](docs/TESTING_AND_RELEASE.md).
Report the evidence actually obtained: a unit test, a physical capture, a
provider-completed transcript and a signed installer each verify a different
part of the workflow. Python-versus-Rust capture comparisons require a
historical build with Python capture. The historical
`-RequireRustAudioPromotionReadiness` switch still requires that comparison.

See the [README](README.md) for setup and user workflows and the
[roadmap](docs/ROADMAP_AND_KNOWN_ISSUES.md) for outstanding work.

## Endpoint-Security-Safe Automation

- Treat enterprise EDR visibility as a design constraint. Never use
  `Invoke-Expression`/`iex`, `-EncodedCommand`, downloaded or generated script
  text, or AST extraction followed by execution. Do not pass generated
  multi-line source through `powershell.exe -Command`.
- Put reusable PowerShell logic in reviewed, checked-in `.ps1` or `.psm1`
  files with typed, allowlisted parameters. Invoke those files with `-File`;
  prefer direct Python, Rust, npm, Cargo, or GitHub Actions primitives when
  PowerShell adds no value.
- PowerShell tests may parse a script to validate syntax, but must not
  re-evaluate extracted function bodies. Prefer static contract tests or a
  narrow invocation of the real checked-in script with fixed arguments.
- Do not routinely add `-ExecutionPolicy Bypass` to new local commands. It is
  not an EDR bypass or a security boundary. Existing documented invocations
  may retain it for compatibility until they are deliberately migrated.
- Keep full installer builds, parallel cache publication, and large process
  trees on GitHub-hosted runners unless the user explicitly requests a local
  build. Locally, run the smallest focused gate once and avoid repeatedly
  replaying an EDR-sensitive command while debugging.
- Run focused Rust tests locally by default. If the standard Windows MSVC
  toolchain cannot build the tests (for example, the SDK lacks `kernel32.lib`),
  use the Windows Rust job from [Quality Gates](.github/workflows/quality-gates.yml)
  through [Hybrid PR Checks](.github/workflows/hybrid-pr-checks.yml) for the
  same revision instead of switching local toolchains.
- If Windows denies a test executable launch or Falcon blocks or quarantines
  the executable, stop local attempts for that target, including rebuilds and
  direct launches. Record the target and error, check available endpoint
  alerts, and use the same Windows CI job for the full Rust suite. Report the
  suite as pending until CI verifies the revision under test.
- Never recommend disabling CrowdStrike or excluding T3 Code, `powershell.exe`, a user,
  the whole repository, or all child processes. After removing the suspicious
  technique, any still-reproducible benign alert must be reviewed by the
  administrator and, if necessary, receive only a narrow, time-bounded IOA or
  signed-publisher exception scoped to the exact script, command line, host
  group, and detection pattern.
