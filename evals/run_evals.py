"""Evaluate RETRIEVAL quality, separately from answer quality.

We measure one thing: for each known question, does the chunk store surface
the document that actually contains the answer?

    hit-rate@k = (# questions where the right source is in the top-k) / (total)

Run it (needs a GEMINI_API_KEY, because it calls the real embedder):
    python evals/run_evals.py

Use it to tune knobs in config.py: chunk_size, chunk_overlap, top_k, embed_dim.
Change one, re-run, watch the number move.
"""

from __future__ import annotations

import json
from pathlib import Path

from docsbot.config import settings
from docsbot.rag import DocsBot

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "evals" / "golden.json").read_text())


def main() -> None:
    bot = DocsBot(ROOT / "docs")
    hits = 0

    print(f"Evaluating retrieval over {len(bot.store)} chunks, top_k={settings.top_k}\n")
    for case in GOLDEN:
        results = bot.store.search(case["question"])
        sources = {h.chunk.source for h in results}
        ok = case["expected_source"] in sources
        hits += ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case['question']}")
        print(f"        expected {case['expected_source']}, got {sorted(sources)}")

    rate = hits / len(GOLDEN)
    print(f"\nhit-rate@{settings.top_k} = {hits}/{len(GOLDEN)} = {rate:.0%}")


if __name__ == "__main__":
    main()
