import hashlib
import hmac
import json

import pytest

from app import instagram
from app.schemas import ReelNote

from .conftest import IG_APP_SECRET, IG_VERIFY_TOKEN, make_note


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(IG_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def webhook_body(**message) -> dict:
    return {
        "object": "instagram",
        "entry": [{"id": "17841400000000000", "messaging": [
            {"sender": {"id": "9876543210"}, "recipient": {"id": "17841400000000000"},
             "message": message}
        ]}],
    }


def test_subscription_handshake(settings):
    assert instagram.verify_subscription(
        "subscribe", IG_VERIFY_TOKEN, "challenge-123", settings
    ) == "challenge-123"


@pytest.mark.parametrize(
    "mode,token",
    [("subscribe", "wrong-token"), ("unsubscribe", IG_VERIFY_TOKEN), ("subscribe", None)],
)
def test_bad_handshake_is_refused(settings, mode, token):
    with pytest.raises(instagram.InstagramError):
        instagram.verify_subscription(mode, token, "challenge", settings)


def test_signature_must_match(settings):
    body = b'{"object":"instagram"}'

    assert instagram.verify_signature(body, sign(body), settings) is True
    assert instagram.verify_signature(body, sign(b"other body"), settings) is False
    assert instagram.verify_signature(body, None, settings) is False
    assert instagram.verify_signature(body, "md5=whatever", settings) is False


def test_shared_reel_yields_the_media_url(settings):
    payload = webhook_body(
        mid="mid.abc",
        attachments=[{"type": "ig_reel", "payload": {
            "url": "https://cdn.example/reel.mp4", "title": "how to budget"}}],
    )

    (reel,) = instagram.parse_events(payload)
    assert reel.media_url == "https://cdn.example/reel.mp4"
    assert reel.sender_id == "9876543210"
    assert reel.mid == "mid.abc"
    assert reel.title == "how to budget"


def test_our_own_replies_are_ignored():
    """Echoes come back through the same webhook; processing them would loop."""
    payload = webhook_body(
        mid="mid.echo",
        is_echo=True,
        attachments=[{"type": "ig_reel", "payload": {"url": "https://cdn.example/r.mp4"}}],
    )

    assert instagram.parse_events(payload) == []


def test_non_message_events_are_ignored():
    assert instagram.parse_events({"entry": [{"messaging": [{"read": {"mid": "x"}}]}]}) == []
    assert instagram.parse_events({}) == []


def test_feed_post_shares_are_skipped():
    """A shared photo post has no video to transcribe."""
    payload = webhook_body(
        mid="mid.post",
        attachments=[{"type": "ig_post", "payload": {"url": "https://cdn.example/p.jpg"}}],
    )

    assert instagram.parse_events(payload) == []


def test_a_pasted_link_falls_back_to_the_page_url():
    payload = webhook_body(mid="mid.text", text="check this https://www.instagram.com/reel/X/")

    (reel,) = instagram.parse_events(payload)
    assert reel.media_url is None
    assert reel.page_url == "https://www.instagram.com/reel/X/"


def test_plain_chat_is_not_a_reel():
    assert instagram.parse_events(webhook_body(mid="mid.hi", text="hey")) == []


def test_reply_is_plain_text_and_fits_in_a_dm():
    reply = instagram.format_reply(make_note(), link="https://notes.example/notes/abc")

    assert reply.startswith("The 50/30/20 budget rule")
    assert "• 50% needs" in reply
    assert "1. Start from take-home pay" in reply
    assert "Savings share: 20%" in reply
    assert reply.endswith("https://notes.example/notes/abc")
    assert len(reply) <= instagram.MAX_DM_CHARS


def test_long_note_is_truncated_but_keeps_the_link():
    note = ReelNote(
        title="A very long one",
        category="finance",
        one_liner="x" * 200,
        takeaways=["y" * 200] * 5,
        key_facts=[],
        steps=["z" * 200] * 5,
        caveats=[],
        tags=["long"],
    )
    link = "https://notes.example/notes/abc"

    reply = instagram.format_reply(note, link=link)

    assert len(reply) <= instagram.MAX_DM_CHARS
    assert reply.endswith(link)
    assert "…" in reply


def test_send_text_reports_a_rejected_reply(settings, monkeypatch):
    import httpx

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(400, text='{"error":"outside window"}', request=request)

    monkeypatch.setattr("app.instagram.httpx.post", fake_post)

    with pytest.raises(instagram.InstagramError, match="400"):
        instagram.send_text("9876543210", "hello", settings)


def test_send_text_targets_the_configured_endpoint(settings, monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs["json"]
        seen["headers"] = kwargs["headers"]
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.instagram.httpx.post", fake_post)
    instagram.send_text("9876543210", "the takeaway", settings)

    assert seen["url"] == "https://graph.instagram.example/v23.0/me/messages"
    assert seen["json"] == {
        "recipient": {"id": "9876543210"},
        "message": {"text": "the takeaway"},
    }
    assert seen["headers"]["Authorization"] == "Bearer ig-access-token"


def test_json_body_round_trips_through_the_signature(settings):
    """The signature covers raw bytes, so re-serializing would break it."""
    body = json.dumps(webhook_body(mid="mid.1", text="https://x.test/r")).encode()

    assert instagram.verify_signature(body, sign(body), settings) is True
