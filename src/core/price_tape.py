"""
Price Tape Buffer.

Records YES‑price history per matched market across successive scans
so the 3D arbitrage surface can be drawn from real Kalshi / Polymarket
data rather than a synthetic placeholder. Each call to ``record``
appends the latest snapshot to the buffer; ``get_tapes`` returns a
fixed‑shape ``(N, T)`` price tape for both venues, left‑padded with
the first observed value so the visualisation is usable from the
very first scan.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Iterable, List, Optional, Tuple

import numpy as np

from .market_matcher import MatchedMarket

logger = logging.getLogger(__name__)


class PriceTapeBuffer:
    """Per‑market ring buffer of (Kalshi YES, Polymarket YES) prices.

    The first time we see a given matched market we allocate a deque
    of length ``max_length`` for it. Each subsequent ``record`` call
    appends the latest snapshot. Markets that disappear from the
    matched set retain their last observed history until evicted by
    the LRU cap (``max_markets``). Designed to be safe to call from
    the SocketIO background task — ``record`` is O(N) and allocates
    nothing in the steady state.
    """

    def __init__(self, max_markets: int = 14, max_length: int = 60):
        self.max_markets = max_markets
        self.max_length = max_length
        # Insertion‑ordered dict so the "top N" stays stable across scans.
        self._markets: dict = {}

    # --- write path -----------------------------------------------------------

    def record(self, matched_markets: Iterable[MatchedMarket]) -> int:
        """Append the latest YES‑price snapshot for each matched market.

        Returns the number of markets recorded. Markets with missing
        prices on either side are skipped to avoid corrupting the tape
        with zeros.
        """
        recorded = 0
        for matched in matched_markets:
            kalshi_yes = self._best_kalshi_yes(matched)
            poly_yes = float(matched.polymarket_market.yes_price or 0.0)
            if kalshi_yes <= 0.0 or poly_yes <= 0.0:
                continue

            key = matched.kalshi_market.ticker
            entry = self._markets.get(key)
            if entry is None:
                if len(self._markets) >= self.max_markets:
                    # Evict the oldest tracked market to keep the cap.
                    self._markets.pop(next(iter(self._markets)))
                entry = {
                    "kalshi": deque(maxlen=self.max_length),
                    "poly": deque(maxlen=self.max_length),
                    "label": (matched.kalshi_market.title or key)[:30],
                }
                self._markets[key] = entry

            entry["kalshi"].append(float(kalshi_yes))
            entry["poly"].append(float(poly_yes))
            recorded += 1

        if recorded:
            logger.debug(
                "PriceTapeBuffer recorded %d markets (tracking %d total)",
                recorded,
                len(self._markets),
            )
        return recorded

    @staticmethod
    def _best_kalshi_yes(matched: MatchedMarket) -> float:
        """Pick a representative Kalshi YES price.

        Prefer the ask (what an arbitrageur would pay to buy YES),
        then fall back to bid, then to the mid. Returning 0.0 means
        "no usable price for this scan".
        """
        market = matched.kalshi_market
        for candidate in (market.yes_ask, market.yes_bid):
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0.0:
                return value
        return 0.0

    # --- read path ------------------------------------------------------------

    @property
    def tracked_markets(self) -> int:
        return len(self._markets)

    def has_data(self) -> bool:
        return any(entry["kalshi"] for entry in self._markets.values())

    def get_tapes(
        self,
        n_markets: int,
        length: int,
    ) -> Optional[Tuple[np.ndarray, np.ndarray, List[str]]]:
        """Return the latest ``length`` samples for up to ``n_markets``.

        Markets with shorter history than ``length`` are left‑padded
        with their first observed value so the surface is renderable
        from the first scan onward. Returns ``None`` if no market has
        any recorded history yet.
        """
        items = [
            (key, entry)
            for key, entry in self._markets.items()
            if entry["kalshi"]
        ]
        if not items:
            return None

        items = items[: max(0, int(n_markets))]
        N = len(items)
        kalshi = np.zeros((N, length), dtype=float)
        poly = np.zeros((N, length), dtype=float)
        labels: List[str] = []

        for i, (_, entry) in enumerate(items):
            k_hist = list(entry["kalshi"])
            p_hist = list(entry["poly"])

            # Left‑pad with the first observed value if we don't have
            # enough history yet, so the surface is flat on the left
            # until real data fills in.
            if len(k_hist) < length:
                pad = length - len(k_hist)
                k_hist = [k_hist[0]] * pad + k_hist
                p_hist = [p_hist[0]] * pad + p_hist
            else:
                k_hist = k_hist[-length:]
                p_hist = p_hist[-length:]

            kalshi[i] = k_hist
            poly[i] = p_hist
            labels.append(entry["label"])

        return kalshi, poly, labels

    def clear(self) -> None:
        """Drop all recorded history."""
        self._markets.clear()
