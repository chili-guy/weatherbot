"""Polybot interactive dashboard — Rich Live, multi-panel, auto-cycling detail view."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis import (
    MarketAnalysis,
    analyze_many,
    filter_tradeable_soon,
    scan_weather_markets,
)
from ..config import Settings
from ..execution.wallet import WalletSnapshot, fetch_wallet_snapshot
from ..storage.repo import Repo


REFRESH_SECONDS = 300
DETAIL_CYCLE_SECONDS = 6
MAX_TABLE_ROWS = 14
ANALYZE_CAP = 80  # hard cap per cycle — otherwise a live scan will hammer APIs
WALLET_REFRESH_SECONDS = 60
DESCRIPTION_MAX_CHARS = 260
TITLE_MAX_CHARS = 64
NEAR_MISS_ROWS = 6


# ──────────────────────────────────────────────────────────────────────────
#   Small helpers
# ──────────────────────────────────────────────────────────────────────────

def _edge_color(edge: float) -> str:
    if edge >= 0.15:
        return "bold bright_green"
    if edge >= 0.08:
        return "green"
    if edge >= 0.03:
        return "yellow"
    if edge > 0:
        return "dim"
    return "red"


def _prob_bar(p: float, width: int = 12, color: str = "cyan") -> Text:
    """Unicode block-fill bar for probability (0..1)."""
    blocks = "▏▎▍▌▋▊▉█"
    total = max(0.0, min(1.0, p)) * width
    full = int(total)
    frac = total - full
    bar = "█" * full
    if frac > 0 and full < width:
        bar += blocks[int(frac * len(blocks))]
    bar = bar.ljust(width)
    return Text(bar, style=color)


def _fmt_usd(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}k"
    return f"${x:,.2f}"


def _short_addr(addr: str | None) -> str:
    if not addr:
        return "—"
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _hours_until(end_iso: str | None) -> float | None:
    if not end_iso:
        return None
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        end = _dt.fromisoformat(end_iso.replace("Z", "+00:00"))
        now = _dt.now(ZoneInfo("UTC"))
        return max((end - now).total_seconds() / 3600.0, 0.0)
    except Exception:
        return None


def _truncate(text: str | None, max_chars: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _bin_display_label(b) -> str:
    """Short canonical label derived from (low, high, unit).

    `TempBin.label` stores the ORIGINAL input string — for binary markets that
    is the whole question, which is useless for display. Recompute from the
    numeric range so bars/rows always show e.g. `35°C` or `≤61°F`.
    """
    u = b.unit
    if b.low is not None and b.high is not None:
        if b.low == b.high:
            return f"{int(b.low)}°{u}"
        return f"{int(b.low)}-{int(b.high)}°{u}"
    if b.low is not None:
        return f"≥{int(b.low)}°{u}"
    if b.high is not None:
        return f"≤{int(b.high)}°{u}"
    return b.label or "?"


# ──────────────────────────────────────────────────────────────────────────
#   Dashboard state
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class DashState:
    analyses: list[MarketAnalysis] = field(default_factory=list)
    scanning: bool = False
    scan_total: int = 0
    scan_done: int = 0
    last_scan_at: datetime | None = None
    last_error: str | None = None
    detail_idx: int = 0
    bankroll_usd: float = 0.0
    wallet: WalletSnapshot | None = None

    @property
    def recommendations(self) -> list[tuple[MarketAnalysis, object]]:
        recs: list[tuple[MarketAnalysis, object]] = []
        for a in self.analyses:
            for o in a.outcomes:
                if o.edge.recommend:
                    recs.append((a, o))
        recs.sort(key=lambda t: t[1].edge.edge, reverse=True)
        return recs

    @property
    def near_miss(self) -> list[tuple[MarketAnalysis, object]]:
        """Outcomes that had positive edge but failed a downstream gate."""
        out: list[tuple[MarketAnalysis, object]] = []
        for a in self.analyses:
            for o in a.outcomes:
                if not o.edge.recommend and o.edge.edge > 0.02:
                    out.append((a, o))
        out.sort(key=lambda t: t[1].edge.edge, reverse=True)
        return out

    @property
    def total_markets(self) -> int:
        return len(self.analyses)

    @property
    def best_edge(self) -> float:
        recs = self.recommendations
        return recs[0][1].edge.edge if recs else 0.0

    @property
    def total_kelly(self) -> float:
        return sum(o.kelly_size_usd for a in self.analyses for o in a.outcomes if o.edge.recommend)

    @property
    def rejection_summary(self) -> list[tuple[str, int]]:
        from collections import Counter
        c: Counter[str] = Counter()
        for a in self.analyses:
            for o in a.outcomes:
                if not o.edge.recommend and o.edge.rejection_reason:
                    reason = o.edge.rejection_reason.split(" ")[0]
                    c[reason] += 1
        return c.most_common(4)


# ──────────────────────────────────────────────────────────────────────────
#   Panels
# ──────────────────────────────────────────────────────────────────────────

def header_panel(state: DashState) -> Panel:
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(justify="left", ratio=2)
    grid.add_column(justify="center", ratio=2)
    grid.add_column(justify="right", ratio=3)

    left = Text.assemble(
        ("⚡ POLYBOT ", "bold bright_yellow"),
        ("• MERCADOS DE CLIMA ", "bold white"),
        ("• AO VIVO", "bold black on bright_green"),
    )

    status = "VARRENDO" if state.scanning else "OCIOSO"
    status_style = "black on yellow" if state.scanning else "black on bright_cyan"

    rej = state.rejection_summary
    rej_txt = "  ".join(f"{r}:{n}" for r, n in rej) if rej else ""

    middle = Text.assemble(
        (f" {status} ", f"bold {status_style}"),
        ("  mercados:", "dim"),
        (f" {state.total_markets} ", "bold white"),
        ("  edge+:", "dim"),
        (f" {len(state.recommendations)} ", "bold bright_green"),
        ("  near-miss:", "dim"),
        (f" {len(state.near_miss)} ", "bold yellow"),
        (f"   {rej_txt}", "dim"),
    )

    w = state.wallet
    if w and w.connected:
        wallet_part = Text.assemble(
            ("carteira ", "dim"),
            (_short_addr(w.funder_address), "bright_white"),
            ("  USDC ", "dim"),
            (_fmt_usd(w.usdc_available), "bold bright_green"),
            ("  em ordens ", "dim"),
            (_fmt_usd(w.usdc_in_orders), "yellow"),
        )
    else:
        wallet_part = Text.assemble(
            ("carteira ", "dim"),
            ("desconectada", "dim italic"),
        )

    right = Text.assemble(
        wallet_part,
        ("   kelly ", "dim"),
        (_fmt_usd(state.total_kelly), "bold bright_green"),
        ("   "),
        (datetime.now().strftime("%H:%M:%S"), "bold cyan"),
    )

    grid.add_row(left, middle, right)
    return Panel(grid, style="on grey7", box=box.HEAVY_EDGE, padding=(0, 1))


def leaderboard_panel(state: DashState) -> Panel:
    t = Table(expand=True, box=box.SIMPLE_HEAVY, show_edge=False,
              header_style="bold white on grey19",
              row_styles=["", "on grey11"], pad_edge=False)
    t.add_column("#", width=3, justify="right", style="dim")
    # Key fix: allow the market cell to WRAP instead of ellipsis-truncating.
    t.add_column("Mercado / Faixa", ratio=5, overflow="fold", no_wrap=False)
    t.add_column("estação", width=7, style="dim", overflow="fold")
    t.add_column("p̂", justify="right", width=6)
    t.add_column("ask", justify="right", width=6)
    t.add_column("edge", justify="right", width=7)
    t.add_column("EV/$", justify="right", width=6)
    t.add_column("liq", justify="right", width=7, style="dim")
    t.add_column("resolve", width=7, justify="right", style="dim")
    t.add_column("Kelly", justify="right", width=8)

    recs = state.recommendations[:MAX_TABLE_ROWS]
    if not recs:
        msg = Text("nenhuma oportunidade com edge positivo no momento — ", style="dim italic")
        if state.near_miss:
            a, o = state.near_miss[0]
            msg.append(
                f"melhor near-miss: {a.market.event_title or a.market.question} "
                f"(edge +{o.edge.edge*100:.1f}pp, {o.edge.rejection_reason})",
                style="yellow",
            )
        else:
            msg.append("aguardando ciclo de scan", style="dim")
        body = Group(
            Text(""),
            Align.center(msg),
            Text(""),
        )
        return Panel(
            body,
            title=Text.assemble(("🔥 ", ""), ("OPORTUNIDADES", "bold bright_green")),
            border_style="bright_green",
            box=box.ROUNDED,
        )

    for i, (a, o) in enumerate(recs, start=1):
        title = a.market.event_title or a.market.question
        title = _truncate(title, TITLE_MAX_CHARS)
        bin_lbl = a.market.group_item_title or o.label
        # Single-line compact cell: title · bin. Slug moved to detail panel.
        market_cell = Text()
        rank_style = "bold bright_yellow" if i == 1 else ("bold bright_green" if i <= 3 else "white")
        market_cell.append(title, style="white")
        if bin_lbl and bin_lbl.lower() not in title.lower():
            market_cell.append(f"  ·  {bin_lbl}", style="bright_cyan")
        edge_c = _edge_color(o.edge.edge)
        hours = _hours_until(a.market.end_date_iso)
        hours_txt = f"{hours:.0f}h" if hours is not None else "—"
        t.add_row(
            Text(str(i), style=rank_style),
            market_cell,
            a.station.code if a.station else "—",
            f"{o.p_model*100:.0f}%",
            f"{o.ask:.2f}" if o.ask is not None else "—",
            Text(f"+{o.edge.edge*100:.1f}pp", style=edge_c),
            Text(f"+{o.edge.ev_per_dollar*100:.0f}%", style=edge_c),
            _fmt_usd(o.edge.liquidity_usd),
            hours_txt,
            Text(_fmt_usd(o.kelly_size_usd), style="bold bright_green"),
        )

    title_cell = Text.assemble(
        ("🔥 ", ""),
        ("OPORTUNIDADES", "bold bright_green"),
        (f"   ({len(state.recommendations)} total, mostrando {len(recs)})", "dim"),
    )
    return Panel(t, title=title_cell, border_style="bright_green", box=box.ROUNDED)


def near_miss_panel(state: DashState) -> Panel:
    """Markets with positive model edge that failed a gate (liquidity, time)."""
    all_rows = state.near_miss
    rows = all_rows[:NEAR_MISS_ROWS]
    t = Table(expand=True, box=box.SIMPLE, show_edge=False,
              header_style="bold white on grey19", pad_edge=False)
    t.add_column("Mercado", ratio=5, overflow="ellipsis", no_wrap=True)
    t.add_column("edge", justify="right", width=7)
    t.add_column("motivo", ratio=3, overflow="ellipsis", no_wrap=True, style="yellow")

    if not rows:
        t.add_row(Text("—", style="dim"), "", "")
    else:
        for a, o in rows:
            title = a.market.event_title or a.market.question
            bin_lbl = a.market.group_item_title or o.label
            compact = f"{_truncate(title, 50)}"
            if bin_lbl and bin_lbl.lower() not in title.lower():
                compact += f" · {bin_lbl}"
            t.add_row(
                Text(compact, style="white"),
                Text(f"+{o.edge.edge*100:.1f}pp", style=_edge_color(o.edge.edge)),
                o.edge.rejection_reason or "",
            )

    extra = len(all_rows) - len(rows)
    title = "QUASE LÁ (edge positivo, bloqueados)"
    if extra > 0:
        title += f"  (+{extra} mais)"
    return Panel(t, title=f"[bold yellow]{title}[/bold yellow]",
                 border_style="yellow", box=box.ROUNDED)


def _distribution_chart(analysis: MarketAnalysis) -> RenderableType:
    """Render the model's probability distribution as a horizontal bar chart."""
    if not analysis.ensemble or not analysis.ensemble.distribution:
        return Text("—  distribuição do ensemble indisponível", style="dim")

    dist = list(analysis.ensemble.distribution)
    if len(dist) == 1:
        p = dist[0].probability
        lbl = _bin_display_label(dist[0].bin)
        rows = [(lbl, p), (f"fora de {lbl}", 1.0 - p)]
    else:
        rows = [(_bin_display_label(bp.bin), bp.probability) for bp in dist]

    chart = Table.grid(expand=True, padding=(0, 1))
    chart.add_column(width=18, overflow="ellipsis", no_wrap=True)
    chart.add_column(ratio=1)
    chart.add_column(width=6, justify="right")

    p_max = max((p for _, p in rows), default=1.0) or 1.0
    for label, p in rows:
        colour = "bright_cyan"
        if p == p_max:
            colour = "bold bright_yellow"
        chart.add_row(
            Text(label, style="white"),
            _prob_bar(p / p_max if p_max > 0 else 0.0, width=32, color=colour),
            Text(f"{p*100:.1f}%", style=colour),
        )
    return chart


