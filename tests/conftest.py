import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DROID_DATA", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # so create_app() doesn't pick up the real .env
