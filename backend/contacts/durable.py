"""Mac Contacts -> durable people (owner: "contacts: ok", NS-ADR-097).

The relay posts the owner's Mac Contacts (name, phones, emails, thumbnail)
to ``POST /api/relay/contacts``. :func:`apply_contacts` merges them into the
existing person model:

- a contact is matched to an existing person by its stored contact id, else
  by any phone number or email address it shares with an existing identity
  (phones compared in E.164 form, emails lower-cased); otherwise a new
  person is created;
- the contact name replaces a person's name only when that name is still a
  bare handle (a number or an address) or is the name Contacts set last time
  (so a name the owner or another source chose is kept);
- missing phone/email identities are added; nothing is deleted;
- the thumbnail goes to the encrypted object store under ``contacts/``; the
  person keeps only the key (``notes["contacts"]["photo_ref"]``).

:func:`hydrate_views` then pushes names into the live Messages plane (so a
thread from ``+15551234567`` shows the contact name) and fills the Contacts
screen's in-memory service. Both are idempotent. Nothing here logs a name,
number, address or photo.
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

PHOTO_PREFIX = "contacts/"
MAX_PHOTO_BYTES = 96 * 1024
ALLOWED_PHOTO_TYPES = frozenset({"image/jpeg", "image/png"})
_HANDLE_LIKE = re.compile(r"^\+?[\d\s().-]+$|@")


def normalize_phone(raw: str) -> str | None:
    """E.164-ish key: ``(555) 123-4567`` -> ``+15551234567`` (US default)."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if raw.strip().startswith("+") or len(digits) > 11:
        return f"+{digits}"
    return digits


def normalize_handle(handle: str) -> str | None:
    handle = (handle or "").strip()
    if "@" in handle:
        return handle.lower()
    return normalize_phone(handle)


def _photo_key(uid: str) -> str:
    return PHOTO_PREFIX + hashlib.sha256(uid.encode()).hexdigest()[:32]


def _is_handle_like(name: str | None) -> bool:
    return not name or bool(_HANDLE_LIKE.search(name))


async def apply_contacts(
    registry: Any, object_store: Any, contacts: list[dict[str, Any]]
) -> dict[str, int]:
    """Merge one batch of relay contacts into people. Returns counts only."""
    from sqlalchemy import select

    from backend.persistence.models.personal import PersonIdentityRow, PersonRow

    out = {"created": 0, "matched": 0, "renamed": 0, "identities": 0, "photos": 0, "skipped": 0}
    async with registry.session_factory() as session:
        people = {p.id: p for p in (await session.execute(select(PersonRow))).scalars()}
        by_uid = {
            (p.notes or {}).get("contacts", {}).get("uid"): p.id
            for p in people.values()
            if isinstance((p.notes or {}).get("contacts"), dict)
        }
        by_handle: dict[str, str] = {}
        owned: dict[str, set[str]] = {}
        for ident in (await session.execute(select(PersonIdentityRow))).scalars():
            key = normalize_handle(ident.handle)
            if key:
                by_handle.setdefault(key, ident.person_id)
                owned.setdefault(ident.person_id, set()).add(key)

        for contact in contacts:
            uid = str(contact.get("uid") or "").strip()
            name = str(contact.get("display_name") or "").strip()[:256]
            phones = [normalize_phone(str(p.get("number") or ""))
                      for p in contact.get("phones") or []]
            emails = [normalize_handle(str(e.get("address") or ""))
                      for e in contact.get("emails") or []]
            keys = [("phone", k) for k in phones if k]
            keys += [("email", k) for k in emails if k and "@" in k]
            if not uid or not name or not keys:
                out["skipped"] += 1
                continue
            person_id = by_uid.get(uid) or next(
                (by_handle[k] for _, k in keys if k in by_handle), None
            )
            person = people.get(person_id) if person_id else None
            if person is None:
                person = PersonRow(display_name=name, notes={})
                session.add(person)
                await session.flush()
                people[person.id] = person
                out["created"] += 1
            else:
                out["matched"] += 1
            notes = dict(person.notes or {})
            previous = notes.get("contacts") if isinstance(notes.get("contacts"), dict) else None
            from_contacts = previous is not None and previous.get("name") == person.display_name
            if person.display_name != name and (
                from_contacts or _is_handle_like(person.display_name)
            ):
                person.display_name = name
                out["renamed"] += 1
            info: dict[str, Any] = {"uid": uid, "source": "mac", "name": name}
            photo = await _store_photo(object_store, uid, contact)
            if photo is not None:
                info.update(photo)
                out["photos"] += 1
            elif previous and previous.get("photo_ref"):
                info.update({k: previous[k] for k in ("photo_ref", "photo_type") if k in previous})
            notes["contacts"] = info
            person.notes = notes
            by_uid[uid] = person.id
            mine = owned.setdefault(person.id, set())
            for channel, key in keys:
                if key in mine:
                    continue
                session.add(PersonIdentityRow(person_id=person.id, channel=channel, handle=key,
                                              resolved=True))
                mine.add(key)
                by_handle.setdefault(key, person.id)
                out["identities"] += 1
        await session.commit()
    return out


