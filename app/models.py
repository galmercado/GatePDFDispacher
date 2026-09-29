"""SQLAlchemy 2.0 ORM models."""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(str, enum.Enum):
    admin = "admin"
    doorman = "doorman"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.doorman)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    location: Mapped[str] = mapped_column(String(255), default="")
    event_date: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    attendees: Mapped[list["Attendee"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Attendee(Base):
    __tablename__ = "attendees"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Legacy column kept so databases created before categories existed keep working; unused.
    ticket_type: Mapped[str] = mapped_column(String(64), default="Standard")
    category: Mapped[str] = mapped_column(String(32), default="פתוח", index=True)
    adult_count: Mapped[int] = mapped_column(Integer, default=1)   # מבוגר tickets
    youth_count: Mapped[int] = mapped_column(Integer, default=0)   # נוער tickets
    ticket_count: Mapped[int] = mapped_column(Integer, default=1)  # always adult_count + youth_count
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_in: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped[Event] = relationship(back_populates="attendees")
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="attendee", cascade="all, delete-orphan", order_by="Ticket.page_number"
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("event_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    attendee_id: Mapped[int] = mapped_column(
        ForeignKey("attendees.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(512))
    claim_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=lambda: uuid.uuid4().hex
    )

    event: Mapped[Event] = relationship(back_populates="tickets")
    attendee: Mapped[Attendee] = relationship(back_populates="tickets")
