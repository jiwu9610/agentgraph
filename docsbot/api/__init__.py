"""The DocsBot HTTP service (FastAPI).

This package exposes the RAG library as a web service the React frontend
talks to:

    schemas.py  request/response shapes (Pydantic) — the API contract
    auth.py     username/password login -> signed JWT, and a bearer dependency
    app.py      the FastAPI app: /health, /auth/login, /ask, /ask/stream, /chat
"""
