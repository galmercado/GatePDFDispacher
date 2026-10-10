"""WhatsApp lines: pick the sending number, fail over when one misbehaves, health-test them before events."""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from . import config
from .database import SessionLocal
from .messages import line_test_text
from .models import AppSetting, Event, WhatsAppLine, utcnow
from .services import SentRef, WhatsAppError, evolution_state, send_whatsapp_text

log = logging.getLogger("gate.lines")

Sender = Callable[..., Awaitable[SentRef | None]]


# --------------------------------------------------------------------------- settings

def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def test_number(db: Session) -> str:
    """The predefined number that receives the pre-event line tests (Lines page setting, else env)."""
    return get_setting(db, "test_number", config.WHATSAPP_TEST_NUMBER)


# --------------------------------------------------------------------------- selection

def _today() -> str:
    return utcnow().date().isoformat()


def roll_day(line: WhatsAppLine) -> None:
    if line.sent_day != _today():
        line.sent_day, line.sent_today = _today(), 0


def ensure_default_line(db: Session) -> None:
    """First start: the single instance configured in .env becomes line #1 ('Main')."""
    if db.query(WhatsAppLine.id).first() is None:
        db.add(WhatsAppLine(label="Main", instance_name=config.EVOLUTION_INSTANCE, priority=1, status="active",
                            daily_cap=config.LINE_DAILY_CAP))
        db.commit()


def usable_lines(db: Session) -> list[WhatsAppLine]:
    """Active lines that still have room today, best (lowest priority number) first."""
    out = []
    for line in db.query(WhatsAppLine).filter(WhatsAppLine.status == "active").order_by(
            WhatsAppLine.priority, WhatsAppLine.id).all():
        roll_day(line)
        if line.sent_today < line.daily_cap:
            out.append(line)
    return out


def line_summary(db: Session) -> dict:
    total = db.query(WhatsAppLine).count()
    working = db.query(WhatsAppLine).filter(WhatsAppLine.status == "active").count()
    return {"total": total, "working": working, "low": working < config.MIN_WORKING_LINES}


def mark_failed(db: Session, line: WhatsAppLine, error: Exception) -> None:
    line.status, line.failed_at, line.last_error = "failed", utcnow(), str(error)[:300]
    db.commit()
    log.warning("WhatsApp line %s (%s) marked as not functioning: %s", line.label, line.instance_name, error)


async def deliver_pdf(db: Session, phone: str, path: str, caption: str, file_name: str,
                      sender: Sender) -> tuple[SentRef | None, WhatsAppLine]:
    """Send one ticket PDF through the best available line, failing over to the next one.

    A line is only declared *not functioning* when it errored (HTTP 5xx / dropped session) and another line then
    delivered the very same message - that proves the problem was the number, not the recipient. If every line
    errors, or the error is a 4xx (bad recipient / payload), nothing is marked.
    """
    lines = usable_lines(db)
    if not lines:
        raise WhatsAppError("No working WhatsApp line is available (all failed, disabled or at their daily limit) "
                            "- see the Lines page")
    suspects: list[tuple[WhatsAppLine, WhatsAppError]] = []
    last: WhatsAppError | None = None
    for line in lines:
        try:
            ref = await sender(phone, path, caption, file_name=file_name, instance=line.instance_name)
        except WhatsAppError as exc:
            if not exc.line_problem:
                raise                       # another number would not help
            suspects.append((line, exc))
            last = exc
            continue
        line.sent_today += 1
        line.last_used_at = utcnow()
        db.commit()
        for bad, exc in suspects:
            mark_failed(db, bad, exc)
        return ref, line
    raise WhatsAppError(f"All {len(lines)} WhatsApp line(s) failed. Last error: {last}",
                        status_code=last.status_code if last else None)


# --------------------------------------------------------------------------- health tests

async def test_line(db: Session, line: WhatsAppLine, number: str | None = None,
                    state_fn=evolution_state, text_fn=send_whatsapp_text) -> bool:
    """Is the session connected, and (if a test number is configured) can it really send a message?"""
    number = test_number(db) if number is None else number
    error = ""
    try:
        line.connection = await state_fn(line.instance_name)
    except WhatsAppError as exc:
        line.connection, error = "unknown", f"Could not check the connection: {exc}"
    if not error and line.connection != "open":
        error = f"Not connected (state: {line.connection})"
    if not error and number:
        try:
            await text_fn(number, line_test_text(line.label), instance=line.instance_name)
        except WhatsAppError as exc:
            error = f"Test message failed: {exc}"
    ok = not error
    line.last_test_at, line.last_test_ok = utcnow(), ok
    line.last_error = error[:300] or None
    if line.status != "disabled":
        if ok:
            line.status, line.failed_at = "active", None
        elif line.status in ("active", "failed"):
            line.status = "failed"
            line.failed_at = line.failed_at or utcnow()
    db.commit()
    return ok


async def run_line_tests(db: Session, **kw) -> dict:
    """Test every line that is not disabled, one by one with random pauses. Returns {'ok': n, 'bad': n}."""
    ok = bad = 0
    lines = db.query(WhatsAppLine).filter(WhatsAppLine.status != "disabled").order_by(
        WhatsAppLine.priority, WhatsAppLine.id).all()
    for i, line in enumerate(lines):
        if i:
            await asyncio.sleep(random.uniform(*config.LINE_TEST_GAP))
        if await test_line(db, line, **kw):
            ok += 1
        else:
            bad += 1
    return {"ok": ok, "bad": bad}


def event_start_utc(event: Event) -> datetime:
    local = event.event_date.replace(tzinfo=ZoneInfo(config.EVENT_TIMEZONE))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


async def pretest_due_events(now: datetime | None = None, **kw) -> int:
    """Run the line test once for every event that starts within LINE_TEST_HOURS_BEFORE hours. Returns tests run."""
    now = now or utcnow()
    ran = 0
    with SessionLocal() as db:
        for event in db.query(Event).filter(Event.lines_tested_at.is_(None)).all():
            start = event_start_utc(event)
            if start - timedelta(hours=config.LINE_TEST_HOURS_BEFORE) <= now < start:
                result = await run_line_tests(db, **kw)
                event.lines_tested_at = utcnow()
                db.commit()
                log.info("Pre-event line test for %s: %s", event.name, result)
                ran += 1
    return ran


async def pretest_loop() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await pretest_due_events()
        except Exception:
            log.exception("Pre-event line test failed")
        await asyncio.sleep(600)
