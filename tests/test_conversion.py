import json
import shutil
import struct
import sys
from unittest.mock import AsyncMock

import pytest

from voice_action.conversion import (
    CONVERT_TOOL,
    READ_DOCUMENT_TOOL,
    prepare_conversion,
    read_document,
    write_new_file,
)
from voice_action.mcp_client import ToolRejectedError
from voice_action.planner import (
    CONVERSION_REQUEST_PATTERN,
    ApprovalRequired,
    Completed,
    ModelTurn,
    Planner,
    ToolCall,
)
from voice_action.risk_guard import ApprovalChangedError, RiskGuard
from voice_action.undo import UndoJournal, UndoKind

macos = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("textutil") or not shutil.which("sips"),
    reason="macOS converters (textutil, sips, cupsfilter) needed",
)


def bmp(width=4, height=4) -> bytes:
    row = b"\x00\x00\xff" * width + b"\x00" * ((4 - width * 3 % 4) % 4)
    pixels = row * height
    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    info = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0
    )
    return header + info + pixels


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return folder.resolve()


def convert(root, source, fmt, **extra):
    arguments = {"source": str(source), "target_format": fmt, **extra}
    prepared = prepare_conversion(arguments, (root,))
    write_new_file(prepared.output, prepared.data)
    return prepared


# ---- engine --------------------------------------------------------------------------------


@macos
def test_documents_pdf_and_text_round_trip(root):
    (root / "메모.txt").write_text("안녕하세요\n두 번째 줄")
    docx = convert(root, root / "메모.txt", "docx")
    assert docx.output.endswith("메모.docx") and docx.data.startswith(b"PK")
    back = convert(root, root / "메모.docx", "txt")
    assert back.output.endswith("메모 (1).txt")  # never overwrites the original
    assert back.preview.startswith("안녕하세요")
    (root / "노트.md").write_text("# 제목\n본문")
    pdf = convert(root, root / "노트.md", "pdf")
    assert pdf.data.startswith(b"%PDF") and pdf.preview == ""
    assert "제목" in convert(root, root / "노트.pdf", "txt").data.decode()


@macos
@pytest.mark.parametrize("target, signature", [("jpeg", b"\xff\xd8\xff"), ("png", b"\x89PNG")])
def test_iphone_heic_photo_to_jpg_and_png(root, target, signature):
    (root / "사진.bmp").write_bytes(bmp())
    heic = convert(root, root / "사진.bmp", "heic")
    assert heic.data[4:12] == b"ftypheic"
    result = convert(root, root / "사진.heic", target)
    assert result.data.startswith(signature)
    assert result.output.endswith(".jpg" if target == "jpeg" else ".png")


def test_csv_and_json_round_trip(root):
    (root / "표.csv").write_text("이름,금액\n점심,12000\n", encoding="utf-8")
    as_json = convert(root, root / "표.csv", "json")
    assert json.loads(as_json.data) == [{"이름": "점심", "금액": "12000"}]
    back = convert(root, root / "표.json", "csv")
    assert back.data.decode() == "이름,금액\n점심,12000\n"


def _unsupported(root):
    (root / "a.csv").write_text("x")
    return {"target_format": "pdf"}, "지원하지 않는"


def _bad_signature(root):
    (root / "a.png").write_text("not png")
    return {"target_format": "jpeg"}, "실제 파일"


def _json_shape(root):
    (root / "a.json").write_text('{"a": 1}')
    return {"target_format": "csv"}, "배열"


def _output_taken(root):
    (root / "a.csv").write_text("x")
    (root / "out.json").write_text("taken")
    return {"target_format": "json", "output": str(root / "out.json")}, "이미 있습니다"


@pytest.mark.parametrize("case", [_unsupported, _bad_signature, _json_shape, _output_taken])
def test_conversion_rejections(root, case):
    arguments, message = case(root)
    source = next(p for p in root.iterdir() if p.name.startswith("a."))
    with pytest.raises(ToolRejectedError, match=message):
        prepare_conversion({"source": str(source), **arguments}, (root,))


