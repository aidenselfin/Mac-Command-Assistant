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
WALK_SECONDS = 10.0
EXCLUDED_DIRS = frozenset({"node_modules", "__pycache__", "venv"})
TEXT_EXTENSIONS = frozenset({"txt", "md", "csv", "tsv", "json", "rst", "log", "yaml", "yml"})
# Documents whose text this search does not look into (only their name, folder and dates).
DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx", "doc", "rtf", "odt", "html", "htm"})
SEPARATORS = re.compile(r"[-_.\s]+")
DIGITS = re.compile(r"\d+")
FILTER_KEYS = frozenset(
    {"extensions", "folder_hint", "modified_after", "modified_before", "content_keywords"}
)

FIND_FILES_DEFINITION = ToolDefinition(
    FIND_FILES_TOOL,
    "파일명이 불분명할 때 허용된 폴더 안에서 파일을 찾는다. 파일명 → 폴더 경로 → 텍스트 내용 "
    "순서로 앱이 알아서 넓혀 가며 찾으므로 keywords에 파일명·주제어와 한/영 변형(예: 회의록, "
    "meeting, minutes)을 넣는다. 각 keyword는 대체어이며 하나만 맞아도 된다. "
    "extensions·folder_hint·modified_*는 사용자가 형식·폴더·날짜를 말했을 때만 쓴다. "
    "folder_hint는 폴더 이름이지 절대경로가 아니다. content_keywords는 파일 이름이 아니라 "
    "본문에 들어 있어야 할 말(예: 예산 승인)로, 이름·폴더로 찾은 후보 안에서만 본문을 확인해 "
    "좁힌다. 앱은 사용자가 지정한 조건을 스스로 풀지 않는다. 조건 때문에 결과가 없으면 "
    "사용자가 말한 조건은 그대로 두고 알리고, 추측해 넣은 조건만 빼고 다시 호출할 수 있다. "
    "결과의 decision이 single이면 그 파일을 쓰고, choose·narrow는 앱이 사용자에게 직접 "
    "묻는다. complete가 false면 limits에 적힌 검사하지 못한 범위를 사용자에게 함께 알린다.",
    {
        "type": "object",
        # No size limits here: a provider checks a generated call against this schema before we
        # ever see it and answers HTTP 400 for "9 items, want 8". The limits are applied in code.
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}},
            "extensions": {"type": ["array", "null"], "items": {"type": "string"}},
            "content_keywords": {"type": ["array", "null"], "items": {"type": "string"}},
            "folder_hint": {"type": ["string", "null"]},
            "modified_after": {"type": ["string", "null"]},
            "modified_before": {"type": ["string", "null"]},
        },
        "required": ["keywords"],
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


def _list_dir(root: Path, folder: tuple[str, ...], limit: int) -> tuple[list[Row], bool]:
    """(rows, more): name, is_dir, is_file and (mtime, size) of at most `limit` children,
    read through a held descriptor.

    A DirEntry from an fd-based scan can only be stat-ed while the descriptor is open, so
    everything is read before it is closed. A folder with more entries than the limit is
    never listed whole in memory.
    """
    descriptor = _open_below(root, folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rows, more = [], False
        with os.scandir(descriptor) as scanner:
            for index, child in enumerate(scanner):
                if index >= limit:
                    more = True
                    break
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
        return sorted(rows), more
    finally:
        os.close(descriptor)


def _walk(roots: tuple[Path, ...]) -> tuple[list[Entry], bool]:
    entries, visited, truncated = [], 0, False
    started = time.monotonic()
    stack = [(root, ()) for root in reversed(roots)]
    while stack:
        if time.monotonic() - started > WALK_SECONDS:
            return entries, True
        root, folder = stack.pop()
        try:
            rows, more = _list_dir(root, folder, MAX_VISITED - visited + 1)
        except OSError:
            continue
        truncated = truncated or more
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
    extensions = {
        item.lower().lstrip(".") for item in (arguments.get("extensions") or [])[:20] if item
    }
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
        and (hint is None or hint in _fold("/".join((entry.root.name, *entry.folder))))
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
) -> tuple[str, list[tuple[Entry, int]], dict]:
    """(stage, ranked matches, coverage of the content scan; empty before that stage)."""
    by_name = []
    for entry in entries:
        name = _fold(entry.name)  # with the extension: 'old.log' must find old.log
        score = sum(bool(pattern.search(name)) for pattern in patterns)
        if score:
            by_name.append((entry, score))
    if by_name:
        return "name", by_name, {}
    by_path = []
    for entry in entries:
        folder = _fold("/".join(entry.folder))
        score = sum(bool(pattern.search(folder)) for pattern in patterns)
        if score:
            by_path.append((entry, score))
    if by_path:
        return "path", by_path, {}
    # What the content scan could not look at, so "nothing found" is never over-claimed.
    coverage = {
        "cut": False,
        "head_only": 0,  # text files longer than the part that is read
        "unreadable": 0,
        "documents": sum(entry.extension in DOCUMENT_EXTENSIONS for entry in entries),
    }
    started, scanned, by_content = time.monotonic(), 0, []
    for entry in entries:
        if entry.extension not in TEXT_EXTENSIONS or entry.size == 0:
            continue
        if scanned >= CONTENT_MAX_FILES or time.monotonic() - started > CONTENT_SECONDS:
            coverage["cut"] = True
            break
        scanned += 1
        try:
            text = _fold(_read_head(entry))
        except OSError:
            coverage["unreadable"] += 1
            continue
        coverage["head_only"] += entry.size > CONTENT_BYTES
        score = sum(len(pattern.findall(text)) for pattern in patterns)
        if score:
            by_content.append((entry, score))
    return ("content" if by_content else "none"), by_content, coverage


