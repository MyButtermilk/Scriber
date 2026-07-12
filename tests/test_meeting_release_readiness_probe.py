from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_readiness_probe_is_redacted_and_non_evidentiary() -> None:
    source = (REPO_ROOT / "scripts" / "probe_meeting_release_readiness.ps1").read_text(encoding="utf-8")
    assert "Get-PnpDevice -Class AudioEndpoint" in source
    assert "rawEndpointIdsEmitted = $false" in source
    assert "deviceNamesEmitted = $false" in source
    assert "accountDetailsEmitted = $false" in source
    assert "secretValuesEmitted = $false" in source
    assert "FriendlyName" in source
    assert "InstanceId" not in source
    assert "Readiness hints are not release evidence" in source
    assert "meeting-release-evidence" not in source
    assert "Readiness output must stay under the repository tmp directory" in source


def test_readiness_probe_covers_clients_routes_and_external_configuration() -> None:
    source = (REPO_ROOT / "scripts" / "probe_meeting_release_readiness.ps1").read_text(encoding="utf-8")
    for marker in (
        "teamsDesktop",
        "zoomDesktop",
        "googleChrome",
        "bluetooth",
        "usb",
        "outlookPublicClientConfigured",
        "updaterSigningKeyConfigured",
        "authenticodeSigningConfigured",
        "currentInstallerAuthenticodeValid",
        "runnableProfileHints",
    ):
        assert marker in source
