from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UX_PROFILE = "ux"
INSTALLER_SIZE_PROFILE = "installer-size"
PROFILE_NAMES = (UX_PROFILE, INSTALLER_SIZE_PROFILE)


class ProfileError(ValueError):
    """Raised when an AutoResearch profile invocation is not safely scoped."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ProfileError(f"missing profile config: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"invalid profile config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"profile config must contain a JSON object: {path}")
    return value


def canonical_run_id(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as exc:
        raise ProfileError("installer-size RunId must be a canonical RFC 4122 UUID") from exc
    canonical = str(parsed)
    if text != canonical or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise ProfileError("installer-size RunId must be a canonical non-nil RFC 4122 UUID")
    return canonical


@dataclass(frozen=True, slots=True)
class ProfileContext:
    repo_root: Path
    name: str
    run_id: str | None
    duration_seconds: int | None
    config_path: Path
    goal_path: Path
    state_root: Path
    run_root: Path | None

    @property
    def is_installer_size(self) -> bool:
        return self.name == INSTALLER_SIZE_PROFILE

    @property
    def config(self) -> dict[str, Any]:
        return _load_object(self.config_path)


def _installer_duration(config: dict[str, Any], requested: int | None) -> int:
    configured = config.get("durationSeconds")
    if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
        raise ProfileError("installer-size config durationSeconds must be a positive integer")
    duration = configured if requested is None else requested
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise ProfileError("DurationSeconds must be a positive integer")
    if duration != configured:
        raise ProfileError(
            f"installer-size DurationSeconds is frozen at {configured}; received {duration}"
        )
    return duration


def resolve_profile_context(
    repo_root: Path | str,
    *,
    profile: str = UX_PROFILE,
    run_id: str | None = None,
    duration_seconds: int | None = None,
    require_run_id: bool = False,
) -> ProfileContext:
    root = Path(repo_root).resolve()
    normalized_profile = str(profile or UX_PROFILE).strip().lower()
    if normalized_profile not in PROFILE_NAMES:
        raise ProfileError(
            f"unknown AutoResearch profile {profile!r}; expected one of {', '.join(PROFILE_NAMES)}"
        )

    if normalized_profile == UX_PROFILE:
        if run_id:
            raise ProfileError("RunId is only valid with -Profile installer-size")
        if duration_seconds is not None:
            raise ProfileError("DurationSeconds is only valid with -Profile installer-size")
        return ProfileContext(
            repo_root=root,
            name=UX_PROFILE,
            run_id=None,
            duration_seconds=None,
            config_path=root / "autoresearch.config.json",
            goal_path=root / "GOAL.md",
            state_root=root / ".git" / "autoresearch",
            run_root=None,
        )

    config_path = root / "scripts" / "perf" / "profiles" / "installer-size" / "config.json"
    goal_path = root / "scripts" / "perf" / "profiles" / "installer-size" / "GOAL.md"
    config = _load_object(config_path)
    duration = _installer_duration(config, duration_seconds)
    canonical = canonical_run_id(run_id) if run_id else None
    if require_run_id and canonical is None:
        raise ProfileError("installer-size requires -RunId <canonical UUID>")

    state_root = (root / "autoresearch-results" / "installer-size").resolve()
    run_root = (state_root / canonical).resolve() if canonical else None
    if run_root is not None and run_root.parent != state_root:
        raise ProfileError("installer-size RunId escaped the namespaced state root")
    return ProfileContext(
        repo_root=root,
        name=INSTALLER_SIZE_PROFILE,
        run_id=canonical,
        duration_seconds=duration,
        config_path=config_path,
        goal_path=goal_path,
        state_root=state_root,
        run_root=run_root,
    )
