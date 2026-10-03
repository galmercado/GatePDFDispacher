"""System tests: bulk import validation, PDF splitting and RBAC isolation."""
import io
import json
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="gate-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORAGE_DIR"] = f"{_TMP}/storage"
os.environ["EVOLUTION_API_KEY"] = "test-key"

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402

from app import config, services  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Attendee, Event, Role, Ticket, User  # noqa: E402


def make_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=200 + i, height=300)  # width identifies the page
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


GUESTS = [
    {"name": "Ada Lovelace", "phone": "+1 (555) 010-0001", "adult_count": 1, "youth_count": 1, "category": "חבר ארגון"},
    {"name": "Grace Hopper", "phone": "15550100002", "email": "Grace@Navy.mil", "age": 45},
    {"name": "Alan Turing", "phone": "15550100003", "ticket_count": 2, "notes": "Vegetarian"},
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        with SessionLocal() as db:
            db.add(User(email="door@event.local", hashed_password=hash_password("door-pass-1"),
                        full_name="Door Person", role=Role.doorman))
            db.commit()
        yield c


def login(email: str, password: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": email, "password": password})
    assert r.status_code == 303, r.text
    return c


@pytest.fixture(scope="module")
def admin(client):
    return login("admin@event.local", "admin1234")


@pytest.fixture(scope="module")
def doorman(client):
    return login("door@event.local", "door-pass-1")


@pytest.fixture()
def event_id(client):
    with SessionLocal() as db:
        ev = Event(name="Gala", location="Hall", event_date=__import__("datetime").datetime(2030, 1, 1, 19))
        db.add(ev)
        db.commit()
        return ev.id


# ----------------------------------------------------------------- bulk import validation

def test_parse_json_normalises_valid_rows():
    items = services.parse_guest_list("g.json", json.dumps({"attendees": GUESTS}).encode())
    assert [i.phone for i in items] == ["15550100001", "15550100002", "15550100003"]
    assert items[1].email == "grace@navy.mil" and items[1].ticket_count == 1
    assert (items[0].category, items[0].adult_count, items[0].youth_count, items[0].ticket_count) == ("חבר ארגון", 1, 1, 2)
    assert (items[2].adult_count, items[2].youth_count, items[2].category) == (2, 0, "פתוח")  # legacy ticket_count


def test_parse_json_reports_every_bad_row():
    bad = [
        {"name": "", "phone": "15550100001"},
        {"name": "B", "phone": "123"},
        {"name": "C", "phone": "15550100003", "age": 200},
        {"name": "D", "phone": "15550100004", "adult_count": 0, "youth_count": 0},
        {"name": "E", "phone": "15550100005", "email": "nope"},
        {"name": "F", "phone": "15550100006"},
        {"name": "f", "phone": "15550100006"},
    ]
    with pytest.raises(services.ImportValidationError) as exc:
        services.parse_guest_list("g.json", json.dumps(bad).encode())
    text = "\n".join(exc.value.errors)
    for row in ("Row 1", "Row 2", "Row 3", "Row 4", "Row 5", "Row 7"):
        assert row in text
    assert "duplicate" in text and "Row 6" not in text


def test_parse_rejects_bad_files():
    for name, data in [("g.csv", b"a"), ("g.json", b"{oops"), ("g.json", b"[]"),
                       ("g.json", b'{"attendees": 5}'), ("g.xlsx", b"not a workbook")]:
        with pytest.raises(services.ImportValidationError):
            services.parse_guest_list(name, data)


def test_parse_xlsx_with_header_aliases():
    df = pd.DataFrame({"Full Name": ["Ada", "Grace"], "WhatsApp": ["15550100001", "15550100002"],
                       "מבוגר": [3, 1], "נוער": [1, None], "קטגוריה": ["פלוס", ""]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    items = services.parse_guest_list("list.xlsx", buf.getvalue())
    assert [(i.name, i.adult_count, i.youth_count, i.category) for i in items] == [
        ("Ada", 3, 1, "פלוס"), ("Grace", 1, 0, "פתוח")]


def test_parse_xlsx_missing_required_column():
    buf = io.BytesIO()
    pd.DataFrame({"name": ["Ada"]}).to_excel(buf, index=False)
    with pytest.raises(services.ImportValidationError, match="phone"):
        services.parse_guest_list("list.xlsx", buf.getvalue())


def test_import_endpoint_is_atomic(admin, event_id):
    bad = json.dumps([GUESTS[0], {"name": "X", "phone": "1"}]).encode()
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", bad)})
    assert r.status_code == 422 and "Row 2" in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 0
    r = admin.post(f"/events/{event_id}/attendees/import",
                   files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert r.status_code == 200 and "Imported 3 guest(s)" in r.text
    r = admin.post(f"/events/{event_id}/attendees/import",                      # re-import: no duplicates
                   files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert "Imported 0 guest(s). Skipped 3 already on the list." in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 3


# ----------------------------------------------------------------- PDF splitting

def test_pdf_split_maps_pages_sequentially(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/tickets/upload",
                   files={"file": ("all.pdf", make_pdf(5), "application/pdf")})
    assert r.status_code == 200 and "assigned 5 ticket(s)" in r.text

    out = config.STORAGE_DIR / "tickets" / str(event_id)
    files = sorted(out.glob("*.pdf"))
    assert len(files) == 5
    for i, f in enumerate(files):  # each file is one page, in original order
        reader = PdfReader(str(f))
        assert len(reader.pages) == 1
        assert int(reader.pages[0].mediabox.width) == 200 + i

    with SessionLocal() as db:
        by_name = {a.name: a for a in db.query(Attendee).filter_by(event_id=event_id)}
        assert [t.page_number for t in by_name["Ada Lovelace"].tickets] == [1, 2]
        assert [t.page_number for t in by_name["Grace Hopper"].tickets] == [3]
        assert [t.page_number for t in by_name["Alan Turing"].tickets] == [4, 5]
        tokens = [t.claim_token for t in db.query(Ticket).filter_by(event_id=event_id)]
        assert len(set(tokens)) == 5 and all(len(t) == 32 for t in tokens)


def test_pdf_shortfall_is_allowed_and_topped_up_later(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(4))})
    assert r.status_code == 200 and "assigned 4 ticket(s)" in r.text
    assert "1 ticket(s) for 1 guest(s) still have no PDF" in r.text          # prompt about the shortfall
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("b.pdf", make_pdf(2))})
    assert "assigned 1 ticket(s)" in r.text and "1 spare page(s)" in r.text and "still have no PDF" not in r.text
    with SessionLocal() as db:
        pages = sorted(t.page_number for t in db.query(Ticket).filter_by(event_id=event_id))
        assert pages == [1, 2, 3, 4, 5]                                      # later pages continue the numbering
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", b"%PDF-garbage")})
    assert r.status_code == 422


def test_pdf_upload_before_guests_waits_in_pool(admin, event_id):
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(3))})
    assert r.status_code == 200 and "3 spare page(s)" in r.text
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert "Assigned 3 spare ticket page(s)" in r.text and "still have no PDF" in r.text


