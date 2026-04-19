"""CLI rendering for analyses — `rich` tables and JSON export."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table

from ..analysis import MarketAnalysis


def render_market_analysis(console: Console, analysis: MarketAnalysis) -> None:
    m = analysis.market
    console.rule(f"[bold]{m.question}")
    console.print(f"slug: [cyan]{m.slug}[/cyan]   id: {m.id}")
    if analysis.station:
        console.print(
            f"station: [yellow]{analysis.station.code}[/yellow] "
            f"({analysis.station.name}, {analysis.station.timezone})"
        )
    if analysis.parsed.resolution_date:
        console.print(f"resolves: {analysis.parsed.resolution_date.isoformat()}")
    if analysis.ensemble:
        u = analysis.parsed.unit or "F"
        console.print(
            f"ensemble: {analysis.ensemble.member_count} members  "
            f"spread: {analysis.ensemble.spread_f:.1f}°{u}  "
            f"bias-corr: {analysis.ensemble.bias_correction_f:+.2f}°{u}  "
            f"climatology mixed: {analysis.ensemble.used_climatology}"
        )
    if analysis.sources_failed:
        console.print(f"[red]sources failed:[/red] {', '.join(analysis.sources_failed)}")
    if analysis.error:
        console.print(f"[red]error: {analysis.error}[/red]")
        return

    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("Faixa")
    table.add_column("p_modelo", justify="right")
    table.add_column("ask", justify="right")
    table.add_column("mid", justify="right")
    table.add_column("edge", justify="right")
    table.add_column("EV/$", justify="right")
    table.add_column("Kelly $", justify="right")
    table.add_column("rec", justify="center")

    for o in analysis.outcomes:
        marker = "[bold green]⭐[/bold green]" if o.edge.recommend else ""
        table.add_row(
            o.label,
            f"{o.p_model*100:5.1f}%",
            f"{o.ask:.3f}" if o.ask is not None else "—",
            f"{o.mid:.3f}" if o.mid is not None else "—",
            f"{o.edge.edge*100:+5.1f}pp",
            f"{o.edge.ev_per_dollar*100:+5.0f}%",
            f"${o.kelly_size_usd:,.2f}" if o.kelly_size_usd > 0 else "—",
            marker,
        )

    console.print(table)

    rec = next((o for o in analysis.outcomes if o.edge.recommend), None)
    if rec:
        console.print(
            f"[bold green]RECOMMENDATION[/bold green]: BUY YES on \"{rec.label}\" "
            f"at {rec.ask:.3f}, size ${rec.kelly_size_usd:,.2f}"
        )
    else:
        reasons = {o.edge.rejection_reason for o in analysis.outcomes if o.edge.rejection_reason}
        console.print(f"[dim]no recommendation — {', '.join(reasons) if reasons else 'no edge'}[/dim]")


def analysis_to_dict(analysis: MarketAnalysis) -> dict[str, Any]:
    return {
        "market": {
            "id": analysis.market.id,
            "slug": analysis.market.slug,
            "question": analysis.market.question,
            "end_date": analysis.market.end_date_iso,
        },
        "station": (
            {
                "code": analysis.station.code,
                "name": analysis.station.name,
                "timezone": analysis.station.timezone,
            }
            if analysis.station
            else None
        ),
        "resolution_date": (
            analysis.parsed.resolution_date.isoformat() if analysis.parsed.resolution_date else None
        ),
        "ensemble": (
            {
                "member_count": analysis.ensemble.member_count,
                "bias_correction_f": analysis.ensemble.bias_correction_f,
                "used_climatology": analysis.ensemble.used_climatology,
                "spread_f": analysis.ensemble.spread_f,
            }
            if analysis.ensemble
            else None
        ),
        "outcomes": [
            {
                "label": o.label,
                "p_model": o.p_model,
                "ask": o.ask,
                "mid": o.mid,
                "edge": o.edge.edge,
                "ev_per_dollar": o.edge.ev_per_dollar,
                "liquidity_usd": o.edge.liquidity_usd,
                "kelly_size_usd": o.kelly_size_usd,
                "recommend": o.edge.recommend,
                "rejection_reason": o.edge.rejection_reason,
            }
            for o in analysis.outcomes
        ],
        "sources_failed": analysis.sources_failed,
        "error": analysis.error,
    }


def to_json(analysis: MarketAnalysis) -> str:
    return json.dumps(analysis_to_dict(analysis), indent=2, ensure_ascii=False)
