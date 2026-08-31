"""Meaning-based search: the half of retrieval that keyword matching misses."""

from __future__ import annotations

import math
from dataclasses import replace

from app import embed as embed_mod
from app.store import Store

from .conftest import make_note


def _stub(monkeypatch, vectors: dict[str, list[float]]):
    """Stand in for the endpoint, keyed on the text handed to it."""
    def fake(texts, *, model, base_url, api_key, query=False):
        return [vectors[t] for t in texts]
    monkeypatch.setattr(embed_mod, "embed", fake)


def test_a_vector_survives_the_round_trip():
    vector = [0.5, -0.25, 0.125, 1.0]
    assert embed_mod.from_blob(embed_mod.to_blob(vector)) == vector


def test_cosine_knows_same_from_opposite():
    assert math.isclose(embed_mod.cosine([1, 0], [1, 0]), 1.0)
    assert math.isclose(embed_mod.cosine([1, 0], [0, 1]), 0.0, abs_tol=1e-9)
    assert embed_mod.cosine([1, 0], [-1, 0]) < 0
    # A stored vector of a different width is a bug, not a match.
    assert embed_mod.cosine([1, 0], [1, 0, 0]) == 0.0


def test_the_transcript_stays_out_of_the_vector():
    """It is long, usually empty on these reels, and averaging it in drags the
    note toward whatever the creator rambled about."""
    text = embed_mod.note_text(make_note().model_dump() | {"transcript": "unrelated chatter"})
    assert "50/30/20" in text
    assert "unrelated chatter" not in text


def test_a_note_gets_a_vector_when_it_is_written(settings, tmp_path, monkeypatch):
    settings = replace(settings, embed_model="a-model")
    store = Store(str(tmp_path / "n.sqlite3"))
    note_id = store.create_pending("https://x.test/r")
    store.save_note(note_id, make_note(), transcript="t")
    note = store.get(note_id)

    _stub(monkeypatch, {embed_mod.note_text(note): [1.0, 0.0, 0.0]})
    assert embed_mod.embed_note(note, settings, store) is True

    assert store.embeddings() == [(note_id, embed_mod.to_blob([1.0, 0.0, 0.0]))]


def test_an_embedding_failure_does_not_lose_the_note(settings, tmp_path, monkeypatch):
    """Search degrades for one note. Nothing else may break."""
    settings = replace(settings, embed_model="a-model")
    store = Store(str(tmp_path / "n.sqlite3"))
    note_id = store.create_pending("https://x.test/r")
    store.save_note(note_id, make_note(), transcript="t")

    def boom(*a, **k):
        raise embed_mod.EmbeddingError("endpoint down")
    monkeypatch.setattr(embed_mod, "embed", boom)

    assert embed_mod.embed_note(store.get(note_id), settings, store) is False
    assert store.get(note_id)["title"] == "The 50/30/20 budget rule"
    assert store.embeddings() == []


def test_ranking_puts_the_closest_note_first(settings, tmp_path, monkeypatch):
    settings = replace(settings, embed_model="a-model")
    store = Store(str(tmp_path / "n.sqlite3"))
    ids = {}
    for name, vec in (("near", [1.0, 0.0]), ("far", [0.0, 1.0])):
        nid = store.create_pending(f"https://x.test/{name}")
        store.save_note(nid, make_note(title=name), transcript="t")
        store.set_embedding(nid, embed_mod.to_blob(vec))
        ids[name] = nid

    _stub(monkeypatch, {"anything": [0.96, 0.28]})
    ranked = embed_mod.rank("anything", settings, store)

    assert ranked[0] == ids["near"]


def test_nothing_close_enough_returns_nothing(settings, tmp_path, monkeypatch):
    """Without a floor the nearest note is always returned, however unrelated,
    and a search for something you never saved comes back confidently wrong."""
    settings = replace(settings, embed_model="a-model")
    store = Store(str(tmp_path / "n.sqlite3"))
    nid = store.create_pending("https://x.test/r")
    store.save_note(nid, make_note(), transcript="t")
    store.set_embedding(nid, embed_mod.to_blob([1.0, 0.0]))

    _stub(monkeypatch, {"unrelated": [0.0, 1.0]})
    assert embed_mod.rank("unrelated", settings, store) == []


def test_with_no_model_configured_it_stays_out_of_the_way(settings, tmp_path):
    """embed_model empty means the service behaves exactly as it did before."""
    store = Store(str(tmp_path / "n.sqlite3"))
    assert embed_mod.rank("anything", settings, store) == []
    assert embed_mod.embed_note({"id": "x", "title": "t"}, settings, store) is False


def test_vectors_never_reach_a_json_response(tmp_path):
    """SELECT * would carry the blob into /api/notes and fail to serialise."""
    store = Store(str(tmp_path / "n.sqlite3"))
    note_id = store.create_pending("https://x.test/r")
    store.save_note(note_id, make_note(), transcript="t")
    store.set_embedding(note_id, embed_mod.to_blob([1.0, 0.0]))

    for note in (store.get(note_id), *store.recent(10), *store.by_ids([note_id])):
        assert "embedding" not in note
