"""FastAPI application: routes, templates and exception handlers."""
import asyncio
import hashlib
import io
import json
import shutil
import random
import re
import uuid
import zipfile
import secrets
from urllib.parse import urlsplit
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import segno
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, config, data, google_auth, site_settings
from .auth import (
    authenticate, clear_auth_cookie, create_access_token, get_current_user, hash_password,
    require_admin, require_staff, set_auth_cookie, verify_password,
)
from . import lines as linesmod
from .google_auth import GoogleAuthError, fetch_profile
from .database import SessionLocal, get_db, init_db
from .models import AppSetting, Attendee, InboxEntry, TicketSource, Event, ImportIssue, OrphanMessage, Role, SentMessage, SpareTicket, WhatsAppLine, Member, TicketBundle, Ticket, User, utcnow
from .messages import ticket_caption
from . import inbox as inboxmod
from .inbox import match_or_create_event
from .progress import Reporter, status as job_status
from .pdfs import DriveError, fetch_drive_pdf, parse_ticket_header, read_ticket_meta
from .services import (
    AttendeeIn, ImportValidationError, describe_validation_error, PdfProcessingError, WhatsAppError, add_ticket_pdf, delete_whatsapp_message,
    import_attendees,
    GridRow, MemberBook, RegistrationError, evolution_connect, evolution_create_instance, evolution_list_instances, evolution_owner_number, evolution_set_profile, evolution_delete_instance, evolution_state,
    event_ticket_dir, fetch_registration_page, name_keys, normalize_phone, parse_registration_grid, parse_member_file, parse_member_text, parse_registration_html, parse_rows, parse_guest_list, rebalance_tickets, release_tickets, audit_tickets, send_whatsapp_pdf, send_whatsapp_text, ticket_status,
)

log = logging.getLogger("gate")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def format_age(value) -> str:
    """20.0 -> "20", 20.5 -> "20.5", None -> ""."""
    if value is None or value == "":
        return ""
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"


templates.env.filters["age"] = format_age
templates.env.globals["app_version"] = __version__
templates.env.globals["error_hint"] = lambda text: linesmod.error_hint(text)


def seed_admin() -> None:
    with SessionLocal() as db:
        for a in db.query(Attendee).all():  # bring older records to international format
            fixed = normalize_phone(a.phone)
            if fixed != a.phone:
                a.phone = fixed
        db.commit()
        if config.ADMIN_EMAIL and not db.query(User).filter(User.email == config.ADMIN_EMAIL).first():
            db.add(User(email=config.ADMIN_EMAIL, full_name="Administrator", role=Role.admin,
                        hashed_password=hash_password(secrets.token_urlsafe(32))))  # Google sign-in only
            db.commit()
            log.warning("Seeded Google admin %s", config.ADMIN_EMAIL)
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


async def revoke_message(db: Session, msg: SentMessage | OrphanMessage) -> bool:
    """Delete one sent ticket message for everyone; record the outcome. Returns success."""
    instance = None
    if getattr(msg, "line_id", None):
        line = db.get(WhatsAppLine, msg.line_id)
        instance = line.instance_name if line else None
    try:
        await delete_whatsapp_message(msg.remote_jid, msg.message_id, instance=instance)
    except WhatsAppError as exc:
        msg.attempts += 1
        msg.delete_at = utcnow() + timedelta(minutes=random.uniform(*config.WHATSAPP_RETRY_MINUTES))
        db.commit()
        log.warning("Could not delete WhatsApp message %s (attempt %s): %s", msg.message_id, msg.attempts, exc)
        return False
    msg.status, msg.deleted_at = "deleted", utcnow()
    db.commit()
    return True


async def revoke_attendee_messages(db: Session, attendee: Attendee) -> tuple[int, int]:
    """Delete every live ticket message sent for this order (and only this order, even if other
    orders share the phone number). Returns (deleted, failed)."""
    pending = db.query(SentMessage).filter(SentMessage.attendee_id == attendee.id,
                                           SentMessage.status == "sent").all()
    ok = 0
    for msg in pending:
        ok += await revoke_message(db, msg)
    return ok, len(pending) - ok


def rotate_claim_tokens(db: Session, attendee: Attendee) -> None:
    """Issue new claim tokens: QR codes / links shown before stop working."""
    for ticket in attendee.tickets:
        ticket.claim_token = uuid.uuid4().hex
    db.query(TicketBundle).filter(TicketBundle.attendee_id == attendee.id).delete()    # their ZIP links too
    db.commit()


def event_end_utc(event: Event) -> datetime:
    """The event's end (start + EVENT_DURATION_HOURS) as naive UTC. Event dates are entered in local time."""
    local = event.event_date.replace(tzinfo=ZoneInfo(config.EVENT_TIMEZONE))
    return (local.astimezone(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=config.EVENT_DURATION_HOURS))


def schedule_delete_at(event: Event, now: datetime | None = None) -> datetime:
    """Random moment 12-36 h (configurable) after the event is over - or after now, if it already is."""
    base = max(event_end_utc(event), now or utcnow())
    return base + timedelta(hours=random.uniform(config.WHATSAPP_DELETE_MIN_HOURS,
                                                 config.WHATSAPP_DELETE_MAX_HOURS))


async def cleanup_due_messages(now: datetime | None = None) -> int:
    """Delete ticket messages whose scheduled time has come (also those of deleted events). Messages
    are deleted one by one with random pauses; a failed deletion is retried after a random 30-90 min;
    after 47 h (WhatsApp no longer allows it) a message is marked expired. Returns the number deleted."""
    if not config.WHATSAPP_DELETE_AFTER_EVENT:
        return 0
    now = now or utcnow()
    stale = now - timedelta(hours=config.WHATSAPP_DELETE_GIVE_UP_HOURS)
    deleted = 0
    with SessionLocal() as db:
        for model in (SentMessage, OrphanMessage):
            for msg in db.query(model).filter(model.status == "sent").order_by(model.sent_at).all():
                if msg.delete_at is None:  # message from before scheduling existed
                    event = db.get(Event, msg.event_id) if model is SentMessage else None
                    msg.delete_at = schedule_delete_at(event, now) if event else now
                    db.commit()
                if msg.delete_at > now:
                    continue
                if msg.sent_at <= stale:
                    msg.status = "expired"
                    db.commit()
                    continue
                if deleted:
                    await asyncio.sleep(random.uniform(*config.WHATSAPP_DELETE_GAP))
                deleted += await revoke_message(db, msg)   # on failure it schedules its own retry
    return deleted


def purge_old_tickets(now: datetime | None = None) -> int:
    """Delete the ticket PDF files of events that ended TICKET_RETENTION_DAYS ago. The guest list and attendance
    stay; tickets keep their rows (so history such as 'sent' remains) but lose file and download links.
    Returns the number of events purged."""
    days = config.TICKET_RETENTION_DAYS
    if days <= 0:
        return 0
    now = now or utcnow()
    purged = 0
    with SessionLocal() as db:
        for event in db.query(Event).filter(Event.tickets_purged_at.is_(None)).all():
            if event_end_utc(event) + timedelta(days=days) > now:
                continue
            shutil.rmtree(event_ticket_dir(event.id), ignore_errors=True)
            db.query(SpareTicket).filter(SpareTicket.event_id == event.id).delete()
            db.query(Ticket).filter(Ticket.event_id == event.id).update({Ticket.file_path: ""})
            ids = [i for (i,) in db.query(Attendee.id).filter(Attendee.event_id == event.id)]
            if ids:
                db.query(TicketBundle).filter(TicketBundle.attendee_id.in_(ids)).delete(synchronize_session=False)
            db.query(AppSetting).filter(AppSetting.key.like(f"tkh.{event.id}.%")).delete(synchronize_session=False)
            event.tickets_purged_at = now
            db.commit()
            purged += 1
            log.info("Deleted the ticket files of event %s (%s days after it ended)", event.id, days)
    return purged


