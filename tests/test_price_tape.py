"""Tests for the rolling price-tape buffer."""

import numpy as np

from src.core.price_tape import PriceTapeBuffer
from tests.conftest import make_kalshi_market, make_matched_market, make_poly_market


def test_record_skips_when_either_price_is_zero():
    buf = PriceTapeBuffer(max_markets=4, max_length=5)
    matched = make_matched_market(
        kalshi=make_kalshi_market(yes_ask=0.0, yes_bid=0.0),
        poly=make_poly_market(yes_price=0.50),
    )
    assert buf.record([matched]) == 0
    assert buf.tracked_markets == 0


def test_record_uses_yes_ask_then_bid_then_skips():
    buf = PriceTapeBuffer(max_markets=4, max_length=5)
    matched_ask = make_matched_market(
        kalshi=make_kalshi_market(ticker="K-A", yes_ask=0.40, yes_bid=0.0),
        poly=make_poly_market(yes_price=0.50),
    )
    matched_bid = make_matched_market(
        kalshi=make_kalshi_market(ticker="K-B", yes_ask=0.0, yes_bid=0.30),
        poly=make_poly_market(yes_price=0.40),
    )
    assert buf.record([matched_ask, matched_bid]) == 2
    tapes = buf.get_tapes(n_markets=4, length=5)
    assert tapes is not None
    kalshi, poly, labels = tapes
    assert kalshi.shape == (2, 5)
    # Most recent (right-most) sample should be the prices we recorded.
    assert kalshi[0, -1] == 0.40
    assert kalshi[1, -1] == 0.30


def test_get_tapes_left_pads_short_history():
    buf = PriceTapeBuffer(max_markets=2, max_length=10)
    for i in range(3):
        matched = make_matched_market(
            kalshi=make_kalshi_market(ticker="K-X", yes_ask=0.4 + 0.01 * i),
            poly=make_poly_market(yes_price=0.5 + 0.01 * i),
        )
        buf.record([matched])

    tapes = buf.get_tapes(n_markets=2, length=10)
    assert tapes is not None
    kalshi, poly, _ = tapes
    assert kalshi.shape == (1, 10)
    # The first observed value (0.40) should fill the padding.
    assert np.allclose(kalshi[0, :8], 0.40)
    # The newest values are at the right edge.
    assert np.isclose(kalshi[0, -1], 0.42)


def test_lru_eviction_when_max_markets_exceeded():
    buf = PriceTapeBuffer(max_markets=2, max_length=5)
    for i in range(4):
        matched = make_matched_market(
            kalshi=make_kalshi_market(ticker=f"K-{i}", yes_ask=0.5),
            poly=make_poly_market(yes_price=0.5),
        )
        buf.record([matched])
    assert buf.tracked_markets == 2


def test_clear_resets_state():
    buf = PriceTapeBuffer()
    buf.record([make_matched_market()])
    assert buf.has_data()
    buf.clear()
    assert not buf.has_data()
    assert buf.get_tapes(n_markets=4, length=5) is None