def test_new_guest_gets_spare_ticket_automatically(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(7))})   # 2 spare
    form = {"name": "Late Guest", "phone": "0501111111", "category": "פתוח", "adult_count": "1", "youth_count": "1"}
    r = admin.post(f"/events/{event_id}/attendees", data=form)
    assert "2 ticket PDF(s) assigned automatically" in r.text and "No spare" not in r.text
    r = admin.post(f"/events/{event_id}/attendees", data={**form, "name": "Later Guest", "phone": "0502222222"})
    assert "No spare ticket PDF left for Later Guest: has 0 of 2" in r.text          # prompt: pool is empty
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("more.pdf", make_pdf(2))})
    assert "assigned 2 ticket(s)" in r.text and "still have no PDF" not in r.text
    with SessionLocal() as db:
        for a in db.query(Attendee).filter_by(event_id=event_id):
            assert len(a.tickets) == a.ticket_count, a.name


def test_removed_or_reduced_guest_frees_pages_for_others(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(5))})
    admin.post(f"/events/{event_id}/attendees", data={"name": "Waiting", "phone": "0503333333",
               "category": "פתוח", "adult_count": "2", "youth_count": "0"})
    with SessionLocal() as db:
        ids = {a.name: a.id for a in db.query(Attendee).filter_by(event_id=event_id)}
        old_token = db.get(Attendee, ids["Ada Lovelace"]).tickets[0].claim_token
    r = admin.post(f"/attendees/{ids['Ada Lovelace']}/delete")                     # frees 2 pages
    assert "reassigned to 2 other ticket(s)" in r.text
    with SessionLocal() as db:
        waiting = db.get(Attendee, ids["Waiting"])
        assert len(waiting.tickets) == 2
        assert old_token not in [t.claim_token for t in waiting.tickets]             # fresh link, old one is dead
    assert TestClient(app).get(f"/tickets/claim/{old_token}").status_code == 404
    # lowering a count returns the surplus page to the pool
    admin.post(f"/attendees/{ids['Alan Turing']}/edit", data={"name": "Alan Turing", "phone": "15550100003",
               "category": "פתוח", "adult_count": "1", "youth_count": "0"})
    with SessionLocal() as db:
        assert len(db.get(Attendee, ids["Alan Turing"]).tickets) == 1
        assert ticket_status_for(db, event_id)["spare"] == 1


def ticket_status_for(db, event_id):
    return services.ticket_status(db, event_id)


def test_replace_upload_starts_over(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(5))})
    with SessionLocal() as db:
        old = {t.claim_token for t in db.query(Ticket).filter_by(event_id=event_id)}
    r = admin.post(f"/events/{event_id}/tickets/upload", data={"replace": "on"},
                   files={"file": ("a.pdf", make_pdf(6))})
    assert "assigned 5 ticket(s)" in r.text and "1 spare page(s)" in r.text
    with SessionLocal() as db:
        assert not old & {t.claim_token for t in db.query(Ticket).filter_by(event_id=event_id)}
        assert len(list((config.STORAGE_DIR / "tickets" / str(event_id)).glob("*.pdf"))) == 6


# ----------------------------------------------------------------- RBAC

def seeded_event(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(5))})
    with SessionLocal() as db:
        return db.query(Attendee).filter_by(event_id=event_id).order_by(Attendee.id).first().id


def test_anonymous_is_redirected_or_rejected(client, event_id):
    anon = TestClient(app, follow_redirects=False)
    r = anon.get("/events")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.post("/attendees/1/toggle", headers={"HX-Request": "true"}).status_code == 401
    assert anon.get(f"/events/{event_id}/search").status_code == 303


def test_doorman_cannot_use_admin_endpoints(admin, doorman, event_id):
    assert doorman.get("/events").status_code == 200
    r = doorman.post("/events", data={"opponent": "הפועל חולון", "event_date": "2030-01-01T10:00"})
    assert r.status_code == 403
    assert doorman.get(f"/events/{event_id}/manage").status_code == 403
    r = doorman.post(f"/events/{event_id}/attendees/import",
                     files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert r.status_code == 403
    r = doorman.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(1))})
    assert r.status_code == 403
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 0
        assert db.query(Event).filter_by(name="מכבי תל אביב נגד הפועל חולון").count() == 0


