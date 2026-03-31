"""SQLAlchemy models for UFC Stats–derived data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Fighter(Base):
    __tablename__ = "fighters"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    nickname: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    height_in: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reach_in: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stance: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    dob: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    fights_a: Mapped[list["Fight"]] = relationship(
        back_populates="fighter_a", foreign_keys="Fight.fighter_a_id"
    )
    fights_b: Mapped[list["Fight"]] = relationship(
        back_populates="fighter_b", foreign_keys="Fight.fighter_b_id"
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(400))
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    raw_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    fights: Mapped[list["Fight"]] = relationship(back_populates="event")


class Fight(Base):
    __tablename__ = "fights"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), ForeignKey("events.id"), index=True)
    fighter_a_id: Mapped[str] = mapped_column(String(32), ForeignKey("fighters.id"), index=True)
    fighter_b_id: Mapped[str] = mapped_column(String(32), ForeignKey("fighters.id"), index=True)
    winner_id: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("fighters.id"), nullable=True)
    weight_class: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    fight_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_str: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    detail_ingested: Mapped[bool] = mapped_column(default=False)

    event: Mapped["Event"] = relationship(back_populates="fights")
    fighter_a: Mapped["Fighter"] = relationship(foreign_keys=[fighter_a_id])
    fighter_b: Mapped["Fighter"] = relationship(foreign_keys=[fighter_b_id])
    stats: Mapped[list["FightFighterStats"]] = relationship(back_populates="fight")


class FightFighterStats(Base):
    """Per-fighter totals for one fight (UFC Stats 'Totals' table)."""

    __tablename__ = "fight_fighter_stats"
    __table_args__ = (UniqueConstraint("fight_id", "fighter_id", name="uq_fight_fighter_stats"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fight_id: Mapped[str] = mapped_column(String(32), ForeignKey("fights.id"), index=True)
    fighter_id: Mapped[str] = mapped_column(String(32), ForeignKey("fighters.id"), index=True)
    kd: Mapped[int] = mapped_column(Integer, default=0)
    sig_str_landed: Mapped[int] = mapped_column(Integer, default=0)
    sig_str_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sig_str_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_str_landed: Mapped[int] = mapped_column(Integer, default=0)
    total_str_attempted: Mapped[int] = mapped_column(Integer, default=0)
    td_landed: Mapped[int] = mapped_column(Integer, default=0)
    td_attempted: Mapped[int] = mapped_column(Integer, default=0)
    td_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sub_att: Mapped[int] = mapped_column(Integer, default=0)
    rev: Mapped[int] = mapped_column(Integer, default=0)
    ctrl_seconds: Mapped[int] = mapped_column(Integer, default=0)

    fight: Mapped["Fight"] = relationship(back_populates="stats")


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    sport_key: Mapped[str] = mapped_column(String(80))
    event_id_external: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)


class FighterComposite(Base):
    """Rolling / composite scores updated after each processed fight (point-in-time externally)."""

    __tablename__ = "fighter_composites"

    fighter_id: Mapped[str] = mapped_column(String(32), ForeignKey("fighters.id"), primary_key=True)
    as_of_fight_id: Mapped[str] = mapped_column(String(32), ForeignKey("fights.id"), primary_key=True)
    strike_score: Mapped[float] = mapped_column(Float, default=0.0)
    grapple_score: Mapped[float] = mapped_column(Float, default=0.0)
    pace_score: Mapped[float] = mapped_column(Float, default=0.0)
    momentum_score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
