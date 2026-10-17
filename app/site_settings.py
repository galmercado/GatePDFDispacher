"""Site constants an admin can change on the Settings page.

Defaults come from the environment / code (config.py, data.py, messages.py). Changed values are stored in the database
(`app_settings`, key `cfg.<name>`) and applied onto those modules at startup and on save, so the rest of the code keeps
reading `config.X` as before. "Reset" removes the stored values and restores the defaults.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from . import config, data, messages
from .models import AppSetting


@dataclass
class Setting:
    key: str
    group: str
    label: str
    kind: str                       # int | float | bool | text | choice | lines | range
    default: Any
    apply: Callable[[Any], None]
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple = ()
    part: str = ""                  # for message wording lists: which part (greet, body ...)


class SettingsError(ValueError):
    pass


def _cfg(name):
    def apply(value):
        setattr(config, name, value)
    return apply


def _data(name):
    def apply(value):
        setattr(data, name, value)
    return apply


def _opponents(value):
    data.OPPONENTS = list(value)


def _arenas(value):
    data.ARENAS = list(value)


def _default_location(value):
    data.DEFAULT_LOCATION = value
    data.ARENAS = [value] + [a for a in data.ARENAS if a != value]      # the default venue is always the first option


def _part(lang, part):
    def apply(value):
        messages.PARTS[lang][part] = list(value) or [""]
    return apply


def _build() -> list[Setting]:
    S = Setting
    items = [
        S("msg.lang", "message", "Language of the ticket message", "choice", config.WHATSAPP_MESSAGE_LANG,
          _cfg("WHATSAPP_MESSAGE_LANG"), "Which wording set below is used.", choices=("en", "he")),
    ]
    labels = {"greet": "Greetings", "body": "Main sentence", "count": "Ticket counter (when an order has several)",
              "when": "Date / time line", "close": "Closing sentences", "emoji": "Emoji (an empty line = none)"}
    helps = {"greet": "Use {name} for the guest's first name.", "body": "Use {event} for the event name.",
             "count": "Use {i} and {n}: ticket number and total.", "when": "Use {when} for the date and time.",
             "close": "Plain sentences.", "emoji": "Optional decoration."}
    for lang in ("en", "he"):
        for part in messages.PART_PLACEHOLDERS:
            items.append(S(f"msg.{lang}.{part}", "message", f"{labels[part]} ({lang.upper()})", "lines",
                           list(messages.DEFAULT_PARTS[lang][part]), _part(lang, part), helps[part], part=part))
    items += [
        S("send.min_interval", "sending", "Minimum seconds between two messages", "float", config.WHATSAPP_MIN_INTERVAL,
          lambda v: setattr(config, "WHATSAPP_MIN_INTERVAL", max(1.0, v)), "Never below 1 second.", lo=1, hi=120),
        S("send.jitter", "sending", "Random extra seconds (0 up to this)", "float", config.WHATSAPP_JITTER,
          _cfg("WHATSAPP_JITTER"), "A random value up to this is added to every gap.", lo=0, hi=120),
        S("send.long_pause_chance", "sending", "Chance of an occasional longer pause (0-1)", "float", config.WHATSAPP_LONG_PAUSE_CHANCE,
          _cfg("WHATSAPP_LONG_PAUSE_CHANCE"), "0.08 = about one message in twelve is followed by an extra 6-15 s.", lo=0, hi=1),
        S("send.typing_ms", "sending", "'Typing…' time before each message (ms)", "range", list(config.WHATSAPP_TYPING_MS),
          lambda v: setattr(config, "WHATSAPP_TYPING_MS", (int(v[0]), int(v[1]))), "Random between the two values.", lo=0, hi=20000),
        S("lines.daily_cap", "sending", "Messages per line per day (default for new lines)", "int", config.LINE_DAILY_CAP,
          _cfg("LINE_DAILY_CAP"), "Existing lines keep their own limit (Lines page).", lo=1, hi=5000),
        S("lines.min_working", "sending", "Alert when fewer lines work than", "int", config.MIN_WORKING_LINES,
          _cfg("MIN_WORKING_LINES"), lo=0, hi=100),
        S("lines.test_hours", "sending", "Test all lines this many hours before an event", "float", config.LINE_TEST_HOURS_BEFORE,
          _cfg("LINE_TEST_HOURS_BEFORE"), lo=1, hi=240),
        S("lines.test_reply", "sending", "Lines answer each other's test message", "bool", config.LINE_TEST_REPLY,
          _cfg("LINE_TEST_REPLY"), "The pre-event test is a short chat between your own numbers; the receiver replies."),
        S("profile.name", "sending", "WhatsApp profile name for lines", "text", config.WHATSAPP_PROFILE_NAME, _cfg("WHATSAPP_PROFILE_NAME"),
          "Set on every line when it connects (and by 'Apply profile')."),
        S("profile.auto", "sending", "Set the profile name and picture automatically when a line connects", "bool", config.WHATSAPP_AUTO_PROFILE,
          _cfg("WHATSAPP_AUTO_PROFILE")),
        S("del.on_unadmit", "cleanup", "Delete the ticket message when an order is un-admitted", "bool", config.WHATSAPP_DELETE_ON_UNADMIT,
          _cfg("WHATSAPP_DELETE_ON_UNADMIT")),
        S("del.after_event", "cleanup", "Delete ticket messages after the event", "bool", config.WHATSAPP_DELETE_AFTER_EVENT,
          _cfg("WHATSAPP_DELETE_AFTER_EVENT")),
        S("del.min_hours", "cleanup", "Earliest deletion, hours after the event ends", "float", config.WHATSAPP_DELETE_MIN_HOURS,
          _cfg("WHATSAPP_DELETE_MIN_HOURS"), lo=0, hi=46),
        S("del.max_hours", "cleanup", "Latest deletion, hours after the event ends", "float", config.WHATSAPP_DELETE_MAX_HOURS,
          _cfg("WHATSAPP_DELETE_MAX_HOURS"), "Each message gets its own random time between the two. WhatsApp allows deleting only ~2 days after sending.", lo=0, hi=46),
        S("del.event_hours", "cleanup", "Event length: the event is 'over' this many hours after it starts", "float", config.EVENT_DURATION_HOURS,
          _cfg("EVENT_DURATION_HOURS"), lo=0, hi=24),
        S("shirt.url", "site", "Shirt payment link (Bit)", "text", config.SHIRT_PAYMENT_URL, _cfg("SHIRT_PAYMENT_URL"),
          "Sent to guests marked 'Shirt missing' and shown as a QR code."),
        S("shirt.message", "site", "Message sent with the shirt payment link", "text", config.SHIRT_MESSAGE, _cfg("SHIRT_MESSAGE"),
          "Use {name}, {count} (tickets held) and {link}."),
        S("files.retention_days", "cleanup", "Delete the ticket PDFs this many days after the event", "int", config.TICKET_RETENTION_DAYS,
          _cfg("TICKET_RETENTION_DAYS"), "0 keeps them. Guest list and attendance stay; only the PDF files (and their download links) go.", lo=0, hi=3650),
        S("files.compress", "site", "Compress ticket PDFs when they are uploaded", "bool", config.PDF_COMPRESS, _cfg("PDF_COMPRESS"),
          "Shrinks images and page data to save storage. QR codes and barcodes are kept lossless (only scaled down if huge)."),
        S("site.country_code", "site", "Default country code for local phone numbers", "text", config.DEFAULT_COUNTRY_CODE,
          _cfg("DEFAULT_COUNTRY_CODE"), "Digits only, e.g. 972 for Israel."),
        S("site.timezone", "site", "Time zone event dates are entered in", "text", config.EVENT_TIMEZONE, _cfg("EVENT_TIMEZONE"),
          "e.g. Asia/Jerusalem."),
        S("site.home_team", "site", "Home team", "text", data.HOME_TEAM, _data("HOME_TEAM")),
        S("site.versus", "site", "Word between the teams in the event name", "text", data.VERSUS, _data("VERSUS")),
        S("site.opponents", "site", "Opponent teams (one per line)", "lines", list(data.OPPONENTS), _opponents,
          "Shown in the event form's dropdown; the form also has an 'other' option."),
        S("site.default_location", "site", "Default venue", "text", data.DEFAULT_LOCATION, _default_location,
          "Preselected for new events; always first in the list."),
        S("site.arenas", "site", "Other venues (one per line)", "lines", [a for a in data.ARENAS if a != data.DEFAULT_LOCATION], _arenas),
    ]
    return items


SETTINGS: dict[str, Setting] = {s.key: s for s in _build()}
GROUPS = {"message": "Ticket message", "sending": "WhatsApp sending & lines", "cleanup": "Deleting messages", "site": "Event form & phone numbers"}
PREFIX = "cfg."


def groups() -> list[tuple[str, str, list[Setting]]]:
    return [(g, title, [s for s in SETTINGS.values() if s.group == g]) for g, title in GROUPS.items()]


# --------------------------------------------------------------------------- parsing / validation

def parse(setting: Setting, raw: Any) -> Any:
    """Turn submitted form text into the setting's value, or raise SettingsError."""
    k, name = setting.kind, setting.label
    try:
        if k in ("int", "float"):
            value = int(str(raw).strip()) if k == "int" else float(str(raw).strip().replace(",", "."))
            if (setting.lo is not None and value < setting.lo) or (setting.hi is not None and value > setting.hi):
                raise SettingsError(f"{name}: must be between {setting.lo:g} and {setting.hi:g}.")
            return value
        if k == "bool":
            return str(raw) in ("on", "true", "1", "True")
        if k == "choice":
            if raw not in setting.choices:
                raise SettingsError(f"{name}: choose one of {', '.join(setting.choices)}.")
            return raw
        if k == "range":
            lo, hi = int(str(raw[0]).strip()), int(str(raw[1]).strip())
            if lo > hi or lo < (setting.lo or 0) or hi > (setting.hi or 10**9):
                raise SettingsError(f"{name}: enter two numbers, smallest first (0-{setting.hi:g}).")
            return [lo, hi]
        if k == "lines":
            lines = list(dict.fromkeys(ln.strip() for ln in str(raw).splitlines() if ln.strip()))
            if len(lines) > 300 or any(len(ln) > 300 for ln in lines):
                raise SettingsError(f"{name}: too many or too long lines.")
            if setting.part:
                sample = {"name": "Dana", "event": "Event", "i": 1, "n": 2, "when": "Sat 10/05 at 20:30"}
                for ln in lines:
                    try:
                        ln.format(**sample)
                    except (KeyError, IndexError, ValueError):
                        allowed = ", ".join("{%s}" % p for p in messages.PART_PLACEHOLDERS[setting.part]) or "no placeholders"
                        raise SettingsError(f"{name}: “{ln[:40]}” uses an unknown placeholder or stray brace (allowed: {allowed}).")
                if setting.part in ("greet", "body") and not lines:
                    raise SettingsError(f"{name}: at least one line is required.")
            elif not lines:
                raise SettingsError(f"{name}: at least one line is required.")
            return lines
        value = " ".join(str(raw).split())            # text
        if not value or len(value) > 120:
            raise SettingsError(f"{name}: required (up to 120 characters).")
        if setting.key == "site.country_code":
            value = re.sub(r"\D", "", value)
            if not 1 <= len(value) <= 4:
                raise SettingsError(f"{name}: 1-4 digits.")
        if setting.key == "site.timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise SettingsError(f"{name}: unknown time zone “{value}”.")
        return value
    except (ValueError, TypeError, IndexError) as exc:
        if isinstance(exc, SettingsError):
            raise
        raise SettingsError(f"{name}: not a valid value.")


