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
    result = services.parse_guest_list("g.json", json.dumps({"attendees": GUESTS}).encode())
    items = result.valid
    assert result.issues == []
    assert [i.phone for i in items] == ["15550100001", "15550100002", "15550100003"]
    assert items[1].email == "grace@navy.mil" and items[1].ticket_count == 1
    assert (items[0].category, items[0].adult_count, items[0].youth_count, items[0].ticket_count) == ("חבר ארגון", 1, 1, 2)
    assert (items[2].adult_count, items[2].youth_count, items[2].category) == (2, 0, "פתוח")  # legacy ticket_count


def test_parse_json_separates_good_rows_from_every_bad_row():
    rows = [
        {"name": "", "phone": "15550100001"},                       # row 1
        {"name": "B", "phone": "123"},                              # row 2
        {"name": "C", "phone": "15550100003", "age": 200},          # row 3
        {"name": "D", "phone": "15550100004", "adult_count": 0, "youth_count": 0},   # row 4
        {"name": "E", "phone": "15550100005", "email": "nope"},     # row 5
        {"name": "F", "phone": "15550100006"},                      # row 6 - ok
        {"name": "f", "phone": "15550100006"},                      # row 7 - duplicate of 6
        {"name": "G", "phone": "15550100007", "age": "abc"},        # row 8
        {"name": "H", "phone": "15550100008", "age": "20.5"},       # row 9 - ok
    ]
    result = services.parse_guest_list("g.json", json.dumps(rows).encode())
    assert [v.name for v in result.valid] == ["F", "H"] and result.valid[1].age == 20.5
    assert [i.label for i in result.issues] == ["Row 1", "Row 2", "Row 3", "Row 4", "Row 5", "Row 7", "Row 8"]
    by = {i.label: " ".join(i.errors) for i in result.issues}
    assert "name" in by["Row 1"] and "phone" in by["Row 2"] and "age" in by["Row 3"] and "tickets" in by["Row 4"]
    assert "duplicate" in by["Row 7"] and "number" in by["Row 8"]
    assert result.issues[1].row["name"] == "B"                                  # raw values kept for the fix form


def test_age_accepts_decimals():
    ok = lambda v: services.AttendeeIn(name="A", phone="0501234567", age=v).age   # noqa: E731
    assert ok(20.5) == 20.5 and ok("20.5") == 20.5 and ok("20,5") == 20.5 and ok(20) == 20.0 and ok("") is None
    for bad in ("abc", "-1", "121", "nan", "inf"):
        with pytest.raises(Exception):
            ok(bad)


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
    items = services.parse_guest_list("list.xlsx", buf.getvalue()).valid
    assert [(i.name, i.adult_count, i.youth_count, i.category) for i in items] == [
        ("Ada", 3, 1, "פלוס"), ("Grace", 1, 0, "פתוח")]


def test_parse_xlsx_missing_required_column():
    buf = io.BytesIO()
    pd.DataFrame({"name": ["Ada"]}).to_excel(buf, index=False)
    with pytest.raises(services.ImportValidationError, match="phone"):
        services.parse_guest_list("list.xlsx", buf.getvalue())


def test_import_keeps_good_rows_and_offers_fix_or_skip(admin, event_id):
    from app.models import ImportIssue
    rows = [GUESTS[0], {"name": "Bad Phone", "phone": "1", "age": "31.5"}, GUESTS[1],
            {"name": "Bad Age", "phone": "0501111111", "age": "x"}]
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(rows).encode())})
    assert r.status_code == 200 and "Imported 2 guest(s)" in r.text
    assert "2 row(s) need fixing" in r.text and "NOT imported" in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 2
        issues = db.query(ImportIssue).filter_by(event_id=event_id).order_by(ImportIssue.id).all()
        assert [i.label for i in issues] == ["Row 2", "Row 4"]
        bad_phone, bad_age = issues[0].id, issues[1].id
    # fixing with a still-invalid value keeps the row and shows the new problem
    form = {"name": "Bad Phone", "phone": "12", "age": "31.5", "category": "מצטרף", "adult_count": "1", "youth_count": "0"}
    r = admin.post(f"/import-issues/{bad_phone}/fix", data=form)
    assert r.status_code == 422 and "need fixing" in r.text
    # a correct fix adds the guest (decimal age kept) and removes the row from the list
    r = admin.post(f"/import-issues/{bad_phone}/fix", data={**form, "phone": "052-1234567"})
    assert r.status_code == 200 and "Added Bad Phone" in r.text and "1 row(s) need fixing" in r.text
    with SessionLocal() as db:
        g = db.query(Attendee).filter_by(event_id=event_id, name="Bad Phone").one()
        assert g.age == 31.5 and g.phone == "972521234567" and g.category == "מצטרף"
        assert db.get(ImportIssue, bad_phone) is None
    # skipping drops the row without adding anyone
    r = admin.post(f"/import-issues/{bad_age}/skip")
    assert r.status_code == 200 and "Skipped Row 4" in r.text and "need fixing" not in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 3
        assert db.query(ImportIssue).filter_by(event_id=event_id).count() == 0
    assert admin.post(f"/import-issues/{bad_age}/skip").status_code == 404           # already handled


def test_skip_all_and_permissions_for_import_issues(admin, doorman, event_id):
    rows = [{"name": "", "phone": "1"}, {"name": "X", "phone": "2"}, GUESTS[0]]
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(rows).encode())})
    from app.models import ImportIssue
    with SessionLocal() as db:
        first = db.query(ImportIssue).filter_by(event_id=event_id).first().id
    assert doorman.post(f"/import-issues/{first}/skip").status_code == 403
    assert doorman.post(f"/import-issues/{first}/fix", data={"name": "A"}).status_code == 403
    assert doorman.post(f"/events/{event_id}/import-issues/skip-all").status_code == 403
    r = admin.post(f"/events/{event_id}/import-issues/skip-all")
    assert "Skipped 2 row(s)" in r.text
    with SessionLocal() as db:
        assert db.query(ImportIssue).filter_by(event_id=event_id).count() == 0
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 1


def test_unreadable_file_still_fails_completely(admin, event_id):
    for name, data in (("g.json", b"{oops"), ("g.csv", b"a,b"), ("g.json", b"[]")):
        r = admin.post(f"/events/{event_id}/attendees/import", files={"file": (name, data)})
        assert r.status_code == 422
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 0


def test_re_import_does_not_duplicate_and_decimal_age_in_forms(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS).encode())})
    assert "Imported 0 guest(s)." in r.text and "3 existing guest(s) already up to date" in r.text
    form = {"name": "Age Test", "phone": "0507777777", "category": "פתוח", "adult_count": "1", "youth_count": "0", "age": "19.5"}
    assert admin.post(f"/events/{event_id}/attendees", data=form).status_code == 200
    page = admin.get(f"/events/{event_id}/manage").text
    assert 'value="19.5"' in page and "19.5.0" not in page
    from app.main import format_age
    assert (format_age(20.0), format_age(20.5), format_age(None), format_age(7)) == ("20", "20.5", "", "7")


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

    async def ok(phone, path, caption, file_name="t.pdf", client=None, instance=None):
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

    async def send(phone, path, caption, file_name="t.pdf", client=None, instance=None):
        state["n"] += 1
        ref = services.SentRef(f"{phone}@s.whatsapp.net", f"MSG{state['n']}")
        state["sent"].append(ref.message_id)
        return ref

    async def delete(remote_jid, message_id, client=None, instance=None):
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


