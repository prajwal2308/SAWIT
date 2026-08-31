"""One account must never be able to reach another's notes.

SQLite has no row-level security, so this is the test that stands in for it.
It exercises the store's whole note surface against two accounts rather than
trusting that each query remembered its filter.
"""

from __future__ import annotations

import inspect

import pytest

from app import accounts
from app.store import Store

from .conftest import make_note


@pytest.fixture
def two_users(tmp_path):
    base = Store(str(tmp_path / "notes.sqlite3"))
    alice = base.create_user("alice@test", accounts.hash_password("correct-horse-1"),
                             "key-alice")
    bob = base.create_user("bob@test", accounts.hash_password("correct-horse-2"), "key-bob")
    return base, base.for_user(alice), base.for_user(bob)


def _note(store, title):
    note_id = store.create_pending(f"https://x.test/{title}")
    store.save_note(note_id, make_note(title=title), transcript=f"{title} transcript")
    return note_id


def test_every_note_read_is_scoped(two_users):
    _, a, b = two_users
    mine = _note(a, "alice-note")
    _note(b, "bob-note")

    assert [n["title"] for n in a.recent()] == ["alice-note"]
    assert [n["title"] for n in a.search("transcript")] == ["alice-note"]
    assert a.by_ids([mine]) and [n["title"] for n in a.by_ids([mine])] == ["alice-note"]
    assert a.category_counts() == [("finance", 1)]
    assert a.status_counts() == {"ready": 1}


def test_a_note_id_from_another_account_is_simply_not_there(two_users):
    """Guessing an id must be indistinguishable from the note not existing."""
    _, a, b = two_users
    theirs = _note(b, "bob-note")

    assert a.get(theirs) is None
    assert a.get_thumbnail(theirs) is None
    assert a.by_ids([theirs]) == []
    assert a.delete(theirs) is False
    assert a.reset_to_pending(theirs) is False

    # And none of that touched it.
    assert b.get(theirs)["title"] == "bob-note"


def test_writes_cannot_reach_across_accounts(two_users):
    _, a, b = two_users
    theirs = _note(b, "bob-note")

    a.save_note(theirs, make_note(title="overwritten"), transcript="hijacked")
    a.mark_failed(theirs, "hijacked")
    a.set_embedding(theirs, b"\x00\x00\x80?")

    still = b.get(theirs)
    assert still["title"] == "bob-note"
    assert still["status"] == "ready"
    assert b.embeddings() == []


def test_dedup_does_not_leak_that_someone_else_saved_it(two_users):
    """find_written must not reveal another account's note for the same URL —
    it would hand back an id that account cannot open."""
    _, a, b = two_users
    b_id = b.create_pending("https://x.test/same")
    b.save_note(b_id, make_note(), transcript="t")

    assert b.find_written("https://x.test/same") == b_id
    assert a.find_written("https://x.test/same") is None


def test_embeddings_and_backfill_are_scoped(two_users):
    _, a, b = two_users
    _note(a, "alice-note")
    _note(b, "bob-note")

    assert [n["title"] for n in a.awaiting_embedding()] == ["alice-note"]
    a.set_embedding(a.recent()[0]["id"], b"\x00\x00\x80?")
    assert len(a.embeddings()) == 1
    assert b.embeddings() == []


def test_an_unbound_store_refuses_to_answer(tmp_path):
    """The failure mode that matters is silently returning everyone's notes."""
    store = Store(str(tmp_path / "notes.sqlite3"))
    for call in (lambda: store.recent(), lambda: store.get("x"),
                 lambda: store.create_pending("https://x.test/r"),
                 lambda: store.search("x"), lambda: store.category_counts()):
        with pytest.raises(RuntimeError, match="not bound to an account"):
            call()


def test_no_note_method_forgets_the_filter(two_users):
    """A guard against the next method added in a hurry: anything reading or
    writing notes must mention user_id in its SQL."""
    _, a, _ = two_users
    exempt = {"recover_orphans", "adopt_orphan_notes"}  # deliberately global
    unscoped = []
    for name, method in inspect.getmembers(Store, inspect.isfunction):
        if name.startswith("_") or name in exempt or name.startswith("user_"):
            continue
        if name in {"create_user", "for_user"}:
            continue
        src = inspect.getsource(method)
        if "notes" in src and "user_id" not in src:
            unscoped.append(name)
    assert unscoped == [], f"these touch notes without scoping: {unscoped}"


def test_notes_written_before_accounts_existed_find_their_owner(tmp_path):
    base = Store(str(tmp_path / "notes.sqlite3"))
    with base._conn() as conn:  # a row from the single-user era
        conn.execute(
            "INSERT INTO notes (id, user_id, url, status, created_at, updated_at) "
            "VALUES ('old','', 'https://x.test/old','ready','2026-01-01','2026-01-01')"
        )
    owner = base.create_user("owner@test", accounts.hash_password("correct-horse-1"), "k")

    assert base.adopt_orphan_notes(owner) == 1
    assert [n["id"] for n in base.for_user(owner).recent()] == ["old"]
