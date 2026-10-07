"""Staged local file search for requests whose file name is unclear.

The model only proposes search words. This module walks the allowed folders itself and widens
the search in fixed steps: file name, then folder path, then the start of text files. It never
follows symlinks and never hands file content to the model; the caller reads a chosen file
through the normal read tools. The result also says whether to proceed, list a few
candidates, or ask the user to narrow a long list.
"""

import json
import os
import re
import stat
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult

FIND_FILES_TOOL = "local__find_files"
MAX_SHOW = 5  # more candidates than this are never listed; the user narrows instead
MAX_KEYWORDS = 6
MAX_FACETS = 3
# Bounds for a folder tree that grows over time; hitting one marks the result truncated.
MAX_DEPTH = 8
MAX_VISITED = 20000
CONTENT_MAX_FILES = 300
CONTENT_BYTES = 64 * 1024
CONTENT_SECONDS = 5.0
EXCLUDED_DIRS = frozenset({"node_modules", "__pycache__", "venv"})
TEXT_EXTENSIONS = frozenset({"txt", "md", "csv", "tsv", "json", "rst", "log", "yaml", "yml"})
SEPARATORS = re.compile(r"[-_.\s]+")
DIGITS = re.compile(r"\d+")
# Models fill unused optional fields with "", and the provider rejects the call before we see it.
DATE_PATTERN = r"^(\d{4}-\d{2}-\d{2})?$"
FILTER_KEYS = frozenset({"extensions", "folder_hint", "modified_after", "modified_before"})

FIND_FILES_DEFINITION = ToolDefinition(
    FIND_FILES_TOOL,
    "파일명이 불분명할 때 허용된 폴더 안에서 파일을 찾는다. 파일명 → 폴더 경로 → 텍스트 내용 "
    "순서로 앱이 알아서 넓혀 가며 찾으므로 keywords에 파일명·주제어와 한/영 변형(예: 회의록, "
    "meeting, minutes)을 넣는다. 각 keyword는 대체어이며 하나만 맞아도 된다. "
    "extensions·folder_hint·modified_*는 사용자가 형식·폴더·날짜를 말했을 때만 쓴다. "
    "folder_hint는 폴더 이름이지 절대경로가 아니다. 조건 때문에 못 찾으면 앱이 조건을 풀어 "
    "다시 찾는다. 결과의 decision이 single이면 그 파일을 쓰고, choose·narrow는 앱이 "
    "사용자에게 직접 묻는다.",
    {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "minItems": 1,
                "maxItems": MAX_KEYWORDS,
            },
            "extensions": {
                "type": ["array", "null"],
                "items": {"type": "string", "maxLength": 10},
                "maxItems": 8,
            },
            "folder_hint": {"type": ["string", "null"], "maxLength": 400},
            "modified_after": {"type": ["string", "null"], "pattern": DATE_PATTERN},
            "modified_before": {"type": ["string", "null"], "pattern": DATE_PATTERN},
        },
        "required": ["keywords"],
        # Stray fields a model invents are ignored, not refused by the provider.
    },
)


@dataclass(frozen=True)
class Entry:
    path: Path
    root: Path
    folder: tuple[str, ...]  # directories below the root, in order
    mtime: float
    size: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def extension(self) -> str:
        return self.path.suffix.lower().lstrip(".")


def _fold(text: str) -> str:
    """Compare form: macOS names are often NFD, and '-', '_', '.' and spaces are one separator."""
    return SEPARATORS.sub(" ", unicodedata.normalize("NFC", text).casefold()).strip()


def _keyword_pattern(keyword: str) -> re.Pattern:
    """Substring match, but a number at either edge must not be part of a longer number."""
    folded = _fold(keyword)
    before = r"(?<!\d)" if folded[:1].isdigit() else ""
    after = r"(?!\d)" if folded[-1:].isdigit() else ""
    return re.compile(before + re.escape(folded) + after)


def _validate(arguments: dict) -> None:
    try:
        Draft202012Validator(FIND_FILES_DEFINITION.schema).validate(arguments)
    except ValidationError as exc:
        raise ToolRejectedError(f"잘못된 {FIND_FILES_TOOL} 인자입니다.") from exc


