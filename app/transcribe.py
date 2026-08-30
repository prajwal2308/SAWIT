"""Speech-to-text, with a local default so a test run costs nothing."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


class TranscriptionError(RuntimeError):
    pass


def transcribe(
    audio_path: Path,
    *,
    backend: str,
    model_size: str,
    base_url: str | None = None,
    api_key: str | None = None,
    hosted_model: str = "whisper-1",
) -> str:
    if backend == "faster-whisper":
        return _faster_whisper(audio_path, model_size)
    if backend == "hosted":
        return _hosted_whisper(audio_path, base_url, api_key, hosted_model)
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


def _hosted_whisper(
    audio_path: Path, base_url: str | None, api_key: str | None, model: str
) -> str:
    """Any OpenAI-compatible transcription endpoint.

    Worth the switch when the host is small: local whisper is what forces a
    2 GB machine, and moving it off-box makes this service light enough for a
    free tier.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise TranscriptionError("openai is not installed but SAWIT_ASR=hosted.") from exc
    if not api_key:
        raise TranscriptionError("ASR_API_KEY is not set but SAWIT_ASR=hosted.")
    client = OpenAI(base_url=base_url, api_key=api_key)
    with audio_path.open("rb") as fh:
        result = client.audio.transcriptions.create(model=model, file=fh)
    return result.text.strip()
