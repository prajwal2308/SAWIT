import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import KEY_COOKIE, app, get_store
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


def test_resharing_a_saved_reel_returns_the_note_instead_of_redoing_it(client):
    """Re-sharing is how people find a reel again. Each repeat would otherwise
    cost another download, another Whisper run and another model call."""
    first = client.post("/ingest", json={"url": "https://x.test/r"},
                        headers={"X-API-Key": API_KEY})
    note_id = first.json()["id"]
    client.store.save_note(note_id, make_note(), transcript="t")

    again = client.post("/ingest", json={"url": "https://x.test/r"},
                        headers={"X-API-Key": API_KEY})

    assert again.json() == {"id": note_id, "status": "ready", "duplicate": True}
    assert len(client.started) == 1, "the reel must not be processed twice"


def test_resharing_a_failed_reel_does_try_again(client):
    """A failure is not a note. Re-sharing it should mean another attempt."""
    first = client.post("/ingest", json={"url": "https://x.test/r"},
                        headers={"X-API-Key": API_KEY})
    client.store.mark_failed(first.json()["id"], "ffmpeg fell over")

    again = client.post("/ingest", json={"url": "https://x.test/r"},
                        headers={"X-API-Key": API_KEY})

    assert again.status_code == 202
    assert again.json()["status"] == "pending"
    assert len(client.started) == 2


