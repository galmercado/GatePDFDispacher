"""SQLAlchemy 2.0 ORM models."""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
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
    age: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Legacy column kept so databases created before categories existed keep working; unused.
    ticket_type: Mapped[str] = mapped_column(String(64), default="Standard")
    category: Mapped[str] = mapped_column(String(32), default="פתוח", index=True)
    adult_count: Mapped[int] = mapped_column(Integer, default=1)   # מבוגר tickets
    youth_count: Mapped[int] = mapped_column(Integer, default=0)   # נוער tickets
    ticket_count: Mapped[int] = mapped_column(Integer, default=1)  # always adult_count + youth_count
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    admitted_adult: Mapped[int] = mapped_column(Integer, default=0)   # tickets let in so far (partial entry)
    admitted_youth: Mapped[int] = mapped_column(Integer, default=0)
    checked_in: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped[Event] = relationship(back_populates="attendees")
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="attendee", cascade="all, delete-orphan", order_by="Ticket.page_number"
    )

    @property
    def admitted_total(self) -> int:
        return (self.admitted_adult or 0) + (self.admitted_youth or 0)

    @property
    def remaining_adult(self) -> int:
        return max(0, self.adult_count - (self.admitted_adult or 0))

    @property
    def remaining_youth(self) -> int:
        return max(0, self.youth_count - (self.admitted_youth or 0))

    @property
    def remaining_total(self) -> int:
        return self.remaining_adult + self.remaining_youth

    @property
    def fully_admitted(self) -> bool:
        return self.ticket_count > 0 and self.admitted_total >= self.ticket_count

    @property
    def partially_admitted(self) -> bool:
        return 0 < self.admitted_total < self.ticket_count


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
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # sent via WhatsApp / shown as QR
    claim_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=lambda: uuid.uuid4().hex
    )

    event: Mapped[Event] = relationship(back_populates="tickets")
    attendee: Mapped[Attendee] = relationship(back_populates="tickets")


class SpareTicket(Base):
    """An uploaded ticket page not yet assigned to anyone. Assigned automatically (lowest page
    first) to guests who are missing tickets; a new claim token is issued on assignment."""

    __tablename__ = "spare_tickets"
    __table_args__ = (UniqueConstraint("event_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(512))


class SentMessage(Base):
    """A ticket message delivered over WhatsApp, kept so it can be deleted for everyone later."""

    __tablename__ = "sent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    attendee_id: Mapped[int] = mapped_column(ForeignKey("attendees.id", ondelete="CASCADE"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_jid: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(128))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(16), default="sent", index=True)  # sent | deleted | expired
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)  # scheduled removal
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ImportIssue(Base):
    """A guest-list row that failed validation. The valid rows are imported right away; these wait
    here until the admin fixes them (and adds the guest) or skips them."""

    __tablename__ = "import_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(32))          # e.g. "Row 7"
    data: Mapped[str] = mapped_column(Text)                 # JSON: the row's values as strings
    errors: Mapped[str] = mapped_column(Text)               # JSON: list of messages
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrphanMessage(Base):
    """A ticket message whose event was deleted. Kept (without any link to the event) so the scheduled
    random deletion on WhatsApp still happens."""

    __tablename__ = "orphan_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    remote_jid: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(128))
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="sent", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
