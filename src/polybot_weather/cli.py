"""Polybot CLI — `polybot scan | analyze | recommend | backtest | calibrate | trade`."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import structlog
import typer
from rich.console import Console
from rich.table import Table

from .analysis import analyze_market, recommend, scan_weather_markets
from .config import get_settings
from .polymarket.gamma import GammaClient
from .reporting.dashboard import render_market_analysis, to_json
from .reporting.tui import run_tui
from .storage.repo import Repo
from .training.backtester import backtest as run_backtest
from .training.calibrator import recalibrate
from .training.resolver import resolve_pending


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


_configure_logging()

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
console = Console()


@app.command()
def scan() -> None:
    """List active weather markets discovered on Polymarket."""
    settings = get_settings()
    markets = asyncio.run(scan_weather_markets(settings))
    if not markets:
        console.print("[yellow]No active weather markets found.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Slug", overflow="fold")
    table.add_column("Question", overflow="fold")
    table.add_column("Ends", style="dim")
    for m in markets:
        table.add_row(m.id, m.slug, m.question, m.end_date_iso or "—")
    console.print(table)
    console.print(f"\n[dim]found {len(markets)} weather markets[/dim]")


@app.command()
def analyze(
    market: str = typer.Argument(..., help="Polymarket slug or numeric id"),
    json_out: bool = typer.Option(False, "--json", help="Print JSON instead of a rich table"),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist to SQLite"),
) -> None:
    """Run the full analysis on a single market."""
    settings = get_settings()
    repo = Repo(settings.db_url) if save else None
    gamma = GammaClient(user_agent=settings.user_agent)

    async def _run():
        gm = await gamma.get_market(market)
        if gm is None:
            console.print(f"[red]Market not found: {market}[/red]")
            raise typer.Exit(code=1)
        return await analyze_market(market=gm, settings=settings, repo=repo)

    analysis = asyncio.run(_run())
    if json_out:
        console.print_json(to_json(analysis))
    else:
        render_market_analysis(console, analysis)


@app.command(name="recommend")
def recommend_cmd(
    min_edge: float = typer.Option(None, "--min-edge", help="Override POLYBOT_MIN_EDGE"),
    save: bool = typer.Option(True, "--save/--no-save"),
) -> None:
    """Scan all weather markets and show those with positive edge."""
    settings = get_settings()
    if min_edge is not None:
        settings.min_edge = min_edge
    repo = Repo(settings.db_url) if save else None
    analyses = asyncio.run(recommend(settings=settings, repo=repo))
    if not analyses:
        console.print("[yellow]No markets cleared the edge thresholds.[/yellow]")
        return
    for a in analyses:
        render_market_analysis(console, a)


@app.command()
def dash(
    save: bool = typer.Option(True, "--save/--no-save", help="Persist to SQLite"),
) -> None:
    """Launch the interactive terminal dashboard."""
    settings = get_settings()
    repo = Repo(settings.db_url) if save else None
    try:
        asyncio.run(run_tui(settings=settings, repo=repo))
    except KeyboardInterrupt:
        pass


@app.command()
def resolve(
    lookback_days: int = typer.Option(21, "--lookback-days", help="How far back to scan"),
) -> None:
    """Pull realized extremes from the ERA5 archive for expired markets."""
    settings = get_settings()
    repo = Repo(settings.db_url)
    records = asyncio.run(resolve_pending(settings=settings, repo=repo, lookback_days=lookback_days))
    if not records:
        console.print("[yellow]No markets awaiting resolution.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", overflow="fold")
    table.add_column("Metric")
    table.add_column("Realized", justify="right")
    table.add_column("Note", style="dim", overflow="fold")
    n_recorded = 0
    for r in records:
        realized = f"{r.realized_value:.2f}" if r.realized_value is not None else "—"
        table.add_row(r.slug, r.metric, realized, r.note or "")
        if r.realized_value is not None:
            n_recorded += 1
    console.print(table)
    console.print(f"\n[dim]{n_recorded}/{len(records)} resolved, rest deferred (archive lag).[/dim]")


@app.command()
def calibrate() -> None:
    """Recompute per-(station, model, month) bias from stored outcomes."""
    settings = get_settings()
    repo = Repo(settings.db_url)
    summary = recalibrate(repo=repo)
    console.print(
        f"[green]{summary.entries_written}[/green] bias entries written · "
        f"[yellow]{summary.entries_skipped_insufficient_samples}[/yellow] skipped (n<5) · "
        f"considered {summary.pairs_considered} forecast/outcome pairs."
    )


@app.command()
def backtest(
    from_date: str = typer.Option(None, "--from", help="ISO date — earliest resolution to include"),
) -> None:
    """Score prior recommendations against realized outcomes."""
    settings = get_settings()
    repo = Repo(settings.db_url)
    parsed_from: datetime | None = None
    if from_date:
        try:
            parsed_from = datetime.fromisoformat(from_date)
        except ValueError:
            console.print(f"[red]--from must be ISO-8601, got {from_date!r}[/red]")
            raise typer.Exit(code=2) from None

    report = run_backtest(repo=repo, from_date=parsed_from, fee_rate=settings.fee_rate)
    if report.n_scored == 0:
        console.print(
            f"[yellow]No scorable recommendations yet "
            f"({report.n_recommended} recommended, {report.n_unparseable} unparseable).[/yellow]"
        )
        return

    def _fmt(v: float | None, spec: str = ".4f") -> str:
        return format(v, spec) if v is not None else "—"

    table = Table(show_header=False, box=None)
    table.add_row("Scored",        str(report.n_scored))
    table.add_row("Brier",         _fmt(report.brier))
    table.add_row("Log-loss",      _fmt(report.log_loss))
    table.add_row("Hit rate",      _fmt(report.hit_rate, ".2%"))
    table.add_row("Avg edge",      _fmt(report.avg_edge, ".3f"))
    table.add_row("Stake",         f"${report.simulated_stake_usd:,.2f}")
    table.add_row("Simulated P&L", f"${report.simulated_pnl_usd:,.2f}")
    table.add_row("ROI",           _fmt(report.roi, ".2%"))
    console.print(table)

    if report.calibration:
        cal = Table(show_header=True, header_style="bold", title="Calibration")
        cal.add_column("Bucket")
        cal.add_column("Predicted", justify="right")
        cal.add_column("Empirical", justify="right")
        cal.add_column("n", justify="right")
        for b in report.calibration:
            cal.add_row(
                f"{b.p_low:.1f}–{b.p_high:.1f}",
                f"{b.predicted_mean:.3f}",
                f"{b.empirical_rate:.3f}",
                str(b.count),
            )
        console.print(cal)


@app.command(name="reset-training")
def reset_training(
    scope: str = typer.Option(
        "bias",
        "--scope",
        help="bias | data | all. 'bias' wipes the bias table only; 'data' also "
             "wipes forecast/recommendation/outcome; 'all' wipes bias + data.",
    ),
    confirm: bool = typer.Option(False, "--confirm", help="REQUIRED to actually delete."),
) -> None:
    """Wipe training-loop tables contaminated by prior parser/unit bugs.

    `market` rows are never touched — only the derived training data.
    """
    if scope not in {"bias", "data", "all"}:
        console.print(f"[red]--scope must be one of bias|data|all, got {scope!r}[/red]")
        raise typer.Exit(code=2)
    if not confirm:
        console.print(
            "[yellow]Dry run. Re-run with [bold]--confirm[/bold] to delete. "
            "Scope presets:[/yellow]\n"
            "  • bias → bias_entry only\n"
            "  • data → forecast + recommendation + outcome (keeps market)\n"
            "  • all  → bias_entry + forecast + recommendation + outcome"
        )
        raise typer.Exit(code=0)

    settings = get_settings()
    repo = Repo(settings.db_url)
    flags = {
        "bias":  scope in ("bias", "all"),
        "forecasts":       scope in ("data", "all"),
        "recommendations": scope in ("data", "all"),
        "outcomes":        scope in ("data", "all"),
    }
    deleted = repo.reset_training_data(**flags)
    for table, n in deleted.items():
        console.print(f"[dim]deleted {n:>6} rows from[/dim] [bold]{table}[/bold]")
    console.print(
        "[green]Done.[/green] Next: run [bold]recommend[/bold] over the coming days, "
        "then [bold]resolve → calibrate → backtest[/bold] once enough clean data accumulates."
    )


@app.command()
def trade(
    market: str = typer.Argument(..., help="Polymarket slug or id"),
    outcome: str = typer.Argument(..., help="Outcome label, must match exactly"),
    size: float = typer.Option(..., "--size", help="USD size"),
    confirm: bool = typer.Option(False, "--confirm", help="REQUIRED to actually submit"),
) -> None:
    """Place a real order — disabled by default; requires env flag and --confirm."""
    settings = get_settings()
    if not confirm:
        console.print("[red]Refusing to trade without --confirm.[/red]")
        raise typer.Exit(code=2)

    from .execution.trader import (
        ExecutionDisabled,
        ExecutionMisconfigured,
        place_order,
    )

    gamma = GammaClient(user_agent=settings.user_agent)

    async def _run():
        gm = await gamma.get_market(market)
        if gm is None or outcome not in gm.outcomes:
            console.print("[red]Market or outcome not found.[/red]")
            raise typer.Exit(code=1)
        idx = gm.outcomes.index(outcome)
        if idx >= len(gm.clob_token_ids):
            console.print("[red]No CLOB token id for this outcome.[/red]")
            raise typer.Exit(code=1)
        token_id = gm.clob_token_ids[idx]

        # Use current best ask as the limit price (caller can extend later).
        from .polymarket.clob import ClobClient

        clob = ClobClient(user_agent=settings.user_agent)
        book = await clob.book(token_id)
        ask = book.best_ask() if book else None
        if ask is None:
            console.print("[red]No ask in book; refusing to send a market-equivalent order blindly.[/red]")
            raise typer.Exit(code=1)
        return token_id, ask.price

    try:
        token_id, price = asyncio.run(_run())
        receipt = place_order(
            settings=settings, token_id=token_id, side="BUY", price=price, size_usd=size
        )
        console.print(f"[green]order placed: {receipt}[/green]")
    except ExecutionDisabled as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=3) from e
    except ExecutionMisconfigured as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=3) from e


if __name__ == "__main__":
    app()
