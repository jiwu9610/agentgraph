"""Persistent vector store specs — persistence + incremental indexing.

These tests need Chroma but NOT a Gemini key: the embedder is stubbed, exactly
as in the in-memory store specs, so they exercise the storage logic, not the
network.

    pip install -e ".[dev,persist]"
    pytest -m phase8
"""

import numpy as np
import pytest

pytestmark = [pytest.mark.phase2, pytest.mark.phase8]

# Skip cleanly (instead of erroring) if the persist extra isn't installed yet.
pytest.importorskip("chromadb", reason="run: pip install -e '.[dev,persist]'")

from docsbot import store_persistent as sp_mod
from docsbot.ingest import Chunk
from docsbot.store_persistent import PersistentVectorStore, chunk_id


# A tiny deterministic "embedding space" so ranking is predictable.
FAKE_VECTORS = {
    "cats are mammals": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    "dogs are mammals": np.array([0.9, 0.1, 0.0], dtype=np.float32),
    "the stock market fell": np.array([0.0, 0.0, 1.0], dtype=np.float32),
    "tell me about felines": np.array([0.95, 0.05, 0.0], dtype=np.float32),
}


@pytest.fixture
def fake_embed(monkeypatch):
    """Replace the real embedder and count how many texts get embedded."""
    calls = {"texts": []}

    def _fake(texts, task_type):  # signature matches store._embed
        calls["texts"].extend(texts)
        return np.array([FAKE_VECTORS[t] for t in texts], dtype=np.float32)

    # The module imported `_embed` into its own namespace, so patch it there.
    monkeypatch.setattr(sp_mod, "_embed", _fake)
    return calls


def _animals():
    return [
        Chunk("cats are mammals", "animals.md", 0),
        Chunk("dogs are mammals", "animals.md", 1),
        Chunk("the stock market fell", "finance.md", 0),
    ]


# --- chunk_id ---------------------------------------------------------------

def test_chunk_id_is_stable_and_unique():
    c = Chunk("cats are mammals", "animals.md", 0)
    assert chunk_id(c) == chunk_id(c)  # deterministic across calls
    # Different content -> different id (source, index, AND text all matter).
    assert chunk_id(c) != chunk_id(Chunk("cats are mammals", "other.md", 0))
    assert chunk_id(c) != chunk_id(Chunk("cats are mammals", "animals.md", 1))
    assert chunk_id(c) != chunk_id(Chunk("DOGS are mammals", "animals.md", 0))


# --- search ranking (same guarantee as the in-memory store) -----------------

def test_search_ranks_by_similarity(tmp_path, fake_embed):
    store = PersistentVectorStore(db_path=str(tmp_path), collection="t_rank")
    store.add(_animals())
    hits = store.search("tell me about felines", k=2)

    assert len(hits) == 2
    assert hits[0].chunk.text == "cats are mammals"          # closest wins
    assert "finance.md" not in {h.chunk.source for h in hits}  # far one excluded
    # Scores are similarities: higher is better, and sorted descending.
    assert hits[0].score >= hits[1].score


# --- persistence: survives a "restart" --------------------------------------

def test_index_persists_across_new_store_instances(tmp_path, fake_embed):
    store = PersistentVectorStore(db_path=str(tmp_path), collection="t_persist")
    store.add(_animals())
    assert len(store) == 3

    # Simulate quitting and relaunching: a brand-new object, same path.
    reopened = PersistentVectorStore(db_path=str(tmp_path), collection="t_persist")
    assert len(reopened) == 3  # nothing was re-added, yet the data is still there
    hits = reopened.search("tell me about felines", k=1)
    assert hits[0].chunk.text == "cats are mammals"


# --- incremental indexing: don't re-embed unchanged chunks ------------------

def test_re_adding_same_chunks_does_not_re_embed(tmp_path, fake_embed):
    store = PersistentVectorStore(db_path=str(tmp_path), collection="t_incr")
    store.add(_animals())
    embedded_after_first = len(fake_embed["texts"])
    assert embedded_after_first == 3

    store.add(_animals())  # identical corpus, second pass
    assert len(store) == 3                              # no duplicates
    assert len(fake_embed["texts"]) == embedded_after_first  # zero new embeds


def test_only_changed_chunk_is_re_embedded(tmp_path, fake_embed):
    store = PersistentVectorStore(db_path=str(tmp_path), collection="t_change")
    store.add(_animals())
    before = len(fake_embed["texts"])

    # One file changed: same source+index, different text. The other two are
    # untouched and must be skipped.
    changed = _animals()
    changed[2] = Chunk("dogs are mammals", "finance.md", 0)  # was "the stock market fell"
    store.add(changed)

    assert len(fake_embed["texts"]) - before == 1  # exactly one re-embed
