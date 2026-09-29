"""FastAPI application: routes, templates and exception handlers."""
import json
from urllib.parse import urlsplit
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import segno
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, data
from .auth import (
    authenticate, clear_auth_cookie, create_access_token, get_current_user, hash_password,
    require_admin, require_staff, set_auth_cookie, verify_password,
)
from .database import SessionLocal, get_db, init_db
from .models import Attendee, Event, Role, Ticket, User, utcnow
from .services import (
    AttendeeIn, ImportValidationError, PdfProcessingError, WhatsAppError, import_attendees,
    map_pdf_to_attendees, normalize_phone, parse_guest_list, send_whatsapp_pdf,
)

log = logging.getLogger("gate")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def seed_admin() -> None:
    with SessionLocal() as db:
        for a in db.query(Attendee).all():  # bring older records to international format
            fixed = normalize_phone(a.phone)
            if fixed != a.phone:
                a.phone = fixed
        db.commit()
        if db.query(User).count() == 0:
            db.add(
                User(
                    email=config.DEFAULT_ADMIN_EMAIL,
                    hashed_password=hash_password(config.DEFAULT_ADMIN_PASSWORD),
                    full_name="Administrator",
                    role=Role.admin,
                )
            )
            db.commit()
            log.warning(
                "Seeded default admin %s / %s - CHANGE THIS PASSWORD",
                config.DEFAULT_ADMIN_EMAIL, config.DEFAULT_ADMIN_PASSWORD,
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.STORAGE_DIR.joinpath("tickets").mkdir(parents=True, exist_ok=True)
    init_db()
    seed_admin()
    yield


app = FastAPI(title="Gate - Event Ticketing & Door Vetting", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers

def render(request: Request, name: str, ctx: dict | None = None, status: int = 200,
           headers: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx or {}, status_code=status, headers=headers)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def get_event_or_404(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


def get_attendee_or_404(db: Session, attendee_id: int) -> Attendee:
    attendee = (
        db.query(Attendee).options(selectinload(Attendee.tickets)).filter_by(id=attendee_id).first()
    )
    if attendee is None:
        raise HTTPException(404, "Attendee not found")
    return attendee


def compute_stats(db: Session, event_id: int) -> dict:
    total, admitted, guests, guests_in = db.query(
        func.coalesce(func.sum(Attendee.ticket_count), 0),
        func.coalesce(func.sum(Attendee.ticket_count).filter(Attendee.checked_in.is_(True)), 0),
        func.count(Attendee.id),
        func.count(Attendee.id).filter(Attendee.checked_in.is_(True)),
    ).filter(Attendee.event_id == event_id).one()
    return {
        "total": total, "admitted": admitted, "remaining": total - admitted,
        "guests": guests, "guests_in": guests_in,
        "percent": round(admitted * 100 / total) if total else 0,
    }


def toast(message: str, kind: str = "success") -> dict:
    return {"toast": {"message": message, "kind": kind}}


def mark_admitted(db: Session, attendee: Attendee) -> None:
    """Idempotent: sending a ticket or showing its QR admits the guest (never un-admits)."""
    if not attendee.checked_in:
        attendee.checked_in = True
        attendee.checked_in_at = utcnow()
        db.commit()


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- errors

@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401:
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        if "text/html" in request.headers.get("accept", "") or request.method == "GET":
            resp = RedirectResponse("/login", status_code=303)
            clear_auth_cookie(resp)
            return resp
    titles = {401: "Sign in required", 403: "Not allowed", 404: "Not found"}
    ctx = {
        "code": exc.status_code,
        "title": titles.get(exc.status_code, "Something went wrong"),
        "detail": exc.detail,
        "user": None,
    }
    if is_htmx(request):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    return render(request, "error.html", ctx, status=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    ctx = {"code": 500, "title": "Something went wrong",
           "detail": "An unexpected error occurred.", "user": None}
    if is_htmx(request):
        return PlainTextResponse("An unexpected error occurred.", status_code=500)
    return render(request, "error.html", ctx, status=500)


# --------------------------------------------------------------------------- auth

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/events", status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    try:
        get_current_user(request, db)
        return RedirectResponse("/events", status_code=303)
    except HTTPException:
        return render(request, "login.html", {"error": None, "email": ""})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = authenticate(db, email, password)
    if user is None:
        return render(request, "login.html",
                      {"error": "Invalid email or password.", "email": email}, status=401)
    resp = RedirectResponse("/events", status_code=303)
    set_auth_cookie(resp, create_access_token(user))
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    clear_auth_cookie(resp)
    return resp


# --------------------------------------------------------------------------- events

def events_ctx(db: Session, user: User, **extra) -> dict:
    events = db.query(Event).order_by(Event.event_date.desc()).all()
    return {"user": user, "events": events, "stats": {e.id: compute_stats(db, e.id) for e in events},
            "error": None, "opponents": data.OPPONENTS, "arenas": data.ARENAS,
            "default_location": data.DEFAULT_LOCATION, "OTHER": data.OTHER,
            "home_team": data.HOME_TEAM, "versus": data.VERSUS,
            "other_opponent": data.OTHER_LABEL_OPPONENT, "other_location": data.OTHER_LABEL_LOCATION, **extra}


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    return render(request, "events.html", events_ctx(db, user))


def pick(choice: str, other_text: str, label: str) -> str:
    """Resolve a select value that may be the free-text "Other" option."""
    if choice == data.OTHER:
        value = " ".join(other_text.split())
        if not value:
            raise ValueError(f"Type the {label} name.")
        return value[:120]
    return choice.strip()


@app.post("/events")
def create_event(
    request: Request,
    opponent: str = Form(...),
    opponent_other: str = Form(""),
    location: str = Form(data.DEFAULT_LOCATION),
    location_other: str = Form(""),
    event_date: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        rival = pick(opponent, opponent_other, "opponent")
        place = pick(location, location_other, "location")
        if not rival or not place:
            raise ValueError("Choose an opponent and a location.")
        when = datetime.fromisoformat(event_date)
    except ValueError as exc:
        msg = str(exc) if "Type the" in str(exc) or "Choose" in str(exc) else "Pick a valid date and time."
        return render(request, "events.html",
                      events_ctx(db, user, error=msg, open_modal=True), status=422)
    db.add(Event(name=f"{data.HOME_TEAM} {data.VERSUS} {rival}", location=place, event_date=when))
    db.commit()
    return RedirectResponse("/events", status_code=303)


def manage_ctx(db: Session, user: User, event: Event, **extra) -> dict:
    tickets = db.query(func.count(Ticket.id)).filter(Ticket.event_id == event.id).scalar()
    attendees = (db.query(Attendee).options(selectinload(Attendee.tickets))
                 .filter(Attendee.event_id == event.id).order_by(func.lower(Attendee.name), Attendee.id).all())
    return {"user": user, "event": event, "stats": compute_stats(db, event.id),
            "ticket_files": tickets, "attendees": attendees, "messages": [], "errors": [], **extra}


@app.get("/events/{event_id}/manage", response_class=HTMLResponse)
def manage_page(event_id: int, request: Request, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    return render(request, "manage.html", manage_ctx(db, user, event))


@app.get("/events/{event_id}/attendees/import", include_in_schema=False)
@app.get("/events/{event_id}/tickets/upload", include_in_schema=False)
def upload_url_visited_directly(event_id: int, user: User = Depends(require_admin)):
    """These URLs only accept form POSTs; a reload or bookmark lands on the manage page."""
    return RedirectResponse(f"/events/{event_id}/manage", status_code=303)


@app.post("/events/{event_id}/attendees/import", response_class=HTMLResponse)
async def import_guest_list(
    event_id: int, request: Request, file: UploadFile = File(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    content = await file.read(config.MAX_LIST_BYTES + 1)
    try:
        if len(content) > config.MAX_LIST_BYTES:
            raise ImportValidationError(["File is too large (5 MB max)"])
        items = parse_guest_list(file.filename or "", content)
    except ImportValidationError as exc:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=exc.errors[:50]), status=422)
    count = import_attendees(db, event.id, items)
    return render(request, "manage.html",
                  manage_ctx(db, user, event, messages=[f"Imported {count} guest(s)."]))


@app.post("/events/{event_id}/tickets/upload", response_class=HTMLResponse)
async def upload_ticket_pdf(
    event_id: int, request: Request, file: UploadFile = File(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    content = await file.read(config.MAX_PDF_BYTES + 1)
    try:
        if len(content) > config.MAX_PDF_BYTES:
            raise PdfProcessingError("File is too large (50 MB max)")
        count, spare = map_pdf_to_attendees(db, event.id, content)
    except PdfProcessingError as exc:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=[str(exc)]), status=422)
    return render(request, "manage.html",
                  manage_ctx(db, user, event, messages=[
                      f"Split and attached {count} ticket(s)."
                      + (f" {spare} extra page(s) were stored but not assigned." if spare else "")]))


# --------------------------------------------------------------------------- manual attendees

def validate_attendee_form(**fields) -> tuple[AttendeeIn | None, list[str]]:
    try:
        return AttendeeIn.model_validate(fields), []
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg'].removeprefix('Value error, ')}"
                      for e in exc.errors()]


def is_duplicate(db: Session, event_id: int, item: AttendeeIn, exclude_id: int | None = None) -> bool:
    q = db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.phone == item.phone,
                                  func.lower(Attendee.name) == item.name.lower())
    if exclude_id:
        q = q.filter(Attendee.id != exclude_id)
    return q.first() is not None


@app.post("/events/{event_id}/attendees", response_class=HTMLResponse)
def add_attendee(
    event_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    ticket_type: str = Form("Standard"), ticket_count: str = Form("1"), notes: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    item, errors = validate_attendee_form(name=name, phone=phone, email=email, age=age,
                                          ticket_type=ticket_type, ticket_count=ticket_count, notes=notes)
    if item and is_duplicate(db, event.id, item):
        errors = [f"{item.name} ({item.phone}) is already on the list."]
    if errors:
        return render(request, "manage.html", manage_ctx(db, user, event, errors=errors), status=422)
    db.add(Attendee(event_id=event.id, **item.model_dump()))
    db.commit()
    msgs = [f"Added {item.name}."]
    if manage_ctx(db, user, event)["ticket_files"]:
        msgs.append("Re-upload the ticket PDF to attach a ticket to this guest.")
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs))


@app.post("/attendees/{attendee_id}/edit", response_class=HTMLResponse)
def edit_attendee(
    attendee_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    ticket_type: str = Form("Standard"), ticket_count: str = Form("1"), notes: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    attendee = get_attendee_or_404(db, attendee_id)
    event = get_event_or_404(db, attendee.event_id)
    item, errors = validate_attendee_form(name=name, phone=phone, email=email, age=age,
                                          ticket_type=ticket_type, ticket_count=ticket_count, notes=notes)
    if item and is_duplicate(db, event.id, item, exclude_id=attendee.id):
        errors = [f"Another guest named {item.name} already has {item.phone}."]
    if errors:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=[f"{attendee.name}: {e}" for e in errors]), status=422)
    for key, value in item.model_dump().items():
        setattr(attendee, key, value)
    db.commit()
    msgs = [f"Updated {attendee.name}."]
    if attendee.tickets and len(attendee.tickets) != attendee.ticket_count:
        msgs.append(f"{attendee.name} now has {attendee.ticket_count} ticket(s) but {len(attendee.tickets)} PDF(s) "
                    "attached - re-upload the ticket PDF to re-map.")
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs))


