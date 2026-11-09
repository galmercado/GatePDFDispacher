"""Test console: split a PDF into single tickets and dispatch tickets on WhatsApp, with a live per-ticket status.

Batches are stored in the database (DispatchBatch / DispatchItem) and sent by a background task, one ticket after the other
through the normal WhatsApp lines (same throttling, wording and fail-over as the real dispatch)."""
import asyncio
import io
import logging
import re
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from . import config
from .database import SessionLocal
from .lines import deliver_pdf
from .messages import ticket_caption
from .models import DispatchBatch, DispatchItem, Event, WhatsAppLine, utcnow
from .pdfs import compress_writer, read_codes, sha256_hex
from .services import WhatsAppError, _open_pdf, _page_bytes, normalize_phone

log = logging.getLogger("gate.lab")
LAB_KEEP_DAYS = 3                 # split batches (their files) are removed after this many days
_tasks: set = set()
_run_lock: asyncio.Lock | None = None
_run_loop = None


def lab_dir(batch_id: int) -> Path:
    return config.STORAGE_DIR / "lab" / str(batch_id)


def parse_numbers(text: str) -> list[str]:
    """Phone numbers from free text (one per line, or separated by commas/spaces); invalid lines are dropped."""
    out = []
    for token in re.split(r"[\n,;]+", text or ""):
        token = token.strip()
        digits = re.sub(r"\D", "", normalize_phone(token)) if token else ""
        if 8 <= len(digits) <= 15:
            out.append(digits)
    return out


def split_to_batch(db: Session, contents: bytes | list[bytes], title: str, numbers: list[str], same_for_all: bool, compress: bool,
                   progress=None) -> DispatchBatch:
    """Split one or more PDFs into single-page files for a new batch (their pages follow each other). Recipients come from
    `numbers`: one for every page (same_for_all) or one per page in order; pages without a number stay empty to be filled in
    on the batch page."""
    if isinstance(contents, (bytes, bytearray)):
        contents = [bytes(contents)]
    pages = []
    for content in contents:
        pages.extend(_open_pdf(content).pages)
    batch = DispatchBatch(kind="split", title=(title or "Split tickets")[:160], state="draft")
    db.add(batch)
    db.commit()
    folder = lab_dir(batch.id)
    folder.mkdir(parents=True, exist_ok=True)
    for number, page in enumerate(pages, start=1):
        if progress:
            progress((number - 1) / len(pages), f"Splitting page {number} of {len(pages)}")
        data = _page_bytes(page)
        code = read_codes(data) or ""
        if compress and config.PDF_COMPRESS:
            try:
                from pypdf import PdfWriter
                writer = PdfWriter()
                writer.add_page(page)
                compress_writer(writer)
                buf = io.BytesIO()
                writer.write(buf)
                if len(buf.getvalue()) < len(data) and (not code or read_codes(buf.getvalue()) == code):
                    data = buf.getvalue()
            except Exception:                                  # noqa: BLE001 - never lose a page over an optimisation
                log.exception("Could not compress page %s", number)
        path = folder / f"page_{number:03d}.pdf"
        path.write_bytes(data)
        phone = (numbers[0] if numbers else "") if same_for_all else (numbers[number - 1] if number <= len(numbers) else "")
        db.add(DispatchItem(batch_id=batch.id, position=number, phone=phone, file_path=str(path), qr_code=code,
                            index_of=f"{number}/{len(pages)}", name=f"Page {number}"))
    db.commit()
    return batch


def guest_batch(db: Session, event: Event, attendees, redirect_phone: str, resend: bool) -> DispatchBatch:
    """One item per ticket PDF of the chosen guests (only the ones not sent yet, unless `resend`)."""
    batch = DispatchBatch(kind="guests", title=f"{event.name}", event_id=event.id, redirect_phone=redirect_phone, state="draft")
    db.add(batch)
    db.commit()
    position = 0
    for guest in attendees:
        tickets = sorted(guest.tickets, key=lambda t: t.page_number)
        for number, ticket in enumerate(tickets, start=1):
            if not resend and ticket.delivered_at:
                continue
            position += 1
            db.add(DispatchItem(batch_id=batch.id, position=position, name=guest.name, attendee_id=guest.id, ticket_id=ticket.id,
                                phone=redirect_phone or guest.phone, file_path=ticket.file_path, qr_code=ticket.qr_code,
                                index_of=f"{number}/{len(tickets)}"))
    db.commit()
    return batch


def batch_counts(db: Session, batch_id: int) -> dict:
    counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for (status,) in db.query(DispatchItem.status).filter(DispatchItem.batch_id == batch_id):
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = sum(counts.values())
    return counts


def _lock() -> asyncio.Lock:
    global _run_lock, _run_loop
    loop = asyncio.get_running_loop()
    if _run_lock is None or _run_loop is not loop:
        _run_lock, _run_loop = asyncio.Lock(), loop
    return _run_lock


