import math

import pytest

from voice_action.config import ConfigError, load_config
from voice_action.file_search import find_files
from voice_action.semantic_index import SemanticIndex

TOPICS = ("cat", "money", "meeting")
WORDS = {
    "고양이": "cat", "cat": "cat", "가계부": "money", "지출": "money", "money": "money",
    "회의": "meeting", "meeting": "meeting",
}


def vector(text: str) -> list[float]:
    counts = [0.0] * len(TOPICS)
    for word, topic in WORDS.items():
        if word in text.lower():
            counts[TOPICS.index(topic)] += 1
    norm = math.hypot(*counts) or 1.0
    return [c / norm for c in counts]


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def query(self, text):
        return vector(text)

    def document(self, text):
        self.calls += 1
        return vector(text)

    def image(self, data):
        self.calls += 1
        return vector(data.decode())  # the fake "picture" is the word it shows


def make_index(tmp_path, root, embedder=None):
    index = SemanticIndex((root,), tmp_path / "db" / "semantic.sqlite3", lambda: embedder, "fake")
    if embedder is not None:
        index._embedder = embedder
        index.refresh()
    return index


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "IMG_001.jpg").write_bytes(b"cat")
    (folder / "IMG_002.png").write_bytes(b"meeting")
    (folder / "expenses.csv").write_text("date,money\n1,100")
    (folder / "notes.txt").write_text("nothing relevant here")
    return folder


def names(result):
    return [item["path"].rsplit("/", 1)[-1] for item in result.get("candidates", [])]


def test_meaning_search_finds_what_no_keyword_matches(tmp_path, root):
    index = make_index(tmp_path, root, FakeEmbedder())
    request = {"keywords": ["고양이"], "query": "고양이 사진"}
    response = find_files(request, (root,), index).structured
    assert (response["stage"], names(response)) == ("meaning", ["IMG_001.jpg"])
    # A similarity guess is never the "only" file: the user confirms it.
    assert response["decision"] == "choose"
    assert "추정" in response["note"]
    cutoff = find_files({"keywords": ["qqq"], "query": "가계부 지출"}, (root,), index).structured
    assert names(cutoff) == ["expenses.csv"]


def test_keyword_hit_never_reaches_the_meaning_stage(tmp_path, root):
    embedder = FakeEmbedder()
    index = make_index(tmp_path, root, embedder)
    request = {"keywords": ["expenses"], "query": "고양이"}
    response = find_files(request, (root,), index).structured
    assert (response["stage"], names(response)) == ("name", ["expenses.csv"])


def test_nothing_similar_is_not_offered(tmp_path, root):
    index = make_index(tmp_path, root, FakeEmbedder())
    response = find_files({"keywords": ["qqq"], "query": "zzz"}, (root,), index).structured
    assert (response["stage"], response["decision"], response["total"]) == ("none", "none", 0)


def test_off_or_without_query_changes_nothing(tmp_path, root):
    index = make_index(tmp_path, root, FakeEmbedder())
    assert find_files({"keywords": ["고양이"]}, (root,), index).structured["stage"] == "none"
    without_index = find_files({"keywords": ["고양이"], "query": "고양이"}, (root,))
    assert without_index.structured["stage"] == "none"


def test_model_still_loading_says_so_instead_of_claiming_nothing_exists(tmp_path, root):
    index = make_index(tmp_path, root)  # embedder not loaded yet
    response = find_files({"keywords": ["qqq"], "query": "고양이"}, (root,), index).structured
    assert response["decision"] == "none" and response["complete"] is False
    assert any("불러오는 중" in reason for reason in response["limits"])


def test_unchanged_files_are_not_embedded_again_and_deleted_ones_are_forgotten(tmp_path, root):
    embedder = FakeEmbedder()
    index = make_index(tmp_path, root, embedder)
    first = embedder.calls
    assert first == 4
    index.refresh()
    assert embedder.calls == first
    (root / "IMG_001.jpg").unlink()
    index.refresh()
    response = find_files({"keywords": ["qqq"], "query": "고양이"}, (root,), index).structured
    assert response["decision"] == "none"


def test_a_link_is_not_followed(tmp_path, root):
    outside = tmp_path / "secret.txt"
    outside.write_text("cat cat cat")
    (root / "link.txt").symlink_to(outside)
    index = make_index(tmp_path, root, FakeEmbedder())
    response = find_files({"keywords": ["qqq"], "query": "cat"}, (root,), index).structured
    assert "link.txt" not in names(response)


def test_semantic_flag_defaults_off_and_is_validated(tmp_path):
    (tmp_path / "voice-action-test").mkdir()
    env = {"GROQ_API_KEY": "k"}
    assert load_config(cwd=tmp_path, home=tmp_path, environ=env).semantic_search is False
    def load(value):
        environ = {**env, "VOICE_ACTION_SEMANTIC_SEARCH": value}
        return load_config(cwd=tmp_path, home=tmp_path, environ=environ)

    assert load("on").semantic_search is True
    with pytest.raises(ConfigError):
        load("x")


def test_a_photo_and_a_text_file_each_need_their_own_score(tmp_path, root):
    class Weak(FakeEmbedder):
        """Every file scores 0.65 against the query: enough for a photo, too little for text."""

        def query(self, text):
            return [1.0, 0.0]

        def document(self, text):
            return [0.65, math.sqrt(1 - 0.65**2)]

        def image(self, data):
            return [0.65, math.sqrt(1 - 0.65**2)]

    index = make_index(tmp_path, root, Weak())
    response = find_files({"keywords": ["qqq"], "query": "음식 사진"}, (root,), index).structured
    assert names(response) == ["IMG_001.jpg", "IMG_002.png"]  # no csv or txt among them


def test_a_background_recheck_does_not_make_a_complete_index_look_partial(tmp_path, root):
    index = make_index(tmp_path, root, FakeEmbedder())
    index._busy = True  # the once-a-minute pass is running again
    response = find_files({"keywords": ["qqq"], "query": "고양이"}, (root,), index).structured
    assert response["complete"] is True
