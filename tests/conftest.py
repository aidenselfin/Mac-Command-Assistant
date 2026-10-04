import pytest

from voice_action.config import load_config


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "voice-action-test"
    root.mkdir()
    (root / "한글 메모.txt").write_text("hello")
    return load_config(cwd=tmp_path, home=tmp_path, environ={"OPENAI_API_KEY": "test-secret"})
