"""Pure logic — no API, no key. These run instantly."""

from docsbot.ingest import Chunk, chunk_text
from docsbot.config import settings


def test_short_text_is_one_chunk():
    chunks = chunk_text("hello world", source="a.md")
    assert len(chunks) == 1
    assert chunks[0] == Chunk(text="hello world", source="a.md", index=0)


def test_long_text_splits_into_multiple_chunks():
    text = "x" * (settings.chunk_size * 3)
    chunks = chunk_text(text, source="big.md")
    assert len(chunks) >= 3
    # indices are sequential starting at 0
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunks_overlap():
    # Two adjacent chunks should share the overlap region.
    text = "".join(str(i % 10) for i in range(settings.chunk_size * 2))
    chunks = chunk_text(text, source="o.md")
    tail = chunks[0].text[-settings.chunk_overlap :]
    head = chunks[1].text[: settings.chunk_overlap]
    assert tail == head


def test_source_is_recorded():
    chunks = chunk_text("some content here", source="notes.md")
    assert all(c.source == "notes.md" for c in chunks)
