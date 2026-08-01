"""Isolated, offline worker for exact-revision Hub snapshot reload checks.

The parent sends one JSON object on stdin. This worker never accepts a token,
repository identifier, or remote revision and cannot contact the Hub.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main() -> int:
    required_environment = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    }
    if any(os.environ.get(name) != value for name, value in required_environment.items()):
        return _fail("isolated reload worker requires an offline environment")
    try:
        dependency_roots = json.loads(
            os.environ["SCRIBER_HUB_RELOAD_DEPENDENCY_ROOTS_JSON"]
        )
    except (KeyError, json.JSONDecodeError):
        return _fail("isolated reload worker has no trusted dependency roots")
    if not isinstance(dependency_roots, list) or not dependency_roots:
        return _fail("isolated reload worker dependency roots are invalid")
    trusted_roots: list[str] = []
    seen_roots: set[str] = set()
    for raw in dependency_roots:
        if not isinstance(raw, str):
            return _fail("isolated reload worker dependency roots are invalid")
        candidate = Path(raw)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return _fail("isolated reload worker dependency roots are invalid")
        normalized = os.path.normcase(str(resolved))
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not resolved.is_dir()
            or str(candidate) != str(resolved)
            or normalized in seen_roots
        ):
            return _fail("isolated reload worker dependency roots are invalid")
        seen_roots.add(normalized)
        trusted_roots.append(str(resolved))
    sys.path[1:1] = trusted_roots

    def dependency_is_trusted(module_name: str) -> bool:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return False
        locations: list[Path] = []
        if spec.origin and spec.origin not in {"built-in", "frozen"}:
            locations.append(Path(spec.origin).resolve())
        if spec.submodule_search_locations:
            locations.extend(
                Path(location).resolve() for location in spec.submodule_search_locations
            )
        return bool(locations) and all(
            any(location.is_relative_to(Path(root)) for root in trusted_roots)
            for location in locations
        )

    if not all(
        dependency_is_trusted(module_name)
        for module_name in ("yaml", "jsonschema")
    ):
        return _fail("isolated reload worker dependencies are incomplete")
    try:
        from scriber_polishing.hub_publication import (
            PublicationError,
            offline_reload_dataset,
            offline_reload_model,
        )
    except ImportError:
        return _fail("isolated reload worker dependencies are incomplete")
    credential_markers = ("TOKEN", "API_KEY", "PASSWORD", "SECRET")
    allowed_credential_control = "HF_HUB_DISABLE_IMPLICIT_" + "TOKEN"
    if any(
        value
        for name, value in os.environ.items()
        if name != allowed_credential_control
        if any(marker in name.upper() for marker in credential_markers)
    ):
        return _fail("isolated reload worker refuses inherited credentials")
    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError:
        return _fail("isolated reload request is invalid JSON")
    if not isinstance(request, Mapping) or request.get("schema_version") != 1:
        return _fail("isolated reload request has an unsupported schema")
    kind = request.get("kind")
    snapshot = request.get("snapshot")
    definition = request.get("definition")
    if kind not in {"model", "dataset"}:
        return _fail("isolated reload request has an invalid artifact kind")
    if kind == "model" and not dependency_is_trusted("transformers"):
        return _fail("isolated reload worker dependencies are incomplete")
    if not isinstance(snapshot, str) or not isinstance(definition, Mapping):
        return _fail("isolated reload request is incomplete")
    try:
        result = (
            offline_reload_model(snapshot, definition)
            if kind == "model"
            else offline_reload_dataset(snapshot, definition)
        )
    except (OSError, ValueError, PublicationError) as error:
        return _fail(f"isolated reload failed: {type(error).__name__}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
