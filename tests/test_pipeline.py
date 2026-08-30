"""Delivery routing: the answer must go back where the reel came from."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import instagram
from app.pipeline import Source, _deliver, _deliver_failure

from .conftest import make_note


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
