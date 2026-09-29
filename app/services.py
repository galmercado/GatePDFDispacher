"""Bulk import parsing, PDF splitting/mapping and WhatsApp delivery (Evolution API)."""
import base64
import io
import asyncio
import json
import logging
import random
import re
import shutil
import time
from pathlib import Path

import httpx
import pandas as pd
from pydantic import BaseModel, ValidationError, field_validator, model_validator
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from . import config, data
from .models import Attendee, SpareTicket, Ticket

log = logging.getLogger("gate.whatsapp")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

COLUMN_ALIASES = {
    "name": {"name", "full name", "fullname", "guest", "guest name", "nombre"},
    "phone": {"phone", "phone number", "mobile", "whatsapp", "cell", "telefono", "teléfono"},
    "email": {"email", "e-mail", "email address", "correo"},
    "age": {"age", "edad"},
    "category": {"category", "attendee type", "type", "group", "סוג", "קטגוריה", "סוג משתתף"},
    "adult_count": {"adult_count", "adult", "adults", "adult tickets", "מבוגר", "מבוגרים"},
    "youth_count": {"youth_count", "youth", "youths", "youth tickets", "נוער"},
    # legacy total: treated as adult tickets when no adult/youth columns are given
    "ticket_count": {"ticket_count", "ticket count", "tickets", "qty", "quantity", "count", "כרטיסים"},
    "notes": {"notes", "note", "comments", "notas"},
}


def normalize_phone(raw) -> str:
    """Return digits-only international format, adding DEFAULT_COUNTRY_CODE to local numbers.

    +972 52-560-7772 / 00972525607772 / 972525607772 / 052-5607772 / 525607772
        -> 972525607772. An explicit "+" or "00" prefix always means "already international".
    Idempotent, so it is safe to apply again at send time.
    """
    text = str(raw if raw is not None else "").strip()
    digits = re.sub(r"\D", "", text)
    cc = config.DEFAULT_COUNTRY_CODE
    if text.startswith("+") or digits.startswith("00"):
        digits = digits.removeprefix("00")
        # "+972 (0)52..." - drop the redundant trunk 0 after the country code
        return cc + digits[len(cc) + 1:] if cc and digits.startswith(cc + "0") else digits
    if digits.startswith("0"):
        return cc + digits.lstrip("0")
    if cc and digits.startswith(cc):
        return digits
    if cc and len(digits) <= 10:  # a national number without its leading 0
        return cc + digits
    return digits  # long number without "+": assume it already carries a country code


class ImportValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class PdfProcessingError(ValueError):
    pass


