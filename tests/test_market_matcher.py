"""Tests for the fuzzy market matcher."""

from src.core.market_matcher import MarketMatcher
from tests.conftest import make_kalshi_market, make_poly_market


def test_exact_title_match_pairs_markets():
    matcher = MarketMatcher(fuzzy_threshold=60)
    kalshi = [make_kalshi_market(ticker="K-1", title="Will candidate X win the election?")]
    poly = [make_poly_market(condition_id="P-1", question="Will candidate X win the election?")]
    matches = matcher.match_markets(kalshi, poly)
    assert len(matches) == 1
    m = matches[0]
    assert m.kalshi_market.ticker == "K-1"
    assert m.polymarket_market.condition_id == "P-1"
    assert m.similarity_score >= 90.0


def test_no_match_when_titles_unrelated():
    matcher = MarketMatcher(fuzzy_threshold=70)
    kalshi = [make_kalshi_market(ticker="K-1", title="Will the Fed cut rates in December?")]
    poly = [make_poly_market(condition_id="P-1", question="Will it rain in Berlin tomorrow?")]
    matches = matcher.match_markets(kalshi, poly)
    assert matches == []


def test_one_to_one_assignment():
    """A Polymarket market should not be matched to two Kalshi markets."""
    matcher = MarketMatcher(fuzzy_threshold=60)
    kalshi = [
        make_kalshi_market(ticker="K-1", title="Will Bitcoin reach $100k in 2025?"),
        make_kalshi_market(ticker="K-2", title="Will Bitcoin reach $100k in 2025?"),
    ]
    poly = [make_poly_market(condition_id="P-1", question="Will Bitcoin reach $100k in 2025?")]
    matches = matcher.match_markets(kalshi, poly)
    assert len(matches) <= 1