def test_admin_can_create_event(admin):
    r = admin.post("/events", data={"opponent": "הפועל ירושלים", "event_date": "2031-06-01T18:30"})
    assert r.status_code == 303
    page = admin.get("/events").text
    assert "מכבי תל אביב נגד הפועל ירושלים" in page and __import__("app.data").data.DEFAULT_LOCATION in page
    r = admin.post("/events", data={"opponent": "__other__", "opponent_other": "צלגיריס",
                                    "location": "__other__", "location_other": "Kaunas Hall",
                                    "event_date": "2031-07-01T20:00"})
    assert r.status_code == 303 and "מכבי תל אביב נגד צלגיריס" in admin.get("/events").text
    assert admin.post("/events", data={"opponent": "__other__", "opponent_other": "  ",
                                       "event_date": "2031-07-01T20:00"}).status_code == 422
    assert admin.post("/events", data={"opponent": "הפועל חולון", "event_date": "bad"}).status_code == 422


def test_doorman_cockpit_search_toggle_stats_qr(admin, doorman, event_id):
    ada = seeded_event(admin, event_id)
    page = doorman.get(f"/events/{event_id}/door")
    assert page.status_code == 200 and 'delay:180ms' in page.text

    r = doorman.get(f"/events/{event_id}/search", params={"q": "grace"})
    assert "Grace Hopper" in r.text and "Ada Lovelace" not in r.text
    r = doorman.get(f"/events/{event_id}/search", params={"q": "0100003"})
    assert "Alan Turing" in r.text and "Grace" not in r.text
    r = doorman.get(f"/events/{event_id}/search", params={"q": "%"})
    assert "No guests match" in r.text  # wildcards are escaped

    r = doorman.post(f"/attendees/{ada}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Admitted" in r.text
    assert r.headers["HX-Trigger"] == "refreshStats"
    stats = doorman.get(f"/events/{event_id}/stats").text
    assert 'id="stat-admitted">2<' in stats and 'id="stat-remaining">3<' in stats
    r = doorman.post(f"/attendees/{ada}/toggle", headers={"HX-Request": "true"})
    assert "Waiting" in r.text
    assert 'id="stat-admitted">0<' in doorman.get(f"/events/{event_id}/stats").text

    qr = doorman.post(f"/attendees/{ada}/qr")
    assert qr.text.count("data:image/svg+xml") == 2
    with SessionLocal() as db:
        token = db.get(Attendee, ada).tickets[0].claim_token
    assert token  # claim URL is embedded in the QR payload, not rendered as text


def test_claim_link_is_public_and_unguessable(admin, event_id):
    ada = seeded_event(admin, event_id)
    with SessionLocal() as db:
        token = db.get(Attendee, ada).tickets[0].claim_token
    anon = TestClient(app)
    page = anon.get(f"/tickets/claim/{token}")
    assert page.status_code == 200 and f"/tickets/claim/{token}/download" in page.text
    r = anon.get(f"/tickets/claim/{token}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    direct = anon.get(f"/tickets/claim/{token}/ticket.pdf")
    assert direct.status_code == 200 and direct.content == r.content
    assert direct.headers["content-disposition"].startswith("inline")
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 1
    assert anon.get("/tickets/claim/" + "0" * 32).status_code == 404
    assert anon.get("/tickets/claim/" + "0" * 32 + "/download").status_code == 404


def test_login_logout_and_cookie_flags(client):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": "admin@event.local", "password": "wrong"}).status_code == 401
    r = c.post("/login", data={"email": "admin@event.local", "password": "admin1234"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/events").status_code == 200
    r = c.post("/logout")
    assert r.status_code == 303 and "access_token=" in r.headers["set-cookie"]
    c.cookies.clear()
    assert c.get("/events").status_code == 303


def test_inactive_user_and_forged_token_rejected(client):
    with SessionLocal() as db:
        db.add(User(email="off@event.local", hashed_password=hash_password("pass-1234"),
                    full_name="Off", role=Role.doorman, is_active=False))
        db.commit()
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": "off@event.local", "password": "pass-1234"}).status_code == 401
    c.cookies.set("access_token", "not.a.jwt")
    assert c.get("/events").status_code == 303


# ----------------------------------------------------------------- WhatsApp

def test_whatsapp_sender_payload(tmp_path):
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(make_pdf(1))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(request.url), request.headers["apikey"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"ok": True})

    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await services.send_whatsapp_pdf("+1 555 010 0001", pdf, "hi", client=c)

    asyncio.run(go())
    assert seen["url"].endswith(f"/message/sendMedia/{config.EVOLUTION_INSTANCE}")
    assert seen["key"] == "test-key"
    assert seen["body"]["number"] == "15550100001" and seen["body"]["mediatype"] == "document"
    assert seen["body"]["media"].startswith("JVBER")  # base64 of "%PDF"


def test_whatsapp_endpoint_success_and_failure(admin, doorman, event_id, monkeypatch):
    ada = seeded_event(admin, event_id)
    sent = []

    async def ok(phone, path, caption, file_name="t.pdf", client=None):
        sent.append((phone, Path(path).name))

    monkeypatch.setattr("app.main.send_whatsapp_pdf", ok)
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert r.status_code == 200 and len(sent) == 2 and "Sent 2 tickets" in r.headers["HX-Trigger"]

    async def boom(*a, **k):
        raise services.WhatsAppError("gateway down")

    monkeypatch.setattr("app.main.send_whatsapp_pdf", boom)
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert r.status_code == 200 and "gateway down" in r.headers["HX-Trigger"]


def test_extra_pdf_pages_are_allowed(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(8))})
    assert r.status_code == 200 and "assigned 5 ticket(s)" in r.text and "3 spare page(s)" in r.text
    assert len(list((config.STORAGE_DIR / "tickets" / str(event_id)).glob("*.pdf"))) == 8
    with SessionLocal() as db:
        assert db.query(Ticket).filter_by(event_id=event_id).count() == 5


# ----------------------------------------------------------------- user management

def test_users_page_is_admin_only(admin, doorman):
    assert admin.get("/users").status_code == 200
    assert doorman.get("/users").status_code == 403
    assert doorman.post("/users", data={"email": "x@y.zz", "full_name": "X", "password": "password1",
                                        "role": "admin"}).status_code == 403
    assert TestClient(app, follow_redirects=False).get("/users").status_code == 303


def test_admin_creates_edits_and_deactivates_user(admin):
    r = admin.post("/users", data={"email": "New@Event.local", "full_name": "New Door",
                                   "password": "password1", "role": "doorman"})
    assert r.status_code == 200 and "Created new@event.local" in r.text
    assert admin.post("/users", data={"email": "new@event.local", "full_name": "Dup",
                                      "password": "password1", "role": "doorman"}).status_code == 422
    assert admin.post("/users", data={"email": "s@event.local", "full_name": "S",
                                      "password": "short", "role": "doorman"}).status_code == 422
    login("new@event.local", "password1")
    with SessionLocal() as db:
        uid = db.query(User).filter_by(email="new@event.local").one().id
    r = admin.post(f"/users/{uid}", data={"full_name": "Renamed", "role": "admin",
                                          "is_active": "on", "new_password": "newpass99"})
    assert r.status_code == 200 and "Renamed" in r.text
    promoted = login("new@event.local", "newpass99")
    assert promoted.get("/users").status_code == 200
    admin.post(f"/users/{uid}", data={"full_name": "Renamed", "role": "admin"})  # inactive
    assert promoted.get("/events").status_code == 303  # existing session revoked
    assert TestClient(app).post("/login", data={"email": "new@event.local",
                                                "password": "newpass99"}).status_code == 401


def test_admin_cannot_lock_themselves_out(admin):
    with SessionLocal() as db:
        me = db.query(User).filter_by(email="admin@event.local").one().id
    for data in ({"full_name": "A", "role": "doorman", "is_active": "on"},
                 {"full_name": "A", "role": "admin"}):
        assert admin.post(f"/users/{me}", data=data).status_code == 422
    assert admin.get("/users").status_code == 200


def test_change_own_password(client):
    with SessionLocal() as db:
        db.add(User(email="pw@event.local", hashed_password=hash_password("oldpass11"),
                    full_name="Pw", role=Role.doorman))
        db.commit()
    c = login("pw@event.local", "oldpass11")
    assert c.post("/account/password", data={"current_password": "bad", "new_password": "newpass22"}).status_code == 422
    assert c.post("/account/password", data={"current_password": "oldpass11", "new_password": "x"}).status_code == 422
    assert c.post("/account/password", data={"current_password": "oldpass11", "new_password": "newpass22"}).status_code == 200
    login("pw@event.local", "newpass22")


def test_whatsapp_sends_are_throttled_globally(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_MIN_INTERVAL", 1.0)
    monkeypatch.setattr(config, "WHATSAPP_JITTER", 0.0)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(make_pdf(1))
    stamps = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(__import__("time").monotonic())
        return httpx.Response(201)

    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            # three concurrent requests (e.g. three doormen tapping at once)
            await asyncio.gather(*[
                services.send_whatsapp_pdf(f"1555010000{i}", pdf, "hi", client=c) for i in range(3)])

    asyncio.run(go())
    assert len(stamps) == 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.98 for g in gaps), gaps


def test_whatsapp_interval_has_one_second_floor(monkeypatch):
    import importlib
    monkeypatch.setenv("WHATSAPP_MIN_INTERVAL", "0.1")
    try:
        assert importlib.reload(config).WHATSAPP_MIN_INTERVAL == 1.0
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_visiting_upload_urls_directly_redirects_to_manage(admin, doorman, event_id):
    for path in ("attendees/import", "tickets/upload"):
        r = admin.get(f"/events/{event_id}/{path}")
        assert r.status_code == 303 and r.headers["location"] == f"/events/{event_id}/manage"
        assert doorman.get(f"/events/{event_id}/{path}").status_code == 403


def test_qr_uses_reachable_base_url(admin, doorman, event_id, monkeypatch):
    ada = seeded_event(admin, event_id)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")
    r = doorman.post(f"/attendees/{ada}/qr", headers={"host": "192.168.1.20:8000"})
    assert "http://192.168.1.20:8000/tickets/claim/" in r.text and "/ticket.pdf" in r.text and "can't open" not in r.text
    r = doorman.post(f"/attendees/{ada}/qr", headers={"host": "localhost:8000"})
    assert "can&#39;t open" in r.text or "can't open" in r.text
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://tickets.example.org")
    assert "https://tickets.example.org/tickets/claim/" in doorman.post(f"/attendees/{ada}/qr").text


def test_gateway_error_details_are_surfaced(tmp_path):
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(make_pdf(1))
    body = {"status": 400, "error": "Bad Request",
            "response": {"message": [{"jid": "x@s.whatsapp.net", "exists": False, "number": "15550100001"}]}}
    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(400, json=body))) as c:
            await services.send_whatsapp_pdf("15550100001", pdf, "hi", client=c)

    with pytest.raises(services.WhatsAppError, match="not registered on WhatsApp"):
        asyncio.run(go())


