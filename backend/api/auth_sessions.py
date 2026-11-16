"""NerveCenter-owned sessions and personal API tokens (NS-ADR-123, NS-ADR-124).

Two credential kinds live here, both stored only as SHA-256 hashes:

- **Refresh sessions**: an opaque 256-bit refresh token that reaches the
  browser only as an ``HttpOnly`` cookie.  Each use rotates the token inside one
  ``family_id``.  Idle (sliding) and absolute expiry are enforced, and a rotated
  token presented after the grace window revokes the whole family.
- **Personal API tokens** (``nct_...``): long-lived bearer tokens owned by a
  person, with optional expiry and revocation.

Token values are never logged and never returned except at creation.  Storage
is PostgreSQL when the auth context has a session factory and the in-process
store otherwise (the same fallback as :mod:`backend.api.auth_service`).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update

from backend.api import auth_service
from backend.api.auth_service import AuthContext, TokenError
from backend.persistence.models.auth import (
    AuthRefreshSession,
    AuthSession,
    PersonalApiToken,
)

log = auth_service.log

PAT_PREFIX = "nct_"
#: ``last_used_at`` on a personal token is written at most this often.
PAT_TOUCH_INTERVAL = timedelta(minutes=5)
#: Roles carried by a NerveCenter session minted from an SSO login.
SSO_SESSION_ROLES = ["user"]
#: Claim naming the refresh family an access token belongs to.
FAMILY_CLAIM = "fam"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite-backed tests may return naive datetimes; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ─── Repositories ─────────────────────────────────────────────────────────────


class _MemoryRepo:
    """In-process storage (no database configured)."""

    def __init__(self) -> None:
        self._store = auth_service._memory_store

    async def add_refresh(self, **fields: Any) -> Any:
        row = SimpleNamespace(
            id=str(uuid.uuid4()), issued_at=_now(), last_used_at=None,
            rotated_at=None, replaced_by=None, revoked=False, revoked_at=None,
            **fields,
        )
        self._store.refresh_sessions[row.id] = row
        return row

    async def get_refresh_by_hash(self, token_hash: str) -> Any:
        for row in self._store.refresh_sessions.values():
            if row.token_hash == token_hash:
                return row
        return None

    async def update_refresh(self, row_id: str, **fields: Any) -> None:
        row = self._store.refresh_sessions.get(row_id)
        if row is not None:
            for key, value in fields.items():
                setattr(row, key, value)

    async def revoke_family(self, family_id: str) -> None:
        now = _now()
        for row in self._store.refresh_sessions.values():
            if row.family_id == family_id and not row.revoked:
                row.revoked, row.revoked_at = True, now

    async def revoke_all_for_sub(self, sub: str) -> None:
        now = _now()
        for row in self._store.refresh_sessions.values():
            if row.sub == sub and not row.revoked:
                row.revoked, row.revoked_at = True, now
        for sess in self._store.sessions.values():
            if sess.sub == sub and not sess.revoked:
                sess.revoked, sess.revoked_at = True, now

    async def add_pat(self, **fields: Any) -> Any:
        row = SimpleNamespace(
            id=str(uuid.uuid4()), created_at=_now(), last_used_at=None,
            revoked_at=None, **fields,
        )
        self._store.personal_tokens[row.id] = row
        return row

    async def get_pat_by_hash(self, token_hash: str) -> Any:
        for row in self._store.personal_tokens.values():
            if row.token_hash == token_hash:
                return row
        return None

    async def get_pat(self, token_id: str) -> Any:
        return self._store.personal_tokens.get(token_id)

    async def list_pats(self, owner_sub: str) -> list[Any]:
        rows = [
            r for r in self._store.personal_tokens.values() if r.owner_sub == owner_sub
        ]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    async def update_pat(self, token_id: str, **fields: Any) -> None:
        row = self._store.personal_tokens.get(token_id)
        if row is not None:
            for key, value in fields.items():
                setattr(row, key, value)


class _DbRepo:
    """PostgreSQL storage; one short transaction per call."""

    def __init__(self, ctx: AuthContext) -> None:
        self._factory = ctx.session_factory

    async def _add(self, row: Any) -> Any:
        async with self._factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            session.expunge(row)
        return row

    async def _one(self, model: Any, *where: Any) -> Any:
        async with self._factory() as session:
            row = (await session.execute(select(model).where(*where))).scalar_one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    async def _update(self, model: Any, row_id: str, **fields: Any) -> None:
        async with self._factory() as session:
            await session.execute(
                update(model).where(model.id == row_id).values(**fields)
            )
            await session.commit()

    async def add_refresh(self, **fields: Any) -> Any:
        return await self._add(AuthRefreshSession(**fields))

    async def get_refresh_by_hash(self, token_hash: str) -> Any:
        return await self._one(
            AuthRefreshSession, AuthRefreshSession.token_hash == token_hash
        )

    async def update_refresh(self, row_id: str, **fields: Any) -> None:
        await self._update(AuthRefreshSession, row_id, **fields)

    async def revoke_family(self, family_id: str) -> None:
        async with self._factory() as session:
            await session.execute(
                update(AuthRefreshSession)
                .where(
                    AuthRefreshSession.family_id == family_id,
                    AuthRefreshSession.revoked.is_(False),
                )
                .values(revoked=True, revoked_at=_now())
            )
            await session.commit()

    async def revoke_all_for_sub(self, sub: str) -> None:
        now = _now()
        async with self._factory() as session:
            await session.execute(
                update(AuthRefreshSession)
                .where(
                    AuthRefreshSession.sub == sub,
                    AuthRefreshSession.revoked.is_(False),
                )
                .values(revoked=True, revoked_at=now)
            )
            await session.execute(
                update(AuthSession)
                .where(AuthSession.sub == sub, AuthSession.revoked.is_(False))
                .values(revoked=True, revoked_at=now)
            )
            await session.commit()

    async def add_pat(self, **fields: Any) -> Any:
        return await self._add(PersonalApiToken(**fields))

    async def get_pat_by_hash(self, token_hash: str) -> Any:
        return await self._one(
            PersonalApiToken, PersonalApiToken.token_hash == token_hash
        )

    async def get_pat(self, token_id: str) -> Any:
        return await self._one(PersonalApiToken, PersonalApiToken.id == token_id)

    async def list_pats(self, owner_sub: str) -> list[Any]:
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(PersonalApiToken)
                    .where(PersonalApiToken.owner_sub == owner_sub)
                    .order_by(PersonalApiToken.created_at.desc())
                )
            ).scalars().all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    async def update_pat(self, token_id: str, **fields: Any) -> None:
        await self._update(PersonalApiToken, token_id, **fields)


def repo_for(ctx: AuthContext) -> _MemoryRepo | _DbRepo:
    return _MemoryRepo() if ctx.session_factory is None else _DbRepo(ctx)


# ─── Refresh sessions ─────────────────────────────────────────────────────────


def _new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def _idle_deadline(ctx: AuthContext, now: datetime, absolute: datetime) -> datetime:
    return min(now + ctx.settings.idle_timeout, absolute)


async def start_session(
    ctx: AuthContext,
    *,
    sub: str,
    identity: dict[str, Any],
    source: str,
    user_agent: str | None = None,
) -> tuple[str, Any]:
    """Create a new refresh family.  Returns ``(raw_refresh_token, row)``."""
    now = _now()
    absolute = now + ctx.settings.absolute_lifetime
    raw = _new_refresh_token()
    row = await repo_for(ctx).add_refresh(
        family_id=uuid.uuid4().hex,
        sub=sub,
        identity=identity,
        source=source,
        token_hash=hash_token(raw),
        idle_expires_at=_idle_deadline(ctx, now, absolute),
        absolute_expires_at=absolute,
        user_agent=(user_agent or "")[:255] or None,
    )
    log.info("auth.session_started", family=row.family_id, source=source)
    return raw, row


async def rotate_session(
    ctx: AuthContext, raw_token: str, *, user_agent: str | None = None
) -> tuple[str, Any]:
    """Validate a refresh token and rotate it.  Returns ``(new_raw_token, new_row)``.

    Raises :class:`TokenError` for an unknown, revoked, idle- or absolutely
    expired token, or a replayed token (which also revokes its family).
    """
    repo = repo_for(ctx)
    token_hash = hash_token(raw_token)
    row = await repo.get_refresh_by_hash(token_hash)
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        raise TokenError("invalid refresh token")
    if row.revoked:
        raise TokenError("session has been revoked")
    now = _now()
    if now >= _aware(row.absolute_expires_at) or now >= _aware(row.idle_expires_at):
        await repo.revoke_family(row.family_id)
        log.info("auth.session_expired", family=row.family_id)
        raise TokenError("session has expired")
    rotated_at = _aware(row.rotated_at)
    if rotated_at is not None:
        if now - rotated_at > ctx.settings.reuse_grace:
            await repo.revoke_family(row.family_id)
            log.warning("auth.refresh_reuse_detected", family=row.family_id, sub=row.sub)
            raise TokenError("refresh token reuse detected; session revoked")
        # Concurrent tab inside the grace window: issue another child below.
    raw = _new_refresh_token()
    absolute = _aware(row.absolute_expires_at)
    child = await repo.add_refresh(
        family_id=row.family_id,
        sub=row.sub,
        identity=row.identity,
        source=row.source,
        token_hash=hash_token(raw),
        idle_expires_at=_idle_deadline(ctx, now, absolute),
        absolute_expires_at=absolute,
        user_agent=(user_agent or row.user_agent or "")[:255] or None,
    )
    if rotated_at is None:
        await repo.update_refresh(
            row.id, rotated_at=now, replaced_by=child.id, last_used_at=now
        )
    return raw, child


async def revoke_family(ctx: AuthContext, family_id: str) -> None:
    await repo_for(ctx).revoke_family(family_id)
    log.info("auth.session_revoked", family=family_id)


async def revoke_family_by_token(ctx: AuthContext, raw_token: str) -> str | None:
    """Revoke the family of a presented refresh token; returns the family id."""
    row = await repo_for(ctx).get_refresh_by_hash(hash_token(raw_token))
    if row is None:
        return None
    await revoke_family(ctx, row.family_id)
    return row.family_id


async def revoke_all_for_sub(ctx: AuthContext, sub: str) -> None:
    await repo_for(ctx).revoke_all_for_sub(sub)
    log.info("auth.sessions_revoked_all", sub=sub[:80])


async def issue_session_access_token(
    ctx: AuthContext, row: Any
) -> tuple[str, datetime]:
    """Mint and record an access token for a refresh-session row."""
    roles = ["admin"] if row.source == "local-admin" else list(SSO_SESSION_ROLES)
    identity = row.identity or {}
    extra: dict[str, Any] = {FAMILY_CLAIM: row.family_id}
    for claim, key in (("preferred_username", "username"), ("email", "email")):
        if isinstance(identity.get(key), str) and identity[key]:
            extra[claim] = identity[key]
    token, exp = ctx.issue_access_token(row.sub, roles, extra=extra)
    await auth_service.record_session(
        ctx,
        jti=auth_service._jti_of(token, ctx),
        sub=row.sub,
        typ=auth_service.TYP_ACCESS,
        expires_at=exp,
        source=row.source,
    )
    return token, exp


# ─── Personal API tokens ──────────────────────────────────────────────────────


def looks_like_pat(token: str) -> bool:
    return token.startswith(PAT_PREFIX)


async def create_pat(
    ctx: AuthContext, *, owner_sub: str, name: str, expires_at: datetime | None
) -> tuple[str, Any]:
    """Create a personal token.  Returns ``(plaintext_token, row)``; shown once."""
    raw = PAT_PREFIX + secrets.token_urlsafe(32)
    row = await repo_for(ctx).add_pat(
        owner_sub=owner_sub,
        name=name,
        token_hash=hash_token(raw),
        prefix=raw[:8],
        expires_at=expires_at,
    )
    log.info("auth.pat_created", token_id=row.id, name=name, owner=owner_sub[:80])
    return raw, row


async def authenticate_pat(ctx: AuthContext, raw: str) -> dict[str, Any]:
    """Resolve a personal token to access-token-shaped claims, or raise 401 detail."""
    repo = repo_for(ctx)
    token_hash = hash_token(raw)
    row = await repo.get_pat_by_hash(token_hash)
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        raise TokenError("invalid API token")
    if row.revoked_at is not None:
        raise TokenError("API token has been revoked")
    now = _now()
    expires_at = _aware(row.expires_at)
    if expires_at is not None and now >= expires_at:
        raise TokenError("API token has expired")
    last_used = _aware(row.last_used_at)
    if last_used is None or now - last_used >= PAT_TOUCH_INTERVAL:
        await repo.update_pat(row.id, last_used_at=now)
    return {
        "sub": row.owner_sub,
        "typ": auth_service.TYP_ACCESS,
        "roles": list(SSO_SESSION_ROLES),
        auth_service.SSO_SOURCE_CLAIM: "pat",
        "token_id": row.id,
        "token_name": row.name,
    }


async def revoke_pat(ctx: AuthContext, *, owner_sub: str, token_id: str) -> bool:
    """Revoke a token owned by ``owner_sub``; False when not found."""
    repo = repo_for(ctx)
    row = await repo.get_pat(token_id)
    if row is None or row.owner_sub != owner_sub:
        return False
    if row.revoked_at is None:
        await repo.update_pat(token_id, revoked_at=_now())
        log.info("auth.pat_revoked", token_id=token_id, owner=owner_sub[:80])
    return True


async def list_pats(ctx: AuthContext, owner_sub: str) -> list[Any]:
    return await repo_for(ctx).list_pats(owner_sub)
