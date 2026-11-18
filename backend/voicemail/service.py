"""In-memory Voicemail service (prompt2 Phase P2 — Personal Operations Plane).

This is the P2 slice of the Personal Operations Plane: it receives
*normalized* voicemail events from the macOS Personal Relay, reconstructs
threads, and produces
:class:`~backend.api.models.AttentionItem` entries so an unread thread
surfaces in the Now screen (prompt2 §77 acceptance: "a new voicemail thread
appears inside NerveCenter and can produce an AttentionItem").

Design notes
------------
- **NerveCenter never touches voicemail audio.** The Personal Relay daemon is
  the ingestion mechanism and it is *out of scope for this deliverable* (the
  §46/§47-style source-side bridge: iOS Share Sheet capture, carrier
  voicemail-to-email, and local Whisper transcription, §14/§15). By the time an
  event reaches this service the transcript and provenance are already
  computed; this service only ever sees normalized voicemail events posted via
  ``POST /api/voicemail/ingest`` — no audio, no model, no carrier access
  in-process.
- **Write-through persistence.** Process memory is the read model, so the
  screens and tests work without ``NERVE_DB_URL`` (same convention as the
  P0/P1 stores). When a :class:`VoicemailRepository` is attached
  (:meth:`attach_store`), every mutation is recorded as dirty and
  :meth:`flush` writes it to Postgres; :meth:`hydrate` reloads everything at
  startup. Dirty state survives a failed flush, so a retried request persists
  it even though the in-memory ingest is already a duplicate.
- **Idempotent ingest.** Voicemail ids are unique: re-posting the same
  ``voicemail_id`` is a no-op, which matters because the relay replays queued
  events when connectivity returns (§48) and must preserve ordering.
- **Deterministic, explainable.** Attention generation reuses the P0
  :class:`~backend.attention.service.AttentionService` scorer, so "why is
  this thread on my Now screen" is always answerable (§21/§22). Callback-request
  detection is a deterministic keyword heuristic on the already-transcribed
  text (no model, §15) and records the matched phrase in ``callback_reason``.
- **Detection, not action.** A callback request only flags the thread and
  escalates its AttentionItem; nothing is dialed or drafted from here (§77
  has no reply-drafting for voicemail).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from backend.api.models import (
    AttentionState,
    Voicemail,
    VoicemailSource,
    VoicemailThread,
)
from backend.attention.service import AttentionService, get_attention_service

# Callback-request detection (prompt2 §15/§77 — example: "Please call us
# regarding your appointment" → CALLBACK REQUIRED). A deterministic keyword
# heuristic over the *already-transcribed* transcript: longest, most specific
# phrases first so the recorded ``callback_reason`` names the strongest match.
# Case-insensitive, word-boundary-anchored, so "call us" does not fire on
# "call-usage" or "a callback to the call-center policy".
_CALLBACK_PHRASES = (
    "call me back",
    "call us back",
    "ring me back",
    "ring us back",
    "give me a call",
    "give us a call",
    "please call me",
    "please call us",
    "call me",
    "call us",
    "call back",
    "callback",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None) -> datetime | None:
    """Coerce a datetime (or ISO-8601 string, as the relay posts) to UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _phone_key(caller_phone: str) -> str:
    """Thread key from a caller phone number: strip non-digits for the key,
    keep the display format separately (§77 thread reconstruction — one
    thread per caller number). ``+1 415-555-0134`` and ``14155550134`` land
    on the same thread. A number with no digits falls back to its stripped
    form so the key is never empty."""
    digits = re.sub(r"\D", "", caller_phone)
    return digits or caller_phone.strip().lower()


def _thread_key(caller_phone: str) -> str:
    """One thread per caller phone number (non-digits stripped for the key)."""
    return f"vthr-{_phone_key(caller_phone)}"


