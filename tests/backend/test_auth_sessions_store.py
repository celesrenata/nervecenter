"""Session settings, refresh-session storage and the 037 migration (NS-ADR-123).

Runs against the in-process store (``session_factory=None``); no database.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.api import auth_service, auth_sessions
from backend.api.auth_service import AuthContext, AuthSettings, TokenError
from backend.persistence.models import Base

SECRET = "unit-secret-" + "s" * 60


def _ctx(**settings: object) -> AuthContext:
    return AuthContext(
        secret=SECRET,
        issuer="https://sso.test.example/realms/test",
        client_id="client",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
        settings=AuthSettings(**settings),
    )


@pytest.fixture(autouse=True)
def _fresh_store():
    auth_service.reset_memory_store()
    yield
    auth_service.reset_memory_store()


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "NERVE_ACCESS_TOKEN_TTL_MINUTES",
        "NERVE_SESSION_IDLE_HOURS",
        "NERVE_SESSION_MAX_DAYS",
        "NERVE_REFRESH_REUSE_GRACE_SECONDS",
        "NERVE_COOKIE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AuthSettings.from_env()
    assert settings.access_ttl is None
    assert auth_service.ACCESS_TOKEN_TTL == timedelta(minutes=60)
    assert settings.idle_timeout == timedelta(hours=24)
    assert settings.absolute_lifetime == timedelta(days=30)
    assert settings.reuse_grace == timedelta(seconds=10)
    assert settings.cookie_secure is True


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NERVE_ACCESS_TOKEN_TTL_MINUTES", "5")
    monkeypatch.setenv("NERVE_SESSION_IDLE_HOURS", "2")
    monkeypatch.setenv("NERVE_SESSION_MAX_DAYS", "3")
    monkeypatch.setenv("NERVE_REFRESH_REUSE_GRACE_SECONDS", "1.5")
    monkeypatch.setenv("NERVE_COOKIE_SECURE", "0")
    settings = AuthSettings.from_env()
    assert settings.access_ttl == timedelta(minutes=5)
    assert settings.idle_timeout == timedelta(hours=2)
    assert settings.absolute_lifetime == timedelta(days=3)
    assert settings.reuse_grace == timedelta(seconds=1.5)
    assert settings.cookie_secure is False


def test_settings_invalid_values_fall_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NERVE_SESSION_IDLE_HOURS", "soon")
    monkeypatch.setenv("NERVE_SESSION_MAX_DAYS", "-4")
    settings = AuthSettings.from_env()
    assert settings.idle_timeout == timedelta(hours=24)
    assert settings.absolute_lifetime == timedelta(days=30)


def test_access_token_uses_configured_ttl():
    ctx = _ctx(access_ttl=timedelta(minutes=7))
    _token, exp = ctx.issue_access_token("someone", ["user"])
    assert timedelta(minutes=6) < exp - datetime.now(UTC) <= timedelta(minutes=7)


async def test_refresh_token_stored_only_as_hash():
    ctx = _ctx()
    raw, row = await auth_sessions.start_session(
        ctx, sub="alice", identity={"sub": "alice"}, source="sso"
    )
    assert row.token_hash == auth_sessions.hash_token(raw)
    assert raw not in {str(v) for v in vars(row).values()}
    assert row.idle_expires_at - row.issued_at <= timedelta(hours=24, seconds=1)
    assert row.absolute_expires_at - row.issued_at <= timedelta(days=30, seconds=1)


async def test_rotation_keeps_family_and_marks_old_row():
    ctx = _ctx()
    raw, first = await auth_sessions.start_session(
        ctx, sub="alice", identity={}, source="sso"
    )
    new_raw, child = await auth_sessions.rotate_session(ctx, raw)
    assert new_raw != raw
    assert child.family_id == first.family_id
    assert child.absolute_expires_at == first.absolute_expires_at
    assert first.rotated_at is not None and first.replaced_by == child.id


async def test_idle_expiry_revokes_family():
    ctx = _ctx()
    raw, row = await auth_sessions.start_session(ctx, sub="a", identity={}, source="sso")
    row.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(TokenError, match="expired"):
        await auth_sessions.rotate_session(ctx, raw)
    assert row.revoked is True


async def test_absolute_expiry_is_enforced_even_when_active():
    ctx = _ctx()
    raw, row = await auth_sessions.start_session(ctx, sub="a", identity={}, source="sso")
    row.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(TokenError, match="expired"):
        await auth_sessions.rotate_session(ctx, raw)


async def test_idle_window_is_capped_by_absolute_window():
    ctx = _ctx(idle_timeout=timedelta(days=10), absolute_lifetime=timedelta(days=1))
    _raw, row = await auth_sessions.start_session(ctx, sub="a", identity={}, source="sso")
    assert row.idle_expires_at == row.absolute_expires_at


async def test_unknown_token_is_rejected():
    with pytest.raises(TokenError, match="invalid"):
        await auth_sessions.rotate_session(_ctx(), "not-a-real-token")


def test_new_tables_are_registered_in_metadata():
    tables = Base.metadata.tables
    assert {"auth_refresh_sessions", "personal_api_tokens"} <= set(tables)
    assert tables["auth_refresh_sessions"].c.token_hash.unique is True
    assert tables["personal_api_tokens"].c.token_hash.unique is True


def test_migration_037_adds_only_new_tables():
    path = (
        Path(auth_service.__file__).parents[1]
        / "persistence/alembic/versions/037_sessions_pat.py"
    )
    spec = importlib.util.spec_from_file_location("mig037", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "037_sessions_pat"
    assert module.down_revision == "036_infra"
    source = path.read_text()
    assert source.count("op.create_table(") == 2
    assert "alter_table" not in source and "add_column" not in source
