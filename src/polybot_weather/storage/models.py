"""SQLAlchemy ORM models — persistence for forecasts, recommendations, outcomes.

Tables:
  market           — one row per unique Polymarket market we've analyzed
  forecast         — one row per (market, run_at) snapshot of model probs
  recommendation   — one row per (forecast, outcome_label) flagged with edge>0
  outcome          — actual realized outcome, used by backtest/calibrate
  bias_entry       — per-(station, model, month) running mean error
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow_naive() -> datetime:
    """Naive UTC datetime. Replacement for deprecated `datetime.utcnow()`.

    The DateTime columns are stored without tzinfo, so we strip it off to keep
    the existing wire format stable.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "market"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    polymarket_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(256), index=True)
    question: Mapped[str] = mapped_column(String(1024))
    metric: Mapped[str] = mapped_column(String(32))
    station_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Unit of `forecast.forecast_mean_f` and `outcome.realized_value` — "F" or
    # "C". Driven by the parser (explicit °C/celsius hint) with fallback to the
    # station's default. Must be consistent across forecast + outcome or the
    # calibration residuals become garbage.
    unit: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolution_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)

    forecasts: Mapped[list[Forecast]] = relationship(back_populates="market", cascade="all, delete-orphan")
    outcome: Mapped[Outcome | None] = relationship(back_populates="market", uselist=False)


class Forecast(Base):
    __tablename__ = "forecast"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("market.id"), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, index=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    bias_correction_f: Mapped[float] = mapped_column(Float, default=0.0)
    used_climatology: Mapped[bool] = mapped_column(Boolean, default=False)
    spread_f: Mapped[float] = mapped_column(Float, default=0.0)
    sources_failed: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Mean of bias-corrected ensemble peaks (post-correction). Used by `calibrate`
    # to compute (realized − forecast_mean) for the bias table update. Same unit
    # as the market — either °F or °C; see Market.metric for metric kind.
    forecast_mean_f: Mapped[float | None] = mapped_column(Float, nullable=True)

    market: Mapped[Market] = relationship(back_populates="forecasts")
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="forecast", cascade="all, delete-orphan"
    )


class Recommendation(Base):
    __tablename__ = "recommendation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecast.id"), index=True)
    outcome_label: Mapped[str] = mapped_column(String(64))
    p_model: Mapped[float] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float] = mapped_column(Float)
    ev_per_dollar: Mapped[float] = mapped_column(Float)
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0.0)
    kelly_size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    recommend: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    forecast: Mapped[Forecast] = relationship(back_populates="recommendations")


class Outcome(Base):
    __tablename__ = "outcome"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("market.id"), unique=True, index=True)
    winning_outcome_label: Mapped[str] = mapped_column(String(64))
    realized_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # e.g. observed °F
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)

    market: Mapped[Market] = relationship(back_populates="outcome")


class BiasEntryRow(Base):
    __tablename__ = "bias_entry"
    __table_args__ = (UniqueConstraint("station", "model", "month", name="uq_bias_smm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station: Mapped[str] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(String(32))
    month: Mapped[int] = mapped_column(Integer)
    mean_error_f: Mapped[float] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
