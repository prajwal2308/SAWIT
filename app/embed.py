"""Vectors for meaning-based search, so a note is findable by what it is about.

FTS5 matches words. Someone looking for "budgeting advice" will not find a note
titled "Allocate monthly net income using a 55/5/10/15/15 split", because none
of those words appear in it — and that is precisely the note they wanted. An
embedding closes that gap.

The vectors come from the same OpenAI-compatible endpoint the extraction uses,
so this needs no new key, no new service, and nothing running locally.
"""

from __future__ import annotations

import array
import logging
import math

log = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "nvidia/nemotron-3-embed-1b"


class EmbeddingError(RuntimeError):
    pass


def embed(
    texts: list[str],
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    query: bool = False,
) -> list[list[float]]:
    """Vectors for a batch of texts.

    Retrieval models are asymmetric: a stored note and the question someone asks
    about it are encoded differently, and mixing the two costs real accuracy.
    `query=True` marks the search side.
    """
    if not texts:
        return []
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise EmbeddingError("openai is not installed.") from exc

    client = OpenAI(base_url=base_url, api_key=api_key or "none")
    try:
        response = client.embeddings.create(
            model=model,
            input=texts,
            encoding_format="float",
            # NVIDIA's retrieval models take this as an extra field; endpoints
            # that do not know it ignore it rather than failing.
            extra_body={"input_type": "query" if query else "passage"},
        )
    except Exception as exc:
        raise EmbeddingError(f"Could not embed ({exc}).") from exc
    return [item.embedding for item in response.data]


def to_blob(vector: list[float]) -> bytes:
    """float32 rather than float64: half the bytes, no accuracy that matters."""
    return array.array("f", vector).tobytes()


def from_blob(blob: bytes) -> list[float]:
    out = array.array("f")
    out.frombytes(blob)
    return list(out)


def cosine(a: list[float], b: list[float]) -> float:
    """Similarity of two vectors, 1.0 being identical.

    A plain loop is the right tool at this scale: a few thousand notes is a few
    milliseconds, and it keeps numpy out of an image that is already tight on
    memory. Reach for a vector index when the note count makes this show up in
    a profile, not before.
    """
    if len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def note_text(note: dict) -> str:
    """What to embed: the meaning of the note, not its scaffolding.

    The transcript is deliberately left out. It is long, often empty on these
    reels, and averaging it into one vector drags the note toward whatever the
    creator rambled about rather than what the note is for.
    """
    parts = [
        note.get("title") or "",
        note.get("category") or "",
        note.get("one_liner") or "",
        " ".join(note.get("takeaways") or []),
        " ".join(note.get("steps") or []),
        " ".join(f"{f.get('label')}: {f.get('value')}" for f in (note.get("key_facts") or [])),
        " ".join(note.get("tags") or []),
    ]
    return "\n".join(p for p in parts if p).strip()