def detect_callback(transcript: str) -> str | None:
    """Deterministic callback-request detection (§15/§77) on a transcript.

    Returns the matched keyword/phrase (the explainable ``callback_reason``),
    or ``None`` for an ordinary message. No randomness, no model — the same
    transcript always yields the same result.
    """
    text = (transcript or "").lower()
    for phrase in _CALLBACK_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return phrase
    return None


class VoicemailService:
    """In-memory voicemail ingestion, threading, and attention generation (P2)."""

    def __init__(self, attention: AttentionService | None = None) -> None:
        self._attention = attention or get_attention_service()
        self._voicemails: dict[str, Voicemail] = {}
        self._threads: dict[str, VoicemailThread] = {}
        self._store: Any = None
        self._dirty_vms: set[str] = set()
        self._dirty_threads: set[str] = set()
        self._deleted_vms: set[str] = set()
        self._deleted_threads: set[str] = set()
        self._flush_lock = asyncio.Lock()

    # -- persistence ---------------------------------------------------------

    def attach_store(self, store: Any) -> None:
        """Attach a durable :class:`VoicemailRepository` (write-through)."""
        self._store = store

    @property
    def has_store(self) -> bool:
        return self._store is not None

    async def hydrate(self) -> dict[str, int]:
        """Load stored voicemails and threads into memory (startup)."""
        if self._store is None:
            return {"voicemails": 0, "threads": 0}
        vm_rows, thread_rows = await self._store.load_all()
        for row in vm_rows:
            row["received_at"] = _ensure_utc(row["received_at"])
            self._voicemails[row["voicemail_id"]] = Voicemail(**row)
        for row in thread_rows:
            for key in ("last_received_at", "created_at", "updated_at"):
                row[key] = _ensure_utc(row[key])
            thread = VoicemailThread(**row)
            # The attention service is in-memory; a stored id may be stale.
            if thread.attention_id and self._attention.get(thread.attention_id) is None:
                thread.attention_id = None
            self._threads[thread.thread_id] = thread
        return {"voicemails": len(vm_rows), "threads": len(thread_rows)}

    async def flush(self) -> None:
        """Write pending changes to the store. Raises if the write fails,
        leaving the changes pending so the next flush retries them."""
        if self._store is None:
            return
        async with self._flush_lock:
            vm_ids, self._dirty_vms = self._dirty_vms, set()
            thread_ids, self._dirty_threads = self._dirty_threads, set()
            del_vms, self._deleted_vms = self._deleted_vms, set()
            del_threads, self._deleted_threads = self._deleted_threads, set()
            if not (vm_ids or thread_ids or del_vms or del_threads):
                return
            try:
                await self._store.apply(
                    voicemails=[
                        self._voicemails[i].model_dump() for i in vm_ids if i in self._voicemails
                    ],
                    threads=[
                        self._threads[i].model_dump() for i in thread_ids if i in self._threads
                    ],
                    deleted_voicemail_ids=del_vms,
                    deleted_thread_ids=del_threads,
                )
            except Exception:
                self._dirty_vms |= vm_ids
                self._dirty_threads |= thread_ids
                self._deleted_vms |= del_vms
                self._deleted_threads |= del_threads
                raise

    # -- ingestion -----------------------------------------------------------
    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest one normalized voicemail (as posted by the Personal Relay).

        Returns ``{"voicemail": Voicemail, "thread": VoicemailThread,
        "new_attention": AttentionItem | None, "duplicate": bool}``.
        Re-ingesting an already-seen ``voicemail_id`` returns
        ``duplicate=True`` and changes nothing (§48 replay safety).
        """
        voicemail_id = payload["voicemail_id"]
        if voicemail_id in self._voicemails:
            thread = self._threads[self._voicemails[voicemail_id].thread_id]
            return {"voicemail": self._voicemails[voicemail_id], "thread": thread, "new_attention": None, "duplicate": True}

        timestamp = _ensure_utc(payload["received_at"]) or _now()
        caller_phone = str(payload["caller_phone"]).strip()
        caller_name = payload.get("caller_name")
        transcript = str(payload.get("transcript", ""))
        duration = int(payload.get("duration_seconds", 0) or 0)
        source = VoicemailSource(payload.get("source", VoicemailSource.SHARE_SHEET.value))

        is_read = bool(payload.get("read", False))
        callback_reason = detect_callback(transcript)
        needs_callback = callback_reason is not None

        voicemail = Voicemail(
            voicemail_id=voicemail_id,
            thread_id=_thread_key(caller_phone),
            caller_phone=caller_phone,
            caller_name=caller_name,
            transcript=transcript,
            duration_seconds=duration,
            received_at=timestamp,
            read=is_read,
            needs_callback=needs_callback,
            callback_reason=callback_reason,
            source=source,
        )
        self._voicemails[voicemail_id] = voicemail
        self._dirty_vms.add(voicemail_id)
        self._deleted_vms.discard(voicemail_id)
        thread = self._threads.get(voicemail.thread_id)
        if thread is None:
            thread = VoicemailThread(
                thread_id=voicemail.thread_id,
                caller_phone=caller_phone,
                caller_name=caller_name,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._threads[thread.thread_id] = thread

        # Thread update: counts + unread bookkeeping. A voicemail is always an
        # inbound from the caller; acknowledging is the only way to clear it.
        if not is_read:
            thread.unread += 1
        thread.voicemail_count += 1
        thread.last_transcript = transcript
        thread.last_duration_seconds = duration
        thread.last_received_at = timestamp
        thread.updated_at = _now()
        if caller_name and thread.caller_name != caller_name:
            thread.caller_name = caller_name
        # The thread's callback flag reflects its latest voicemail, so the
        # list badge and the AttentionItem track the most recent ask.
        thread.needs_callback = needs_callback
        thread.callback_reason = callback_reason
        self._dirty_threads.add(thread.thread_id)
        self._deleted_threads.discard(thread.thread_id)
        # Attention generation (§77): a fresh voicemail the user has not yet
        # engaged with (unread == 1) — or one carrying a callback request —
        # (re)creates the thread's AttentionItem. A source keeps exactly ONE
        # live item: if the thread already has an unresolved item we *refresh*
        # it in place instead of stacking duplicates.
        new_attention = None
        # A voicemail already read on the phone raises no attention.
        should_escalate = not is_read and (thread.unread == 1 or needs_callback)
        if should_escalate:
            apayload = self._attention_payload(thread, voicemail)
            existing = self._attention.get(thread.attention_id) if thread.attention_id else None
            still_live = (
                existing is not None
                and existing.status in (AttentionState.NOW, AttentionState.TODAY, AttentionState.SOON, AttentionState.WAITING_ON)
            )
            if still_live and thread.attention_id is not None:
                new_attention = self._attention.refresh(thread.attention_id, apayload)
            else:
                new_attention = self._attention.create(apayload)
                thread.attention_id = new_attention.attention_id

        return {
            "voicemail": voicemail,
            "thread": thread,
            "new_attention": new_attention,
            "duplicate": False,
        }

    def delete(self, voicemail_id: str) -> dict[str, Any]:
        """Remove one voicemail (retraction from the relay) and recompute its
        thread. Unknown ids are a no-op (``found=False``) so replays are safe.
        When the last voicemail of a thread goes, the thread is removed and its
        AttentionItem resolved. Returns ``{"found": bool, "thread": ... | None}``.
        """
        voicemail = self._voicemails.pop(voicemail_id, None)
        if voicemail is None:
            return {"found": False, "thread": None}
        self._dirty_vms.discard(voicemail_id)
        self._deleted_vms.add(voicemail_id)
        thread = self._threads.get(voicemail.thread_id)
        if thread is None:
            return {"found": True, "thread": None}
        remaining = self.get_voicemails(thread.thread_id)
        if not remaining:
            if thread.attention_id:
                self._attention.resolve(thread.attention_id)
            del self._threads[thread.thread_id]
            self._dirty_threads.discard(thread.thread_id)
            self._deleted_threads.add(thread.thread_id)
            return {"found": True, "thread": None}
        newest = remaining[-1]
        thread.voicemail_count = len(remaining)
        thread.unread = sum(1 for m in remaining if not m.read)
        thread.last_transcript = newest.transcript
        thread.last_duration_seconds = newest.duration_seconds
        thread.last_received_at = newest.received_at
        thread.needs_callback = newest.needs_callback
        thread.callback_reason = newest.callback_reason
        thread.updated_at = _now()
        self._dirty_threads.add(thread.thread_id)
        if thread.unread == 0 and thread.attention_id:
            self._attention.resolve(thread.attention_id)
        return {"found": True, "thread": thread}

    def _attention_payload(self, thread: VoicemailThread, voicemail: Voicemail) -> dict[str, Any]:
        who = thread.caller_name or thread.caller_phone
        if voicemail.needs_callback:
            reason = f"Call {who} back — “{voicemail.callback_reason}”"
            urgency = 0.9
            why = [f"unread inbound voicemail from {who}", f"callback-request detected: matched “{voicemail.callback_reason}”"]
        else:
            reason = f"New voicemail from {who}"
            urgency = 0.6
            why = [f"unread inbound voicemail from {who}"]
        return {
            "reason": reason,
            "urgency": urgency,
            "importance": 0.6,
            "entities": [who],
            "sources": ["voicemail"],
            "suggested_action": f"Call {who} back" if voicemail.needs_callback else f"Listen to the voicemail from {who}",
            "status": AttentionState.NOW.value,
            "why": why,
        }

    # -- queries -------------------------------------------------------------
    def list_threads(self) -> list[VoicemailThread]:
        """Threads, most recently active first."""
        return sorted(
            self._threads.values(),
            key=lambda t: (t.last_received_at or datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )

    def get_thread(self, thread_id: str) -> VoicemailThread | None:
        return self._threads.get(thread_id)

    def get_voicemails(self, thread_id: str) -> list[Voicemail]:
        """All voicemails in a thread, chronological (thread reconstruction)."""
        msgs = [m for m in self._voicemails.values() if m.thread_id == thread_id]
        return sorted(msgs, key=lambda m: m.received_at)

    # -- engagement ----------------------------------------------------------
    def acknowledge(self, thread_id: str) -> VoicemailThread | None:
        """Mark the thread read: clear the unread count, resolve its
        AttentionItem (the human acted on it), and mark every voicemail in it
        as read."""
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        thread.unread = 0
        thread.updated_at = _now()
        self._dirty_threads.add(thread_id)
        for m in self.get_voicemails(thread_id):
            m.read = True
            self._dirty_vms.add(m.voicemail_id)
        if thread.attention_id:
            self._attention.resolve(thread.attention_id)
        return thread

    # -- seeding / tests -----------------------------------------------------
    def seed_demo(self) -> None:
        """Dev-mode seeder (NERVE_SEED_VOICEMAILS=1): a small, realistic
        thread so the Voicemail screen has content before the relay posts
        real traffic — including one callback-request voicemail (§15 example)."""
        base = _now()
        self.ingest({
            "voicemail_id": "vm-seed-1",
            "caller_phone": "+1 415-555-0134",
            "caller_name": "Alice Nguyen",
            "transcript": "Hi — just confirming our meeting is still on for Thursday.",
            "duration_seconds": 18,
            "received_at": base,
            "source": "share-sheet",
        })
        self.ingest({
            "voicemail_id": "vm-seed-2",
            "caller_phone": "+1 415-555-0134",
            "caller_name": "Alice Nguyen",
            "transcript": "Oh, and please call us regarding your appointment.",
            "duration_seconds": 11,
            "received_at": base,
            "source": "share-sheet",
        })


# -- process-wide singleton (mirrors the P0 attention service) ---------------
_service: VoicemailService | None = None


def get_voicemail_service() -> VoicemailService:
    global _service
    if _service is None:
        _service = VoicemailService()
    return _service


def reset_voicemail_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = VoicemailService()