def test_json_keys_use_the_same_aliases_as_spreadsheet_columns():
    rows = [
        {"name": "A", "phone": "0501234567", "adult": 3, "youth": 1},
        {"Name": "B", "Phone": "0501234568", "Adults": "4", "category": "plus"},
        {"full_name": "C", "mobile": "0501234569", "Adult Count": 2, "Youth-Count": 2, "מבוגר": 2},
        {"name": "D", "phone": "0501234560", "tickets": 5},                   # legacy total -> adults
        {"name": "E", "phone": "0501234561", "מבוגר": 1, "נוער": 2, "סוג": "מצטרף"},
        {"name": "F", "phone": "0501234562", "adult_count": 0, "youth_count": 2},   # zero adults is valid
    ]
    result = services.parse_guest_list("g.json", json.dumps(rows).encode())
    got = [(v.name, v.adult_count, v.youth_count, v.ticket_count, v.category) for v in result.valid]
    assert got == [("A", 3, 1, 4, "פתוח"), ("B", 4, 0, 4, "פלוס"), ("C", 2, 2, 4, "פתוח"),
                   ("D", 5, 0, 5, "פתוח"), ("E", 1, 2, 3, "מצטרף"), ("F", 0, 2, 2, "פתוח")]
    assert result.issues == [] and result.ignored == []


def test_unrecognised_fields_are_reported_not_silently_dropped(admin, event_id):
    rows = [{"name": "A", "phone": "0501234567", "grown ups": 3, "kids": 1}]
    assert services.parse_guest_list("g.json", json.dumps(rows).encode()).ignored == ["grown ups", "kids"]
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(rows).encode())})
    assert r.status_code == 200 and "not recognised and were ignored: grown ups, kids" in r.text
    buf = io.BytesIO()
    pd.DataFrame({"name": ["X"], "phone": ["0507654321"], "Adults": [3], "Kids": [1]}).to_excel(buf, index=False)
    res = services.parse_guest_list("l.xlsx", buf.getvalue())
    assert res.valid[0].adult_count == 3 and res.ignored == ["Kids"]


def test_json_alias_counts_reach_the_database(admin, event_id):
    rows = [{"name": "Big Group", "phone": "0501234567", "adult": 3, "youth": 2}]
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(rows).encode())})
    with SessionLocal() as db:
        g = db.query(Attendee).filter_by(event_id=event_id, name="Big Group").one()
        assert (g.adult_count, g.youth_count, g.ticket_count) == (3, 2, 5)


def test_reimport_updates_existing_guests_but_only_provided_fields(admin, event_id):
    base = [{"name": "Dana Levi", "phone": "0501111111", "adult": 1, "youth": 0, "category": "פתוח", "age": 30, "notes": "keep me"},
            {"name": "Noa Bar", "phone": "0502222222", "adult": 2, "youth": 1}]
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(base).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(4))})
    again = [{"name": "Dana Levi", "phone": "0501111111", "adult": 4, "youth": 2, "category": "פלוס", "age": 30.5},   # changed
             {"name": "Noa Bar", "phone": "0502222222", "adult": 2},                  # same adults; youth not given -> keep
             {"name": "New Person", "phone": "0503333333", "adult": 1}]
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(again).encode())})
    assert "Imported 1 guest(s)." in r.text and "Updated 1 existing guest(s)" in r.text
    assert "tickets adult 1→4, youth 0→2" in r.text and "category פתוח→פלוס" in r.text and "1 existing guest(s) already up to date" in r.text
    with SessionLocal() as db:
        dana = db.query(Attendee).filter_by(event_id=event_id, name="Dana Levi").one()
        assert (dana.adult_count, dana.youth_count, dana.ticket_count, dana.category, dana.age) == (4, 2, 6, "פלוס", 30.5)
        assert dana.notes == "keep me"                                              # not in the file -> untouched
        noa = db.query(Attendee).filter_by(event_id=event_id, name="Noa Bar").one()
        assert (noa.adult_count, noa.youth_count) == (2, 1)
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 3
        # ticket PDFs follow the new counts: 7 pages needed now (6 + 0 + 3?), pool had none spare
        assert len(dana.tickets) <= dana.ticket_count
    assert "still have no PDF" in r.text                                             # shortfall is flagged


def test_reimport_can_lower_tickets_and_frees_pages(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Big", "phone": "0501111111", "adult": 3}, {"name": "Wait", "phone": "0502222222", "adult": 2}]).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(4))})   # Big 3, Wait 1 of 2
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Big", "phone": "0501111111", "adult": 2}]).encode())})
    assert "tickets adult 3→2" in r.text
    with SessionLocal() as db:
        got = {a.name: len(a.tickets) for a in db.query(Attendee).filter_by(event_id=event_id)}
    assert got == {"Big": 2, "Wait": 2}                                              # freed page went to the waiting guest


def test_legacy_ticket_total_and_zero_ticket_guard_on_update(admin, event_id):
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Kid Only", "phone": "0501111111", "adult": 0, "youth": 2}]).encode())})
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Kid Only", "phone": "0501111111", "adult": 0, "youth": 0}]).encode())})
    assert r.status_code == 200 and "NOT imported" in r.text                          # invalid row -> fix/skip panel
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Kid Only", "phone": "0501111111", "tickets": 3}]).encode())})
    with SessionLocal() as db:
        g = db.query(Attendee).filter_by(event_id=event_id, name="Kid Only").one()
        assert (g.adult_count, g.youth_count) == (3, 0)                                # legacy total = adults
    r = admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "Kid Only", "phone": "0501111111", "adult": 0}]).encode())})         # 0 tickets in total
    assert "NOT imported" in r.text                                                  # flagged for fix/skip, nothing changed
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id, name="Kid Only").one().adult_count == 3


def test_delete_event_removes_everything_and_keeps_whatsapp_cleanup(admin, doorman, wa, monkeypatch):
    import asyncio
    from datetime import timedelta
    from app.main import cleanup_due_messages
    from app.models import Event, ImportIssue, OrphanMessage, SentMessage, SpareTicket, utcnow
    admin.post("/events", data={"opponent": "הפועל חולון", "event_date": "2031-01-01T20:00"})
    with SessionLocal() as db:
        eid = db.query(Event).order_by(Event.id.desc()).first().id
    admin.post(f"/events/{eid}/attendees/import", files={"file": ("g.json", json.dumps(GUESTS + [{"name": "", "phone": "1"}]).encode())})
    admin.post(f"/events/{eid}/tickets/upload", files={"file": ("a.pdf", make_pdf(7))})
    with SessionLocal() as db:
        ada = db.query(Attendee).filter_by(event_id=eid, name="Ada Lovelace").one().id
    doorman.post(f"/attendees/{ada}/whatsapp")
    folder = config.STORAGE_DIR / "tickets" / str(eid)
    assert folder.exists()
    assert doorman.post(f"/events/{eid}/delete").status_code == 403
    r = admin.post(f"/events/{eid}/delete")
    assert r.status_code == 200 and "Deleted" in r.text and "2 WhatsApp ticket message(s)" in r.text
    assert not folder.exists()
    with SessionLocal() as db:
        assert db.get(Event, eid) is None
        for model in (Attendee, SpareTicket, ImportIssue, Ticket, SentMessage):
            assert db.query(model).filter_by(event_id=eid).count() == 0, model
        orphans = db.query(OrphanMessage).all()
        assert len(orphans) == 2
        for o in orphans:
            o.delete_at = utcnow() - timedelta(minutes=1)
        db.commit()
    monkeypatch.setattr(config, "WHATSAPP_DELETE_GAP", (0.0, 0.0))
    assert asyncio.run(cleanup_due_messages()) == 2 and sorted(wa["deleted"]) == ["MSG1", "MSG2"]
    assert admin.post(f"/events/{eid}/delete").status_code == 404


