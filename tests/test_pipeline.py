"""Delivery routing: the answer must go back where the reel came from."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import instagram
from app.pipeline import Source, _deliver, _deliver_failure

from .conftest import bound_store, make_note


@pytest.fixture
def spy(monkeypatch):
    calls = {"dm": [], "push": [], "push_fail": []}
    monkeypatch.setattr(
        "app.pipeline.instagram.send_text",
        lambda recipient, text, _settings: calls["dm"].append((recipient, text)),
    )
    monkeypatch.setattr(
        "app.pipeline.notify.push_note",
        lambda note, **kwargs: calls["push"].append((note.title, kwargs.get("click_url"))),
    )
    monkeypatch.setattr(
        "app.pipeline.notify.push_failure",
        lambda url, error, **kwargs: calls["push_fail"].append((url, error)),
    )
    return calls


def test_a_silent_reel_is_still_a_note(settings, spy, tmp_path, monkeypatch):
    """No audio track is ordinary — a caption over stripped music, or the
    video-only stream Instagram hands a datacenter IP. The frames carry it."""
    from app import pipeline
    from app.media import Media

    store = bound_store(str(tmp_path / "notes.sqlite3"))
    note_id = store.create_pending("https://x.test/r")
    seen: dict = {}

    monkeypatch.setattr(pipeline.media_mod, "fetch", lambda *a, **k: Media(
        audio_path=None, frames=[b"jpeg-bytes"], title="T", duration=8.0))
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **k: pytest.fail(
        "transcribe must not run when there is no audio track"))
    monkeypatch.setattr(pipeline.extract_mod, "extract",
                        lambda **kw: seen.update(kw) or make_note())

    pipeline.process(note_id, Source(page_url="https://x.test/r"), settings, store)

    assert seen["transcript"] == ""
    assert seen["frames"] == [b"jpeg-bytes"]
    assert store.get(note_id)["status"] == "ready"


def test_a_dm_share_is_answered_in_the_thread(settings, spy):
    settings = replace(settings, ntfy_topic="a-topic", public_base_url="https://notes.test")
    source = Source(page_url="instagram-dm:mid.1", media_url="https://cdn/r.mp4",
                    reply_to="9876543210")

    _deliver("abc123", source, make_note(), settings)

    assert spy["dm"][0][0] == "9876543210"
    assert "The 50/30/20 budget rule" in spy["dm"][0][1]
    assert "https://notes.test/notes/abc123" in spy["dm"][0][1]
    # The DM is the delivery; a push as well would be a second buzz for one reel.
    assert spy["push"] == []


def test_a_share_sheet_link_falls_back_to_push(settings, spy):
    settings = replace(settings, ntfy_topic="a-topic", public_base_url="https://notes.test")

    _deliver("abc123", Source(page_url="https://x.test/r"), make_note(), settings)

    assert spy["dm"] == []
    assert spy["push"] == [("The 50/30/20 budget rule", "https://notes.test/notes/abc123")]


def test_a_rejected_dm_still_reaches_you_by_push(settings, spy, monkeypatch):
    """Outside the 24-hour window Instagram refuses the reply; don't lose the note."""
    settings = replace(settings, ntfy_topic="a-topic")

    def boom(recipient, text, _settings):
        raise instagram.InstagramError("outside the 24 hour window")

    monkeypatch.setattr("app.pipeline.instagram.send_text", boom)

    _deliver("abc123", Source(page_url="x", reply_to="9876543210"), make_note(), settings)

    assert spy["push"] == [("The 50/30/20 budget rule", None)]


def test_failures_are_reported_in_the_thread(settings, spy):
    _deliver_failure(
        Source(page_url="x", reply_to="9876543210"), "login required", settings
    )

    assert "login required" in spy["dm"][0][1]
    assert spy["push_fail"] == []


def test_failures_without_a_thread_go_to_push(settings, spy):
    settings = replace(settings, ntfy_topic="a-topic")

    _deliver_failure(Source(page_url="https://x.test/r"), "boom", settings)

    assert spy["push_fail"] == [("https://x.test/r", "boom")]


def test_nothing_configured_means_nothing_is_delivered(settings, spy):
    """No DM thread and no push topic: the note is still saved, silently."""
    _deliver("abc123", Source(page_url="https://x.test/r"), make_note(), settings)

    assert spy["dm"] == [] and spy["push"] == []


def test_an_image_post_is_written_from_its_caption(settings, spy, tmp_path, monkeypatch):
    """Not every saved post is a video. A carousel has no video stream at all,
    and its caption is where the content actually lives."""
    from app import pipeline
    from app.media import Media

    store = bound_store(str(tmp_path / "notes.sqlite3"))
    note_id = store.create_pending("https://x.test/p")
    seen: dict = {}
    caption = ("Only got one day in Acadia? Do this. This route hits the highlights "
               "without feeling rushed, from coastal cliffs to mountain overlooks.")

    monkeypatch.setattr(pipeline.media_mod, "fetch", lambda *a, **k: Media(
        audio_path=None, frames=[], caption=caption, title="Post by shakaguide",
        uploader="shakaguide", slides=10))
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **k: pytest.fail(
        "there is no audio on an image post"))
    monkeypatch.setattr(pipeline.extract_mod, "extract",
                        lambda **kw: seen.update(kw) or make_note())

    pipeline.process(note_id, Source(page_url="https://x.test/p"), settings, store)

    assert seen["transcript"] == caption
    assert store.get(note_id)["status"] == "ready"