def test_paths_must_stay_inside_allowed_folders(root, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n")
    (root / "link.csv").symlink_to(outside)
    for source in (outside, root / "link.csv"):
        with pytest.raises(ToolRejectedError, match="허용 폴더"):
            prepare_conversion({"source": str(source), "target_format": "json"}, (root,))


@macos
def test_read_document_returns_text_without_writing(root):
    (root / "메모.txt").write_text("문서 읽기 확인 " * 20)
    convert(root, root / "메모.txt", "docx")
    before = sorted(p.name for p in root.iterdir())
    result = read_document({"path": str(root / "메모.docx")}, (root,), 30)
    assert result.ok and result.truncated and result.text.startswith("문서 읽기 확인")
    assert sorted(p.name for p in root.iterdir()) == before
    with pytest.raises(ToolRejectedError, match="filesystem__read_text_file"):
        read_document({"path": str(root / "메모.txt")}, (root,), 30)


# ---- approval and Undo -----------------------------------------------------------------------


def guard(root, tmp_path):
    journal = UndoJournal(tmp_path / "undo.sqlite3", (root,))
    return RiskGuard(AsyncMock(), journal, (root,)), journal


async def test_conversion_is_written_only_after_approval_and_undo_removes_it(root, tmp_path):
    (root / "표.csv").write_text("a\n1\n")
    risk_guard, journal = guard(root, tmp_path)
    prepared = await risk_guard.prepare_write(
        CONVERT_TOOL, {"source": str(root / "표.csv"), "target_format": "json"}
    )
    assert prepared.operation == "convert" and prepared.targets == (str(root / "표.csv"),)
    assert not (root / "표.json").exists()  # nothing before approval
    result = await risk_guard.execute_approved(prepared)
    assert result.ok and (root / "표.json").exists()
    assert journal.undo_last_write().kind is UndoKind.CREATE_UNDONE
    assert not (root / "표.json").exists() and (root / "표.csv").exists()


async def test_source_changed_during_approval_needs_new_approval(root, tmp_path):
    (root / "표.csv").write_text("a\n1\n")
    risk_guard, _ = guard(root, tmp_path)
    arguments = {"source": str(root / "표.csv"), "target_format": "json"}
    prepared = await risk_guard.prepare_write(CONVERT_TOOL, arguments)
    (root / "표.csv").write_text("a\n2\n")
    with pytest.raises(ApprovalChangedError) as caught:
        await risk_guard.execute_approved(prepared)
    assert not (root / "표.json").exists()
    await risk_guard.execute_approved(caught.value.prepared)
    assert json.loads((root / "표.json").read_text()) == [{"a": "2"}]


async def test_read_document_is_read_only_through_risk_guard(root, tmp_path):
    risk_guard, _ = guard(root, tmp_path)
    with pytest.raises(ToolRejectedError):
        await risk_guard.execute_read_only(READ_DOCUMENT_TOOL, {"path": str(root / "x.pdf")})


# ---- planner -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, offered",
    [
        ("사진.heic를 jpg로 바꿔줘", True),
        ("회의록.txt를 PDF로 변환해줘", True),
        ("표.csv json으로 저장", True),
        ("폴더 파일 목록 보여줘", False),
        ("회의록 요약해줘", False),
    ],
)
def test_conversion_tool_is_offered_only_for_conversion_requests(text, offered):
    assert bool(CONVERSION_REQUEST_PATTERN.search(text)) is offered


def convert_turn(root):
    arguments = {"source": str(root / "표.csv"), "target_format": "json"}
    return ModelTurn(None, [ToolCall("c", CONVERT_TOOL, json.dumps(arguments))])


def planner_for(config, model, journal=None):
    gateway = AsyncMock()
    gateway.list_tools.return_value = []
    return Planner(config, gateway, model, undo_journal=journal), gateway


async def test_planner_converts_after_approval(config, tmp_path):
    root = config.allowed_directories[0]
    (root / "표.csv").write_text("a\n1\n")
    model = AsyncMock()
    model.generate.side_effect = [convert_turn(root), ModelTurn("변환했습니다.", [])]
    journal = UndoJournal(tmp_path / "undo.sqlite3", config.allowed_directories)
    planner, gateway = planner_for(config, model, journal)
    pending = await planner.run("표.csv를 json으로 변환해줘")
    offered = {tool.name for tool in model.generate.await_args_list[0].args[1]}
    assert {CONVERT_TOOL, READ_DOCUMENT_TOOL} <= offered
    assert isinstance(pending, ApprovalRequired) and pending.operation == "convert"
    assert not (root / "표.json").exists()
    assert await planner.approve(pending.context) == Completed("변환했습니다.")
    assert (root / "표.json").exists()
    gateway.call_tool.assert_not_called()  # a local tool, no MCP call


async def test_planner_reject_reports_conversion(config):
    root = config.allowed_directories[0]
    (root / "표.csv").write_text("a\n1\n")
    model = AsyncMock()
    model.generate.side_effect = [convert_turn(root)]
    planner, _ = planner_for(config, model)
    pending = await planner.run("표.csv를 json으로 변환해줘")
    assert planner.reject(pending.context) == Completed(
        "파일 변환을 거절했습니다. 파일은 변경하지 않았습니다."
    )
    assert not (root / "표.json").exists()
