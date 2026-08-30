"""Turn a transcript plus a few stills into a structured note.

Two backends. Anthropic is the better extractor, particularly at
reconstructing a calculation into `steps`. Any OpenAI-compatible endpoint
(`nvidia`) works too and is what the free tiers offer — pick a multimodal
model there, or the on-screen numbers are lost.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from .schemas import ReelNote

log = logging.getLogger(__name__)


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
    backend: str = "anthropic",
    source_title: str | None = None,
    uploader: str | None = None,
    description: str | None = None,
    client: Any | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ReelNote:
    if not transcript.strip() and not frames:
        raise ExtractionError("Nothing to work with: no transcript and no frames.")

    prompt = _prompt(transcript, source_title, uploader, description)
    if backend == "anthropic":
        return _extract_anthropic(prompt, frames, model, client)
    if backend == "nvidia":
        return _extract_openai_compatible(prompt, frames, model, client, base_url, api_key)
    raise ExtractionError(f"Unknown extraction backend {backend!r}.")


def _extract_anthropic(
    prompt: str, frames: list[bytes], model: str, client: Any | None
) -> ReelNote:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _b64(frame),
            },
        }
        for frame in frames
    ]
    content.append({"type": "text", "text": prompt})

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


def _extract_openai_compatible(
    prompt: str,
    frames: list[bytes],
    model: str,
    client: Any | None,
    base_url: str | None,
    api_key: str | None,
) -> ReelNote:
    if client is None:
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key)

    content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(frame)}"}}
        for frame in frames
    ]
    content.append({"type": "text", "text": prompt})
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": content},
    ]
    schema = strict_schema(ReelNote)

    try:
        text = _chat(client, model, messages, {
            "type": "json_schema",
            "json_schema": {"name": "reel_note", "strict": True, "schema": schema},
        })
    except Exception as exc:
        # Structured-output support varies across open models. Falling back to
        # plain JSON mode with the schema in the prompt keeps the weaker ones
        # usable instead of failing the note outright.
        log.warning("json_schema rejected by %s (%s); retrying in JSON mode", model, exc)
        nudged = list(messages)
        nudged[0] = {
            "role": "system",
            "content": f"{SYSTEM}\n\nReply with JSON matching this schema exactly:\n"
                       f"{json.dumps(schema)}",
        }
        text = _chat(client, model, nudged, {"type": "json_object"})

    return _parse(text, model)


def _chat(client: Any, model: str, messages: list, response_format: dict) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4000,
        temperature=0.2,
        response_format=response_format,
    )
    return response.choices[0].message.content or ""


def _parse(text: str, model: str) -> ReelNote:
    # Some models wrap JSON in a fenced block even when asked not to.
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return ReelNote.model_validate_json(cleaned)
    except Exception as exc:
        raise ExtractionError(
            f"{model} did not return a usable note ({exc}). "
            f"First 200 characters: {cleaned[:200]!r}"
        ) from exc


def _b64(frame: bytes) -> str:
    return base64.standard_b64encode(frame).decode("ascii")


def strict_schema(model_cls: type) -> dict[str, Any]:
    """A self-contained strict JSON schema.

    `$ref`/`$defs` are where open models' constrained decoding tends to fall
    over, so references are inlined and every object is closed.
    """
    raw = model_cls.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(defs[name])
        node = {key: resolve(value) for key, value in node.items()}
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = sorted(node.get("properties", {}))
        return node

    return resolve(raw)


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
