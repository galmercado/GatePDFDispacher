"""Ticket inbox: PDF files that arrive on WhatsApp from configured numbers are stored as tickets for a match.

Evolution pushes every received message to /webhooks/evolution (event messages.upsert). Only files from the numbers listed
on the Ticket inbox page are used; a PDF from anyone else is just listed there, so it can be allowed with one click."""
import asyncio
import hashlib
import logging
import re
from datetime import timedelta

from sqlalchemy.orm import Session

from . import config, data
from .database import SessionLocal
from .models import AppSetting, Event, InboxEntry, SpareTicket, Ticket, TicketSource, utcnow
from .pdfs import parse_ticket_header, read_ticket_meta, _letters
from .services import PdfProcessingError, WhatsAppError, add_ticket_pdf, evolution_media_base64

log = logging.getLogger("gate.inbox")
KEEP_UNKNOWN = 200          # how many "unknown sender" rows are kept


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or "").split("@")[0].split(":")[0])


def sender_numbers(data_: dict) -> set[str]:
    """Every number / id that identifies the sender of a message (phone jid, LID and the alternatives Baileys adds)."""
    key = data_.get("key") or {}
    group = str(key.get("remoteJid", "")).endswith("@g.us")
    fields = [key.get("participant"), key.get("participantAlt"), key.get("senderPn"), data_.get("senderPn"), data_.get("participant")]
    if not group:
        fields += [key.get("remoteJid"), key.get("remoteJidAlt")]
    return {d for d in (_digits(f) for f in fields if f) if len(d) >= 7}


def extract_document(data_: dict) -> dict | None:
    """{'file_name', 'mimetype', 'caption'} when the message carries a document, else None."""
    message = data_.get("message") or {}
    for _ in range(3):                                   # documentWithCaptionMessage / ephemeral wrappers
        inner = message.get("documentWithCaptionMessage") or message.get("ephemeralMessage") or message.get("viewOnceMessage")
        if not inner:
            break
        message = inner.get("message") or {}
    doc = message.get("documentMessage")
    if not isinstance(doc, dict):
        return None
    return {"file_name": str(doc.get("fileName") or doc.get("title") or ""), "mimetype": str(doc.get("mimetype") or ""),
            "caption": str(doc.get("caption") or "")}


def match_or_create_event(db: Session, header: dict, meta: dict) -> tuple[Event | None, bool, str]:
    """The event a ticket file belongs to: same opponent and same day. Without a date on the first page the nearest
    upcoming game against that opponent. Creates the event when the date is known but nothing matches.
    Returns (event, created, problem)."""
    opponent = header["opponent"]
    key = _letters(opponent)
    games = [e for e in db.query(Event).order_by(Event.event_date).all() if key and key in _letters(e.name)]
    when = meta["when"]
    if when is None:
        upcoming = [e for e in games if e.event_date >= utcnow() - timedelta(days=1)]
        if upcoming:
            return upcoming[0], False, ""
        return None, False, "No date found on the first page and no existing game against " + opponent + "."
    for event in games:
        if abs((event.event_date.date() - when.date()).days) == 0:
            return event, False, ""
    place = meta["location"] or ("" if header["away"] else data.DEFAULT_LOCATION)
    event = Event(name=f"{data.HOME_TEAM} {data.VERSUS} {opponent}", location=place,
                  event_date=when if meta["has_time"] else when.replace(hour=20))
    db.add(event)
    db.commit()
    return event, True, ""


def next_match(db: Session) -> Event | None:
    return (db.query(Event).filter(Event.event_date >= utcnow() - timedelta(days=1)).order_by(Event.event_date).first())


def choose_event(db: Session, source: TicketSource, content: bytes, caption: str, file_name: str) -> tuple[Event | None, str]:
    """The match a received file belongs to: the source's fixed match, else what the caption / file name and the first page
    say (like the upload API), else the next upcoming match. Returns (event, how)."""
    if source.event_id:
        event = db.get(Event, source.event_id)
        if event is not None:
            return event, "fixed match of this number"
    for header in (caption, re.sub(r"\.pdf$", "", file_name, flags=re.I)):
        try:
            info = parse_ticket_header(header or "")
        except ValueError:
            continue
        event, created, _ = match_or_create_event(db, info, read_ticket_meta(content))
        if event is not None:
            return event, ("created from the file" if created else "found from the file")
    event = next_match(db)
    return (event, "next upcoming match") if event is not None else (None, "")


def _prune_unknown(db: Session) -> None:
    old = (db.query(InboxEntry.id).filter(InboxEntry.status == "unknown").order_by(InboxEntry.id.desc()).offset(KEEP_UNKNOWN).all())
    if old:
        db.query(InboxEntry).filter(InboxEntry.id.in_([i for (i,) in old])).delete(synchronize_session=False)


def _entry(db: Session, **fields) -> InboxEntry:
    entry = InboxEntry(**fields)
    db.add(entry)
    db.commit()
    return entry


def _finish(db: Session, entry: InboxEntry, status: str, detail: str, event_id: int | None = None, pages: int = 0) -> str:
    entry.status, entry.detail, entry.event_id, entry.pages = status, detail, event_id, pages
    db.commit()
    return status


DOWNLOAD_ATTEMPTS = (0, 2.0, 5.0)      # seconds to wait before each try: Evolution may not have stored a just-received message yet


async def _download(fetch_media, instance: str, item: dict):
    last: WhatsAppError | None = None
    for wait in DOWNLOAD_ATTEMPTS:
        if wait:
            await asyncio.sleep(wait)
        try:
            return await fetch_media(instance, item)
        except WhatsAppError as exc:
            last = exc
    raise last or WhatsAppError("download failed")