def test_phone_normalisation_defaults_to_israel():
    n = services.normalize_phone
    for raw in ("525607772", "0525607772", "052-560-7772", "972525607772", "+972 52-560-7772",
                "00972525607772", "+972 (0)52-560-7772", "972 52 560 7772"):
        assert n(raw) == "972525607772", raw
    assert n("+1 (555) 010-0001") == "15550100001"       # explicit "+" is left alone
    assert n("15550100001") == "15550100001"              # long, no "+": assumed international
    assert n(n("0525607772")) == n("0525607772")          # idempotent


def test_import_and_search_use_normalised_phones(admin, doorman, event_id):
    guests = [{"name": "Dana Levi", "phone": "052-5607772"}, {"name": "Noa Bar", "phone": "525607773"}]
    r = admin.post(f"/events/{event_id}/attendees/import",
                   files={"file": ("g.json", json.dumps(guests).encode())})
    assert r.status_code == 200
    with SessionLocal() as db:
        phones = sorted(a.phone for a in db.query(Attendee).filter_by(event_id=event_id))
    assert phones == ["972525607772", "972525607773"]
    for q in ("0525607772", "525607772", "972525607772"):
        assert "Dana Levi" in doorman.get(f"/events/{event_id}/search", params={"q": q}).text


def test_whatsapp_and_qr_admit_the_guest(admin, doorman, event_id, monkeypatch):
    ada = seeded_event(admin, event_id)

    async def boom(*a, **k):
        raise services.WhatsAppError("gateway down")

    monkeypatch.setattr("app.main.send_whatsapp_pdf", boom)
    r = doorman.post(f"/attendees/{ada}/whatsapp")       # admitted even though the send failed
    assert "Admitted" in r.text and "refreshStats" in r.headers["HX-Trigger"] and "toast" in r.headers["HX-Trigger"]
    with SessionLocal() as db:
        first = db.get(Attendee, ada).checked_in_at
        assert db.get(Attendee, ada).checked_in
    doorman.post(f"/attendees/{ada}/toggle", headers={"HX-Request": "true"})   # undo
    r = doorman.post(f"/attendees/{ada}/qr")             # QR re-admits and refreshes the card out-of-band
    assert 'hx-swap-oob="true"' in r.text and "Admitted" in r.text
    assert r.headers["HX-Trigger"] == "refreshStats"
    r = doorman.post(f"/attendees/{ada}/qr")             # idempotent: never un-admits
    with SessionLocal() as db:
        assert db.get(Attendee, ada).checked_in and db.get(Attendee, ada).checked_in_at >= first
    assert 'id="stat-admitted">2<' in doorman.get(f"/events/{event_id}/stats").text


