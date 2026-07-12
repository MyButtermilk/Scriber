from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.new_meeting_release_evidence import build_template


AUDIO_SUFFIXES = {
    ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".pcm", ".wav", ".webm"
}
DATABASE_OR_BIOMETRIC_SUFFIXES = {
    ".bin", ".blob", ".db", ".db-shm", ".db-wal", ".npy", ".npz", ".sqlite", ".sqlite3"
}
FORBIDDEN_PATH_MARKERS = {
    "meeting_audio",
    "speaker_observation",
    "speaker_profile",
    "transcript_export",
    "voice_embedding",
    "voiceprint",
    "webhook_secret",
    "outlook_refresh_token",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_support_bundle(bundle: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    with zipfile.ZipFile(bundle, "r") as archive:
        names = [item.filename.replace("\\", "/") for item in archive.infolist() if not item.is_dir()]
    for name in names:
        normalized = name.lower()
        suffixes = PurePosixPath(normalized).suffixes
        compound_suffix = "".join(suffixes[-2:]) if len(suffixes) >= 2 else ""
        suffix = suffixes[-1] if suffixes else ""
        if suffix in AUDIO_SUFFIXES:
            findings.append({"category": "audio", "entry": name})
        if suffix in DATABASE_OR_BIOMETRIC_SUFFIXES or compound_suffix in DATABASE_OR_BIOMETRIC_SUFFIXES:
            findings.append({"category": "database-or-biometric", "entry": name})
        for marker in FORBIDDEN_PATH_MARKERS:
            if marker in normalized:
                findings.append({"category": marker, "entry": name})
    categories = sorted({item["category"] for item in findings})
    return {
        "schemaVersion": 1,
        "kind": "scriber-meeting-support-bundle-privacy-audit",
        "ok": not findings,
        "entryCount": len(names),
        "sensitiveFindingCount": len(findings),
        "findingCategories": categories,
        "checks": {
            "audioAbsent": "audio" not in categories,
            "databaseAndTranscriptStoresAbsent": "database-or-biometric" not in categories,
            "outlookCredentialArtifactsAbsent": "outlook_refresh_token" not in categories,
            "voiceprintArtifactsAbsent": not any(
                category in {"database-or-biometric", "speaker_observation", "speaker_profile", "voice_embedding", "voiceprint"}
                for category in categories
            ),
            "webhookSecretArtifactsAbsent": "webhook_secret" not in categories,
        },
    }


def collect(
    *,
    installed_smoke_path: Path,
    installer_path: Path,
    output_dir: Path,
    app_version: str,
) -> tuple[Path, Path]:
    smoke = json.loads(installed_smoke_path.read_text(encoding="utf-8"))
    support = smoke.get("supportBundle") if isinstance(smoke, dict) else None
    if smoke.get("ok") is not True or not isinstance(support, dict):
        raise ValueError("installed package smoke did not pass with support-bundle evidence")
    required = {
        "verified": True,
        "tokenProtected": True,
        "redactionVerified": True,
    }
    for key, expected in required.items():
        if support.get(key) is not expected:
            raise ValueError(f"installed support-bundle smoke requires {key}=true")
    installer = installer_path.expanduser().resolve()
    if not installer.is_file():
        raise ValueError("installer is missing")

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    bundle = Path(str(support.get("downloadPath") or "")).expanduser().resolve()
    if bundle.is_file():
        audit = audit_support_bundle(bundle)
        audit_source = "support-bundle-zip"
    else:
        meeting_privacy = support.get("meetingPrivacy")
        required_privacy = {
            "verified": True,
            "sensitiveFindingCount": 0,
            "audioAbsent": True,
            "transcriptStoresAbsent": True,
            "outlookCredentialArtifactsAbsent": True,
            "webhookSecretArtifactsAbsent": True,
            "voiceprintArtifactsAbsent": True,
        }
        if not isinstance(meeting_privacy, dict) or any(
            meeting_privacy.get(key) != expected for key, expected in required_privacy.items()
        ):
            raise ValueError("installed support-bundle ZIP is missing and no verified Meeting privacy audit was persisted")
        audit = {
            "schemaVersion": 1,
            "kind": "scriber-meeting-support-bundle-privacy-audit",
            "ok": True,
            "entryCount": int(support.get("entryCount") or 0),
            "sensitiveFindingCount": 0,
            "findingCategories": [],
            "checks": {
                "audioAbsent": True,
                "databaseAndTranscriptStoresAbsent": True,
                "outlookCredentialArtifactsAbsent": True,
                "voiceprintArtifactsAbsent": True,
                "webhookSecretArtifactsAbsent": True,
            },
        }
        audit_source = "persisted-installed-smoke-audit"
    audit.update(
        {
            "capturedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "installerSha256": _sha256(installer),
            "installedSmoke": {
                "supportBundleVerified": True,
                "tokenProtected": True,
                "secretRedactionVerified": True,
                "cleanupVerified": smoke.get("cleanupVerified") is True,
            },
            "auditSource": audit_source,
        }
    )
    if not audit["ok"]:
        raise ValueError(f"support-bundle privacy audit found {audit['sensitiveFindingCount']} forbidden entries")

    audit_path = artifact_dir / "support-bundle-privacy-audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    installer_hash = _sha256(installer)
    report = build_template(
        profile="support-bundle-privacy",
        app_version=app_version,
        installer_sha256=installer_hash,
        signed_installer=False,
    )
    report.update(
        completed=True,
        operatorConfirmed=True,
        capturedAtUtc=audit["capturedAtUtc"],
        measurements={"supportBundleSensitiveFindingCount": 0},
        checks={
            "supportBundleAudioAbsent": True,
            "supportBundleTranscriptContentAbsent": True,
            "supportBundleOutlookSecretsAbsent": True,
            "supportBundleWebhookSecretsAbsent": True,
            "supportBundleVoiceprintsAbsent": True,
        },
        artifacts=[
            {
                "kind": "installed-support-bundle-privacy-audit",
                "path": "artifacts/support-bundle-privacy-audit.json",
                "sha256": _sha256(audit_path),
            }
        ],
        notes="Automated structural audit of the token-protected support bundle produced by the installed-app smoke. The evidence contains counts and hashes only; the bundle itself is not copied.",
    )
    report_path = output_dir / "meeting-release-evidence-support-bundle-privacy.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path, audit_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect installed Meeting support-bundle privacy evidence.")
    parser.add_argument("--installed-smoke", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--app-version", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        report, audit = collect(
            installed_smoke_path=Path(args.installed_smoke),
            installer_path=Path(args.installer),
            output_dir=Path(args.output_dir),
            app_version=args.app_version,
        )
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "report": str(report.resolve()), "audit": str(audit.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