def detail_panel(analysis: Optional[MarketAnalysis]) -> Panel:
    if analysis is None:
        body = Align.center(
            Text("\n\nvarrendo — os detalhes aparecem aqui\n\n", style="dim italic"),
            vertical="middle",
        )
        return Panel(body, title="[bold cyan]DETALHES[/bold cyan]",
                     border_style="cyan", box=box.ROUNDED)

    m = analysis.market
    parsed = analysis.parsed
    ens = analysis.ensemble

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right", no_wrap=True)
    meta.add_column(style="bright_white", overflow="fold")

    station_line = (
        f"{analysis.station.name} ({analysis.station.code}, {analysis.station.timezone})"
        if analysis.station else "—"
    )
    res_date = parsed.resolution_date.isoformat() if parsed.resolution_date else "—"
    hours_left = _hours_until(m.end_date_iso)
    hours_txt = f"{hours_left:.1f}h" if hours_left is not None else "—"

    metric_pt = {
        "max_temp": "temp. máxima",
        "min_temp": "temp. mínima",
        "hurricane": "furacão",
        "snowfall": "neve",
        "rainfall": "chuva",
        "unknown": "desconhecida",
    }.get(parsed.metric, parsed.metric)

    meta.add_row("estação", station_line)
    meta.add_row("resolve", f"{res_date}   (em {hours_txt})")
    meta.add_row("métrica / unidade", f"{metric_pt}  •  °{parsed.unit or '?'}")
    if parsed.bins:
        bins_txt = ", ".join(_bin_display_label(b) for b in parsed.bins[:8])
        if len(parsed.bins) > 8:
            bins_txt += f"  (+{len(parsed.bins)-8} bins)"
        meta.add_row("faixas parseadas", bins_txt)
    if ens:
        meta.add_row(
            "ensemble",
            f"{ens.member_count} membros  •  dispersão {ens.spread_f:.1f}°  "
            f"•  viés {ens.bias_correction_f:+.2f}°"
            + ("  •  +climatologia" if ens.used_climatology else "  •  sem climatologia"),
        )
    if m.volume_24hr is not None or m.liquidity is not None:
        vol = m.volume_24hr or 0.0
        liq = m.liquidity or 0.0
        meta.add_row("volume 24h  /  liquidez", f"{_fmt_usd(vol)}   /   {_fmt_usd(liq)}")
    meta.add_row("confiança parser", f"{parsed.confidence*100:.0f}%")
    if parsed.notes:
        meta.add_row("notas parser", Text("; ".join(parsed.notes), style="yellow"))
    if m.resolution_source:
        meta.add_row("fonte de resolução", _truncate(m.resolution_source, 160))
    if m.description:
        meta.add_row("descrição", _truncate(m.description, DESCRIPTION_MAX_CHARS))
    if analysis.sources_failed:
        meta.add_row("fontes falharam", Text(", ".join(analysis.sources_failed), style="red"))
    if analysis.error:
        meta.add_row("erro", Text(analysis.error, style="bold red"))

    t = Table(expand=True, box=box.MINIMAL_HEAVY_HEAD, header_style="bold cyan")
    t.add_column("Resultado", ratio=3, overflow="fold", no_wrap=False)
    t.add_column("p̂", justify="right", width=7)
    t.add_column("ask", justify="right", width=7)
    t.add_column("mid", justify="right", width=7, style="dim")
    t.add_column("edge", justify="right", width=8)
    t.add_column("EV/$", justify="right", width=7)
    t.add_column("liq", justify="right", width=7, style="dim")
    t.add_column("Kelly", justify="right", width=9)
    t.add_column("", width=3, justify="center")

    for o in analysis.outcomes:
        marker = "★" if o.edge.recommend else ""
        colour = _edge_color(o.edge.edge)
        row_style = "bold bright_green" if o.edge.recommend else ""
        ask_txt = f"{o.ask:.3f}" if o.ask is not None else "—"
        mid_txt = f"{o.mid:.3f}" if o.mid is not None else "—"
        kelly_txt = _fmt_usd(o.kelly_size_usd) if o.kelly_size_usd > 0 else "—"
        t.add_row(
            o.label,
            f"{o.p_model*100:.1f}%",
            ask_txt,
            mid_txt,
            Text(f"{o.edge.edge*100:+.1f}pp", style=colour),
            f"{o.edge.ev_per_dollar*100:+.0f}%",
            _fmt_usd(o.edge.liquidity_usd),
            kelly_txt,
            Text(marker, style="bold bright_yellow"),
            style=row_style,
        )

    rec = next((o for o in analysis.outcomes if o.edge.recommend), None)
    if rec:
        banner = Text.assemble(
            (" COMPRAR ", "bold black on bright_green"),
            ("  "),
            (f"'{rec.label}' @ {rec.ask:.3f}", "bold bright_green"),
            ("  →  ", "dim"),
            (f"tamanho {_fmt_usd(rec.kelly_size_usd)}", "bold"),
            (f"  (edge +{rec.edge.edge*100:.1f}pp, EV/$ +{rec.edge.ev_per_dollar*100:.0f}%)",
             "dim"),
        )
    else:
        reasons = {o.edge.rejection_reason for o in analysis.outcomes if o.edge.rejection_reason}
        reason_txt = " / ".join(sorted(filter(None, reasons))) or "sem edge"
        banner = Text(f"sem recomendação — {reason_txt}", style="dim italic")

    title = m.event_title or m.question
    header = Text.assemble(
        ("📄  ", "bold dim"),
        (title, "bold bright_white"),
    )
    subheader = Text(f"slug {m.slug}   id {m.id}", style="dim")

    # Order: actionable first (banner + outcomes table), then context (meta),
    # then distribution chart at the bottom. Clipping at the bottom is benign;
    # clipping the outcomes table (as seen before) is not.
    group = Group(
        header, subheader, Text(""),
        banner, Text(""),
        t, Text(""),
        meta, Text(""),
        Panel(_distribution_chart(analysis), title="distribuição do modelo",
              border_style="grey37", box=box.MINIMAL, padding=(0, 1)),
    )

    border = "bright_green" if rec else "cyan"
    return Panel(group, title="[bold cyan]DETALHES DO MERCADO[/bold cyan]",
                 border_style=border, box=box.ROUNDED, padding=(1, 2))


