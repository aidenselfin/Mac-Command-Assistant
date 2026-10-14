"""Optional meaning-based file search, used only when the name/folder/content search found nothing.

A local embedding model (EmbeddingGemma 2) turns text files and images into vectors; a request
such as "고양이 사진" then finds cat.jpg although no word of it is in the file name. The vectors
only rank files: the caller still asks the user to confirm, and no file content or vector ever
goes to the chat model. Needs the optional `semantic` extra; without it the feature stays off.
"""

import array
import io
import math
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from voice_action import file_search
from voice_action.file_search import DOCUMENT_EXTENSIONS, TEXT_EXTENSIONS, Entry

DEFAULT_DB_PATH = Path(".voice-action/semantic.sqlite3")
MODEL_NAME = "google/embeddinggemma-2"
DIM = 256  # Matryoshka cut: close to full quality at a third of the storage
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"})
INDEXED_EXTENSIONS = TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
MAX_FILES = 2000
MAX_IMAGE_BYTES = 12 * 1024 * 1024
TEXT_CHARS = 2000
# ponytail: fixed cut-offs from a 20-file trial (256d); recalibrate on real folders if they misfire.
# Right answers scored 0.71-0.82 there, unrelated files 0.60-0.66, and a request about nothing in
# the folder topped out at 0.66.
MIN_SCORE = 0.69  # below this nothing is similar enough to offer
MARGIN = 0.07  # nor is a file this far under the best match
REFRESH_SECONDS = 60


class Embedder(Protocol):
    def query(self, text: str) -> list[float]: ...
    def document(self, text: str) -> list[float]: ...
    def image(self, data: bytes) -> list[float]: ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = MODEL_NAME, dim: int = DIM):
        import torch
        from sentence_transformers import SentenceTransformer

        try:  # iPhone photos are HEIC; Pillow reads them only with this plugin
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass
        # float32: the model overflows in float16. No audio encoder, so ~440M parameters.
        self._model = SentenceTransformer(
            model_name, model_kwargs={"torch_dtype": torch.float32},
            config_kwargs={"audio_config": None},
        )
        self._dim = dim
        self._lock = threading.Lock()

    def _encode(self, item) -> list[float]:
        with self._lock:
            vector = self._model.encode(item, truncate_dim=self._dim, normalize_embeddings=True)
        return vector.tolist()

    def query(self, text: str) -> list[float]:
        return self._encode(f"task: search result | query: {text}")

    def document(self, text: str) -> list[float]:
        return self._encode(text)

    def image(self, data: bytes) -> list[float]:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as picture:
            picture = picture.convert("RGB")
        picture.thumbnail((1024, 1024))
        return self._encode({"image": [picture]})


@dataclass(frozen=True)
class SemanticResult:
    state: str  # ready | partial | loading | error
    hits: tuple[tuple[Entry, float], ...] = ()
    done: int = 0
    total: int = 0


def _rel(entry: Entry) -> str:
    return "/".join((*entry.folder, entry.name))


def _read_bytes(entry: Entry, limit: int) -> bytes | None:
    descriptor = file_search._open_below(entry.root, (*entry.folder, entry.name), os.O_RDONLY)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return os.read(descriptor, limit)
    finally:
        os.close(descriptor)


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    vector = array.array("f")
    vector.frombytes(blob)
    return vector


def _dot(a: array.array, b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=False))


