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
from typing import NamedTuple

import httpx
import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from . import config, data
from .pdfs import compress_writer
from .models import Attendee, Event, SpareTicket, Ticket

log = logging.getLogger("gate.whatsapp")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

COLUMN_ALIASES = {
    "name": {"name", "full name", "fullname", "guest", "guest name", "nombre", "שם", "שם מלא", "שם המזמין", "שם הלקוח",
             "שם משתתף", "שם האורח", "מזמין"},
    "first_name": {"first name", "firstname", "given name", "שם פרטי"},
    "last_name": {"last name", "lastname", "surname", "family name", "שם משפחה", "משפחה"},
    "phone": {"phone", "phone number", "mobile", "whatsapp", "cell", "telefono", "teléfono", "טלפון", "טלפון נייד", "נייד",
              "פלאפון", "סלולרי", "מספר טלפון", "מס טלפון", "מספר נייד", "טל"},
    "email": {"email", "e-mail", "email address", "correo", "אימייל", "אי מייל", "מייל", "דואל", "דוא ל", "דואר אלקטרוני",
              "כתובת מייל", "כתובת אימייל"},
    "age": {"age", "edad", "גיל"},
    "category": {"category", "attendee type", "type", "group", "סוג", "קטגוריה", "סוג משתתף", "סוג הזמנה", "סוג מנוי",
                 "סוג כרטיס", "סטטוס חבר"},
    "adult_count": {"adult_count", "adult", "adults", "adult tickets", "מבוגר", "מבוגרים", "כרטיסי מבוגר", "כמות מבוגרים",
                    "מס מבוגרים", "מספר מבוגרים"},
    "youth_count": {"youth_count", "youth", "youths", "youth tickets", "נוער", "כרטיסי נוער", "כמות נוער", "בני נוער",
                    "מס נוער", "מספר נוער"},
    # legacy total: treated as adult tickets when no adult/youth columns are given
    "ticket_count": {"ticket_count", "ticket count", "tickets", "qty", "quantity", "count", "כרטיסים", "כמות", "כמות כרטיסים",
                     "מס כרטיסים", "מספר כרטיסים"},
    "notes": {"notes", "note", "comments", "notas", "הערות", "הערה"},
    # the reservation's id in the registration system: lets a re-import find the same order even if its details changed
    "external_ref": {"external_ref", "מזהה", "מזהה הזמנה", "מספר הזמנה", "order id", "reservation id", "external id", "ref"},
    "transport": {"transport", "shuttle", "הסעה", "הסעות"},   # folded into the notes when > 0
}
# Columns of the registration site that carry nothing we store (not reported as "unrecognised")
IGNORED_COLUMNS = {"id", "#", "פעולות", "actions", "שולם", "paid", "התקבל", "received", "מס", "מספר"}


def _norm_key(key) -> str:
    text = re.sub(r"[\"'״׳.:*()\[\]/]", " ", str(key).lower())      # punctuation, quotes, geresh/gershayim
    return re.sub(r"[\s_\-]+", " ", text).strip()


# "Adult Count", "adult_count", "ADULTS", "מבוגר" ... -> adult_count (works for .xlsx headers and JSON keys)
_ALIAS_LOOKUP = {_norm_key(alias): field for field, aliases in COLUMN_ALIASES.items()
                 for alias in aliases | {field}}


def canonical_field(key) -> str | None:
    return _ALIAS_LOOKUP.get(_norm_key(key))


