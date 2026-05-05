"""Tests for TradeExecutor identifier resolution and reconciliation."""

import pytest

from src.core.arbitrage_detector import ArbitrageDetector
from src.core.trade_executor import OrderSide, TradeExecutor, TradeStatus
from tests.conftest import make_kalshi_market, make_matched_market, make_poly_market


@pytest.fixture
def executor():
    return TradeExecutor(max_trade_size=100.0, dry_run=True)


def _make_opportunity():
    detector = ArbitrageDetector(min_profit_pct=0.5, slippage_factor=0.01)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.40, yes_bid=0.38),
        poly=make_poly_market(yes_price=0.55, yes_token="TOKEN-YES", no_token="TOKEN-NO"),
    )
    return detector.detect_cross_platform_arb(matched)[0]


def test_resolve_identifiers_kalshi_leg(executor):
    opp = _make_opportunity()
    ticker, token_id = executor._resolve_identifiers(opp, "kalshi", "yes")
    assert ticker == opp.matched_market.kalshi_market.ticker
    assert token_id is None


def test_resolve_identifiers_polymarket_yes(executor):
    opp = _make_opportunity()
    ticker, token_id = executor._resolve_identifiers(opp, "polymarket", "yes")
    assert ticker is None
    assert token_id == "TOKEN-YES"


def test_resolve_identifiers_polymarket_no(executor):
    opp = _make_opportunity()
    ticker, token_id = executor._resolve_identifiers(opp, "polymarket", "no")
    assert token_id == "TOKEN-NO"


@pytest.mark.asyncio
async def test_dry_run_execute_uses_correct_token(executor):
    opp = _make_opportunity()
    trade = await executor.execute_opportunity(opp, size_usd=50.0)
    # Buy is on Kalshi (cheaper), sell on Polymarket (richer).
    assert trade.buy_order.platform == "kalshi"
    assert trade.buy_order.ticker is not None
    assert trade.buy_order.token_id is None

    assert trade.sell_order.platform == "polymarket"
    assert trade.sell_order.ticker is None
    assert trade.sell_order.token_id == "TOKEN-YES"

    assert trade.status == TradeStatus.COMPLETED
    assert trade.realized_profit > 0


@pytest.mark.asyncio
async def test_polymarket_order_without_token_id_fails_clearly():
    """Sanity check the regression we just fixed.

    In *live* mode (dry_run=False), Polymarket orders without a
    token_id must short-circuit with a clear error rather than
    falling through to the SDK and 500ing.
    """
    from src.core.trade_executor import TradeOrder

    live_executor = TradeExecutor(max_trade_size=100.0, dry_run=False)
    live_executor.polymarket = object()  # avoid "not configured" branch

    bad_order = TradeOrder(
        id="ORD-X",
        platform="polymarket",
        ticker=None,
        token_id=None,
        side=OrderSide.BUY,
        outcome="yes",
        price=0.5,
        quantity=10,
    )
    success, _, error = await live_executor._execute_polymarket_order(bad_order)
    assert success is False
    assert "token_id" in error
