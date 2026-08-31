import json

import pytest

from app.extract import ExtractionError, _prompt, extract, strict_schema
from app.schemas import ReelNote

from .conftest import make_note


class FakeResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response):
        self._response = response
        self.call = None

    def parse(self, **kwargs):
        self.call = kwargs
        return self._response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def test_frames_and_transcript_are_both_sent():
    client = FakeClient(FakeResponse(make_note()))

    note = extract(
        transcript="fifty thirty twenty",
        frames=[b"\xff\xd8fake-jpeg-a", b"\xff\xd8fake-jpeg-b"],
        model="claude-opus-5",
        client=client,
    )

    assert isinstance(note, ReelNote)
    content = client.messages.call["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert "fifty thirty twenty" in content[-1]["text"]
    assert client.messages.call["output_format"] is ReelNote


def test_caption_and_creator_reach_the_prompt():
    text = _prompt("spoken words", "Platform Title", "@somecreator", "caption with #hashtags")

    assert "@somecreator" in text
    assert "caption with #hashtags" in text
    assert "spoken words" in text


def test_silent_reel_still_extracts_from_frames():
    text = _prompt("", None, None, None)

    assert "rely entirely on the frames" in text


def test_nothing_to_work_with_is_an_error():
    with pytest.raises(ExtractionError):
        extract(transcript="   ", frames=[], model="claude-opus-5", client=FakeClient(None))


def test_refusal_is_surfaced_not_silently_dropped():
    client = FakeClient(FakeResponse(None, stop_reason="refusal"))

    with pytest.raises(ExtractionError, match="declined"):
        extract(transcript="something", frames=[], model="claude-opus-5", client=client)


class FakeChatCompletions:
    """Stands in for an OpenAI-compatible endpoint (NVIDIA and friends)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        message = type("Message", (), {"content": result})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice]})()


class FakeOpenAI:
    def __init__(self, *responses):
        self.chat = type("Chat", (), {"completions": FakeChatCompletions(*responses)})()

    @property
    def calls(self):
        return self.chat.completions.calls


NOTE_JSON = """
{"title": "The 50/30/20 budget rule", "category": "finance",
 "one_liner": "Split take-home pay into needs, wants and savings.",
 "takeaways": ["50% needs"], "key_facts": [{"label": "Savings", "value": "20%"}],
 "steps": ["Start from take-home pay"], "caveats": [], "tags": ["budget"]}
"""


def test_nvidia_backend_sends_frames_as_image_urls():
    client = FakeOpenAI(NOTE_JSON)

    note = extract(transcript="fifty thirty twenty", frames=[b"\xff\xd8jpeg"],
                   model="meta/llama-4-maverick-17b-128e-instruct",
                   backend="nvidia", client=client)

    assert note.title == "The 50/30/20 budget rule"
    content = client.calls[0]["messages"][1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[1]["type"] == "text"


def test_nvidia_backend_asks_for_a_strict_schema_first():
    client = FakeOpenAI(NOTE_JSON)

    extract(transcript="t", frames=[], model="m", backend="nvidia", client=client)

    fmt = client.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert "$ref" not in json.dumps(fmt["json_schema"]["schema"])


def test_a_model_without_schema_support_falls_back_to_json_mode():
    """Weaker open models reject json_schema; the note should still land."""
    client = FakeOpenAI(RuntimeError("response_format json_schema not supported"), NOTE_JSON)

    note = extract(transcript="t", frames=[], model="m", backend="nvidia", client=client)

    assert note.title == "The 50/30/20 budget rule"
    assert client.calls[1]["response_format"] == {"type": "json_object"}
    # The schema moves into the prompt so the model still knows the shape.
    assert "one_liner" in client.calls[1]["messages"][0]["content"]


def test_a_fenced_json_reply_is_still_parsed():
    client = FakeOpenAI(f"```json\n{NOTE_JSON}\n```")

    note = extract(transcript="t", frames=[], model="m", backend="nvidia", client=client)

    assert note.category == "finance"


def test_unusable_output_names_the_model_and_shows_what_came_back():
    client = FakeOpenAI("I'd be happy to help you summarize this reel!")

    with pytest.raises(ExtractionError, match="happy to help"):
        extract(transcript="t", frames=[], model="some/model", backend="nvidia",
                client=client)


def test_an_unknown_backend_is_rejected():
    with pytest.raises(ExtractionError, match="Unknown extraction backend"):
        extract(transcript="t", frames=[], model="m", backend="mistral", client=None)


def test_the_strict_schema_is_self_contained():
    schema = strict_schema(ReelNote)

    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == schema["required"]
    key_fact = schema["properties"]["key_facts"]["items"]
    assert key_fact["additionalProperties"] is False
    assert key_fact["required"] == ["label", "value"]


def test_a_note_wrapped_in_reasoning_is_recovered():
    """A reasoning model narrates before it answers. The note is still in there,
    and losing the whole reel to a prose preamble is the wrong trade."""
    from app.extract import _parse

    body = json.dumps({
        "title": "The 50/30/20 budget rule",
        "category": "finance",
        "one_liner": "Split take-home pay into needs, wants and savings.",
        "takeaways": ["50% needs"],
        "key_facts": [{"label": "Savings share", "value": "20%"}],
        "steps": [], "caveats": [], "tags": ["budget"],
    })

    note = _parse(f"We need to classify this reel. It is finance.\n{body}\nDone.", "m")
    assert note.title == "The 50/30/20 budget rule"


def test_a_reply_with_no_object_at_all_still_fails():
    from app.extract import ExtractionError, _parse

    with pytest.raises(ExtractionError):
        _parse("I cannot summarise this reel.", "m")
