"""Ticket PDF helpers: size reduction, Google Drive download, and reading the game out of a ticket file
(its header line and its first page)."""
import io
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher

import httpx
from pypdf import PdfReader, PdfWriter

from . import config, data

log = logging.getLogger("gate.pdfs")


class DriveError(ValueError):
    pass


# --------------------------------------------------------------------------- compression

MIN_IMAGE_BYTES = 20 * 1024        # smaller images are not worth touching


def _shrink_image(img) -> None:
    """Re-encode one image of a page when that makes it smaller.

    Flat images (QR codes, barcodes, logos: at most 256 colours) are only scaled down - and only when huge - and
    stored without loss as an indexed image, so a code stays sharp. Photo-like images are also JPEG-compressed.
    The old image is kept whenever the result is not smaller or anything looks unusual."""
    from PIL import Image

    ref = img.indirect_reference
    if ref is None:
        return
    pdf, slot = ref.pdf, ref.idnum - 1
    old = pdf._objects[slot]
    if any(key in old for key in ("/SMask", "/Mask", "/ImageMask", "/Decode")):
        return
    old_len = len(getattr(old, "_data", b""))
    if old_len < MIN_IMAGE_BYTES:
        return

    pil = img.image
    if pil.mode in ("1", "RGBA", "LA", "PA"):
        return
    rgb = pil.convert("RGB")
    flat = rgb.getcolors(maxcolors=256) is not None
    limit = config.PDF_IMAGE_MAX_PX
    if max(rgb.size) > limit:
        scale = limit / max(rgb.size)
        rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                         Image.Resampling.BOX if flat else Image.Resampling.LANCZOS)
    if flat:
        img.replace(rgb.quantize(colors=256, dither=Image.Dither.NONE))
    else:
        img.replace(rgb.convert("L") if pil.mode == "L" else rgb, quality=config.PDF_JPEG_QUALITY, optimize=True)
    fresh = pdf._objects[slot]
    if len(getattr(fresh, "_data", b"")) >= old_len:
        pdf._objects[slot] = old
        old.indirect_reference = ref


def compress_writer(writer: PdfWriter) -> None:
    """Make the PDF in `writer` smaller without changing how it looks or scans: re-encode oversized images
    (see _shrink_image), deflate the page content and drop duplicate / unused objects."""
    for page in writer.pages:
        try:
            images = list(page.images)
        except Exception:                      # unreadable image dictionary: leave the page as it is
            images = []
        for img in images:
            try:
                _shrink_image(img)
            except Exception as exc:
                log.debug("Image left unchanged: %s", exc)
        page.compress_content_streams()
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)


# --------------------------------------------------------------------------- Google Drive

_DRIVE_ID = re.compile(r"^[\w-]{10,}$")
DRIVE_DOWNLOAD = "https://drive.usercontent.google.com/download"


def drive_file_id(link: str) -> str:
    """The file id inside a Google Drive share link (…/file/d/ID/view, open?id=ID, uc?id=ID)."""
    link = link.strip()
    if not re.match(r"https?://(drive|docs)\.google\.com/", link):
        raise DriveError("That is not a Google Drive link (drive.google.com/file/d/…).")
    if "/folders/" in link:
        raise DriveError("That link is a folder - share the PDF file itself and paste its link.")
    match = re.search(r"/d/([\w-]+)", link) or re.search(r"[?&]id=([\w-]+)", link)
    if not match or not _DRIVE_ID.match(match.group(1)):
        raise DriveError("Could not find the file in that Drive link.")
    return match.group(1)


async def fetch_drive_pdf(link: str, client: httpx.AsyncClient | None = None, progress=None) -> bytes:
    """Download a PDF that is shared on Google Drive as 'anyone with the link'."""
    file_id = drive_file_id(link)
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=15.0))
    try:
        async with client.stream("GET", DRIVE_DOWNLOAD, params={"id": file_id, "export": "download", "confirm": "t"}) as resp:
            if resp.status_code != 200:
                raise DriveError(f"Google Drive answered {resp.status_code} - is the file shared with "
                                 "“anyone with the link”?")
            body = bytearray()
            total = int(resp.headers.get("content-length") or 0)
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if progress:
                    progress(len(body), total)
                if len(body) > config.MAX_PDF_BYTES:
                    raise DriveError(f"The file is too large ({config.MAX_PDF_BYTES // (1024 * 1024)} MB max).")
    except httpx.HTTPError as exc:
        raise DriveError(f"Could not reach Google Drive: {exc.__class__.__name__}")
    finally:
        if own:
            await client.aclose()
    if not bytes(body[:1024]).lstrip().startswith(b"%PDF"):
        raise DriveError("Google Drive did not return a PDF - share the file with “anyone with the link” "
                         "and make sure it is a PDF.")
    return bytes(body)