@app.post("/attendees/{attendee_id}/delete", response_class=HTMLResponse)
def delete_attendee(attendee_id: int, request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    event = get_event_or_404(db, attendee.event_id)
    name = attendee.name
    db.delete(attendee)
    db.commit()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=[f"Removed {name}."]))


# --------------------------------------------------------------------------- doorman

@app.get("/events/{event_id}/door", response_class=HTMLResponse)
def door_page(event_id: int, request: Request, user: User = Depends(require_staff),
              db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    attendees = search_attendees(db, event.id, "")
    return render(request, "doorman.html", {
        "user": user, "event": event, "attendees": attendees, "q": "",
        "stats": compute_stats(db, event.id),
    })


def search_attendees(db: Session, event_id: int, q: str, limit: int = 300) -> list[Attendee]:
    query = (
        db.query(Attendee).options(selectinload(Attendee.tickets))
        .filter(Attendee.event_id == event_id)
    )
    q = q.strip()
    if q:
        conds = [Attendee.name.ilike(f"%{like_escape(q)}%", escape="\\")]
        digits = "".join(c for c in q if c.isdigit())
        if digits:
            conds.append(Attendee.phone.like(f"%{digits}%"))
            if digits.startswith("0") and len(digits) > 1:  # local format: 052-5607772
                conds.append(Attendee.phone.like(f"%{digits.lstrip('0')}%"))
        query = query.filter(or_(*conds))
    return query.order_by(func.lower(Attendee.name), Attendee.id).limit(limit).all()


@app.get("/events/{event_id}/search", response_class=HTMLResponse)
def search(event_id: int, request: Request, q: str = "", user: User = Depends(require_staff),
           db: Session = Depends(get_db)):
    get_event_or_404(db, event_id)
    return render(request, "partials/attendee_rows.html",
                  {"attendees": search_attendees(db, event_id, q), "q": q.strip()})


@app.get("/events/{event_id}/stats", response_class=HTMLResponse)
def stats(event_id: int, request: Request, user: User = Depends(require_staff),
          db: Session = Depends(get_db)):
    get_event_or_404(db, event_id)
    return render(request, "partials/stats.html", {"stats": compute_stats(db, event_id)})


def card_response(request: Request, attendee: Attendee, trigger: dict | str) -> HTMLResponse:
    header = trigger if isinstance(trigger, str) else json.dumps({"refreshStats": "", **trigger})
    return render(request, "partials/attendee_rows.html",
                  {"attendees": [attendee], "q": "x"}, headers={"HX-Trigger": header})


@app.post("/attendees/{attendee_id}/toggle", response_class=HTMLResponse)
def toggle_check_in(attendee_id: int, request: Request, user: User = Depends(require_staff),
                    db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    attendee.checked_in = not attendee.checked_in
    attendee.checked_in_at = utcnow() if attendee.checked_in else None
    db.commit()
    return card_response(request, attendee, "refreshStats")


@app.post("/attendees/{attendee_id}/whatsapp", response_class=HTMLResponse)
async def whatsapp_dispatch(attendee_id: int, request: Request,
                            user: User = Depends(require_staff), db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    event = db.get(Event, attendee.event_id)
    mark_admitted(db, attendee)
    if not attendee.tickets:
        return card_response(request, attendee, toast("No ticket PDF is attached to this guest.", "error"))
    try:
        for ticket in attendee.tickets:
            await send_whatsapp_pdf(
                attendee.phone, ticket.file_path,
                caption=f"Hi {attendee.name.split()[0]}, here is your ticket for {event.name}.",
                file_name=f"ticket-{ticket.page_number}.pdf",
            )
    except WhatsAppError as exc:
        log.warning("WhatsApp dispatch failed for attendee %s: %s", attendee.id, exc)
        return card_response(request, attendee, toast(f"WhatsApp failed: {exc}", "error"))
    n = len(attendee.tickets)
    return card_response(request, attendee, toast(f"Sent {n} ticket{'s' if n != 1 else ''} to {attendee.name}"))


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def public_base_url(request: Request) -> str:
    """Base URL for QR codes. A localhost PUBLIC_BASE_URL is useless to a phone, so it is
    ignored in favour of the address the doorman's own browser is using."""
    if config.PUBLIC_BASE_URL and not is_loopback(config.PUBLIC_BASE_URL):
        return config.PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


@app.post("/attendees/{attendee_id}/qr", response_class=HTMLResponse)
def qr_modal(attendee_id: int, request: Request, user: User = Depends(require_staff),
             db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    mark_admitted(db, attendee)
    base = public_base_url(request)
    unreachable = is_loopback(base)
    codes = []
    for ticket in attendee.tickets:
        url = f"{base}/tickets/claim/{ticket.claim_token}/ticket.pdf"
        qr = segno.make(url, error="m")
        codes.append({"page": ticket.page_number, "url": url,
                      "svg": qr.svg_data_uri(scale=8, border=2, dark="#0f172a", light="#ffffff")})
    return render(request, "partials/qr_modal.html",
                  {"attendee": attendee, "codes": codes, "unreachable": unreachable},
                  headers={"HX-Trigger": "refreshStats"})


def ticket_by_token(db: Session, token: str) -> Ticket:
    ticket = db.query(Ticket).options(selectinload(Ticket.attendee)).filter(
        Ticket.claim_token == token).first()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    return ticket


@app.get("/tickets/claim/{token}", response_class=HTMLResponse)
def claim_ticket(token: str, request: Request, db: Session = Depends(get_db)):
    """Public, unguessable (UUID4 hex) landing page the QR code points to."""
    ticket = ticket_by_token(db, token)
    event = db.get(Event, ticket.event_id)
    return render(request, "claim.html", {"ticket": ticket, "event": event,
                                          "attendee": ticket.attendee})


def ticket_pdf_response(db: Session, token: str, disposition: str) -> FileResponse:
    ticket = ticket_by_token(db, token)
    path = Path(ticket.file_path).resolve()
    if not path.is_relative_to(config.STORAGE_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "Ticket file is unavailable")
    return FileResponse(path, media_type="application/pdf", filename=f"ticket-{ticket.page_number}.pdf",
                        content_disposition_type=disposition,
                        headers={"Cache-Control": "private, no-store"})


@app.get("/tickets/claim/{token}/ticket.pdf")
def open_ticket_pdf(token: str, db: Session = Depends(get_db)):
    """Direct link to the PDF itself (what the QR code encodes); opens in the phone's viewer."""
    return ticket_pdf_response(db, token, "inline")


@app.get("/tickets/claim/{token}/download")
def download_ticket(token: str, db: Session = Depends(get_db)):
    return ticket_pdf_response(db, token, "attachment")


# --------------------------------------------------------------------------- users & account

MIN_PASSWORD = 8


def users_ctx(db: Session, user: User, **extra) -> dict:
    return {"user": user, "users": db.query(User).order_by(User.created_at, User.id).all(),
            "errors": [], "messages": [], "roles": list(Role), **extra}


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "users.html", users_ctx(db, user))


@app.post("/users", response_class=HTMLResponse)
def create_user(
    request: Request,
    email: str = Form(...), full_name: str = Form(...), password: str = Form(...),
    role: str = Form("doorman"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    email = email.strip().lower()
    errors = []
    if "@" not in email or " " in email or len(email) > 255:
        errors.append("Enter a valid email address.")
    if not full_name.strip():
        errors.append("Full name is required.")
    if len(password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    if role not in Role.__members__:
        errors.append("Invalid role.")
    if not errors and db.query(User).filter(User.email == email).first():
        errors.append("A user with that email already exists.")
    if errors:
        return render(request, "users.html", users_ctx(db, user, errors=errors), status=422)
    db.add(User(email=email, full_name=full_name.strip()[:255],
                hashed_password=hash_password(password), role=Role[role]))
    db.commit()
    return render(request, "users.html", users_ctx(db, user, messages=[f"Created {email}."]))


@app.post("/users/{user_id}", response_class=HTMLResponse)
def update_user(
    user_id: int, request: Request,
    full_name: str = Form(...), role: str = Form(...), is_active: str = Form(""),
    new_password: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    active = is_active == "on"
    errors = []
    if role not in Role.__members__:
        errors.append("Invalid role.")
    if not full_name.strip():
        errors.append("Full name is required.")
    if new_password and len(new_password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    if target.id == user.id and (role != Role.admin.value or not active):
        errors.append("You cannot demote or deactivate your own account.")
    if errors:
        return render(request, "users.html", users_ctx(db, user, errors=errors), status=422)
    target.full_name = full_name.strip()[:255]
    target.role = Role[role]
    target.is_active = active
    if new_password:
        target.hashed_password = hash_password(new_password)
    db.commit()
    return render(request, "users.html", users_ctx(db, user, messages=[f"Updated {target.email}."]))


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(get_current_user)):
    return render(request, "account.html", {"user": user, "errors": [], "messages": []})


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...), new_password: str = Form(...),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    errors = []
    if not verify_password(current_password, user.hashed_password):
        errors.append("Current password is incorrect.")
    if len(new_password) < MIN_PASSWORD:
        errors.append(f"New password must be at least {MIN_PASSWORD} characters.")
    if errors:
        return render(request, "account.html", {"user": user, "errors": errors, "messages": []}, status=422)
    user.hashed_password = hash_password(new_password)
    db.commit()
    return render(request, "account.html", {"user": user, "errors": [], "messages": ["Password updated."]})
