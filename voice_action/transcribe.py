"""Speech-to-text for push-to-talk: one recorded clip -> Korean command text (Groq Whisper)."""

import os
import tempfile
import time
from pathlib import Path

from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "whisper-large-v3-turbo"
MAX_BYTES = 20 * 1024 * 1024  # Groq rejects larger uploads
PREFIX = "voice-action-voice-"


def clip_path(raw):
    """Return the clip only if it is a plain file the app just wrote into the temp directory."""
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        real = path.resolve()  # resolve once: check and use the same location
    except (ValueError, OSError):  # e.g. NUL byte in the path
        return None
    if path.is_symlink() or not real.name.startswith(PREFIX) or real.parent != temp:
        return None
    path = real
    try:
        info = path.stat()
    except OSError:
        return None
    return path if path.is_file() and 0 < info.st_size <= MAX_BYTES else None


def transcribe(path, api_key, timeout=60):
    """Send the clip to Groq and return stripped text; the clip is always deleted."""
    try:
        client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=timeout)
        # O_NOFOLLOW: a symlink swapped in after clip_path() must not leak other files.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as clip:
            result = client.audio.transcriptions.create(
                model=MODEL, file=(path.name, clip), language="ko", response_format="text")
        return (result if isinstance(result, str) else result.text).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def purge_stale(max_age=3600):
    """Delete clips left behind by an app quit during recording or transcription."""
    for path in Path(tempfile.gettempdir()).glob(f"{PREFIX}*"):
        try:
            if not path.is_symlink() and time.time() - path.stat().st_mtime > max_age:
                path.unlink()
        except OSError:
            pass
