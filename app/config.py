"""Runtime configuration, read from environment variables."""
import os
import re
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me-please-0123456789abcdef")
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "480"))
COOKIE_NAME = "access_token"
COOKIE_SECURE = _bool("COOKIE_SECURE", False)  # set true behind HTTPS

# Absolute public URL used inside QR codes (e.g. https://tickets.example.org).
# Falls back to the request's base URL when empty.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
# Country code prepended to local numbers (e.g. 052-5607772 -> 972525607772).
DEFAULT_COUNTRY_CODE = re.sub(r"\D", "", os.getenv("DEFAULT_COUNTRY_CODE", "972"))
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "event-door")
# Anti-ban throttle: minimum gap between any two outgoing WhatsApp messages
# (global across all users). Never allowed below 1 second.
# Gap between two sends = MIN_INTERVAL + random(0, JITTER) seconds (hard floor 1 s), plus - now and then - a longer
# "human" pause. Before each message Evolution also shows "typing..." for a random 1.2-3.8 s.
WHATSAPP_MIN_INTERVAL = max(1.0, float(os.getenv("WHATSAPP_MIN_INTERVAL", "3.0")))
WHATSAPP_JITTER = max(0.0, float(os.getenv("WHATSAPP_JITTER", "4.0")))
WHATSAPP_LONG_PAUSE_CHANCE = min(1.0, max(0.0, float(os.getenv("WHATSAPP_LONG_PAUSE_CHANCE", "0.08"))))
WHATSAPP_LONG_PAUSE = (6.0, 15.0)            # extra seconds on those occasional long pauses
WHATSAPP_TYPING_MS = (1200, 3800)            # simulated typing time before each message
WHATSAPP_MESSAGE_LANG = os.getenv("WHATSAPP_MESSAGE_LANG", "en")   # en | he

# Several WhatsApp numbers ("lines") with automatic fail-over; managed on the admin-only Lines page.
LINE_DAILY_CAP = int(os.getenv("LINE_DAILY_CAP", "150"))            # messages per line per day, then the next line is used
MIN_WORKING_LINES = int(os.getenv("MIN_WORKING_LINES", "3"))        # admins see an alert below this
WHATSAPP_TEST_NUMBER = re.sub(r"\D", "", os.getenv("WHATSAPP_TEST_NUMBER", ""))   # receives the pre-event line test
LINE_TEST_HOURS_BEFORE = float(os.getenv("LINE_TEST_HOURS_BEFORE", "24"))
LINE_TEST_REPLY = os.getenv("LINE_TEST_REPLY", "true").strip().lower() in {"1", "true", "yes", "on"}   # the receiving line answers
WHATSAPP_PROFILE_NAME = os.getenv("WHATSAPP_PROFILE_NAME", "the gate mta")      # profile name given to every newly connected line
WHATSAPP_AUTO_PROFILE = os.getenv("WHATSAPP_AUTO_PROFILE", "true").strip().lower() in {"1", "true", "yes", "on"}
WHATSAPP_RECONNECT_WAIT = (4.0, 8.0)     # seconds to wait before retrying after 'Connection Closed'
LINE_TEST_GAP = (8.0, 25.0)                                          # random seconds between two line tests

# Google sign-in (OAuth 2.0). Only users whose email is already in the Users list may sign in.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
# Optional: an admin created at startup that signs in with Google (in addition to the default admin).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()

DEFAULT_ADMIN_EMAIL = "admin@event.local"
DEFAULT_ADMIN_PASSWORD = "admin1234"

# Ticket messages sent over WhatsApp are removed with "delete for everyone":
#  * immediately when an order is un-admitted (that order only), together with its QR link;
#  * after the event: each message gets its own random deletion time between MIN and MAX hours after the
#    event ended, so deletions are spread out and look spontaneous instead of one burst.
# WhatsApp only allows deleting for everyone for about two days after sending, so messages sent long before
# an event may no longer be deletable.
WHATSAPP_DELETE_ON_UNADMIT = _bool("WHATSAPP_DELETE_ON_UNADMIT", True)
WHATSAPP_DELETE_AFTER_EVENT = _bool("WHATSAPP_DELETE_AFTER_EVENT", True)
WHATSAPP_DELETE_MIN_HOURS = float(os.getenv("WHATSAPP_DELETE_MIN_HOURS", "12"))
WHATSAPP_DELETE_MAX_HOURS = max(WHATSAPP_DELETE_MIN_HOURS, float(os.getenv("WHATSAPP_DELETE_MAX_HOURS", "36")))
EVENT_DURATION_HOURS = float(os.getenv("EVENT_DURATION_HOURS", "3"))   # event start + this = "event over"
EVENT_TIMEZONE = os.getenv("EVENT_TIMEZONE", "Asia/Jerusalem")          # timezone the event dates are entered in
WHATSAPP_DELETE_GAP = (3.0, 15.0)        # random pause (seconds) between two deletions
WHATSAPP_RETRY_MINUTES = (30.0, 90.0)    # random wait before retrying a failed deletion
WHATSAPP_CLEANUP_POLL_SECONDS = 600.0    # DB-only check for due messages; WhatsApp is contacted only for due ones
WHATSAPP_CLEANUP_STARTUP_DELAY = 60.0
WHATSAPP_DELETE_GIVE_UP_HOURS = 47.0

MAX_PDF_BYTES = 200 * 1024 * 1024
# Ticket PDFs are made smaller when they are split (images re-encoded, content deflated); QR codes stay lossless.
PDF_COMPRESS = _bool("PDF_COMPRESS", True)
PDF_IMAGE_MAX_PX = int(os.getenv("PDF_IMAGE_MAX_PX", "1600"))      # larger images are scaled down to this many pixels
PDF_JPEG_QUALITY = int(os.getenv("PDF_JPEG_QUALITY", "80"))        # for photo-like images only
# Ticket PDFs are deleted this many days after the event (0 = keep them).
TICKET_RETENTION_DAYS = int(os.getenv("TICKET_RETENTION_DAYS", "30"))
# Key for POST /api/tickets (automatic upload from another service). Empty = the API is switched off.
TICKETS_API_KEY = os.getenv("TICKETS_API_KEY", "").strip()
MAX_LIST_BYTES = 5 * 1024 * 1024

# External registration system whose admin page lists the reservations of a game (scraped on demand).
REGISTRATION_BASE_URL = os.getenv("REGISTRATION_BASE_URL", "https://gate11.co.il").rstrip("/")
REGISTRATION_PATH = "/admin/page.php?id=ticketReservations&gameId={game_id}"
REGISTRATION_COOKIE = os.getenv("REGISTRATION_COOKIE", "")      # optional default; normally pasted on the Manage page
MAX_REGISTRATION_BYTES = 5 * 1024 * 1024
