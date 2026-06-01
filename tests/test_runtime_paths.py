import os

from src.runtime import paths


def test_dev_paths_default_to_repo_root(monkeypatch):
    monkeypatch.delenv("SCRIBER_DATA_DIR", raising=False)
    monkeypatch.delenv("SCRIBER_DATABASE_PATH", raising=False)
    monkeypatch.delenv("SCRIBER_DOWNLOADS_DIR", raising=False)

    root = paths.repo_root()

    assert paths.uses_user_data_dir() is False
    assert paths.data_dir() == root
    assert paths.settings_path() == root / "settings.json"
    assert paths.database_path() == root / "transcripts.db"


def test_explicit_data_dir_controls_runtime_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SCRIBER_DATABASE_PATH", raising=False)
    monkeypatch.delenv("SCRIBER_DOWNLOADS_DIR", raising=False)

    assert paths.uses_user_data_dir() is True
    assert paths.data_dir() == tmp_path.resolve()
    assert paths.settings_path() == tmp_path / "settings.json"
    assert paths.env_path() == tmp_path / ".env"
    assert paths.database_path() == tmp_path / "transcripts.db"
    assert paths.downloads_dir() == (tmp_path / "downloads").resolve()


def test_relative_downloads_dir_uses_data_dir_when_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DOWNLOADS_DIR", "custom-downloads")

    assert paths.downloads_dir() == (tmp_path / "data" / "custom-downloads").resolve()


def test_database_path_can_be_overridden(monkeypatch, tmp_path):
    custom_db = tmp_path / "custom" / "scriber.db"

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DATABASE_PATH", str(custom_db))

    assert paths.database_path() == custom_db.resolve()


def test_logs_and_support_bundle_dirs_use_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SCRIBER_LOG_DIR", raising=False)

    assert paths.logs_dir() == (tmp_path / "data" / "logs").resolve()
    assert paths.support_bundles_dir() == (tmp_path / "data" / "support-bundles").resolve()


def test_logs_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_LOG_DIR", "diagnostics")

    assert paths.logs_dir() == (tmp_path / "data" / "diagnostics").resolve()


def test_migrate_legacy_runtime_data_copies_missing_files(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    data = tmp_path / "data"
    legacy.mkdir()
    (legacy / ".env").write_text("SONIOX_API_KEY=dummy\n", encoding="utf-8")
    (legacy / "settings.json").write_text('{"summarizationPrompt":"old"}', encoding="utf-8")
    (legacy / "transcripts.db").write_bytes(b"sqlite")
    (legacy / "downloads" / "youtube").mkdir(parents=True)
    (legacy / "downloads" / "youtube" / "audio.mp3").write_bytes(b"audio")
    (legacy / "models").mkdir()
    (legacy / "models" / "model.bin").write_bytes(b"model")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setenv("SCRIBER_LEGACY_DATA_DIR", str(legacy))
    monkeypatch.delenv("SCRIBER_SKIP_LEGACY_DATA_MIGRATION", raising=False)

    result = paths.migrate_legacy_runtime_data()

    assert result["attempted"] is True
    assert result["source"] == str(legacy.resolve())
    assert (data / ".env").read_text(encoding="utf-8") == "SONIOX_API_KEY=dummy\n"
    assert (data / "settings.json").read_text(encoding="utf-8") == '{"summarizationPrompt":"old"}'
    assert (data / "transcripts.db").read_bytes() == b"sqlite"
    assert (data / "downloads" / "youtube" / "audio.mp3").read_bytes() == b"audio"
    assert (data / "models" / "model.bin").read_bytes() == b"model"


def test_migrate_legacy_runtime_data_never_overwrites(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    data = tmp_path / "data"
    legacy.mkdir()
    data.mkdir()
    (legacy / ".env").write_text("OLD=1\n", encoding="utf-8")
    (legacy / "transcripts.db").write_bytes(b"old-db")
    (legacy / "transcripts.db-wal").write_bytes(b"old-wal")
    (data / ".env").write_text("NEW=1\n", encoding="utf-8")
    (data / "transcripts.db").write_bytes(b"new-db")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setenv("SCRIBER_LEGACY_DATA_DIR", str(legacy))

    result = paths.migrate_legacy_runtime_data()

    assert result["attempted"] is True
    assert (data / ".env").read_text(encoding="utf-8") == "NEW=1\n"
    assert (data / "transcripts.db").read_bytes() == b"new-db"
    assert not (data / "transcripts.db-wal").exists()


def test_migrate_legacy_runtime_data_continues_across_sources(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    data = tmp_path / "data"
    first.mkdir()
    second.mkdir()
    (first / ".env").write_text("SONIOX_API_KEY=first\n", encoding="utf-8")
    (second / "transcripts.db").write_bytes(b"second-db")
    (second / "transcripts.db-wal").write_bytes(b"second-wal")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setenv("SCRIBER_LEGACY_DATA_DIR", os.pathsep.join([str(first), str(second)]))

    result = paths.migrate_legacy_runtime_data()

    assert result["sources"] == [str(first.resolve()), str(second.resolve())]
    assert (data / ".env").read_text(encoding="utf-8") == "SONIOX_API_KEY=first\n"
    assert (data / "transcripts.db").read_bytes() == b"second-db"
    assert (data / "transcripts.db-wal").read_bytes() == b"second-wal"


def test_migrate_legacy_runtime_data_does_not_mix_wal_from_later_source(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    data = tmp_path / "data"
    first.mkdir()
    second.mkdir()
    (first / "transcripts.db").write_bytes(b"first-db")
    (second / "transcripts.db-wal").write_bytes(b"second-wal")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setenv("SCRIBER_LEGACY_DATA_DIR", os.pathsep.join([str(first), str(second)]))

    paths.migrate_legacy_runtime_data()

    assert (data / "transcripts.db").read_bytes() == b"first-db"
    assert not (data / "transcripts.db-wal").exists()
