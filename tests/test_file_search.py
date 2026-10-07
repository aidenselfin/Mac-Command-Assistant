import os
import unicodedata

import pytest

from voice_action import file_search
from voice_action.file_search import clarification_spec, find_files
from voice_action.mcp_client import ToolRejectedError


def find(root, *keywords, **extra):
    return find_files({"keywords": list(keywords), **extra}, (root,)).structured


def names(response):
    return [item["path"].rsplit("/", 1)[-1] for item in response.get("candidates", [])]


def test_name_match_is_the_first_stage_and_stops_there(tmp_path):
    (tmp_path / "회의록.md").write_text("본문")
    (tmp_path / "other.txt").write_text("회의록 이야기")  # content match must not be reached
    response = find(tmp_path, "회의록")
    assert (response["stage"], response["decision"]) == ("name", "single")
    assert names(response) == ["회의록.md"]


def test_nfd_file_name_matches_nfc_keyword(tmp_path):
    (tmp_path / unicodedata.normalize("NFD", "회의록.md")).write_text("x")
    assert find(tmp_path, unicodedata.normalize("NFC", "회의록"))["total"] == 1


def test_english_variant_and_separators(tmp_path):
    (tmp_path / "Team_Meeting-Minutes.txt").write_text("x")
    response = find(tmp_path, "회의록", "meeting minutes")
    assert names(response) == ["Team_Meeting-Minutes.txt"]


def test_widens_to_folder_path_then_content(tmp_path):
    folder = tmp_path / "프로젝트A" / "기획"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("x")
    assert find(tmp_path, "기획")["stage"] == "path"
    (tmp_path / "memo.txt").write_text("예산은 3천만원으로 확정")
    response = find(tmp_path, "예산")
    assert (response["stage"], names(response)) == ("content", ["memo.txt"])
    assert find(tmp_path, "존재하지않는단어") == {
        "stage": "none", "decision": "none", "total": 0, "truncated": False,
    }


def test_content_search_skips_non_text_and_reads_only_the_head(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "CONTENT_BYTES", 10)
    (tmp_path / "late.txt").write_text("가" * 20 + "찾는말")  # beyond the head
    (tmp_path / "image.png").write_text("찾는말")
    assert find(tmp_path, "찾는말")["decision"] == "none"


def test_content_scan_limit_marks_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "CONTENT_MAX_FILES", 2)
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("nothing")
    response = find(tmp_path, "zzz")
    assert response["truncated"] is True and "일부만" in response["note"]


def test_subfolders_symlinks_hidden_and_excluded_dirs(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "보고서.md").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "보고서.md").write_text("x")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "보고서.md").write_text("x")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "보고서-밖.md").write_text("x")
    os.symlink(outside, tmp_path / "link")
    os.symlink(outside / "보고서-밖.md", tmp_path / "보고서-링크.md")
    response = find(tmp_path, "보고서")
    assert response["total"] == 1 and response["candidates"][0]["path"].endswith("a/b/보고서.md")


def test_depth_and_visit_limits_mark_truncated(tmp_path, monkeypatch):
    deep = tmp_path.joinpath(*"abcd")
    deep.mkdir(parents=True)
    (deep / "x.md").write_text("x")
    monkeypatch.setattr(file_search, "MAX_DEPTH", 2)
    assert find(tmp_path, "x")["truncated"] is True
    monkeypatch.setattr(file_search, "MAX_DEPTH", 8)
    monkeypatch.setattr(file_search, "MAX_VISITED", 2)
    assert find(tmp_path, "x")["truncated"] is True


def test_filters_extension_folder_and_date(tmp_path):
    (tmp_path / "자료").mkdir()
    (tmp_path / "자료" / "메모.txt").write_text("x")
    (tmp_path / "메모.md").write_text("x")
    assert names(find(tmp_path, "메모", extensions=[".md"])) == ["메모.md"]
    assert names(find(tmp_path, "메모", folder_hint="자료")) == ["메모.txt"]
    old = tmp_path / "메모.md"
    os.utime(old, (0, 0))
    assert names(find(tmp_path, "메모", modified_after="2020-01-01")) == ["메모.txt"]