def test_manual_attendee_add_edit_delete(admin, doorman, event_id):
    form = {"name": "Yael Cohen", "phone": "050-1234567", "adult_count": "2", "youth_count": "1",
            "category": "פלוס", "email": "", "age": "34", "notes": "Aisle seat"}
    r = admin.post(f"/events/{event_id}/attendees", data=form)
    assert r.status_code == 200 and "Added Yael Cohen" in r.text
    with SessionLocal() as db:
        a = db.query(Attendee).filter_by(event_id=event_id, name="Yael Cohen").one()
        assert a.phone == "972501234567" and (a.adult_count, a.youth_count, a.ticket_count) == (2, 1, 3)
        assert a.age == 34 and a.email is None and a.category == "פלוס"
        aid = a.id
    assert admin.post(f"/events/{event_id}/attendees", data=form).status_code == 422   # duplicate
    assert admin.post(f"/events/{event_id}/attendees", data={**form, "name": "", "phone": "1"}).status_code == 422

    r = admin.post(f"/attendees/{aid}/edit", data={**form, "name": "Yael Levi", "adult_count": "1", "youth_count": "1", "category": "מצטרף"})
    assert r.status_code == 200 and "Updated Yael Levi" in r.text
    assert admin.post(f"/attendees/{aid}/edit", data={**form, "age": "999"}).status_code == 422
    assert admin.post(f"/attendees/{aid}/edit", data={**form, "adult_count": "0", "youth_count": "0"}).status_code == 422
    assert admin.post(f"/attendees/{aid}/edit", data={**form, "category": "nope"}).status_code == 422
    with SessionLocal() as db:
        assert db.get(Attendee, aid).name == "Yael Levi" and db.get(Attendee, aid).ticket_count == 2
        assert db.get(Attendee, aid).category == "מצטרף"

    for c in (doorman,):
        assert c.post(f"/events/{event_id}/attendees", data=form).status_code == 403
        assert c.post(f"/attendees/{aid}/edit", data=form).status_code == 403
        assert c.post(f"/attendees/{aid}/delete").status_code == 403

    r = admin.post(f"/attendees/{aid}/delete")
    assert r.status_code == 200 and "Removed Yael Levi" in r.text
    with SessionLocal() as db:
        assert db.get(Attendee, aid) is None


def test_door_groups_and_counts_by_category(admin, doorman, event_id):
    guests = [
        {"name": "Member One", "phone": "0521111111", "category": "חבר ארגון", "adult_count": 2, "youth_count": 1},
        {"name": "Joiner One", "phone": "0522222222", "category": "מצטרף", "adult_count": 1},
        {"name": "Plus One", "phone": "0523333333", "category": "פלוס", "youth_count": 2},
        {"name": "Open One", "phone": "0524444444", "category": "פתוח", "adult_count": 1, "youth_count": 1},
        {"name": "Member Two", "phone": "0525555555", "category": "חבר ארגון", "adult_count": 1},
    ]
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(guests).encode())})
    assert r.status_code == 200, r.text
    page = doorman.get(f"/events/{event_id}/door").text
    order = [page.index(f'aria-label="{c}"') for c in ("חבר ארגון", "מצטרף", "פלוס", "פתוח")]
    assert order == sorted(order)                                   # four separate sections, in order
    with SessionLocal() as db:
        ids = {a.name: a.id for a in db.query(Attendee).filter_by(event_id=event_id)}
    doorman.post(f"/attendees/{ids['Member One']}/toggle", headers={"HX-Request": "true"})   # 3 tickets
    doorman.post(f"/attendees/{ids['Plus One']}/qr")                                          # 2 youth
    from app.main import compute_stats
    with SessionLocal() as db:
        st = compute_stats(db, event_id)
    assert (st["total"], st["admitted"], st["remaining"]) == (9, 5, 4)          # all types together
    assert (st["adult_total"], st["adult_in"], st["youth_total"], st["youth_in"]) == (5, 2, 4, 3)
    cats = {c["name"]: (c["admitted"], c["total"]) for c in st["categories"]}
    assert cats == {"חבר ארגון": (3, 4), "מצטרף": (0, 1), "פלוס": (2, 2), "פתוח": (0, 2)}
    assert 'id="stat-admitted">5<' in doorman.get(f"/events/{event_id}/stats").text
    assert "<b" in doorman.get(f"/events/{event_id}/stats/category/0").text
    assert doorman.get(f"/events/{event_id}/stats/category/9").status_code == 404
    r = doorman.get(f"/events/{event_id}/search", params={"q": "plus"})
    assert 'aria-label="פלוס"' in r.text and 'aria-label="חבר ארגון"' not in r.text