async def cleanup_loop() -> None:
    await asyncio.sleep(config.WHATSAPP_CLEANUP_STARTUP_DELAY)
    while True:
        try:
            purge_old_tickets()
        except Exception:
            log.exception("Ticket file cleanup failed")
        try:
            n = await cleanup_due_messages()
            if n:
                log.info("Deleted %s WhatsApp ticket message(s) after their event", n)
        except Exception:  # never let the background task die
            log.exception("WhatsApp cleanup run failed")
        await asyncio.sleep(config.WHATSAPP_CLEANUP_POLL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.STORAGE_DIR.joinpath("tickets").mkdir(parents=True, exist_ok=True)
    init_db()
    seed_admin()
    with SessionLocal() as db:
        site_settings.load_and_apply(db)
        linesmod.ensure_default_line(db)
    tasks = [asyncio.create_task(cleanup_loop()), asyncio.create_task(linesmod.pretest_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()


app = FastAPI(title="Gate - Event Ticketing & Door Vetting", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers

def render(request: Request, name: str, ctx: dict | None = None, status: int = 200,
           headers: dict | None = None) -> HTMLResponse:
    if name == "manage.html" and ctx is not None:
        ctx.setdefault("bookmarklet", bookmarklet_source(public_base_url(request)))
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
    """Ticket counts (not guests): overall, per category and per ticket kind (adult/youth)."""
    def paid(col, admitted_only=False):
        expr = func.sum(col).filter(Attendee.checked_in.is_(True)) if admitted_only else func.sum(col)
        return func.coalesce(expr, 0)

    rows = db.query(
        Attendee.category, func.count(Attendee.id),
        func.count(Attendee.id).filter(Attendee.checked_in.is_(True)),
        paid(Attendee.ticket_count), func.coalesce(func.sum(Attendee.admitted_adult + Attendee.admitted_youth), 0),
        paid(Attendee.adult_count), func.coalesce(func.sum(Attendee.admitted_adult), 0),
        paid(Attendee.youth_count), func.coalesce(func.sum(Attendee.admitted_youth), 0),
    ).filter(Attendee.event_id == event_id).group_by(Attendee.category).all()
    keys = ("guests", "guests_in", "total", "admitted", "adult_total", "adult_in", "youth_total", "youth_in")
    by_cat = {r[0]: dict(zip(keys, r[1:])) for r in rows}
    blank = dict.fromkeys(keys, 0)
    categories = [{"name": c, **by_cat.get(c, blank)} for c in data.CATEGORIES]
    overall = {k: sum(c[k] for c in categories) for k in keys}
    overall.update(
        remaining=overall["total"] - overall["admitted"],
        percent=round(overall["admitted"] * 100 / overall["total"]) if overall["total"] else 0,
        categories=categories,
    )
    return overall


def group_by_category(attendees: list[Attendee]) -> list[tuple[str, list[Attendee]]]:
    return [(c, [a for a in attendees if a.category == c]) for c in data.CATEGORIES
            if any(a.category == c for a in attendees)]


def toast(message: str, kind: str = "success") -> dict:
    return {"toast": {"message": message, "kind": kind}}


def set_admission(attendee: Attendee, adult: int, youth: int) -> None:
    """Record how many adult/youth tickets have been let in (clamped to the order) and keep checked_in in sync."""
    attendee.admitted_adult = max(0, min(int(adult), attendee.adult_count))
    attendee.admitted_youth = max(0, min(int(youth), attendee.youth_count))
    if attendee.admitted_total > 0:
        attendee.checked_in = True
        attendee.checked_in_at = attendee.checked_in_at or utcnow()
    else:
        attendee.checked_in = False
        attendee.checked_in_at = None


def admit_all(db: Session, attendee: Attendee) -> None:
    """Idempotent: let in every remaining ticket of the order."""
    set_admission(attendee, attendee.adult_count, attendee.youth_count)
    db.commit()


mark_admitted = admit_all


def reset_admission(db: Session, attendee: Attendee) -> None:
    set_admission(attendee, 0, 0)
    for ticket in attendee.tickets:
        ticket.delivered_at = None
    db.commit()


def full_targets(attendee: Attendee) -> list[Ticket]:
    """Tickets the main WhatsApp/QR buttons hand out: the rest, if some were already given out
    (after a partial send); otherwise all of them (a plain resend)."""
    delivered = [t for t in attendee.tickets if t.delivered_at]
    undelivered = [t for t in attendee.tickets if not t.delivered_at]
    return undelivered if (delivered and undelivered) else list(attendee.tickets)


HELD_MSG = "The remaining tickets are held until an admin confirms the shirt (Shirt missing)."


def hand_out_targets(attendee: Attendee) -> list[Ticket]:
    """What the main WhatsApp/QR buttons hand out: like full_targets, but never the tickets held for a missing shirt."""
    if not attendee.held:
        return full_targets(attendee)
    return [t for t in attendee.tickets if not t.delivered_at][: attendee.releasable]


def split_counts(k: int, adult: int, youth: int) -> tuple[int, int]:
    """k tickets were actually handed over out of (adult + youth) requested: adults first."""
    a = min(adult, k)
    return a, min(youth, k - a)


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
    return {"status": "ok", "version": __version__}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    try:
        get_current_user(request, db)
        return RedirectResponse("/events", status_code=303)
    except HTTPException:
        return render(request, "login.html", {"error": None, "email": "", "google": google_auth.enabled()})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = authenticate(db, email, password)
    if user is None:
        return render(request, "login.html",
                      {"error": "Invalid email or password.", "email": email,
                       "google": google_auth.enabled()}, status=401)
    resp = RedirectResponse("/events", status_code=303)
    set_auth_cookie(resp, create_access_token(user))
    return resp


def oauth_redirect_uri(request: Request) -> str:
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return base + google_auth.CALLBACK_PATH


def login_error(request: Request, message: str, status: int = 403) -> HTMLResponse:
    resp = render(request, "login.html", {"error": message, "email": "", "google": google_auth.enabled()},
                  status=status)
    resp.delete_cookie(google_auth.STATE_COOKIE, path="/")
    return resp


@app.get("/auth/google/login", include_in_schema=False)
def google_login(request: Request):
    if not google_auth.enabled():
        raise HTTPException(404, "Google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(google_auth.authorization_url(oauth_redirect_uri(request), state), status_code=303)
    resp.set_cookie(google_auth.STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=config.COOKIE_SECURE, path="/")
    return resp


@app.get(google_auth.CALLBACK_PATH, include_in_schema=False)
async def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                          db: Session = Depends(get_db)):
    if not google_auth.enabled():
        raise HTTPException(404, "Google sign-in is not configured")
    expected = request.cookies.get(google_auth.STATE_COOKIE, "")
    if error or not code or not expected or not secrets.compare_digest(expected, state):
        return login_error(request, "Google sign-in was cancelled or expired. Please try again.", 400)
    try:
        profile = await fetch_profile(code, oauth_redirect_uri(request))
    except GoogleAuthError as exc:
        log.warning("Google sign-in failed: %s", exc)
        return login_error(request, str(exc), 400)
    user = db.query(User).filter(User.email == profile["email"]).first()
    if user is None or not user.is_active:
        log.warning("Google sign-in refused for %s (not an active user)", profile["email"])
        return login_error(request, f"{profile['email']} is not authorised. Ask an admin to add your email under Users.")
    resp = RedirectResponse("/events", status_code=303)
    resp.delete_cookie(google_auth.STATE_COOKIE, path="/")
    set_auth_cookie(resp, create_access_token(user))
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    clear_auth_cookie(resp)
    return resp


# --------------------------------------------------------------------------- events

def lines_alert_for(db: Session, user: User) -> dict | None:
    """Admins only: shown on the home page when fewer than MIN_WORKING_LINES WhatsApp lines work."""
    if user.role != Role.admin:
        return None
    info = linesmod.line_summary(db)
    return {**info, "minimum": config.MIN_WORKING_LINES} if info["low"] else None


def events_ctx(db: Session, user: User, **extra) -> dict:
    events = db.query(Event).order_by(Event.event_date.desc()).all()
    return {"user": user, "events": events, "stats": {e.id: compute_stats(db, e.id) for e in events},
            "error": None, "messages": [], "lines_alert": lines_alert_for(db, user), "opponents": data.OPPONENTS, "arenas": data.ARENAS,
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
    location: str = Form(""),
    location_other: str = Form(""),
    event_date: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        rival = pick(opponent, opponent_other, "opponent")
        place = pick(location or data.DEFAULT_LOCATION, location_other, "location")
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


def load_issues(db: Session, event_id: int) -> list[dict]:
    rows = db.query(ImportIssue).filter(ImportIssue.event_id == event_id).order_by(ImportIssue.id).all()
    return [{"id": r.id, "label": r.label, "data": json.loads(r.data), "errors": json.loads(r.errors)} for r in rows]


def manage_ctx(db: Session, user: User, event: Event, **extra) -> dict:
    tickets = db.query(func.count(Ticket.id)).filter(Ticket.event_id == event.id, Ticket.file_path != "").scalar()
    status = ticket_status(db, event.id)
    warnings = list(extra.pop("warnings", []))
    if status["missing"]:
        warnings.append(f"{status['missing']} ticket(s) for {status['guests_missing']} guest(s) still have no PDF. "
                        "Upload more ticket pages below - they are assigned automatically.")
    attendees = (db.query(Attendee).options(selectinload(Attendee.tickets))
                 .filter(Attendee.event_id == event.id).order_by(func.lower(Attendee.name), Attendee.id).all())
    return {"user": user, "event": event, "stats": compute_stats(db, event.id),
            "issues": load_issues(db, event.id), "has_cookie": bool(registration_cookie(db)), "members_count": db.query(Member).count(),
            "registration_host": config.REGISTRATION_BASE_URL.removeprefix("https://"),
            "registration_url": (config.REGISTRATION_BASE_URL + config.REGISTRATION_PATH.format(game_id=event.external_id)) if event.external_id else "", "max_pdf_mb": config.MAX_PDF_BYTES // (1024 * 1024),
            "max_list_mb": config.MAX_LIST_BYTES // (1024 * 1024), "ticket_files": tickets, "spare_pages": status["spare"], "warnings": warnings, "attendees": attendees, "groups": group_by_category(attendees),
            "categories": data.CATEGORIES, "messages": [], "errors": [], **extra}


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
    event_id: int, request: Request, file: UploadFile = File(...), job: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    content = await file.read(config.MAX_LIST_BYTES + 1)
    report = Reporter(job)
    try:
        if len(content) > config.MAX_LIST_BYTES:
            raise ImportValidationError(["File is too large (5 MB max)"])
        report(0.05, "Reading the file")
        parsed = await asyncio.to_thread(parse_guest_list, file.filename or "", content, member_book(db))
    except ImportValidationError as exc:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=exc.errors[:50]), status=422)
    return await finish_import(request, db, user, event, parsed, report=report)


async def finish_import(request: Request, db: Session, user: User, event: Event, parsed, source: str = "",
                        report: Reporter | None = None) -> HTMLResponse:
    report = report or Reporter()
    msgs, warnings = await asyncio.to_thread(run_import, db, event, parsed, source, report.part(0.1, 1.0))
    report.done()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs, warnings=warnings))


def member_book(db: Session) -> MemberBook:
    """Phone book of organisation members (Members page), used to fill in phone numbers that are missing."""
    entries = [(m.name, m.phone) for m in db.query(Member)]
    return MemberBook(entries, override=linesmod.get_setting(db, "members_override_phone", "") == "1")


def fill_member_phone(db: Session, name: str, phone: str, category: str = "") -> str:
    if 7 <= len(re.sub(r"\D", "", phone or "")) <= 15:
        return phone
    if data.CATEGORY_ALIASES.get(category.strip(), category.strip()) == "פלוס":      # plus guests never get a member's number
        return phone
    return member_book(db).lookup(name) or phone


def run_import(db: Session, event: Event, parsed, source: str = "", progress=None) -> tuple[list[str], list[str]]:
    """Save the valid rows (adding new guests, updating existing ones), park the bad rows for fix-or-skip and
    hand out spare ticket PDFs. Returns (messages, warnings)."""
    outcome = import_attendees(db, event.id, parsed.valid, progress=(lambda f, label=None: progress(0.85 * f, label)) if progress else None)
    if progress:
        progress(0.9, "Giving spare ticket pages to the new guests")
    for issue in parsed.issues:
        db.add(ImportIssue(event_id=event.id, label=issue.label, data=json.dumps(issue.row, ensure_ascii=False),
                           errors=json.dumps(issue.errors, ensure_ascii=False)))
    db.commit()
    result = rebalance_tickets(db, event.id)
    msgs = [f"{source}Imported {outcome.added} guest(s)."]
    if outcome.updated:
        shown = "; ".join(outcome.changes[:12]) + (f"; … and {len(outcome.changes) - 12} more" if len(outcome.changes) > 12 else "")
        msgs.append(f"Updated {outcome.updated} existing guest(s): {shown}")
    if outcome.unchanged:
        msgs.append(f"{outcome.unchanged} existing guest(s) already up to date.")
    if parsed.filled:
        msgs.append(f"{parsed.filled} phone number(s) were taken from the Members list.")
    if result["assigned"]:
        msgs.append(f"Assigned {result['assigned']} spare ticket page(s) automatically.")
    warnings = list(outcome.warnings)
    if parsed.issues:
        missing = sum(1 for i in parsed.issues if any(e.startswith("phone") for e in i.errors))
        hint = f" ({missing} without a phone number - add them to the Members list and press “Retry waiting rows”)" if missing else ""
        warnings.append(f"{len(parsed.issues)} row(s) have problems and were NOT imported{hint}. Fix them or skip them below.")
    if parsed.ignored:
        warnings.append("These columns/keys were not recognised and were ignored: " + ", ".join(parsed.ignored)
                        + ". Recognised: name, phone, category, adult/מבוגר, youth/נוער, email, age, notes.")
    if parsed.bad_ages:
        warnings.append(f"{parsed.bad_ages} row(s) had an age that is not a number; the age was left empty.")
    return msgs, warnings


# ---- import from the registration system (gate11.co.il admin page)

COOKIE_KEY = "registration_cookie"


def registration_cookie(db: Session) -> str:
    return linesmod.get_setting(db, COOKIE_KEY, config.REGISTRATION_COOKIE)


@app.post("/events/{event_id}/registration/id", response_class=HTMLResponse)
def save_registration_id(event_id: int, request: Request, external_id: str = Form(""),
                         user: User = Depends(require_admin), db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    external_id = external_id.strip()
    if external_id and not re.fullmatch(r"\d{1,9}", external_id):
        return render(request, "manage.html", manage_ctx(db, user, event, errors=["The registration event ID must be a number."]), status=422)
    event.external_id = external_id or None
    db.commit()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=[
        f"Registration event ID saved ({external_id})." if external_id else "Registration event ID cleared."]))


