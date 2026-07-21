"""Vector store search specs. The embedding call is stubbed out so the test is
deterministic and needs no API key — under test is the SEARCH logic (cosine
ranking), not Gemini's model.

The trick: map known texts to known vectors, then assert the right chunk wins.
"""

import numpy as np
import pytest

from docsbot import store as store_mod
from docsbot.ingest import Chunk
from docsbot.store import VectorStore


# A tiny fake "embedding space": each text maps to a hand-picked 3-D vector.
FAKE_VECTORS = {
    "cats are mammals": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    "dogs are mammals": np.array([0.9, 0.1, 0.0], dtype=np.float32),
    "the stock market fell": np.array([0.0, 0.0, 1.0], dtype=np.float32),
    # query:
    "tell me about felines": np.array([0.95, 0.05, 0.0], dtype=np.float32),
}


@pytest.fixture
def fake_embed(monkeypatch):
    def _fake(texts, task_type):  # signature matches store._embed
        return np.array([FAKE_VECTORS[t] for t in texts], dtype=np.float32)

    monkeypatch.setattr(store_mod, "_embed", _fake)


def test_search_ranks_by_similarity(fake_embed):
    s = VectorStore()
    s.add(
        [
            Chunk("cats are mammals", "animals.md", 0),
            Chunk("dogs are mammals", "animals.md", 1),
            Chunk("the stock market fell", "finance.md", 0),
        ]
    )
    hits = s.search("tell me about felines", k=2)

    assert len(hits) == 2
    # the cat chunk is closest to the feline query
    assert hits[0].chunk.text == "cats are mammals"
    # finance chunk should NOT be in the top 2
    assert "finance.md" not in {h.chunk.source for h in hits}


def test_empty_store_returns_no_hits():
    assert VectorStore().search("anything") == []


def test_len_tracks_added_chunks(fake_embed):
    s = VectorStore()
    s.add([Chunk("cats are mammals", "a.md", 0)])
    assert len(s) == 1
