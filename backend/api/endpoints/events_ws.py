"""Realtime WebSocket endpoint — ``GET /api/events/ws``.

This is the missing realtime half of the stack (audit gaps U-6 / CC-1): the
REST endpoints under :mod:`backend.api.endpoints.events` expose the event
*history*, but nothing pushed *live* updates to the web client. This route
streams live events/status from the NATS JetStream event bus to every connected
WebSocket client.

Design
------
- One **ephemeral** JetStream pull subscription per connection. Ephemeral is
  deliberate: the app's own event processing uses a *durable* consumer
  (:class:`~backend.events.subscriber.EventSubscriber`); reusing that durable
  consumer for browser fan-out would collide (one fetcher per durable consumer)
  and leak persistent consumers across connections. An ephemeral consumer is
  scoped to the connection and dropped on disconnect.
- Messages are decoded with the same :class:`~backend.events.models.EventEnvelope`
  wire format the publisher writes, so clients receive the canonical
  :class:`schemas.events.Event` payload.

Honesty contract
----------------
- Event bus **not configured or not connected** → the client receives a single
  ``{"type":"error", ...}`` frame and the socket closes cleanly (no fake stream,
  no silent hang).
- Each live event is forwarded as ``{"type":"update","payload":<event>}``; a
  periodic ``{"type":"status",...}`` heartbeat keeps idle connections (and the
  nginx ``proxy_read_timeout``) warm.

These frame types match the web ``WebSocketClient`` / ``useRealtimeUpdates``
contract (``type: "update" | "status" | "error"``).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from backend.api import auth
from backend.events.models import EventEnvelope

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/events", tags=["events-ws"])

#: Idle-fetch window (seconds). Doubles as the heartbeat cadence: when no event
#: arrives within this window we emit a status frame to keep the connection and
#: the upstream proxy warm. 30s is comfortably under the nginx ``proxy_read_timeout``.
FETCH_TIMEOUT_S = 30.0

#: Seconds a new connection has to send its auth frame, and the close code used
#: when it does not (or the credential is not a signed-in person).
AUTH_TIMEOUT_S = 5.0
CLOSE_UNAUTHORIZED = 4401


def _frame(kind: str, payload: Any) -> str:
    return json.dumps({"type": kind, "payload": payload}, default=str)


async def _authenticate_first_frame(websocket: WebSocket) -> bool:
    """Read ``{"type":"auth","token":...}`` and verify it as a person (NS-ADR-122).

    A browser WebSocket cannot send an ``Authorization`` header, so the first
    frame carries the bearer token. On any failure the socket closes with 4401.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_S)
        frame = json.loads(raw)
        is_auth = isinstance(frame, dict) and frame.get("type") == "auth"
        token = frame.get("token") if is_auth else None
        if not isinstance(token, str) or not token:
            raise ValueError("no auth frame")
        await auth._authenticate_default(websocket, token)
    except WebSocketDisconnect:
        return False
    except (TimeoutError, ValueError, HTTPException):
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return False
    return True


async def _forward_event(websocket: WebSocket, subscription: Any, subject: str) -> None:
    """Pull events from the stream and forward each to the socket until done.

    Acks every message it forwards (JetStream semantics) so it is not
    redelivered. Runs until ``websocket`` is closed or the caller cancels the
    enclosing task.
    """
    while True:
        try:
            msgs = await subscription.fetch(1, timeout=FETCH_TIMEOUT_S)
        except TimeoutError:  # noqa: PERF203 - heartbeat, not an error
            await websocket.send_text(_frame("status", {"ok": True, "subject": subject}))
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a transport hiccup must not drop the app
            log.warning("events.ws.fetch_error", error=str(exc))
            await websocket.send_text(_frame("error", {"message": f"event bus read error: {exc}"}))
            await asyncio.sleep(0.5)
            continue

        if not msgs:
            continue

        for msg in msgs:
            try:
                envelope = EventEnvelope.from_json(msg.data, subject=getattr(msg, "subject", subject))
                payload = envelope.event.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 - malformed frame must not kill the stream
                log.warning("events.ws.decode_error", error=str(exc))
                payload = {"subject": subject, "decode_error": str(exc)}
            await websocket.send_text(_frame("update", payload))
            # Ack so this consumer's redelivery budget isn't consumed.
            try:
                await msg.ack()
            except Exception:  # noqa: BLE001 - best-effort ack
                pass


@router.websocket("/ws")
async def events_websocket(websocket: WebSocket) -> None:
    """Accept a realtime connection and stream live events until disconnect."""
    await websocket.accept()
    if not await _authenticate_first_frame(websocket):
        return

    nats = getattr(websocket.app.state, "nats_client", None)
    if nats is None:
        await websocket.send_text(
            _frame("error", {"message": "event bus is not configured (no NATS URL set)"})
        )
        await websocket.close()
        return
    if not nats.is_connected:
        await websocket.send_text(
            _frame("error", {"message": "event bus is configured but not connected"})
        )
        await websocket.close()
        return

    try:
        js = nats.jetstream
    except RuntimeError as exc:
        await websocket.send_text(_frame("error", {"message": f"event bus unavailable: {exc}"}))
        await websocket.close()
        return

    subject = nats.config.events_subject
    try:
        # Ephemeral (no durable) pull consumer — one per connection.
        subscription = await js.pull_subscribe(subject)
    except Exception as exc:  # noqa: BLE001 - subscribe failure is a clean close
        await websocket.send_text(_frame("error", {"message": f"subscribe failed: {exc}"}))
        await websocket.close()
        return

    await websocket.send_text(_frame("status", {"connected": True, "subject": subject}))
    log.info("events.ws.connected", subject=subject)

    try:
        await _forward_event(websocket, subscription, subject)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await subscription.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        log.info("events.ws.disconnected")
