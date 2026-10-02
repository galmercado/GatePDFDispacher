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

# Ticket messages are removed from WhatsApp ("delete for everyone") when the guest is admitted and, as a
# safety net, this many hours after sending (0 disables the timer). WhatsApp only allows deleting for
# everyone for roughly two days, so keep this well below 48.
WHATSAPP_AUTO_DELETE_HOURS = float(os.getenv("WHATSAPP_AUTO_DELETE_HOURS", "24"))
WHATSAPP_DELETE_ON_ADMIT = _bool("WHATSAPP_DELETE_ON_ADMIT", True)
# The timer-based cleanup is deliberately infrequent (to look less like a bot to WhatsApp): it wakes up every
# WHATSAPP_CLEANUP_INTERVAL_HOURS and only talks to WhatsApp if an event took place in the last
# WHATSAPP_CLEANUP_WINDOW_HOURS.
WHATSAPP_CLEANUP_INTERVAL_HOURS = max(1.0, float(os.getenv("WHATSAPP_CLEANUP_INTERVAL_HOURS", "12")))
WHATSAPP_CLEANUP_WINDOW_HOURS = float(os.getenv("WHATSAPP_CLEANUP_WINDOW_HOURS", "48"))
WHATSAPP_CLEANUP_STARTUP_DELAY = 300.0  # seconds after boot before the first check
WHATSAPP_DELETE_GIVE_UP_HOURS = 47.0

MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_LIST_BYTES = 5 * 1024 * 1024
