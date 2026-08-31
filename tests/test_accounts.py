"""Signing in, and the upgrade that must not lose anyone's notes."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import accounts
from app.config import get_settings
from app.main import (
    KEY_COOKIE,
    OWNER_EMAIL,
    SESSION_COOKIE,
    app,
    base_store,
    bootstrap_owner,
)
from app.store import Store

from .conftest import API_KEY, make_note

PASSWORD = "correct-horse-battery"


@pytest.fixture
def web(settings):
    store = Store(settings.db_path)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[base_store] = lambda: store
    client = TestClient(app)
    client.store = store
    yield client
    app.dependency_overrides.clear()


# ---- passwords and sessions ----

def test_a_password_verifies_only_against_itself():
    stored = accounts.hash_password(PASSWORD)
    assert accounts.verify_password(PASSWORD, stored)
    assert not accounts.verify_password(PASSWORD + "x", stored)
    # Two hashes of one password differ: the salt is doing its job.
    assert stored != accounts.hash_password(PASSWORD)


def test_a_mangled_hash_is_a_failure_not_a_crash():
    for junk in ("", "nonsense", "scrypt$notanumber$8$1$aaa$bbb", "md5$1$1$1$aa$bb"):
        assert accounts.verify_password(PASSWORD, junk) is False


def test_a_session_survives_a_round_trip_but_not_tampering():
    token = accounts.sign_session("user-1", "server-secret")
    assert accounts.read_session(token, "server-secret") == "user-1"
    # Another server's secret must not vouch for it.
    assert accounts.read_session(token, "different-secret") is None
    # Nor a payload someone edited.
    body, sig = token.split(".", 1)
    assert accounts.read_session(f"{body}x.{sig}", "server-secret") is None


def test_an_expired_session_is_refused():
    old = accounts.sign_session("user-1", "s", now=time.time() - accounts.SESSION_TTL - 10)
    assert accounts.read_session(old, "s") is None


# ---- the upgrade path ----

def test_the_existing_key_and_notes_survive_the_upgrade(settings):
    """SAWIT_API_KEY is already in somebody's Shortcut, and their notes predate
    accounts entirely. Neither may be lost by deploying this."""
    store = Store(settings.db_path)
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO notes (id, user_id, url, status, created_at, updated_at, title) "
            "VALUES ('old','','https://x.test/old','ready','2026-01-01','2026-01-01','Old note')"
        )

    bootstrap_owner(store, settings)

    owner = store.user_by_api_key(settings.api_key)
    assert owner is not None and owner["email"] == OWNER_EMAIL
    assert [n["title"] for n in store.for_user(owner["id"]).recent()] == ["Old note"]


def test_bootstrap_runs_once(settings):
    store = Store(settings.db_path)
    bootstrap_owner(store, settings)
    bootstrap_owner(store, settings)
    assert store.user_count() == 1


# ---- signing in ----

def test_signing_up_lands_on_the_setup_not_an_empty_list(web):
    """A new account has no notes, so the note list is a dead end. Send them to
    the thing that makes the app do something."""
    response = web.post("/signup", data={"email": "New@Test.com", "password": PASSWORD},
                        follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/welcome"
    assert web.cookies.get(SESSION_COOKIE)
    assert web.get("/").status_code == 200
    # Stored lowercased, so casing cannot create a second account.
    assert web.store.user_by_email("new@test.com") is not None


def test_the_same_email_cannot_be_taken_twice(web):
    web.post("/signup", data={"email": "a@test.com", "password": PASSWORD})
    again = web.post("/signup", data={"email": "A@TEST.com", "password": PASSWORD},
                     follow_redirects=False)
    assert "already+registered" in again.headers["location"]
    assert web.store.user_count() == 1


def test_a_weak_password_is_refused(web):
    response = web.post("/signup", data={"email": "a@test.com", "password": "short"},
                        follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert web.store.user_count() == 0


def test_a_wrong_password_and_an_unknown_account_fail_alike(web):
    web.post("/signup", data={"email": "a@test.com", "password": PASSWORD})
    web.post("/logout")

    wrong = web.post("/login", data={"email": "a@test.com", "password": "nope"},
                     follow_redirects=False)
    unknown = web.post("/login", data={"email": "ghost@test.com", "password": PASSWORD},
                       follow_redirects=False)

    # Identical answers: which half was wrong is not something to leak.
    assert wrong.headers["location"] == unknown.headers["location"]


def test_signing_out_clears_both_ways_in(web):
    """Dropping only the session would leave the API-key cookie signing you
    back in on the next request."""
    web.post("/signup", data={"email": "a@test.com", "password": PASSWORD})
    web.post("/logout")

    assert not web.cookies.get(SESSION_COOKIE)
    assert not web.cookies.get(KEY_COOKIE)
    assert web.get("/", follow_redirects=False).status_code == 401


def test_your_key_is_yours_and_reaches_only_your_notes(web, settings):
    bootstrap_owner(web.store, settings)
    owner = web.store.user_by_api_key(settings.api_key)
    theirs = web.store.for_user(owner["id"]).create_pending("https://x.test/owned")
    web.store.for_user(owner["id"]).save_note(theirs, make_note(title="Owner note"),
                                              transcript="t")

    web.post("/signup", data={"email": "new@test.com", "password": PASSWORD})
    mine = web.get("/api/notes").json()

    assert mine == [], "a fresh account starts empty, not with somebody else's library"
    # And the owner's key still reaches the owner's note.
    assert web.get("/api/notes", headers={"X-API-Key": API_KEY}).json()[0]["title"] \
        == "Owner note"


def test_a_key_that_matches_no_account_is_refused(web):
    assert web.get("/api/notes", headers={"X-API-Key": "not-anybodys-key"}).status_code == 401


def test_the_bootstrapped_account_can_claim_itself(web, settings):
    """It holds the notes but has no password, so without this its library is
    reachable only through a URL with a key in it."""
    bootstrap_owner(web.store, settings)
    owner = web.store.user_by_api_key(settings.api_key)
    note = web.store.for_user(owner["id"]).create_pending("https://x.test/mine")
    web.store.for_user(owner["id"]).save_note(note, make_note(title="Mine"), transcript="t")

    response = web.post("/account/credentials",
                        data={"email": "me@test.com", "password": PASSWORD},
                        headers={"X-API-Key": API_KEY}, follow_redirects=False)
    assert response.status_code == 303

    web.cookies.clear()
    web.post("/login", data={"email": "me@test.com", "password": PASSWORD})

    # Same account, same notes — not a new empty one.
    assert [n["title"] for n in web.get("/api/notes").json()] == ["Mine"]
    assert web.store.user_count() == 1


def test_claiming_cannot_steal_an_email_in_use(web, settings):
    bootstrap_owner(web.store, settings)
    web.post("/signup", data={"email": "taken@test.com", "password": PASSWORD})
    web.cookies.clear()

    response = web.post("/account/credentials",
                        data={"email": "taken@test.com", "password": PASSWORD},
                        headers={"X-API-Key": API_KEY}, follow_redirects=False)

    assert "another+account" in response.headers["location"]


def test_the_account_page_leads_with_the_one_tap_installer(web, settings):
    """Building the Shortcut by hand is where people give up. When a deployment
    has an iCloud link, that is what should be in front of them."""
    from dataclasses import replace as dc_replace

    link = "https://www.icloud.com/shortcuts/deadbeef"
    app.dependency_overrides[get_settings] = lambda: dc_replace(
        settings, shortcut_url=link)
    bootstrap_owner(web.store, settings)

    page = web.get("/account", headers={"X-API-Key": API_KEY}).text

    assert link in page
    assert "Add the Sawit shortcut" in page
    # The manual recipe stays, folded away rather than removed.
    assert "Or build it by hand" in page


def test_without_a_link_it_says_how_to_make_one(web, settings):
    bootstrap_owner(web.store, settings)
    page = web.get("/account", headers={"X-API-Key": API_KEY}).text
    assert "SAWIT_SHORTCUT_URL" in page


def test_the_welcome_page_carries_your_key_and_the_installer(web, settings):
    from dataclasses import replace as dc_replace

    link = "https://www.icloud.com/shortcuts/deadbeef"
    app.dependency_overrides[get_settings] = lambda: dc_replace(settings, shortcut_url=link)
    web.post("/signup", data={"email": "new@test.com", "password": PASSWORD})

    page = web.get("/welcome").text
    key = web.store.user_by_email("new@test.com")["api_key"]

    assert link in page
    assert key in page, "the key has to be on the page they are told to paste it from"
    assert "Add to Home Screen" in page


def test_a_shortcut_url_that_is_not_a_url_is_ignored(monkeypatch):
    """Turning the installer off by typing something in the box must not leave
    a broken link on the page every new account sees."""
    from app.config import get_settings

    for junk in ("", "   ", "disabled", "none", "PASTE-A-LINK"):
        monkeypatch.setenv("SAWIT_API_KEY", "k" * 20)
        monkeypatch.setenv("SAWIT_SHORTCUT_URL", junk)
        get_settings.cache_clear()
        assert get_settings().shortcut_url is None, junk

    monkeypatch.setenv("SAWIT_SHORTCUT_URL", "https://www.icloud.com/shortcuts/abc")
    get_settings.cache_clear()
    assert get_settings().shortcut_url == "https://www.icloud.com/shortcuts/abc"
    get_settings.cache_clear()
