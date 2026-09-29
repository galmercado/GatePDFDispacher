"""SQLite engine (WAL mode), declarative Base and session dependency."""
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_dir(config.DATABASE_URL)

engine: Engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("PRAGMA busy_timeout=30000;")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate(bind: Engine) -> None:
    """Add columns introduced after the first release to pre-existing databases (SQLite)."""
    insp = inspect(bind)
    if "attendees" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("attendees")}
    with bind.begin() as conn:
        if "category" not in cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN category VARCHAR(32) NOT NULL DEFAULT 'פתוח'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_attendees_category ON attendees (category)"))
        counts_added = False
        for col in ("adult_count", "youth_count"):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE attendees ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"))
                counts_added = True
        if counts_added:  # every pre-existing ticket counts as an adult ticket
            conn.execute(text("UPDATE attendees SET adult_count = ticket_count"))


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(bind=engine)
    migrate(engine)
