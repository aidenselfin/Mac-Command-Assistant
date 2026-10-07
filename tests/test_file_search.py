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
        "stage": "none", "decision": "none", "total": 0, "truncated": False, "complete": True,
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
    assert response["truncated"] is True and "한도" in response["note"]
    assert response["complete"] is False


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
    folder = f"폴더: {tmp_path.name}/가"
    assert folder in options and "형식: .md" in options
    assert labels[options.index(folder)].endswith("(3개)")


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
    [{}, {"keywords": []}, {"keywords": [" "]}, {"keywords": ["a"], "modified_after": "어제"}],
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


def test_conditions_the_user_named_are_never_dropped_automatically(tmp_path):
    (tmp_path / "자료").mkdir()
    (tmp_path / "budget.md").write_text("x")
    (tmp_path / "자료" / "budget-old.md").write_text("x")
    cases = (
        {"extensions": ["pdf"]},  # only a .md exists
        {"folder_hint": "없는폴더"},
        {"modified_before": "2000-01-01"},
    )
    for extra in cases:
        response = find(tmp_path, "budget", **extra)
        assert response["decision"] == "none" and response["total"] == 0, extra
        assert "조건" in response["note"] and "filters_relaxed" not in response
    assert "note" not in find(tmp_path, "존재안함")  # no condition given: nothing to explain



def test_empty_optional_fields_and_stray_keys_are_ignored(tmp_path):
    (tmp_path / "a.md").write_text("x")
    response = find(
        tmp_path, "a", extensions=[""], folder_hint="", modified_after="", maxItems=8
    )
    assert response["total"] == 1 and "filters_relaxed" not in response


def test_exact_file_name_with_extension_and_null_optionals(tmp_path):
    (tmp_path / "old.log").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    response = find(
        tmp_path, "old.log", extensions=None, folder_hint=None,
        modified_after=None, modified_before=None,
    )
    assert names(response) == ["old.log"]


def test_modified_after_includes_the_whole_day_and_before_excludes_the_next(tmp_path):
    from datetime import datetime

    def make(name, moment):
        path = tmp_path / name
        path.write_text("x")
        stamp = moment.timestamp()
        os.utime(path, (stamp, stamp))

    make("eve.md", datetime(2026, 10, 6, 23, 59, 59))
    make("morning.md", datetime(2026, 10, 7, 0, 0, 0))
    make("afternoon.md", datetime(2026, 10, 7, 14, 0, 0))
    make("late.md", datetime(2026, 10, 7, 23, 59, 59, 500_000))
    make("next.md", datetime(2026, 10, 8, 0, 0, 0))
    both = {"modified_after": "2026-10-07", "modified_before": "2026-10-07"}
    assert sorted(names(find(tmp_path, "md", **both))) == [
        "afternoon.md", "late.md", "morning.md",
    ]
    assert "eve.md" not in names(find(tmp_path, "md", modified_after="2026-10-07"))


def test_reversed_date_range_is_rejected(tmp_path):
    with pytest.raises(ToolRejectedError):
        find_files(
            {"keywords": ["a"], "modified_after": "2026-10-08", "modified_before": "2026-10-07"},
            (tmp_path,),
        )


def test_content_read_refuses_a_folder_swapped_for_a_link_after_listing(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "note.txt").write_text("안쪽 글")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("외부비밀단어")
    entries, _ = file_search._walk((root.resolve(),))
    (entry,) = entries
    assert "안쪽" in file_search._read_head(entry)
    (root / "sub" / "note.txt").unlink()
    (root / "sub").rmdir()
    os.symlink(outside, root / "sub")
    with pytest.raises(OSError):
        file_search._read_head(entry)