def test_migration_adds_category_and_ticket_kind_columns(tmp_path):
    from sqlalchemy import create_engine, text
    from app.database import migrate
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE attendees (id INTEGER PRIMARY KEY, event_id INTEGER, name TEXT, phone TEXT, "
                       "ticket_type TEXT NOT NULL, ticket_count INTEGER NOT NULL, checked_in BOOLEAN)"))
        c.execute(text("INSERT INTO attendees VALUES (1, 1, 'Old Guest', '972521', 'VIP', 3, 0)"))
    migrate(eng)
    migrate(eng)  # idempotent
    with eng.begin() as c:
        row = c.execute(text("SELECT category, adult_count, youth_count, ticket_count FROM attendees")).one()
    assert tuple(row) == ("פתוח", 3, 0, 3)


# ----------------------------------------------------------------- Google sign-in

@pytest.fixture()
def google_on(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")


def test_google_disabled_by_default(client):
    c = TestClient(app, follow_redirects=False)
    assert "Sign in with Google" not in c.get("/login").text
    assert c.get("/auth/google/login").status_code == 404
    assert c.get("/auth/google/callback?code=x&state=y").status_code == 404


def test_google_login_flow_only_admits_registered_users(client, google_on, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    c = TestClient(app, follow_redirects=False)
    assert "Sign in with Google" in c.get("/login").text
    r = c.get("/auth/google/login")
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["redirect_uri"] == ["http://testserver/auth/google/callback"] and q["scope"] == ["openid email profile"]
    state = q["state"][0]
    assert "httponly" in r.headers["set-cookie"].lower()

    async def registered(code, redirect_uri, client=None):
        return {"email": "door@event.local", "name": "Door Person"}

    async def stranger(code, redirect_uri, client=None):
        return {"email": "stranger@gmail.com", "name": "Nope"}

    monkeypatch.setattr("app.main.fetch_profile", registered)
    assert c.get("/auth/google/callback", params={"code": "c", "state": "forged"}).status_code == 400  # bad state
    state = parse_qs(urlsplit(c.get("/auth/google/login").headers["location"]).query)["state"][0]  # failure burns the state
    r = c.get("/auth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/events"
    assert c.get("/events").status_code == 200

    monkeypatch.setattr("app.main.fetch_profile", stranger)
    c2 = TestClient(app, follow_redirects=False)
    state2 = parse_qs(urlsplit(c2.get("/auth/google/login").headers["location"]).query)["state"][0]
    r = c2.get("/auth/google/callback", params={"code": "c", "state": state2})
    assert r.status_code == 403 and "not authorised" in r.text
    assert c2.get("/events").status_code == 303                                   # no session issued
    assert TestClient(app, follow_redirects=False).get("/auth/google/callback",
                                                       params={"code": "c", "state": state2}).status_code == 400  # no cookie


def test_google_profile_exchange_requires_verified_email(google_on):
    import asyncio

    def make(verified):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"email": "A@Gmail.com", "email_verified": verified, "name": "A"})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    from app import google_auth

    async def go(verified):
        async with make(verified) as c:
            return await google_auth.fetch_profile("code", "http://x/cb", client=c)

    assert asyncio.run(go(True)) == {"email": "a@gmail.com", "name": "A"}
    with pytest.raises(google_auth.GoogleAuthError):
        asyncio.run(go(False))


def test_admin_can_create_google_only_user(admin, google_on):
    r = admin.post("/users", data={"email": "g@event.local", "full_name": "G", "password": "", "role": "doorman"})
    assert r.status_code == 200 and "Created g@event.local" in r.text
    assert TestClient(app).post("/login", data={"email": "g@event.local", "password": ""}).status_code in (401, 422)


def test_password_required_when_google_not_configured(admin):
    r = admin.post("/users", data={"email": "p@event.local", "full_name": "P", "password": "", "role": "doorman"})
    assert r.status_code == 422


# ----------------------------------------------------------------- WhatsApp message deletion

def test_gateway_returns_message_ref_and_deletes_for_everyone(tmp_path):
    import asyncio
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(make_pdf(1))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.method == "POST":
            return httpx.Response(201, json={"key": {"remoteJid": "972501234567@s.whatsapp.net", "fromMe": True, "id": "3EB0ABC"}})
        return httpx.Response(200, json={"status": "SUCCESS"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            ref = await services.send_whatsapp_pdf("0501234567", pdf, "hi", client=c)
            await services.delete_whatsapp_message(ref.remote_jid, ref.message_id, client=c)
            return ref

    ref = asyncio.run(go())
    assert tuple(ref) == ("972501234567@s.whatsapp.net", "3EB0ABC")
    method, path, body = calls[1]
    assert (method, path) == ("DELETE", f"/chat/deleteMessageForEveryone/{config.EVOLUTION_INSTANCE}")
    assert body == {"id": "3EB0ABC", "remoteJid": "972501234567@s.whatsapp.net", "fromMe": True}


@pytest.fixture()
def wa(monkeypatch):
    """Fake gateway: records sends and deletes; deletes can be made to fail."""
    state = {"sent": [], "deleted": [], "fail_delete": False, "n": 0}

    async def send(phone, path, caption, file_name="t.pdf", client=None):
        state["n"] += 1
        ref = services.SentRef(f"{phone}@s.whatsapp.net", f"MSG{state['n']}")
        state["sent"].append(ref.message_id)
        return ref

    async def delete(remote_jid, message_id, client=None):
        if state["fail_delete"]:
            raise services.WhatsAppError("gateway down")
        state["deleted"].append(message_id)

    monkeypatch.setattr("app.main.send_whatsapp_pdf", send)
    monkeypatch.setattr("app.main.delete_whatsapp_message", delete)
    from app.models import SentMessage
    with SessionLocal() as db:                      # isolate from messages left behind by other tests
        db.query(SentMessage).update({"status": "expired"})
        db.commit()
    return state


def message_status(attendee_id):
    from app.models import SentMessage
    with SessionLocal() as db:
        return {m.message_id: m.status for m in db.query(SentMessage).filter_by(attendee_id=attendee_id)}


def sent_rows(attendee_id):
    from app.models import SentMessage
    with SessionLocal() as db:
        return {m.message_id: m for m in db.query(SentMessage).filter_by(attendee_id=attendee_id)}


def ticket_tokens(attendee_id):
    with SessionLocal() as db:
        return [t.claim_token for t in db.get(Attendee, attendee_id).tickets]


def toggle(doorman, attendee_id):
    return doorman.post(f"/attendees/{attendee_id}/toggle", headers={"HX-Request": "true"})


def test_admitting_and_resending_delete_nothing(admin, doorman, event_id, wa):
    ada = seeded_event(admin, event_id)
    doorman.post(f"/attendees/{ada}/whatsapp")
    doorman.post(f"/attendees/{ada}/whatsapp")
    doorman.post(f"/attendees/{ada}/qr")
    assert wa["sent"] == ["MSG1", "MSG2", "MSG3", "MSG4"] and wa["deleted"] == []


def test_unadmit_deletes_that_orders_message_and_disables_its_qr_link(admin, doorman, event_id, wa):
    ada = seeded_event(admin, event_id)                        # Ada has 2 tickets
    doorman.post(f"/attendees/{ada}/whatsapp")                 # admits + sends MSG1, MSG2
    old = ticket_tokens(ada)
    assert TestClient(app).get(f"/tickets/claim/{old[0]}/ticket.pdf").status_code == 200
    r = toggle(doorman, ada)                                   # un-admit
    assert "Waiting" in r.text and "deleted from WhatsApp" in r.headers["HX-Trigger"]
    assert wa["deleted"] == ["MSG1", "MSG2"]
    assert {m.status for m in sent_rows(ada).values()} == {"deleted"}
    anon = TestClient(app)
    for token in old:                                          # the old QR / links are dead
        assert anon.get(f"/tickets/claim/{token}/ticket.pdf").status_code == 404
        assert anon.get(f"/tickets/claim/{token}").status_code == 404
    new = ticket_tokens(ada)
    assert not set(new) & set(old)
    qr = doorman.post(f"/attendees/{ada}/qr")                  # a fresh QR works again
    assert new[0] in qr.text and anon.get(f"/tickets/claim/{new[0]}/ticket.pdf").status_code == 200
    assert toggle(doorman, ada).status_code == 200             # admitting afterwards deletes nothing more
    assert wa["deleted"] == ["MSG1", "MSG2"]


def test_unadmit_only_touches_that_order_even_if_the_phone_is_shared(admin, doorman, event_id, wa):
    guests = [{"name": "Order One", "phone": "0509999999", "adult_count": 1},
              {"name": "Order Two", "phone": "0509999999", "adult_count": 1}]
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(guests).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(2))})
    with SessionLocal() as db:
        ids = {a.name: a.id for a in db.query(Attendee).filter_by(event_id=event_id)}
    doorman.post(f"/attendees/{ids['Order One']}/whatsapp")    # MSG1
    doorman.post(f"/attendees/{ids['Order Two']}/whatsapp")    # MSG2, both admitted
    two_token = ticket_tokens(ids["Order Two"])[0]
    toggle(doorman, ids["Order One"])                          # un-admit order one only
    assert wa["deleted"] == ["MSG1"]
    assert sent_rows(ids["Order Two"])["MSG2"].status == "sent"                       # untouched
    assert TestClient(app).get(f"/tickets/claim/{two_token}/ticket.pdf").status_code == 200
    assert "Admitted" in doorman.get(f"/events/{event_id}/search", params={"q": "Order Two"}).text


