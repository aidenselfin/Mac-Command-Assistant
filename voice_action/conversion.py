"""File conversion (approved write) and document reading (read-only) as local tools.

Converters are fixed macOS tools and the standard library; the model only names the source
file and the target format. Conversions run in a temporary folder before approval, so the
approval shows the real result, and the output is always a new file next to the source.
"""

import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult

CONVERT_TOOL = "local__convert_file"
READ_DOCUMENT_TOOL = "local__read_document"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
TIMEOUT_SECONDS = 60
PREVIEW_CHARS = 200

TEXTUTIL, SIPS, CUPSFILTER = "/usr/bin/textutil", "/usr/bin/sips", "/usr/sbin/cupsfilter"
# Extension aliases -> canonical format name.
ALIASES = {"jpg": "jpeg", "htm": "html", "tif": "tiff", "heif": "heic", "markdown": "md"}
TEXT_DOCUMENTS = {"txt", "md"}
TEXTUTIL_INPUT = {"txt", "md", "rtf", "html", "doc", "docx", "odt"}
TEXTUTIL_OUTPUT = {"txt", "rtf", "html", "doc", "docx", "odt"}
IMAGES = {"png", "jpeg", "heic", "tiff", "gif", "bmp"}
READABLE_DOCUMENTS = {"pdf", "rtf", "html", "doc", "docx", "odt"}
TEXT_OUTPUTS = {"txt", "rtf", "html", "csv", "json"}
EXTENSION = {"jpeg": ".jpg"}  # the extension written for a format, default ".<format>"
TARGET_FORMATS = sorted(TEXTUTIL_OUTPUT | IMAGES | {"pdf", "csv", "json"})
# Leading bytes for formats that have them; a mismatch with the extension is refused.
SIGNATURES = {
    "pdf": (b"%PDF",),
    "png": (b"\x89PNG",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF8",),
    "bmp": (b"BM",),
    "tiff": (b"II*\x00", b"MM\x00*"),
    "docx": (b"PK\x03\x04",),
    "odt": (b"PK\x03\x04",),
    "doc": (b"\xd0\xcf\x11\xe0",),
    "rtf": (b"{\\rtf",),
}

CONVERT_DEFINITION = ToolDefinition(
    CONVERT_TOOL,
    "파일 형식 변환. 원본은 그대로 두고 같은 폴더에 새 파일을 만든다(덮어쓰지 않음). "
    "문서 txt·md·rtf·html·doc·docx·odt 사이 변환, txt·md→pdf(서식 없는 글), pdf→txt, "
    "이미지 png·jpeg(jpg)·heic·tiff·gif·bmp 사이 변환(아이폰 HEIC→JPG·PNG 포함), "
    "csv↔json. 사용자 승인 후 실행된다.",
    {
        "type": "object",
        "properties": {
            "source": {"type": "string", "minLength": 1},
            "target_format": {"type": "string", "enum": TARGET_FORMATS},
            "output": {"type": "string", "minLength": 1},
        },
        "required": ["source", "target_format"],
        "additionalProperties": False,
    },
)
READ_DOCUMENT_DEFINITION = ToolDefinition(
    READ_DOCUMENT_TOOL,
    "pdf·docx·doc·rtf·odt·html 문서의 글자를 읽어 텍스트로 돌려준다. 파일을 만들거나 "
    "바꾸지 않는다. 일반 텍스트 파일(txt, md, csv)은 filesystem__read_text_file을 쓴다.",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class PreparedConversion:
    source: str
    output: str
    target_format: str
    source_state: str = field(repr=False)
    data: bytes = field(repr=False)

    @property
    def preview(self) -> str:
        if self.target_format not in TEXT_OUTPUTS:
            return ""
        return self.data.decode("utf-8", errors="replace")[:PREVIEW_CHARS]

    @property
    def preview_truncated(self) -> bool:
        return self.target_format in TEXT_OUTPUTS and len(self.data) > PREVIEW_CHARS


def format_of(path: Path) -> str:
    extension = path.suffix.lower().lstrip(".")
    return ALIASES.get(extension, extension)


def _validate(definition: ToolDefinition, arguments: dict) -> None:
    try:
        Draft202012Validator(definition.schema).validate(arguments)
    except ValidationError as exc:
        raise ToolRejectedError(f"잘못된 {definition.name} 인자입니다.") from exc


def _inside(raw: object, roots: tuple[Path, ...], *, exists: bool) -> Path:
    """An absolute, already-canonical path under an allowed root, never through a symlink."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ToolRejectedError("파일 경로가 올바르지 않습니다.")
    path = Path(raw)
    try:
        escapes = (
            not path.is_absolute()
            or not any(path.is_relative_to(root) for root in roots)
            or path.is_symlink()
            or (exists and path.resolve(strict=True) != path)
            or (not exists and path.parent.resolve(strict=True) != path.parent)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolRejectedError("파일 경로를 확인할 수 없습니다.") from exc
    if escapes:
        raise ToolRejectedError("허용 폴더 밖이거나 링크를 거치는 경로입니다.")
    return path


def _read_source(path: Path) -> tuple[bytes, str]:
    """Bytes of a regular file and its identity (device, inode, content hash)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ToolRejectedError("원본 파일을 열 수 없습니다.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolRejectedError("일반 파일만 변환할 수 있습니다.")
        if metadata.st_size > MAX_SOURCE_BYTES:
            raise ToolRejectedError("원본 파일은 최대 20 MiB까지 변환할 수 있습니다.")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    state = f"{metadata.st_dev}:{metadata.st_ino}:{hashlib.sha256(data).hexdigest()}"
    return data, state


def _check_signature(data: bytes, fmt: str) -> None:
    if fmt == "heic":
        if data[4:8] != b"ftyp" or data[8:12] not in {b"heic", b"heix", b"mif1", b"msf1"}:
            raise ToolRejectedError("확장자와 실제 파일 형식이 다릅니다.")
        return
    signatures = SIGNATURES.get(fmt)
    if signatures and not data.startswith(signatures):
        raise ToolRejectedError("확장자와 실제 파일 형식이 다릅니다.")


def _run(command: list[str]) -> bytes:
    """Fixed converter binaries only, no shell, bounded time."""
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolRejectedError("변환 시간이 60초를 넘어 중단했습니다.") from exc
    except OSError as exc:
        raise ToolRejectedError("이 컴퓨터에서 변환 도구를 실행할 수 없습니다.") from exc
    if completed.returncode != 0:
        raise ToolRejectedError("변환에 실패했습니다.")
    return completed.stdout


def _pdf_text(path: Path) -> str:
    try:
        from Foundation import NSURL
        from Quartz import PDFDocument
    except ImportError as exc:
        raise ToolRejectedError("이 컴퓨터에서 PDF를 읽을 수 없습니다.") from exc
    document = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
    if document is None:
        raise ToolRejectedError("PDF를 열 수 없습니다. 손상되었거나 암호가 걸려 있습니다.")
    return str(document.string() or "")


def _csv_to_json(data: bytes) -> bytes:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")


def _json_to_csv(data: bytes) -> bytes:
    try:
        rows = json.loads(data.decode("utf-8-sig"))
    except ValueError as exc:
        raise ToolRejectedError("JSON을 읽을 수 없습니다.") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ToolRejectedError("CSV로 바꿀 JSON은 객체들의 배열이어야 합니다.")
    if any(isinstance(v, dict | list) for row in rows for v in row.values()):
        raise ToolRejectedError("중첩된 값이 있는 JSON은 CSV로 바꿀 수 없습니다.")
    header = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def supported(source_format: str, target_format: str) -> bool:
    if source_format == target_format:
        return False
    if {source_format, target_format} == {"csv", "json"}:
        return True
    if sys.platform != "darwin":  # every other converter is a macOS tool
        return False
    return (
        (source_format in TEXTUTIL_INPUT and target_format in TEXTUTIL_OUTPUT)
        or (source_format in TEXT_DOCUMENTS and target_format == "pdf")
        or (source_format == "pdf" and target_format == "txt")
        or (source_format in IMAGES and target_format in IMAGES)
    )


def convert(source: Path, data: bytes, source_format: str, target_format: str) -> bytes:
    """Convert in a private temporary folder and return the output bytes."""
    if source_format == "csv" and target_format == "json":
        return _csv_to_json(data)
    if source_format == "json" and target_format == "csv":
        return _json_to_csv(data)
    with tempfile.TemporaryDirectory(prefix="voice-action-convert-") as folder:
        # Work on a private copy, so a source swapped mid-run can't change the input.
        copy = Path(folder) / f"source.{source_format}"
        copy.write_bytes(data)
        out = Path(folder) / f"output{EXTENSION.get(target_format, '.' + target_format)}"
        if source_format == "pdf":
            output = _pdf_text(copy).encode("utf-8")
        elif target_format == "pdf":
            output = _run([CUPSFILTER, "-m", "application/pdf", "-i", "text/plain", str(copy)])
        elif source_format in IMAGES:
            _run([SIPS, "-s", "format", target_format, str(copy), "--out", str(out)])
            output = out.read_bytes()
        else:
            textutil_format = ["-format", "txt"] if source_format == "md" else []
            _run([TEXTUTIL, *textutil_format, "-convert", target_format, "-output", str(out),
                  str(copy)])
            output = out.read_bytes()
    if not output:
        raise ToolRejectedError("변환 결과가 비어 있습니다.")
    if len(output) > MAX_OUTPUT_BYTES:
        raise ToolRejectedError("변환 결과가 50 MiB를 넘습니다.")
    return output


def default_output(source: Path, target_format: str) -> Path:
    """Same folder and name, new extension; never an existing file ("이름 (1).pdf")."""
    extension = EXTENSION.get(target_format, "." + target_format)
    candidate = source.with_suffix(extension)
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = source.with_name(f"{source.stem} ({index}){extension}")
        index += 1
    return candidate


def prepare_conversion(arguments: dict, roots: tuple[Path, ...]) -> PreparedConversion:
    _validate(CONVERT_DEFINITION, arguments)
    source = _inside(arguments["source"], roots, exists=True)
    source_format, target_format = format_of(source), ALIASES.get(
        arguments["target_format"], arguments["target_format"]
    )
    if not supported(source_format, target_format):
        raise ToolRejectedError(
            f"지원하지 않는 변환입니다: {source_format or '확장자 없음'} → {target_format}"
        )
    if "output" in arguments:
        output = _inside(arguments["output"], roots, exists=False)
        if output.exists() or output.is_symlink():
            raise ToolRejectedError("출력 파일이 이미 있습니다. 덮어쓰지 않습니다.")
        if format_of(output) != target_format:
            raise ToolRejectedError("출력 파일 확장자가 변환 형식과 다릅니다.")
    else:
        output = default_output(source, target_format)
        _inside(str(output), roots, exists=False)
    data, state = _read_source(source)
    _check_signature(data, source_format)
    output_bytes = convert(source, data, source_format, target_format)
    return PreparedConversion(str(source), str(output), target_format, state, output_bytes)


def changed_since(source: str, state: str, output: str) -> bool:
    """True if the source is no longer the approved file or the output name got taken."""
    try:
        current = _read_source(Path(source))[1]
    except ToolRejectedError:
        return True
    target = Path(output)
    return current != state or target.exists() or target.is_symlink()


def write_new_file(raw: str, data: bytes) -> None:
    """Create the output only if nothing is there yet, durably."""
    path = Path(raw)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise ToolRejectedError(
            "출력 파일이 승인 대기 중에 생겼습니다. 덮어쓰지 않습니다."
        ) from exc
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_document(arguments: dict, roots: tuple[Path, ...], limit: int) -> ToolResult:
    _validate(READ_DOCUMENT_DEFINITION, arguments)
    path = _inside(arguments["path"], roots, exists=True)
    fmt = format_of(path)
    if fmt not in READABLE_DOCUMENTS:
        raise ToolRejectedError(
            "읽을 수 있는 문서 형식이 아닙니다. "
            "텍스트 파일은 filesystem__read_text_file을 쓰세요."
        )
    data, _state = _read_source(path)
    _check_signature(data, fmt)
    text = convert(path, data, fmt, "txt").decode("utf-8", errors="replace")
    truncated = len(text) > limit
    return ToolResult(
        True, text[:limit] + ("\n[TRUNCATED]" if truncated else ""), truncated=truncated
    )