@app.post("/registration/cookie", response_class=HTMLResponse)
def save_registration_cookie(request: Request, event_id: int = Form(...), cookie: str = Form(""), clear: str = Form(""),
                             user: User = Depends(require_admin), db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    cookie = " ".join(cookie.split()).removeprefix("Cookie:").strip()
    if clear:
        linesmod.set_setting(db, COOKIE_KEY, "")
        return render(request, "manage.html", manage_ctx(db, user, event, messages=["Session cookie removed."]))
    if not cookie or "=" not in cookie or len(cookie) > 4000:
        return render(request, "manage.html", manage_ctx(db, user, event, errors=[
            "Paste the value of the Cookie header (looks like name=value; other=value)."]), status=422)
    linesmod.set_setting(db, COOKIE_KEY, cookie)
    return render(request, "manage.html", manage_ctx(db, user, event, messages=["Session cookie saved."]))


@app.post("/events/{event_id}/registration/fetch", response_class=HTMLResponse)
async def fetch_registration(event_id: int, request: Request, job: str = Form(""), user: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    report = Reporter(job)
    try:
        report(0.02, "Downloading the reservations page")
        page = await fetch_registration_page(event.external_id or "", registration_cookie(db))
        report(0.07, "Reading the table")
        parsed = await asyncio.to_thread(parse_registration_html, page, member_book(db))
    except (RegistrationError, ImportValidationError) as exc:
        errors = exc.errors if isinstance(exc, ImportValidationError) else [str(exc)]
        return render(request, "manage.html", manage_ctx(db, user, event, errors=errors), status=422)
    return await finish_import(request, db, user, event, parsed, source=f"Registration #{event.external_id}: ", report=report)


@app.post("/events/{event_id}/registration/upload", response_class=HTMLResponse)
async def upload_registration_page(event_id: int, request: Request, file: UploadFile = File(None), html: str = Form(""),
                                   job: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Fallback without any credentials: a page saved from the browser ("Save as...") or pasted HTML."""
    event = get_event_or_404(db, event_id)
    content = (await file.read(config.MAX_REGISTRATION_BYTES + 1)) if file is not None and file.filename else html.encode()
    try:
        if len(content) > config.MAX_REGISTRATION_BYTES:
            raise RegistrationError("The page is too large")
        report = Reporter(job)
        report(0.05, "Reading the table")
        parsed = await asyncio.to_thread(parse_registration_html, content, member_book(db))
    except RegistrationError as exc:
        return render(request, "manage.html", manage_ctx(db, user, event, errors=[str(exc)]), status=422)
    return await finish_import(request, db, user, event, parsed, source="Saved page: ", report=report)


def get_issue_or_404(db: Session, issue_id: int) -> ImportIssue:
    issue = db.get(ImportIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "That row was already handled")
    return issue


@app.post("/import-issues/{issue_id}/fix", response_class=HTMLResponse)
def fix_import_issue(
    issue_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    category: str = Form(data.DEFAULT_CATEGORY), adult_count: str = Form(""), youth_count: str = Form(""),
    notes: str = Form(""), external_ref: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    issue = get_issue_or_404(db, issue_id)
    event = get_event_or_404(db, issue.event_id)
    phone = fill_member_phone(db, name, phone, category)
    form = dict(name=name, phone=phone, email=email, age=age, category=category,
                adult_count=adult_count, youth_count=youth_count, notes=notes, external_ref=external_ref)
    item, errors = validate_attendee_form(**form)
    if item and is_duplicate(db, event.id, item):
        errors = [f"{item.name} ({item.phone}) is already on the list."]
    if errors:        # keep what they typed, show the new problems
        issue.data, issue.errors = json.dumps(form, ensure_ascii=False), json.dumps(errors, ensure_ascii=False)
        db.commit()
        return render(request, "manage.html", manage_ctx(db, user, event), status=422)
    attendee = Attendee(event_id=event.id, **item.model_dump())
    db.add(attendee)
    db.delete(issue)
    db.commit()
    rebalance_tickets(db, event.id)
    return render(request, "manage.html", manage_ctx(db, user, event, **guest_outcome(db, attendee, "Added")))


@app.post("/events/{event_id}/import-issues/retry", response_class=HTMLResponse)
def retry_import_issues(event_id: int, request: Request, user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Re-check every waiting row (e.g. after adding phone numbers to the Members list) and import those that now pass."""
    event = get_event_or_404(db, event_id)
    book = member_book(db)
    solved, filled, still = [], 0, 0
    for issue in db.query(ImportIssue).filter(ImportIssue.event_id == event.id).order_by(ImportIssue.id).all():
        result = parse_rows([json.loads(issue.data)], offset=1, members=book)
        if result.valid:
            solved.extend(result.valid)
            filled += result.filled
            db.delete(issue)
        else:
            still += 1
            issue.errors = json.dumps(result.issues[0].errors, ensure_ascii=False)
    outcome = import_attendees(db, event.id, solved)
    db.commit()
    rebalance_tickets(db, event.id)
    msgs = [f"Added {outcome.added} waiting guest(s)." + (f" {filled} phone number(s) came from the Members list." if filled else "")]
    warns = [f"{still} row(s) still need fixing."] if still else []
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs, warnings=warns))


@app.post("/import-issues/{issue_id}/skip", response_class=HTMLResponse)
def skip_import_issue(issue_id: int, request: Request, user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    issue = get_issue_or_404(db, issue_id)
    event = get_event_or_404(db, issue.event_id)
    label = issue.label
    db.delete(issue)
    db.commit()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=[f"Skipped {label}."]))


@app.post("/events/{event_id}/import-issues/skip-all", response_class=HTMLResponse)
def skip_all_import_issues(event_id: int, request: Request, user: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    n = db.query(ImportIssue).filter(ImportIssue.event_id == event.id).delete()
    db.commit()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=[f"Skipped {n} row(s)."]))


def upload_notes(result: dict) -> list[str]:
    """Plain sentences about duplicate pages and unreadable codes found while adding ticket pages."""
    out = []
    dups = result.get("duplicates") or []
    if dups:
        shown = ", ".join(f"page {n} (same code as {of})" for n, of in dups[:8]) + (" …" if len(dups) > 8 else "")
        out.append(f"Skipped {len(dups)} duplicate page(s) of the PDF: {shown}.")
    out.extend(n for n in result.get("notes") or [] if not n.startswith("Skipped"))
    return out


@app.post("/events/{event_id}/tickets/upload", response_class=HTMLResponse)
async def upload_ticket_pdf(
    event_id: int, request: Request, file: UploadFile = File(...), replace: str = Form(""), job: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    content = await file.read(config.MAX_PDF_BYTES + 1)
    report = Reporter(job)
    try:
        if len(content) > config.MAX_PDF_BYTES:
            raise PdfProcessingError(f"File is too large ({config.MAX_PDF_BYTES // (1024 * 1024)} MB max)")
        result = await asyncio.to_thread(add_ticket_pdf, db, event.id, content, replace == "on", report)
    except PdfProcessingError as exc:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=[str(exc)]), status=422)
    report.done()
    msgs = [f"Added {result['pages']} page(s); assigned {result['assigned']} ticket(s) to guests."]
    if result["spare"]:
        msgs.append(f"{result['spare']} spare page(s) are waiting for new guests.")
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs, warnings=upload_notes(result)))


@app.get("/jobs/{token}")
def job_progress(token: str, user: User = Depends(require_staff)):
    """Progress of a long action (the browser polls this to draw the percentage ring)."""
    return JSONResponse(job_status(token), headers={"Cache-Control": "no-store"})


@app.post("/events/{event_id}/tickets/drive", response_class=HTMLResponse)
async def upload_tickets_from_drive(
    event_id: int, request: Request, links: str = Form(""), replace: str = Form(""), job: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    """Fetch ticket PDFs from Google Drive share links (one per line; the files must be shared with 'anyone with the link')."""
    event = get_event_or_404(db, event_id)
    urls = [u for u in re.split(r"[\s,]+", links) if u][:20]
    if not urls:
        return render(request, "manage.html", manage_ctx(db, user, event, errors=["Paste a Google Drive link."]), status=422)
    msgs, errors = [], []
    report = Reporter(job)
    for index, url in enumerate(urls):
        share = report.part(index / len(urls), (index + 1) / len(urls))
        try:
            content = await fetch_drive_pdf(
                url, progress=lambda done, total, s=share: s(0.3 * (done / total if total else 0), "Downloading from Google Drive"))
            result = await asyncio.to_thread(add_ticket_pdf, db, event.id, content, replace == "on" and index == 0,
                                             share.part(0.3, 1.0))
        except (DriveError, PdfProcessingError) as exc:
            errors.append(f"{url[:70]}: {exc}")
            continue
        msgs.append(f"Drive file {index + 1}: added {result['pages']} page(s); assigned {result['assigned']} ticket(s) to guests."
                    + (f" {result['spare']} spare." if result["spare"] else ""))
        msgs.extend(upload_notes(result))
    report.done()
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs, errors=errors),
                  status=422 if errors and not msgs else 200)


def api_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": detail}, status_code=status)


@app.post("/api/tickets")
async def api_upload_tickets(
    request: Request, file: UploadFile = File(None), header: str = Form(""), url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Automatic ticket upload from another service. Authenticate with the TICKETS_API_KEY (header X-API-Key or
    'Authorization: Bearer'). Send multipart form data: `file` (the PDF) or `url` (a Google Drive link), and `header`
    (the mail subject / title, e.g. 'קיבלת כרטיסים למכבי ראפיד ת"א - עירוני קריית אתא - לא מסומן'). The opponent comes
    from the header, date and venue from the first page; the event is created when it does not exist yet."""
    if not config.TICKETS_API_KEY:
        return api_error(503, "The upload API is switched off: set TICKETS_API_KEY.")
    given = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not given or not secrets.compare_digest(given.encode(), config.TICKETS_API_KEY.encode()):
        return api_error(401, "Wrong or missing API key.")
    header = header or (file.filename if file and file.filename else "")
    try:
        info = parse_ticket_header(header)
    except ValueError as exc:
        return api_error(422, f"header: {exc}")
    try:
        if file is not None and file.filename:
            content = await file.read(config.MAX_PDF_BYTES + 1)
            if len(content) > config.MAX_PDF_BYTES:
                return api_error(413, f"File is too large ({config.MAX_PDF_BYTES // (1024 * 1024)} MB max).")
        elif url:
            content = await fetch_drive_pdf(url)
        else:
            return api_error(422, "Send the PDF as `file` or a Google Drive link as `url`.")
        meta = read_ticket_meta(content)
        event, created, problem = match_or_create_event(db, info, meta)
        if event is None:
            return api_error(422, problem)
        digest = f"tkh.{event.id}.{hashlib.sha256(content).hexdigest()[:24]}"
        if db.get(AppSetting, digest) and (db.query(Ticket).filter(Ticket.event_id == event.id).first()
                                           or db.query(SpareTicket).filter(SpareTicket.event_id == event.id).first()):
            return {"ok": True, "duplicate": True, "event_id": event.id, "event": event.name,
                    "message": "This exact file was uploaded before - nothing changed."}
        result = add_ticket_pdf(db, event.id, content)
    except (DriveError, PdfProcessingError) as exc:
        return api_error(422, str(exc))
    db.merge(AppSetting(key=digest, value=utcnow().isoformat()))
    db.commit()
    return {"ok": True, "duplicate": False, "created_event": created, "event_id": event.id, "event": event.name,
            "date": event.event_date.isoformat(timespec="minutes"), "location": event.location, "opponent": info["opponent"],
            "seating": info["seating"], "pages": result["pages"], "assigned": result["assigned"],
            "spare": result["spare"], "missing": result["missing"],
            "duplicate_pages": [{"page": n, "same_code_as": of} for n, of in result["duplicates"]],
            "pages_without_readable_code": result["unreadable"]}


@app.api_route("/events/{event_id}/tickets/audit", methods=["GET", "POST"], response_class=HTMLResponse)
async def audit_ticket_files(event_id: int, request: Request, job: str = Form(""), user: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    """Re-read every stored ticket page: unchanged files, QR codes, and pages that share a code or a file."""
    event = get_event_or_404(db, event_id)
    report = Reporter(job)
    result = await asyncio.to_thread(audit_tickets, db, event.id, report)
    report.done()
    return render(request, "manage.html", manage_ctx(db, user, event, audit=result))


@app.post("/events/{event_id}/delete", response_class=HTMLResponse)
def delete_event(event_id: int, request: Request, user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Delete an event with its guests, tickets and uploaded PDFs. Ticket messages already sent on
    WhatsApp are kept in a link-free queue so they are still deleted at their scheduled time."""
    event = get_event_or_404(db, event_id)
    name = event.name
    live = db.query(SentMessage).filter(SentMessage.event_id == event.id, SentMessage.status == "sent").all()
    for msg in live:
        db.add(OrphanMessage(line_id=msg.line_id, remote_jid=msg.remote_jid, message_id=msg.message_id, sent_at=msg.sent_at,
                             delete_at=msg.delete_at or schedule_delete_at(event), attempts=msg.attempts))
    for model in (SentMessage, SpareTicket, ImportIssue, Ticket):
        db.query(model).filter(model.event_id == event.id).delete()
    db.query(AppSetting).filter(AppSetting.key.like(f"tkh.{event.id}.%")).delete(synchronize_session=False)
    db.query(TicketSource).filter(TicketSource.event_id == event.id).update({TicketSource.event_id: None})
    db.query(Attendee).filter(Attendee.event_id == event.id).delete()
    db.delete(event)
    db.commit()
    shutil.rmtree(event_ticket_dir(event_id), ignore_errors=True)
    msgs = [f"Deleted “{name}” with its guests and tickets."]
    if live:
        msgs.append(f"{len(live)} WhatsApp ticket message(s) will still be removed at their scheduled time.")
    return render(request, "events.html", events_ctx(db, user, messages=msgs))


# --------------------------------------------------------------------------- manual attendees

def validate_attendee_form(**fields) -> tuple[AttendeeIn | None, list[str]]:
    try:
        return AttendeeIn.model_validate(fields), []
    except ValidationError as exc:
        return None, [(f"{'.'.join(str(p) for p in e['loc'])}: " if e["loc"] else "")
                      + e["msg"].removeprefix("Value error, ") for e in exc.errors()]


def is_duplicate(db: Session, event_id: int, item: AttendeeIn, exclude_id: int | None = None) -> bool:
    if item.external_ref:       # identified by its reservation id: only the same reservation is a duplicate
        q = db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.external_ref == item.external_ref)
        return (q.filter(Attendee.id != exclude_id) if exclude_id else q).first() is not None
    q = db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.phone == item.phone,
                                  func.lower(Attendee.name) == item.name.lower())
    if exclude_id:
        q = q.filter(Attendee.id != exclude_id)
    return q.first() is not None


