"""Guards the one contract this app has with an external SDK.

extract.py hands a pydantic model to `messages.parse(output_format=...)` and
trusts the SDK to turn it into a strict JSON schema. If that conversion ever
changes shape, extraction breaks at runtime against a live API — which is the
worst place to find out. These run the SDK's own transform locally.
"""

from __future__ import annotations

import pytest

from app.schemas import ReelNote

anthropic = pytest.importorskip("anthropic")


@pytest.fixture(scope="module")
def schema() -> dict:
    from anthropic.resources.messages.messages import transform_schema
    from pydantic import TypeAdapter

    return transform_schema(TypeAdapter(ReelNote).json_schema())


def test_the_sdk_still_exposes_what_extract_calls():
    messages = anthropic.Anthropic(api_key="not-a-real-key").messages
    parse = getattr(messages, "parse", None)

    assert parse is not None, "messages.parse() is gone; extract.py needs rewriting"

    import inspect

    params = inspect.signature(parse).parameters
    for name in ("model", "max_tokens", "system", "thinking", "messages", "output_format"):
        assert name in params, f"messages.parse() no longer accepts {name}"


def test_the_note_model_converts_to_a_strict_schema(schema):
    assert schema["additionalProperties"] is False
    # Strict mode requires every property to be listed as required.
    assert sorted(schema["properties"]) == sorted(schema["required"])
    assert sorted(schema["properties"]) == [
        "category", "caveats", "key_facts", "one_liner",
        "steps", "tags", "takeaways", "title",
    ]


def test_nested_key_facts_are_strict_too(schema):
    key_fact = schema["$defs"]["KeyFact"]

    assert key_fact["additionalProperties"] is False
    assert sorted(key_fact["required"]) == ["label", "value"]


def test_the_field_descriptions_survive_the_conversion(schema):
    """They are prompt, not documentation — losing them degrades extraction."""
    assert "saves a rewatch" in schema["properties"]["steps"]["description"]
    assert "clickbait" in schema["properties"]["title"]["description"]


def test_every_category_the_ui_can_filter_on_is_offered(schema):
    assert set(schema["properties"]["category"]["enum"]) == {
        "finance", "travel", "food", "tech", "news",
        "fitness", "education", "shopping", "other",
    }
