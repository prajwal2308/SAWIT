import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app, get_store
from app.store import Store

from .conftest import API_KEY, IG_VERIFY_TOKEN, make_note
from .test_instagram import sign, webhook_body


@pytest.fixture
def client(settings, monkeypatch):
    store = Store(settings.db_path)
    started: list[tuple[str, object]] = []

    # The real pipeline downloads and transcribes; record the call instead.
    monkeypatch.setattr(
        "app.main.process",
        lambda note_id, source, _settings, _store: started.append((note_id, source)),
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    test_client = TestClient(app)
    test_client.store = store
    test_client.started = started
    yield test_client
    app.dependency_overrides.clear()


def test_ingest_requires_the_key(client):
    assert client.post("/ingest", json={"url": "https://example.com/r/1"}).status_code == 401
    assert client.post(
        "/ingest",
        json={"url": "https://example.com/r/1"},
        headers={"X-API-Key": "wrong"},
    ).status_code == 401


def test_ingest_returns_immediately_and_queues_the_work(client):
    response = client.post(
        "/ingest",
        json={"url": "https://www.instagram.com/reel/ABC/"},
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 202
    note_id = response.json()["id"]
    assert response.json()["status"] == "pending"
    assert len(client.started) == 1
    queued_id, source = client.started[0]
    assert queued_id == note_id
    assert source.page_url == "https://www.instagram.com/reel/ABC/"
    # Share-sheet links have no media URL and nowhere to reply to.
    assert source.media_url is None
    assert source.reply_to is None


def test_shared_text_around_the_url_is_tolerated(client):
    response = client.post(
        "/ingest",
        json={"url": "look at this https://www.facebook.com/share/r/abc/ 😀"},
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 202
    assert client.started[0][1].page_url == "https://www.facebook.com/share/r/abc/"


def test_text_with_no_url_is_rejected(client):
    response = client.post(
        "/ingest", json={"url": "no link here"}, headers={"X-API-Key": API_KEY}
    )

    assert response.status_code == 422


def test_browser_views_accept_the_key_as_a_query_param(client):
    note_id = client.store.create_pending("https://example.com/r/9")
    client.store.save_note(note_id, make_note(), transcript="fifty thirty twenty")

    assert client.get("/").status_code == 401

    listing = client.get("/", params={"k": API_KEY})
    assert listing.status_code == 200
    assert "The 50/30/20 budget rule" in listing.text

    page = client.get(f"/notes/{note_id}", params={"k": API_KEY})
    assert page.status_code == 200
    assert "Multiply by 0.5 for needs" in page.text
    assert "Savings share" in page.text


def test_search_narrows_the_listing(client):
    for note in (make_note(), make_note(title="Kyoto in November", category="travel")):
        note_id = client.store.create_pending("https://example.com/r/x")
        client.store.save_note(note_id, note, transcript="unrelated words")

    hits = client.get("/", params={"k": API_KEY, "q": "kyoto"})
    assert "Kyoto in November" in hits.text
    assert "The 50/30/20 budget rule" not in hits.text


def test_unknown_note_is_404(client):
    assert client.get("/notes/nope", params={"k": API_KEY}).status_code == 404


def test_webhook_handshake_echoes_the_challenge(client):
    response = client.get(
        "/webhook/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": IG_VERIFY_TOKEN,
                "hub.challenge": "42"},
    )

    assert response.status_code == 200
    assert response.text == "42"


def test_webhook_handshake_rejects_a_bad_token(client):
    response = client.get(
        "/webhook/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
    )

    assert response.status_code == 403


def test_unsigned_webhook_is_refused(client):
    body = json.dumps(webhook_body(mid="m1", text="https://x.test/r")).encode()

    response = client.post("/webhook/instagram", content=body,
                           headers={"Content-Type": "application/json"})

    assert response.status_code == 403
    assert client.started == []


def test_shared_reel_is_queued_with_a_reply_target(client):
    body = json.dumps(webhook_body(
        mid="mid.reel.1",
        attachments=[{"type": "ig_reel", "payload": {"url": "https://cdn.test/r.mp4"}}],
    )).encode()

    response = client.post(
        "/webhook/instagram", content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)},
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": 1}
    _, source = client.started[0]
    assert source.media_url == "https://cdn.test/r.mp4"
    assert source.reply_to == "9876543210"


def test_a_retried_webhook_is_not_processed_twice(client):
    body = json.dumps(webhook_body(
        mid="mid.reel.dup",
        attachments=[{"type": "ig_reel", "payload": {"url": "https://cdn.test/r.mp4"}}],
    )).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)}

    first = client.post("/webhook/instagram", content=body, headers=headers)
    second = client.post("/webhook/instagram", content=body, headers=headers)

    assert first.json() == {"accepted": 1}
    assert second.json() == {"accepted": 0}
    assert len(client.started) == 1