def start(batch_id: int, sender, on_sent=None) -> None:
    """Send a batch in the background (queued items only)."""
    task = asyncio.ensure_future(run_batch(batch_id, sender, on_sent))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def run_batch(batch_id: int, sender, on_sent=None) -> None:
    """Send every queued item of the batch, one after the other. `on_sent(db, item, ref, line)` runs after each success."""
    async with _lock():
        with SessionLocal() as db:
            batch = db.get(DispatchBatch, batch_id)
            if batch is None:
                return
            batch.state = "running"
            db.commit()
            event = db.get(Event, batch.event_id) if batch.event_id else None
            ids = [i for (i,) in db.query(DispatchItem.id).filter(DispatchItem.batch_id == batch_id, DispatchItem.status == "queued")
                   .order_by(DispatchItem.position)]
            for item_id in ids:
                item = db.get(DispatchItem, item_id)
                if item is None or item.status != "queued":
                    continue
                if not item.phone:
                    item.status, item.detail = "skipped", "No number for this page."
                    db.commit()
                    continue
                item.status, item.detail = "sending", ""
                db.commit()
                try:
                    caption = _caption(batch, item, event, db)
                    ref, line = await deliver_pdf(db, item.phone, item.file_path, caption=caption,
                                                  file_name=f"ticket-{item.position}.pdf", sender=sender)
                except WhatsAppError as exc:
                    item.status, item.detail = "failed", str(exc)[:400]
                    db.commit()
                    continue
                except Exception as exc:                       # noqa: BLE001 - one bad ticket must not stop the batch
                    log.exception("Dispatch item %s crashed", item_id)
                    item.status, item.detail = "failed", f"Unexpected error: {exc}"[:400]
                    db.commit()
                    continue
                item.status, item.sent_at, item.line_id = "sent", utcnow(), line.id
                item.detail = f"via {line.label}"
                if ref is not None:
                    item.remote_jid, item.message_id = ref.remote_jid, ref.message_id
                db.commit()
                if on_sent is not None:
                    try:
                        on_sent(db, item, ref, line)
                    except Exception:                          # noqa: BLE001
                        log.exception("Could not record dispatch item %s", item_id)
            batch.state = "done"
            db.commit()


def _caption(batch: DispatchBatch, item: DispatchItem, event: Event | None, db: Session) -> str:
    index, total = (int(x) for x in item.index_of.split("/"))
    name = item.name if batch.kind == "guests" else ""
    base = ticket_caption(name, event.name if event else batch.title, event.event_date if event else None, index, total)
    if batch.redirect_phone:
        return f"[TEST for {item.name}] {base}"
    return base


def retry_failed(db: Session, batch_id: int) -> int:
    n = db.query(DispatchItem).filter(DispatchItem.batch_id == batch_id, DispatchItem.status.in_(("failed", "skipped")),
                                      DispatchItem.phone != "").update({DispatchItem.status: "queued", DispatchItem.detail: ""},
                                                                       synchronize_session=False)
    db.commit()
    return n


async def unsend_batch(batch_id: int, deleter) -> tuple[int, int]:
    """Delete the sent messages of a batch from WhatsApp ("for everyone"). Returns (deleted, failed)."""
    done = failed = 0
    with SessionLocal() as db:
        for item in db.query(DispatchItem).filter(DispatchItem.batch_id == batch_id, DispatchItem.status == "sent",
                                                   DispatchItem.unsent.is_(False), DispatchItem.message_id != ""):
            line = db.get(WhatsAppLine, item.line_id) if item.line_id else None
            try:
                await deleter(item.remote_jid, item.message_id, instance=line.instance_name if line else None)
                item.unsent = True
                item.detail = (item.detail + " · deleted from WhatsApp").strip(" ·")
                done += 1
            except WhatsAppError as exc:
                failed += 1
                item.detail = f"Could not delete: {exc}"[:400]
            db.commit()
    return done, failed


def batch_zip(db: Session, batch_id: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for item in db.query(DispatchItem).filter(DispatchItem.batch_id == batch_id).order_by(DispatchItem.position):
            path = Path(item.file_path)
            if path.is_file():
                safe = re.sub(r"[^\w-]+", "_", item.name) or "ticket"
                zf.write(path, f"{item.position:03d}-{safe}.pdf")
    return buf.getvalue()


def delete_batch(db: Session, batch: DispatchBatch) -> None:
    if batch.kind == "split":
        shutil.rmtree(lab_dir(batch.id), ignore_errors=True)
    db.query(DispatchItem).filter(DispatchItem.batch_id == batch.id).delete()
    db.delete(batch)
    db.commit()


def cleanup_old(now=None) -> int:
    """Remove split batches (and their files) older than LAB_KEEP_DAYS, and old guest batches' records after 30 days."""
    now = now or utcnow()
    removed = 0
    with SessionLocal() as db:
        for batch in db.query(DispatchBatch).all():
            age = now - batch.created_at
            if (batch.kind == "split" and age > timedelta(days=LAB_KEEP_DAYS)) or age > timedelta(days=30):
                delete_batch(db, batch)
                removed += 1
    return removed


def recover_interrupted() -> None:
    """After a restart: items that were being sent are marked failed, and running batches finished."""
    with SessionLocal() as db:
        db.query(DispatchItem).filter(DispatchItem.status == "sending").update(
            {DispatchItem.status: "failed", DispatchItem.detail: "Interrupted by a restart - press Retry."}, synchronize_session=False)
        db.query(DispatchBatch).filter(DispatchBatch.state == "running").update({DispatchBatch.state: "done"}, synchronize_session=False)
        db.commit()


def preview_png(path: str, scale: float = 0.6) -> bytes | None:
    """A small picture of a ticket page (None when the renderer is not installed)."""
    try:
        import pypdfium2
        from .pdfs import _PDFIUM_LOCK
    except ImportError:
        return None
    try:
        with _PDFIUM_LOCK:
            doc = pypdfium2.PdfDocument(Path(path).read_bytes())
            try:
                image = doc[0].render(scale=scale).to_pil()
            finally:
                doc.close()
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:                                          # noqa: BLE001
        return None
