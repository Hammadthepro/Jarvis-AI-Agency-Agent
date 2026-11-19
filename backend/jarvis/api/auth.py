"""Authentication (Supabase-issued JWTs) and explicit workspace authorization.

RLS is defence in depth only: the API uses a privileged DB role, so EVERY query is scoped by the
workspace resolved here, and role checks gate writes."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, Request

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import Forbidden, JarvisError

ROLE_RANK = {"viewer": 0, "operator": 1, "owner": 2}


class Unauthorized(JarvisError):
    code, http_status = "UNAUTHORIZED", 401


class RateLimited(JarvisError):
    code, http_status = "RATE_LIMITED", 429


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str | None
    workspace_id: uuid.UUID
    role: str

    def require(self, role: str) -> None:
        if ROLE_RANK[self.role] < ROLE_RANK[role]:
            raise Forbidden(f"requires {role} role")


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._jwks = jwt.PyJWKClient(settings.supabase_jwks_url, cache_keys=True) if settings.supabase_jwks_url else None

    async def verify(self, token: str) -> dict:
        opts = {"require": ["exp", "sub"]}
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") in ("RS256", "ES256") and self._jwks:
                key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
                return jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=self.s.supabase_jwt_audience,
                                  issuer=self.s.supabase_jwt_issuer, options=opts)
            if header.get("alg") == "HS256" and self.s.supabase_jwt_secret:
                return jwt.decode(token, self.s.supabase_jwt_secret.get_secret_value(), algorithms=["HS256"],
                                  audience=self.s.supabase_jwt_audience, issuer=self.s.supabase_jwt_issuer, options=opts)
        except jwt.PyJWTError as exc:
            raise Unauthorized(f"invalid token: {exc}") from exc
        raise Unauthorized("token algorithm not accepted or verifier not configured")


class RateLimiter:
    """API rate limiting backed by the shared Postgres token buckets (works across API instances)."""

    def __init__(self, db, limits: dict | None = None) -> None:
        import json as _json
        from pathlib import Path as _P

        from jarvis.core.ratelimit import PgRateLimiter, provider_rate_limits  # noqa: F401
        self.pg = PgRateLimiter(db)
        self.limits = limits or _json.loads((_P(__file__).resolve().parents[1] / "resources" / "default_limits.json")
                                            .read_text())["api_rate_limits"]

    async def hit(self, key: str, cost: float = 1, kind: str | None = None) -> None:
        cfg = self.limits[kind or key.split(":", 1)[0]]
        d = await self.pg.take(f"api:{key}", capacity=cfg["capacity"], refill_per_s=cfg["capacity"] / cfg["per_seconds"], cost=cost)
        if not d.allowed:
            raise RateLimited("too many requests", details={"retry_after_s": round(d.retry_after_s, 1)})


async def ensure_user_and_workspace(db, user_id: uuid.UUID, email: str | None, requested_ws: str | None) -> tuple[uuid.UUID, str] | None:
    await db.execute("""insert into app_users(id, email) values ($1,$2)
                        on conflict (id) do update set email = coalesce(excluded.email, app_users.email)""", user_id, email)
    if requested_ws:
        try:
            ws = uuid.UUID(requested_ws)
        except ValueError as exc:
            raise Forbidden("invalid workspace id") from exc
        role = await db.fetchval("select role from memberships where workspace_id=$1 and user_id=$2", ws, user_id)
        if role is None:
            raise Forbidden("not a member of this workspace")
        return ws, role
    row = await db.fetchrow("select workspace_id, role from memberships where user_id=$1 order by created_at limit 1", user_id)
    return (row["workspace_id"], row["role"]) if row else None


async def create_workspace(db, user_id: uuid.UUID, name: str) -> uuid.UUID:
    async with db.transaction() as conn:
        ws = await conn.fetchval("insert into workspaces(name, created_by) values ($1,$2) returning id", name, user_id)
        await conn.execute("insert into memberships(workspace_id, user_id, role) values ($1,$2,'owner')", ws, user_id)
        await conn.execute("insert into system_settings(workspace_id) values ($1)", ws)
        await budget.seed_default_limits(conn, ws)
        await conn.execute("""insert into scoring_rule_sets(workspace_id, version, rules, is_active) values ($1,1,$2,true)""",
                           ws, __import__("jarvis.agents.qualification", fromlist=["x"]).DEFAULT_RULES)
        await conn.execute("insert into audit_logs(workspace_id, actor_type, actor_id, action) values ($1,'user',$2,'workspace.created')",
                           ws, str(user_id))
    return ws


def get_state(request: Request):
    return request.app.state


async def principal(request: Request, authorization: str | None = Header(None),
                    x_workspace_id: str | None = Header(None)) -> Principal:
    st = request.app.state
    ip = request.client.host if request.client else "?"
    await st.limiter.hit(f"ip:{ip}")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    claims = await st.verifier.verify(authorization.split(" ", 1)[1])
    try:
        uid = uuid.UUID(claims["sub"])
    except ValueError as exc:
        raise Unauthorized("token subject is not a user id") from exc
    await st.limiter.hit(f"user:{uid}")
    found = await ensure_user_and_workspace(st.db, uid, claims.get("email"), x_workspace_id)
    if found is None:
        ws = await create_workspace(st.db, uid, (claims.get("email") or "My") .split("@")[0] + " workspace")
        found = (ws, "owner")
    return Principal(uid, claims.get("email"), found[0], found[1])


CurrentPrincipal = Depends(principal)