def wallet_panel(wallet: WalletSnapshot | None, bankroll_cfg: float) -> Panel:
    if wallet is None:
        body = Align.center(
            Text("buscando carteira…", style="dim italic"), vertical="middle",
        )
        return Panel(body, title="[bold magenta]CARTEIRA[/bold magenta]",
                     border_style="magenta", box=box.ROUNDED)

    if not wallet.connected:
        hint = Text.assemble(
            ("defina ", "dim"),
            ("POLYBOT_PRIVATE_KEY", "bold yellow"),
            (" e ", "dim"),
            ("POLYBOT_FUNDER_ADDRESS", "bold yellow"),
            (" no .env para habilitar.", "dim"),
        )
        body = Group(
            Align.center(Text("carteira desconectada", style="bold magenta")),
            Align.center(hint),
            Text(""),
            Align.center(
                Text(f"banca configurada: {_fmt_usd(bankroll_cfg)}", style="dim"),
            ),
        )
        if wallet.last_error:
            body = Group(body, Align.center(Text(wallet.last_error, style="red dim")))
        return Panel(body, title="[bold magenta]CARTEIRA[/bold magenta]",
                     border_style="magenta", box=box.ROUNDED)

    head = Table.grid(expand=True, padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True)
    head.add_column(style="bright_white")
    head.add_row("endereço", wallet.funder_address or "—")
    head.add_row("USDC disponível", Text(_fmt_usd(wallet.usdc_available), style="bold bright_green"))
    head.add_row("USDC em ordens", Text(_fmt_usd(wallet.usdc_in_orders), style="yellow"))
    head.add_row("ordens abertas", str(wallet.open_orders_count))
    head.add_row("banca configurada", _fmt_usd(bankroll_cfg))
    head.add_row(
        "atualizado",
        wallet.fetched_at.strftime("%H:%M:%S") + (f"  ⚠ {wallet.last_error}" if wallet.last_error else ""),
    )

    trades_t = Table(expand=True, box=box.SIMPLE, show_edge=False,
                     header_style="bold white on grey19", pad_edge=False)
    trades_t.add_column("quando", width=8, style="dim")
    trades_t.add_column("mercado", ratio=3, overflow="fold")
    trades_t.add_column("lado", width=4)
    trades_t.add_column("preço", justify="right", width=7)
    trades_t.add_column("tam", justify="right", width=8)
    if not wallet.recent_trades:
        trades_t.add_row(Text("—", style="dim"), Text("nenhum trade ainda", style="dim italic"), "", "", "")
    else:
        for tr in wallet.recent_trades:
            ts = tr.ts.strftime("%d/%m %H:%M") if tr.ts else "—"
            trades_t.add_row(
                ts,
                Text(tr.market_slug or "?", style="white"),
                Text(tr.side, style=("bright_green" if tr.side.upper() == "BUY" else "red")),
                f"{tr.price:.3f}" if tr.price else "—",
                _fmt_usd(tr.size * tr.price) if tr.price else f"{tr.size:.2f}",
            )

    body = Group(
        head,
        Text(""),
        Panel(trades_t, title="trades recentes", border_style="grey37",
              box=box.MINIMAL, padding=(0, 1)),
    )
    return Panel(body, title="[bold magenta]CARTEIRA[/bold magenta]",
                 border_style="magenta", box=box.ROUNDED, padding=(0, 1))