# ----------------------------------------------------------------- partial entry, admin edit on the door, event delete button

def party(admin, event_id, adult=2, youth=1, pages=None):
    rows = [{"name": "Party Order", "phone": "0501234500", "adult": adult, "youth": youth}]
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(rows).encode())})
    admin.post(f"/events/{event_id}/tickets/upload", files={"file": ("a.pdf", make_pdf(pages or adult + youth))})
    with SessionLocal() as db:
        return db.query(Attendee).filter_by(event_id=event_id, name="Party Order").one().id


def admitted(att_id):
    with SessionLocal() as db:
        a = db.get(Attendee, att_id)
        return (a.admitted_adult, a.admitted_youth, a.checked_in, [bool(t.delivered_at) for t in a.tickets])


def test_partial_whatsapp_send_then_the_rest(admin, doorman, event_id, wa):
    att = party(admin, event_id)                                   # 2 adults + 1 youth = 3 tickets
    form = doorman.get(f"/attendees/{att}/partial-form")
    assert form.status_code == 200 and "Partial send" in form.text and "WhatsApp" in form.text
    r = doorman.post(f"/attendees/{att}/partial", data={"adult": "1", "youth": "0", "via": "whatsapp"})
    assert 'hx-swap-oob="true"' in r.text and "Partial 1/3" in r.text
    assert "2 still to come" in r.headers["HX-Trigger"] and "closeModal" in r.headers["HX-Trigger-After-Swap"]
    assert admitted(att) == (1, 0, True, [True, False, False]) and wa["sent"] == ["MSG1"]
    stats = doorman.get(f"/events/{event_id}/stats").text
    assert 'id="stat-admitted">1<' in stats and 'id="stat-remaining">2<' in stats
    # the main buttons now hand out the rest
    card = doorman.get(f"/events/{event_id}/search", params={"q": "Party"}).text
    assert "rest (2)" in card and "Partial send" in card
    # another partial amount: one youth ticket
    doorman.post(f"/attendees/{att}/partial", data={"adult": "0", "youth": "1", "via": "whatsapp"})
    assert admitted(att) == (1, 1, True, [True, True, False]) and wa["sent"] == ["MSG1", "MSG2"]
    # the rest with the main WhatsApp button
    r = doorman.post(f"/attendees/{att}/whatsapp")
    assert wa["sent"] == ["MSG1", "MSG2", "MSG3"] and "Sent 1 ticket to" in r.headers["HX-Trigger"]
    assert admitted(att) == (2, 1, True, [True, True, True])
    assert "Partial send" not in doorman.get(f"/events/{event_id}/search", params={"q": "Party"}).text   # nothing left


def test_partial_by_qr_shows_only_the_chosen_tickets(admin, doorman, event_id):
    att = party(admin, event_id, adult=2, youth=0)
    r = doorman.post(f"/attendees/{att}/partial", data={"adult": "1", "youth": "0", "via": "qr"})
    assert r.text.count("data:image/svg+xml") == 1 and "1 of 2 tickets shown" in r.text and "Partial 1/2" in r.text
    assert admitted(att) == (1, 0, True, [True, False])
    r = doorman.post(f"/attendees/{att}/qr")                        # main QR button: the rest
    assert r.text.count("data:image/svg+xml") == 1 and admitted(att) == (2, 0, True, [True, True])


def test_partial_send_validation_and_whatsapp_failure(admin, doorman, event_id, wa):
    att = party(admin, event_id, adult=2, youth=0)
    for bad in ({"adult": "3", "youth": "0"}, {"adult": "0", "youth": "0"}, {"adult": "x", "youth": "0"},
                {"adult": "-1", "youth": "0"}, {"adult": "0", "youth": "1"}):
        r = doorman.post(f"/attendees/{att}/partial", data={**bad, "via": "whatsapp"})
        assert r.status_code == 200 and "role=\"alert\"" in r.text, bad
    assert admitted(att) == (0, 0, False, [False, False]) and wa["sent"] == []
    assert "Choose WhatsApp or QR" in doorman.post(f"/attendees/{att}/partial", data={"adult": "1", "youth": "0", "via": "pigeon"}).text

    async def boom(*a, **k):
        raise services.WhatsAppError("gateway down")
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr("app.main.send_whatsapp_pdf", boom)
    try:
        r = doorman.post(f"/attendees/{att}/partial", data={"adult": "1", "youth": "0", "via": "whatsapp"})
    finally:
        mp.undo()
    assert "gateway down" in r.text and admitted(att) == (0, 0, False, [False, False])   # nobody admitted on failure


def test_partial_stops_when_not_enough_ticket_pdfs(admin, doorman, event_id, wa):
    att = party(admin, event_id, adult=2, youth=0, pages=1)         # only 1 PDF page for 2 tickets
    r = doorman.post(f"/attendees/{att}/partial", data={"adult": "2", "youth": "0", "via": "whatsapp"})
    assert "Only 1 ticket PDF(s) are available" in r.text and wa["sent"] == []


def test_undo_resets_partial_entry(admin, doorman, event_id, wa):
    att = party(admin, event_id, adult=2, youth=0)
    doorman.post(f"/attendees/{att}/partial", data={"adult": "1", "youth": "0", "via": "whatsapp"})
    r = toggle(doorman, att)                                         # undo after a partial entry
    assert "Waiting" in r.text and wa["deleted"] == ["MSG1"] and admitted(att) == (0, 0, False, [False, False])
    doorman.post(f"/attendees/{att}/whatsapp")                       # sends both again
    assert admitted(att) == (2, 0, True, [True, True])


def test_only_admin_can_edit_on_the_door_page(admin, doorman, event_id):
    ada = seeded_event(admin, event_id)
    page = doorman.get(f"/events/{event_id}/door").text
    assert "/edit-form" not in page and "Edit</button>" not in page                   # doorman: no edit button
    assert "/edit-form" in admin.get(f"/events/{event_id}/door").text
    assert doorman.get(f"/attendees/{ada}/edit-form").status_code == 403
    form = {"name": "Ada Lovelace", "phone": "+1 (555) 010-0001", "category": "פלוס", "adult_count": "3",
            "youth_count": "2", "email": "", "age": "", "notes": ""}
    assert doorman.post(f"/attendees/{ada}/door-edit", data=form).status_code == 403
    assert admin.get(f"/attendees/{ada}/edit-form").status_code == 200
    r = admin.post(f"/attendees/{ada}/door-edit", data=form)
    assert 'hx-swap-oob="true"' in r.text and "5 ticket(s)" in r.headers["HX-Trigger"] and "closeModal" in r.headers["HX-Trigger-After-Swap"]
    with SessionLocal() as db:
        a = db.get(Attendee, ada)
        assert (a.adult_count, a.youth_count, a.ticket_count, a.category) == (3, 2, 5, "פלוס")
    bad = admin.post(f"/attendees/{ada}/door-edit", data={**form, "adult_count": "0", "youth_count": "0"})
    assert bad.status_code == 200 and 'role="alert"' in bad.text and 'name="adult_count"' in bad.text


