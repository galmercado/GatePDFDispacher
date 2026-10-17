"""Ticket message wording. Every message is assembled from interchangeable parts, so no two guests get
exactly the same text (a fixed template is an easy fingerprint for spam filters)."""
import random
from collections import deque
from datetime import datetime

from . import config

_RECENT: deque[str] = deque(maxlen=12)

_EN = {
    "greet": ["Hi {name}", "Hello {name}", "Hey {name}", "Hi {name},", "Hello {name},", "Good day {name}", "Hi there {name}"],
    "body": [
        "your ticket for {event} is attached",
        "here is your ticket for {event}",
        "attached is your ticket for {event}",
        "sending you your ticket for {event}",
        "this is your ticket for {event}",
        "your {event} ticket is attached",
    ],
    "count": ["(ticket {i} of {n})", "- ticket {i}/{n}", "[{i}/{n}]", ""],
    "when": ["The game is on {when}.", "See you on {when}!", "We start {when}.", "Kick-off: {when}.", ""],
    "close": ["Please show it at the entrance.", "Show it at the door.", "Have it ready at the entrance.",
              "Keep it handy for the entrance.", "See you there!", "Enjoy the game!", ""],
    "emoji": ["", "", "🏀", "🎟️", "🙂", "👍", ""],
}
_HE = {
    "greet": ["שלום {name}", "היי {name}", "הי {name},", "שלום {name},", "היי {name}!", "אהלן {name}"],
    "body": [
        "מצורף הכרטיס שלך ל{event}",
        "הכרטיס שלך ל{event} מצורף",
        "זה הכרטיס שלך ל{event}",
        "שולחים לך את הכרטיס ל{event}",
        "מצורף כרטיס ל{event}",
    ],
    "count": ["(כרטיס {i} מתוך {n})", "- כרטיס {i}/{n}", "[{i}/{n}]", ""],
    "when": ["המשחק ב-{when}.", "נתראה ב-{when}!", "מתחילים ב-{when}.", ""],
    "close": ["יש להציג אותו בכניסה.", "אפשר להציג אותו בכניסה.", "שיהיה זמין לכניסה.", "נתראה במשחק!", "משחק מהנה!", ""],
    "emoji": ["", "", "🏀", "🎟️", "🙂", "👍", ""],
}


PARTS = {"en": _EN, "he": _HE}                    # edited at runtime on the Settings page
DEFAULT_PARTS = {lang: {k: list(v) for k, v in parts.items()} for lang, parts in PARTS.items()}
PART_PLACEHOLDERS = {"greet": ("name",), "body": ("event",), "count": ("i", "n"), "when": ("when",), "close": (), "emoji": ()}


def _first_name(name: str) -> str:
    return (name.split() or [name])[0]


def ticket_caption(name: str, event_name: str, event_date: datetime | None = None, index: int = 1, total: int = 1,
                   lang: str | None = None, rng: random.Random | None = None) -> str:
    """A slightly different message each time (never repeats one of the last dozen)."""
    rng = rng or random
    parts = PARTS["he" if (lang or config.WHATSAPP_MESSAGE_LANG) == "he" else "en"]
    when = event_date.strftime("%a %d/%m at %H:%M") if event_date else ""
    for _ in range(20):
        greet = rng.choice(parts["greet"]).format(name=_first_name(name))
        body = rng.choice(parts["body"]).format(event=event_name)
        count = rng.choice(parts["count"]).format(i=index, n=total) if total > 1 else ""
        sep1 = rng.choice([", ", " - ", ": ", " "]) if not greet.endswith(",") else " "
        head = f"{greet}{sep1}{body}"
        if count:
            head += " " + count
        head += rng.choice([".", ".", "", "!"])
        extra = []
        when_part = rng.choice(parts["when"]).format(when=when) if when else ""
        if when_part:
            extra.append(when_part)
        close = rng.choice(parts["close"])
        if close:
            extra.append(close)
        rng.shuffle(extra)
        glue = rng.choice([" ", "\n", "\n\n"])
        text = head + (glue + " ".join(extra) if extra else "")
        emoji = rng.choice(parts["emoji"])
        if emoji:
            text += rng.choice([" ", ""]) + emoji
        if text not in _RECENT:
            break
    _RECENT.append(text)
    return text


def line_test_text(label: str, rng: random.Random | None = None) -> str:
    rng = rng or random
    return rng.choice(["Line check", "Test message", "Checking in", "Quick test"]) + f" - {label} " + \
        rng.choice(["✓", "OK", "👍", ""]) + f" {rng.randint(1000, 9999)}"


_CHAT = {
    "en": {"hello": ["Hey", "Hi", "Hello", "Hey there", "Hi!"],
           "tail": ["how's it going?", "all good?", "just checking in", "quick check", "anything new?", ""],
           "reply": ["👍", "all good", "got it, thanks", "yep", "received 🙂", "all good here"]},
    "he": {"hello": ["היי", "שלום", "הי", "אהלן", "היי!"],
           "tail": ["מה נשמע?", "הכל טוב?", "בדיקה קטנה", "רק בודקים", "יש חדש?", ""],
           "reply": ["👍", "הכל טוב", "קיבלתי, תודה", "כן", "התקבל 🙂", "הכל בסדר פה"]},
}


def line_chat_text(reply: bool = False, rng: random.Random | None = None) -> str:
    """Short, ordinary-looking message between two of our own numbers (line health test)."""
    rng = rng or random
    parts = _CHAT["he" if config.WHATSAPP_MESSAGE_LANG == "he" else "en"]
    if reply:
        return rng.choice(parts["reply"])
    return (rng.choice(parts["hello"]) + rng.choice([" ", ", "]) + rng.choice(parts["tail"])).strip(" ,")
