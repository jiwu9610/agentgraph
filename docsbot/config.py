"""Single source of truth for every knob in the app.

Every tunable lives here and every other module imports from here, so a model
name or magic number is never defined twice. Change a value in one place and
the whole app follows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env once, when this module is first imported.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # --- Provider / models -------------------------------------------------
    # The free chat model. gemini-2.5-flash-lite is even cheaper/faster if you
    # blow through the free quota while iterating.
    chat_model: str = os.getenv("DOCSBOT_MODEL", "gemini-2.5-flash")

    # Gemini's embedding model. Free tier. Defaults to 3072 dims; we ask for a
    # smaller 768 to keep the in-memory store light (Phase 5).
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768

    # --- RAG knobs (Phases 4-6) -------------------------------------------
    chunk_size: int = 800        # characters per chunk
    chunk_overlap: int = 100     # characters shared between adjacent chunks
    top_k: int = 4               # how many chunks to retrieve per question

    # --- Phase 8: persistence ---------------------------------------------
    # Where the on-disk vector store lives. Chroma writes a small DB here so you
    # don't re-embed every run. Relative to the repo root by default.
    db_path: str = os.getenv("DOCSBOT_DB_PATH", ".docsbot_db")
    collection_name: str = "docsbot"

    # --- Phase 9: reliability ---------------------------------------------
    # Retry transient failures (HTTP 429 rate limits, 5xx) with exponential
    # backoff. These are the knobs; the policy is yours to implement.
    max_retries: int = 4
    backoff_base: float = 0.5    # seconds; delay = backoff_base * 2**attempt
    backoff_max: float = 8.0     # cap so a long backoff can't stall forever
    request_timeout: float = 30.0  # seconds per model call

    # Rough USD-per-million-token prices, for the cost ledger. The free tier
    # bills you nothing, but real engineers track spend from day one.
    usd_per_1m_input: float = 0.075
    usd_per_1m_output: float = 0.30

    # --- Phase 10: hybrid retrieval ---------------------------------------
    # Hybrid search blends keyword (BM25) and semantic (vector) rankings.
    # rrf_k is the smoothing constant in Reciprocal Rank Fusion.
    rrf_k: int = 60
    rerank_top_n: int = 8        # how many fused candidates to send to the reranker

    # --- Phase 11: the API + auth -----------------------------------------
    # Simple username/password auth. Override all three in your .env; NEVER ship
    # the defaults. The secret signs JWTs — change it and every token invalidates.
    auth_user: str = os.getenv("DOCSBOT_USER", "admin")
    auth_password: str = os.getenv("DOCSBOT_PASSWORD", "changeme")
    auth_secret: str = os.getenv("DOCSBOT_SECRET", "dev-only-insecure-secret")
    token_ttl_seconds: int = 3600

    # --- Redis: rate limiting + chat session history -----------------------
    # Local default for development; deployments (e.g. docker compose) inject
    # REDIS_URL. Both consumers fail open / degrade when Redis is unreachable.
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    # Fixed-window budget per authenticated user on the model-calling routes.
    rate_limit_max_requests: int = int(os.getenv("DOCSBOT_RATE_LIMIT_MAX_REQUESTS", "60"))
    rate_limit_window_seconds: int = int(os.getenv("DOCSBOT_RATE_LIMIT_WINDOW_SECONDS", "60"))
    # How long an idle chat session's history is kept.
    chat_session_ttl_seconds: int = int(os.getenv("DOCSBOT_CHAT_SESSION_TTL_SECONDS", "3600"))


    # --- Phase 13–17: knowledge-graph agent --------------------------------
    kg_db_path: str = os.getenv("DOCSBOT_KG_DB_PATH", ".docsbot_kg.sqlite")
    session_db_path: str = os.getenv("DOCSBOT_SESSION_DB_PATH", ".docsbot_sessions.sqlite")
    # Prefer a cheaper/faster model for bulk extraction; keep chat_model for answers.
    extract_model: str = os.getenv("DOCSBOT_EXTRACT_MODEL", "gemini-2.5-flash-lite")
    agent_max_hops: int = int(os.getenv("DOCSBOT_AGENT_MAX_HOPS", "6"))
    agent_max_tool_calls: int = int(os.getenv("DOCSBOT_AGENT_MAX_TOOL_CALLS", "12"))
    agent_max_usd: float = float(os.getenv("DOCSBOT_AGENT_MAX_USD", "0.05"))

    # --- Phase 17: Phoenix / OpenTelemetry --------------------------------
    phoenix_project: str = os.getenv("PHOENIX_PROJECT_NAME", "docsbot-kg")
    phoenix_collector_endpoint: str = os.getenv(
        "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
    )

    # --- Phase 18–22: performance, cost, and reliability budgets -----------
    # A cheap model for work that doesn't need the flagship: classification,
    # routing, yes/no guards. Using the expensive model for a one-word answer
    # is the most common line item on a surprising bill.
    cheap_model: str = os.getenv("DOCSBOT_CHEAP_MODEL", "gemini-2.5-flash-lite")

    answer_cache_ttl_s: float = 300.0
    session_ttl_s: float = 3600.0
    max_sessions: int = 1000
    # How many prior turns to resend as context. Resending the whole transcript
    # makes token cost grow quadratically with conversation length.
    history_window_turns: int = 3

    # How many candidates the reranker may score concurrently. Serial reranking
    # is the difference between one round-trip and `rerank_top_n` of them.
    rerank_concurrency: int = 8

    # Circuit breaker: trip after this many consecutive provider failures.
    breaker_threshold: int = 5
    breaker_reset_after_s: float = 30.0

    # --- Phase 22: the regression gate -------------------------------------
    # Budgets the CI perf gate enforces. Numbers, not vibes.
    # The latency budget alone is env-overridable: wall clock depends on the
    # machine, so shared CI runners get a documented tolerance (see ci.yml),
    # while the call/token/cost budgets are deterministic and stay exact
    # everywhere. The 0.130 s default is calibrated on a quiet 4-core node.
    budget_ask_p95_s: float = float(os.getenv("DOCSBOT_BUDGET_ASK_P95_S", "0.130"))
    budget_ask_input_tokens: int = 3200
    budget_ask_provider_calls: int = 4
    budget_ask_usd: float = 0.00034

    @property
    def api_key(self) -> str:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and paste "
                "a free key from https://aistudio.google.com/apikey"
            )
        return key


settings = Settings()