def test_editing_amount_keeps_and_clamps_admission(admin, doorman, event_id, wa):
    att = party(admin, event_id, adult=3, youth=0)
    doorman.post(f"/attendees/{att}/partial", data={"adult": "2", "youth": "0", "via": "whatsapp"})
    base = {"name": "Party Order", "phone": "0501234500", "category": "פתוח", "email": "", "age": "", "notes": ""}
    admin.post(f"/attendees/{att}/door-edit", data={**base, "adult_count": "5", "youth_count": "0"})      # more tickets
    assert admitted(att)[:3] == (2, 0, True)
    r = doorman.get(f"/events/{event_id}/search", params={"q": "Party"}).text
    assert "Partial 2/5" in r
    admin.post(f"/attendees/{att}/door-edit", data={**base, "adult_count": "1", "youth_count": "0"})      # fewer than admitted
    assert admitted(att)[:3] == (1, 0, True) and "Admitted" in doorman.get(f"/events/{event_id}/search", params={"q": "Party"}).text


def test_delete_event_button_is_rendered_for_admins_only(admin, doorman, event_id):
    page = admin.get("/events").text
    assert f'action="/events/{event_id}/delete"' in page and "Delete</button>" in page
    assert "Danger zone" in admin.get(f"/events/{event_id}/manage").text
    assert "/delete" not in doorman.get("/events").text
    assert TestClient(app).get("/healthz").json()["version"]


def test_migration_adds_partial_entry_columns(tmp_path):
    from sqlalchemy import create_engine, text
    from app.database import migrate
    eng = create_engine(f"sqlite:///{tmp_path}/old3.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE attendees (id INTEGER PRIMARY KEY, event_id INTEGER, name TEXT, phone TEXT, ticket_type TEXT NOT NULL, "
                       "ticket_count INTEGER NOT NULL, adult_count INTEGER NOT NULL DEFAULT 0, youth_count INTEGER NOT NULL DEFAULT 0, "
                       "category VARCHAR(32) NOT NULL DEFAULT 'פתוח', checked_in BOOLEAN)"))
        c.execute(text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, event_id INTEGER, attendee_id INTEGER, page_number INTEGER, file_path TEXT, claim_token TEXT)"))
        c.execute(text("CREATE TABLE sent_messages (id INTEGER PRIMARY KEY, event_id INTEGER, attendee_id INTEGER, remote_jid TEXT, message_id TEXT, "
                       "sent_at DATETIME, status TEXT, attempts INTEGER, delete_at DATETIME, deleted_at DATETIME)"))
        c.execute(text("INSERT INTO attendees VALUES (1,1,'In','9721','S',3,2,1,'פתוח',1), (2,1,'Out','9722','S',2,2,0,'פתוח',0)"))
    migrate(eng)
    migrate(eng)
    with eng.begin() as c:
        rows = c.execute(text("SELECT name, admitted_adult, admitted_youth FROM attendees ORDER BY id")).all()
        c.execute(text("SELECT delivered_at FROM tickets")); c.execute(text("SELECT ticket_id FROM sent_messages"))
    assert [tuple(r) for r in rows] == [("In", 2, 1), ("Out", 0, 0)]


# ----------------------------------------------------------------- WhatsApp lines, wording, jitter

def test_ticket_text_varies_and_never_looks_like_a_placeholder():
    from datetime import datetime
    from app.messages import ticket_caption
    when = datetime(2030, 3, 1, 20, 30)
    texts = [ticket_caption("Dana Levi", "Maccabi vs. Holon", when, index=1, total=2, lang="en") for _ in range(200)]
    assert len(set(texts)) > 150                                      # almost every message is different
    assert all("Dana" in t and "Maccabi vs. Holon" in t and "{" not in t and "}" not in t for t in texts)
    he = [ticket_caption("דנה לוי", "מכבי נגד חולון", when, lang="he") for _ in range(100)]
    assert len(set(he)) > 50 and all("דנה" in t and "{" not in t for t in he)
    assert all(not a == b for a, b in zip(texts, texts[1:]))          # never the same twice in a row


def test_send_gap_has_random_jitter_and_a_floor(monkeypatch):
    import asyncio
    gaps = []

    async def fake_sleep(sec):
        gaps.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(config, "WHATSAPP_MIN_INTERVAL", 3.0)
    monkeypatch.setattr(config, "WHATSAPP_JITTER", 4.0)
    monkeypatch.setattr(config, "WHATSAPP_LONG_PAUSE_CHANCE", 0.0)
    th = services._SendThrottle()

    async def go():
        for _ in range(60):
            th.done()
            await th.wait_turn()

    asyncio.run(go())
    assert all(3.0 - 0.05 <= g <= 7.0 for g in gaps) and len(set(round(g, 2) for g in gaps)) > 40
    assert config.WHATSAPP_MIN_INTERVAL >= 1.0


def test_typing_delay_is_sent_with_each_message(tmp_path):
    import asyncio
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(make_pdf(1))
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"key": {"remoteJid": "x@s.whatsapp.net", "id": "A"}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await services.send_whatsapp_pdf("0501234567", pdf, "hi", client=c, instance="line-7")
            await services.send_whatsapp_text("0501234567", "hi", instance="line-7", client=c)

    asyncio.run(go())
    assert seen[0][0].endswith("/message/sendMedia/line-7") and seen[1][0].endswith("/message/sendText/line-7")
    assert all(config.WHATSAPP_TYPING_MS[0] <= body["delay"] <= config.WHATSAPP_TYPING_MS[1] for _, body in seen)


@pytest.fixture()
def lines3(monkeypatch):
    """Three fresh lines (the default 'Main' is removed) and a sender that errors on chosen instances."""
    from app.models import WhatsAppLine
    with SessionLocal() as db:
        db.query(WhatsAppLine).delete()
        for i in (1, 2, 3):
            db.add(WhatsAppLine(label=f"L{i}", instance_name=f"inst-{i}", priority=i, status="active", daily_cap=150))
        db.commit()
    state = {"calls": [], "bad": {}, "n": 0}

    async def send(phone, path, caption, file_name="t.pdf", client=None, instance=None):
        state["calls"].append(instance)
        if instance in state["bad"]:
            raise services.WhatsAppError(state["bad"][instance][1], status_code=state["bad"][instance][0])
        state["n"] += 1
        return services.SentRef(f"{phone}@s.whatsapp.net", f"M{state['n']}")

    monkeypatch.setattr("app.main.send_whatsapp_pdf", send)
    yield state
    with SessionLocal() as db:
        db.query(WhatsAppLine).delete()
        db.commit()
        from app.lines import ensure_default_line
        ensure_default_line(db)


def line_status():
    from app.models import WhatsAppLine
    with SessionLocal() as db:
        return {l.label: l.status for l in db.query(WhatsAppLine).order_by(WhatsAppLine.id)}


def test_failover_marks_lines_only_when_another_line_delivers(admin, doorman, event_id, lines3):
    ada = seeded_event(admin, event_id)
    lines3["bad"] = {"inst-1": (500, "Internal Server Error"), "inst-2": (500, "Internal Server Error")}
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert "Sent 2 tickets" in r.headers["HX-Trigger"]
    assert lines3["calls"][:3] == ["inst-1", "inst-2", "inst-3"]
    assert line_status() == {"L1": "failed", "L2": "failed", "L3": "active"}
    lines3["calls"].clear()
    doorman.post(f"/attendees/{ada}/whatsapp")                         # failed lines are skipped from now on
    assert set(lines3["calls"]) == {"inst-3"}
    assert {m.line_id is not None for m in sent_rows(ada).values()} == {True}


