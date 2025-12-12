"""
Arbitrage Detector Module

Detects arbitrage opportunities between matched Kalshi and Polymarket markets.
Calculates potential profits considering fees, slippage, and execution risks.
"""

import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .market_matcher import MatchedMarket

logger = logging.getLogger(__name__)


class ArbitrageType(Enum):
    """Types of arbitrage opportunities"""
    CROSS_PLATFORM_YES = "cross_platform_yes"  # Buy YES on one, sell on other
    CROSS_PLATFORM_NO = "cross_platform_no"    # Buy NO on one, sell on other
    GUARANTEED_PROFIT = "guaranteed_profit"     # Risk-free profit opportunity


@dataclass
class ArbitrageOpportunity:
    """Represents an arbitrage opportunity between platforms"""
    id: str
    matched_market: MatchedMarket
    arb_type: ArbitrageType

    # Buy side
    buy_platform: str  # 'kalshi' or 'polymarket'
    buy_side: str      # 'yes' or 'no'
    buy_price: float   # Price to buy at

    # Sell side
    sell_platform: str
    sell_side: str
    sell_price: float

    # Profit calculations
    spread: float           # Raw price difference
    profit_pct: float       # Profit percentage before fees
    net_profit_pct: float   # Profit percentage after estimated fees
    estimated_fees: float   # Estimated total fees

    # Risk metrics
    confidence: float       # Match confidence (from matcher)
    liquidity_score: float  # Combined liquidity metric
    risk_score: float       # Overall risk score (0-100, lower is better)

    # Timestamps
    detected_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity is profitable after fees"""
        return self.net_profit_pct > 0

    @property
    def summary(self) -> str:
        """Human-readable summary"""
        return (
            f"{self.arb_type.value}: Buy {self.buy_side.upper()} on {self.buy_platform} "
            f"@ {self.buy_price:.2f}, Sell {self.sell_side.upper()} on {self.sell_platform} "
            f"@ {self.sell_price:.2f} = {self.net_profit_pct:.2f}% profit"
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "id": self.id,
            "kalshi_ticker": self.matched_market.kalshi_market.ticker,
            "kalshi_title": self.matched_market.kalshi_market.title,
            "polymarket_question": self.matched_market.polymarket_market.question,
            "arb_type": self.arb_type.value,
            "buy_platform": self.buy_platform,
            "buy_side": self.buy_side,
            "buy_price": self.buy_price,
            "sell_platform": self.sell_platform,
            "sell_side": self.sell_side,
            "sell_price": self.sell_price,
            "spread": self.spread,
            "profit_pct": self.profit_pct,
            "net_profit_pct": self.net_profit_pct,
            "estimated_fees": self.estimated_fees,
            "confidence": self.confidence,
            "liquidity_score": self.liquidity_score,
            "risk_score": self.risk_score,
            "detected_at": self.detected_at.isoformat(),
            "is_profitable": self.is_profitable,
        }


class ArbitrageDetector:
    """
    Detects and analyzes arbitrage opportunities

    Strategies:
    1. Direct arbitrage: Buy YES/NO cheaper on one platform, sell on another
    2. Inverse arbitrage: Exploit YES/NO price inversions
    3. Guaranteed profit: When sum of best prices < 1 or > 1
    """

    # Fee structures (approximate)
    KALSHI_MAKER_FEE = 0.00  # 0% maker fee
    KALSHI_TAKER_FEE = 0.01  # 1% taker fee
    POLYMARKET_FEE = 0.02    # ~2% effective fee
    SLIPPAGE_ESTIMATE = 0.01 # 1% estimated slippage

    def __init__(
        self,
        min_profit_pct: float = 1.0,
        include_fees: bool = True,
        slippage_factor: float = 0.01,
        min_liquidity: float = 1000
    ):
        """
        Initialize arbitrage detector

        Args:
            min_profit_pct: Minimum profit percentage to report
            include_fees: Account for fees in calculations
            slippage_factor: Estimated slippage (default 1%)
            min_liquidity: Minimum liquidity threshold
        """
        self.min_profit_pct = min_profit_pct
        self.include_fees = include_fees
        self.slippage_factor = slippage_factor
        self.min_liquidity = min_liquidity
        self._opportunities: Dict[str, ArbitrageOpportunity] = {}
        self._opp_counter = 0

    def _generate_id(self) -> str:
        """Generate unique opportunity ID"""
        self._opp_counter += 1
        return f"ARB-{self._opp_counter:06d}"

    def _estimate_fees(self, platform: str, is_taker: bool = True) -> float:
        """Estimate fees for a trade"""
        if platform == "kalshi":
            return self.KALSHI_TAKER_FEE if is_taker else self.KALSHI_MAKER_FEE
        else:
            return self.POLYMARKET_FEE

    def _calculate_liquidity_score(self, matched: MatchedMarket) -> float:
        """
        Calculate combined liquidity score

        Higher is better (more liquid = easier to execute)
        """
        kalshi_vol = matched.kalshi_market.volume
        poly_vol = matched.polymarket_market.volume

        # Use geometric mean normalized to 0-100
        if kalshi_vol > 0 and poly_vol > 0:
            combined = (kalshi_vol * poly_vol) ** 0.5
            # Normalize: $10k combined volume = 50 score
            score = min(100, (combined / 10000) * 50)
            return score
        return 0

    def _calculate_risk_score(
        self,
        matched: MatchedMarket,
        profit_pct: float,
        liquidity_score: float
    ) -> float:
        """
        Calculate overall risk score (0-100, lower is better)

        Factors:
        - Match confidence (lower similarity = higher risk)
        - Liquidity (lower liquidity = higher risk)
        - Profit margin (higher margin may indicate data issues)
        """
        # Base risk from match confidence
        confidence_risk = 100 - matched.similarity_score

        # Liquidity risk
        liquidity_risk = 100 - liquidity_score

        # Anomaly risk (suspiciously high profits may indicate stale data)
        anomaly_risk = 0
        if profit_pct > 10:
            anomaly_risk = min(50, (profit_pct - 10) * 5)

        # Weighted combination
        risk = (
            confidence_risk * 0.3 +
            liquidity_risk * 0.4 +
            anomaly_risk * 0.3
        )

        return min(100, max(0, risk))

    def detect_cross_platform_arb(
        self,
        matched: MatchedMarket
    ) -> List[ArbitrageOpportunity]:
        """
        Detect cross-platform arbitrage opportunities

        Look for price differences where:
        - Kalshi YES ask < Polymarket YES price (buy Kalshi, sell Polymarket)
        - Polymarket YES price < Kalshi YES bid (buy Polymarket, sell Kalshi)
        - Same analysis for NO side
        """
        opportunities = []
        kalshi = matched.kalshi_market
        poly = matched.polymarket_market

        liquidity_score = self._calculate_liquidity_score(matched)

        # Check minimum liquidity
        if liquidity_score < (self.min_liquidity / 1000):
            return opportunities

        # ===== YES Side Arbitrage =====

        # Buy YES on Kalshi (at ask), "sell" on Polymarket
        # On Polymarket, selling YES is equivalent to buying NO at (1 - yes_price)
        kalshi_yes_ask = kalshi.yes_ask
        poly_yes_price = poly.yes_price

        if kalshi_yes_ask > 0 and poly_yes_price > 0:
            # Strategy 1: Buy Kalshi YES cheap, sell Polymarket YES expensive
            if poly_yes_price > kalshi_yes_ask:
                spread = poly_yes_price - kalshi_yes_ask
                profit_pct = (spread / kalshi_yes_ask) * 100

                fees = self._estimate_fees("kalshi") + self._estimate_fees("polymarket")
                net_profit = profit_pct - (fees * 100) - (self.slippage_factor * 100)

                if net_profit >= self.min_profit_pct:
                    risk = self._calculate_risk_score(matched, net_profit, liquidity_score)

                    opp = ArbitrageOpportunity(
                        id=self._generate_id(),
                        matched_market=matched,
                        arb_type=ArbitrageType.CROSS_PLATFORM_YES,
                        buy_platform="kalshi",
                        buy_side="yes",
                        buy_price=kalshi_yes_ask,
                        sell_platform="polymarket",
                        sell_side="yes",
                        sell_price=poly_yes_price,
                        spread=spread,
                        profit_pct=profit_pct,
                        net_profit_pct=net_profit,
                        estimated_fees=fees,
                        confidence=matched.similarity_score,
                        liquidity_score=liquidity_score,
                        risk_score=risk
                    )
                    opportunities.append(opp)

        # Strategy 2: Buy Polymarket YES cheap, sell Kalshi YES expensive
        kalshi_yes_bid = kalshi.yes_bid
        if kalshi_yes_bid > 0 and poly_yes_price > 0:
            if kalshi_yes_bid > poly_yes_price:
                spread = kalshi_yes_bid - poly_yes_price
                profit_pct = (spread / poly_yes_price) * 100

                fees = self._estimate_fees("kalshi") + self._estimate_fees("polymarket")
                net_profit = profit_pct - (fees * 100) - (self.slippage_factor * 100)

                if net_profit >= self.min_profit_pct:
                    risk = self._calculate_risk_score(matched, net_profit, liquidity_score)

                    opp = ArbitrageOpportunity(
                        id=self._generate_id(),
                        matched_market=matched,
                        arb_type=ArbitrageType.CROSS_PLATFORM_YES,
                        buy_platform="polymarket",
                        buy_side="yes",
                        buy_price=poly_yes_price,
                        sell_platform="kalshi",
                        sell_side="yes",
                        sell_price=kalshi_yes_bid,
                        spread=spread,
                        profit_pct=profit_pct,
                        net_profit_pct=net_profit,
                        estimated_fees=fees,
                        confidence=matched.similarity_score,
                        liquidity_score=liquidity_score,
                        risk_score=risk
                    )
                    opportunities.append(opp)

        # ===== NO Side Arbitrage =====

        kalshi_no_ask = kalshi.no_ask
        kalshi_no_bid = kalshi.no_bid
        poly_no_price = poly.no_price

        # Strategy 3: Buy Kalshi NO cheap, sell Polymarket NO expensive
        if kalshi_no_ask > 0 and poly_no_price > 0:
            if poly_no_price > kalshi_no_ask:
                spread = poly_no_price - kalshi_no_ask
                profit_pct = (spread / kalshi_no_ask) * 100

                fees = self._estimate_fees("kalshi") + self._estimate_fees("polymarket")
                net_profit = profit_pct - (fees * 100) - (self.slippage_factor * 100)

                if net_profit >= self.min_profit_pct:
                    risk = self._calculate_risk_score(matched, net_profit, liquidity_score)

                    opp = ArbitrageOpportunity(
                        id=self._generate_id(),
                        matched_market=matched,
                        arb_type=ArbitrageType.CROSS_PLATFORM_NO,
                        buy_platform="kalshi",
                        buy_side="no",
                        buy_price=kalshi_no_ask,
                        sell_platform="polymarket",
                        sell_side="no",
                        sell_price=poly_no_price,
                        spread=spread,
                        profit_pct=profit_pct,
                        net_profit_pct=net_profit,
                        estimated_fees=fees,
                        confidence=matched.similarity_score,
                        liquidity_score=liquidity_score,
                        risk_score=risk
                    )
                    opportunities.append(opp)

        # Strategy 4: Buy Polymarket NO cheap, sell Kalshi NO expensive
        if kalshi_no_bid > 0 and poly_no_price > 0:
            if kalshi_no_bid > poly_no_price:
                spread = kalshi_no_bid - poly_no_price
                profit_pct = (spread / poly_no_price) * 100

                fees = self._estimate_fees("kalshi") + self._estimate_fees("polymarket")
                net_profit = profit_pct - (fees * 100) - (self.slippage_factor * 100)

                if net_profit >= self.min_profit_pct:
                    risk = self._calculate_risk_score(matched, net_profit, liquidity_score)

                    opp = ArbitrageOpportunity(
                        id=self._generate_id(),
                        matched_market=matched,
                        arb_type=ArbitrageType.CROSS_PLATFORM_NO,
                        buy_platform="polymarket",
                        buy_side="no",
                        buy_price=poly_no_price,
                        sell_platform="kalshi",
                        sell_side="no",
                        sell_price=kalshi_no_bid,
                        spread=spread,
                        profit_pct=profit_pct,
                        net_profit_pct=net_profit,
                        estimated_fees=fees,
                        confidence=matched.similarity_score,
                        liquidity_score=liquidity_score,
                        risk_score=risk
                    )
                    opportunities.append(opp)

        return opportunities

    def detect_guaranteed_profit(
        self,
        matched: MatchedMarket
    ) -> Optional[ArbitrageOpportunity]:
        """
        Detect guaranteed profit opportunities

        This occurs when:
        - Best YES price + Best NO price < 1 (buy both, guaranteed profit)
        - Happens due to market inefficiencies or stale quotes
        """
        kalshi = matched.kalshi_market
        poly = matched.polymarket_market

        # Get best prices across both platforms
        best_yes = min(
            kalshi.yes_ask if kalshi.yes_ask > 0 else 1,
            poly.yes_price if poly.yes_price > 0 else 1
        )
        best_no = min(
            kalshi.no_ask if kalshi.no_ask > 0 else 1,
            poly.no_price if poly.no_price > 0 else 1
        )

        total_cost = best_yes + best_no

        # If total < 1, there's guaranteed profit
        if total_cost < 1.0:
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100

            # Account for fees
            total_fees = 2 * (self._estimate_fees("kalshi") + self._estimate_fees("polymarket"))
            net_profit_pct = profit_pct - (total_fees * 100)

            if net_profit_pct >= self.min_profit_pct:
                liquidity = self._calculate_liquidity_score(matched)
                risk = self._calculate_risk_score(matched, net_profit_pct, liquidity)

                # Determine which platform has better YES/NO prices
                yes_platform = "kalshi" if kalshi.yes_ask <= poly.yes_price else "polymarket"
                no_platform = "kalshi" if kalshi.no_ask <= poly.no_price else "polymarket"

                return ArbitrageOpportunity(
                    id=self._generate_id(),
                    matched_market=matched,
                    arb_type=ArbitrageType.GUARANTEED_PROFIT,
                    buy_platform=yes_platform,
                    buy_side="yes",
                    buy_price=best_yes,
                    sell_platform=no_platform,
                    sell_side="no",
                    sell_price=best_no,
                    spread=profit,
                    profit_pct=profit_pct,
                    net_profit_pct=net_profit_pct,
                    estimated_fees=total_fees,
                    confidence=matched.similarity_score,
                    liquidity_score=liquidity,
                    risk_score=risk
                )

        return None

    def scan_all(
        self,
        matched_markets: List[MatchedMarket]
    ) -> List[ArbitrageOpportunity]:
        """
        Scan all matched markets for arbitrage opportunities

        Args:
            matched_markets: List of matched market pairs

        Returns:
            List of detected arbitrage opportunities, sorted by profit
        """
        all_opportunities = []

        for matched in matched_markets:
            # Cross-platform arbitrage
            cross_opps = self.detect_cross_platform_arb(matched)
            all_opportunities.extend(cross_opps)

            # Guaranteed profit
            guaranteed = self.detect_guaranteed_profit(matched)
            if guaranteed:
                all_opportunities.append(guaranteed)

        # Store opportunities
        for opp in all_opportunities:
            self._opportunities[opp.id] = opp

        # Sort by net profit descending
        all_opportunities.sort(key=lambda x: x.net_profit_pct, reverse=True)

        logger.info(f"Found {len(all_opportunities)} arbitrage opportunities")

        return all_opportunities

    def get_opportunities(
        self,
        min_profit: Optional[float] = None,
        max_risk: Optional[float] = None,
        arb_type: Optional[ArbitrageType] = None
    ) -> List[ArbitrageOpportunity]:
        """
        Get filtered list of opportunities

        Args:
            min_profit: Minimum net profit percentage
            max_risk: Maximum risk score
            arb_type: Filter by arbitrage type

        Returns:
            Filtered list of opportunities
        """
        opps = list(self._opportunities.values())

        if min_profit is not None:
            opps = [o for o in opps if o.net_profit_pct >= min_profit]

        if max_risk is not None:
            opps = [o for o in opps if o.risk_score <= max_risk]

        if arb_type is not None:
            opps = [o for o in opps if o.arb_type == arb_type]

        return sorted(opps, key=lambda x: x.net_profit_pct, reverse=True)

    def get_opportunity(self, opp_id: str) -> Optional[ArbitrageOpportunity]:
        """Get specific opportunity by ID"""
        return self._opportunities.get(opp_id)

    def clear_opportunities(self):
        """Clear all stored opportunities"""
        self._opportunities.clear()

    def get_stats(self) -> Dict:
        """Get statistics about detected opportunities"""
        opps = list(self._opportunities.values())

        if not opps:
            return {
                "total": 0,
                "profitable": 0,
                "avg_profit": 0,
                "max_profit": 0,
                "by_type": {}
            }

        profitable = [o for o in opps if o.is_profitable]

        return {
            "total": len(opps),
            "profitable": len(profitable),
            "avg_profit": sum(o.net_profit_pct for o in profitable) / len(profitable) if profitable else 0,
            "max_profit": max(o.net_profit_pct for o in opps),
            "avg_risk": sum(o.risk_score for o in opps) / len(opps),
            "by_type": {
                t.value: len([o for o in opps if o.arb_type == t])
                for t in ArbitrageType
            }
        }
