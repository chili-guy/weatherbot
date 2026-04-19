"""Thin repository over SQLAlchemy session — keeps query logic out of CLI."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..probability.calibration import BiasEntry, BiasKey, BiasTable
from .models import Base, BiasEntryRow, Forecast, Market, Outcome, Recommendation


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Repo:
    def __init__(self, db_url: str) -> None:
        self.engine = create_engine(db_url, future=True)
        self._SessionLocal = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns declared in models.py that are missing from an older DB.

        SQLite only: `ALTER TABLE ... ADD COLUMN`. Safe to run on every boot
        — if the column already exists we silently skip.
        """
        inspector = inspect(self.engine)

        def _add_missing(table: str, cols: dict[str, str]) -> None:
            if not inspector.has_table(table):
                return
            existing = {c["name"] for c in inspector.get_columns(table)}
            with self.engine.begin() as conn:
                for col, sql_type in cols.items():
                    if col not in existing:
                        try:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}"))
                        except Exception:
                            pass

        _add_missing("forecast", {"forecast_mean_f": "FLOAT"})
        _add_missing("market", {"unit": "VARCHAR(2)"})

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def upsert_market(
        self,
        *,
        polymarket_id: str,
        slug: str,
        question: str,
        metric: str,
        station_code: str | None,
        resolution_date: datetime | None,
        unit: str | None = None,
    ) -> int:
        with self.session() as s:
            existing = s.scalar(select(Market).where(Market.polymarket_id == polymarket_id))
            if existing is not None:
                existing.slug = slug
                existing.question = question
                existing.metric = metric
                existing.station_code = station_code
                existing.resolution_date = resolution_date
                existing.unit = unit
                return existing.id
            m = Market(
                polymarket_id=polymarket_id,
                slug=slug,
                question=question,
                metric=metric,
                station_code=station_code,
                resolution_date=resolution_date,
                unit=unit,
            )
            s.add(m)
            s.flush()
            return m.id

    def record_forecast(
        self,
        *,
        market_id: int,
        member_count: int,
        bias_correction_f: float,
        used_climatology: bool,
        spread_f: float,
        sources_failed: list[str] | None = None,
        forecast_mean_f: float | None = None,
    ) -> int:
        with self.session() as s:
            f = Forecast(
                market_id=market_id,
                member_count=member_count,
                bias_correction_f=bias_correction_f,
                used_climatology=used_climatology,
                spread_f=spread_f,
                sources_failed=",".join(sources_failed) if sources_failed else None,
                forecast_mean_f=forecast_mean_f,
            )
            s.add(f)
            s.flush()
            return f.id

    # ──────────────────────────────────────────────────────────────────
    #  Training-loop queries (resolve / calibrate / backtest)
    # ──────────────────────────────────────────────────────────────────

    def markets_awaiting_resolution(self, *, lookback_days: int = 21) -> list[Market]:
        """Markets whose resolution_date has passed in the last `lookback_days`
        and that don't yet have a recorded outcome."""
        cutoff = _utcnow_naive() - timedelta(days=lookback_days)
        with self.session() as s:
            stmt = (
                select(Market)
                .where(Market.resolution_date.is_not(None))
                .where(Market.resolution_date <= _utcnow_naive())
                .where(Market.resolution_date >= cutoff)
            )
            all_markets = s.scalars(stmt).all()
            resolved_ids = set(
                s.scalars(select(Outcome.market_id)).all()
            )
            return [m for m in all_markets if m.id not in resolved_ids]

    def forecast_outcome_pairs(self) -> list[tuple[Forecast, Outcome, Market]]:
        """Joined (Forecast, Outcome, Market) for every resolved market that
        has at least one recorded forecast with a non-null forecast_mean_f."""
        with self.session() as s:
            stmt = (
                select(Forecast, Outcome, Market)
                .join(Market, Forecast.market_id == Market.id)
                .join(Outcome, Outcome.market_id == Market.id)
                .where(Forecast.forecast_mean_f.is_not(None))
            )
            return list(s.execute(stmt).all())

    def recommendations_with_outcomes(
        self, *, from_date: datetime | None = None
    ) -> list[tuple[Recommendation, Outcome, Market, Forecast]]:
        """Every (Recommendation, Outcome, Market, Forecast) where the market
        resolved at or after `from_date`. Used for backtesting."""
        with self.session() as s:
            stmt = (
                select(Recommendation, Outcome, Market, Forecast)
                .join(Forecast, Recommendation.forecast_id == Forecast.id)
                .join(Market, Forecast.market_id == Market.id)
                .join(Outcome, Outcome.market_id == Market.id)
            )
            if from_date is not None:
                stmt = stmt.where(Market.resolution_date >= from_date)
            return list(s.execute(stmt).all())

    def record_recommendation(
        self,
        *,
        forecast_id: int,
        outcome_label: str,
        p_model: float,
        ask: float | None,
        mid: float | None,
        edge: float,
        ev_per_dollar: float,
        liquidity_usd: float,
        kelly_size_usd: float,
        recommend: bool,
        rejection_reason: str | None,
    ) -> None:
        with self.session() as s:
            r = Recommendation(
                forecast_id=forecast_id,
                outcome_label=outcome_label,
                p_model=p_model,
                ask=ask,
                mid=mid,
                edge=edge,
                ev_per_dollar=ev_per_dollar,
                liquidity_usd=liquidity_usd,
                kelly_size_usd=kelly_size_usd,
                recommend=recommend,
                rejection_reason=rejection_reason,
            )
            s.add(r)

    def record_outcome(
        self, *, market_id: int, winning_outcome_label: str, realized_value: float | None
    ) -> None:
        # Use SQLite UPSERT so repeated resolves for the same market are safe
        # regardless of session/identity-map quirks. `market_id` has a unique
        # index, so it's the natural conflict target.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with self.session() as s:
            stmt = sqlite_insert(Outcome).values(
                market_id=market_id,
                winning_outcome_label=winning_outcome_label,
                realized_value=realized_value,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["market_id"],
                set_={
                    "winning_outcome_label": winning_outcome_label,
                    "realized_value": realized_value,
                },
            )
            s.execute(stmt)

    def reset_training_data(
        self, *, bias: bool, forecasts: bool, recommendations: bool, outcomes: bool
    ) -> dict[str, int]:
        """Delete rows from the training tables. Returns row counts deleted."""
        from sqlalchemy import delete

        deleted: dict[str, int] = {}
        with self.session() as s:
            if recommendations:
                deleted["recommendation"] = s.execute(delete(Recommendation)).rowcount or 0
            if outcomes:
                deleted["outcome"] = s.execute(delete(Outcome)).rowcount or 0
            if forecasts:
                deleted["forecast"] = s.execute(delete(Forecast)).rowcount or 0
            if bias:
                deleted["bias_entry"] = s.execute(delete(BiasEntryRow)).rowcount or 0
        return deleted

    def load_bias_table(self) -> BiasTable:
        with self.session() as s:
            rows = s.scalars(select(BiasEntryRow)).all()
        entries = [
            BiasEntry(
                key=BiasKey(station=r.station, model=r.model, month=r.month),
                mean_error_f=r.mean_error_f,
                sample_count=r.sample_count,
            )
            for r in rows
        ]
        return BiasTable(entries=entries)

    def upsert_bias(self, entry: BiasEntry) -> None:
        with self.session() as s:
            row = s.scalar(
                select(BiasEntryRow).where(
                    BiasEntryRow.station == entry.key.station,
                    BiasEntryRow.model == entry.key.model,
                    BiasEntryRow.month == entry.key.month,
                )
            )
            if row is None:
                s.add(
                    BiasEntryRow(
                        station=entry.key.station,
                        model=entry.key.model,
                        month=entry.key.month,
                        mean_error_f=entry.mean_error_f,
                        sample_count=entry.sample_count,
                        updated_at=_utcnow_naive(),
                    )
                )
            else:
                row.mean_error_f = entry.mean_error_f
                row.sample_count = entry.sample_count
                row.updated_at = _utcnow_naive()