# --------------------------------------------------------------------------- reading the game from a ticket file

def _letters(text: str) -> str:
    """Letters and digits only, lower-case - names compare without spaces, quotes and punctuation."""
    return re.sub(r"[\W_]+", "", text.lower().replace("קריית", "קרית"))


def is_home_team(name: str) -> bool:
    """Our own team in any of its spellings (מכבי תל אביב, מכבי ת"א, מכבי ראפיד ת"א …)."""
    n = _letters(name)
    return "מכבי" in n and any(key in n for key in ("תא", "תלאביב", "ראפיד"))


def canonical_opponent(name: str) -> str:
    """The spelling used in the opponents list when the name is (nearly) one of them, else the name as given."""
    wanted = _letters(name)
    best, best_score = "", 0.0
    for known in data.OPPONENTS:
        score = SequenceMatcher(None, wanted, _letters(known)).ratio()
        if score > best_score:
            best, best_score = known, score
    return best if best_score >= 0.82 else " ".join(name.split())


_DASH = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")


def parse_ticket_header(header: str) -> dict:
    """'קיבלת כרטיסים למכבי ראפיד ת"א - עירוני קריית אתא - לא מסומן' -> {'opponent': 'עירוני קריית אתא',
    'away': False, 'seating': 'לא מסומן'}. Raises ValueError when no opponent can be found."""
    text = " ".join(header.replace("‏", "").replace("‎", "").split())
    prefix = re.match(r"^קיבלת\s+כרטיסים\s*", text)
    if prefix:
        text = text[prefix.end():]
        if text.startswith("ל") and not text.startswith("לא "):
            text = text[1:]
    parts = [p.strip(" -–—") for p in _DASH.split(text) if p.strip(" -–—")]
    if len(parts) < 2:
        raise ValueError("The header should look like “… <home team> - <away team> - <seating>”.")
    first, second = parts[0], parts[1]
    if is_home_team(first):
        opponent, away = second, False
    elif is_home_team(second):
        opponent, away = first, True
    else:
        opponent, away = second, False          # unknown home team: the second name is taken as the opponent
    return {"opponent": canonical_opponent(opponent), "away": away,
            "seating": parts[2] if len(parts) > 2 else ""}


_HEBREW_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6, "יולי": 7,
                  "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}


def _first_page_texts(content: bytes) -> list[str]:
    """Text of the first page as extracted, and line by line reversed (some Hebrew PDFs store visual order)."""
    try:
        text = PdfReader(io.BytesIO(content)).pages[0].extract_text() or ""
    except Exception:
        return []
    return [text, "\n".join(line[::-1] for line in text.splitlines())]


def _date_in(text: str) -> datetime | None:
    def make(y, m, d):
        y = int(y)
        y += 2000 if y < 100 else 0
        try:
            return datetime(y, int(m), int(d))
        except ValueError:
            return None

    candidates = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4}|\d{2})(?!\d)", text):
        candidates.append((m.start(), make(m.group(3), m.group(2), m.group(1))))
    for m in re.finditer(r"(?<!\d)(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?!\d)", text):
        candidates.append((m.start(), make(m.group(1), m.group(2), m.group(3))))
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s+ב?(" + "|".join(_HEBREW_MONTHS) + r")\s+(\d{4})", text):
        candidates.append((m.start(), make(m.group(3), _HEBREW_MONTHS[m.group(2)], m.group(1))))
    candidates = sorted((c for c in candidates if c[1] and 2000 <= c[1].year <= 2100), key=lambda c: c[0])
    if not candidates:
        return None
    pos, day = candidates[0]
    clock = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text[max(0, pos - 80): pos + 120])
    return day.replace(hour=int(clock.group(1)), minute=int(clock.group(2))) if clock else day


def _arena_in(text: str) -> str:
    blob = _letters(text)
    best, best_len = "", 0
    for arena in data.ARENAS:
        names = {arena, re.sub(r"\(.*?\)", "", arena), *re.findall(r"\((.*?)\)", arena)}
        for name in names:
            key = _letters(name)
            if len(key) >= 4 and key in blob and len(key) > best_len:
                best, best_len = arena, len(key)
    return best


def read_ticket_meta(content: bytes) -> dict:
    """{'when': datetime | None, 'has_time': bool, 'location': str} from the first page of the ticket PDF."""
    when, location = None, ""
    for text in _first_page_texts(content):
        when = when or _date_in(text)
        location = location or _arena_in(text)
    return {"when": when, "has_time": bool(when and (when.hour or when.minute)), "location": location}