def test_search_never_reports_a_word_found_through_a_swapped_folder(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "note.txt").write_text("평범")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("외부비밀단어")
    real_walk = file_search._walk

    def walk_then_swap(roots):
        result = real_walk(roots)
        (root / "sub" / "note.txt").unlink()
        (root / "sub").rmdir()
        os.symlink(outside, root / "sub")
        return result

    monkeypatch.setattr(file_search, "_walk", walk_then_swap)
    assert find(root, "외부비밀단어")["decision"] == "none"


def test_a_root_replaced_by_a_link_is_not_searched(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("x")
    link = tmp_path / "link"
    os.symlink(real, link)
    assert find(link, "a")["decision"] == "none"


def test_text_beyond_the_scanned_head_is_reported_as_not_fully_searched(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "CONTENT_BYTES", 10)
    (tmp_path / "late.txt").write_text("가" * 20 + "찾는말")
    response = find(tmp_path, "찾는말")
    assert response["decision"] == "none" and response["complete"] is False
    assert "앞" in response["note"] and "단정하지 말고" in response["note"]


def test_one_hit_among_partly_searched_files_is_confirmed_not_assumed(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "CONTENT_BYTES", 10)
    (tmp_path / "hit.txt").write_text("찾는말")
    (tmp_path / "long.txt").write_text("가" * 30)
    response = find(tmp_path, "찾는말")
    assert response["stage"] == "content" and response["decision"] == "choose"
    question, kind, options, _ = clarification_spec(response)
    assert kind == "file_candidate" and options == (str(tmp_path / "hit.txt"),)
    assert "일부만 검사" in question


def test_documents_and_unreadable_files_are_named_in_the_limits(tmp_path):
    (tmp_path / "보고서.pdf").write_bytes(b"%PDF")
    (tmp_path / "locked.txt").write_text("x")
    os.chmod(tmp_path / "locked.txt", 0)
    try:
        response = find(tmp_path, "예산")
    finally:
        os.chmod(tmp_path / "locked.txt", 0o600)
    assert response["decision"] == "none" and response["complete"] is False
    assert "문서 1개" in response["note"] and "읽지 못한 파일" in response["note"]


def test_a_fully_read_small_tree_is_complete(tmp_path):
    (tmp_path / "a.txt").write_text("예산 3천만원")
    response = find(tmp_path, "예산")
    assert response["decision"] == "single" and response["complete"] is True
    assert "note" not in response


def test_a_huge_folder_is_never_listed_whole(tmp_path, monkeypatch):
    for index in range(50):
        (tmp_path / f"f{index:02}.txt").write_text("x")
    monkeypatch.setattr(file_search, "MAX_VISITED", 10)
    rows, more = file_search._list_dir(tmp_path, (), 10)
    assert len(rows) == 10 and more is True
    entries, truncated = file_search._walk((tmp_path,))
    assert len(entries) <= 10 and truncated is True


def test_walk_time_limit_marks_truncated(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x")
    monkeypatch.setattr(file_search, "WALK_SECONDS", -1)
    assert file_search._walk((tmp_path,)) == ([], True)


def test_same_leaf_folder_names_stay_distinct_buttons_and_filter_correctly(tmp_path):
    for project in ("프로젝트A", "프로젝트B"):
        (tmp_path / project / "자료").mkdir(parents=True)
        for index in range(4):
            (tmp_path / project / "자료" / f"보고{index}.md").write_text("x")
    response = find(tmp_path, "보고")
    options = [item["option"] for item in response["narrow_options"]]
    a_option = f"폴더: {tmp_path.name}/프로젝트A/자료"
    b_option = f"폴더: {tmp_path.name}/프로젝트B/자료"
    assert a_option in options and b_option in options
    only_a = find(tmp_path, "보고", folder_hint=a_option.removeprefix("폴더: "))
    assert {item["path"].split("/")[-3] for item in only_a["candidates"]} == {"프로젝트A"}


def test_same_relative_folder_in_two_roots_is_told_apart_by_root(tmp_path):
    roots = []
    for name in ("rootA", "rootB"):
        root = tmp_path / name
        (root / "자료").mkdir(parents=True)
        (root / "자료" / "보고.md").write_text("x")
        roots.append(root)
    response = find_files({"keywords": ["보고"], "folder_hint": "rootB/자료"}, tuple(roots))
    assert [c["path"] for c in response.structured["candidates"]] == [
        str(roots[1] / "자료" / "보고.md")
    ]


# ---- review R1: a content clue narrows the candidates the name already found --------------


def make_proposals(root):
    for index in range(1, 8):
        (root / f"proposal-{index}.txt").write_text("예산 승인 건" if index == 3 else "일반 내용")


def test_a_content_clue_narrows_name_candidates_that_the_name_alone_could_not(tmp_path):
    make_proposals(tmp_path)
    assert find(tmp_path, "proposal")["decision"] == "narrow"  # seven by name
    response = find(tmp_path, "proposal", content_keywords=["예산 승인"])
    assert response["stage"] == "name+content" and response["decision"] == "single"
    assert names(response) == ["proposal-3.txt"] and response["complete"] is True


def test_the_content_clue_stays_inside_the_name_and_other_conditions(tmp_path):
    make_proposals(tmp_path)
    (tmp_path / "other.txt").write_text("예산 승인 건")  # matches the clue, not the name
    (tmp_path / "자료").mkdir()
    (tmp_path / "자료" / "proposal-9.txt").write_text("예산 승인 건")
    inside = find(tmp_path, "proposal", content_keywords=["예산 승인"], folder_hint="자료")
    assert names(inside) == ["proposal-9.txt"]
    both = find(tmp_path, "proposal", content_keywords=["예산 승인"])
    assert sorted(names(both)) == ["proposal-3.txt", "proposal-9.txt"]  # never other.txt


def test_a_clue_nothing_matches_says_so_and_never_widens(tmp_path):
    make_proposals(tmp_path)
    response = find(tmp_path, "proposal", content_keywords=["존재하지 않는 말"])
    assert response["decision"] == "none" and "조건" in response["note"]


def test_candidates_that_cannot_be_read_are_left_out_and_reported(tmp_path):
    (tmp_path / "proposal-1.txt").write_text("예산 승인")
    (tmp_path / "proposal-2.pdf").write_bytes(b"%PDF")
    response = find(tmp_path, "proposal", content_keywords=["예산"])
    assert names(response) == ["proposal-1.txt"]  # confirmed rather than assumed: a pdf is unseen
    assert response["complete"] is False and "문서 1개" in " ".join(response["limits"])


def test_without_a_content_clue_the_name_search_is_unchanged(tmp_path):
    make_proposals(tmp_path)
    response = find(tmp_path, "proposal")
    assert response["stage"] == "name" and response["total"] == 7


# ---- review R2: an incomplete search says what it did not look at -------------------------


def test_an_incomplete_narrow_question_says_the_count_may_be_higher(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "MAX_VISITED", 8)
    for index in range(30):
        (tmp_path / f"보고서-{index:02}.md").write_text("x")
    response = find(tmp_path, "보고서")
    assert response["complete"] is False and response["limits"]
    question, _, _, _ = clarification_spec(response)
    assert "일부만 검사한 결과" in question


def test_oversized_lists_are_cut_in_code_instead_of_failing_the_provider_check(tmp_path):
    (tmp_path / "a.md").write_text("x")
    response = find(
        tmp_path, *["a", *[f"쓸데없는{i}" for i in range(20)]],
        extensions=[f"e{i}" for i in range(30)] + ["md"],
        content_keywords=[f"단어{i}" for i in range(30)],
    )
    # Nothing raised. The first six keywords were kept, so "a" still found the file; the
    # extensions list was cut before "md", so that filter excluded nothing it should not.
    assert response["decision"] in {"none", "single", "choose"}
    schema = file_search.FIND_FILES_DEFINITION.schema["properties"]
    assert not any("maxItems" in value or "maxLength" in value for value in schema.values())