def _limits(walk_cut: bool, coverage: dict) -> list[str]:
    """Why a result may not cover everything, in words for the model and the user."""
    reasons = []
    if walk_cut:
        reasons.append("폴더가 크거나 깊어 일부 폴더·파일을 건너뛰었습니다")
    if coverage.get("cut"):
        reasons.append("내용 검사 파일 수·시간 한도에서 멈췄습니다")
    if coverage.get("head_only"):
        kib = CONTENT_BYTES // 1024
        reasons.append(f"긴 텍스트 파일 {coverage['head_only']}개는 앞 {kib} KiB만 검사했습니다")
    if coverage.get("documents"):
        reasons.append(
            f"pdf·docx 등 문서 {coverage['documents']}개는 내용을 검사하지 않았습니다(이름·폴더만)"
        )
    if coverage.get("unreadable"):
        reasons.append(f"읽지 못한 파일이 {coverage['unreadable']}개 있습니다")
    return reasons


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
                facets.append((f"{kind}: {value}", f"{kind}: {value} ({count}개)"))
    return facets[:MAX_FACETS]


def _narrow_by_content(
    ranked: list[Entry], words: list[re.Pattern], coverage: dict
) -> tuple[list[Entry], dict]:
    """Keep the candidates whose beginning mentions one of the words; best match first.

    Only text files can be checked. A candidate that cannot be (a pdf, an unreadable file) is
    left out and counted, so the answer says what it could not look into.
    """
    coverage = {"cut": False, "head_only": 0, "unreadable": 0, "documents": 0, **coverage}
    started, scanned, kept = time.monotonic(), 0, []
    for entry in ranked:
        if entry.extension not in TEXT_EXTENSIONS:
            coverage["documents"] += entry.extension in DOCUMENT_EXTENSIONS
            continue
        if scanned >= CONTENT_MAX_FILES or time.monotonic() - started > CONTENT_SECONDS:
            coverage["cut"] = True
            break
        scanned += 1
        try:
            text = _fold(_read_head(entry))
        except OSError:
            coverage["unreadable"] += 1
            continue
        coverage["head_only"] += entry.size > CONTENT_BYTES
        score = sum(len(pattern.findall(text)) for pattern in words)
        if score:
            kept.append((entry, score))
    kept.sort(key=lambda item: (-item[1], -item[0].mtime, item[0].name))
    return [entry for entry, _ in kept], coverage


