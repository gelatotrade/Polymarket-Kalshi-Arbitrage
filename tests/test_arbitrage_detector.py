"""Tests for the arbitrage profit calculation."""

from src.core.arbitrage_detector import ArbitrageDetector, ArbitrageType
from tests.conftest import make_kalshi_market, make_matched_market, make_poly_market


def test_no_opportunity_when_prices_match():
    detector = ArbitrageDetector(min_profit_pct=0.5, slippage_factor=0.01)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.50, yes_bid=0.49, no_ask=0.51, no_bid=0.50),
        poly=make_poly_market(yes_price=0.50, no_price=0.50),
    )
    assert detector.detect_cross_platform_arb(matched) == []


def test_yes_side_cross_platform_arb_detected():
    """Buy YES on Kalshi at 0.40, sell on Polymarket at 0.55 -> ~30% gross."""
    detector = ArbitrageDetector(min_profit_pct=0.5, slippage_factor=0.01)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.40, yes_bid=0.38),
        poly=make_poly_market(yes_price=0.55),
    )
    opps = detector.detect_cross_platform_arb(matched)
    assert opps, "expected an arbitrage opportunity"
    opp = opps[0]
    assert opp.buy_platform == "kalshi"
    assert opp.sell_platform == "polymarket"
    assert opp.buy_side == "yes"
    assert opp.buy_price == 0.40
    assert opp.sell_price == 0.55
    # Gross profit pct ~= (0.55 - 0.40) / 0.40 * 100 = 37.5
    assert 30.0 < opp.profit_pct < 40.0
    # Net should be lower (fees + slippage)
    assert opp.net_profit_pct < opp.profit_pct
    assert opp.net_profit_pct > 0


def test_low_liquidity_filter_blocks_arbs():
    detector = ArbitrageDetector(min_profit_pct=0.5, min_liquidity=1_000_000)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.40, yes_bid=0.38, volume=10),
        poly=make_poly_market(yes_price=0.55, volume=10),
    )
    assert detector.detect_cross_platform_arb(matched) == []


def test_guaranteed_profit_when_yes_plus_no_below_one():
    detector = ArbitrageDetector(min_profit_pct=0.5)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.30, no_ask=0.30),
        poly=make_poly_market(yes_price=0.55, no_price=0.55),
    )
    opp = detector.detect_guaranteed_profit(matched)
    # Total cost = 0.60, payout = 1.00, gross profit = 40 / 60 = 66%
    assert opp is not None
    assert opp.arb_type == ArbitrageType.GUARANTEED_PROFIT
    assert opp.spread > 0