async def _store_photo(
    object_store: Any, uid: str, contact: dict[str, Any]
) -> dict[str, str] | None:
    raw = contact.get("photo_b64")
    ptype = contact.get("photo_type")
    if object_store is None or not raw or ptype not in ALLOWED_PHOTO_TYPES:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    if not data or len(data) > MAX_PHOTO_BYTES:
        return None
    if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
        return None
    key = _photo_key(uid)
    try:
        await object_store.put(key, data, content_type=ptype)
    except Exception as exc:  # noqa: BLE001 - a photo is optional
        log.warning("contacts.photo_store_failed", error_type=type(exc).__name__)
        return None
    return {"photo_ref": key, "photo_type": ptype}


async def contact_people(registry: Any) -> list[tuple[Any, list[Any]]]:
    """People that came from Contacts, each with its identities."""
    from sqlalchemy import select

    from backend.persistence.models.personal import PersonIdentityRow, PersonRow

    async with registry.session_factory() as session:
        people = [
            p for p in (await session.execute(select(PersonRow))).scalars()
            if isinstance((p.notes or {}).get("contacts"), dict)
        ]
        idents: dict[str, list[Any]] = {}
        if people:
            for ident in (
                await session.execute(
                    select(PersonIdentityRow).where(
                        PersonIdentityRow.person_id.in_([p.id for p in people])
                    )
                )
            ).scalars():
                idents.setdefault(ident.person_id, []).append(ident)
    return [(p, idents.get(p.id, [])) for p in people]


async def hydrate_views(
    registry: Any, messages_service: Any, contacts_service: Any
) -> dict[str, int]:
    """Push contact names into Messages and fill the Contacts screen. Never raises."""
    out = {"people": 0, "bindings": 0}
    if registry is None:
        return out
    try:
        rows = await contact_people(registry)
    except Exception as exc:  # noqa: BLE001 - the screens must not fail on this
        log.warning("contacts.hydrate_failed", error_type=type(exc).__name__)
        return out
    for person, idents in rows:
        handles = sorted({i.handle for i in idents})
        if messages_service is not None:
            for handle in handles:
                messages_service.register_person(handle, person.display_name)
                out["bindings"] += 1
        if contacts_service is not None:
            contacts_service.ingest({
                "person_id": person.id,
                "display_name": person.display_name,
                "emails": [h for h in handles if "@" in h],
                "phones": [h for h in handles if "@" not in h],
                "source": "personal-relay",
                "is_vip": bool(person.vip),
            })
        out["people"] += 1
    return out


async def photo_for(registry: Any, object_store: Any, *, person_id: str | None = None,
                    handle: str | None = None) -> tuple[bytes, str] | None:
    """The stored thumbnail for a person (by id or by any of their handles)."""
    from sqlalchemy import select

    from backend.persistence.models.personal import PersonIdentityRow, PersonRow

    if registry is None or object_store is None:
        return None
    async with registry.session_factory() as session:
        if person_id is None and handle:
            key = normalize_handle(handle)
            if not key:
                return None
            ident = (
                await session.execute(
                    select(PersonIdentityRow).where(PersonIdentityRow.handle == key).limit(1)
                )
            ).scalars().first()
            person_id = ident.person_id if ident else None
        person = await session.get(PersonRow, person_id) if person_id else None
    info = (person.notes or {}).get("contacts") if person is not None else None
    if not isinstance(info, dict) or not str(info.get("photo_ref", "")).startswith(PHOTO_PREFIX):
        return None
    try:
        data = await object_store.get(info["photo_ref"])
    except Exception:  # noqa: BLE001 - missing photo is a 404
        return None
    return data, str(info.get("photo_type") or "image/jpeg")


__all__ = ["apply_contacts", "hydrate_views", "normalize_handle", "normalize_phone", "photo_for"]
