"""Trade execution — DISABLED by default.

To enable, set in `.env`:
  POLYBOT_EXECUTION_ENABLED=true
  POLYBOT_PRIVATE_KEY=0x...        (NEVER COMMIT)
  POLYBOT_FUNDER_ADDRESS=0x...

CLI also requires `--confirm` on every individual `polybot trade` call.

This module wraps `py_clob_client` lazily so the dependency is only imported
when execution is actually attempted — keeps the surface area cold for the
analysis-only path.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings


class ExecutionDisabled(RuntimeError):  # noqa: N818  — descriptive name preferred
    pass


class ExecutionMisconfigured(RuntimeError):  # noqa: N818
    pass


@dataclass
class TradeReceipt:
    token_id: str
    side: str
    price: float
    size: float
    order_id: str | None
    raw: dict


def _ensure_enabled(settings: Settings) -> None:
    if not settings.execution_enabled:
        raise ExecutionDisabled(
            "POLYBOT_EXECUTION_ENABLED is false. Set it to true in .env to enable trading."
        )
    if not settings.private_key:
        raise ExecutionMisconfigured("POLYBOT_PRIVATE_KEY is not set.")
    if not settings.funder_address:
        raise ExecutionMisconfigured("POLYBOT_FUNDER_ADDRESS is not set.")


def place_order(
    *,
    settings: Settings,
    token_id: str,
    side: str,
    price: float,
    size_usd: float,
) -> TradeReceipt:
    """Place a limit order on Polymarket. Side ∈ {"BUY","SELL"}.

    `size_usd` is converted to share-count by `size_usd / price`. The CLOB
    requires a minimum order size; the wrapper surfaces any error from the
    upstream client unchanged.
    """
    _ensure_enabled(settings)

    # Lazy import — keeps the trading dependency tree off the analysis path.
    from py_clob_client.client import ClobClient as _Sdk
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.constants import POLYGON

    client = _Sdk(
        host="https://clob.polymarket.com",
        key=settings.private_key,
        chain_id=POLYGON,
        funder=settings.funder_address,
        signature_type=2,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    contracts = round(size_usd / price, 2)
    args = OrderArgs(token_id=token_id, price=price, size=contracts, side=side.upper())
    signed = client.create_order(args)
    resp = client.post_order(signed, OrderType.GTC)

    return TradeReceipt(
        token_id=token_id,
        side=side.upper(),
        price=price,
        size=contracts,
        order_id=resp.get("orderID") if isinstance(resp, dict) else None,
        raw=resp if isinstance(resp, dict) else {},
    )