def test_two_to_five_candidates_are_listed_with_labels(tmp_path):
    for index in range(1, 4):
        (tmp_path / f"계획{index}.md").write_text("x" * index)
    question, kind, options, labels = clarification_spec(find(tmp_path, "계획"))
    assert kind == "file_candidate" and len(options) == len(labels) == 3
    assert all(path.startswith(str(tmp_path)) for path in options)
    assert "KB" not in labels[0] and "B" in labels[0]


def test_numbered_series_is_summarized_not_listed(tmp_path):
    for index in range(1, 38):
        (tmp_path / f"임시-{index}.txt").write_text("x")
    newest = tmp_path / "임시-12.txt"
    os.utime(newest, (4_000_000_000, 4_000_000_000))
    response = find(tmp_path, "임시")
    assert response["decision"] == "narrow" and "candidates" not in response
    assert response["series"]["count"] == 37
    assert response["series"]["range"] == "임시-1 ~ 임시-37"
    assert response["series"]["newest"] == str(newest)
    question, kind, options, labels = clarification_spec(response)
    assert kind == "narrow_search" and options[0] == str(newest)
    assert "37개" in question and len(options) <= 1 + file_search.MAX_FACETS


def test_many_unrelated_candidates_offer_folder_and_type_facets(tmp_path):
    for folder in ("가", "나"):
        (tmp_path / folder).mkdir()
        for index in range(3):
            (tmp_path / folder / f"보고서{chr(97 + index)}.md").write_text("x")
    (tmp_path / "나" / "보고서z.txt").write_text("x")
    question, kind, options, labels = clarification_spec(find(tmp_path, "보고서"))
    assert kind == "narrow_search" and "7개" in question
    assert "폴더: 가" in options and "형식: .md" in options
    assert labels[options.index("폴더: 가")].endswith("(3개)")


def test_single_and_none_need_no_question(tmp_path):
    (tmp_path / "a.md").write_text("x")
    assert clarification_spec(find(tmp_path, "a")) is None
    assert clarification_spec(find(tmp_path, "zzz")) is None


def test_number_in_keyword_does_not_match_longer_number(tmp_path):
    for name in ("임시-1.txt", "임시-12.txt"):
        (tmp_path / name).write_text("x")
    assert names(find(tmp_path, "임시-1")) == ["임시-1.txt"]


@pytest.mark.parametrize(
    "arguments",
    [{}, {"keywords": []}, {"keywords": ["a"] * 7}, {"keywords": ["a"], "x": 1},
     {"keywords": ["a"], "modified_after": "어제"}],
)
def test_invalid_arguments_are_rejected(tmp_path, arguments):
    with pytest.raises(ToolRejectedError):
        find_files(arguments, (tmp_path,))


def test_absolute_root_path_as_folder_hint_means_no_folder_filter(tmp_path):
    (tmp_path / "자료").mkdir()
    (tmp_path / "자료" / "임시-1.txt").write_text("x")
    root = tmp_path.resolve()
    assert find(root, "임시", folder_hint=str(root))["total"] == 1
    assert find(root, "임시", folder_hint=str(root / "자료"))["total"] == 1


def test_wrong_filters_are_relaxed_instead_of_hiding_the_file(tmp_path):
    (tmp_path / "임시-1.txt").write_text("x")
    response = find(tmp_path, "임시", extensions=["tmp"], folder_hint="없는폴더")
    assert response["total"] == 1 and response["filters_relaxed"] is True
    assert "filters_relaxed" not in find(tmp_path, "임시", extensions=["txt"])
    assert find(tmp_path, "존재안함", extensions=["tmp"])["decision"] == "none"
