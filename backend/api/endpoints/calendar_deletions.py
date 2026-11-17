"""Ask calendar-deletion routes: confirm or cancel a stored deletion proposal.

Default-deny like every route: not in ``PUBLIC_ROUTES``. The browser sends row
ids only; the server acts on the events it stored when it made the proposal.
A proposal is bound to the signed-in ``sub`` and lives 15 minutes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.calendar.delete_proposals import get_delete_store
from backend.context.ask_actions import ActionError
from backend.context.ask_delete import confirm_deletion

router = APIRouter(prefix="/api/calendar/deletions", tags=["calendar"])


class DeletionConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: Row ids the person left checked. Omitted: the rows selected by default.
    item_ids: list[str] | None = Field(default=None, max_length=200)
    #: Required only when the request did not say "all" or "next".
    scope: str | None = Field(default=None, max_length=20)


def _sub(request: Request) -> str:
    claims = getattr(request.state, "principal", None) or {}
    return str(claims.get("sub") or claims.get("sid") or "")


def _missing() -> HTTPException:
    return HTTPException(status_code=404, detail={"error_code": "proposal_not_found"})


@router.post("/{proposal_id}/confirm")
async def confirm_calendar_deletion(
    request: Request, proposal_id: str, body: DeletionConfirmRequest | None = None
) -> Any:
    store = get_delete_store(request.app.state)
    proposal = store.get(proposal_id, _sub(request))
    if proposal is None:
        raise _missing()
    if getattr(request.app.state, "repository_registry", None) is None:
        return {"accepted": True, "executed": False,
                "detail": "durable storage is not configured"}
    edits = body or DeletionConfirmRequest()
    try:
        return await confirm_deletion(
            request.app.state, store, proposal, item_ids=edits.item_ids, scope=edits.scope
        )
    except ActionError as exc:
        return JSONResponse(status_code=exc.status, content=exc.body())


@router.post("/{proposal_id}/cancel")
async def cancel_calendar_deletion(request: Request, proposal_id: str) -> dict[str, Any]:
    if not get_delete_store(request.app.state).cancel(proposal_id, _sub(request)):
        raise _missing()
    return {"status": "cancelled", "proposal_id": proposal_id}