def guest_outcome(db: Session, attendee: Attendee, verb: str) -> dict:
    """Messages/warnings after a guest was added or changed: were their ticket PDFs assigned?"""
    db.refresh(attendee)
    have, need = len(attendee.tickets), attendee.ticket_count
    if have >= need:
        return {"messages": [f"{verb} {attendee.name} - {need} ticket PDF(s) assigned automatically."]}
    return {"messages": [f"{verb} {attendee.name}."],
            "warnings": [f"No spare ticket PDF left for {attendee.name}: has {have} of {need}. "
                         "Upload more ticket pages below and they will be assigned automatically."]}


def update_attendee(db: Session, attendee: Attendee, event: Event, **fields) -> list[str]:
    """Validate and apply a guest edit (name, phone, category, ticket counts ...). Admission is kept but clamped to the
    new ticket counts, and ticket PDFs are re-balanced. Returns error messages (empty on success)."""
    item, errors = validate_attendee_form(**fields)
    if item and is_duplicate(db, event.id, item, exclude_id=attendee.id):
        errors = [f"Another guest named {item.name} already has {item.phone}."]
    if errors:
        return errors
    for key, value in item.model_dump().items():
        setattr(attendee, key, value)
    set_admission(attendee, attendee.admitted_adult, attendee.admitted_youth)
    db.commit()
    rebalance_tickets(db, event.id)
    return []