def test_a_bad_recipient_or_total_outage_marks_nothing(admin, doorman, event_id, lines3):
    ada = seeded_event(admin, event_id)
    lines3["bad"] = {"inst-1": (400, "number is not on WhatsApp")}      # 4xx: the recipient, not the line
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert "WhatsApp failed" in r.headers["HX-Trigger"] and lines3["calls"] == ["inst-1"]
    assert set(line_status().values()) == {"active"}
    lines3["bad"] = {f"inst-{i}": (500, "boom") for i in (1, 2, 3)}     # everything 500s -> not provably the lines
    lines3["calls"].clear()
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert "All 3 WhatsApp line(s) failed" in r.headers["HX-Trigger"]
    assert set(line_status().values()) == {"active"}


def test_daily_cap_moves_to_the_next_line(admin, doorman, event_id, lines3):
    from app.models import WhatsAppLine
    ada = seeded_event(admin, event_id)
    with SessionLocal() as db:
        l1 = db.query(WhatsAppLine).filter_by(label="L1").one()
        l1.daily_cap = 1
        db.commit()
    doorman.post(f"/attendees/{ada}/whatsapp")                          # 2 tickets: first on L1 (cap reached), second on L2
    assert lines3["calls"] == ["inst-1", "inst-2"]
    with SessionLocal() as db:
        assert db.query(WhatsAppLine).filter_by(label="L1").one().sent_today == 1


def test_no_working_line_gives_a_clear_error(admin, doorman, event_id, lines3):
    from app.models import WhatsAppLine
    ada = seeded_event(admin, event_id)
    with SessionLocal() as db:
        db.query(WhatsAppLine).update({"status": "failed"})
        db.commit()
    r = doorman.post(f"/attendees/{ada}/whatsapp")
    assert "No working WhatsApp line" in r.headers["HX-Trigger"]


def test_deleting_a_message_uses_the_line_that_sent_it(admin, doorman, event_id, lines3, monkeypatch):
    ada = seeded_event(admin, event_id)
    lines3["bad"] = {"inst-1": (500, "boom")}
    doorman.post(f"/attendees/{ada}/whatsapp")                          # sent through inst-2
    used = []

    async def delete(remote_jid, message_id, client=None, instance=None):
        used.append(instance)

    monkeypatch.setattr("app.main.delete_whatsapp_message", delete)
    toggle(doorman, ada)
    assert used == ["inst-2", "inst-2"]


def test_line_tests_mark_working_and_broken_lines(lines3, monkeypatch):
    import asyncio
    from app.lines import run_line_tests, set_setting
    monkeypatch.setattr(config, "LINE_TEST_GAP", (0.0, 0.0))
    sent = []

    async def state(name, client=None):
        return {"inst-1": "open", "inst-2": "close", "inst-3": "open"}[name]

    async def text(number, body, instance=None, client=None):
        sent.append((number, instance))
        if instance == "inst-3":
            raise services.WhatsAppError("limited", status_code=500)

    async def go():
        with SessionLocal() as db:
            set_setting(db, "test_number", "972501112222")
            return await run_line_tests(db, state_fn=state, text_fn=text)

    assert asyncio.run(go()) == {"ok": 1, "bad": 2}
    assert line_status() == {"L1": "active", "L2": "failed", "L3": "failed"}
    assert sent == [("972501112222", "inst-1"), ("972501112222", "inst-3")]      # the disconnected line gets no message
    # a repaired line comes back on its own at the next test
    async def state_ok(name, client=None):
        return "open"

    async def text_ok(number, body, instance=None, client=None):
        pass

    async def again():
        with SessionLocal() as db:
            return await run_line_tests(db, state_fn=state_ok, text_fn=text_ok)

    assert asyncio.run(again()) == {"ok": 3, "bad": 0} and set(line_status().values()) == {"active"}


def test_pretest_runs_once_24_hours_before_an_event(lines3, monkeypatch):
    import asyncio
    from datetime import timedelta
    from app.lines import event_start_utc, pretest_due_events
    from app.models import Event, utcnow
    monkeypatch.setattr(config, "LINE_TEST_GAP", (0.0, 0.0))
    calls = []

    async def state(name, client=None):
        calls.append(name)
        return "open"

    async def text(number, body, instance=None, client=None):
        pass

    with SessionLocal() as db:
        soon = Event(name="soon", location="x", event_date=utcnow() + timedelta(hours=20))
        later = Event(name="later", location="x", event_date=utcnow() + timedelta(days=3))
        db.add_all([soon, later])
        db.commit()
        soon_id, later_id = soon.id, later.id
    asyncio.run(pretest_due_events(state_fn=state, text_fn=text))
    assert len(calls) == 3                                               # three lines, once
    asyncio.run(pretest_due_events(state_fn=state, text_fn=text))
    assert len(calls) == 3                                               # not repeated
    with SessionLocal() as db:
        assert db.get(Event, soon_id).lines_tested_at is not None and db.get(Event, later_id).lines_tested_at is None
    asyncio.run(pretest_due_events(now=utcnow() + timedelta(days=2, hours=2), state_fn=state, text_fn=text))
    assert len(calls) == 6                                               # the later event, 24 h before its start


def test_low_line_alert_is_for_admins_only(admin, doorman, lines3):
    from app.models import WhatsAppLine
    assert "WhatsApp lines running low" not in admin.get("/events").text      # 3 working = enough
    with SessionLocal() as db:
        db.query(WhatsAppLine).filter_by(label="L3").update({"status": "failed"})
        db.commit()
    page = admin.get("/events").text
    assert "WhatsApp lines running low" in page and "only 2 of 3" in page
    assert "WhatsApp lines running low" not in doorman.get("/events").text


def test_lines_manager_is_admin_only_and_manages_lines(admin, doorman, lines3, monkeypatch):
    created = []

    async def create(name, client=None):
        created.append(name)

    async def delete_inst(name, client=None):
        created.append(f"deleted:{name}")

    monkeypatch.setattr("app.main.evolution_create_instance", create)
    monkeypatch.setattr("app.main.evolution_delete_instance", delete_inst)
    assert doorman.get("/lines").status_code == 403 and doorman.post("/lines/test-all").status_code == 403
    assert TestClient(app, follow_redirects=False).get("/lines").status_code == 303
    page = admin.get("/lines").text
    assert "3 of 3 line(s) working" in page and "L1" in page
    r = admin.post("/lines", data={"label": "Spare SIM", "daily_cap": "80", "phone": "052-9999999"})
    assert r.status_code == 200 and "Added “Spare SIM”" in r.text and created == ["event-door-4"]
    assert admin.post("/lines", data={"label": "x", "daily_cap": "abc"}).status_code == 422
    r = admin.post("/lines/bulk")
    assert "Created 6 line(s)" in r.text and len(created) == 7                          # topped up to 10
    from app.models import WhatsAppLine
    with SessionLocal() as db:
        assert db.query(WhatsAppLine).count() == 10
        spare = db.query(WhatsAppLine).filter_by(label="Spare SIM").one()
        assert (spare.status, spare.daily_cap, spare.phone) == ("pending", 80, "972529999999")
        sid = spare.id
    assert "disabled" in admin.post(f"/lines/{sid}/toggle").text
    admin.post(f"/lines/{sid}/primary")
    with SessionLocal() as db:
        assert db.get(WhatsAppLine, sid).priority < 1
    assert "Test number saved" in admin.post("/lines/settings", data={"test_number": "052-1234567"}).text
    assert admin.post("/lines/settings", data={"test_number": "12"}).status_code == 422
    assert "Removed Spare SIM" in admin.post(f"/lines/{sid}/delete").text and f"deleted:event-door-4" in created