def footer_panel(state: DashState, countdown: int | None) -> Panel:
    bar = Table.grid(expand=True, padding=(0, 2))
    bar.add_column(justify="left", ratio=2)
    bar.add_column(justify="right", ratio=1)

    if state.scanning:
        pct = (state.scan_done / state.scan_total * 100) if state.scan_total else 0
        blocks = int(pct / 100 * 24)
        bar_str = "█" * blocks + "░" * (24 - blocks)
        left = Text.assemble(
            ("analisando ", "dim"),
            (f"{state.scan_done}/{state.scan_total}", "bright_white"),
            ("  ["),
            (bar_str, "bright_yellow"),
            (f"]  {pct:.0f}%"),
        )
    elif countdown is not None:
        left = Text.assemble(
            ("próximo scan em ", "dim"),
            (f"{countdown}s", "bright_cyan"),
            ("   •   ", "dim"),
            ("ctrl+c para sair", "dim italic"),
        )
    else:
        left = Text("ocioso", style="dim italic")

    if state.last_error:
        right = Text(f"⚠ {state.last_error[:80]}", style="bold red")
    elif state.last_scan_at:
        right = Text(
            f"último scan {state.last_scan_at.strftime('%H:%M:%S')}",
            style="dim",
        )
    else:
        right = Text("")

    bar.add_row(left, right)
    return Panel(bar, style="on grey7", box=box.HEAVY_EDGE, padding=(0, 1))


