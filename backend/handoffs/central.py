"""Central opencode executor (NS-ADR-106).

Runs on the agent runner's tick (like the researcher): claims one queued
``central`` handoff at a time and drives the in-cluster ``opencode serve``
(v2 HTTP API, Basic auth ``opencode:<password>``):

    POST /api/session                         (title, location, permissions)
    POST /api/session/{id}/prompt             (the brief)
    POST /api/experimental/session/{id}/wait  (blocks until idle)
    GET  /api/session/{id}/message?order=desc (last assistant text = summary)

NerveCenter itself executes no code. Every session denies ``git push`` unless
the owner approved push for that handoff. Without ``NERVE_OPENCODE_URL`` and
``NERVE_OPENCODE_PASSWORD`` the executor is off and central handoffs stay queued.
The password is never logged.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
from typing import Any

import httpx
import structlog

from backend.handoffs.service import HandoffService

log = structlog.get_logger(__name__)

URL_ENV = "NERVE_OPENCODE_URL"
PASSWORD_ENV = "NERVE_OPENCODE_PASSWORD"
WORKSPACES_ENV = "NERVE_OPENCODE_WORKSPACES"
MODEL_ENV = "NERVE_OPENCODE_MODEL"  # optional "providerID/modelID"
CLAIMER = "central-opencode"
WAIT_TIMEOUT_S = 30 * 60

NO_PUSH = [
    {"action": "shell", "resource": "git push *", "effect": "deny"},
    {"action": "shell", "resource": "git remote *", "effect": "deny"},
]


class OpenCodeError(RuntimeError):
    pass


def session_permissions(push_approved: bool) -> list[dict[str, str]]:
    base = [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    return base if push_approved else base + NO_PUSH


def workspace_dir(root: str, repo: str | None) -> str:
    if not repo:
        return root
    joined = posixpath.normpath(posixpath.join(root, repo))
    if not joined.startswith(root.rstrip("/") + "/"):
        raise OpenCodeError("repo escapes the workspace root")
    return joined


def last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for msg in messages:  # newest first
        if msg.get("type") != "assistant":
            continue
        texts = [c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text"]
        text = "\n".join(t for t in texts if t).strip()
        if text:
            return text
    return ""


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = httpx.BasicAuth("opencode", password)
        self._transport = transport
        self._timeout = timeout

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                auth=self._auth,
                transport=self._transport,
                timeout=timeout or self._timeout,
            ) as client:
                resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"cannot reach opencode ({exc.__class__.__name__})") from None
        if resp.status_code >= 400:
            raise OpenCodeError(
                f"opencode {method} {path.rsplit('/', 1)[-1]} -> HTTP {resp.status_code}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def run(
        self, *, title: str, directory: str, text: str, push_approved: bool, model: str | None
    ) -> tuple[str, str]:
        body: dict[str, Any] = {
            "title": title,
            "location": {"directory": directory},
            "permissions": session_permissions(push_approved),
        }
        if model and "/" in model:
            provider, model_id = model.split("/", 1)
            body["model"] = {"providerID": provider, "id": model_id}
        created = await self._call("POST", "/api/session", json=body)
        sid = (created or {}).get("data", {}).get("id")
        if not sid:
            raise OpenCodeError("opencode returned no session id")
        await self._call("POST", f"/api/session/{sid}/prompt", json={"text": text})
        await self._call("POST", f"/api/experimental/session/{sid}/wait", timeout=WAIT_TIMEOUT_S)
        page = await self._call(
            "GET", f"/api/session/{sid}/message", params={"order": "desc", "limit": "50"}
        )
        return sid, last_assistant_text((page or {}).get("data", []))


class CentralExecutor:
    def __init__(
        self,
        handoffs: HandoffService,
        client: OpenCodeClient,
        *,
        workspaces: str = "/workspaces",
        model: str | None = None,
        context: Any = None,
    ) -> None:
        self.handoffs = handoffs
        self.client = client
        self.workspaces = workspaces
        self.model = model
        self.context = context  # ProjectService or None
        self._task: asyncio.Task[Any] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def tick(self) -> None:
        """Start one claimed handoff in the background; never blocks the runner."""
        if self.busy:
            return
        job = await self.handoffs.claim_next("central", CLAIMER)
        if job is None:
            return
        self._task = asyncio.create_task(self.execute(job))

    async def _prompt(self, job: dict[str, Any]) -> str:
        parts = [job["brief"]]
        if job.get("project_slug") and self.context is not None:
            try:
                parts.append(
                    "Project context from NerveCenter:\n"
                    + await self.context.context_text(job["project_slug"])
                )
            except Exception:  # noqa: BLE001 - context is optional
                pass
        rule = (
            "You may push only because the owner approved it for this task."
            if job["push_approved"]
            else "Do not push to any git remote; leave changes committed locally for owner review."
        )
        parts.append(
            rule + " End with a short summary of what you changed and how you verified it."
        )
        return "\n\n".join(parts)

    async def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            directory = workspace_dir(self.workspaces, job.get("repo"))
            sid, summary = await self.client.run(
                title=job["title"],
                directory=directory,
                text=await self._prompt(job),
                push_approved=bool(job["push_approved"]),
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the row
            log.warning("handoffs.central_failed", handoff_id=job["id"], error=type(exc).__name__)
            return await self.handoffs.finish(
                job["id"], ok=False, error=str(exc)[:500], claimer=CLAIMER
            )
        log.info("handoffs.central_done", handoff_id=job["id"])
        return await self.handoffs.finish(
            job["id"],
            ok=True,
            summary=summary or "(no summary returned)",
            claimer=CLAIMER,
            session_ref=sid,
        )


def central_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    e = env if env is not None else os.environ
    return {
        "configured": bool((e.get(URL_ENV) or "").strip() and (e.get(PASSWORD_ENV) or "").strip()),
        "url": (e.get(URL_ENV) or "").strip() or None,
    }


def build_executor(app: Any, env: dict[str, str] | None = None) -> CentralExecutor | None:
    e = env if env is not None else os.environ
    url = (e.get(URL_ENV) or "").strip()
    password = (e.get(PASSWORD_ENV) or "").strip()
    if not url or not password:
        return None
    from backend.handoffs.service import get_service
    from backend.projects import get_service as project_service

    handoffs = get_service(app)
    if handoffs is None:
        return None
    return CentralExecutor(
        handoffs,
        OpenCodeClient(url, password),
        workspaces=(e.get(WORKSPACES_ENV) or "/workspaces").rstrip("/") or "/workspaces",
        model=(e.get(MODEL_ENV) or "").strip() or None,
        context=project_service(app),
    )


def attach(app: Any) -> CentralExecutor | None:
    """Hook the executor onto the agent runner loop (idempotent)."""
    existing = getattr(app.state, "handoff_executor", None)
    if existing is not None:
        return existing
    from backend.agents.runner import get_runner

    runner = get_runner(app)
    executor = build_executor(app)
    if executor is None or runner is None:
        return None
    app.state.handoff_executor = executor
    runner.add_tick_hook(executor.tick)
    return executor
