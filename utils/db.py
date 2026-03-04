"""
SQLAlchemy models and CRUD helpers for the trade log.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config.settings import DATABASE_URL
from utils.logger import get_logger

log = get_logger(__name__)

Base = declarative_base()


# ── Models ───────────────────────────────────────────────────────────
class TradeLog(Base):
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, default=date.today)
    instrument = Column(String, nullable=False)
    direction = Column(String, nullable=False)        # BUY or SELL
    entry_price = Column(Float)
    exit_price = Column(Float)
    quantity = Column(Integer)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)
    pnl = Column(Float, default=0.0)
    strategy = Column(String)
    sl_price = Column(Float)
    target_price = Column(Float)
    exit_reason = Column(String)                      # SL_HIT, TARGET_HIT, SQUARE_OFF, MANUAL
    order_ids = Column(String, default="[]")          # JSON list
    charges = Column(Float, default=0.0)


class DailySummary(Base):
    __tablename__ = "daily_summary"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    gross_pnl = Column(Float, default=0.0)
    net_pnl = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    capital_used = Column(Float, default=0.0)


# ── Engine / Session ─────────────────────────────────────────────────
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)
    log.info("Database initialised (%s).", DATABASE_URL)


def get_session() -> Session:
    return SessionLocal()


# ── CRUD helpers ─────────────────────────────────────────────────────
def log_trade(
    instrument: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    entry_time: datetime,
    exit_time: datetime,
    pnl: float,
    strategy: str,
    sl_price: float,
    target_price: float,
    exit_reason: str,
    order_ids: list[str] | None = None,
    charges: float = 0.0,
) -> TradeLog:
    session = get_session()
    try:
        trade = TradeLog(
            date=entry_time.date(),
            instrument=instrument,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            entry_time=entry_time,
            exit_time=exit_time,
            pnl=pnl,
            strategy=strategy,
            sl_price=sl_price,
            target_price=target_price,
            exit_reason=exit_reason,
            order_ids=json.dumps(order_ids or []),
            charges=charges,
        )
        session.add(trade)
        session.commit()
        log.info("Trade logged: %s %s %s P&L=%.2f", direction, instrument, exit_reason, pnl)
        return trade
    finally:
        session.close()


def get_todays_trades() -> list[TradeLog]:
    session = get_session()
    try:
        return session.query(TradeLog).filter(TradeLog.date == date.today()).all()
    finally:
        session.close()


def save_daily_summary(
    total: int,
    wins: int,
    losses: int,
    gross_pnl: float,
    net_pnl: float,
    max_dd: float,
    capital_used: float,
) -> DailySummary:
    session = get_session()
    try:
        existing: Optional[DailySummary] = (
            session.query(DailySummary)
            .filter(DailySummary.date == date.today())
            .first()
        )
        if existing:
            existing.total_trades = total
            existing.winning_trades = wins
            existing.losing_trades = losses
            existing.gross_pnl = gross_pnl
            existing.net_pnl = net_pnl
            existing.max_drawdown = max_dd
            existing.capital_used = capital_used
            summary = existing
        else:
            summary = DailySummary(
                date=date.today(),
                total_trades=total,
                winning_trades=wins,
                losing_trades=losses,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                max_drawdown=max_dd,
                capital_used=capital_used,
            )
            session.add(summary)
        session.commit()
        return summary
    finally:
        session.close()