# ----------------------------------------------------------------- import from the registration system

REG_HTML = """<!doctype html><html dir="rtl"><head><meta charset="utf-8"><title>הזמנות כרטיסים</title></head><body>
<table class="nav"><tr><td><a href="/">בית</a></td><td><a href="/admin">ניהול</a></td></tr></table>
<h2>הזמנות למשחק 377</h2>
<table id="res" class="table">
  <thead><tr><th>#</th><th>שם פרטי</th><th>שם משפחה</th><th>טלפון נייד</th><th>דוא"ל</th><th>סוג</th><th>מבוגרים</th><th>נוער</th><th>גיל</th><th>תשלום</th></tr></thead>
  <tbody>
    <tr><td>1</td><td><b>דנה</b></td><td>לוי</td><td>052-1111111</td><td><a href="mailto:d@x.co">d@x.co</a></td><td>חבר ארגון</td><td>2</td><td>1</td><td>34.5</td><td>שולם</td></tr>
    <tr><td>2</td><td>נועה</td><td>בר</td><td>0522222222</td><td></td><td>פלוס</td><td>1</td><td></td><td></td><td>שולם</td></tr>
    <tr><td>3</td><td>בלי</td><td>טלפון</td><td>12</td><td></td><td>פתוח</td><td>1</td><td>0</td><td></td><td>ממתין</td></tr>
    <tr><th>#</th><th>שם פרטי</th><th>שם משפחה</th><th>טלפון נייד</th><th>דוא"ל</th><th>סוג</th><th>מבוגרים</th><th>נוער</th><th>גיל</th><th>תשלום</th></tr>
    <tr><td>4</td><td>יוסי</td><td>כהן</td><td>+972 53-3333333</td><td></td><td>מצטרף</td><td>0</td><td>3</td><td></td><td>שולם</td></tr>
    <tr><td colspan="10">סה"כ 4 הזמנות</td></tr>
  </tbody>
</table></body></html>"""


def test_registration_table_is_found_and_mapped():
    from app.services import extract_table, parse_registration_html
    rows = extract_table(REG_HTML.encode())
    assert len(rows) == 4 and rows[0]["שם פרטי"] == "דנה" and "סה\"כ" not in str(rows)     # nav table, footer, repeated header ignored
    res = parse_registration_html(REG_HTML.encode())
    got = {v.name: (v.phone, v.category, v.adult_count, v.youth_count, v.age, v.email) for v in res.valid}
    assert got == {"דנה לוי": ("972521111111", "חבר ארגון", 2, 1, 34.5, "d@x.co"),
                   "נועה בר": ("972522222222", "פלוס", 1, 0, None, None),
                   "יוסי כהן": ("972533333333", "מצטרף", 0, 3, None, None)}
    assert [i.label for i in res.issues] == ["Row 3"] and "phone" in res.issues[0].errors[0]
    assert res.ignored == ["תשלום"]            # "#" (row number) is silently ignored


def test_registration_page_without_a_usable_table():
    from app.services import RegistrationError, extract_table
    for html in ("", "<html><body><p>hello</p></body></html>",
                 "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>"):
        with pytest.raises(RegistrationError):
            extract_table(html)