def test_pasting_a_saved_link_again_opens_the_note(client):
    client.get("/", params={"k": API_KEY})
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="t")

    response = client.post("/add", data={"url": "https://x.test/r"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/notes/{note_id}"
    assert client.started == []


def test_pasting_a_link_into_the_page_queues_it(client):
    client.get("/", params={"k": API_KEY})

    response = client.post(
        "/add",
        data={"url": "https://www.instagram.com/reel/ABC/"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(client.started) == 1
    note_id, source = client.started[0]
    assert source.page_url == "https://www.instagram.com/reel/ABC/"
    # Straight to the note, so you watch it work rather than hunting the list.
    assert response.headers["location"] == f"/notes/{note_id}"


def test_the_paste_box_needs_a_real_link(client):
    client.get("/", params={"k": API_KEY})
    response = client.post("/add", data={"url": "not a link"})
    assert response.status_code == 400
    assert client.started == []


def test_the_paste_box_needs_the_key(client):
    assert client.post("/add", data={"url": "https://example.com/r/1"}).status_code == 401
    assert client.started == []


def test_search_adds_meaning_hits_after_the_keyword_ones(client, monkeypatch):
    """The two answer different questions. Keeping keyword first means adding
    meaning never costs precision on the searches that already worked."""
    from app import embed as embed_mod

    word = client.store.create_pending("https://x.test/word")
    client.store.save_note(word, make_note(title="A budget rule"), transcript="budget")
    meaning = client.store.create_pending("https://x.test/meaning")
    client.store.save_note(
        meaning,
        # Deliberately shares not one word with the query — which is the whole
        # point: FTS cannot reach it, and it is still the note you wanted.
        make_note(title="Allocate 55/5/10/15/15", tags=["allocation"],
                  one_liner="Divide net income five ways.",
                  takeaways=["55 percent to essentials"], steps=[], key_facts=[]),
        transcript="nothing in common",
    )

    monkeypatch.setattr(embed_mod, "rank", lambda *a, **k: [meaning, word])
    client.get("/", params={"k": API_KEY})

    page = client.get("/", params={"q": "budget"}).text

    assert "A budget rule" in page
    assert "Allocate 55/5/10/15/15" in page          # found with no shared word
    assert page.index("A budget rule") < page.index("Allocate 55/5/10/15/15")


def test_reindex_only_touches_notes_without_a_vector(client, monkeypatch):
    from app import embed as embed_mod

    done = client.store.create_pending("https://x.test/done")
    client.store.save_note(done, make_note(), transcript="t")
    client.store.set_embedding(done, embed_mod.to_blob([1.0, 0.0]))
    todo = client.store.create_pending("https://x.test/todo")
    client.store.save_note(todo, make_note(), transcript="t")

    seen = []
    monkeypatch.setattr(embed_mod, "embed_note",
                        lambda note, s, st: seen.append(note["id"]) or True)

    body = client.post("/api/reindex", headers={"X-API-Key": API_KEY}).json()

    assert seen == [todo], "a note that already has a vector must be left alone"
    assert body["considered"] == 1


def test_clearing_the_search_really_clears_it(client):
    """The category used to ride along in a hidden field, so emptying the box
    you could see left you filtered by something you could not."""
    for cat in ("finance", "travel"):
        note_id = client.store.create_pending(f"https://x.test/{cat}")
        client.store.save_note(note_id, make_note(category=cat, title=f"A {cat} note"),
                               transcript="t")
    client.get("/", params={"k": API_KEY})

    narrowed = client.get("/", params={"category": "finance"}).text
    assert "A travel note" not in narrowed
    # Nothing may carry the category invisibly out of this page.
    assert "name=category" not in narrowed

    cleared = client.get("/").text
    assert "A travel note" in cleared and "A finance note" in cleared


def test_the_page_says_what_it_is_filtered_by(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="t")
    client.get("/", params={"k": API_KEY})

    page = client.get("/", params={"q": "budget", "category": "finance"}).text

    assert "Showing" in page and "budget" in page and "finance" in page


def test_the_feed_shows_one_note_per_screen(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="fifty thirty twenty")

    page = client.get("/feed", params={"k": API_KEY}).text

    assert "The 50/30/20 budget rule" in page
    assert "Multiply by 0.5 for needs" in page          # the steps travel with it
    # Sideways for cards, down for reading: two axes, two jobs, so neither
    # gesture has to guess which one was meant.
    assert "scroll-snap-type:x mandatory" in page
    # The still is a poster, not a player — the reel itself lives on Instagram.
    assert "https://x.test/r" in page


def test_the_feed_leaves_out_notes_that_are_not_written_yet(client):
    client.store.create_pending("https://x.test/pending")
    ready = client.store.create_pending("https://x.test/ready")
    client.store.save_note(ready, make_note(), transcript="t")

    page = client.get("/feed", params={"k": API_KEY}).text

    assert "The 50/30/20 budget rule" in page
    assert "https://x.test/pending" not in page


def test_the_feed_needs_the_key(client):
    assert client.get("/feed").status_code == 401


def test_a_browser_visit_trades_the_key_for_a_cookie(client):
    assert client.get("/", params={"k": API_KEY}).status_code == 200
    assert client.cookies.get(KEY_COOKIE) == API_KEY
    # The key has done its job: the cookie alone opens the page from here on.
    assert client.get("/").status_code == 200


def test_the_cookie_keeps_the_key_out_of_generated_links(client):
    client.store.create_pending("https://example.com/r/9")

    # Still on the visit that sets the cookie, so a browser refusing cookies
    # keeps working on ?k= alone rather than locking itself out.
    assert API_KEY in client.get("/", params={"k": API_KEY}).text

    once_cookied = client.get("/").text
    assert API_KEY not in once_cookied
    assert "?k=" not in once_cookied
    # A hidden field with an empty value would put a bare k= on every search.
    assert "name=k" not in once_cookied


def test_a_rejected_key_grants_no_cookie(client):
    assert client.get("/", params={"k": "wrong"}).status_code == 401
    assert KEY_COOKIE not in client.cookies


def test_the_shortcut_gets_no_cookie(client):
    # It is not a browser — it sends the header on every call and has no jar.
    response = client.post(
        "/ingest", json={"url": "https://example.com/r/9"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 202
    assert KEY_COOKIE not in client.cookies


def test_a_form_post_still_redirects_when_only_the_cookie_carries_the_key(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.get("/", params={"k": API_KEY})

    response = client.post(f"/notes/{note_id}/delete", follow_redirects=False)

    # Without the cookie this would look like an API call and answer in JSON.
    assert response.status_code == 303
    assert response.headers["location"] == "/"


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


def stock(client) -> None:
    for note in (
        make_note(),
        make_note(title="Kyoto in November", category="travel"),
        make_note(title="Cold brew ratio", category="food"),
    ):
        note_id = client.store.create_pending("https://example.com/r/x")
        client.store.save_note(note_id, note, transcript="shared transcript")


def test_api_filters_by_category(client):
    stock(client)

    listing = client.get("/api/notes", params={"category": "travel"},
                         headers={"X-API-Key": API_KEY}).json()

    assert [n["title"] for n in listing] == ["Kyoto in November"]


def test_api_exposes_the_categories_in_use(client):
    stock(client)

    counts = client.get("/api/categories", headers={"X-API-Key": API_KEY}).json()

    assert counts == [
        {"category": "finance", "count": 1},
        {"category": "food", "count": 1},
        {"category": "travel", "count": 1},
    ]


def test_chips_offer_only_categories_that_exist(client):
    stock(client)

    page = client.get("/", params={"k": API_KEY}).text

    assert "category=travel" in page
    assert "category=food" in page
    # Nothing is tagged fitness, so a fitness chip would be a dead end.
    assert "category=fitness" not in page


def test_a_chip_narrows_the_listing(client):
    stock(client)

    page = client.get("/", params={"k": API_KEY, "category": "travel"}).text

    assert "Kyoto in November" in page
    assert "The 50/30/20 budget rule" not in page
    assert "Cold brew ratio" not in page


def test_a_chip_keeps_the_current_search(client):
    stock(client)

    page = client.get("/", params={"k": API_KEY, "q": "shared"}).text

    # Every chip carries the query, so tapping one narrows instead of resetting.
    assert "q=shared&amp;category=travel" in page


def test_an_empty_category_says_so(client):
    stock(client)

    page = client.get("/", params={"k": API_KEY, "category": "fitness"}).text

    assert "Nothing in fitness yet." in page


def test_status_reports_what_is_wired_up(client):
    stock(client)

    body = client.get("/api/status", headers={"X-API-Key": API_KEY}).json()

    assert body["notes"] == {"ready": 3}
    assert body["asr_backend"] == "faster-whisper"
    assert body["instagram_dm"] is True
    assert body["push"] is False
    assert client.get("/api/status").status_code == 401


def test_a_failed_note_can_be_retried(client):
    note_id = client.store.create_pending("https://www.instagram.com/reel/ABC/")
    client.store.mark_failed(note_id, "ffmpeg blew up")

    response = client.post(f"/notes/{note_id}/retry", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    assert client.store.get(note_id)["status"] == "pending"
    assert client.started[0][1].page_url == "https://www.instagram.com/reel/ABC/"


def test_retrying_an_expired_dm_attachment_explains_itself(client):
    note_id = client.store.create_pending("instagram-dm:mid.123")
    client.store.mark_failed(note_id, "download failed")

    response = client.post(f"/notes/{note_id}/retry", headers={"X-API-Key": API_KEY})

    assert response.status_code == 400
    assert "Share it again" in response.json()["detail"]
    assert client.started == []


def test_retrying_a_missing_note_is_404(client):
    assert client.post("/notes/nope/retry", headers={"X-API-Key": API_KEY}).status_code == 404


def test_a_note_can_be_deleted_through_the_api(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="t")

    response = client.delete(f"/api/notes/{note_id}", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    assert client.store.get(note_id) is None
    assert client.delete(f"/api/notes/{note_id}",
                         headers={"X-API-Key": API_KEY}).status_code == 404


def test_deleting_from_the_page_returns_you_to_the_list(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="t")

    response = client.post(f"/notes/{note_id}/delete", params={"k": API_KEY},
                           follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/?k={API_KEY}"
    assert client.store.get(note_id) is None


def test_a_failed_note_offers_retry_and_delete(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.mark_failed(note_id, "login required")

    page = client.get(f"/notes/{note_id}", params={"k": API_KEY}).text

    assert f"/notes/{note_id}/retry" in page
    assert f"/notes/{note_id}/delete" in page
    assert "login required" in page


def test_a_finished_note_offers_delete_but_not_retry(client):
    note_id = client.store.create_pending("https://x.test/r")
    client.store.save_note(note_id, make_note(), transcript="t")

    page = client.get(f"/notes/{note_id}", params={"k": API_KEY}).text

    assert f"/notes/{note_id}/delete" in page
    assert "/retry" not in page
