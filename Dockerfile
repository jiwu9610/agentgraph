# Backend service image.
#
# Installs the docsbot package with the extras the HTTP service needs:
#   api     - FastAPI + uvicorn + JWT auth
#   persist - Chroma-backed persistent vector store
#   session - Redis client for chat sessions and rate limiting
#
# The dependency layer is built from pyproject.toml plus the package source, so
# code-only edits still rebuild quickly under Docker's layer cache.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY docsbot ./docsbot
RUN pip install ".[api,persist,session]"

# Document corpus ingested at startup / first use.
COPY corpus ./corpus

EXPOSE 8000

# The service exposes an app factory rather than a module-level app object;
# --factory tells uvicorn to call it once at startup.
CMD ["uvicorn", "--factory", "docsbot.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