@app.post("/events/{event_id}/attendees", response_class=HTMLResponse)
def add_attendee(
    event_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    category: str = Form(data.DEFAULT_CATEGORY), adult_count: str = Form("1"), youth_count: str = Form("0"),
    notes: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    event = get_event_or_404(db, event_id)
    phone = fill_member_phone(db, name, phone, category)
    item, errors = validate_attendee_form(name=name, phone=phone, email=email, age=age, category=category,
                                          adult_count=adult_count, youth_count=youth_count, notes=notes)
    if item and is_duplicate(db, event.id, item):
        errors = [f"{item.name} ({item.phone}) is already on the list."]
    if errors:
        return render(request, "manage.html", manage_ctx(db, user, event, errors=errors), status=422)
    attendee = Attendee(event_id=event.id, **item.model_dump())
    db.add(attendee)
    db.commit()
    rebalance_tickets(db, event.id)
    return render(request, "manage.html", manage_ctx(db, user, event, **guest_outcome(db, attendee, "Added")))


@app.post("/attendees/{attendee_id}/edit", response_class=HTMLResponse)
def edit_attendee(
    attendee_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    category: str = Form(data.DEFAULT_CATEGORY), adult_count: str = Form("1"), youth_count: str = Form("0"),
    notes: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    attendee = get_attendee_or_404(db, attendee_id)
    event = get_event_or_404(db, attendee.event_id)
    errors = update_attendee(db, attendee, event, name=name, phone=phone, email=email, age=age, category=category,
                             adult_count=adult_count, youth_count=youth_count, notes=notes)
    if errors:
        return render(request, "manage.html",
                      manage_ctx(db, user, event, errors=[f"{attendee.name}: {e}" for e in errors]), status=422)
    return render(request, "manage.html", manage_ctx(db, user, event, **guest_outcome(db, attendee, "Updated")))


@app.post("/attendees/{attendee_id}/delete", response_class=HTMLResponse)
async def delete_attendee(attendee_id: int, request: Request, user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    event = get_event_or_404(db, attendee.event_id)
    name = attendee.name
    await revoke_attendee_messages(db, attendee)   # best effort: pull their ticket messages
    release_tickets(db, attendee)      # their ticket pages go back to the pool for other guests
    db.delete(attendee)
    db.commit()
    result = rebalance_tickets(db, event.id)
    msgs = [f"Removed {name}."]
    if result["assigned"]:
        msgs.append(f"Their freed ticket page(s) were reassigned to {result['assigned']} other ticket(s).")
    return render(request, "manage.html", manage_ctx(db, user, event, messages=msgs))


# --------------------------------------------------------------------------- doorman

@app.get("/events/{event_id}/door", response_class=HTMLResponse)
def door_page(event_id: int, request: Request, user: User = Depends(require_staff),
              db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    stats = compute_stats(db, event.id)
    return render(request, "doorman.html", {
        "user": user, "event": event, "stats": stats, "q": "", "event_id": event.id, "categories": data.CATEGORIES,
        "groups": group_by_category(search_attendees(db, event.id, "")),
        "counts": {c["name"]: c for c in stats["categories"]},
    })


def search_attendees(db: Session, event_id: int, q: str, limit: int = 300) -> list[Attendee]:
    query = (
        db.query(Attendee).options(selectinload(Attendee.tickets))
        .filter(Attendee.event_id == event_id)
    )
    q = q.strip()
    if q:
        conds = [Attendee.name.ilike(f"%{like_escape(q)}%", escape="\\"),
                 Attendee.external_ref.like(f"%{like_escape(q)}%", escape="\\")]       # the registration order number
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
    stats = compute_stats(db, event_id)
    return render(request, "partials/attendee_groups.html", {
        "groups": group_by_category(search_attendees(db, event_id, q)), "q": q.strip(),
        "event_id": event_id, "categories": data.CATEGORIES, "counts": {c["name"]: c for c in stats["categories"]}})


@app.get("/events/{event_id}/stats/category/{index}", response_class=HTMLResponse)
def category_count(event_id: int, index: int, request: Request, user: User = Depends(require_staff),
                   db: Session = Depends(get_db)):
    if not 0 <= index < len(data.CATEGORIES):
        raise HTTPException(404, "Unknown category")
    get_event_or_404(db, event_id)
    c = compute_stats(db, event_id)["categories"][index]
    return render(request, "partials/category_count.html", {"c": c})


@app.get("/events/{event_id}/stats", response_class=HTMLResponse)
def stats(event_id: int, request: Request, user: User = Depends(require_staff),
          db: Session = Depends(get_db)):
    get_event_or_404(db, event_id)
    return render(request, "partials/stats.html", {"stats": compute_stats(db, event_id)})


def card_response(request: Request, attendee: Attendee, trigger: dict | str) -> HTMLResponse:
    header = trigger if isinstance(trigger, str) else json.dumps({"refreshStats": "", **trigger})
    return render(request, "partials/attendee_rows.html",
                  {"attendees": [attendee], "q": "x"}, headers={"HX-Trigger": header})


def unadmit_toast(deleted: int, failed: int) -> dict:
    if failed:
        return toast("Un-admitted and QR link disabled, but the WhatsApp ticket message could not be deleted yet "
                     "- it will be retried automatically.", "error")
    if deleted:
        return toast("Un-admitted: ticket message deleted from WhatsApp and the QR link disabled.")
    return toast("Un-admitted: the QR link was disabled.")


def mark_delivered(db: Session, tickets: list[Ticket]) -> None:
    for ticket in tickets:
        ticket.delivered_at = utcnow()
    db.commit()


async def send_tickets(db: Session, attendee: Attendee, event: Event, tickets: list[Ticket], progress=None) -> list[Ticket]:
    """Send these tickets over WhatsApp one by one (varied wording, best available line with fail-over);
    returns the ones that went out. Raises WhatsAppError only if nothing was sent."""
    sent: list[Ticket] = []
    total = len(attendee.tickets)
    try:
        for number, ticket in enumerate(tickets, start=1):
            if progress:
                progress((number - 1) / len(tickets), f"Sending ticket {number} of {len(tickets)} to {attendee.name}")
            ref, line = await linesmod.deliver_pdf(
                db, attendee.phone, ticket.file_path,
                caption=ticket_caption(attendee.name, event.name, event.event_date,
                                       index=attendee.tickets.index(ticket) + 1, total=total),
                file_name=f"ticket-{ticket.page_number}.pdf", sender=send_whatsapp_pdf,
            )
            if ref:  # remember it: deleted on un-admit, or at a random time after the event
                db.add(SentMessage(event_id=event.id, attendee_id=attendee.id, ticket_id=ticket.id, line_id=line.id,
                                   remote_jid=ref.remote_jid, message_id=ref.message_id,
                                   delete_at=schedule_delete_at(event)))
            ticket.delivered_at = utcnow()
            ticket.send_failed = False
            db.commit()
            sent.append(ticket)
            if progress:
                progress(number / len(tickets), f"Sent ticket {number} of {len(tickets)}")
    except WhatsAppError:
        if not sent:
            raise
        log.warning("WhatsApp send for attendee %s stopped after %s of %s ticket(s)", attendee.id, len(sent), len(tickets))
    return sent


def qr_bundle(db: Session, request: Request, attendee: Attendee, tickets: list[Ticket]) -> tuple[dict | None, bool]:
    """ONE QR code whose link downloads a ZIP with all of the given ticket PDFs (however many there are)."""
    base = public_base_url(request)
    if not tickets:
        return None, is_loopback(base)
    bundle = TicketBundle(attendee_id=attendee.id, ticket_ids=json.dumps([t.id for t in tickets]))
    db.add(bundle)
    db.commit()
    url = f"{base}/tickets/bundle/{bundle.token}/tickets.zip"
    qr = segno.make(url, error="m")
    return {"url": url, "count": len(tickets), "pages": [t.page_number for t in tickets],
            "svg": qr.svg_data_uri(scale=8, border=2, dark="#0f172a", light="#ffffff")}, is_loopback(base)


def oob_card(request: Request, attendee: Attendee, trigger: dict, close_modal: bool = True) -> HTMLResponse:
    """Response for actions made inside the popup: refresh the card behind it, show a toast, close the popup."""
    headers = {"HX-Trigger": json.dumps({"refreshStats": "", **trigger})}
    if close_modal:
        headers["HX-Trigger-After-Swap"] = json.dumps({"closeModal": ""})
    return render(request, "partials/attendee_rows.html", {"attendees": [attendee], "q": "x", "oob": True},
                  headers=headers)


@app.post("/attendees/{attendee_id}/toggle", response_class=HTMLResponse)
async def toggle_check_in(attendee_id: int, request: Request, user: User = Depends(require_staff),
                          db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    note = None
    if attendee.checked_in:           # un-admit (also after a partial entry): this order's messages and QR links go away
        reset_admission(db, attendee)
        rotate_claim_tokens(db, attendee)
        if config.WHATSAPP_DELETE_ON_UNADMIT:
            note = unadmit_toast(*await revoke_attendee_messages(db, attendee))
        else:
            note = unadmit_toast(0, 0)
    elif attendee.held:               # "Admit without sending" lets in only what is not held for a missing shirt
        if attendee.releasable < 1:
            return card_response(request, attendee, toast(HELD_MSG, "error"))
        a, y = split_counts(attendee.releasable, attendee.remaining_adult, attendee.remaining_youth)
        set_admission(attendee, attendee.admitted_adult + a, attendee.admitted_youth + y)
        db.commit()
    else:
        admit_all(db, attendee)
    return card_response(request, attendee, note or "refreshStats")


@app.post("/attendees/{attendee_id}/whatsapp", response_class=HTMLResponse)
async def whatsapp_dispatch(attendee_id: int, request: Request, job: str = Form(""),
                            user: User = Depends(require_staff), db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    event = db.get(Event, attendee.event_id)
    held = attendee.held > 0
    if held and attendee.releasable < 1:
        return card_response(request, attendee, toast(HELD_MSG, "error"))
    targets = hand_out_targets(attendee)
    if held:
        a, y = split_counts(len(targets), attendee.remaining_adult, attendee.remaining_youth)
    else:
        admit_all(db, attendee)
    if not attendee.tickets:
        return card_response(request, attendee, toast("No ticket PDF is attached to this guest.", "error"))
    try:
        sent = await send_tickets(db, attendee, event, targets, Reporter(job))
    except WhatsAppError as exc:
        log.warning("WhatsApp dispatch failed for attendee %s: %s", attendee.id, exc)
        return card_response(request, attendee, toast(f"WhatsApp failed: {exc}", "error"))
    if held:                       # only what was really sent counts as admitted
        pa, py = split_counts(len(sent), a, y)
        set_admission(attendee, attendee.admitted_adult + pa, attendee.admitted_youth + py)
        db.commit()
    n = len(sent)
    if n < len(targets):
        return card_response(request, attendee, toast(f"Only {n} of {len(targets)} tickets were sent - tap again to retry.", "error"))
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
    if attendee.held and attendee.releasable < 1:
        return oob_card(request, attendee, toast(HELD_MSG, "error"))
    targets = hand_out_targets(attendee)
    if attendee.held:
        a, y = split_counts(len(targets), attendee.remaining_adult, attendee.remaining_youth)
        set_admission(attendee, attendee.admitted_adult + a, attendee.admitted_youth + y)
        db.commit()
    else:
        admit_all(db, attendee)
    mark_delivered(db, targets)
    bundle, unreachable = qr_bundle(db, request, attendee, targets)
    return render(request, "partials/qr_modal.html",
                  {"attendee": attendee, "bundle": bundle, "unreachable": unreachable},
                  headers={"HX-Trigger": "refreshStats"})


# ---- resend: give a guest the tickets that were already handed out once more (e.g. the first message never arrived)

def resend_targets(attendee: Attendee) -> list[Ticket]:
    return [t for t in attendee.tickets if t.delivered_at]


@app.get("/attendees/{attendee_id}/resend-form", response_class=HTMLResponse)
def resend_form(attendee_id: int, request: Request, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    return render(request, "partials/resend_form.html", {"attendee": attendee, "count": len(resend_targets(attendee)), "errors": []})


@app.post("/attendees/{attendee_id}/resend", response_class=HTMLResponse)
async def resend_tickets(attendee_id: int, request: Request, via: str = Form("whatsapp"), job: str = Form(""),
                         user: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Send the already-delivered tickets again, without changing who is admitted."""
    attendee = get_attendee_or_404(db, attendee_id)
    event = db.get(Event, attendee.event_id)
    targets = resend_targets(attendee)

    def again(error: str):
        return render(request, "partials/resend_form.html", {"attendee": attendee, "count": len(targets), "errors": [error]})

    if not targets:
        return again("No ticket has been sent to this guest yet.")
    if via == "qr":
        bundle, unreachable = qr_bundle(db, request, attendee, targets)
        return render(request, "partials/qr_modal.html", {"attendee": attendee, "bundle": bundle, "unreachable": unreachable,
                                                          "note": "Tickets that were already handed out"})
    try:
        sent = await send_tickets(db, attendee, event, targets, Reporter(job))
    except WhatsAppError as exc:
        return again(f"WhatsApp failed: {exc}")
    return oob_card(request, attendee, toast(f"Sent {len(sent)} ticket{'s' if len(sent) != 1 else ''} to {attendee.name} again"))


# ---- "shirt missing": the guest gets the payment link; the tickets without a shirt are held until an admin confirms

def shirt_message(attendee: Attendee, count: int) -> str:
    first = (attendee.name.split() or [""])[0]
    return config.SHIRT_MESSAGE.format(name=first, count=count, link=config.SHIRT_PAYMENT_URL)


def shirt_ctx(attendee: Attendee, **extra) -> dict:
    return {"attendee": attendee, "count": attendee.held or 1, "errors": [], "has_link": bool(config.SHIRT_PAYMENT_URL), **extra}


@app.get("/attendees/{attendee_id}/shirt-form", response_class=HTMLResponse)
def shirt_form(attendee_id: int, request: Request, user: User = Depends(require_staff), db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    if attendee.remaining_total < 1:
        return oob_card(request, attendee, toast("All tickets of this guest are already in.", "error"))
    return render(request, "partials/shirt_form.html", shirt_ctx(attendee))


@app.post("/attendees/{attendee_id}/shirt-missing", response_class=HTMLResponse)
async def shirt_missing(attendee_id: int, request: Request, count: str = Form("1"), via: str = Form("whatsapp"),
                        job: str = Form(""), user: User = Depends(require_staff), db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    try:
        n = int(count or 0)
    except ValueError:
        n = 0
    error = None
    if not config.SHIRT_PAYMENT_URL:
        error = "No payment link is set (Settings -> Shirt payment link)."
    elif not 1 <= n <= attendee.remaining_total:
        error = f"Choose between 1 and {attendee.remaining_total} ticket(s)."
    elif via not in ("whatsapp", "qr"):
        error = "Choose WhatsApp or QR."
    if error:
        return render(request, "partials/shirt_form.html", shirt_ctx(attendee, errors=[error], count=max(n, 1)))
    if via == "qr":
        attendee.shirt_hold = n
        db.commit()
        qr = segno.make(config.SHIRT_PAYMENT_URL, error="m")
        return render(request, "partials/shirt_qr.html",
                      {"attendee": attendee, "url": config.SHIRT_PAYMENT_URL, "count": n,
                       "svg": qr.svg_data_uri(scale=8, border=2, dark="#0f172a", light="#ffffff")},
                      headers={"HX-Trigger": "refreshStats"})
    Reporter(job)(0.2, "Sending the payment link")
    try:
        await linesmod.deliver_text(db, attendee.phone, shirt_message(attendee, n), send_whatsapp_text)
    except WhatsAppError as exc:
        log.warning("Shirt link WhatsApp send failed for attendee %s: %s", attendee.id, exc)
        return render(request, "partials/shirt_form.html", shirt_ctx(attendee, errors=[f"WhatsApp failed: {exc}"], count=n))
    attendee.shirt_hold = n
    db.commit()
    return oob_card(request, attendee, toast(f"Payment link sent to {attendee.name}. {n} ticket(s) are held until an admin confirms."))


@app.post("/attendees/{attendee_id}/shirt-release", response_class=HTMLResponse)
def shirt_release(attendee_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """An admin confirms the shirt: the held tickets can now be handed out with the normal buttons."""
    attendee = get_attendee_or_404(db, attendee_id)
    held = attendee.held
    attendee.shirt_hold = 0
    db.commit()
    return card_response(request, attendee, toast(f"Released {held} ticket(s) for {attendee.name} - send them with the normal buttons."))


# ---- partial send: hand out only some of an order's tickets (the rest of the party arrives later)

def partial_ctx(attendee: Attendee, **extra) -> dict:
    undelivered = [t for t in attendee.tickets if not t.delivered_at]
    all_a, all_y = split_counts(attendee.releasable, attendee.remaining_adult, attendee.remaining_youth)
    return {"attendee": attendee, "errors": [], "undelivered": len(undelivered), "all_adult": all_a, "all_youth": all_y,
            "adult": min(1, attendee.remaining_adult) if attendee.remaining_adult else 0,
            "youth": 0 if attendee.remaining_adult else min(1, attendee.remaining_youth), **extra}


@app.get("/attendees/{attendee_id}/partial-form", response_class=HTMLResponse)
def partial_form(attendee_id: int, request: Request, user: User = Depends(require_staff),
                 db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    return render(request, "partials/partial_form.html", partial_ctx(attendee))


@app.post("/attendees/{attendee_id}/partial", response_class=HTMLResponse)
async def partial_send(
    attendee_id: int, request: Request,
    adult: str = Form("0"), youth: str = Form("0"), via: str = Form("whatsapp"), job: str = Form(""),
    user: User = Depends(require_staff), db: Session = Depends(get_db),
):
    attendee = get_attendee_or_404(db, attendee_id)
    event = db.get(Event, attendee.event_id)
    errors: list[str] = []
    try:
        a, y = int(adult or 0), int(youth or 0)
    except ValueError:
        a = y = 0
        errors.append("Enter whole numbers.")
    undelivered = [t for t in attendee.tickets if not t.delivered_at]
    if not errors:
        if a < 0 or y < 0 or a > attendee.remaining_adult or y > attendee.remaining_youth:
            errors.append(f"Only {attendee.remaining_adult} adult and {attendee.remaining_youth} youth ticket(s) are left to send.")
        elif a + y < 1:
            errors.append("Choose at least one ticket.")
        elif a + y > attendee.releasable:
            errors.append(f"Only {attendee.releasable} ticket(s) can be sent now - {attendee.held} are held until an admin "
                          "confirms the shirt.")
        elif a + y > len(undelivered):
            errors.append(f"Only {len(undelivered)} ticket PDF(s) are available for this guest - ask an admin to upload more pages.")
        if via not in ("whatsapp", "qr"):
            errors.append("Choose WhatsApp or QR.")
    if errors:
        return render(request, "partials/partial_form.html", partial_ctx(attendee, errors=errors, adult=a, youth=y))
    targets = undelivered[: a + y]
    first_name = attendee.name

    if via == "qr":
        mark_delivered(db, targets)
        set_admission(attendee, attendee.admitted_adult + a, attendee.admitted_youth + y)
        db.commit()
        bundle, unreachable = qr_bundle(db, request, attendee, targets)
        return render(request, "partials/qr_modal.html",
                      {"attendee": attendee, "bundle": bundle, "unreachable": unreachable,
                       "note": f"{len(targets)} of {attendee.ticket_count} tickets shown - {attendee.admitted_total} admitted so far"},
                      headers={"HX-Trigger": "refreshStats"})

    try:
        sent = await send_tickets(db, attendee, event, targets, Reporter(job))
    except WhatsAppError as exc:
        log.warning("Partial WhatsApp send failed for attendee %s: %s", attendee.id, exc)
        return render(request, "partials/partial_form.html",
                      partial_ctx(attendee, errors=[f"WhatsApp failed: {exc}"], adult=a, youth=y))
    pa, py = split_counts(len(sent), a, y)
    set_admission(attendee, attendee.admitted_adult + pa, attendee.admitted_youth + py)
    db.commit()
    if len(sent) < len(targets):
        msg = toast(f"Only {len(sent)} of {len(targets)} tickets were sent to {first_name} - open Partial send to retry.", "error")
    else:
        left = attendee.ticket_count - attendee.admitted_total
        msg = toast(f"Sent {len(sent)} ticket{'s' if len(sent) != 1 else ''} to {first_name}"
                    + (f" - {left} still to come" if left else " - all tickets sent"))
    return oob_card(request, attendee, msg)


# ---- admin-only editing from the door page

@app.get("/attendees/{attendee_id}/edit-form", response_class=HTMLResponse)
def door_edit_form(attendee_id: int, request: Request, user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    attendee = get_attendee_or_404(db, attendee_id)
    return render(request, "partials/edit_form.html", {"attendee": attendee, "errors": [], "form": None,
                                                       "categories": data.CATEGORIES})


@app.post("/attendees/{attendee_id}/door-edit", response_class=HTMLResponse)
def door_edit(
    attendee_id: int, request: Request,
    name: str = Form(""), phone: str = Form(""), email: str = Form(""), age: str = Form(""),
    category: str = Form(data.DEFAULT_CATEGORY), adult_count: str = Form("1"), youth_count: str = Form("0"),
    notes: str = Form(""),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    attendee = get_attendee_or_404(db, attendee_id)
    event = get_event_or_404(db, attendee.event_id)
    form = dict(name=name, phone=phone, email=email, age=age, category=category,
                adult_count=adult_count, youth_count=youth_count, notes=notes)
    errors = update_attendee(db, attendee, event, **form)
    if errors:
        return render(request, "partials/edit_form.html", {"attendee": attendee, "errors": errors, "form": form,
                                                           "categories": data.CATEGORIES})
    db.refresh(attendee)
    return oob_card(request, attendee, toast(f"Saved {attendee.name} - {attendee.ticket_count} ticket(s)"))


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


@app.get("/tickets/bundle/{token}/tickets.zip")
def download_bundle(token: str, db: Session = Depends(get_db)):
    """What the door QR code opens: all PDFs handed out together, as one ZIP. Public, unguessable token."""
    bundle = db.query(TicketBundle).filter(TicketBundle.token == token).first()
    if bundle is None:
        raise HTTPException(404, "Link not found")
    ids = json.loads(bundle.ticket_ids)
    tickets = {t.id: t for t in db.query(Ticket).filter(Ticket.id.in_(ids), Ticket.attendee_id == bundle.attendee_id)}
    root = config.STORAGE_DIR.resolve()
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:          # PDFs are compressed already
        for tid in ids:
            ticket = tickets.get(tid)
            path = Path(ticket.file_path).resolve() if ticket else None
            if path is None or not path.is_relative_to(root) or not path.is_file():
                continue
            count += 1
            zf.write(path, f"ticket-{count}.pdf")
    if not count:
        raise HTTPException(404, "These tickets are no longer available")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="tickets.zip"', "Cache-Control": "private, no-store"})


@app.get("/tickets/claim/{token}/ticket.pdf")
def open_ticket_pdf(token: str, db: Session = Depends(get_db)):
    """Direct link to the PDF itself (what the QR code encodes); opens in the phone's viewer."""
    return ticket_pdf_response(db, token, "inline")


@app.get("/tickets/claim/{token}/download")
def download_ticket(token: str, db: Session = Depends(get_db)):
    return ticket_pdf_response(db, token, "attachment")


# --------------------------------------------------------------------------- WhatsApp lines (admin only)

TARGET_LINES = 10


def lines_ctx(db: Session, user: User, **extra) -> dict:
    lines = db.query(WhatsAppLine).order_by(WhatsAppLine.priority, WhatsAppLine.id).all()
    today = utcnow().date().isoformat()
    for line in lines:      # counters of a previous day read as 0
        line.today_count = line.sent_today if line.sent_day == today else 0
    info = linesmod.line_summary(db)
    return {"user": user, "lines": lines, "summary": info, "minimum": config.MIN_WORKING_LINES,
            "test_number": linesmod.test_number(db), "target": TARGET_LINES, "messages": [], "errors": [], "strays": [], **extra}


def get_line_or_404(db: Session, line_id: int) -> WhatsAppLine:
    line = db.get(WhatsAppLine, line_id)
    if line is None:
        raise HTTPException(404, "Line not found")
    return line


def next_instance_name(db: Session) -> str:
    existing = {n for (n,) in db.query(WhatsAppLine.instance_name)}
    i = len(existing) + 1
    while f"{config.EVOLUTION_INSTANCE}-{i}" in existing:
        i += 1
    return f"{config.EVOLUTION_INSTANCE}-{i}"


async def create_line(db: Session, label: str, cap: int, phone: str = "") -> WhatsAppLine:
    """Create the Evolution instance and the line record (status 'pending' until the phone is linked)."""
    name = next_instance_name(db)
    await evolution_create_instance(name)
    last = db.query(func.max(WhatsAppLine.priority)).scalar() or 0
    line = WhatsAppLine(label=label, instance_name=name, phone=normalize_phone(phone) if phone else None,
                        priority=last + 1, status="pending", daily_cap=cap)
    db.add(line)
    db.commit()
    return line


LINE_LOCK = asyncio.Lock()


async def stray_instances(db: Session) -> list[dict]:
    """Instances that exist in Evolution but belong to no line here (leftovers, double clicks)."""
    known = {n for (n,) in db.query(WhatsAppLine.instance_name)}
    return [i for i in await evolution_list_instances() if i["name"] not in known]


background_tasks: set = set()                       # keeps references so running jobs are not garbage-collected


@app.post("/webhooks/evolution", include_in_schema=False)
async def evolution_webhook(request: Request, token: str = "", db: Session = Depends(get_db)):
    """Delivery reports (messages.update) pushed by Evolution: tells us when WhatsApp refused a message that Evolution had
    accepted. Only Evolution (inside the Docker network) knows the token."""
    if not token or not secrets.compare_digest(token.encode(), config.EVOLUTION_WEBHOOK_TOKEN.encode()):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    event_name = str(body.get("event", "")).lower().replace("_", ".")
    if event_name == "messages.upsert":              # a received message: may be a ticket PDF from a configured number
        background_tasks.add(asyncio.ensure_future(inboxmod.process_webhook(body)))
        return {"ok": True, "queued": True}
    if event_name != "messages.update":
        return {"ok": True, "ignored": True}
    items = body.get("data")
    results = []
    for item in items if isinstance(items, list) else [items]:
        if not isinstance(item, dict) or item.get("fromMe") is False:
            continue
        results.append(linesmod.apply_delivery_update(db, str(body.get("instance") or body.get("instanceName") or ""),
                                                      str(item.get("keyId") or ""), str(item.get("status") or "")))
    return {"ok": True, "results": results}


# --------------------------------------------------------------------------- ticket inbox (PDFs received on WhatsApp)

def inbox_ctx(db: Session, user: User, **extra) -> dict:
    sources = db.query(TicketSource).order_by(TicketSource.id).all()
    entries = db.query(InboxEntry).order_by(InboxEntry.id.desc()).limit(80).all()
    events = db.query(Event).order_by(Event.event_date.desc()).limit(60).all()
    names = {e.id: e.name for e in events}
    known = {s.phone for s in sources}
    return {"user": user, "sources": sources, "entries": entries, "events": events, "event_names": names, "known": known,
            "messages": [], "errors": [], **extra}


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "inbox.html", inbox_ctx(db, user))


def parse_source_event(db: Session, value: str) -> int | None:
    return int(value) if value.strip().isdigit() and db.get(Event, int(value)) else None


@app.post("/inbox/sources", response_class=HTMLResponse)
def add_ticket_source(request: Request, label: str = Form(""), phone: str = Form(""), event_id: str = Form(""),
                      user: User = Depends(require_admin), db: Session = Depends(get_db)):
    digits = re.sub(r"\D", "", normalize_phone(phone)) if phone.strip() else ""
    if not 7 <= len(digits) <= 20:
        return render(request, "inbox.html", inbox_ctx(db, user, errors=["Enter the WhatsApp number the tickets come from."]), status=422)
    if db.query(TicketSource).filter(TicketSource.phone == digits).first():
        return render(request, "inbox.html", inbox_ctx(db, user, errors=["That number is already on the list."]), status=422)
    label = " ".join(label.split())[:64] or f"+{digits}"
    db.add(TicketSource(label=label, phone=digits, event_id=parse_source_event(db, event_id)))
    db.commit()
    return render(request, "inbox.html", inbox_ctx(db, user, messages=[f"{label} can now send tickets by WhatsApp."]))


@app.post("/inbox/allow", response_class=HTMLResponse)
def allow_inbox_sender(request: Request, sender: str = Form(""), label: str = Form(""),
                       user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """One click from the list of received files: use this sender's PDFs as tickets from now on."""
    digits = re.sub(r"\D", "", sender)
    if not 7 <= len(digits) <= 20 or db.query(TicketSource).filter(TicketSource.phone == digits).first():
        return render(request, "inbox.html", inbox_ctx(db, user, errors=["That sender is already allowed or invalid."]), status=422)
    label = " ".join(label.split())[:64] or f"+{digits}"
    db.add(TicketSource(label=label, phone=digits))
    db.commit()
    return render(request, "inbox.html", inbox_ctx(db, user, messages=[
        f"{label} is allowed. Send the PDF again - files that already arrived are not processed retroactively."]))


@app.post("/inbox/sources/{source_id}/update", response_class=HTMLResponse)
def update_ticket_source(source_id: int, request: Request, event_id: str = Form(""), active: str = Form(""),
                         user: User = Depends(require_admin), db: Session = Depends(get_db)):
    source = db.get(TicketSource, source_id)
    if source is None:
        raise HTTPException(404, "Number not found")
    source.event_id = parse_source_event(db, event_id)
    source.active = active == "on"
    db.commit()
    return render(request, "inbox.html", inbox_ctx(db, user, messages=[f"Saved {source.label}."]))


@app.post("/inbox/sources/{source_id}/delete", response_class=HTMLResponse)
def delete_ticket_source(source_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    source = db.get(TicketSource, source_id)
    if source is not None:
        db.delete(source)
        db.commit()
    return render(request, "inbox.html", inbox_ctx(db, user, messages=["Number removed."]))


@app.post("/inbox/clear", response_class=HTMLResponse)
def clear_inbox_log(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    db.query(InboxEntry).delete()
    db.commit()
    return render(request, "inbox.html", inbox_ctx(db, user, messages=["The list was cleared."]))


@app.get("/lines", response_class=HTMLResponse)
async def lines_page(request: Request, added: int = 0, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    messages = []
    line = db.get(WhatsAppLine, added) if added else None
    if line is not None:
        messages.append(f"Added “{line.label}”. Press Connect and scan its QR code with the phone that holds this number.")
    return render(request, "lines.html", lines_ctx(db, user, messages=messages, strays=await stray_instances(db)))


@app.post("/lines/stray-delete", response_class=HTMLResponse)
async def delete_stray_instance(request: Request, name: str = Form(""), user: User = Depends(require_admin),
                                db: Session = Depends(get_db)):
    if db.query(WhatsAppLine).filter(WhatsAppLine.instance_name == name).first():
        return render(request, "lines.html", lines_ctx(db, user, errors=["That instance belongs to a line - remove the line instead."]), status=422)
    try:
        await evolution_delete_instance(name)
    except WhatsAppError as exc:
        return render(request, "lines.html", lines_ctx(db, user, errors=[f"Could not remove {name}: {exc}"], strays=await stray_instances(db)), status=422)
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"Removed the stray instance {name}."], strays=await stray_instances(db)))


@app.post("/lines", response_class=HTMLResponse)
async def add_line(request: Request, label: str = Form(""), daily_cap: str = Form(""), phone: str = Form(""),
                   user: User = Depends(require_admin), db: Session = Depends(get_db)):
    label = " ".join(label.split())[:64] or f"Line {db.query(WhatsAppLine).count() + 1}"
    try:
        cap = int(daily_cap) if daily_cap.strip() else config.LINE_DAILY_CAP
        if not 1 <= cap <= 5000:
            raise ValueError
    except ValueError:
        return render(request, "lines.html", lines_ctx(db, user, errors=["Daily limit must be a number between 1 and 5000."]), status=422)
    async with LINE_LOCK:       # one creation at a time: a double click must not make two instances
        recent = db.query(WhatsAppLine).filter(WhatsAppLine.created_at > utcnow() - timedelta(seconds=15),
                                               WhatsAppLine.status == "pending").first()
        if recent is not None:
            return RedirectResponse(f"/lines?added={recent.id}", status_code=303)
        if phone.strip() and db.query(WhatsAppLine).filter(WhatsAppLine.phone == normalize_phone(phone)).first():
            return render(request, "lines.html", lines_ctx(db, user, errors=["A line with that phone number already exists."]), status=422)
        try:
            line = await create_line(db, label, cap, phone)
        except WhatsAppError as exc:
            return render(request, "lines.html", lines_ctx(db, user, errors=[f"Could not create the line: {exc}"]), status=422)
    return RedirectResponse(f"/lines?added={line.id}", status_code=303)       # redirect: a page refresh cannot create another


@app.post("/lines/bulk", response_class=HTMLResponse)
async def add_lines_bulk(request: Request, job: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Top up to TARGET_LINES (10) lines so there are always spares ready; each still needs its QR scan."""
    made, errors = 0, []
    report = Reporter(job)
    async with LINE_LOCK:
        while db.query(WhatsAppLine).count() < TARGET_LINES:
            report(db.query(WhatsAppLine).count() / TARGET_LINES, f"Creating line {db.query(WhatsAppLine).count() + 1} of {TARGET_LINES}")
            try:
                await create_line(db, f"Line {db.query(WhatsAppLine).count() + 1}", config.LINE_DAILY_CAP)
                made += 1
            except WhatsAppError as exc:
                errors.append(f"Stopped after {made} new line(s): {exc}")
                break
    msgs = [f"Created {made} line(s). Connect each one by scanning its QR code."] if made else ["There are already {0} lines.".format(TARGET_LINES)]
    return render(request, "lines.html", lines_ctx(db, user, messages=msgs, errors=errors), status=422 if errors else 200)


@app.post("/lines/settings", response_class=HTMLResponse)
def lines_settings(request: Request, test_number: str = Form(""), user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    digits = normalize_phone(test_number) if test_number.strip() else ""
    if test_number.strip() and not 8 <= len(digits) <= 15:
        return render(request, "lines.html", lines_ctx(db, user, errors=["That test number does not look valid."]), status=422)
    linesmod.set_setting(db, "test_number", digits)
    return render(request, "lines.html", lines_ctx(db, user, messages=["Test number saved." if digits else "Test number cleared."]))


@app.post("/lines/test-all", response_class=HTMLResponse)
async def test_all_lines(request: Request, job: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = await linesmod.run_line_tests(db, progress=Reporter(job))
    return render(request, "lines.html", lines_ctx(db, user, messages=[
        f"Tested all lines: {result['ok']} working, {result['bad']} not working."]))


@app.post("/lines/{line_id}/test", response_class=HTMLResponse)
async def test_one_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    ok = await linesmod.test_one(db, line)
    msg = f"{line.label}: working." if ok else f"{line.label}: {line.last_error}"
    return render(request, "lines.html", lines_ctx(db, user, **({"messages": [msg]} if ok else {"errors": [msg]})))


@app.post("/lines/{line_id}/diagnose", response_class=HTMLResponse)
async def diagnose_one_line(line_id: int, request: Request, target: str = Form(""),
                            user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Run the step-by-step check and show every raw answer from Evolution."""
    line = get_line_or_404(db, line_id)
    digits = normalize_phone(target) if target.strip() else None
    steps = await linesmod.diagnose_line(db, line, digits)
    return render(request, "lines.html", lines_ctx(db, user, diagnosis={"line": line.label, "steps": steps}))


@app.post("/lines/{line_id}/refresh", response_class=HTMLResponse)
async def refresh_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    try:
        line.connection = await evolution_state(line.instance_name)
        if line.connection == "open" and line.status == "pending":
            line.status = "active"
        db.commit()
        if line.connection == "open":
            await linesmod.on_connected(db, line, phone_fn=evolution_owner_number, profile_fn=evolution_set_profile)
    except WhatsAppError as exc:
        return render(request, "lines.html", lines_ctx(db, user, errors=[f"{line.label}: {exc}"]), status=422)
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"{line.label}: connection is “{line.connection}”."]))


@app.post("/lines/{line_id}/connect", response_class=HTMLResponse)
async def connect_line(line_id: int, request: Request, number: str = Form(""), poll: str = Form(""),
                       user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Popup content: QR code (refreshed every few seconds) or pairing code; closes itself once linked."""
    line = get_line_or_404(db, line_id)
    ctx = {"line": line, "connected": False, "qr": "", "pairing_code": "", "error": ""}
    try:
        state = await evolution_state(line.instance_name)
        if state == "missing":
            await evolution_create_instance(line.instance_name)
            state = "connecting"
        line.connection = state
        if state == "open":
            if line.status == "pending":
                line.status = "active"
            db.commit()
            await linesmod.on_connected(db, line, phone_fn=evolution_owner_number, profile_fn=evolution_set_profile)
            return render(request, "partials/line_qr.html", {**ctx, "connected": True},
                          headers={"HX-Trigger": json.dumps({"lineConnected": ""})})
        if poll and state != "connecting":
            # The page's automatic refresh must not ask Evolution to connect while the session is closed or restarting
            # (WhatsApp restarts it right after the QR scan): that would start a SECOND socket for the same login,
            # and the two kick each other out forever ("conflict - replaced").
            db.commit()
            ctx["error"] = (f"Linking… WhatsApp is finishing the connection (state: {state}). If nothing changes within a minute, "
                            "press Connect / QR again.")
            return render(request, "partials/line_qr.html", ctx)
        info = await evolution_connect(line.instance_name, number or line.phone)
        db.commit()
        ctx.update(qr=info["qr"], pairing_code=info["pairing_code"])
        if not info["qr"] and not info["pairing_code"]:
            ctx["error"] = "WhatsApp has not produced a QR code yet - it is retried automatically."
    except WhatsAppError as exc:
        ctx["error"] = str(exc)
    return render(request, "partials/line_qr.html", ctx)


@app.post("/lines/{line_id}/reset-session", response_class=HTMLResponse)
async def reset_line_session(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Start the line's WhatsApp session from scratch (delete + recreate the Evolution instance); the phone is linked again
    with a new QR code. Use it when a session keeps looping with 'conflict - replaced'."""
    line = get_line_or_404(db, line_id)
    try:
        await evolution_delete_instance(line.instance_name)
        await asyncio.sleep(1.5)
        await evolution_create_instance(line.instance_name)
    except WhatsAppError as exc:
        return render(request, "lines.html", lines_ctx(db, user, errors=[f"Could not reset {line.label}: {exc}"]), status=422)
    line.status, line.connection, line.profile_set, line.last_error, line.failed_at = "pending", "unknown", False, None, None
    db.commit()
    return render(request, "lines.html", lines_ctx(db, user, messages=[
        f"{line.label}: session reset. Press Connect / QR and scan the code ONCE with the phone."]))


@app.post("/lines/profile-all", response_class=HTMLResponse)
async def profile_all_lines(request: Request, job: str = Form(""), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """(Re)apply the profile name and picture to every connected line, with pauses in between."""
    done = failed = 0
    report = Reporter(job)
    todo = list(db.query(WhatsAppLine).filter(WhatsAppLine.status != "disabled").order_by(WhatsAppLine.id))
    for i, line in enumerate(todo):
        report(i / max(1, len(todo)), f"Updating the profile of {line.label} ({i + 1} of {len(todo)})")
        try:
            state = await evolution_state(line.instance_name)
        except WhatsAppError:
            continue
        line.connection = state
        if state != "open":
            continue
        if i:
            await asyncio.sleep(random.uniform(3.0, 10.0))
        if await linesmod.apply_profile(db, line, profile_fn=evolution_set_profile):
            done += 1
        else:
            failed += 1
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"Profile applied to {done} line(s)."],
                                                    errors=[f"{failed} line(s) could not be updated - see the line's details."] if failed else []))


@app.post("/lines/{line_id}/profile", response_class=HTMLResponse)
async def profile_one_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    try:
        line.connection = await evolution_state(line.instance_name)
    except WhatsAppError as exc:
        return render(request, "lines.html", lines_ctx(db, user, errors=[f"{line.label}: {exc}"]), status=422)
    if line.connection != "open":
        db.commit()
        return render(request, "lines.html", lines_ctx(db, user, errors=[f"{line.label} is not connected - scan its QR code first."]), status=422)
    if await linesmod.apply_profile(db, line, profile_fn=evolution_set_profile):
        return render(request, "lines.html", lines_ctx(db, user, messages=[f"{line.label}: profile set to “{config.WHATSAPP_PROFILE_NAME}” with the logo."]))
    return render(request, "lines.html", lines_ctx(db, user, errors=[f"{line.label}: {line.profile_error}"]), status=422)


@app.post("/lines/{line_id}/toggle", response_class=HTMLResponse)
def toggle_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    if line.status == "disabled":
        line.status = "active" if line.connection == "open" else "pending"
        msg = f"{line.label} enabled."
    else:
        line.status, msg = "disabled", f"{line.label} disabled - it will not be used."
    db.commit()
    return render(request, "lines.html", lines_ctx(db, user, messages=[msg]))


@app.post("/lines/{line_id}/reset", response_class=HTMLResponse)
def reset_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    line.status, line.last_error, line.failed_at = "active", None, None
    db.commit()
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"{line.label} marked as working again."]))


@app.post("/lines/{line_id}/primary", response_class=HTMLResponse)
def make_primary(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    first = db.query(func.min(WhatsAppLine.priority)).scalar() or 1
    line.priority = first - 1
    db.commit()
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"{line.label} is now the first line used."]))