class SemanticIndex:
    """Vectors of the files under the allowed roots, kept in a small SQLite file."""

    def __init__(self, roots: tuple[Path, ...], db_path: Path, embedder_factory, model_tag: str):
        self._roots, self._db_path = roots, db_path
        self._factory, self._tag = embedder_factory, model_tag
        self._embedder: Embedder | None = None
        self._error = False
        self._done = self._total = 0
        self._busy = False
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    # -- storage -------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS docs (root TEXT, rel TEXT, mtime REAL, size INTEGER,"
            " model TEXT, vec BLOB, PRIMARY KEY (root, rel))"
        )
        try:
            os.chmod(self._db_path, 0o600)  # derived from file contents: owner only
        except OSError:
            pass
        return connection

    # -- indexing ------------------------------------------------------------------------
    def start(self) -> None:
        """Load the model and bring the index up to date in the background; safe to repeat."""
        with self._lock:
            recent = self._last_refresh and time.monotonic() - self._last_refresh < REFRESH_SECONDS
            if self._busy or self._error or recent:
                return
            self._busy = True
        threading.Thread(target=self._run, name="semantic-index", daemon=True).start()

    def _run(self) -> None:
        try:
            if self._embedder is None:
                self._embedder = self._factory()
            self.refresh()
        except Exception:  # noqa: BLE001 - missing extra, no model download, ...: stay off
            self._error = True
        finally:
            self._last_refresh = time.monotonic()
            self._busy = False

    def refresh(self) -> None:
        """One synchronous pass: embed new or changed files, forget deleted ones."""
        assert self._embedder is not None
        entries, _ = file_search._walk(self._roots)
        wanted = [e for e in entries if e.extension in INDEXED_EXTENSIONS][:MAX_FILES]
        self._total, self._done = len(wanted), 0
        connection = self._connect()
        try:
            known = {
                (root, rel): (mtime, size, model)
                for root, rel, mtime, size, model in connection.execute(
                    "SELECT root, rel, mtime, size, model FROM docs"
                )
            }
            live = set()
            for entry in wanted:
                key = (str(entry.root), _rel(entry))
                live.add(key)
                if known.get(key) == (entry.mtime, entry.size, self._tag):
                    self._done += 1
                    continue
                vector = self._embed(entry)
                if vector is None:
                    continue
                connection.execute(
                    "INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?, ?)",
                    (*key, entry.mtime, entry.size, self._tag, _pack(vector)),
                )
                connection.commit()
                self._done += 1
            for key in known.keys() - live:
                connection.execute("DELETE FROM docs WHERE root = ? AND rel = ?", key)
            connection.commit()
        finally:
            connection.close()

    def _embed(self, entry: Entry) -> list[float] | None:
        try:
            if entry.extension in IMAGE_EXTENSIONS:
                data = _read_bytes(entry, MAX_IMAGE_BYTES)
                return None if data is None else self._embedder.image(data)
            head = ""
            if entry.extension in TEXT_EXTENSIONS and entry.size:
                head = file_search._read_head(entry)[:TEXT_CHARS]
            # Documents (pdf, docx) are not read here: only their path says what they are.
            return self._embedder.document(f"title: {_rel(entry)} | text: {head or 'none'}")
        except Exception:  # noqa: BLE001 - one unreadable or odd file must not stop the pass
            return None

    # -- search --------------------------------------------------------------------------
    def search(self, query: str, scope: list[Entry], limit: int) -> SemanticResult:
        if self._error:
            return SemanticResult("error")
        self.start()  # a new pass at most once a minute, so files added later are found too
        if self._embedder is None:
            return SemanticResult("loading")
        wanted = {(str(e.root), _rel(e)): e for e in scope}
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT root, rel, vec FROM docs WHERE model = ?", (self._tag,)
            )
            stored = [(wanted[(r, p)], blob) for r, p, blob in rows if (r, p) in wanted]
        finally:
            connection.close()
        state = "partial" if self._busy or self._done < self._total else "ready"
        if not stored:
            return SemanticResult(state, (), self._done, self._total)
        vector = self._embedder.query(query)
        scored = sorted(((e, _dot(_unpack(b), vector)) for e, b in stored), key=lambda x: -x[1])
        best = scored[0][1]
        hits = tuple(
            (e, s) for e, s in scored[:limit] if s >= MIN_SCORE and s >= best - MARGIN
        )
        return SemanticResult(state, hits, self._done, self._total)


_shared: dict[tuple[Path, ...], SemanticIndex] = {}
_shared_lock = threading.Lock()


def shared(roots: tuple[Path, ...]) -> SemanticIndex:
    """One index per process (a planner is built per command in one-shot mode)."""
    with _shared_lock:
        index = _shared.get(roots)
        if index is None:
            index = SemanticIndex(
                roots, DEFAULT_DB_PATH, SentenceTransformerEmbedder, f"{MODEL_NAME}:{DIM}"
            )
            _shared[roots] = index
        index.start()
        return index
