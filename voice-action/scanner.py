import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MAX_FILES = 500
EXCLUDE_DIRS = {".git", "node_modules", ".Trash", "__pycache__", ".openspec"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass
class SnapCache:
    snap_text: str
    file_mtimes: dict = field(default_factory=dict)  # {str(path): mtime float}


_cache: dict[str, SnapCache] = {}


def _sha256_prefix(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            chunk = f.read(65536)
            while chunk:
                h.update(chunk)
                chunk = f.read(65536)
    except OSError:
        return "00000000"
    return h.hexdigest()[:8]


def _resolve_dst_conflict(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def scan_directory(root: Path, include_hidden: bool = False) -> str:
    entries = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if p.name in EXCLUDE_FILES:
                continue
            if not include_hidden and p.name.startswith("."):
                continue
            try:
                stat = p.stat()
                entries.append((p, stat.st_size, stat.st_mtime))
            except OSError:
                continue
    except PermissionError:
        pass

    entries.sort(key=lambda x: x[2], reverse=True)
    truncated_total = len(entries)
    entries = entries[:MAX_FILES]

    lines = [
        f"root: {root}",
        f"scanned_at: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
        f"files: {truncated_total}",
        "",
        "path,size,mtime,sha8",
    ]
    for p, size, mtime in entries:
        rel = p.relative_to(root)
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        sha8 = _sha256_prefix(p)
        lines.append(f"{rel},{size},{mtime_str},{sha8}")

    return "\n".join(lines)


def get_snap(root: Path, include_hidden: bool = False) -> str:
    key = str(root)
    try:
        current_mtimes: dict[str, float] = {}
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if p.name in EXCLUDE_FILES:
                continue
            if not include_hidden and p.name.startswith("."):
                continue
            try:
                current_mtimes[str(p)] = p.stat().st_mtime
            except OSError:
                pass
    except PermissionError:
        current_mtimes = {}

    cached = _cache.get(key)
    if cached and cached.file_mtimes == current_mtimes:
        return cached.snap_text

    snap = scan_directory(root, include_hidden)
    _cache[key] = SnapCache(snap_text=snap, file_mtimes=current_mtimes)
    return snap