# ──────────────────────────────────────────────────────────────────────────
#   Layout + loop
# ──────────────────────────────────────────────────────────────────────────

def _build_layout() -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    root["body"].split_row(
        Layout(name="left", ratio=5),
        Layout(name="right", ratio=7),
    )
    root["left"].split_column(
        Layout(name="opportunities", ratio=3),
        Layout(name="near_miss", ratio=2),
    )
    root["right"].split_column(
        Layout(name="detail", ratio=3),
        Layout(name="wallet", ratio=2),
    )
    return root


def _render(layout: Layout, state: DashState, countdown: int | None, bankroll_cfg: float) -> None:
    layout["header"].update(header_panel(state))
    layout["opportunities"].update(leaderboard_panel(state))
    layout["near_miss"].update(near_miss_panel(state))

    recs = state.recommendations
    detail: MarketAnalysis | None = None
    if recs:
        detail = recs[state.detail_idx % len(recs)][0]
    elif state.analyses:
        detail = state.analyses[state.detail_idx % len(state.analyses)]
    layout["detail"].update(detail_panel(detail))
    layout["wallet"].update(wallet_panel(state.wallet, bankroll_cfg))

    layout["footer"].update(footer_panel(state, countdown))


async def _scan_cycle(state: DashState, settings: Settings, repo: Repo | None) -> None:
    state.scanning = True
    state.last_error = None
    try:
        markets = await scan_weather_markets(settings)
        markets = filter_tradeable_soon(
            markets, max_hours_to_resolution=settings.max_hours_to_resolution,
        )
        markets.sort(key=lambda m: (m.volume_24hr or 0.0), reverse=True)
        markets = markets[:ANALYZE_CAP]
        state.scan_total = len(markets)
        state.scan_done = 0
        state.analyses = []

        def _progress(done: int, total: int, a: MarketAnalysis) -> None:
            state.scan_done = done
            state.scan_total = total
            state.analyses.append(a)

        await analyze_many(
            markets, settings=settings, repo=repo, concurrency=6, on_progress=_progress,
        )
        state.last_scan_at = datetime.now()
    except Exception as e:
        state.last_error = str(e)
    finally:
        state.scanning = False