def _day(value: str | None, *, end: bool) -> float | None:
    """Local midnight starting that day; for `end`, midnight starting the next day (exclusive)."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ToolRejectedError(f"잘못된 날짜입니다: {value}") from exc
    if end:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day).timestamp()


NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_below(root: Path, parts: tuple[str, ...], flags: int) -> int:
    """Open root/parts one element at a time, never through a link.

    Each element is opened relative to the directory already held, so a folder swapped for a
    link after it was listed is refused instead of followed. Raises OSError otherwise.
    """
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            opened = os.open(
                part, (flags if last else os.O_RDONLY | os.O_DIRECTORY) | NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = opened
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


Row = tuple[str, bool, bool, tuple]


def _list_dir(root: Path, folder: tuple[str, ...]) -> tuple[list[Row], bool]:
    """(name, is_dir, is_file, (mtime, size)) of the children, read through a held descriptor.

    A DirEntry from an fd-based scan can only be stat-ed while the descriptor is open, so
    everything is read before it is closed.
    """
    descriptor = _open_below(root, folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rows = []
        with os.scandir(descriptor) as scanner:
            for child in sorted(scanner, key=lambda item: item.name):
                if child.name.startswith(".") or child.is_symlink():
                    rows.append((child.name, False, False, ()))
                    continue
                try:
                    is_dir = child.is_dir(follow_symlinks=False)
                    is_file = not is_dir and child.is_file(follow_symlinks=False)
                    info = child.stat(follow_symlinks=False) if is_file else None
                except OSError:
                    rows.append((child.name, False, False, ()))
                    continue
                rows.append(
                    (child.name, is_dir, is_file, (info.st_mtime, info.st_size) if info else ())
                )
        return rows, False
    finally:
        os.close(descriptor)


def _walk(roots: tuple[Path, ...]) -> tuple[list[Entry], bool]:
    entries, visited, truncated = [], 0, False
    stack = [(root, ()) for root in reversed(roots)]
    while stack:
        root, folder = stack.pop()
        try:
            rows, _ = _list_dir(root, folder)
        except OSError:
            continue
        for name, is_dir, is_file, info in rows:
            visited += 1
            if visited > MAX_VISITED:
                return entries, True
            if is_dir:
                if name in EXCLUDED_DIRS:
                    continue
                if len(folder) >= MAX_DEPTH:
                    truncated = True
                    continue
                stack.append((root, (*folder, name)))
            elif is_file:
                entries.append(Entry(root.joinpath(*folder, name), root, folder, *info))
    return entries, truncated


def _folder_hint(arguments: dict, roots: tuple[Path, ...]) -> str | None:
    """The hint as folder words. A path under an allowed root counts as its folders below."""
    raw = arguments.get("folder_hint")
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        for root in roots:
            if path.is_relative_to(root):
                raw = "/".join(path.relative_to(root).parts)
                break
    return _fold(raw) or None


def _filter(entries: list[Entry], arguments: dict, roots: tuple[Path, ...]) -> list[Entry]:
    extensions = {item.lower().lstrip(".") for item in arguments.get("extensions") or [] if item}
    after = _day(arguments.get("modified_after"), end=False)
    before = _day(arguments.get("modified_before"), end=True)
    if after is not None and before is not None and after >= before:
        raise ToolRejectedError("modified_after가 modified_before보다 늦습니다.")
    hint = _folder_hint(arguments, roots)
    return [
        entry
        for entry in entries
        if (not extensions or entry.extension in extensions)
        and (after is None or entry.mtime >= after)
        and (before is None or entry.mtime < before)
        and (hint is None or hint in _fold("/".join(entry.folder)))
    ]


def _read_head(entry: Entry) -> str:
    descriptor = _open_below(entry.root, (*entry.folder, entry.name), os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ""
        return os.read(descriptor, CONTENT_BYTES).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def _search(
    entries: list[Entry], patterns: list[re.Pattern]
) -> tuple[str, list[tuple[Entry, int]], bool]:
    """(stage, ranked matches, content scan cut short)."""
    by_name = []
    for entry in entries:
        name = _fold(entry.name)  # with the extension: 'old.log' must find old.log
        score = sum(bool(pattern.search(name)) for pattern in patterns)
        if score:
            by_name.append((entry, score))
    if by_name:
        return "name", by_name, False
    by_path = []
    for entry in entries:
        folder = _fold("/".join(entry.folder))
        score = sum(bool(pattern.search(folder)) for pattern in patterns)
        if score:
            by_path.append((entry, score))
    if by_path:
        return "path", by_path, False
    started, scanned, cut, by_content = time.monotonic(), 0, False, []
    for entry in entries:
        if entry.extension not in TEXT_EXTENSIONS or entry.size == 0:
            continue
        if scanned >= CONTENT_MAX_FILES or time.monotonic() - started > CONTENT_SECONDS:
            cut = True
            break
        scanned += 1
        try:
            text = _fold(_read_head(entry))
        except OSError:
            continue
        score = sum(len(pattern.findall(text)) for pattern in patterns)
        if score:
            by_content.append((entry, score))
    return ("content" if by_content else "none"), by_content, cut


def _folder_label(entry: Entry) -> str:
    return "/".join((entry.root.name, *entry.folder))


def _series(entries: list[Entry]) -> dict | None:
    """The largest group of files that differ only by their numbers, if it has several files."""
    groups: dict[tuple, list[Entry]] = {}
    for entry in entries:
        key = (entry.path.parent, entry.extension, DIGITS.sub("#", _fold(entry.path.stem)))
        groups.setdefault(key, []).append(entry)
    key, group = max(groups.items(), key=lambda item: len(item[1]))
    if len(group) < 2 or "#" not in key[2]:
        return None

    def order(entry: Entry) -> list[int]:
        return [int(number) for number in DIGITS.findall(entry.path.stem)]

    ordered = sorted(group, key=order)
    newest = max(group, key=lambda entry: entry.mtime)
    return {
        "count": len(group),
        "range": f"{ordered[0].path.stem} ~ {ordered[-1].path.stem}",
        "newest": str(newest.path),
        "newest_label": f"가장 최근 파일: {newest.name} ({_date(newest)})",
    }


def _date(entry: Entry) -> str:
    return datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d")


def _size(size: int) -> str:
    return f"{size}B" if size < 1024 else f"{size / 1024:.0f}KB" if size < 1 << 20 else (
        f"{size / (1 << 20):.1f}MB"
    )


def _describe(entry: Entry) -> str:
    return f"{entry.name} · {_folder_label(entry)} · {_date(entry)} · {_size(entry.size)}"


def _facets(entries: list[Entry]) -> list[tuple[str, str]]:
    """(option, label): the folders, then extensions, that split the candidates best."""
    facets = []
    for kind, values in (
        ("폴더", [_folder_label(entry) for entry in entries]),
        ("형식", [f".{entry.extension}" for entry in entries if entry.extension]),
    ):
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        # A facet every candidate shares cannot narrow anything.
        for value, count in sorted(counts.items(), key=lambda item: -item[1]):
            if count < len(entries):
                leaf = value.rsplit("/", 1)[-1] if kind == "폴더" else value
                facets.append((f"{kind}: {leaf}", f"{kind}: {value} ({count}개)"))
    return facets[:MAX_FACETS]


def find_files(arguments: dict, roots: tuple[Path, ...]) -> ToolResult:
    _validate(arguments)
    keywords = list(dict.fromkeys(_fold(item) for item in arguments["keywords"] if _fold(item)))
    if not keywords:
        raise ToolRejectedError("검색어가 비어 있습니다.")
    patterns = [_keyword_pattern(item) for item in keywords]
    # Roots come canonical from the config; resolving here would follow a link swapped in later.
    entries, walk_cut = _walk(roots)
    stage, matches, content_cut = _search(_filter(entries, arguments, roots), patterns)
    filtered = any(arguments.get(key) for key in FILTER_KEYS)
    # Best match first; newer files break ties.
    ranked = [entry for entry, _ in sorted(matches, key=lambda m: (-m[1], -m[0].mtime, m[0].name))]
    count = len(ranked)
    decision = (
        "none" if count == 0 else "single" if count == 1
        else "choose" if count <= MAX_SHOW else "narrow"
    )
    response: dict = {
        "stage": stage, "decision": decision, "total": count,
        "truncated": walk_cut or content_cut,
    }
    if response["truncated"]:
        response["note"] = "폴더가 크거나 깊어 일부만 검사했습니다. 결과가 전부가 아닐 수 있습니다."
    if filtered and decision == "none":
        # Never widen on our own: a condition the user named must not be dropped silently.
        response["note"] = response.get("note", "") + (
            " 형식·폴더·날짜 조건을 적용했을 때 결과가 없습니다. 사용자가 말한 조건이면 풀지 말고 "
            "그대로 알리세요. 사용자가 말하지 않은 추측 조건이면 빼고 다시 호출하세요."
        ).strip()
    if decision in {"single", "choose"}:
        response["candidates"] = [
            {"path": str(entry.path), "label": _describe(entry)} for entry in ranked
        ]
    elif decision == "narrow":
        series = _series(ranked)
        if series and series["count"] > MAX_SHOW:
            response["series"] = series
        response["narrow_options"] = [
            {"option": option, "label": label} for option, label in _facets(ranked)
        ]
    return ToolResult(
        True, json.dumps(response, ensure_ascii=False), structured=response,
        truncated=response["truncated"],
    )


def clarification_spec(response: dict) -> tuple[str, str, tuple[str, ...], tuple[str, ...]] | None:
    """(question, kind, options, labels) when the user must pick or narrow; else None."""
    decision = response.get("decision")
    if decision == "choose":
        items = response["candidates"]
        return (
            "어떤 파일인가요? 하나를 선택해 주세요.", "file_candidate",
            tuple(item["path"] for item in items), tuple(item["label"] for item in items),
        )
    if decision != "narrow":
        return None
    options, labels = [], []
    series = response.get("series")
    if series:
        options.append(series["newest"])
        labels.append(series["newest_label"])
        head = (
            f"번호만 다른 파일이 {series['count']}개 있어요 ({series['range']}). "
            "원하는 번호나 날짜를 입력하거나 버튼을 눌러 주세요."
        )
    else:
        head = (
            f"후보가 {response['total']}개로 많아요. 폴더, 날짜, 형식, 파일 내용의 단서를 "
            "입력하거나 버튼으로 범위를 좁혀 주세요."
        )
    for item in response.get("narrow_options", ()):
        options.append(item["option"])
        labels.append(item["label"])
    return head, "narrow_search", tuple(options), tuple(labels)
