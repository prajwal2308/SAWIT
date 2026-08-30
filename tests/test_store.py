from app.store import Store

from .conftest import make_note


def test_pending_note_is_created_then_completed(settings):
    store = Store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/1")

    assert store.get(note_id)["status"] == "pending"

    store.save_note(note_id, make_note(), transcript="fifty thirty twenty", duration=41.0)

    saved = store.get(note_id)
    assert saved["status"] == "ready"
    assert saved["title"] == "The 50/30/20 budget rule"
    assert saved["steps"] == ["Start from take-home pay", "Multiply by 0.5 for needs"]
    assert saved["key_facts"] == [{"label": "Savings share", "value": "20%"}]
    assert saved["has_thumbnail"] is False


def test_failed_note_records_the_error(settings):
    store = Store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/2")
    store.mark_failed(note_id, "login required")

    saved = store.get(note_id)
    assert saved["status"] == "failed"
    assert saved["error"] == "login required"


def test_search_matches_transcript_and_steps(settings):
    store = Store(settings.db_path)
    for index, note in enumerate(
        [make_note(), make_note(title="Kyoto in November", category="travel", tags=["japan"])]
    ):
        note_id = store.create_pending(f"https://example.com/reel/{index}")
        store.save_note(note_id, note, transcript="momiji season is late november")

    assert [n["title"] for n in store.search("budget")] == ["The 50/30/20 budget rule"]
    assert [n["title"] for n in store.search("japan")] == ["Kyoto in November"]
    # Body text is indexed too, not just the title.
    assert len(store.search("momiji")) == 2
    assert store.search("nothingmatchesthis") == []


def test_search_does_not_choke_on_fts_syntax(settings):
    store = Store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/3")
    store.save_note(note_id, make_note(), transcript="anything")

    # A stray quote is treated as text, not as FTS5 syntax.
    assert store.search('budget"') == store.search("budget")
    # Multiple terms narrow rather than widen.
    assert len(store.search("budget rule")) == 1
    assert store.search("budget kyoto") == []
    assert store.search("   ") == []


def test_reprocessing_does_not_duplicate_search_hits(settings):
    store = Store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/4")
    store.save_note(note_id, make_note(), transcript="first pass")
    store.save_note(note_id, make_note(), transcript="second pass")

    assert len(store.search("budget")) == 1


def test_recent_is_newest_first(settings):
    store = Store(settings.db_path)
    ids = [store.create_pending(f"https://example.com/reel/{i}") for i in range(3)]
    for note_id in ids:
        store.save_note(note_id, make_note(), transcript="x")

    assert [n["id"] for n in store.recent()] == list(reversed(ids))