def find_files(arguments: dict, roots: tuple[Path, ...]) -> ToolResult:
    _validate(arguments)
    keywords = list(
        dict.fromkeys(_fold(item[:100]) for item in arguments["keywords"] if _fold(item[:100]))
    )[:MAX_KEYWORDS]
    if not keywords:
        raise ToolRejectedError("검색어가 비어 있습니다.")
    patterns = [_keyword_pattern(item) for item in keywords]
    # Roots come canonical from the config; resolving here would follow a link swapped in later.
    entries, walk_cut = _walk(roots)
    scope = _filter(entries, arguments, roots)
    stage, matches, coverage = _search(scope, patterns)
    filtered = any(arguments.get(key) for key in FILTER_KEYS)
    # Best match first; newer files break ties.
    ranked = [entry for entry, _ in sorted(matches, key=lambda m: (-m[1], -m[0].mtime, m[0].name))]
    content_words = [
        _keyword_pattern(word)
        for word in dict.fromkeys(
            _fold(item[:100]) for item in (arguments.get("content_keywords") or [])[:MAX_KEYWORDS]
        )
        if word
    ]
    if content_words and ranked:
        ranked, coverage = _narrow_by_content(ranked, content_words, coverage)
        stage = f"{stage}+content"
    count = len(ranked)
    reasons = _limits(walk_cut, coverage)
    decision = (
        "none" if count == 0 else "single" if count == 1
        else "choose" if count <= MAX_SHOW else "narrow"
    )
    if decision == "single" and reasons:
        # One hit among files that were not fully looked at is not "the only one": confirm it.
        decision = "choose"
    response: dict = {
        "stage": stage, "decision": decision, "total": count,
        "truncated": walk_cut or coverage.get("cut", False),
        "complete": not reasons,
    }
    notes = []
    if reasons:
        response["limits"] = reasons  # for the app's own wording; the note is for the model
        notes.append("검사 범위의 한계: " + "; ".join(reasons) + ".")
        if decision == "none":
            notes.append("없다고 단정하지 말고, 파일명·폴더 같은 단서를 사용자에게 물으세요.")
    if filtered and decision == "none":
        # Never widen on our own: a condition the user named must not be dropped silently.
        notes.append(
            "형식·폴더·날짜 조건을 적용했을 때 결과가 없습니다. 사용자가 말한 조건이면 풀지 말고 "
            "그대로 알리세요. 사용자가 말하지 않은 추측 조건이면 빼고 다시 호출하세요."
        )
    if notes:
        response["note"] = " ".join(notes)
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


LIST_FILES_TOOL = "local__list_files"
LIST_MAX = 50
LIST_DEFINITION = ToolDefinition(
    LIST_FILES_TOOL,
    "허용된 폴더 안 파일을 크기·수정일과 함께 목록으로 본다(하위 폴더 포함). 가장 큰 파일, "
    "최근에 바뀐 파일, 며칠 안/며칠 넘은 파일, 형식별 목록, 폴더별 용량처럼 크기·날짜로 "
    "고르거나 정렬하는 요청에 쓴다. sort_by는 size·modified·name(기본 modified, 큰 것·최근 "
    "것 먼저, ascending이 true면 반대). name_contains는 파일 이름에 들어갈 말(하나만 맞아도 "
    "됨)이다. paths는 이미 찾은 후보 경로만 볼 때 쓴다. "
    "modified_within_days=N은 최근 N일 안에 바뀐 파일, "
    "not_modified_for_days=N은 N일 넘게 안 바뀐 파일이다. group_by가 folder면 최상위 "
    "폴더별 합계 용량을 큰 순서로 준다. 결과는 사용자에게 보이지 않으므로 답에 이름을 "
    "결과 순서대로 직접 적고, 바이트 크기는 size_bytes를 쓴다. 안 쓰는 인자는 생략한다.",
    {
        "type": "object",
        "properties": {
            "sort_by": {"type": ["string", "null"], "enum": ["size", "modified", "name", None]},
            "ascending": {"type": ["boolean", "null"]},
            "extensions": {"type": ["array", "null"], "items": {"type": "string"}},
            "folder_hint": {"type": ["string", "null"]},
            "name_contains": {"type": ["array", "null"], "items": {"type": "string"}},
            "paths": {"type": ["array", "null"], "items": {"type": "string"}},
            "modified_within_days": {"type": ["number", "null"]},
            "not_modified_for_days": {"type": ["number", "null"]},
            "group_by": {"type": ["string", "null"], "enum": ["folder", None]},
            "limit": {"type": ["integer", "null"]},
        },
    },
)


