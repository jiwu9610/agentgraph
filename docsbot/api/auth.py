"""Bearer-token authentication for the API.

`POST /auth/login` exchanges a username/password pair (checked against
``settings.auth_user`` / ``settings.auth_password``) for a signed JWT. Every
protected route then requires ``Authorization: Bearer <token>``; the token is
verified locally by signature + expiry (HS256 with ``settings.auth_secret``),
so no session store or database lookup is needed.

Failure modes are deliberately uniform: a wrong password, a missing header, a
forged token, and an expired token all produce a plain 401 — never a 500, and
never a message that reveals which check failed. Passwords and the signing
secret are never logged or returned.
"""

from __future__ import annotations

import hmac
import time

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings

_ALGORITHM = "HS256"

# auto_error=False: a missing/malformed header yields `creds is None` so the
# dependency below controls the 401 response, not the framework.
bearer_scheme = HTTPBearer(auto_error=False)


def authenticate(username: str, password: str) -> bool:
    """Return True iff the credentials match the configured user.

    Both fields are compared with ``hmac.compare_digest`` so the comparison
    time does not leak how much of the value matched.
    """
    user_ok = hmac.compare_digest(username.encode(), settings.auth_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), settings.auth_password.encode())
    return user_ok and pass_ok


def create_token(username: str) -> str:
    """Return a signed JWT for `username` expiring in ``token_ttl_seconds``."""
    payload = {
        "sub": username,
        "exp": int(time.time()) + settings.token_ttl_seconds,
    }
    return jwt.encode(payload, settings.auth_secret, algorithm=_ALGORITHM)


def verify_token(token: str) -> str | None:
    """Return the username if `token` is valid and unexpired, else None.

    An invalid signature, a malformed token, or an expired token is an
    expected condition, not an error: it returns None so the caller can map
    it to a clean 401.
    """
    try:
        payload = jwt.decode(token, settings.auth_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """FastAPI dependency: enforce a valid bearer token; return the username.

    Routes become protected by declaring ``user: str = Depends(require_auth)``.
    Raises HTTPException(401) when the header is absent or the token fails
    verification.
    """
    if creds is not None:
        username = verify_token(creds.credentials)
        if username is not None:
            return username
    raise HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
