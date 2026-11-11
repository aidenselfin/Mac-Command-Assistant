import os

import pytest

from voice_action.file_search import group_files
from voice_action.mcp_client import ToolRejectedError


def group(root, **arguments):
    return group_files(arguments, (root,)).structured


def labels(response):
    return {item["label"]: sorted(p.rsplit("/", 1)[-1] for p in item["files"])
            for item in response["groups"]}


def make(root, *names):
    for name in names:
        (root / name).write_text("비밀 내용")


def test_keyword_groups_match_names_only(tmp_path):
    make(tmp_path, "회의록_1월.md", "회의록 2월.txt", "Invoice-03.pdf", "memo.txt")
    response = group(tmp_path, keywords=["회의록", "invoice"])
    assert labels(response) == {
        "회의록": ["회의록 2월.txt", "회의록_1월.md"], "invoice": ["Invoice-03.pdf"],
    }
    assert "비밀" not in str(response)  # content never reaches the model


def test_similar_names_and_kinds(tmp_path):
    make(tmp_path, "IMG_0001.jpg", "IMG_0002.jpg", "보고서_최종.docx", "보고서_초안.docx",
         "a.png", "single.txt")
    by_name = labels(group(tmp_path, by="name"))
    assert by_name == {"img": ["IMG_0001.jpg", "IMG_0002.jpg"],
                       "보고서": ["보고서_초안.docx", "보고서_최종.docx"]}
    by_kind = labels(group(tmp_path, by="kind"))
    assert by_kind["이미지"] == ["IMG_0001.jpg", "IMG_0002.jpg", "a.png"]
    assert by_kind["문서"] == ["single.txt", "보고서_초안.docx", "보고서_최종.docx"]


def test_files_already_in_the_matching_folder_are_left_out(tmp_path):
    (tmp_path / "보고서").mkdir()
    make(tmp_path, "보고서_a.md", "보고서_b.md", "보고서/보고서_c.md")
    files = labels(group(tmp_path, keywords=["보고서"]))["보고서"]
    assert files == ["보고서_a.md", "보고서_b.md"]


def test_large_group_is_cut_and_counted(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_action.file_search.GROUP_SHOW", 2)
    make(tmp_path, *(f"scan_{index}.pdf" for index in range(5)))
    item = group(tmp_path, keywords=["scan"])["groups"][0]
    assert (item["count"], len(item["files"])) == (5, 2)


def test_symlinked_file_is_not_grouped(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make(outside, "secret_1.txt")
    root = tmp_path / "root"
    root.mkdir()
    make(root, "secret_2.txt")
    os.symlink(outside / "secret_1.txt", root / "secret_1.txt")
    assert labels(group(root, keywords=["secret"])) == {"secret": ["secret_2.txt"]}


def test_bad_arguments_are_rejected(tmp_path):
    with pytest.raises(ToolRejectedError):
        group_files({"by": "color"}, (tmp_path,))


def test_is_read_only():
    from voice_action.risk_guard import ToolRisk, classify_tool

    assert classify_tool("local__group_files") is ToolRisk.READ_ONLY


def test_offered_only_for_grouping_requests():
    from voice_action.planner import GROUP_PATTERN

    assert GROUP_PATTERN.search("비슷한 파일끼리 묶어줘")
    assert GROUP_PATTERN.search("다운로드 폴더 정리해줘")
    assert not GROUP_PATTERN.search("회의록 요약해줘")