async def _wallet_cycle(state: DashState, settings: Settings) -> None:
    try:
        state.wallet = await asyncio.to_thread(fetch_wallet_snapshot, settings)
    except Exception as e:
        state.wallet = WalletSnapshot(connected=False, last_error=str(e))


async def run_tui(settings: Settings, repo: Repo | None = None) -> None:
    console = Console()
    layout = _build_layout()
    state = DashState(bankroll_usd=settings.bankroll_usd)

    # Kick off an initial wallet fetch in parallel with the first scan.
    wallet_task: asyncio.Task | None = asyncio.create_task(_wallet_cycle(state, settings))
    wallet_last_at = datetime.now()

    with Live(layout, console=console, screen=True, refresh_per_second=4,
              transient=False) as live:
        while True:
            scan_task = asyncio.create_task(_scan_cycle(state, settings, repo))

            while not scan_task.done():
                _render(layout, state, countdown=None, bankroll_cfg=settings.bankroll_usd)
                live.refresh()
                await asyncio.sleep(0.4)

            await scan_task

            for sec in range(REFRESH_SECONDS, 0, -1):
                if sec % DETAIL_CYCLE_SECONDS == 0:
                    state.detail_idx += 1

                # Refresh wallet in the background every minute.
                if (
                    (wallet_task is None or wallet_task.done())
                    and (datetime.now() - wallet_last_at).total_seconds() >= WALLET_REFRESH_SECONDS
                ):
                    wallet_task = asyncio.create_task(_wallet_cycle(state, settings))
                    wallet_last_at = datetime.now()

                _render(layout, state, countdown=sec, bankroll_cfg=settings.bankroll_usd)
                live.refresh()
                await asyncio.sleep(1)