@app.post("/lines/{line_id}/delete", response_class=HTMLResponse)
async def delete_line(line_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    line = get_line_or_404(db, line_id)
    label, errors = line.label, []
    try:
        await evolution_delete_instance(line.instance_name)
    except WhatsAppError as exc:
        errors.append(f"The instance could not be removed from Evolution ({exc}); the line was removed here anyway.")
    db.delete(line)
    db.commit()
    return render(request, "lines.html", lines_ctx(db, user, messages=[f"Removed {label}."], errors=errors))


# --------------------------------------------------------------------------- members phone book (admin only)

def members_ctx(db: Session, user: User, **extra) -> dict:
    members = db.query(Member).order_by(func.lower(Member.name)).all()
    return {"user": user, "members": members, "override": linesmod.get_setting(db, "members_override_phone", "") == "1",
            "messages": [], "errors": [], **extra}


def clean_member(name: str, phone: str) -> tuple[str, str, list[str]]:
    name = " ".join((name or "").split())[:255]
    digits = normalize_phone(phone) if (phone or "").strip() else ""
    errors = []
    if not name:
        errors.append("name is missing")
    if not 8 <= len(digits) <= 15:
        errors.append("phone number is missing or not valid")
    return name, digits, errors


@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "members.html", members_ctx(db, user))


