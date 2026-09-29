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
    {"name": "Ada Lovelace", "phone": "+1 (555) 010-0001", "ticket_count": 2, "ticket_type": "VIP"},
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
    assert items[0].ticket_type == "VIP"


def test_parse_json_reports_every_bad_row():
    bad = [
        {"name": "", "phone": "15550100001"},
        {"name": "B", "phone": "123"},
        {"name": "C", "phone": "15550100003", "age": 200},
        {"name": "D", "phone": "15550100004", "ticket_count": 0},
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
                       "Tickets": [3, 1], "Tier": ["Sponsor", ""]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    items = services.parse_guest_list("list.xlsx", buf.getvalue())
    assert [(i.name, i.ticket_count, i.ticket_type) for i in items] == [
        ("Ada", 3, "Sponsor"), ("Grace", 1, "Standard")]


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


# ----------------------------------------------------------------- PDF splitting

def test_pdf_split_maps_pages_sequentially(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/tickets/upload",
                   files={"file": ("all.pdf", make_pdf(5), "application/pdf")})
    assert r.status_code == 200 and "attached 5 ticket(s)" in r.text

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


def test_pdf_page_count_mismatch_and_garbage_rejected(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import",
               files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(4))})
    assert r.status_code == 422 and "needs 5" in r.text
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", b"%PDF-garbage")})
    assert r.status_code == 422
    with SessionLocal() as db:
        assert db.query(Ticket).filter_by(event_id=event_id).count() == 0


def test_pdf_upload_requires_guest_list(admin, event_id):
    r = admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(1))})
    assert r.status_code == 422 and "guest list" in r.text


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
    r = doorman.post("/events", data={"name": "X", "event_date": "2030-01-01T10:00"})
    assert r.status_code == 403
    assert doorman.get(f"/events/{event_id}/manage").status_code == 403
    r = doorman.post(f"/events/{event_id}/attendees/import",
                     files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert r.status_code == 403
    r = doorman.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(1))})
    assert r.status_code == 403
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 0
        assert db.query(Event).filter_by(name="X").count() == 0


def test_admin_can_create_event(admin):
    r = admin.post("/events", data={"name": "Fundraiser", "location": "Park",
                                    "event_date": "2031-06-01T18:30"})
    assert r.status_code == 303
    assert "Fundraiser" in admin.get("/events").text
    r = admin.post("/events", data={"name": "  ", "event_date": "bad"})
    assert r.status_code == 422


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

    qr = doorman.get(f"/attendees/{ada}/qr")
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
    assert r.status_code == 200 and "attached 5 ticket(s)" in r.text and "3 extra page(s)" in r.text
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
    r = doorman.get(f"/attendees/{ada}/qr", headers={"host": "192.168.1.20:8000"})
    assert "http://192.168.1.20:8000/tickets/claim/" in r.text and "can't open" not in r.text
    r = doorman.get(f"/attendees/{ada}/qr", headers={"host": "localhost:8000"})
    assert "can&#39;t open" in r.text or "can't open" in r.text
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://tickets.example.org")
    assert "https://tickets.example.org/tickets/claim/" in doorman.get(f"/attendees/{ada}/qr").text


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
