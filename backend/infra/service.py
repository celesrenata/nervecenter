"""Infrastructure connectors, read-only agent access and plan/act (NS-ADR-120).

Connectors: ``kubernetes`` (server, optional CA PEM or insecure flag, default
namespace; secret = bearer token) and ``ssh`` (host, port, user, pinned
known_hosts; secret = ``{"private_key", "passphrase"}``). Secrets are written
to the vault and are never returned: reads show only ``secret_set``.

Plan/act: an agent proposes a plan (steps + verification + rollback). It stays
``proposed`` until the owner approves it. On approval the steps run exactly as
written, one at a time and in order, either through the central opencode
(default: opencode calls the NerveCenter MCP tools ``infra_get_plan`` /
``infra_run_step``; credentials never leave NerveCenter) or directly here.
A failed step stops the plan (``failed``); verification steps run read-only.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from backend.infra import executor
from backend.infra.policy import (
    PolicyError,
    check_kubectl,
    check_ssh_act_command,
    redact,
    ssh_read_command,
)
from backend.persistence.models.infra import InfraConnector, InfraPlan

log = structlog.get_logger(__name__)

KINDS = ("kubernetes", "ssh")
OWNER_KIND = "infra_connector"
SECRET_KIND = "infra_credential"
ACT_MODES = ("opencode", "direct")
MAX_STEPS = 25
_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


class InfraInputError(ValueError):
    pass


#: Live output kept per running step (characters, tail).
LIVE_MAX_CHARS = 19_000  # under policy.MAX_OUTPUT_CHARS


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or set(name.lower()) - _NAME_OK:
        raise InfraInputError("name: 1-64 letters, digits, - or _")
    return name.lower()


def _clean_config(kind: str, cfg: Any) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    if kind == "kubernetes":
        server = str(cfg.get("server") or "").strip()
        if not server.startswith("https://") or len(server) > 300:
            raise InfraInputError("server must be an https:// URL")
        out: dict[str, Any] = {"server": server}
        ca = str(cfg.get("ca_pem") or "").strip()
        if ca:
            if "BEGIN CERTIFICATE" not in ca or len(ca) > 20_000:
                raise InfraInputError("ca_pem must be a PEM certificate")
            out["ca_pem"] = ca
        out["insecure_skip_tls_verify"] = bool(cfg.get("insecure_skip_tls_verify")) and not ca
        ns = str(cfg.get("namespace") or "").strip()
        if ns:
            out["namespace"] = _clean_name(ns)
        return out
    host, user = cfg.get("host"), cfg.get("user")
    port = cfg.get("port", 22)
    if not executor.valid_host(host):
        raise InfraInputError("host must be a hostname or IPv4 address")
    if not executor.valid_user(user):
        raise InfraInputError("user must be a plain login name")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise InfraInputError("port must be 1-65535")
    return {"host": host, "port": port, "user": user}


def connector_view(row: InfraConnector, secret_set: bool) -> dict[str, Any]:
    cfg = dict(row.config or {})
    view: dict[str, Any] = {
        "id": row.id, "kind": row.kind, "name": row.name, "secret_set": secret_set,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if row.kind == "kubernetes":
        view.update(server=cfg.get("server"), namespace=cfg.get("namespace"),
                    ca_set=bool(cfg.get("ca_pem")),
                    insecure_skip_tls_verify=bool(cfg.get("insecure_skip_tls_verify")))
    else:
        view.update(host=cfg.get("host"), port=cfg.get("port"), user=cfg.get("user"),
                    host_key_status=row.host_key_status,
                    fingerprints=cfg.get("fingerprints") or [],
                    pending_fingerprints=cfg.get("pending_fingerprints") or [])
    return view


def plan_view(p: InfraPlan) -> dict[str, Any]:
    return {
        "id": p.id, "agent_id": p.agent_id, "title": p.title, "summary": p.summary,
        "steps": p.steps, "verify": p.verify, "rollback": p.rollback, "status": p.status,
        "act_mode": p.act_mode, "decided_by": p.decided_by,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "results": p.results, "handoff_id": p.handoff_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


class InfraService:
    def __init__(self, session_factory: Any, vault: Any, *, handoffs: Any = None) -> None:
        self._sessions = session_factory
        self._vault = vault  # registry.connections (upsert_secret/get_secret/delete_secret)
        self._handoffs = handoffs
        self._tasks: set[asyncio.Task[Any]] = set()
        # Live output of the step running now, per plan (memory only; see live_view).
        self._live: dict[str, dict[str, Any]] = {}

    # -- connectors ------------------------------------------------------------------

    async def _secret(self, connector_id: str) -> str | None:
        return await self._vault.get_secret(kind=SECRET_KIND, owner_kind=OWNER_KIND,
                                            owner_id=connector_id)

    async def list_connectors(self) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (await s.execute(select(InfraConnector).order_by(InfraConnector.name))).scalars()
            rows = list(rows)
        return [connector_view(r, bool(await self._secret(r.id))) for r in rows]

    async def _row(self, s: Any, ident: str) -> InfraConnector:
        row = await s.get(InfraConnector, ident)
        if row is None:
            row = (await s.execute(
                select(InfraConnector).where(InfraConnector.name == str(ident).lower())
            )).scalars().first()
        if row is None:
            raise KeyError(ident)
        return row

    async def create_connector(self, kind: str, name: str, config: Any) -> dict[str, Any]:
        if kind not in KINDS:
            raise InfraInputError("kind must be kubernetes or ssh")
        row = InfraConnector(kind=kind, name=_clean_name(name), config=_clean_config(kind, config),
                             host_key_status="unpinned")
        async with self._sessions() as s:
            if (await s.execute(select(InfraConnector).where(InfraConnector.name == row.name))
                    ).scalars().first():
                raise InfraInputError("a connector with that name exists")
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return connector_view(row, False)

    async def update_connector(self, ident: str, config: Any) -> dict[str, Any]:
        async with self._sessions() as s:
            row = await self._row(s, ident)
            new = _clean_config(row.kind, {**(row.config or {}), **(config or {})})
            if row.kind == "ssh":
                old = row.config or {}
                same_target = all(old.get(k) == new.get(k) for k in ("host", "port"))
                for k in ("known_hosts", "fingerprints") if same_target else ():
                    if old.get(k):
                        new[k] = old[k]
                if not same_target:
                    row.host_key_status = "unpinned"
            row.config = new
            await s.commit()
            await s.refresh(row)
        return connector_view(row, bool(await self._secret(row.id)))

    async def set_secret(self, ident: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._sessions() as s:
            row = await self._row(s, ident)
        if row.kind == "kubernetes":
            token = str(payload.get("token") or "").strip()
            if not token or len(token) > 8_000 or any(c.isspace() for c in token):
                raise InfraInputError("token must be a single value without spaces")
            text = token
        else:
            key = str(payload.get("private_key") or "")
            passphrase = payload.get("passphrase") or None
            try:
                executor.normalize_ssh_key(key, passphrase)  # validates type and passphrase
            except executor.ExecutorError as exc:
                raise InfraInputError(exc.code) from exc
            text = json.dumps({"private_key": key, "passphrase": passphrase})
        await self._vault.upsert_secret(kind=SECRET_KIND, owner_kind=OWNER_KIND,
                                        owner_id=row.id, plaintext=text)
        log.info("infra.secret_set", connector=row.name, kind=row.kind)
        return connector_view(row, True)

    async def delete_connector(self, ident: str) -> None:
        async with self._sessions() as s:
            row = await self._row(s, ident)
            await self._vault.delete_secret(kind=SECRET_KIND, owner_kind=OWNER_KIND,
                                            owner_id=row.id)
            await s.delete(row)
            await s.commit()

    async def scan_host_key(self, ident: str) -> dict[str, Any]:
        async with self._sessions() as s:
            row = await self._row(s, ident)
            if row.kind != "ssh":
                raise InfraInputError("only ssh connectors have host keys")
            lines, prints = await executor.scan_host_keys(row.config or {})
            row.config = {**(row.config or {}), "pending_known_hosts": lines,
                          "pending_fingerprints": prints}
            if row.host_key_status != "pinned":
                row.host_key_status = "pending"
            await s.commit()
            await s.refresh(row)
        return connector_view(row, bool(await self._secret(row.id)))

    async def trust_host_key(self, ident: str, fingerprints: list[str]) -> dict[str, Any]:
        """Pin the scanned keys, only if the owner confirms the exact fingerprints."""
        async with self._sessions() as s:
            row = await self._row(s, ident)
            cfg = dict(row.config or {})
            pending = cfg.get("pending_fingerprints") or []
            if not pending or sorted(fingerprints or []) != sorted(pending):
                raise InfraInputError("fingerprints do not match the last scan")
            cfg["known_hosts"] = cfg.pop("pending_known_hosts")
            cfg["fingerprints"] = cfg.pop("pending_fingerprints")
            row.config = cfg
            row.host_key_status = "pinned"
            await s.commit()
            await s.refresh(row)
        log.info("infra.host_key_pinned", connector=row.name)
        return connector_view(row, bool(await self._secret(row.id)))

    # -- running ----------------------------------------------------------------------

    async def _execute(self, row: InfraConnector, step: dict[str, Any], *, act: bool,
                       on_output: Any = None) -> executor.RunResult:
        secret = await self._secret(row.id)
        if row.kind == "kubernetes":
            args = check_kubectl(step.get("args"), act=act, stdin=step.get("stdin"))
            return await executor.run_kubectl(row.config or {}, secret, args,
                                              on_output=on_output,
                                              stdin=step.get("stdin"), act=act)
        if row.host_key_status != "pinned":
            raise executor.ExecutorError("host_key_not_pinned")
        if act:
            command = check_ssh_act_command(step.get("command"))
        else:
            command = ssh_read_command(step.get("command"), step.get("params"))
        return await executor.run_ssh(row.config or {}, secret, command, act=act,
                                      on_output=on_output)

    async def run_read(self, connector: str, step: dict[str, Any]) -> dict[str, Any]:
        """Plan mode: one read-only command. ``{"ok", "exit", "output"}`` or a refusal."""
        try:
            async with self._sessions() as s:
                row = await self._row(s, connector)
            res = await self._execute(row, step, act=False)
        except KeyError:
            return {"ok": False, "denied_reason": "unknown_connector"}
        except (PolicyError, executor.ExecutorError) as exc:
            return {"ok": False, "denied_reason": exc.code}
        log.info("infra.read", connector=row.name, kind=row.kind, ok=res.ok)
        return res.as_dict()

    # -- plans ------------------------------------------------------------------------

    async def _validate_steps(self, steps: Any, *, act: bool) -> list[dict[str, Any]]:
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise InfraInputError(f"steps: a list of at most {MAX_STEPS}")
        out: list[dict[str, Any]] = []
        async with self._sessions() as s:
            for raw in steps:
                if not isinstance(raw, dict):
                    raise InfraInputError("each step is an object")
                try:
                    row = await self._row(s, str(raw.get("connector") or ""))
                except KeyError as exc:
                    raise InfraInputError("step names an unknown connector") from exc
                step: dict[str, Any] = {"connector": row.name,
                                        "purpose": str(raw.get("purpose") or "")[:300]}
                try:
                    if row.kind == "kubernetes":
                        step["args"] = check_kubectl(raw.get("args"), act=act,
                                                     stdin=raw.get("stdin"))
                        if raw.get("stdin") is not None:
                            step["stdin"] = raw["stdin"]
                    elif act:
                        step["command"] = check_ssh_act_command(raw.get("command"))
                    else:
                        ssh_read_command(raw.get("command"), raw.get("params"))
                        step["command"] = raw.get("command")
                        step["params"] = raw.get("params") or {}
                except PolicyError as exc:
                    raise InfraInputError(f"step refused: {exc.code}") from exc
                out.append(step)
        return out

    async def propose(self, agent_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        if not 1 <= len(title) <= 200:
            raise InfraInputError("title: 1-200 characters")
        steps = await self._validate_steps(payload.get("steps"), act=True)
        if not steps:
            raise InfraInputError("a plan needs at least one step")
        verify = await self._validate_steps(payload.get("verify") or [], act=False)
        plan = InfraPlan(agent_id=agent_id, title=title,
                         summary=str(payload.get("summary") or "")[:8_000],
                         steps=steps, verify=verify,
                         rollback=str(payload.get("rollback") or "")[:8_000],
                         status="proposed", results=[])
        async with self._sessions() as s:
            s.add(plan)
            await s.commit()
            await s.refresh(plan)
        log.info("infra.plan_proposed", plan_id=plan.id, steps=len(steps))
        return plan_view(plan)

    async def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            q = select(InfraPlan).order_by(InfraPlan.created_at.desc()).limit(100)
            if status:
                q = q.where(InfraPlan.status == status)
            return [
                {**plan_view(p), "live": self.live_view(p.id)}
                for p in (await s.execute(q)).scalars()
            ]

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        async with self._sessions() as s:
            p = await s.get(InfraPlan, plan_id)
            if p is None:
                raise KeyError(plan_id)
            return {**plan_view(p), "live": self.live_view(plan_id)}

    def live_view(self, plan_id: str) -> dict[str, Any] | None:
        """Output so far of the step running now (redacted, tail-capped), or None."""
        live = self._live.get(plan_id)
        if live is None:
            return None
        # Tail first (redact() caps from the start), with room for a secret that
        # straddles the cut; the redacted tail fits the output cap.
        text = redact("".join(live["buffer"])[-LIVE_MAX_CHARS:])
        return {"index": live["index"], "started_at": live["started_at"], "output": text}

    async def decide(self, plan_id: str, *, approve: bool, actor: str,
                     act_mode: str = "opencode") -> dict[str, Any]:
        if act_mode not in ACT_MODES:
            raise InfraInputError("act_mode must be opencode or direct")
        async with self._sessions() as s:
            p = await s.get(InfraPlan, plan_id)
            if p is None:
                raise KeyError(plan_id)
            if p.status != "proposed":
                raise InfraInputError(f"plan is {p.status}")
            p.status = "approved" if approve else "rejected"
            p.act_mode = act_mode
            p.decided_by, p.decided_at = actor[:200], _now()
            await s.commit()
            await s.refresh(p)
        log.info("infra.plan_decided", plan_id=plan_id, approved=approve, mode=act_mode)
        if approve:
            if act_mode == "opencode" and self._handoffs is not None:
                handoff = await self._handoffs.enqueue(
                    target="opencode", runner="central", title=f"Infra: {p.title}"[:200],
                    brief=self.opencode_brief(plan_view(p)), requested_by=actor[:200] or "owner",
                )
                async with self._sessions() as s:
                    row = await s.get(InfraPlan, plan_id)
                    row.handoff_id = handoff.get("id")
                    await s.commit()
            else:
                task = asyncio.create_task(self.run_all(plan_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return await self.get_plan(plan_id)

    @staticmethod
    def opencode_brief(plan: dict[str, Any]) -> str:
        lines = [
            f"Carry out the approved infrastructure plan {plan['id']} ({plan['title']}).",
            "You do not have the credentials. Use ONLY the NerveCenter MCP tools:",
            "  infra_get_plan(plan_id) to read it, then infra_run_step(plan_id, index) for each",
            "  step in order (0, 1, 2 ...). Each step runs exactly as the owner approved it.",
            "After every step read its output. If a step fails or the output is not what the",
            "plan expects, STOP: do not run further steps, do not improvise or retry with",
            "different commands. Report what happened and the rollback described in the plan.",
            "When all steps succeed, run infra_run_verify(plan_id) and report the results.",
            "", f"Plan summary: {plan['summary'][:2000]}",
        ]
        return "\n".join(lines)

    async def run_step(self, plan_id: str, index: int) -> dict[str, Any]:
        """Run the next approved step (``index`` must be the next one)."""
        async with self._sessions() as s:
            p = await s.get(InfraPlan, plan_id)
            if p is None:
                raise KeyError(plan_id)
            if p.status not in ("approved", "running"):
                raise InfraInputError(f"plan is {p.status}")
            results = list(p.results or [])
            if index != len(results) or not 0 <= index < len(p.steps):
                raise InfraInputError("steps run in order; that is not the next step")
            step = dict(p.steps[index])
            p.status = "running"
            await s.commit()
            row = await self._row(s, step["connector"])
        live = {"index": index, "started_at": datetime.now(UTC).isoformat(), "buffer": []}
        self._live[plan_id] = live

        def on_output(text: str) -> None:
            buf = live["buffer"]
            buf.append(text)
            if sum(len(c) for c in buf) > LIVE_MAX_CHARS * 2:  # keep the tail only
                joined = "".join(buf)[-LIVE_MAX_CHARS:]
                buf[:] = [joined]

        try:
            res = (await self._execute(row, step, act=True, on_output=on_output)).as_dict()
        except (PolicyError, executor.ExecutorError) as exc:
            res = {"ok": False, "exit": None, "output": f"[refused: {exc.code}]"}
        finally:
            if self._live.get(plan_id) is live:
                del self._live[plan_id]
        async with self._sessions() as s:
            p = await s.get(InfraPlan, plan_id)
            p.results = [*list(p.results or []), {"index": index, **res}]
            if not res["ok"]:
                p.status = "failed"
            elif index == len(p.steps) - 1:
                p.status = "done"
            await s.commit()
        log.info("infra.step", plan_id=plan_id, index=index, ok=res["ok"])
        return {"index": index, **res}

    async def run_verify(self, plan_id: str) -> list[dict[str, Any]]:
        plan = await self.get_plan(plan_id)
        if plan["status"] not in ("done", "failed"):
            raise InfraInputError("verification runs after the steps")
        out = []
        for i, step in enumerate(plan["verify"]):
            out.append({"index": i, **(await self.run_read(step["connector"], step))})
        return out

    async def run_all(self, plan_id: str) -> None:
        try:
            plan = await self.get_plan(plan_id)
            for i in range(len(plan["steps"])):
                if not (await self.run_step(plan_id, i))["ok"]:
                    return
        except Exception as exc:  # noqa: BLE001 - recorded in logs; status stays as set
            log.warning("infra.run_all_failed", plan_id=plan_id, error=type(exc).__name__)


def get_service(app: Any) -> InfraService | None:
    svc = getattr(app.state, "infra_service", None)
    if svc is not None:
        return svc
    registry = getattr(app.state, "repository_registry", None)
    if registry is None:
        return None
    from backend.handoffs.service import get_service as handoffs_service

    svc = InfraService(registry.session_factory, registry.connections,
                       handoffs=handoffs_service(app))
    app.state.infra_service = svc
    return svc


__all__ = ["InfraInputError", "InfraService", "get_service"]
