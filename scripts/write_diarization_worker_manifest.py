"""Create the build attestation for Scriber's bundled diarization worker.

The output is bundled next to the executable by the signed installer/updater.
Runtime code refuses a frozen worker that lacks this manifest or whose digest,
size, protocol, engine version, or static-link identity does not match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


WORKER_NAME = "scriber-diarization-sidecar"
WORKER_FILE = f"{WORKER_NAME}.exe"
MANIFEST_FILE = f"{WORKER_NAME}.manifest.json"
WORKER_VERSION = "0.1.0"
SHERPA_ONNX_VERSION = "1.13.3"
PROTOCOL_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe(executable: Path, argument: str) -> dict[str, Any]:
    process = subprocess.run(
        [str(executable), argument],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if (
        process.returncode != 0
        or len(process.stdout) > MAX_CONTROL_BYTES
        or len(process.stderr) > MAX_CONTROL_BYTES
    ):
        raise RuntimeError("Diarization worker version probe failed.")
    try:
        payload = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Diarization worker returned invalid version metadata.") from exc
    if (
        payload.get("schemaVersion") != PROTOCOL_SCHEMA_VERSION
        or payload.get("ok") is not True
        or payload.get("worker", {}).get("name") != WORKER_NAME
        or payload.get("worker", {}).get("version") != WORKER_VERSION
        or payload.get("engine", {}).get("name") != "sherpa-onnx"
        or payload.get("engine", {}).get("version") != SHERPA_ONNX_VERSION
        or payload.get("engine", {}).get("linkMode") != "static"
    ):
        raise RuntimeError("Diarization worker version metadata is incompatible.")
    return payload


def build_manifest(executable: Path) -> dict[str, Any]:
    executable = executable.expanduser().resolve()
    if executable.name.casefold() != WORKER_FILE.casefold() or not executable.is_file():
        raise ValueError(f"Expected an existing {WORKER_FILE} executable.")
    version = _probe(executable, "--version")
    self_test = _probe(executable, "--self-test")
    if (
        self_test.get("schemaVersion") != PROTOCOL_SCHEMA_VERSION
        or self_test.get("ok") is not True
        or self_test.get("loadsUserAudio") is not False
        or self_test.get("loadsModels") is not False
        or self_test.get("platform", {}).get("windows") is not True
        or self_test.get("platform", {}).get("memoryLimit") != "jobObject"
    ):
        raise RuntimeError("Diarization worker self-test metadata is incompatible.")
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "distribution": "bundled-signed-scriber-resource",
        "worker": {
            "name": WORKER_NAME,
            "fileName": WORKER_FILE,
            "version": WORKER_VERSION,
            "protocolSchemaVersion": PROTOCOL_SCHEMA_VERSION,
            "sherpaOnnxVersion": SHERPA_ONNX_VERSION,
            "linkMode": "static",
            "sha256": _sha256(executable),
            "byteSize": executable.stat().st_size,
        },
    }


def write_manifest(executable: Path, output: Path) -> Path:
    executable = executable.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.name != MANIFEST_FILE or output.parent != executable.parent:
        raise ValueError(f"Manifest must be written next to the worker as {MANIFEST_FILE}.")
    payload = build_manifest(executable)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_manifest(args.executable, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
