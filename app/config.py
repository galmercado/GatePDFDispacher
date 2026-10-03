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
WHATSAPP_MIN_INTERVAL = max(1.0, float(os.getenv("WHATSAPP_MIN_INTERVAL", "1.5")))
WHATSAPP_JITTER = max(0.0, float(os.getenv("WHATSAPP_JITTER", "0.5")))

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
MAX_LIST_BYTES = 5 * 1024 * 1024
