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
