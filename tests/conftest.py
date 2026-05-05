"""Shared pytest fixtures."""

from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import pytest

from src.api.kalshi_client import KalshiMarket
from src.api.polymarket_client import PolymarketMarket
from src.core.market_matcher import MatchedMarket


def make_kalshi_market(
    ticker: str = "K-DEMO",
    title: str = "Will event X happen?",
    yes_bid: float = 0.42,
    yes_ask: float = 0.45,
    no_bid: float = 0.55,
    no_ask: float = 0.58,
    volume: int = 5000,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        title=title,
        subtitle="",
        category="politics",
        status="open",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        volume=volume,
        open_interest=volume,
        expiration_time=None,
        result=None,
    )


def make_poly_market(
    condition_id: str = "0xabc",
    question: str = "Will event X happen?",
    yes_price: float = 0.50,
    no_price: float = 0.50,
    yes_token: str = "12345",
    no_token: str = "67890",
    volume: float = 5000.0,
) -> PolymarketMarket:
    return PolymarketMarket(
        condition_id=condition_id,
        question_id=condition_id,
        question=question,
        description="",
        outcome_yes_token=yes_token,
        outcome_no_token=no_token,
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        liquidity=volume,
        end_date=None,
        category="politics",
        active=True,
        closed=False,
    )


def make_matched_market(
    kalshi: Optional[KalshiMarket] = None,
    poly: Optional[PolymarketMarket] = None,
    similarity: float = 92.0,
) -> MatchedMarket:
    return MatchedMarket(
        kalshi_market=kalshi or make_kalshi_market(),
        polymarket_market=poly or make_poly_market(),
        similarity_score=similarity,
        match_type="fuzzy",
        matched_at=datetime.utcnow(),
    )


@pytest.fixture
def matched_market():
    return make_matched_market()


@pytest.fixture
def matched_market_factory():
    return make_matched_market
