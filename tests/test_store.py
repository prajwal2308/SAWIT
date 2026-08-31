
from .conftest import bound_store, make_note


def test_pending_note_is_created_then_completed(settings):
    store = bound_store(settings.db_path)
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
    store = bound_store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/2")
    store.mark_failed(note_id, "login required")

    saved = store.get(note_id)
    assert saved["status"] == "failed"
    assert saved["error"] == "login required"


def test_search_matches_transcript_and_steps(settings):
    store = bound_store(settings.db_path)
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
    store = bound_store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/3")
    store.save_note(note_id, make_note(), transcript="anything")

    # A stray quote is treated as text, not as FTS5 syntax.
    assert store.search('budget"') == store.search("budget")
    # Multiple terms narrow rather than widen.
    assert len(store.search("budget rule")) == 1
    assert store.search("budget kyoto") == []
    assert store.search("   ") == []


def test_reprocessing_does_not_duplicate_search_hits(settings):
    store = bound_store(settings.db_path)
    note_id = store.create_pending("https://example.com/reel/4")
    store.save_note(note_id, make_note(), transcript="first pass")
    store.save_note(note_id, make_note(), transcript="second pass")

    assert len(store.search("budget")) == 1


def test_recent_is_newest_first(settings):
    store = bound_store(settings.db_path)
    ids = [store.create_pending(f"https://example.com/reel/{i}") for i in range(3)]
    for note_id in ids:
        store.save_note(note_id, make_note(), transcript="x")

    assert [n["id"] for n in store.recent()] == list(reversed(ids))


def stock_the_store(store) -> None:
    notes = [
        make_note(),
        make_note(title="Index funds explained"),
        make_note(title="Kyoto in November", category="travel", tags=["japan"]),
        make_note(title="Cold brew ratio", category="food", tags=["coffee"]),
    ]
    for index, note in enumerate(notes):
        note_id = store.create_pending(f"https://example.com/reel/{index}")
        store.save_note(note_id, note, transcript="shared transcript text")


def test_recent_filters_by_category(settings):
    store = bound_store(settings.db_path)
    stock_the_store(store)

    assert len(store.recent()) == 4
    assert len(store.recent(category="finance")) == 2
    assert [n["title"] for n in store.recent(category="travel")] == ["Kyoto in November"]
    assert store.recent(category="fitness") == []


def test_search_and_category_narrow_together(settings):
    store = bound_store(settings.db_path)
    stock_the_store(store)

    # The transcript is shared, so search alone matches everything.
    assert len(store.search("shared")) == 4
    assert len(store.search("shared", category="finance")) == 2
    assert store.search("kyoto", category="finance") == []
    assert [n["title"] for n in store.search("kyoto", category="travel")] == [
        "Kyoto in November"
    ]


def test_category_counts_are_biggest_first(settings):
    store = bound_store(settings.db_path)
    stock_the_store(store)

    assert store.category_counts() == [("finance", 2), ("food", 1), ("travel", 1)]


def test_pending_notes_have_no_category_to_count(settings):
    store = bound_store(settings.db_path)
    store.create_pending("https://example.com/reel/pending")

    assert store.category_counts() == []
    # ...but they still show up in the unfiltered list, so nothing is hidden.
    assert len(store.recent()) == 1


def test_a_category_filter_cannot_be_injected(settings):
    store = bound_store(settings.db_path)
    stock_the_store(store)

    assert store.recent(category="finance' OR '1'='1") == []


def test_a_restart_does_not_leave_notes_stuck_working(settings):
    store = bound_store(settings.db_path)
    orphan = store.create_pending("https://example.com/reel/interrupted")
    done = store.create_pending("https://example.com/reel/done")
    store.save_note(done, make_note(), transcript="finished")

    assert store.recover_orphans() == 1

    assert store.get(orphan)["status"] == "failed"
    assert "Retry" in store.get(orphan)["error"]
    # A finished note is not touched.
    assert store.get(done)["status"] == "ready"


def test_recovery_is_a_no_op_when_nothing_was_in_flight(settings):
    store = bound_store(settings.db_path)
    store.save_note(store.create_pending("https://x.test/r"), make_note(), transcript="t")

    assert store.recover_orphans() == 0


def test_a_failed_note_can_be_reset_for_another_run(settings):
    store = bound_store(settings.db_path)
    note_id = store.create_pending("https://x.test/r")
    store.mark_failed(note_id, "login required")

    assert store.reset_to_pending(note_id) is True

    note = store.get(note_id)
    assert note["status"] == "pending"
    assert note["error"] is None
    assert store.reset_to_pending("nope") is False


def test_deleting_a_note_also_drops_it_from_search(settings):
    store = bound_store(settings.db_path)
    note_id = store.create_pending("https://x.test/r")
    store.save_note(note_id, make_note(), transcript="fifty thirty twenty")
    assert len(store.search("budget")) == 1

    assert store.delete(note_id) is True

    assert store.get(note_id) is None
    assert store.search("budget") == []
    assert store.delete(note_id) is False


def test_status_counts_cover_every_state(settings):
    store = bound_store(settings.db_path)
    store.save_note(store.create_pending("https://x.test/1"), make_note(), transcript="t")
    store.mark_failed(store.create_pending("https://x.test/2"), "boom")
    store.create_pending("https://x.test/3")

    assert store.status_counts() == {"ready": 1, "failed": 1, "pending": 1}