def test_failed_unadmit_deletion_is_retried_later(admin, doorman, event_id, wa):
    import asyncio
    from datetime import timedelta
    from app.main import cleanup_due_messages
    from app.models import utcnow
    ada = seeded_event(admin, event_id)
    doorman.post(f"/attendees/{ada}/whatsapp")
    wa["fail_delete"] = True
    r = toggle(doorman, ada)
    assert "Waiting" in r.text and "could not be deleted yet" in r.headers["HX-Trigger"]
    assert {m.status for m in sent_rows(ada).values()} == {"sent"}
    assert ticket_tokens(ada)                                                         # QR link is dead regardless
    wa["fail_delete"] = False
    # the failed deletion is not retried immediately (no hammering) ...
    assert asyncio.run(cleanup_due_messages()) == 0
    # ... a retry is scheduled 30-90 minutes ahead (random), and then it succeeds
    from app.models import SentMessage
    for m in sent_rows(ada).values():
        assert timedelta(minutes=29) <= m.delete_at - utcnow() <= timedelta(minutes=91)
    assert asyncio.run(cleanup_due_messages(now=utcnow() + timedelta(hours=2))) == 2
    assert {m.status for m in sent_rows(ada).values()} == {"deleted"}


def test_messages_are_scheduled_12_to_36_hours_after_the_event(admin, doorman, event_id, wa):
    from datetime import datetime, timedelta
    ada = seeded_event(admin, event_id)       # event_date: 2030-01-01 19:00 Israel time (UTC+2)
    doorman.post(f"/attendees/{ada}/whatsapp")
    end_utc = datetime(2030, 1, 1, 19, 0) - timedelta(hours=2) + timedelta(hours=config.EVENT_DURATION_HOURS)
    for m in sent_rows(ada).values():
        assert end_utc + timedelta(hours=12) <= m.delete_at <= end_utc + timedelta(hours=36)