def list_files(arguments: dict, roots: tuple[Path, ...]) -> ToolResult:
    try:
        Draft202012Validator(LIST_DEFINITION.schema).validate(arguments)
    except ValidationError as exc:
        raise ToolRejectedError(f"잘못된 {LIST_FILES_TOOL} 인자입니다.") from exc
    within = arguments.get("modified_within_days")
    idle = arguments.get("not_modified_for_days")
    if any(days is not None and days < 0 for days in (within, idle)):
        raise ToolRejectedError("일 수는 0 이상이어야 합니다.")
    entries, truncated = _walk(roots)
    entries = _filter(entries, arguments, roots)
    words = [_fold(word[:100]) for word in (arguments.get("name_contains") or [])[:MAX_KEYWORDS]]
    words = [word for word in words if word]
    if len(arguments.get("paths") or []) > LIST_MAX:
        raise ToolRejectedError(f"paths는 {LIST_MAX}개까지입니다.")
    paths = {str(Path(path)) for path in arguments.get("paths") or []}
    now = time.time()
    entries = [
        entry for entry in entries
        if (not paths or str(entry.path) in paths)
        and (not words or any(word in _fold(entry.name) for word in words))
        and (within is None or entry.mtime >= now - within * 86400)
        and (idle is None or entry.mtime < now - idle * 86400)
    ]
    limit = max(1, min(arguments.get("limit") or 20, LIST_MAX))
    descending = not arguments.get("ascending")
    response: dict = {"total": len(entries), "truncated": truncated}
    if arguments.get("group_by") == "folder":
        totals: dict[str, list[int]] = {}
        for entry in entries:
            if entry.folder:
                # Two allowed roots may each hold a folder of the same name.
                label = entry.folder[0]
                if len(roots) > 1:  # roots may share a name too, so the full path
                    label = str(entry.root / label)
                total = totals.setdefault(label, [0, 0])
                total[0] += entry.size
                total[1] += 1
        folders = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
        if not descending:
            folders.reverse()
        response["folders"] = [
            {"folder": name, "size_bytes": size, "size": _size(size), "files": count}
            for name, (size, count) in folders[:limit]
        ]
    else:
        key = arguments.get("sort_by") or "modified"
        ordered = sorted(
            entries,
            key=lambda entry: entry.name if key == "name" else getattr(
                entry, "size" if key == "size" else "mtime"
            ),
            reverse=descending if key != "name" else not descending,
        )
        response["files"] = [
            {
                "name": entry.name, "path": str(entry.path), "size_bytes": entry.size,
                "size": _size(entry.size),
                "modified": datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M"),
            }
            for entry in ordered[:limit]
        ]
        response["shown"] = len(response["files"])
    return ToolResult(
        True, json.dumps(response, ensure_ascii=False), structured=response, truncated=truncated
    )


GROUP_FILES_TOOL = "local__group_files"
GROUP_SHOW = 20  # paths per group handed to the model; the count says how many there are
MAX_GROUPS = 8
KINDS = {
    "이미지": {"jpg", "jpeg", "png", "gif", "heic", "webp", "bmp", "tiff", "svg"},
    "문서": {"pdf", "doc", "docx", "hwp", "hwpx", "txt", "md", "rtf", "odt", "pages",
             "ppt", "pptx", "key"},
    "표": {"xls", "xlsx", "csv", "tsv", "numbers"},
    "압축": {"zip", "rar", "7z", "tar", "gz", "dmg"},
    "동영상": {"mp4", "mov", "avi", "mkv"},
    "음악": {"mp3", "wav", "m4a", "aac", "flac"},
}
GROUP_DEFINITION = ToolDefinition(
    GROUP_FILES_TOOL,
    "허용된 폴더 안 파일을 비슷한 것끼리 묶어 보여준다(읽기 전용, 파일 내용은 보지 않음). "
    "keywords를 주면 이름에 그 말이 들어간 파일을 말마다 한 묶음으로, 없으면 by에 따라 "
    "name(이름 앞부분이 같은 파일)·kind(이미지·문서·표 등 종류) 묶음을 준다(기본 둘 다). "
    "extensions·folder_hint는 사용자가 형식·폴더를 말했을 때만 쓴다. 이미 같은 이름 폴더에 "
    "든 파일은 빠진다. 결과의 folder는 옮길 폴더 이름 제안이다. 결과는 사용자에게 "
    "보이지 않으니 묶음 이름과 파일 수·예시 파일을 답에 적는다. 사용자가 옮기라고 "
    "했을 때만 local__create_directory 후 파일마다 local__move_file로 옮기고, 옮기라는 "
    "말이 없으면 묶음만 알리고 옮길지 물어본다. 직전에 보여준 묶음을 옮기라는 요청이면 "
    "같은 인자로 다시 불러 경로를 얻는다.",
    {
        "type": "object",
        "properties": {
            "keywords": {"type": ["array", "null"], "items": {"type": "string"}},
            "by": {"type": ["string", "null"], "enum": ["name", "kind", None]},
            "extensions": {"type": ["array", "null"], "items": {"type": "string"}},
            "folder_hint": {"type": ["string", "null"]},
        },
    },
)


