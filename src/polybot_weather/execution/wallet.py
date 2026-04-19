"""Read-only wallet snapshot for the TUI — USDC balance, open orders, trades.

All calls here are GETs against Polymarket's CLOB. Nothing posts orders or
moves funds. Requires the same env as `trader.py` (POLYBOT_PRIVATE_KEY and
POLYBOT_FUNDER_ADDRESS), but does NOT require `POLYBOT_EXECUTION_ENABLED` —
reading your own balance is safe, executing is not.

Fetches run synchronously inside `py_clob_client`; wrap with
`asyncio.to_thread` from the TUI to avoid blocking the render loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings


@dataclass
class Trade:
    market_slug: str | None
    outcome: str | None
    side: str
    price: float
    size: float
    ts: datetime | None


@dataclass
class WalletSnapshot:
    connected: bool
    funder_address: str | None = None
    usdc_available: float = 0.0          # free collateral on the CLOB
    usdc_in_orders: float = 0.0          # locked in open orders
    open_orders_count: int = 0
    open_orders_volume_usd: float = 0.0  # sum of (price * remaining_size)
    recent_trades: list[Trade] = field(default_factory=list)
    last_error: str | None = None
    fetched_at: datetime = field(default_factory=datetime.now)


def _build_client(settings: Settings):
    """Return a configured py_clob_client instance, or None if misconfigured."""
    if not settings.private_key or not settings.funder_address:
        return None
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=settings.private_key,
        chain_id=POLYGON,
        funder=settings.funder_address,
        signature_type=2,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def fetch_wallet_snapshot(settings: Settings, *, trade_limit: int = 10) -> WalletSnapshot:
    if not settings.private_key or not settings.funder_address:
        return WalletSnapshot(connected=False, last_error="POLYBOT_PRIVATE_KEY/FUNDER_ADDRESS not set")

    snap = WalletSnapshot(connected=True, funder_address=settings.funder_address)
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError as e:
        snap.connected = False
        snap.last_error = f"py_clob_client unavailable: {e}"
        return snap

    try:
        client = _build_client(settings)
    except Exception as e:
        snap.connected = False
        snap.last_error = f"client init failed: {e}"
        return snap

    try:
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # Polymarket returns balance in 6-decimal USDC units as a string.
        raw = bal.get("balance") if isinstance(bal, dict) else None
        if raw is not None:
            try:
                snap.usdc_available = float(raw) / 1_000_000.0
            except (TypeError, ValueError):
                pass
    except Exception as e:
        snap.last_error = f"balance: {e}"

    try:
        orders = client.get_orders()
        if isinstance(orders, list):
            snap.open_orders_count = len(orders)
            vol = 0.0
            for o in orders:
                try:
                    p = float(o.get("price", 0))
                    remaining = float(o.get("size", 0)) - float(o.get("size_matched", 0))
                    vol += p * max(remaining, 0)
                except (TypeError, ValueError):
                    continue
            snap.open_orders_volume_usd = vol
            snap.usdc_in_orders = vol
    except Exception as e:
        snap.last_error = (snap.last_error or "") + f" orders: {e}"

    try:
        trades = client.get_trades()
        if isinstance(trades, list):
            for raw in trades[:trade_limit]:
                ts = None
                if "match_time" in raw:
                    try:
                        ts = datetime.fromtimestamp(int(raw["match_time"]))
                    except (TypeError, ValueError):
                        ts = None
                snap.recent_trades.append(
                    Trade(
                        market_slug=raw.get("market_slug") or raw.get("asset_id"),
                        outcome=raw.get("outcome") or raw.get("side"),
                        side=str(raw.get("side", "")),
                        price=float(raw.get("price", 0) or 0),
                        size=float(raw.get("size", 0) or 0),
                        ts=ts,
                    )
                )
    except Exception as e:
        snap.last_error = (snap.last_error or "") + f" trades: {e}"

    return snap