def test_fetching_the_registration_page(monkeypatch):
    import asyncio
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["cookie"] = str(request.url), request.headers.get("cookie")
        kind = seen.get("kind")
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://accounts.google.com/o/oauth2/auth"})
        if kind == "login":
            return httpx.Response(200, text='<html><form action="https://accounts.google.com/x"><input type="password"></form></html>')
        return httpx.Response(200, content=REG_HTML.encode())

    async def go(game_id, cookie="PHPSESSID=abc; x=1"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await services.fetch_registration_page(game_id, cookie, client=c)

    assert asyncio.run(go("377")).startswith(b"<!doctype")
    assert seen["url"] == "https://gate11.co.il/admin/page.php?id=ticketReservations&gameId=377"
    assert seen["cookie"] == "PHPSESSID=abc; x=1"
    for kind in ("redirect", "login"):
        seen["kind"] = kind
        with pytest.raises(services.RegistrationError, match="expired"):
            asyncio.run(go("377"))
    for bad in ("377&x=1", "../etc", "abc", "", "37 7"):                      # only digits ever reach the URL
        with pytest.raises(services.RegistrationError):
            asyncio.run(go(bad))
    with pytest.raises(services.RegistrationError, match="cookie"):
        asyncio.run(go("377", cookie=" "))


def test_registration_import_endpoints(admin, doorman, event_id, monkeypatch):
    page = admin.get(f"/events/{event_id}/manage").text
    assert "Import from the registration system" in page and "Fetch &amp; import" not in page      # no ID yet
    assert doorman.post(f"/events/{event_id}/registration/id", data={"external_id": "377"}).status_code == 403
    assert admin.post(f"/events/{event_id}/registration/id", data={"external_id": "37x"}).status_code == 422
    r = admin.post(f"/events/{event_id}/registration/id", data={"external_id": "377"})
    assert "Registration event ID saved (377)" in r.text and "Fetch &amp; import game 377" in r.text

    # no cookie yet -> clear message, nothing imported
    monkeypatch.setattr(config, "REGISTRATION_COOKIE", "")
    r = admin.post(f"/events/{event_id}/registration/fetch")
    assert r.status_code == 422 and "No session cookie saved" in r.text
    # cookie: validated, stored, never echoed back
    assert admin.post("/registration/cookie", data={"event_id": event_id, "cookie": "nonsense"}).status_code == 422
    secret = "PHPSESSID=SUPERSECRET123; lang=he"
    r = admin.post("/registration/cookie", data={"event_id": event_id, "cookie": "Cookie: " + secret})
    assert "Session cookie saved" in r.text and "SUPERSECRET123" not in r.text and "saved" in r.text
    assert "SUPERSECRET123" not in admin.get(f"/events/{event_id}/manage").text
    assert doorman.post("/registration/cookie", data={"event_id": event_id, "cookie": secret}).status_code == 403

    calls = []

    async def fake_fetch(game_id, cookie, client=None):
        calls.append((game_id, cookie))
        return REG_HTML.encode()

    monkeypatch.setattr("app.main.fetch_registration_page", fake_fetch)
    r = admin.post(f"/events/{event_id}/registration/fetch")
    assert calls == [("377", secret)] and "Registration #377: Imported 3 guest(s)." in r.text
    assert "1 row(s) have problems" in r.text and "not recognised and were ignored: תשלום" in r.text
    with SessionLocal() as db:
        dana = db.query(Attendee).filter_by(event_id=event_id, name="דנה לוי").one()
        assert (dana.adult_count, dana.youth_count, dana.age, dana.category) == (2, 1, 34.5, "חבר ארגון")

    # later the registration changed Dana to 4 adults: re-fetch updates her, adds nobody
    changed = REG_HTML.replace("<td>2</td><td>1</td><td>34.5</td>", "<td>4</td><td>1</td><td>34.5</td>")

    async def fake_fetch2(game_id, cookie, client=None):
        return changed.encode()

    monkeypatch.setattr("app.main.fetch_registration_page", fake_fetch2)
    r = admin.post(f"/events/{event_id}/registration/fetch")
    assert "Imported 0 guest(s)" in r.text and "Updated 1 existing guest(s): דנה לוי: tickets adult 2→4" in r.text
    # expired session is reported, not crashed
    async def expired(game_id, cookie, client=None):
        raise services.RegistrationError("The registration site asked to sign in again - the saved session cookie has expired.")

    monkeypatch.setattr("app.main.fetch_registration_page", expired)
    r = admin.post(f"/events/{event_id}/registration/fetch")
    assert r.status_code == 422 and "cookie has expired" in r.text
    assert "Session cookie removed" in admin.post("/registration/cookie", data={"event_id": event_id, "clear": "1"}).text


def test_registration_saved_page_upload(admin, doorman, event_id):
    r = admin.post(f"/events/{event_id}/registration/upload", files={"file": ("page.html", REG_HTML.encode("utf-8"), "text/html")})
    assert r.status_code == 200 and "Saved page: Imported 3 guest(s)." in r.text
    r = admin.post(f"/events/{event_id}/registration/upload", data={"html": "<p>nothing here</p>"})
    assert r.status_code == 422 and "No table" in r.text
    assert doorman.post(f"/events/{event_id}/registration/upload", data={"html": REG_HTML}).status_code == 403
    assert admin.post(f"/events/{event_id}/registration/upload").status_code == 422        # nothing provided


def test_hebrew_and_split_names_also_work_in_json_imports():
    rows = [{"שם פרטי": "דנה", "שם משפחה": "לוי", "נייד": "0501234567", "מבוגרים": 3, "נוער": 1, "קטגוריה": "פלוס"}]
    res = services.parse_guest_list("g.json", json.dumps(rows, ensure_ascii=False).encode())
    v = res.valid[0]
    assert (v.name, v.adult_count, v.youth_count, v.category) == ("דנה לוי", 3, 1, "פלוס") and res.ignored == []


# ----------------------------------------------------------------- gate11 page structure, members phone book, bookmarklet

@pytest.fixture(autouse=True)
def _fresh_members_book():
    from app.models import AppSetting, Member
    try:
        with SessionLocal() as db:
            db.query(Member).delete()
            db.query(AppSetting).filter_by(key="members_override_phone").delete()
            db.commit()
    except Exception:       # tables do not exist yet before the first app start (pure unit tests)
        pass
    yield


def site_html():
    """Synthetic page shaped like the real reservations page: one table, a section row per category, the header
    repeated in each section, totals + spacer rows, hidden 'saved' texts, checkboxes, junk in the age column."""
    head = ("<tr><th>מזהה</th><th>שם</th><th>גיל</th><th>טלפון</th><th>מבוגר</th><th>נוער</th><th>הסעה</th>"
            "<th>שולם</th><th>התקבל</th><th>הערות</th><th>פעולות</th></tr>")

    def row(i, ref, name, age, phone, a, y, notes="אתר ארגון", transport=0):
        return (f'<tr class="{"even" if i % 2 else "odd"}-row"><td>{ref}</td><td>{name}</td><td>{age}</td>'
                f'<td class="hidden-xs hidden-sm">{phone}</td><td>{a}</td><td>{y}</td><td>{transport}</td>'
                '<td><input type="checkbox" checked><span class="hide"> </span><div class="hide alert">נשמר</div></td>'
                '<td><input type="checkbox" class="receive-checkbox"><div class="hide alert">לא התקבל נשמר</div></td>'
                f'<td>{notes}</td><td><a href="#" class="edit-action">x</a></td></tr>')

    def section(cls, title):
        return f'<tr class="group section-title section-title-{cls}"><td></td><td>{title}</td>' + "<td></td>" * 9 + "</tr>"

    def footer(cls, total):
        return (f'<tr class="section-footer section-footer-{cls}"><td></td><td>סה"כ: {total}</td>' + "<td></td>" * 9 + "</tr>"
                f'<tr class="section-separator section-separator-{cls}">' + "<td></td>" * 11 + "</tr>")

    body = "".join([
        section("members", "חברי ארגון"), head,
        row(1, 11111111, "אבי לוי", "", "", 2, 0), row(2, 11111112, "גילה כהן", "", "052-7000001", 1, 1),
        footer("members", 2),
        section("newcomers", "מצטרפים"), head, row(3, 22222221, "רן שלום", "", "", 1, 0), footer("newcomers", 1),
        section("plus", "פלוסים"), head,
        row(4, 33333331, "נועם פלוס", "", "", 1, 0, notes="נרשם עי מישהו. פלוס של אבי"), footer("plus", 1),
        section("public", "פתוח"), head,
        row(5, 44444441, "דנה כרמל", "34.5", "0501110001", 2, 1, transport=2),
        row(6, 44444442, "דנה כרמל", "yoniball12", "0501110001", 1, 0),          # second order, same name + phone
        row(7, 44444443, "יוסי אפל", "51 ו 14", "+972 53-1110003", 0, 2, notes="נרשם עי Someone."),
        footer("public", 3),
    ])
    return f'<html><body><table id="dataTables-ticketReservations"><thead>{head}</thead><tbody>{body}</tbody></table></body></html>'


def test_site_page_sections_become_categories_and_noise_is_dropped():
    res = services.parse_registration_html(site_html().encode())
    got = {(v.external_ref): (v.name, v.category, v.adult_count, v.youth_count) for v in res.valid}
    assert got == {"44444441": ("דנה כרמל", "פתוח", 2, 1), "44444442": ("דנה כרמל", "פתוח", 1, 0),   # two orders kept apart
                   "44444443": ("יוסי אפל", "פתוח", 0, 2), "11111112": ("גילה כהן", "חבר ארגון", 1, 1)}
    assert sorted(i.row["external_ref"] for i in res.issues) == ["11111111", "22222221", "33333331"]   # blank phone
    assert {i.row["category"] for i in res.issues} == {"חבר ארגון", "מצטרף", "פלוס"}
    assert res.bad_ages == 2 and res.ignored == []
    by = {v.external_ref: v for v in res.valid}
    assert by["44444441"].age == 34.5 and by["44444441"].notes == "הסעה: 2" and by["44444442"].age is None
    assert by["44444443"].phone == "972531110003" and by["44444443"].notes == "נרשם עי Someone."        # boilerplate note dropped
    assert not any("סה" in v.name for v in res.valid)                                                 # totals rows ignored


def test_members_list_fills_missing_phones_from_the_site_import():
    from app.services import MemberBook
    book = MemberBook([("אבי לוי", "972521000001"), ("רן שלום", "972521000002"), ("כהן דוד", "972521000003"),
                       ("שם כפול", "972521000004"), ("שם כפול", "972521000005")])
    assert book.lookup("לוי  אבי") == "972521000001" and book.lookup('אבי "לוי"') == "972521000001"   # order/quotes don't matter
    assert book.lookup("דוד כהן") == "972521000003" and book.lookup("שם כפול") is None and book.lookup("מישהו") is None
    res = services.parse_registration_html(site_html().encode(), members=book)
    assert res.filled == 2 and sorted(i.row["external_ref"] for i in res.issues) == ["33333331"]       # the plus is not a member
    by = {v.name: v for v in res.valid}
    assert by["אבי לוי"].phone == "972521000001" and by["אבי לוי"].category == "חבר ארגון"
    override = MemberBook([("גילה כהן", "972529999999")], override=True)       # the member's own number wins
    assert {v.name: v.phone for v in services.parse_registration_html(site_html().encode(), members=override).valid}["גילה כהן"] == "972529999999"
    keep = MemberBook([("גילה כהן", "972529999999")], override=False)
    assert {v.name: v.phone for v in services.parse_registration_html(site_html().encode(), members=keep).valid}["גילה כהן"] == "972527000001"


def test_reimport_matches_by_reservation_id_not_by_name(admin, event_id):
    r = admin.post(f"/events/{event_id}/registration/upload", data={"html": site_html()})
    assert "Saved page: Imported 4 guest(s)." in r.text and "3 row(s) have problems" in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id, name="דנה כרמל").count() == 2        # two separate orders
    changed = site_html().replace("<td>44444441</td><td>דנה כרמל</td>", "<td>44444441</td><td>דנה כרמל-לוי</td>")
    r = admin.post(f"/events/{event_id}/registration/upload", data={"html": changed})
    assert "Imported 0 guest(s)." in r.text and "name דנה כרמל→דנה כרמל-לוי" in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 4                          # renamed, not duplicated
        assert db.query(Attendee).filter_by(external_ref="44444441").one().name == "דנה כרמל-לוי"
    # a guest imported earlier from a plain file (no id) is adopted by the site's reservation
    admin.post(f"/events/{event_id}/attendees/import", files={"file": ("g.json", json.dumps(
        [{"name": "רונית פתוח", "phone": "0509990000", "adult": 1}]).encode())})
    row = '<tr class="odd-row"><td>55555555</td><td>רונית פתוח</td><td></td><td>0509990000</td><td>2</td><td>0</td><td>0</td><td></td><td></td><td></td><td></td></tr>'
    admin.post(f"/events/{event_id}/registration/upload", data={"html": site_html().replace("</tbody>", row + "</tbody>")})
    with SessionLocal() as db:
        g = db.query(Attendee).filter_by(event_id=event_id, name="רונית פתוח").all()
        assert len(g) == 1 and g[0].external_ref == "55555555" and g[0].adult_count == 2


