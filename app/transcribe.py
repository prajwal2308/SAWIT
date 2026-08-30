"""Speech-to-text, with a local default so a test run costs nothing."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


class TranscriptionError(RuntimeError):
    pass


def transcribe(audio_path: Path, *, backend: str, model_size: str) -> str:
    if backend == "faster-whisper":
        return _faster_whisper(audio_path, model_size)
    if backend == "openai":
        return _openai_whisper(audio_path)
    raise TranscriptionError(f"Unknown ASR backend {backend!r}.")


@lru_cache(maxsize=2)
def _load_local_model(model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TranscriptionError(
            "faster-whisper is not installed. `pip install faster-whisper`, or set "
            "SAWIT_ASR=openai."
        ) from exc
    # int8 on CPU is the setting that makes this viable on a small cloud box.
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def _faster_whisper(audio_path: Path, model_size: str) -> str:
    model = _load_local_model(model_size)
    segments, _info = model.transcribe(str(audio_path), vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


def _openai_whisper(audio_path: Path) -> str:
    """Optional hosted backend — faster than CPU whisper, ~$0.006/minute."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise TranscriptionError("openai is not installed but SAWIT_ASR=openai.") from exc
    if not os.environ.get("OPENAI_API_KEY"):
        raise TranscriptionError("OPENAI_API_KEY is not set but SAWIT_ASR=openai.")
    client = OpenAI()
    with audio_path.open("rb") as fh:
        result = client.audio.transcriptions.create(model="whisper-1", file=fh)
    return result.text.strip()
