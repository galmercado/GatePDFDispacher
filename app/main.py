"""FastAPI application: routes, templates and exception handlers."""
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import segno
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .auth import (
    authenticate, clear_auth_cookie, create_access_token, get_current_user, hash_password,
    require_admin, require_staff, set_auth_cookie,
)
from .database import SessionLocal, get_db, init_db
from .models import Attendee, Event, Role, Ticket, User, utcnow
from .services import (
    ImportValidationError, PdfProcessingError, WhatsAppError, import_attendees,
    map_pdf_to_attendees, parse_guest_list, send_whatsapp_pdf,
)

log = logging.getLogger("gate")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def seed_admin() -> None:
    with SessionLocal() as db:
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


def toast(message: str, kind: str = "success") -> str:
    return json.dumps({"toast": {"message": message, "kind": kind}})


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
            "error": None, **extra}


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    return render(request, "events.html", events_ctx(db, user))


@app.post("/events")
def create_event(
    request: Request,
    name: str = Form(...),
    location: str = Form(""),
    event_date: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = name.strip()
    try:
        when = datetime.fromisoformat(event_date)
    except ValueError:
        when = None
    if not name or when is None:
        return render(request, "events.html",
                      events_ctx(db, user, error="A name and a valid date are required.",
                                 open_modal=True), status=422)
    event = Event(name=name[:255], location=location.strip()[:255], event_date=when)
    db.add(event)
    db.commit()
    return RedirectResponse("/events", status_code=303)


def manage_ctx(db: Session, user: User, event: Event, **extra) -> dict:
    tickets = db.query(func.count(Ticket.id)).filter(Ticket.event_id == event.id).scalar()
    return {"user": user, "event": event, "stats": compute_stats(db, event.id),
            "ticket_files": tickets, "messages": [], "errors": [], **extra}


@app.get("/events/{event_id}/manage", response_class=HTMLResponse)
def manage_page(event_id: int, request: Request, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    return render(request, "manage.html", manage_ctx(db, user, event))


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
        count = map_pdf_to_attendees(db, event.id, content)
    except PdfProcessingError as exc:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=[str(exc)]), status=422)
    return render(request, "manage.html",
                  manage_ctx(db, user, event, messages=[f"Split and attached {count} ticket(s)."]))


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
    headers = {"HX-Trigger": trigger if isinstance(trigger, str) else json.dumps(trigger)}
    return render(request, "partials/attendee_rows.html",
                  {"attendees": [attendee], "q": "x"}, headers=headers)


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


@app.get("/attendees/{attendee_id}/qr", response_class=HTMLResponse)
def qr_modal(attendee_id: int, request: Request, user: User = Depends(require_staff),
             db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    codes = []
    for ticket in attendee.tickets:
        url = f"{base}/tickets/claim/{ticket.claim_token}"
        qr = segno.make(url, error="m")
        codes.append({"page": ticket.page_number, "url": url,
                      "svg": qr.svg_data_uri(scale=8, border=2, dark="#0f172a", light="#ffffff")})
    return render(request, "partials/qr_modal.html", {"attendee": attendee, "codes": codes})


@app.get("/tickets/claim/{token}")
def claim_ticket(token: str, db: Session = Depends(get_db)):
    """Public, unguessable (UUID4 hex) download link that the QR code points to."""
    ticket = db.query(Ticket).filter(Ticket.claim_token == token).first()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    path = Path(ticket.file_path).resolve()
    if not path.is_relative_to(config.STORAGE_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "Ticket file is unavailable")
    return FileResponse(path, media_type="application/pdf", filename=f"ticket-{ticket.page_number}.pdf",
                        headers={"Cache-Control": "private, no-store"})