def canonicalize_row(row: dict) -> tuple[dict, list[str]]:
    """Map a row's keys to model fields. Returns (row, names of columns/keys that were not recognised)."""
    out: dict = {}
    unknown: list[str] = []
    for key, value in row.items():
        field = canonical_field(key)
        if field is None:
            if _norm_key(key) not in IGNORED_COLUMNS and str(key).strip():
                unknown.append(str(key))
        else:
            out[field] = value
    transport = str(out.pop("transport", "") or "").strip()
    if transport not in ("", "0"):
        out["notes"] = (str(out.get("notes", "") or "").strip() + f" הסעה: {transport}").strip()
    first, last = str(out.pop("first_name", "") or "").strip(), str(out.pop("last_name", "") or "").strip()
    if not str(out.get("name", "") or "").strip() and (first or last):      # "שם פרטי" + "שם משפחה" -> name
        out["name"] = f"{first} {last}".strip()
    return out, unknown


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
    age: float | None = None
    category: str = data.DEFAULT_CATEGORY
    adult_count: int = 0
    youth_count: int = 0
    ticket_count: int = 0  # derived; as input it is a legacy "total" meaning adult tickets
    notes: str | None = None
    external_ref: str | None = None
    # which keys the source row actually contained (so a re-import only updates those); not stored
    provided: frozenset[str] = Field(default=frozenset(), exclude=True)

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

    @field_validator("external_ref", mode="before")
    @classmethod
    def _ref(cls, v):
        v = re.sub(r"\s+", "", str(v)) if v is not None else ""
        if len(v) > 32:
            raise ValueError("reference is too long")
        return v or None

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
    def _age_num(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            f = float(str(v).strip().replace(",", "."))   # "20,5" and "20.5" both fine
        except ValueError:
            raise ValueError("age must be a number (e.g. 20 or 20.5)")
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("age must be a number (e.g. 20 or 20.5)")
        return f

    @field_validator("age")
    @classmethod
    def _age(cls, v: float | None) -> float | None:
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
    fields = {canonical_field(c) for c in df.columns}
    missing = [f for f in ("name", "phone") if f not in fields]
    if missing:
        raise ImportValidationError([f"Missing required column(s): {', '.join(missing)}"])
    return df.to_dict(orient="records")


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


def name_keys(name: str) -> tuple[str, str]:
    """(ordered key, order-insensitive key) of a person's name, ignoring case, quotes and spacing."""
    tokens = [t for t in _norm_key(re.sub(r"[,]", " ", name)).split() if t]
    return " ".join(tokens), " ".join(sorted(tokens))


class MemberBook:
    """Lookup of organisation members' phone numbers by name. Ambiguous names (two members, same name) match nobody."""

    def __init__(self, entries: list[tuple[str, str]] = (), override: bool = False):
        self.override = override
        self._by_name: dict[str, str | None] = {}
        self._by_tokens: dict[str, str | None] = {}
        for name, phone in entries:
            ordered, tokens = name_keys(name)
            for table, key in ((self._by_name, ordered), (self._by_tokens, tokens)):
                table[key] = phone if key not in table or table[key] == phone else None

    def __bool__(self) -> bool:
        return bool(self._by_name)

    def lookup(self, name: str) -> str | None:
        ordered, tokens = name_keys(name or "")
        return self._by_name.get(ordered) or self._by_tokens.get(tokens)


def _phone_ok(value) -> bool:
    return 7 <= len(re.sub(r"\D", "", str(value or ""))) <= 15


class RowIssue(NamedTuple):
    label: str            # "Row 7"
    row: dict             # the row's values as strings, ready to show in a form
    errors: list[str]


class ParseResult(NamedTuple):
    valid: list[AttendeeIn]
    issues: list[RowIssue]
    ignored: list[str] = []   # column / key names that were not recognised (and therefore not imported)
    filled: int = 0           # phone numbers taken from the members list
    bad_ages: int = 0         # rows whose age was not a number and was ignored (lenient imports only)


def _row_as_form(row: dict) -> dict:
    """Row values as strings; a legacy ticket_count becomes adult_count so the fix form can show it."""
    out = {k: ("" if v is None else str(v).strip()) for k, v in row.items()}
    if not (out.get("adult_count") or out.get("youth_count")) and out.get("ticket_count"):
        out["adult_count"] = out["ticket_count"]
    return out


def describe_validation_error(exc: ValidationError) -> list[str]:
    return [(f"{'.'.join(str(p) for p in e['loc'])}: " if e["loc"] else "")
            + e["msg"].removeprefix("Value error, ") for e in exc.errors()]


def parse_guest_list(filename: str, content: bytes, members: "MemberBook | None" = None) -> ParseResult:
    """Parse an .xlsx/.json guest list. A file that cannot be read at all raises ImportValidationError;
    otherwise valid rows are returned and each bad row comes back as a RowIssue to fix or skip."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".xlsx":
        rows, offset = _rows_from_xlsx(content), 2  # header occupies spreadsheet row 1
    elif ext == ".json":
        rows, offset = _rows_from_json(content), 1
    else:
        raise ImportValidationError(["Unsupported file type: upload a .xlsx or .json file"])
    if not rows:
        raise ImportValidationError(["The file contains no guests"])
    return parse_rows(rows, offset, members)


def parse_rows(rows: list[dict], offset: int = 1, members: "MemberBook | None" = None,
               lenient_age: bool = False) -> ParseResult:
    """Validate rows (dicts keyed by column name/alias). Bad rows come back as RowIssue to fix or skip."""
    valid: list[AttendeeIn] = []
    issues: list[RowIssue] = []
    filled = bad_ages = 0
    ignored: dict[str, None] = {}
    seen: set[tuple[str, str]] = set()
    for i, raw_row in enumerate(rows):
        label = f"Row {i + offset}"
        row, unknown = canonicalize_row(raw_row)
        ignored.update(dict.fromkeys(unknown))
        if members:   # organisation members: take the phone number from the members list when missing
            cat = data.CATEGORY_ALIASES.get(_norm_key(row.get("category", "")), str(row.get("category", "") or "").strip())
            found = members.lookup(str(row.get("name", "")))
            # a פלוס is somebody's guest: never give them a member's number
            if found and cat != "פלוס" and (not _phone_ok(row.get("phone")) or (members.override and cat == data.CATEGORIES[0])):
                row["phone"], filled = found, filled + 1
        if lenient_age and str(row.get("age", "") or "").strip():
            try:
                float(str(row["age"]).strip().replace(",", "."))
            except ValueError:
                row.pop("age"), None
                bad_ages += 1
        shown = _row_as_form(row)
        cleaned = {k: v for k, v in row.items() if v is not None and str(v).strip() != ""}
        try:
            item = AttendeeIn.model_validate(cleaned)
        except ValidationError as exc:
            issues.append(RowIssue(label, shown, describe_validation_error(exc)))
            continue
        item.provided = frozenset(cleaned)
        key = ("ref", item.external_ref) if item.external_ref else (item.name.lower(), item.phone)
        if key in seen:
            what = f"reservation {item.external_ref}" if item.external_ref else f"{item.name}, {item.phone}"
            issues.append(RowIssue(label, shown, [f"duplicate of an earlier row ({what})"]))
            continue
        seen.add(key)
        valid.append(item)
    return ParseResult(valid, issues, list(ignored), filled, bad_ages)


class ImportOutcome(NamedTuple):
    added: int
    updated: int
    unchanged: int
    changes: list[str]      # human-readable list of what changed on existing guests
    warnings: list[str]


def _fmt(value) -> str:
    if value is None or value == "":
        return "-"
    return f"{value:g}" if isinstance(value, float) else str(value)


def import_attendees(db: Session, event_id: int, items: list[AttendeeIn]) -> ImportOutcome:
    """Add new guests and update existing ones with whatever the source provides.

    A guest is recognised by its reservation id (`external_ref`, from the registration system) if the row has one -
    so two orders with the same name and phone stay two guests - otherwise by name + phone. Only fields present in
    the source row are changed, so a row with just name + phone never resets anything. Ticket counts follow
    adult/youth if given (a legacy 'tickets' total means adults).
    """
    existing = list(db.query(Attendee).filter(Attendee.event_id == event_id))
    by_ref = {g.external_ref: g for g in existing if g.external_ref}
    by_name: dict[tuple[str, str], Attendee] = {}
    for g in existing:
        by_name.setdefault((g.name.lower(), g.phone), g)
    added = updated = unchanged = 0
    changes: list[str] = []
    warnings: list[str] = []
    for item in items:
        key = (item.name.lower(), item.phone)
        guest = by_ref.get(item.external_ref) if item.external_ref else by_name.get(key)
        diffs: list[str] = []
        if guest is None and item.external_ref:
            guest = by_name.get(key)                 # same person imported earlier without a reservation id: adopt it
            if guest is not None and guest.external_ref:
                guest = None                          # that one belongs to another reservation: this is a separate order
            elif guest is not None:
                guest.external_ref = item.external_ref
                by_ref[item.external_ref] = guest
        if guest is None:
            guest = Attendee(event_id=event_id, **item.model_dump())
            db.add(guest)
            by_name.setdefault(key, guest)
            if item.external_ref:
                by_ref[item.external_ref] = guest
            added += 1
            continue
        if item.external_ref and guest.external_ref == item.external_ref:     # identified by id: name/phone may have changed
            for field in ("name", "phone"):
                if getattr(item, field) != getattr(guest, field):
                    diffs.append(f"{field} {_fmt(getattr(guest, field))}→{_fmt(getattr(item, field))}")
                    setattr(guest, field, getattr(item, field))
        for field in ("email", "age", "category", "notes"):
            if field in item.provided and getattr(item, field) != getattr(guest, field):
                diffs.append(f"{field} {_fmt(getattr(guest, field))}→{_fmt(getattr(item, field))}")
                setattr(guest, field, getattr(item, field))
        if item.provided & {"adult_count", "youth_count", "ticket_count"}:
            if item.provided & {"adult_count", "youth_count"}:
                adult = item.adult_count if "adult_count" in item.provided else guest.adult_count
                youth = item.youth_count if "youth_count" in item.provided else guest.youth_count
            else:                                   # legacy total
                adult, youth = item.ticket_count, 0
            if adult + youth < 1:
                warnings.append(f"{guest.name}: the file would leave 0 tickets - counts left unchanged.")
            elif (adult, youth) != (guest.adult_count, guest.youth_count):
                diffs.append(f"tickets adult {guest.adult_count}→{adult}, youth {guest.youth_count}→{youth}")
                guest.adult_count, guest.youth_count, guest.ticket_count = adult, youth, adult + youth
        if diffs:
            updated += 1
            changes.append(f"{guest.name}: " + ", ".join(diffs))
        else:
            unchanged += 1
    db.commit()
    return ImportOutcome(added, updated, unchanged, changes, warnings)


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
        if config.PDF_COMPRESS:
            try:
                compress_writer(writer)
            except Exception:           # never lose a ticket over an optimisation
                log.exception("Could not compress ticket page %s", index)
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
    db.query(Event).filter(Event.id == event_id).update({Event.tickets_purged_at: None})
    for offset, path in enumerate(paths, start=1):
        db.add(SpareTicket(event_id=event_id, page_number=last + offset, file_path=str(path)))
    db.commit()
    return {"pages": len(paths), **rebalance_tickets(db, event_id)}


# --------------------------------------------------------------------------- WhatsApp

class WhatsAppError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, raw: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.raw = raw            # the gateway's raw answer (for diagnostics)

    @property
    def line_problem(self) -> bool:
        """Looks like trouble with the *sending number* (HTTP 5xx, dropped session) rather than with
        the message or the recipient (4xx)."""
        text = str(self).lower()
        return bool((self.status_code or 0) >= 500
                    or re.search(r"connection closed|not connected|disconnected|logged out|timed out|timeout", text))


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
        if random.random() < config.WHATSAPP_LONG_PAUSE_CHANCE:      # an occasional longer, human-looking pause
            gap += random.uniform(*config.WHATSAPP_LONG_PAUSE)
        remaining = self._last_done + gap - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def done(self) -> None:
        self._last_done = time.monotonic()


throttle = _SendThrottle()


class SentRef(NamedTuple):
    remote_jid: str
    message_id: str


async def _gateway_call(method: str, path: str, payload: dict | None, client: httpx.AsyncClient | None,
                        instance: str | None = None, throttled: bool = True) -> httpx.Response:
    """One call to Evolution API. Calls that talk to WhatsApp (send/delete) are throttled and share the
    same randomised gap; pure management calls (instance state, create ...) are not."""
    if not config.EVOLUTION_API_KEY:
        raise WhatsAppError("EVOLUTION_API_KEY is not configured")
    url = f"{config.EVOLUTION_API_URL}{path}"
    if instance != "":
        url += f"/{instance or config.EVOLUTION_INSTANCE}"
    headers = {"apikey": config.EVOLUTION_API_KEY, "Content-Type": "application/json"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0))
    try:
        if throttled:
            async with throttle.lock():
                await throttle.wait_turn()
                try:
                    resp = await client.request(method, url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise WhatsAppError(f"Could not reach the WhatsApp gateway: {exc.__class__.__name__}")
                finally:
                    throttle.done()
        else:
            try:
                resp = await client.request(method, url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise WhatsAppError(f"Could not reach the WhatsApp gateway: {exc.__class__.__name__}")
    finally:
        if owns_client:
            await client.aclose()
    if resp.status_code >= 400:
        detail = describe_gateway_error(resp)
        log.warning("Evolution API HTTP %s on %s: %s", resp.status_code, path, resp.text[:500])
        raise WhatsAppError(f"WhatsApp gateway rejected the request (HTTP {resp.status_code}): {detail}",
                            status_code=resp.status_code, raw=resp.text[:600])
    return resp


async def send_whatsapp_pdf(
    phone: str,
    pdf_path: str | Path,
    caption: str,
    file_name: str = "ticket.pdf",
    client: httpx.AsyncClient | None = None,
    instance: str | None = None,
) -> SentRef | None:
    """Send a PDF document through Evolution API's /message/sendMedia/{instance}.

    Returns the sent message's (jid, id) so it can be deleted later, or None if the gateway
    did not report one.
    """
    try:
        encoded = base64.b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise WhatsAppError(f"Ticket file unavailable: {exc}")
    payload = {
        "number": normalize_phone(phone),
        "mediatype": "document",
        "mimetype": "application/pdf",
        "caption": caption,
        "media": encoded,
        "fileName": file_name,
        "delay": random.randint(*config.WHATSAPP_TYPING_MS),   # Evolution shows "typing..." this long first
    }
    resp = await _gateway_call("POST", "/message/sendMedia", payload, client, instance)
    try:
        key = resp.json().get("key") or {}
        if key.get("id") and key.get("remoteJid"):
            return SentRef(key["remoteJid"], key["id"])
    except (ValueError, AttributeError):
        pass
    log.warning("sendMedia response had no message key; this message cannot be auto-deleted")
    return None


async def delete_whatsapp_message(remote_jid: str, message_id: str,
                                  client: httpx.AsyncClient | None = None, instance: str | None = None) -> None:
    """Delete a message we sent "for everyone" (Evolution: /chat/deleteMessageForEveryone)."""
    await _gateway_call("DELETE", "/chat/deleteMessageForEveryone",
                        {"id": message_id, "remoteJid": remote_jid, "fromMe": True}, client, instance)


async def send_whatsapp_text(phone: str, text: str, instance: str | None = None,
                             client: httpx.AsyncClient | None = None, typing: bool = True, _retry: bool = True) -> None:
    """Plain text message (used by the line health test). If the gateway answers 5xx while simulating "typing...",
    the same text is retried once without it - the typing/presence step is the most fragile part of Evolution."""
    payload = {"number": normalize_phone(phone), "text": text}
    if typing:
        payload["delay"] = random.randint(*config.WHATSAPP_TYPING_MS)
    try:
        await _gateway_call("POST", "/message/sendText", payload, client, instance)
    except WhatsAppError as exc:
        if _retry and (exc.status_code or 0) >= 500 and (typing or "connection closed" in str(exc).lower() + exc.raw.lower()):
            if "connection closed" in str(exc).lower() + exc.raw.lower():   # the socket was just re-connecting: give it a moment
                await asyncio.sleep(random.uniform(*config.WHATSAPP_RECONNECT_WAIT))
            log.warning("sendText failed with HTTP %s; retrying once without the typing delay", exc.status_code)
            await send_whatsapp_text(phone, text, instance, client, typing=False, _retry=False)
        else:
            raise


async def evolution_numbers_exist(instance: str, numbers: list[str], client: httpx.AsyncClient | None = None) -> list[dict]:
    """Ask Evolution whether these phone numbers are registered on WhatsApp (POST /chat/whatsappNumbers)."""
    resp = await _gateway_call("POST", "/chat/whatsappNumbers", {"numbers": [normalize_phone(n) for n in numbers]},
                               client, instance, throttled=False)
    body = resp.json()
    return body if isinstance(body, list) else []


# ---- instance management (talks to Evolution only, never to WhatsApp contacts: not throttled)

async def evolution_create_instance(name: str, client: httpx.AsyncClient | None = None) -> None:
    await _gateway_call("POST", "/instance/create",
                        {"instanceName": name, "integration": "WHATSAPP-BAILEYS", "qrcode": True},
                        client, instance="", throttled=False)


async def evolution_state(name: str, client: httpx.AsyncClient | None = None) -> str:
    """'open' (connected), 'connecting', 'close' ... ; 'missing' if Evolution does not know the instance."""
    try:
        resp = await _gateway_call("GET", "/instance/connectionState", None, client, name, throttled=False)
    except WhatsAppError as exc:
        if exc.status_code == 404:
            return "missing"
        raise
    body = resp.json()
    return str((body.get("instance") or body).get("state", "unknown"))


async def evolution_connect(name: str, number: str | None = None,
                            client: httpx.AsyncClient | None = None) -> dict:
    """QR code (data URI) and/or pairing code for linking the phone."""
    url_instance = f"{name}?number={normalize_phone(number)}" if number else name
    resp = await _gateway_call("GET", "/instance/connect", None, client, url_instance, throttled=False)
    body = resp.json()
    return {"qr": body.get("base64") or "", "pairing_code": body.get("pairingCode") or "", "raw_count": body.get("count")}


async def evolution_delete_instance(name: str, client: httpx.AsyncClient | None = None) -> None:
    try:
        await _gateway_call("DELETE", "/instance/delete", None, client, name, throttled=False)
    except WhatsAppError as exc:
        if exc.status_code != 404:
            raise


# --------------------------------------------------------------------------- registration system (HTML table)

class RegistrationError(RuntimeError):
    pass


class GridRow(NamedTuple):
    cls: str            # the <tr> class attribute
    cells: list[str]    # visible text of each cell


_SECTION_CLASS = {"members": "חבר ארגון", "newcomers": "מצטרף", "plus": "פלוס", "public": "פתוח"}
_SECTION_TEXT = {_norm_key(k): v for k, v in {**data.CATEGORY_ALIASES, **{c: c for c in data.CATEGORIES}}.items()}


def _cell_text(el) -> str:
    for hidden in el.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' hide ')]|.//script|.//style"):
        hidden.drop_tree()          # e.g. the hidden "saved" confirmations next to checkboxes
    return " ".join(el.text_content().split())


def grid_from_html(html: bytes | str) -> list[GridRow]:
    """The reservations table of an HTML page (the table with the most recognisable column names, then most rows)."""
    import lxml.etree
    import lxml.html

    if not html or not html.strip():
        raise RegistrationError("The page is empty")
    if isinstance(html, bytes):
        try:                                   # UTF-8 unless the bytes say otherwise (a charset <meta> still works for the rest)
            html = html.decode("utf-8")
        except UnicodeDecodeError:
            pass
    try:
        doc = lxml.html.fromstring(html)
    except ValueError:                         # str with an encoding declaration
        doc = lxml.html.fromstring(html.encode("utf-8") if isinstance(html, str) else html)
    except lxml.etree.ParserError as exc:
        raise RegistrationError(f"Could not read the page: {exc}")
    best, best_score = None, (-1, -1)
    for table in doc.xpath("//table"):
        grid = [GridRow(tr.get("class") or "", [_cell_text(c) for c in tr.xpath("./th|./td")]) for tr in table.xpath(".//tr")]
        grid = [g for g in grid if any(g.cells)]
        known = max((sum(1 for c in g.cells if canonical_field(c)) for g in grid[:8]), default=0)
        score = (known, len(grid))
        if known >= 2 and score > best_score:
            best, best_score = grid, score
    if best is None:
        raise RegistrationError("No table with recognisable columns (name, phone, tickets ...) was found on that page.")
    return best


def rows_from_grid(grid: list[GridRow]) -> list[dict]:
    """Turn the table into rows keyed by header text.

    The registration site groups reservations under section rows (חברי ארגון / מצטרפים / פלוסים / פתוח), repeats the
    header row in every section and ends each with a totals row: sections become each guest's category, the
    repeated headers, totals and spacers are dropped.
    """
    hi = max(range(min(8, len(grid))), key=lambda i: (sum(1 for c in grid[i].cells if canonical_field(c)), -i), default=0)
    headers = grid[hi].cells
    if sum(1 for c in headers if canonical_field(c)) < 2:
        raise RegistrationError("No table with recognisable columns (name, phone, tickets ...) was found on that page.")
    category: str | None = None
    rows: list[dict] = []
    for g in grid[hi + 1:]:
        tokens = g.cls.split()
        nonempty = [c for c in g.cells if c]
        if not nonempty or any(t.startswith(("section-footer", "section-separator")) for t in tokens):
            continue
        if g.cells == headers or sum(1 for c in g.cells if canonical_field(c)) >= max(3, len(nonempty) - 1):
            continue                                                   # repeated header row
        is_title = any(t.startswith("section-title") for t in tokens)
        if is_title or (len(nonempty) == 1 and _norm_key(nonempty[0]) in _SECTION_TEXT):
            suffix = next((t.rsplit("-", 1)[1] for t in tokens if t.startswith("section-title-")), "")
            category = _SECTION_CLASS.get(suffix) or _SECTION_TEXT.get(_norm_key(nonempty[0])) or nonempty[0]
            continue
        if re.match(r"^(סה[\"״']?כ|total)\b", nonempty[0].strip().lower()) or (len(nonempty) >= 2 and re.match(r"^(סה[\"״']?כ|total)\b", nonempty[1].strip().lower())):
            continue                                                   # totals row
        if len(g.cells) < max(2, len(headers) // 2):
            continue
        row = {h: (g.cells[i] if i < len(g.cells) else "") for i, h in enumerate(headers) if h}
        has_category = any(canonical_field(k) == "category" and v.strip() for k, v in row.items())
        if category and not has_category:
            row["category"] = category
        rows.append(row)
    if not rows:
        raise RegistrationError("The table has no reservations")
    return rows


REGISTRATION_BOILERPLATE_NOTES = {"אתר ארגון"}      # the registration site's default note ("organisation website")


def _tidy_registration_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        for key, value in list(row.items()):
            if canonical_field(key) == "notes" and str(value).strip() in REGISTRATION_BOILERPLATE_NOTES:
                row[key] = ""
    return rows


def extract_table(html: bytes | str) -> list[dict]:
    return rows_from_grid(grid_from_html(html))


def parse_registration_html(html: bytes | str, members: "MemberBook | None" = None) -> ParseResult:
    return parse_rows(_tidy_registration_rows(extract_table(html)), offset=1, members=members, lenient_age=True)


def parse_registration_grid(grid: list[GridRow], members: "MemberBook | None" = None) -> ParseResult:
    return parse_rows(_tidy_registration_rows(rows_from_grid(grid)), offset=1, members=members, lenient_age=True)


async def fetch_registration_page(game_id: str, cookie: str, client: httpx.AsyncClient | None = None) -> bytes:
    """Download the reservations page of one game, signed in with the browser session `cookie`.

    Only the configured registration host is contacted, game_id must be digits, and redirects are never followed
    (a redirect means the session is no longer valid).
    """
    game_id = (game_id or "").strip()
    if not re.fullmatch(r"\d{1,9}", game_id):
        raise RegistrationError("The registration event ID must be a number")
    if not cookie.strip():
        raise RegistrationError("No session cookie saved - paste it first (or upload the saved page instead)")
    url = config.REGISTRATION_BASE_URL + config.REGISTRATION_PATH.format(game_id=game_id)
    headers = {"Cookie": cookie.strip(), "Accept": "text/html", "User-Agent": "Mozilla/5.0 (compatible; GateImport/1.0)"}
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0), follow_redirects=False)
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise RegistrationError(f"Could not reach the registration site: {exc.__class__.__name__}")
    finally:
        if owns:
            await client.aclose()
    if 300 <= resp.status_code < 400 or resp.status_code in (401, 403):
        raise RegistrationError("The registration site asked to sign in again - the saved session cookie has expired. "
                                "Sign in with Google in your browser and paste a fresh cookie.")
    if resp.status_code >= 400:
        raise RegistrationError(f"The registration site answered HTTP {resp.status_code}")
    body = resp.content[: config.MAX_REGISTRATION_BYTES + 1]
    if len(body) > config.MAX_REGISTRATION_BYTES:
        raise RegistrationError("The page is too large")
    lowered = body[:200000].lower()
    if b"<table" not in lowered and (b"accounts.google.com" in lowered or b'type="password"' in lowered):
        raise RegistrationError("The registration site showed a sign-in page - the session cookie has expired.")
    return body


# --------------------------------------------------------------------------- members phone book

def parse_member_text(text: str) -> list[tuple[str, str]]:
    """Lines like 'name, phone' / 'phone<TAB>name' (any order, comma/tab/semicolon separated) -> (name, raw phone)."""
    pairs = []
    for line in text.splitlines():
        parts = [p.strip() for p in re.split(r"[\t,;|]+", line) if p.strip()]
        if not parts:
            continue
        phone = next((p for p in parts if len(re.sub(r"\D", "", p)) >= 7), "")
        name = " ".join(p for p in parts if p != phone)
        if not phone and all(canonical_field(p) for p in parts):      # a header line such as "שם, טלפון"
            continue
        pairs.append((name, phone))
    return pairs


def parse_member_file(filename: str, content: bytes) -> list[tuple[str, str]]:
    """.xlsx / .csv / .json with name + phone columns (aliases and Hebrew headers accepted) -> (name, raw phone)."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".json":
        rows = _rows_from_json(content)
    elif ext in (".xlsx", ".csv"):
        try:
            df = (pd.read_excel(io.BytesIO(content), engine="openpyxl", dtype=str) if ext == ".xlsx"
                  else pd.read_csv(io.BytesIO(content), dtype=str, encoding="utf-8-sig"))
        except Exception as exc:
            raise ImportValidationError([f"Could not read the file: {exc}"])
        rows = df.fillna("").to_dict(orient="records")
    elif ext in (".txt", ".tsv", ""):
        return parse_member_text(content.decode("utf-8-sig", errors="replace"))
    else:
        raise ImportValidationError(["Unsupported file type: upload .xlsx, .csv, .json or a text file"])
    pairs = []
    for row in rows:
        canon, _ = canonicalize_row(row)
        pairs.append((str(canon.get("name", "")).strip(), str(canon.get("phone", "")).strip()))
    if not pairs:
        raise ImportValidationError(["The file contains no rows"])
    return pairs


# --------------------------------------------------------------------------- WhatsApp account of a line

async def evolution_list_instances(client: httpx.AsyncClient | None = None) -> list[dict]:
    """Every instance Evolution knows: [{'name', 'number', 'state'}]. Empty list if Evolution cannot be asked."""
    try:
        resp = await _gateway_call("GET", "/instance/fetchInstances", None, client, instance="", throttled=False)
        body = resp.json()
    except (WhatsAppError, ValueError):
        return []
    out = []
    for item in body if isinstance(body, list) else []:
        item = item.get("instance", item) if isinstance(item, dict) else {}
        name = item.get("name") or item.get("instanceName")
        if name:
            raw = item.get("ownerJid") or item.get("owner") or item.get("number") or ""
            out.append({"name": str(name), "number": re.sub(r"\D", "", str(raw).split("@")[0].split(":")[0]),
                        "state": str(item.get("connectionStatus") or item.get("status") or "")})
    return out


async def evolution_owner_number(name: str, client: httpx.AsyncClient | None = None) -> str | None:
    """The phone number the instance is linked to (digits), or None if unknown / not linked yet."""
    try:
        resp = await _gateway_call("GET", f"/instance/fetchInstances?instanceName={name}", None, client, instance="", throttled=False)
        body = resp.json()
    except (WhatsAppError, ValueError):
        return None
    item = (body[0] if isinstance(body, list) and body else body) or {}
    item = item.get("instance", item) if isinstance(item, dict) else {}
    raw = item.get("ownerJid") or item.get("owner") or item.get("number") or ""
    digits = re.sub(r"\D", "", str(raw).split("@")[0].split(":")[0])
    return digits if 8 <= len(digits) <= 15 else None


async def evolution_set_profile(name: str, display_name: str, picture_b64: str | None,
                                client: httpx.AsyncClient | None = None) -> list[str]:
    """Set the account's WhatsApp profile name and picture. Returns a list of problems (empty = all applied)."""
    problems: list[str] = []
    try:
        await _gateway_call("POST", "/chat/updateProfileName", {"name": display_name}, client, name, throttled=False)
    except WhatsAppError as exc:
        problems.append(f"name: {exc}")
    if picture_b64:
        await asyncio.sleep(random.uniform(2.0, 6.0))
        try:
            await _gateway_call("POST", "/chat/updateProfilePicture", {"picture": picture_b64}, client, name, throttled=False)
        except WhatsAppError as exc:
            problems.append(f"picture: {exc}")
    return problems
