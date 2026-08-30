import pytest

from app.extract import ExtractionError, _prompt, extract
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
