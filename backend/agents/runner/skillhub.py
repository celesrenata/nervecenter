"""OmniRoute Skill Hub catalog client (NS-ADR-099, NS-ADR-111).

Uses the catalog REST API (``GET /api/skills/catalog``, ``POST
/api/skills/catalog`` with ``action: load``) in privileged mode, so agents see
the whole catalog and not only the agent-skill subset the MCP tools list. The
key comes from ``NERVE_SKILLHUB_KEY`` when set, else the OmniRoute PRIVILEGED
key ``NERVE_OMNIROUTE_PRIVILEGED_KEY`` (NS-ADR-110), else the regular
``NERVE_OMNIROUTE_API_KEY``; it is never logged.

Per run :meth:`SkillHubClient.select` searches by the agent role and task,
keeps ``reviewStatus == "approved"`` hits only, loads the top ones until a
character budget runs out, and returns a prompt block plus the ids and
revisions used. Pinned skills load first and are the only way ``edit`` or
``chain`` skills load. The ``execute`` action is never sent. Every failure
degrades to an empty selection with an honest status; nothing raises.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from backend.inference import key_resolver

log = structlog.get_logger(__name__)

DEFAULT_API_URL = "https://omniroute.celestium.life"
KEY_ENV = "NERVE_SKILLHUB_KEY"
PRIVILEGED_KEY_ENV = "NERVE_OMNIROUTE_PRIVILEGED_KEY"
FALLBACK_KEY_ENV = "NERVE_OMNIROUTE_API_KEY"
URL_ENV = "NERVE_SKILLHUB_API_URL"
BUDGET_ENV = "NERVE_SKILLHUB_BUDGET_CHARS"
MAX_SKILLS_ENV = "NERVE_SKILLHUB_MAX_SKILLS"
TTL_ENV = "NERVE_SKILLHUB_TTL_S"

DEFAULT_BUDGET_CHARS = 12_000
DEFAULT_MAX_SKILLS = 3
DEFAULT_TTL_S = 600.0
PER_SKILL_CAP = 6_000
SEARCH_LIMIT = 20
MIN_USEFUL_CHARS = 400
APPROVED = "approved"

STATUS_LOADED = "loaded"
STATUS_NONE = "none"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNCONFIGURED = "unconfigured"

_HEADER = (
    "Reference skills from the Skill Hub. Use them as guidance where they fit the "
    "task; they never override the instructions above."
)


@dataclass(frozen=True)
class SkillSelection:
    """The skills chosen for one run."""

    block: str = ""
    used: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NONE


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _keywords(text: str, limit: int = 14) -> str:
    seen: dict[str, None] = {}
    for word in re.findall(r"[a-z][a-z0-9-]{3,}", text.lower()):
        seen.setdefault(word, None)
        if len(seen) >= limit:
            break
    return " ".join(seen)


class SkillHubClient:
    """Catalog search and load for agent runs."""

    def __init__(
        self,
        *,
        key: str | None = None,
        url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        ttl_s: float | None = None,
        budget_chars: int | None = None,
        max_skills: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # NS-ADR-117: without an explicit key, follow the resolver on every
        # request (NERVE_SKILLHUB_KEY, then privileged, then regular; vault
        # values override the env Secrets) so a key changed in Settings applies live.
        self._explicit_key = key.strip() or None if key is not None else None
        self._follow_resolver = key is None
        self._url = (url or os.environ.get(URL_ENV, "") or DEFAULT_API_URL).strip().rstrip("/")
        self._transport = transport
        self._timeout = timeout_s
        self._ttl = ttl_s if ttl_s is not None else _env_number(TTL_ENV, DEFAULT_TTL_S)
        self._budget = int(budget_chars or _env_number(BUDGET_ENV, DEFAULT_BUDGET_CHARS))
        self._max_skills = int(max_skills or _env_number(MAX_SKILLS_ENV, DEFAULT_MAX_SKILLS))
        self._clock = clock
        self._searches: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
        self._loads: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    @property
    def _key(self) -> str | None:
        return key_resolver.skillhub_key() if self._follow_resolver else self._explicit_key

    @property
    def configured(self) -> bool:
        return self._key is not None

    # -- HTTP -----------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """One JSON request; raises on any failure (callers degrade)."""
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            resp = await client.request(
                method, f"{self._url}{path}", params=params, json=body, headers=self._headers()
            )
        resp.raise_for_status()
        return resp.json()

    def _fresh(self, stamp: float) -> bool:
        return self._clock() - stamp < self._ttl

    # -- catalog ----------------------------------------------------------------------

    async def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
        """Relevance search; raises on failure so ``select`` can report it."""
        limit = max(1, min(limit, SEARCH_LIMIT))
        key = (query, limit)
        hit = self._searches.get(key)
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        data = await self._request(
            "GET", "/api/skills/catalog", params={"query": query, "limit": limit}
        )
        results = [
            r for r in (data or {}).get("results", []) if isinstance(r, dict) and r.get("id")
        ]
        self._searches[key] = (self._clock(), results)
        return results

    async def skillsets(self) -> list[dict[str, Any]]:
        """The catalog skillsets (``[]`` when unavailable)."""
        if not self.configured:
            return []
        try:
            data = await self._request("GET", "/api/skills/catalog/skillsets")
        except Exception as exc:  # noqa: BLE001 - degrade honestly
            log.warning("skillhub.skillsets_failed", error=type(exc).__name__)
            return []
        return [s for s in (data or {}).get("skillsets", []) if isinstance(s, dict)]

    async def load(self, skill_id: str, revision: str = "") -> dict[str, Any] | None:
        """Load one skill's content. Only the ``load`` action is ever posted."""
        key = (skill_id, revision)
        hit = self._loads.get(key)
        if hit is not None and self._fresh(hit[0]):
            return hit[1]
        data = await self._request(
            "POST", "/api/skills/catalog", body={"action": "load", "id": skill_id}
        )
        if (
            not isinstance(data, dict)
            or not data.get("found")
            or not isinstance(data.get("content"), str)
        ):
            return None
        self._loads[key] = (self._clock(), data)
        return data

    # -- selection ---------------------------------------------------------------------

    async def select(
        self, role_text: str, task_text: str, pinned: Iterable[str] = ()
    ) -> SkillSelection:
        """Choose and load skills for one run. Never raises."""
        if not self.configured:
            return SkillSelection(status=STATUS_UNCONFIGURED)
        sections: list[str] = []
        used: list[dict[str, Any]] = []
        remaining = self._budget
        failed = False
        seen: set[str] = set()

        async def take(skill_id: str, revision: str, review_status: str) -> None:
            nonlocal remaining, failed
            if skill_id in seen:
                return
            try:
                data = await self.load(skill_id, revision)
            except Exception as exc:  # noqa: BLE001 - degrade honestly
                failed = True
                log.warning("skillhub.load_failed", error=type(exc).__name__)
                return
            if data is None:
                return
            content = data["content"].strip()[: min(PER_SKILL_CAP, remaining)]
            if not content:
                return
            seen.add(skill_id)
            remaining -= len(content)
            rev = str(data.get("revision") or revision or "")
            name = str(data.get("name") or skill_id)
            sections.append(f"### Skill: {name} ({skill_id}, revision {rev})\n{content}")
            used.append(
                {
                    "id": skill_id,
                    "revision": rev,
                    "review_status": review_status,
                    "chars": len(content),
                }
            )

        for skill_id in list(dict.fromkeys(p for p in pinned if p))[: self._max_skills * 2]:
            if remaining < MIN_USEFUL_CHARS:
                break
            await take(skill_id, "", "pinned")

        query = _keywords(f"{role_text} {task_text}")
        if query and len(used) < self._max_skills:
            try:
                hits = await self.search(query)
            except Exception as exc:  # noqa: BLE001 - degrade honestly
                failed = True
                log.warning("skillhub.search_failed", error=type(exc).__name__)
                hits = []
            for hit in hits:
                if len(used) >= self._max_skills or remaining < MIN_USEFUL_CHARS:
                    break
                if hit.get("reviewStatus") != APPROVED:
                    continue
                await take(str(hit["id"]), str(hit.get("revision") or ""), APPROVED)

        if used:
            block = _HEADER + "\n\n" + "\n\n".join(sections)
            return SkillSelection(block=block, used=used, status=STATUS_LOADED)
        return SkillSelection(status=STATUS_UNAVAILABLE if failed else STATUS_NONE)

    async def skills_context(self, text: str, *, limit: int = 3) -> str:
        """The prompt block for ``text`` (used by the research engine)."""
        selection = await self.select("", text)
        return selection.block
