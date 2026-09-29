"""Reference lists for the event scheduling form. Edit freely - one name per line.

The teams and arenas are compiled from general knowledge (not fetched live), so review
them once (team names are in Hebrew). Anything missing can be typed via the "Other" option in the form.
"""

HOME_TEAM = "מכבי תל אביב"
VERSUS = "נגד"

# ליגת ווינר - קבוצות יריבות (הקבוצות הנוכחיות והאחרונות בליגה)
OPPONENTS = [
    "הפועל תל אביב",
    "הפועל ירושלים",
    "הפועל חולון",
    "הפועל באר שבע",
    "הפועל חיפה",
    "הפועל אילת",
    "הפועל גלבוע גליל",
    "הפועל גליל עליון",
    "הפועל עפולה",
    "בני הרצליה",
    "עירוני נס ציונה",
    "עירוני קריית אתא",
    "מכבי ראשון לציון",
    "מכבי רעננה",
    "מכבי חיפה",
]

DEFAULT_LOCATION = "Home (Menora Mivtachim Arena)"

ARENAS = [
    DEFAULT_LOCATION,
    "Drive in Arena (Tel Aviv)",
    "Yad Eliyahu Arena (Tel Aviv)",
    "Pais Arena (Jerusalem)",
    "Toto Arena (Holon)",
    "Be'er Sheva Arena",
    "Romema Arena (Haifa)",
    "Begin Sport Hall (Eilat)",
    "Herzliya Arena",
    "Ness Ziona Arena",
    "Kiryat Ata Arena",
    "Rishon LeZion Arena",
    "Ra'anana Sports Hall",
    "Galil Elyon Arena (Kiryat Shmona)",
    "Gilboa Galil Arena",
    "Enerbox Arena (Hadera)",
    "Netanya Arena",
]

OTHER = "__other__"
OTHER_LABEL_OPPONENT = "אחר (הקלידו שם)…"
OTHER_LABEL_LOCATION = "Other (type a venue)…"

# Attendee categories (display order) and the ticket kinds an attendee can hold.
CATEGORIES = ["חבר ארגון", "מצטרף", "פלוס", "פתוח"]
DEFAULT_CATEGORY = "פתוח"
CATEGORY_ALIASES = {
    "member": "חבר ארגון", "organization member": "חבר ארגון", "organisation member": "חבר ארגון",
    "joiner": "מצטרף", "new": "מצטרף", "new member": "מצטרף",
    "plus": "פלוס",
    "open": "פתוח",
}
