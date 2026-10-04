"""FastAPI dependencies shared by the routers: the signed-in user (bearer token) and admin authorization
(the break-glass admin key, or a signed-in user whose role is 'admin')."""
import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException

from .. import auth  # service layer (app/auth.py)
from ..core.config import settings
from ..core.security import verify_token
from ..db import db


def access_claims(authorization: str | None) -> dict | None:
    """Validated access-token claims, or None. Does not look at the session (logout must work with stale tokens)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    data = verify_token(authorization[7:].strip())
    return data if data and data.get("typ") == "access" and data.get("sid") else None


def current_user(authorization: str | None = Header(default=None)) -> dict:
    data = access_claims(authorization)
    if not data:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    with db() as con:  # logout / password reset / account deletion end access immediately
        active = auth.session_active(con, data["sid"], data["sub"])
    if not active:
        raise HTTPException(401, "Session ended", headers={"WWW-Authenticate": "Bearer"})
    return data


def _admin_key_ok(supplied: str | None) -> bool:
    # bytes + compare_digest: constant-time, and a non-ASCII header value cannot raise TypeError (-> 500)
    return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8", "replace"), settings.admin_key.encode("utf-8"))


@dataclass(frozen=True)
class AdminActor:
    """Who is performing an admin action. ``label`` goes into audit_log.actor; ``user_id`` is None for the admin key."""

    label: str
    user_id: int | None = None


KEY_ACTOR = AdminActor("admin-key")


def _admin_user(authorization: str | None) -> AdminActor | None:
    """The signed-in admin-role user behind a Bearer access token, or None. The role and the disabled flag are read
    from the database on every request, so removing a role or disabling an account takes effect immediately."""
    data = access_claims(authorization)
    if not data:
        return None
    with db() as con:
        if not auth.session_active(con, data["sid"], data["sub"]):
            return None
        row = con.execute("SELECT role, disabled_at FROM users WHERE id=?", (data["sub"],)).fetchone()
    if row is None or row["role"] != "admin" or row["disabled_at"] is not None:
        return None
    return AdminActor(f"user:{data['sub']}", int(data["sub"]))


def require_admin(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> AdminActor:
    """Admin authorization: ``X-Admin-Key`` (break-glass, also how the first admin is appointed) or a Bearer access
    token of a user with role 'admin'. Anything else is 403 (never 401 / never a hint which part was wrong)."""
    if x_admin_key:  # a key was offered: judge only the key (an empty header counts as no key)
        if not settings.admin_key:
            raise HTTPException(403, "Admin API disabled (set MAVE_ADMIN_KEY)")
        if not _admin_key_ok(x_admin_key):
            raise HTTPException(403, "Forbidden")
        return KEY_ACTOR
    actor = _admin_user(authorization)
    if actor is None:
        if not settings.admin_key:
            raise HTTPException(403, "Admin API disabled (set MAVE_ADMIN_KEY)")
        raise HTTPException(403, "Forbidden")
    return actor


def require_admin_or_bearer(x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> None:
    """Metrics scrapers (Prometheus) can send the admin key as X-Admin-Key or as a Bearer token."""
    if not settings.admin_key:
        raise HTTPException(403, "Admin API disabled (set MAVE_ADMIN_KEY)")
    supplied = x_admin_key
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not _admin_key_ok(supplied):
        raise HTTPException(403, "Forbidden")