def _name_key(entry: Entry) -> str | None:
    """First word of the name that is not a number: IMG_0001 -> img, 보고서_최종 -> 보고서.

    ponytail: first-word heuristic; 'final report' and 'final draft' land together.
    """
    for word in _fold(entry.path.stem).split():
        word = DIGITS.sub("", word)
        if len(word) >= 2:
            return word
    return None


def group_files(arguments: dict, roots: tuple[Path, ...]) -> ToolResult:
    try:
        Draft202012Validator(GROUP_DEFINITION.schema).validate(arguments)
    except ValidationError as exc:
        raise ToolRejectedError(f"잘못된 {GROUP_FILES_TOOL} 인자입니다.") from exc
    entries, truncated = _walk(roots)
    entries = _filter(entries, arguments, roots)
    keywords = list(dict.fromkeys(
        word for word in (_fold(item[:100]) for item in (arguments.get("keywords") or []))
        if word
    ))[:MAX_KEYWORDS]
    groups: list[tuple[str, dict[str, list[Entry]]]] = []
    if keywords:
        patterns = [(word, _keyword_pattern(word)) for word in keywords]
        groups.append(("keyword", {
            word: [entry for entry in entries if pattern.search(_fold(entry.name))]
            for word, pattern in patterns
        }))
    else:
        by = arguments.get("by")
        if by in (None, "name"):
            named: dict[str, list[Entry]] = {}
            for entry in entries:
                if key := _name_key(entry):
                    named.setdefault(key, []).append(entry)
            groups.append(("name", {k: v for k, v in named.items() if len(v) >= 2}))
        if by in (None, "kind"):
            kinds: dict[str, list[Entry]] = {}
            for entry in entries:
                for kind, extensions in KINDS.items():
                    if entry.extension in extensions:
                        kinds.setdefault(kind, []).append(entry)
            groups.append(("kind", kinds))
    found = []
    for by, members in groups:
        for label, files in members.items():
            # A file already in a folder of the group's name is organized already.
            files = sorted(
                (entry for entry in files if not entry.folder or _fold(entry.folder[-1]) != label),
                key=lambda entry: (_folder_label(entry), entry.name),
            )
            if files:
                found.append({
                    "by": by, "label": label, "folder": label, "count": len(files),
                    "files": [str(entry.path) for entry in files[:GROUP_SHOW]],
                })
    found.sort(key=lambda item: -item["count"])
    response = {"groups": found[:MAX_GROUPS], "more_groups": max(0, len(found) - MAX_GROUPS),
                "truncated": truncated}
    return ToolResult(
        True, json.dumps(response, ensure_ascii=False), structured=response, truncated=truncated
    )


def clarification_spec(response: dict) -> tuple[str, str, tuple[str, ...], tuple[str, ...]] | None:
    """(question, kind, options, labels) when the user must pick or narrow; else None."""
    decision = response.get("decision")
    if decision == "choose":
        items = response["candidates"]
        partial = " (일부만 검사한 결과예요)" if response.get("complete") is False else ""
        return (
            f"어떤 파일인가요? 하나를 선택해 주세요.{partial}", "file_candidate",
            tuple(item["path"] for item in items), tuple(item["label"] for item in items),
        )
    if decision != "narrow":
        return None
    options, labels = [], []
    series = response.get("series")
    partial = (
        " (일부만 검사한 결과라 실제 후보는 더 많을 수 있어요)"
        if response.get("complete") is False else ""
    )
    if series:
        options.append(series["newest"])
        labels.append(series["newest_label"])
        head = (
            f"번호만 다른 파일이 {series['count']}개 있어요 ({series['range']}). "
            f"원하는 번호나 날짜를 입력하거나 버튼을 눌러 주세요.{partial}"
        )
    else:
        head = (
            f"후보가 {response['total']}개로 많아요. 폴더, 날짜, 형식, 파일 내용의 단서를 "
            f"입력하거나 버튼으로 범위를 좁혀 주세요.{partial}"
        )
    for item in response.get("narrow_options", ()):
        options.append(item["option"])
        labels.append(item["label"])
    return head, "narrow_search", tuple(options), tuple(labels)