class AttendeeIn(BaseModel):
    name: str
    phone: str
    email: str | None = None
    age: int | None = None
    category: str = data.DEFAULT_CATEGORY
    adult_count: int = 0
    youth_count: int = 0
    ticket_count: int = 0  # derived; as input it is a legacy "total" meaning adult tickets
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(str(v).split())
        if not v:
            raise ValueError("name is required")
        if len(v) > 255:
            raise ValueError("name is too long")
        return v

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v) -> str:
        if not 7 <= len(re.sub(r"\D", "", str(v if v is not None else ""))) <= 15:
            raise ValueError("phone must contain 7-15 digits")
        phone = normalize_phone(v)
        if not 8 <= len(phone) <= 15:
            raise ValueError("phone number is not a valid international number")
        return phone

    @field_validator("email", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is not None and not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v.lower() if v else v

    @staticmethod
    def _whole(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")
        if f != int(f):
            raise ValueError("must be a whole number")
        return int(f)

    @field_validator("age", mode="before")
    @classmethod
    def _age_int(cls, v):
        return None if v is None or (isinstance(v, str) and not v.strip()) else cls._whole(v)

    @field_validator("age")
    @classmethod
    def _age(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v <= 120:
            raise ValueError("age must be between 0 and 120")
        return v

    @field_validator("adult_count", "youth_count", "ticket_count", mode="before")
    @classmethod
    def _count_int(cls, v):
        return 0 if v is None or (isinstance(v, str) and not v.strip()) else cls._whole(v)

    @field_validator("adult_count", "youth_count", "ticket_count")
    @classmethod
    def _count_range(cls, v: int) -> int:
        if not 0 <= v <= 50:
            raise ValueError("ticket counts must be between 0 and 50")
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        v = " ".join(str(v).split()) if v is not None else ""
        if not v:
            return data.DEFAULT_CATEGORY
        v = data.CATEGORY_ALIASES.get(v.lower(), v)
        if v not in data.CATEGORIES:
            raise ValueError("category must be one of: " + ", ".join(data.CATEGORIES))
        return v

    @model_validator(mode="after")
    def _totals(self):
        if not ({"adult_count", "youth_count"} & self.model_fields_set):
            self.adult_count = self.ticket_count or 1
        total = self.adult_count + self.youth_count
        if not 1 <= total <= 50:
            raise ValueError("an attendee needs 1-50 tickets in total (adult + youth)")
        self.ticket_count = total
        return self


# --------------------------------------------------------------------------- import

def _rows_from_xlsx(content: bytes) -> list[dict]:
    try:
        df = pd.read_excel(io.BytesIO(content), engine="openpyxl", dtype=str)
    except Exception as exc:
        raise ImportValidationError([f"Could not read the spreadsheet: {exc}"])
    df = df.fillna("")
    rename: dict[str, str] = {}
    for col in df.columns:
        key = str(col).strip().lower()
        for field, aliases in COLUMN_ALIASES.items():
            if key in aliases:
                rename[col] = field
    df = df.rename(columns=rename)
    missing = [f for f in ("name", "phone") if f not in df.columns]
    if missing:
        raise ImportValidationError([f"Missing required column(s): {', '.join(missing)}"])
    keep = [c for c in df.columns if c in COLUMN_ALIASES]
    return df[keep].to_dict(orient="records")


def _rows_from_json(content: bytes) -> list[dict]:
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportValidationError([f"Invalid JSON: {exc}"])
    if isinstance(data, dict):
        data = data.get("attendees")
    if not isinstance(data, list):
        raise ImportValidationError(['JSON must be a list or an object with an "attendees" list'])
    for i, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            raise ImportValidationError([f"Row {i}: expected an object"])
    return data


def parse_guest_list(filename: str, content: bytes) -> list[AttendeeIn]:
    """Parse and validate an .xlsx/.json guest list. All-or-nothing: any bad row aborts."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".xlsx":
        rows, offset = _rows_from_xlsx(content), 2  # header occupies spreadsheet row 1
    elif ext == ".json":
        rows, offset = _rows_from_json(content), 1
    else:
        raise ImportValidationError(["Unsupported file type: upload a .xlsx or .json file"])
    if not rows:
        raise ImportValidationError(["The file contains no guests"])

    parsed: list[AttendeeIn] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows):
        label = f"Row {i + offset}"
        row = {k: v for k, v in row.items() if v is not None and str(v).strip() != ""}
        try:
            item = AttendeeIn.model_validate(row)
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(p) for p in err["loc"]) or "row"
                msg = err["msg"].removeprefix("Value error, ")
                errors.append(f"{label} ({field}): {msg}")
            continue
        key = (item.name.lower(), item.phone)
        if key in seen:
            errors.append(f"{label}: duplicate of an earlier row ({item.name}, {item.phone})")
            continue
        seen.add(key)
        parsed.append(item)
    if errors:
        raise ImportValidationError(errors)
    return parsed


def import_attendees(db: Session, event_id: int, items: list[AttendeeIn]) -> tuple[int, int]:
    """Add guests; anyone already on the event (same name + phone) is skipped, so re-importing
    a list never duplicates people or inflates the ticket total. Returns (added, skipped)."""
    existing = {(a.name.lower(), a.phone) for a in
                db.query(Attendee.name, Attendee.phone).filter(Attendee.event_id == event_id)}
    new = [i for i in items if (i.name.lower(), i.phone) not in existing]
    db.add_all(Attendee(event_id=event_id, **item.model_dump()) for item in new)
    db.commit()
    return len(new), len(items) - len(new)


# --------------------------------------------------------------------------- PDFs

def event_ticket_dir(event_id: int) -> Path:
    return config.STORAGE_DIR / "tickets" / str(event_id)


def split_pdf(content: bytes, event_id: int, start: int = 1) -> list[Path]:
    """Split a multi-page PDF into single-page files (ticket_NNNN.pdf, numbered from `start`)
    in storage/tickets/{event_id}/. Existing files are never touched."""
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise PdfProcessingError("Encrypted PDFs are not supported")
        page_count = len(reader.pages)
    except PdfProcessingError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError) as exc:
        raise PdfProcessingError(f"Not a valid PDF: {exc}")
    if page_count == 0:
        raise PdfProcessingError("The PDF has no pages")

    out_dir = event_ticket_dir(event_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for index, page in enumerate(reader.pages, start=start):
        writer = PdfWriter()
        writer.add_page(page)
        path = out_dir / f"ticket_{index:04d}.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        paths.append(path)
    return paths


def ticket_status(db: Session, event_id: int) -> dict:
    """{'missing': tickets guests still lack, 'spare': unassigned pages, 'guests_missing': int}"""
    attendees = db.query(Attendee).options(selectinload(Attendee.tickets)).filter(
        Attendee.event_id == event_id).all()
    gaps = [a.ticket_count - len(a.tickets) for a in attendees]
    spare = db.query(SpareTicket).filter(SpareTicket.event_id == event_id).count()
    return {"missing": sum(g for g in gaps if g > 0), "guests_missing": sum(1 for g in gaps if g > 0),
            "spare": spare}


def rebalance_tickets(db: Session, event_id: int) -> dict:
    """Keep every guest's tickets in line with their ticket_count using the event's spare pages.

    1. Guests holding more tickets than they should (count lowered / guest removed elsewhere)
       return the surplus (highest pages first) to the spare pool.
    2. Guests missing tickets receive spare pages, lowest page first, in guest order.
    Returns {'assigned': n, 'missing': n, 'spare': n}.
    """
    db.expire_all()  # collections loaded earlier in the request may be stale
    attendees = (db.query(Attendee).options(selectinload(Attendee.tickets))
                 .filter(Attendee.event_id == event_id).order_by(Attendee.id).all())
    for a in attendees:
        surplus = len(a.tickets) - a.ticket_count
        if surplus > 0:
            for t in sorted(a.tickets, key=lambda t: t.page_number, reverse=True)[:surplus]:
                db.add(SpareTicket(event_id=event_id, page_number=t.page_number, file_path=t.file_path))
                db.delete(t)
    db.flush()
    db.expire_all()

    spares = (db.query(SpareTicket).filter(SpareTicket.event_id == event_id)
              .order_by(SpareTicket.page_number).all())
    queue = iter(spares)
    assigned = missing = 0
    for a in attendees:
        need = a.ticket_count - min(len(a.tickets), a.ticket_count)
        for _ in range(need):
            spare = next(queue, None)
            if spare is None:
                missing += 1
                continue
            db.add(Ticket(event_id=event_id, attendee_id=a.id, page_number=spare.page_number,
                          file_path=spare.file_path))
            db.delete(spare)
            assigned += 1
    db.commit()
    db.expire_all()
    left = db.query(SpareTicket).filter(SpareTicket.event_id == event_id).count()
    return {"assigned": assigned, "missing": missing, "spare": left}


def release_tickets(db: Session, attendee: Attendee) -> None:
    """Return an attendee's tickets to the spare pool (call before deleting them)."""
    for t in list(attendee.tickets):
        db.add(SpareTicket(event_id=attendee.event_id, page_number=t.page_number, file_path=t.file_path))
        db.delete(t)
    db.flush()
    db.expire(attendee, ["tickets"])  # already deleted; don't let a later cascade delete them twice


def add_ticket_pdf(db: Session, event_id: int, content: bytes, replace: bool = False) -> dict:
    """Add the pages of a (merged) PDF to the event's ticket pool and hand them out.

    Additive by default: later uploads top up guests who are still missing tickets.
    replace=True discards all existing tickets and spare pages first.
    Returns {'pages': added, 'assigned', 'missing', 'spare'}.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise PdfProcessingError("Encrypted PDFs are not supported")
        if len(reader.pages) == 0:
            raise PdfProcessingError("The PDF has no pages")
    except PdfProcessingError:
        raise
    except Exception as exc:
        raise PdfProcessingError(f"Not a valid PDF: {exc}")

    if replace:
        db.query(Ticket).filter(Ticket.event_id == event_id).delete()
        db.query(SpareTicket).filter(SpareTicket.event_id == event_id).delete()
        db.commit()
        out_dir = event_ticket_dir(event_id)
        if out_dir.exists():
            shutil.rmtree(out_dir)
    last = max(
        db.query(func.coalesce(func.max(Ticket.page_number), 0)).filter(Ticket.event_id == event_id).scalar(),
        db.query(func.coalesce(func.max(SpareTicket.page_number), 0)).filter(
            SpareTicket.event_id == event_id).scalar(),
    )
    paths = split_pdf(content, event_id, start=last + 1)
    for offset, path in enumerate(paths, start=1):
        db.add(SpareTicket(event_id=event_id, page_number=last + offset, file_path=str(path)))
    db.commit()
    return {"pages": len(paths), **rebalance_tickets(db, event_id)}


# --------------------------------------------------------------------------- WhatsApp

class WhatsAppError(RuntimeError):
    pass


def describe_gateway_error(resp: httpx.Response) -> str:
    """Turn an Evolution API error body into a short, human-readable reason."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:160] or "no details"
    inner = body.get("response", {}).get("message", body.get("message", body.get("error", "")))
    items = inner if isinstance(inner, list) else [inner]
    parts = []
    for item in items:
        if isinstance(item, dict) and item.get("exists") is False:
            parts.append(f"{item.get('number', 'this number')} is not registered on WhatsApp "
                         "(check the country code / local prefix)")
        elif isinstance(item, dict):
            parts.append(json.dumps(item)[:120])
        elif item:
            parts.append(str(item))
    return "; ".join(parts)[:220] or "no details"


class _SendThrottle:
    """Serialises sends and enforces a minimum gap since the previous send finished.

    Process-wide, so simultaneous requests from several doormen are queued rather
    than fired together. (The app runs a single worker, so in-process is enough.)
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_done = 0.0

    def lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock

    async def wait_turn(self) -> None:
        gap = config.WHATSAPP_MIN_INTERVAL + random.uniform(0, config.WHATSAPP_JITTER)
        remaining = self._last_done + gap - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def done(self) -> None:
        self._last_done = time.monotonic()


throttle = _SendThrottle()


async def send_whatsapp_pdf(
    phone: str,
    pdf_path: str | Path,
    caption: str,
    file_name: str = "ticket.pdf",
    client: httpx.AsyncClient | None = None,
) -> None:
    """Send a PDF document through Evolution API's /message/sendMedia/{instance}."""
    if not config.EVOLUTION_API_KEY:
        raise WhatsAppError("EVOLUTION_API_KEY is not configured")
    try:
        encoded = base64.b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise WhatsAppError(f"Ticket file unavailable: {exc}")

    url = f"{config.EVOLUTION_API_URL}/message/sendMedia/{config.EVOLUTION_INSTANCE}"
    payload = {
        "number": normalize_phone(phone),
        "mediatype": "document",
        "mimetype": "application/pdf",
        "caption": caption,
        "media": encoded,
        "fileName": file_name,
    }
    headers = {"apikey": config.EVOLUTION_API_KEY, "Content-Type": "application/json"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0))
    try:
        async with throttle.lock():
            await throttle.wait_turn()
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise WhatsAppError(
                    f"Could not reach the WhatsApp gateway: {exc.__class__.__name__}"
                )
            finally:
                throttle.done()
    finally:
        if owns_client:
            await client.aclose()
    if resp.status_code >= 400:
        detail = describe_gateway_error(resp)
        log.warning("Evolution API HTTP %s for %s: %s", resp.status_code, payload["number"], resp.text[:500])
        raise WhatsAppError(f"WhatsApp gateway rejected the message (HTTP {resp.status_code}): {detail}")