def test_event_end_conversion_and_random_spread():
    from datetime import datetime, timedelta
    from app.main import event_end_utc, schedule_delete_at
    from app.models import Event
    ev = Event(name="x", location="y", event_date=datetime(2030, 7, 1, 20, 0))      # summer: UTC+3
    assert event_end_utc(ev) == datetime(2030, 7, 1, 17, 0) + timedelta(hours=config.EVENT_DURATION_HOURS)
    times = [schedule_delete_at(ev) for _ in range(50)]
    assert len({t for t in times}) > 40                                              # spread out, not one instant
    lo, hi = event_end_utc(ev) + timedelta(hours=12), event_end_utc(ev) + timedelta(hours=36)
    assert all(lo <= t <= hi for t in times)
    past = Event(name="x", location="y", event_date=datetime(2020, 1, 1, 20, 0))      # already over: counts from now
    from app.models import utcnow
    assert schedule_delete_at(past) >= utcnow() + timedelta(hours=12)


def test_cleanup_deletes_due_messages_only_and_gives_up_after_47h(admin, doorman, event_id, wa, monkeypatch):
    import asyncio
    from datetime import timedelta
    from app.main import cleanup_due_messages
    from app.models import SentMessage, utcnow
    monkeypatch.setattr(config, "WHATSAPP_DELETE_GAP", (0.0, 0.0))
    ada = seeded_event(admin, event_id)
    doorman.post(f"/attendees/{ada}/whatsapp")                        # event in 2030: nothing is due
    assert asyncio.run(cleanup_due_messages()) == 0 and wa["deleted"] == []
    rows = sent_rows(ada)
    m1, m2 = rows["MSG1"], rows["MSG2"]
    with SessionLocal() as db:
        db.get(SentMessage, m1.id).delete_at = utcnow() - timedelta(minutes=5)      # due
        db.get(SentMessage, m2.id).delete_at = utcnow() + timedelta(hours=5)        # later
        db.commit()
    assert asyncio.run(cleanup_due_messages()) == 1 and wa["deleted"] == ["MSG1"]
    with SessionLocal() as db:                                         # too old for WhatsApp to delete
        row = db.get(SentMessage, m2.id)
        row.sent_at, row.delete_at = utcnow() - timedelta(hours=48), utcnow() - timedelta(minutes=1)
        db.commit()
    assert asyncio.run(cleanup_due_messages()) == 0
    assert sent_rows(ada)["MSG2"].status == "expired"


def test_legacy_messages_without_schedule_get_one(admin, doorman, event_id, wa):
    import asyncio
    from app.main import cleanup_due_messages
    from app.models import SentMessage
    ada = seeded_event(admin, event_id)
    doorman.post(f"/attendees/{ada}/whatsapp")
    with SessionLocal() as db:
        db.query(SentMessage).filter_by(attendee_id=ada).update({"delete_at": None})
        db.commit()
    asyncio.run(cleanup_due_messages())
    assert all(m.delete_at is not None for m in sent_rows(ada).values())


def test_deletion_switches(admin, doorman, event_id, wa, monkeypatch):
    import asyncio
    from datetime import timedelta
    from app.main import cleanup_due_messages
    from app.models import SentMessage, utcnow
    ada = seeded_event(admin, event_id)
    doorman.post(f"/attendees/{ada}/whatsapp")
    monkeypatch.setattr(config, "WHATSAPP_DELETE_ON_UNADMIT", False)
    r = toggle(doorman, ada)
    assert wa["deleted"] == [] and "QR link was disabled" in r.headers["HX-Trigger"]
    monkeypatch.setattr(config, "WHATSAPP_DELETE_AFTER_EVENT", False)
    with SessionLocal() as db:
        db.query(SentMessage).update({"delete_at": utcnow() - timedelta(hours=1)})
        db.commit()
    assert asyncio.run(cleanup_due_messages()) == 0 and wa["deleted"] == []


def test_removing_an_order_pulls_only_its_messages(admin, doorman, event_id, wa):
    ada = seeded_event(admin, event_id)
    with SessionLocal() as db:
        alan = [a.id for a in db.query(Attendee).filter_by(event_id=event_id, name="Alan Turing")][0]
    doorman.post(f"/attendees/{ada}/whatsapp")                         # MSG1, MSG2
    doorman.post(f"/attendees/{alan}/whatsapp")                        # MSG3, MSG4
    admin.post(f"/attendees/{ada}/delete")
    assert wa["deleted"] == ["MSG1", "MSG2"] and sent_rows(alan)["MSG3"].status == "sent"


def test_migration_adds_delete_at(tmp_path):
    from sqlalchemy import create_engine, text
    from app.database import migrate
    eng = create_engine(f"sqlite:///{tmp_path}/old2.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE sent_messages (id INTEGER PRIMARY KEY, event_id INTEGER, attendee_id INTEGER, "
                       "remote_jid TEXT, message_id TEXT, sent_at DATETIME, status TEXT, attempts INTEGER, deleted_at DATETIME)"))
    migrate(eng)
    migrate(eng)
    with eng.begin() as c:
        c.execute(text("SELECT delete_at FROM sent_messages"))