def test_members_page_and_retrying_waiting_rows(admin, doorman, event_id):
    assert doorman.get("/members").status_code == 403 and TestClient(app, follow_redirects=False).get("/members").status_code == 303
    r = admin.post(f"/events/{event_id}/registration/upload", data={"html": site_html()})
    assert "3 row(s) have problems" in r.text and "without a phone number" in r.text
    assert "Added אבי לוי" in admin.post("/members", data={"name": "אבי לוי", "phone": "052-1000001"}).text
    assert admin.post("/members", data={"name": "לוי אבי", "phone": "052-1000009"}).status_code == 422       # same name, other order
    assert admin.post("/members", data={"name": "x", "phone": "12"}).status_code == 422
    r = admin.post("/members/import", data={"text": "שם, טלפון\nרן שלום, 053-2000002\n0541234567\tנועם פלוס\nבלי טלפון\nאבי לוי, 052-1000001"})
    assert "Added 2, updated 0, 1 already up to date." in r.text and "Line 3" in r.text                      # header skipped, bad line reported
    assert doorman.post("/members/import", data={"text": "a, 0501234567"}).status_code == 403
    r = admin.post(f"/events/{event_id}/import-issues/retry")                                             # the waiting rows now pass
    assert "Added 3 waiting guest(s). 3 phone number(s) came from the Members list." in r.text and "need fixing" not in r.text
    with SessionLocal() as db:
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 7
        g = db.query(Attendee).filter_by(event_id=event_id, name="אבי לוי").one()
        assert (g.phone, g.category, g.external_ref, g.adult_count) == ("972521000001", "חבר ארגון", "11111111", 2)
    form = {"name": "דוד חדש", "phone": "", "category": "חבר ארגון", "adult_count": "1", "youth_count": "0"}
    assert admin.post(f"/events/{event_id}/attendees", data=form).status_code == 422                       # not in the book yet
    admin.post("/members", data={"name": "דוד חדש", "phone": "054-3000003"})
    r = admin.post(f"/events/{event_id}/attendees", data=form)                                            # manual add uses the book
    assert r.status_code == 200 and "Added דוד חדש" in r.text
    with SessionLocal() as db:
        from app.models import Member
        mid = db.query(Member).filter_by(name="רן שלום").one().id
    assert "Saved רן שלום" in admin.post(f"/members/{mid}/edit", data={"name": "רן שלום", "phone": "053-7777777", "notes": "x"}).text
    assert "Setting saved" in admin.post("/members/settings", data={"override": "on"}).text
    assert "checked" in admin.get("/members").text.split('id="override"')[1][:60]
    assert "Removed רן שלום" in admin.post(f"/members/{mid}/delete").text


def test_member_file_formats():
    assert services.parse_member_file("m.csv", "שם,טלפון\nדנה לוי,052-1111111\n".encode("utf-8")) == [("דנה לוי", "052-1111111")]
    buf = io.BytesIO()
    pd.DataFrame({"שם פרטי": ["יוסי"], "שם משפחה": ["כהן"], "נייד": ["053-2222222"]}).to_excel(buf, index=False)
    assert services.parse_member_file("m.xlsx", buf.getvalue()) == [("יוסי כהן", "053-2222222")]
    assert services.parse_member_file("m.json", json.dumps([{"name": "A B", "phone": "0501234567"}]).encode()) == [("A B", "0501234567")]
    with pytest.raises(services.ImportValidationError):
        services.parse_member_file("m.pdf", b"x")


def test_send_to_gate_bookmarklet_and_receiver(admin, doorman, event_id):
    from app.main import bookmarklet_source
    js = bookmarklet_source("https://tickets.example.org")
    assert js.startswith("javascript:") and "var O='https://tickets.example.org'" in js and "/registration/receiver" in js and "e.origin!==O" in js
    assert "\n" not in js and '"' not in js                                  # one line, safe inside an href attribute
    page = admin.get(f"/events/{event_id}/manage").text
    assert "Send to Gate" in page and "/registration/receiver" in page
    assert doorman.get("/registration/receiver").status_code == 403
    recv = admin.get("/registration/receiver")
    assert recv.status_code == 200 and "https://gate11.co.il" in recv.text and "window.opener" in recv.text
    import lxml.html          # rows exactly as the bookmarklet collects them: [{c: row class, t: [cell texts]}]
    table = lxml.html.fromstring(site_html()).xpath("//table")[0]
    rows = [{"c": tr.get("class") or "", "t": [" ".join(c.text_content().split()) for c in tr.xpath("./th|./td")]} for tr in table.xpath(".//tr")]
    body = {"event_id": event_id, "game_id": "376", "rows": rows}
    assert doorman.post("/registration/receive", json=body).status_code == 403
    out = admin.post("/registration/receive", json=body)
    assert out.status_code == 200 and out.json()["ok"] and "Registration #376: Imported 4 guest(s)." in out.json()["messages"][0]
    assert any("3 row(s) have problems" in w for w in out.json()["warnings"]) and out.json()["manage_url"] == f"/events/{event_id}/manage"
    with SessionLocal() as db:
        assert db.get(Event, event_id).external_id == "376"                 # the game id is remembered for next time
        assert db.query(Attendee).filter_by(event_id=event_id).count() == 4
    assert admin.post("/registration/receive", json={**body, "event_id": 99999}).status_code == 404
    assert admin.post("/registration/receive", json={"event_id": event_id, "game_id": "1", "rows": [{"c": "", "t": ["a", "b"]}]}).status_code == 422
    assert admin.post("/registration/receive", json={"event_id": event_id}).status_code == 422