def current(key: str) -> Any:
    return _values.get(key, SETTINGS[key].default)


_values: dict[str, Any] = {}


def _apply(key: str, value: Any) -> None:
    SETTINGS[key].apply(value)
    _values[key] = value


# --------------------------------------------------------------------------- persistence

def load_and_apply(db: Session) -> None:
    for row in db.query(AppSetting).filter(AppSetting.key.like(PREFIX + "%")):
        key = row.key[len(PREFIX):]
        if key in SETTINGS:
            try:
                _apply(key, json.loads(row.value))
            except (ValueError, TypeError):
                continue


def save_group(db: Session, group: str, form: dict[str, Any]) -> list[str]:
    """Validate and store every setting of `group` found in the form. Nothing is saved if anything is invalid."""
    parsed, errors = {}, []
    for setting in SETTINGS.values():
        if setting.group != group:
            continue
        if setting.kind == "range":
            raw = [form.get(f"f.{setting.key}.lo", ""), form.get(f"f.{setting.key}.hi", "")]
        elif setting.kind == "bool":
            raw = "on" if form.get(f"f.{setting.key}") else ""
        else:
            raw = form.get(f"f.{setting.key}")
            if raw is None:
                continue
        try:
            parsed[setting.key] = parse(setting, raw)
        except SettingsError as exc:
            errors.append(str(exc))
    if "del.min_hours" in parsed or "del.max_hours" in parsed:
        lo, hi = parsed.get("del.min_hours", current("del.min_hours")), parsed.get("del.max_hours", current("del.max_hours"))
        if hi < lo:
            errors.append("Deleting messages: the latest time must not be before the earliest.")
    if errors:
        return errors
    for key, value in parsed.items():
        row = db.get(AppSetting, PREFIX + key)
        text = json.dumps(value, ensure_ascii=False)
        if row is None:
            db.add(AppSetting(key=PREFIX + key, value=text))
        else:
            row.value = text
    db.commit()
    for key, value in parsed.items():
        _apply(key, value)
    # the default venue must stay first and the arena list consistent
    if "site.default_location" in parsed or "site.arenas" in parsed:
        _default_location(current("site.default_location"))
        _arenas([current("site.default_location")] + [a for a in current("site.arenas") if a != current("site.default_location")])
    return []


def reset_group(db: Session, group: str | None = None) -> None:
    for setting in SETTINGS.values():
        if group is None or setting.group == group:
            row = db.get(AppSetting, PREFIX + setting.key)
            if row is not None:
                db.delete(row)
            setting.apply(setting.default)
            _values.pop(setting.key, None)
    db.commit()
    if group in (None, "site"):
        _default_location(current("site.default_location"))
        _arenas([current("site.default_location")] + [a for a in current("site.arenas") if a != current("site.default_location")])


def preview_messages(count: int = 6) -> list[str]:
    from datetime import datetime, timedelta
    when = datetime.now() + timedelta(days=3)
    event = f"{data.HOME_TEAM} {data.VERSUS} {data.OPPONENTS[0] if data.OPPONENTS else '...'}"
    name = "דנה לוי" if config.WHATSAPP_MESSAGE_LANG == "he" else "Dana Levi"
    return [messages.ticket_caption(name, event, when, index=1 + i % 2, total=2) for i in range(count)]
