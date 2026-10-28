import os
import time

import pytest

from voice_action.file_search import list_files
from voice_action.mcp_client import ToolRejectedError
from voice_action.risk_guard import ToolRisk, classify_tool

DAY = 86400


def make(root, name, size, age_days=0):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


def listed(root, **arguments):
    return list_files(arguments, (root,)).structured


def names(response):
    return [item["name"] for item in response["files"]]


def test_sorts_by_size_largest_first_with_bytes(tmp_path):
    make(tmp_path, "a.txt", 100)
    make(tmp_path, "b.txt", 500)
    make(tmp_path, "c.txt", 300)
    response = listed(tmp_path, sort_by="size")
    assert names(response) == ["b.txt", "c.txt", "a.txt"]
    assert response["files"][0]["size_bytes"] == 500


def test_sorts_by_modified_newest_first_and_limit(tmp_path):
    for name, age in (("a.txt", 5), ("b.txt", 1), ("c.txt", 3)):
        make(tmp_path, name, 1, age)
    assert names(listed(tmp_path, sort_by="modified")) == ["b.txt", "c.txt", "a.txt"]
    response = listed(tmp_path, sort_by="modified", limit=2)
    assert names(response) == ["b.txt", "c.txt"]
    assert (response["total"], response["shown"]) == (3, 2)


def test_age_window_and_extension_filters(tmp_path):
    make(tmp_path, "new.csv", 1, 1)
    make(tmp_path, "old.csv", 1, 10)
    make(tmp_path, "new.txt", 1, 1)
    make(tmp_path, "x.pdf", 1, 45)
    make(tmp_path, "y.pdf", 1, 70)
    make(tmp_path, "w.pdf", 1, 5)
    assert names(listed(tmp_path, modified_within_days=3, extensions=["csv"])) == ["new.csv"]
    window = listed(tmp_path, extensions=[".PDF"], modified_within_days=60,
                    not_modified_for_days=30)
    assert names(window) == ["x.pdf"]


def test_folder_totals_largest_first(tmp_path):
    make(tmp_path, "큰폴더/a.txt", 900)
    make(tmp_path, "작은폴더/b.txt", 10)
    make(tmp_path, "중간폴더/c.txt", 300)
    make(tmp_path, "중간폴더/sub/d.txt", 300)
    make(tmp_path, "top.txt", 5000)  # a loose file is not a folder
    response = listed(tmp_path, group_by="folder")
    assert [f["folder"] for f in response["folders"]] == ["큰폴더", "중간폴더", "작은폴더"]
    assert response["folders"][1]["size_bytes"] == 600


def test_rejects_bad_arguments(tmp_path):
    with pytest.raises(ToolRejectedError):
        list_files({"sort_by": "colour"}, (tmp_path,))
    with pytest.raises(ToolRejectedError):
        list_files({"modified_within_days": -1}, (tmp_path,))


def test_symlink_outside_root_is_not_listed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    make(outside, "secret.txt", 50)
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "file-link.txt").symlink_to(outside / "secret.txt")
    assert listed(root)["files"] == []


def test_is_read_only():
    assert classify_tool("local__list_files") is ToolRisk.READ_ONLY


def test_ranked_pick_words():
    from voice_action.planner import RANKED_PICK_PATTERN

    assert RANKED_PICK_PATTERN.search("가장 최근에 수정한 계약서의 금액")
    assert RANKED_PICK_PATTERN.search("제일 큰 사진")
    assert not RANKED_PICK_PATTERN.search("계약서 금액이 얼마야")


def test_name_contains_narrows_before_the_limit(tmp_path):
    make(tmp_path, "계약서_v1.txt", 1, 40)
    make(tmp_path, "계약서_v2.txt", 1, 5)
    for index in range(25):
        make(tmp_path, f"other{index}.txt", 1, 0)  # newer unrelated files
    response = listed(tmp_path, name_contains=["계약서"], sort_by="modified", limit=1)
    assert names(response) == ["계약서_v2.txt"]


def test_paths_limit_to_candidates_and_roots_keep_same_named_folders_apart(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    make(one, "자료/a.txt", 10)
    make(two, "자료/b.txt", 30)
    response = list_files({"group_by": "folder"}, (one, two)).structured
    assert [f["folder"] for f in response["folders"]] == ["two/자료", "one/자료"]
    only = list_files({"paths": [str(one / "자료/a.txt")]}, (one, two)).structured
    assert names(only) == ["a.txt"]
