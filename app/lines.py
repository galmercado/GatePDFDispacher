"""WhatsApp lines: pick the sending number, fail over when one misbehaves, health-test them before events."""
import asyncio
import base64
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from . import config
from .database import SessionLocal
from .messages import line_chat_text, line_test_text
from .models import AppSetting, Event, WhatsAppLine, utcnow
from .services import (
    SentRef, WhatsAppError, evolution_owner_number, evolution_set_profile, evolution_state, send_whatsapp_text,
)

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


# --------------------------------------------------------------------------- WhatsApp profile of a line

DEFAULT_PICTURE = Path(__file__).parent / "assets" / "profile.jpg"


def profile_picture_path() -> Path:
    """The picture given to every line: the one uploaded on the Settings page, else the built-in logo."""
    custom = config.STORAGE_DIR / "profile-picture"
    return custom if custom.is_file() else DEFAULT_PICTURE


def profile_picture_b64() -> str | None:
    try:
        return base64.b64encode(profile_picture_path().read_bytes()).decode("ascii")
    except OSError:
        return None


async def apply_profile(db: Session, line: WhatsAppLine, profile_fn=None) -> bool:
    """Give the connected account the configured profile name and picture."""
    profile_fn = profile_fn or evolution_set_profile
    try:
        problems = await profile_fn(line.instance_name, config.WHATSAPP_PROFILE_NAME, profile_picture_b64())
    except WhatsAppError as exc:
        problems = [str(exc)]
    line.profile_set = not problems
    line.profile_error = "; ".join(problems)[:300] or None
    db.commit()
    if problems:
        log.warning("Could not set the WhatsApp profile of %s: %s", line.label, line.profile_error)
    return not problems


async def on_connected(db: Session, line: WhatsAppLine, phone_fn=None, profile_fn=None) -> None:
    """A line just proved to be connected: remember its phone number and (once) set its profile name/picture."""
    phone_fn = phone_fn or evolution_owner_number
    if not line.phone:
        try:
            line.phone = await phone_fn(line.instance_name) or None
            db.commit()
        except Exception:
            log.exception("Could not read the phone number of %s", line.label)
    if config.WHATSAPP_AUTO_PROFILE and not line.profile_set and line.connection == "open":
        await apply_profile(db, line, profile_fn)


# --------------------------------------------------------------------------- health tests

async def test_line(db: Session, line: WhatsAppLine, target: str | None = None, peer: bool = False,
                    state_fn=None, text_fn=None, phone_fn=None, profile_fn=None, known_state: str | None = None) -> bool:
    """Is the session connected, and can it really send a message to `target` (another of our own lines, or the
    predefined test number)? Without a target only the connection is checked."""
    state_fn, text_fn = state_fn or evolution_state, text_fn or send_whatsapp_text
    error = ""
    try:
        line.connection = known_state or await state_fn(line.instance_name)
    except WhatsAppError as exc:
        line.connection, error = "unknown", f"Could not check the connection: {exc}"
    if not error and line.connection != "open":
        error = f"Not connected (state: {line.connection})"
    if not error and not known_state:
        await on_connected(db, line, phone_fn, profile_fn)
    if not error and target:
        try:
            await text_fn(target, line_chat_text() if peer else line_test_text(line.label), instance=line.instance_name)
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


def partner_for(line: WhatsAppLine, peers: list[WhatsAppLine]) -> WhatsAppLine | None:
    """The next connected line in the ring after `line` (None if `line` is not a connected peer or is alone)."""
    if line in peers and len(peers) > 1:
        return peers[(peers.index(line) + 1) % len(peers)]
    return None


async def test_one(db: Session, line: WhatsAppLine, state_fn=None, text_fn=None, phone_fn=None, profile_fn=None) -> bool:
    """Test a single line against its neighbour in the ring (falls back to the test number), with the reply."""
    state_fn, text_fn = state_fn or evolution_state, text_fn or send_whatsapp_text
    others = db.query(WhatsAppLine).filter(WhatsAppLine.status != "disabled").order_by(WhatsAppLine.priority, WhatsAppLine.id).all()
    for other in others:
        try:
            other.connection = await state_fn(other.instance_name)
            if other.connection == "open" and not other.phone:
                await on_connected(db, other, phone_fn, profile_fn)
        except WhatsAppError:
            other.connection = "unknown"
    db.commit()
    peers = [l for l in others if l.connection == "open" and l.phone]
    partner = partner_for(line, peers)
    target = partner.phone if partner else (test_number(db) or None)
    passed = await test_line(db, line, target=target, peer=partner is not None, state_fn=state_fn, text_fn=text_fn,
                             phone_fn=phone_fn, profile_fn=profile_fn,
                             known_state=line.connection if line.connection != "unknown" else None)
    if passed and partner is not None and config.LINE_TEST_REPLY and line.phone:
        try:
            await asyncio.sleep(random.uniform(*config.LINE_TEST_GAP))
            await text_fn(line.phone, line_chat_text(reply=True), instance=partner.instance_name)
        except WhatsAppError:
            pass
    return passed


async def run_line_tests(db: Session, state_fn=None, text_fn=None, phone_fn=None, profile_fn=None) -> dict:
    """Test every line that is not disabled, one by one with random pauses.

    The lines talk to each other: each one messages the next connected line in a ring (so every number both sends and
    receives some ordinary activity) and the receiver answers with a short reply. Only when no other line is available
    is the predefined test number used. Returns {'ok': n, 'bad': n}.
    """
    state_fn, text_fn = state_fn or evolution_state, text_fn or send_whatsapp_text
    lines = db.query(WhatsAppLine).filter(WhatsAppLine.status != "disabled").order_by(
        WhatsAppLine.priority, WhatsAppLine.id).all()
    for line in lines:                                   # who is connected, and what are their numbers? (no WhatsApp traffic)
        try:
            line.connection = await state_fn(line.instance_name)
            if line.connection == "open":
                await on_connected(db, line, phone_fn, profile_fn)
        except WhatsAppError:
            line.connection = "unknown"
    db.commit()
    peers = [l for l in lines if l.connection == "open" and l.phone]
    fallback = test_number(db)
    ok = bad = 0
    for i, line in enumerate(lines):
        if i:
            await asyncio.sleep(random.uniform(*config.LINE_TEST_GAP))
        partner = partner_for(line, peers)
        target = partner.phone if partner else (fallback or None)
        passed = await test_line(db, line, target=target, peer=partner is not None, state_fn=state_fn, text_fn=text_fn,
                                 phone_fn=phone_fn, profile_fn=profile_fn,
                                 known_state=line.connection if line.connection != "unknown" else None)
        if passed and partner is not None and config.LINE_TEST_REPLY and line.phone:
            try:                                          # the other number answers: activity in both directions
                await asyncio.sleep(random.uniform(*config.LINE_TEST_GAP))
                await text_fn(line.phone, line_chat_text(reply=True), instance=partner.instance_name)
            except WhatsAppError as exc:
                log.info("Reply from %s to %s failed: %s", partner.label, line.label, exc)
        ok, bad = (ok + 1, bad) if passed else (ok, bad + 1)
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
