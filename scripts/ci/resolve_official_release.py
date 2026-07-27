"""Resolve the one canonical decision that may authorize an official release."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys


OFFICIAL_TAG_PATTERN = re.compile(r"^refs/tags/v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def read_source_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise ValueError(f"{path} does not contain a literal __version__ assignment")


def resolve_official_release(
    *,
    event_name: str,
    ref: str,
    ref_type: str,
    github_sha: str,
    main_sha: str,
    source_version: str,
) -> dict[str, object]:
    github_sha = github_sha.strip().lower()
    main_sha = main_sha.strip().lower()
    tag_match = OFFICIAL_TAG_PATTERN.fullmatch(ref)
    tag_version = tag_match.group("version") if tag_match else ""
    checks = {
        "pushEvent": event_name == "push",
        "canonicalTagRef": tag_match is not None,
        "tagRefType": ref_type == "tag",
        "validGithubSha": COMMIT_PATTERN.fullmatch(github_sha) is not None,
        "validMainSha": COMMIT_PATTERN.fullmatch(main_sha) is not None,
        "exactMainCommit": github_sha == main_sha,
        "sourceVersionMatchesTag": bool(tag_version and source_version == tag_version),
    }
    official = all(checks.values())
    return {
        "schemaVersion": 1,
        "officialRelease": official,
        "releaseTag": f"v{tag_version}" if tag_version else "",
        "releaseVersion": tag_version,
        "sourceVersion": source_version,
        "githubSha": github_sha,
        "mainSha": main_sha,
        "checks": checks,
    }


def _write_outputs(path: Path, decision: dict[str, object]) -> None:
    lines = [
        f"official-release={str(decision['officialRelease']).lower()}",
        f"release-tag={decision['releaseTag']}",
        f"release-version={decision['releaseVersion']}",
    ]
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--ref-type", required=True)
    parser.add_argument("--github-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--version-file", type=Path, default=Path("src/version.py"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    source_version = read_source_version(args.version_file)
    decision = resolve_official_release(
        event_name=args.event_name,
        ref=args.ref,
        ref_type=args.ref_type,
        github_sha=args.github_sha,
        main_sha=args.main_sha,
        source_version=source_version,
    )
    print(json.dumps(decision, indent=2, sort_keys=True))
    if args.github_output:
        _write_outputs(args.github_output, decision)

    # A tag-like automatic invocation is never allowed to degrade into a
    # non-official build. It must satisfy every official-release invariant.
    if args.event_name == "push" and args.ref.startswith("refs/tags/v"):
        if not decision["officialRelease"]:
            failed = ", ".join(name for name, passed in decision["checks"].items() if not passed)
            print(f"Official release authorization failed: {failed}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
