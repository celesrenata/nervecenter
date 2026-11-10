"""Select the observer's OmniRoute alias from NerveCenter's own combo settings.

Owner decision (NS-ADR-033): NerveShield reuses NerveCenter's ``Reader`` combo
instead of a hard-coded alias. The selection is only a *candidate*: every call
still goes through :class:`LocalOnlyRouteResolver`, which walks the selected
alias's full OmniRoute tree and denies if any hop (fallbacks included) is not
provably local. Choosing a combo can never make a cloud route pass.

Fail closed: a missing, duplicated, malformed or non-OmniRoute ``Reader`` row,
or an unreadable combo table, yields a typed deny and nothing is sent.
Re-read on every call (no cache), so an edit in NerveCenter or OmniRoute takes
effect on the next message. Library only; nothing in the pipeline calls this.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from backend.mail_security.route_guard import LocalOnlyRouteResolver, RouteDecision

DEFAULT_ROLE = "Reader"
DEFAULT_PROVIDER_NAMES = frozenset({"Omniroute"})

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


class SelectionReason(StrEnum):
    """Log-safe outcome codes. Everything except SELECTED is a deny."""

    SELECTED = "SELECTED"
    COMBOS_UNREADABLE = "COMBOS_UNREADABLE"
    ROLE_MISSING = "ROLE_MISSING"
    ROLE_AMBIGUOUS = "ROLE_AMBIGUOUS"
    ROLE_MALFORMED = "ROLE_MALFORMED"
    PROVIDER_NOT_OMNIROUTE = "PROVIDER_NOT_OMNIROUTE"
    ROUTE_DENIED = "ROUTE_DENIED"


class ComboSource(Protocol):
    """Read access to NerveCenter combos (``InferenceComboService`` fits)."""

    async def list(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ObserverRouteSelection:
    """Result of selecting and guarding the observer route.

    ``alias`` is set only when the role row was usable; ``route`` is set only
    when the resolver ran. ``allowed`` requires both a selection and an
    ALLOWED_LOCAL_ONLY route decision.
    """

    reason: SelectionReason
    role: str
    alias: str = ""
    route: RouteDecision | None = None

    @property
    def allowed(self) -> bool:
        return (
            self.reason is SelectionReason.SELECTED
            and self.route is not None
            and self.route.allowed
        )

    def log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "observer_role_combo": self.role,
            "observer_selection": self.reason.value,
            "observer_route_allowed": self.allowed,
        }
        if self.route is not None:
            fields.update(self.route.log_fields())
        return fields


def select_alias(
    rows: Sequence[Mapping[str, Any]],
    *,
    role: str = DEFAULT_ROLE,
    provider_names: frozenset[str] = DEFAULT_PROVIDER_NAMES,
) -> tuple[SelectionReason, str]:
    """Pick the alias bound to ``role``. Pure; returns (reason, alias or "")."""
    matches = [r for r in rows if isinstance(r, Mapping) and r.get("name") == role]
    if not matches:
        return SelectionReason.ROLE_MISSING, ""
    if len(matches) > 1:
        return SelectionReason.ROLE_AMBIGUOUS, ""
    row = matches[0]
    alias = row.get("model_id")
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        return SelectionReason.ROLE_MALFORMED, ""
    if row.get("provider_name") not in provider_names:
        return SelectionReason.PROVIDER_NOT_OMNIROUTE, ""
    return SelectionReason.SELECTED, alias


async def resolve_observer_route(
    combos: ComboSource,
    resolver: LocalOnlyRouteResolver,
    *,
    role: str = DEFAULT_ROLE,
    provider_names: frozenset[str] = DEFAULT_PROVIDER_NAMES,
) -> ObserverRouteSelection:
    """Read the role's combo, then guard it. Never raises; denies on any failure."""
    try:
        rows = await combos.list()
    except Exception:  # noqa: BLE001 - fail closed; DB errors may embed config text
        return ObserverRouteSelection(SelectionReason.COMBOS_UNREADABLE, role)
    if not isinstance(rows, list):
        return ObserverRouteSelection(SelectionReason.COMBOS_UNREADABLE, role)
    reason, alias = select_alias(rows, role=role, provider_names=provider_names)
    if reason is not SelectionReason.SELECTED:
        return ObserverRouteSelection(reason, role)
    decision = await resolver.resolve(alias)
    if not decision.allowed:
        return ObserverRouteSelection(SelectionReason.ROUTE_DENIED, role, alias, decision)
    return ObserverRouteSelection(SelectionReason.SELECTED, role, alias, decision)