@app.post("/members", response_class=HTMLResponse)
def add_member(request: Request, name: str = Form(""), phone: str = Form(""), notes: str = Form(""),
               user: User = Depends(require_admin), db: Session = Depends(get_db)):
    name, digits, errors = clean_member(name, phone)
    key = name_keys(name)[1]
    if not errors and db.query(Member).filter(Member.name_key == key).first():
        errors.append(f"{name} is already in the list - edit that entry instead")
    if errors:
        return render(request, "members.html", members_ctx(db, user, errors=[f"{e}." for e in errors]), status=422)
    db.add(Member(name=name, name_key=key, phone=digits, notes=notes.strip()[:500] or None))
    db.commit()
    return render(request, "members.html", members_ctx(db, user, messages=[f"Added {name}."]))


@app.post("/members/import", response_class=HTMLResponse)
async def import_members(request: Request, file: UploadFile = File(None), text: str = Form(""),
                         user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Bulk add / update: a file (.xlsx .csv .json .txt) or pasted lines. Existing names get the new phone number."""
    try:
        if file is not None and file.filename:
            content = await file.read(config.MAX_LIST_BYTES + 1)
            if len(content) > config.MAX_LIST_BYTES:
                raise ImportValidationError(["File is too large (5 MB max)"])
            pairs = parse_member_file(file.filename, content)
        else:
            pairs = parse_member_text(text)
    except ImportValidationError as exc:
        return render(request, "members.html", members_ctx(db, user, errors=exc.errors), status=422)
    if not pairs:
        return render(request, "members.html", members_ctx(db, user, errors=["Nothing to import - paste lines like “name, phone” or upload a file."]), status=422)
    existing = {m.name_key: m for m in db.query(Member)}
    added = updated = unchanged = 0
    errors, seen = [], {}
    for i, (raw_name, raw_phone) in enumerate(pairs, start=1):
        name, digits, problems = clean_member(raw_name, raw_phone)
        if problems:
            errors.append(f"Line {i}: {', '.join(problems)}" + (f" ({name})" if name else ""))
            continue
        key = name_keys(name)[1]
        if seen.get(key, digits) != digits:
            errors.append(f"Line {i}: {name} appears with two different phone numbers - skipped")
            continue
        seen[key] = digits
        member = existing.get(key)
        if member is None:
            member = Member(name=name, name_key=key, phone=digits)
            db.add(member)
            existing[key] = member
            added += 1
        elif member.phone != digits:
            member.phone, updated = digits, updated + 1
        else:
            unchanged += 1
    db.commit()
    msgs = [f"Added {added}, updated {updated}, {unchanged} already up to date."]
    return render(request, "members.html", members_ctx(db, user, messages=msgs, errors=errors[:30]), status=200)


@app.post("/members/settings", response_class=HTMLResponse)
def members_settings(request: Request, override: str = Form(""), user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    linesmod.set_setting(db, "members_override_phone", "1" if override == "on" else "")
    return render(request, "members.html", members_ctx(db, user, messages=["Setting saved."]))


@app.post("/members/{member_id}/edit", response_class=HTMLResponse)
def edit_member(member_id: int, request: Request, name: str = Form(""), phone: str = Form(""), notes: str = Form(""),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(404, "Member not found")
    name, digits, errors = clean_member(name, phone)
    key = name_keys(name)[1]
    clash = db.query(Member).filter(Member.name_key == key, Member.id != member.id).first()
    if clash:
        errors.append(f"{name} is already in the list")
    if errors:
        return render(request, "members.html", members_ctx(db, user, errors=[f"{e}." for e in errors]), status=422)
    member.name, member.name_key, member.phone, member.notes = name, key, digits, notes.strip()[:500] or None
    db.commit()
    return render(request, "members.html", members_ctx(db, user, messages=[f"Saved {name}."]))


@app.post("/members/{member_id}/delete", response_class=HTMLResponse)
def delete_member(member_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(404, "Member not found")
    name = member.name
    db.delete(member)
    db.commit()
    return render(request, "members.html", members_ctx(db, user, messages=[f"Removed {name}."]))


# ---- "Send to Gate" bookmarklet: the registration page posts its table to a popup of this site

class ReceivedRow(BaseModel):
    c: str = Field("", max_length=300)
    t: list[str] = Field(default_factory=list, max_length=60)


class ReceivedTable(BaseModel):
    event_id: int
    game_id: str = Field("", max_length=32)
    rows: list[ReceivedRow] = Field(max_length=5000)


def bookmarklet_source(origin: str) -> str:
    """javascript: URL to drag to the bookmarks bar. Runs on the registration site's reservations page, collects the
    visible text of the table and hands it to the receiver popup of this site (checked by origin on both sides)."""
    js = (
        "(function(){var O='%s';var ts=[].slice.call(document.querySelectorAll('table'));"
        "var t=document.querySelector('table[id*=ticketReservations]')||ts.sort(function(a,b){return b.rows.length-a.rows.length})[0];"
        "if(!t){alert('No reservations table found on this page.');return;}"
        "var rows=[].slice.call(t.rows).map(function(r){return{c:r.className,t:[].slice.call(r.cells).map(function(c){"
        "var k=c.cloneNode(true);[].slice.call(k.querySelectorAll('.hide,script,style')).forEach(function(e){e.remove()});"
        "return(k.textContent||'').replace(/\\s+/g,' ').trim()})}});"
        "var g=(new URLSearchParams(location.search)).get('gameId')||'';"
        "window.open(O+'/registration/receiver','gate_receiver','width=560,height=740');"
        "window.addEventListener('message',function(e){if(e.origin!==O||!e.data||!e.data.gateReady)return;"
        "e.source.postMessage({type:'gate-table',rows:rows,gameId:g},O)});})();"
    ) % origin
    return "javascript:" + js


@app.get("/registration/receiver", response_class=HTMLResponse)
def registration_receiver(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    events = db.query(Event).order_by(Event.event_date.desc()).all()
    return render(request, "registration_receiver.html", {
        "user": user, "events": events, "reg_origin": config.REGISTRATION_BASE_URL})


@app.post("/registration/receive")
def registration_receive(body: ReceivedTable, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    event = db.get(Event, body.event_id)
    if event is None:
        return JSONResponse({"ok": False, "errors": ["Choose an event."]}, status_code=404)
    try:
        grid = [GridRow(r.c, [" ".join(x.split()) for x in r.t]) for r in body.rows]
        parsed = parse_registration_grid(grid, member_book(db))
    except RegistrationError as exc:
        return JSONResponse({"ok": False, "errors": [str(exc)]}, status_code=422)
    if re.fullmatch(r"\d{1,9}", body.game_id or "") and not event.external_id:
        event.external_id = body.game_id          # remember which registration game this event is
        db.commit()
    msgs, warnings = run_import(db, event, parsed, source=f"Registration #{body.game_id}: " if body.game_id else "")
    return {"ok": True, "messages": msgs, "warnings": warnings, "manage_url": f"/events/{event.id}/manage"}


# --------------------------------------------------------------------------- site settings (admin only)

def settings_ctx(db: Session, user: User, **extra) -> dict:
    return {"user": user, "groups": site_settings.groups(), "value": site_settings.current, "titles": site_settings.GROUPS,
            "preview": site_settings.preview_messages(), "messages": [], "errors": [], **extra}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "settings.html", settings_ctx(db, user))


@app.get("/settings/profile-picture")
def profile_picture(user: User = Depends(require_admin)):
    path = linesmod.profile_picture_path()
    return FileResponse(path, media_type="image/png" if path.read_bytes()[:4] == b"\x89PNG" else "image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.post("/settings/profile-picture", response_class=HTMLResponse)
async def upload_profile_picture(request: Request, file: UploadFile = File(None), reset: str = Form(""),
                                 user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = config.STORAGE_DIR / "profile-picture"
    if reset:
        target.unlink(missing_ok=True)
        return render(request, "settings.html", settings_ctx(db, user, messages=["The built-in logo is used again."]))
    data_ = await file.read(2 * 1024 * 1024 + 1) if file is not None and file.filename else b""
    if not data_ or len(data_) > 2 * 1024 * 1024 or not (data_[:3] == b"\xff\xd8\xff" or data_[:4] == b"\x89PNG"):
        return render(request, "settings.html", settings_ctx(db, user, errors=["Upload a JPEG or PNG picture (up to 2 MB)."]), status=422)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data_)
    return render(request, "settings.html", settings_ctx(db, user, messages=[
        "Profile picture saved. It is used for lines connected from now on; press “Apply profile to all connected lines” on the Lines page to update the others."]))


@app.post("/settings/{group}", response_class=HTMLResponse)
async def save_settings(group: str, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if group not in site_settings.GROUPS:
        raise HTTPException(404, "Unknown settings group")
    form = dict((await request.form()).items())
    errors = site_settings.save_group(db, group, form)
    if errors:
        return render(request, "settings.html", settings_ctx(db, user, errors=errors, open_group=group), status=422)
    return render(request, "settings.html", settings_ctx(db, user, messages=[f"{site_settings.GROUPS[group]}: saved."], open_group=group))


@app.post("/settings/{group}/reset", response_class=HTMLResponse)
def reset_settings(group: str, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if group not in site_settings.GROUPS:
        raise HTTPException(404, "Unknown settings group")
    site_settings.reset_group(db, group)
    return render(request, "settings.html", settings_ctx(db, user, messages=[f"{site_settings.GROUPS[group]}: back to the defaults."], open_group=group))


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
    email: str = Form(...), full_name: str = Form(...), password: str = Form(""),
    role: str = Form("doorman"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    email = email.strip().lower()
    errors = []
    if "@" not in email or " " in email or len(email) > 255:
        errors.append("Enter a valid email address.")
    if not full_name.strip():
        errors.append("Full name is required.")
    if password and len(password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    if not password and not google_auth.enabled():
        errors.append("Set a password (Google sign-in is not configured).")
    if role not in Role.__members__:
        errors.append("Invalid role.")
    if not errors and db.query(User).filter(User.email == email).first():
        errors.append("A user with that email already exists.")
    if errors:
        return render(request, "users.html", users_ctx(db, user, errors=errors), status=422)
    db.add(User(email=email, full_name=full_name.strip()[:255],
                hashed_password=hash_password(password or secrets.token_urlsafe(32)), role=Role[role]))
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