async def handle_message(db: Session, instance: str, item: dict, fetch_media=None) -> str:
    """Process one received message. Returns the status written to the inbox ('' = nothing to do)."""
    fetch_media = fetch_media or evolution_media_base64
    key = item.get("key") or {}
    if key.get("fromMe") is True or not key.get("id"):
        return ""
    doc = extract_document(item)
    if doc is None:
        return ""
    is_pdf = "pdf" in doc["mimetype"].lower() or doc["file_name"].lower().endswith(".pdf")
    message_id = str(key["id"])
    if db.query(InboxEntry.id).filter(InboxEntry.message_id == message_id).first():
        return ""                                                       # webhook delivered twice
    numbers = sender_numbers(item)
    sender = sorted(numbers, key=len)[0] if numbers else ""
    common = dict(instance=instance, sender=sender, sender_name=str(item.get("pushName") or "")[:128],
                  message_id=message_id, file_name=doc["file_name"][:255])
    source = None
    if numbers:
        source = (db.query(TicketSource).filter(TicketSource.active.is_(True), TicketSource.phone.in_(numbers)).first())
    if source is None:
        if not is_pdf:
            return ""
        _entry(db, status="unknown", detail="Not one of the ticket numbers - allow it below to use its PDFs as tickets.", **common)
        _prune_unknown(db)
        db.commit()
        return "unknown"
    common["sender"] = source.phone
    if not is_pdf:
        _entry(db, status="rejected", detail="Not a PDF file.", **common)
        return "rejected"
    entry = _entry(db, status="processing", detail="Downloading the file…", **common)   # listed at once, so nothing is ever lost silently
    try:
        content, _mime, _name = await _download(fetch_media, instance, item)
    except WhatsAppError as exc:
        return _finish(db, entry, "error", f"Could not download the file from WhatsApp: {exc}")
    if not content.lstrip().startswith(b"%PDF"):
        return _finish(db, entry, "rejected", "The file is not a valid PDF.")
    entry.detail = "Reading the pages…"
    db.commit()
    event, how = await asyncio.to_thread(choose_event, db, source, content, doc["caption"], doc["file_name"])
    if event is None:
        return _finish(db, entry, "rejected", "No match to put the tickets in - create the match, or choose one for this number.")
    digest = f"tkh.{event.id}.{hashlib.sha256(content).hexdigest()[:24]}"
    if db.get(AppSetting, digest) and (db.query(Ticket).filter(Ticket.event_id == event.id).first()
                                       or db.query(SpareTicket).filter(SpareTicket.event_id == event.id).first()):
        return _finish(db, entry, "duplicate", "This exact file was already stored for this match.", event.id)
    try:
        result = await asyncio.to_thread(add_ticket_pdf, db, event.id, content)
    except PdfProcessingError as exc:
        return _finish(db, entry, "rejected", str(exc), event.id)
    db.merge(AppSetting(key=digest, value=utcnow().isoformat()))
    db.commit()
    parts = [f"Stored {result['pages']} page(s) for “{event.name}” ({how})"]
    if result["duplicates"]:
        parts.append(f"{len(result['duplicates'])} duplicate page(s) skipped")
    if result["assigned"]:
        parts.append(f"{result['assigned']} given to guests")
    return _finish(db, entry, "accepted" if result["pages"] else "duplicate", "; ".join(parts) + ".", event.id, result["pages"])


_QUEUE_LOCK = None      # created lazily inside the running loop
_QUEUE_LOOP = None


def _lock() -> asyncio.Lock:
    global _QUEUE_LOCK, _QUEUE_LOOP
    loop = asyncio.get_running_loop()
    if _QUEUE_LOCK is None or _QUEUE_LOOP is not loop:
        _QUEUE_LOCK, _QUEUE_LOOP = asyncio.Lock(), loop
    return _QUEUE_LOCK


async def process_webhook(body: dict, fetch_media=None) -> list[str]:
    """Background job for one messages.upsert webhook (never raises). Files that arrive together (15-20 PDFs in a row) are
    handled one after the other, in the order they came in."""
    out = []
    instance = str(body.get("instance") or body.get("instanceName") or "")
    items = body.get("data")
    async with _lock():
        for item in items if isinstance(items, list) else [items]:
            if not isinstance(item, dict):
                continue
            try:
                with SessionLocal() as db:
                    out.append(await handle_message(db, instance, item, fetch_media))
            except Exception as exc:
                log.exception("Could not process a received WhatsApp message")
                _record_failure(instance, item, exc)
    return out


def _record_failure(instance: str, item: dict, exc: Exception) -> None:
    """Whatever went wrong, the file shows up in the inbox as an error instead of disappearing."""
    try:
        key = item.get("key") or {}
        doc = extract_document(item) or {}
        with SessionLocal() as db:
            entry = db.query(InboxEntry).filter(InboxEntry.message_id == str(key.get("id", ""))).first()
            if entry is not None:
                entry.status, entry.detail = "error", f"Unexpected error: {exc}"[:500]
            elif key.get("id"):
                numbers = sender_numbers(item)
                db.add(InboxEntry(instance=instance, sender=sorted(numbers, key=len)[0] if numbers else "", message_id=str(key["id"]),
                                  file_name=str(doc.get("file_name", ""))[:255], status="error", detail=f"Unexpected error: {exc}"[:500]))
            db.commit()
    except Exception:
        log.exception("Could not record the failed message")
