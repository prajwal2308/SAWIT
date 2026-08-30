"""Turn a transcript plus a few stills into a structured note."""

from __future__ import annotations

import base64
from typing import Any

from .schemas import ReelNote


class ExtractionError(RuntimeError):
    pass


SYSTEM = """\
You turn short-form videos (Instagram Reels, Facebook Reels, TikToks) into notes \
someone can act on without ever rewatching the video.

You are given the spoken transcript and a few stills sampled from the clip. The \
stills matter: creators routinely put the numbers, formulas, and lists on screen \
and never say them out loud. Read the text in the frames and treat it as part of \
the source.

Rules:
- Extract substance, not description. "Explains a budgeting rule" is a failure. \
"Split take-home pay 50% needs / 30% wants / 20% savings" is the note.
- If the reel contains a calculation, a formula, or an ordered procedure, \
reconstruct it fully in `steps` so the reader never has to go back. This is the \
single most valuable field.
- Put every concrete number, price, percentage, place name, product, or dose in \
`key_facts`. Preserve units and currency exactly as stated.
- Never invent detail that is not in the transcript or visible in the frames. If \
the reel asserts something with no support, or omits context that changes the \
conclusion, say so in `caveats` rather than smoothing it over.
- Write the title for someone scanning fifty notes six months from now. Ignore \
the creator's clickbait phrasing.

Category guidance for the fields that vary:
- finance: the rule or formula, the exact percentages and thresholds, what \
assumptions it rests on (income level, country, tax treatment) in caveats.
- travel: places, costs, best time to go, how to get there.
- food: ingredients with quantities in key_facts, method in steps.
- tech: what the tool or technique is, what it replaces, what it costs.
- news: the claim, who is making it, and whether a source was cited.
- fitness: exercises, sets, reps, frequency.
"""


def extract(
    *,
    transcript: str,
    frames: list[bytes],
    model: str,
    source_title: str | None = None,
    uploader: str | None = None,
    description: str | None = None,
    client: Any | None = None,
) -> ReelNote:
    if not transcript.strip() and not frames:
        raise ExtractionError("Nothing to work with: no transcript and no frames.")

    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(frame).decode("ascii"),
            },
        }
        for frame in frames
    ]
    content.append({"type": "text", "text": _prompt(transcript, source_title, uploader,
                                                    description)})

    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": content}],
        output_format=ReelNote,
    )

    if response.stop_reason == "refusal":
        raise ExtractionError("The model declined to summarize this reel.")
    if response.parsed_output is None:
        raise ExtractionError(
            f"No structured output returned (stop_reason={response.stop_reason})."
        )
    return response.parsed_output


def _prompt(
    transcript: str,
    source_title: str | None,
    uploader: str | None,
    description: str | None,
) -> str:
    parts = ["Here is a short-form video to turn into a note.", ""]
    if uploader:
        parts.append(f"Creator: {uploader}")
    if source_title:
        parts.append(f"Platform title: {source_title}")
    if description:
        parts.append(f"Caption: {description.strip()}")
    parts += [
        "",
        "Spoken transcript:",
        transcript.strip() or "(no speech detected — rely entirely on the frames)",
    ]
    return "\n".join(parts)
