"""The agent's spoken voice (ask.js talk mode): Piper, an open-source neural
TTS that runs on the server's CPU (github.com/OHF-Voice/piper1-gpl), with the
Italian "paola" voice (rhasspy/piper-voices). Lorenzo, 01/10: the browser
voice is "molto robotico [...] vale la pena utilizzare dei modelli con una
voce realistica". About 0.8 s of CPU for 10 s of speech on two cores, so one
synthesis at a time; the browser voice stays as the fallback when the model
is missing (tests, a checkout without the download) or anything fails.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

# The Dockerfile downloads the voices here; override with PIPER_VOICES_DIR.
VOICES_DIR = Path(os.environ.get("PIPER_VOICES_DIR", "/srv/voices"))
VOICES = {"it": "it_IT-paola-medium", "en": "en_US-lessac-medium"}
MAX_CHARS = 900

_loaded: dict[str, object] = {}
_lock = threading.Lock()
_one = asyncio.Semaphore(1)


def available(lang: str = "it") -> bool:
    return (VOICES_DIR / f"{VOICES.get(lang[:2], VOICES['it'])}.onnx").is_file()


def _voice(lang: str):
    name = VOICES.get(lang[:2], VOICES["it"])
    with _lock:
        if name not in _loaded:
            from piper import PiperVoice
            _loaded[name] = PiperVoice.load(str(VOICES_DIR / f"{name}.onnx"))
        return _loaded[name]


def warm(lang: str = "it") -> None:
    """Load the voice ahead of the first answer (about 2 s)."""
    try:
        _voice(lang)
    except Exception:
        logger.exception("piper voice load")


def _wav(text: str, lang: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        _voice(lang).synthesize_wav(text[:MAX_CHARS], w)
    return buf.getvalue()


async def speak(text: str, lang: str = "it") -> bytes | None:
    """WAV bytes of `text`, or None when the voice is not installed."""
    if not text.strip() or not available(lang):
        return None
    async with _one:
        return await asyncio.to_thread(_wav, text, lang)
