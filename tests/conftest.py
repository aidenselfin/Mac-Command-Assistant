import pytest

from voice_action.config import load_config


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "voice-action-test"
    root.mkdir()
    (root / "한글 메모.txt").write_text("hello")
    return load_config(cwd=tmp_path, home=tmp_path, environ={"OPENAI_API_KEY": "test-secret"})


@pytest.fixture(autouse=True)
def isolated_feedback_db(tmp_path, monkeypatch):
    """Keep test runs out of the real feedback history in the working directory."""
    monkeypatch.setattr("voice_action.feedback.DEFAULT_DB_PATH", tmp_path / "feedback.sqlite3")
