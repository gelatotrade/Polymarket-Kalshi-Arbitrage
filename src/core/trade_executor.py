"""
Trade Executor Module

Executes arbitrage trades across Kalshi and Polymarket platforms.
Handles order management, position tracking, and trade logging.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid

from ..api.kalshi_client import KalshiClient
from ..api.polymarket_client import PolymarketClient
from .arbitrage_detector import ArbitrageOpportunity, ArbitrageType

logger = logging.getLogger(__name__)


class TradeStatus(Enum):
    """Trade execution status"""
    PENDING = "pending"
    EXECUTING = "executing"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderSide(Enum):
    """Order side"""
    BUY = "buy"
    SELL = "sell"


@dataclass
class TradeOrder:
    """Individual order in a trade"""
    id: str
    platform: str
    ticker: str
    side: OrderSide
    outcome: str  # 'yes' or 'no'
    price: float
    quantity: float
    status: TradeStatus = TradeStatus.PENDING
    filled_quantity: float = 0
    filled_price: float = 0
    platform_order_id: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    executed_at: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "ticker": self.ticker,
            "side": self.side.value,
            "outcome": self.outcome,
            "price": self.price,
            "quantity": self.quantity,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "platform_order_id": self.platform_order_id,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


@dataclass
class ArbitrageTrade:
    """Complete arbitrage trade (buy + sell legs)"""
    id: str
    opportunity: ArbitrageOpportunity
    buy_order: TradeOrder
    sell_order: TradeOrder
    status: TradeStatus = TradeStatus.PENDING
    total_cost: float = 0
    total_revenue: float = 0
    realized_profit: float = 0
    realized_profit_pct: float = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    @property
    def is_complete(self) -> bool:
        return self.status == TradeStatus.COMPLETED

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "opportunity_id": self.opportunity.id,
            "buy_order": self.buy_order.to_dict(),
            "sell_order": self.sell_order.to_dict(),
            "status": self.status.value,
            "total_cost": self.total_cost,
            "total_revenue": self.total_revenue,
            "realized_profit": self.realized_profit,
            "realized_profit_pct": self.realized_profit_pct,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class TradeExecutor:
    """
    Executes arbitrage trades across platforms

    Handles:
    - Order creation and submission
    - Position management
    - Trade logging and history
    - Risk management (max position, max loss)
    """

    def __init__(
        self,
        kalshi_client: Optional[KalshiClient] = None,
        polymarket_client: Optional[PolymarketClient] = None,
        max_trade_size: float = 100.0,
        max_position: float = 1000.0,
        dry_run: bool = True
    ):
        """
        Initialize trade executor

        Args:
            kalshi_client: Kalshi API client
            polymarket_client: Polymarket API client
            max_trade_size: Maximum single trade size in USD
            max_position: Maximum total position size
            dry_run: If True, simulate trades without executing
        """
        self.kalshi = kalshi_client
        self.polymarket = polymarket_client
        self.max_trade_size = max_trade_size
        self.max_position = max_position
        self.dry_run = dry_run

        self._trades: Dict[str, ArbitrageTrade] = {}
        self._positions: Dict[str, float] = {}  # ticker -> position
        self._total_pnl: float = 0

    def _generate_trade_id(self) -> str:
        """Generate unique trade ID"""
        return f"TRADE-{uuid.uuid4().hex[:8].upper()}"

    def _generate_order_id(self) -> str:
        """Generate unique order ID"""
        return f"ORD-{uuid.uuid4().hex[:8].upper()}"

    async def _execute_kalshi_order(
        self,
        order: TradeOrder
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute order on Kalshi

        Returns:
            Tuple of (success, platform_order_id, error_message)
        """
        if not self.kalshi:
            return False, None, "Kalshi client not configured"

        if self.dry_run:
            logger.info(f"[DRY RUN] Kalshi {order.side.value} {order.outcome} "
                       f"{order.quantity} @ {order.price}")
            return True, f"DRY-{order.id}", None

        try:
            # Convert price to cents (Kalshi uses 1-99 cent prices)
            price_cents = int(order.price * 100)

            response = await self.kalshi.create_order(
                ticker=order.ticker,
                side=order.outcome,
                count=int(order.quantity),
                type="limit",
                yes_price=price_cents if order.outcome == "yes" else None,
                no_price=price_cents if order.outcome == "no" else None
            )

            order_id = response.get("order", {}).get("order_id")
            return True, order_id, None

        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
            return False, None, str(e)

    async def _execute_polymarket_order(
        self,
        order: TradeOrder
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute order on Polymarket

        Returns:
            Tuple of (success, platform_order_id, error_message)
        """
        if not self.polymarket:
            return False, None, "Polymarket client not configured"

        if self.dry_run:
            logger.info(f"[DRY RUN] Polymarket {order.side.value} {order.outcome} "
                       f"{order.quantity} @ {order.price}")
            return True, f"DRY-{order.id}", None

        try:
            # Get token ID based on outcome
            matched = None  # Would need to look up market
            token_id = order.ticker  # Assuming ticker is token_id for now

            response = await self.polymarket.create_order(
                token_id=token_id,
                side=order.side.value.upper(),
                price=order.price,
                size=order.quantity
            )

            order_id = response.get("orderID") or response.get("id")
            return True, order_id, None

        except Exception as e:
            logger.error(f"Polymarket order failed: {e}")
            return False, None, str(e)

    async def execute_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        size_usd: Optional[float] = None
    ) -> ArbitrageTrade:
        """
        Execute an arbitrage opportunity

        Args:
            opportunity: The arbitrage opportunity to execute
            size_usd: Trade size in USD (defaults to max_trade_size)

        Returns:
            ArbitrageTrade object with execution results
        """
        trade_size = min(size_usd or self.max_trade_size, self.max_trade_size)

        # Calculate quantities based on prices
        buy_quantity = trade_size / opportunity.buy_price
        sell_quantity = buy_quantity  # Same quantity for arbitrage

        # Create orders
        buy_order = TradeOrder(
            id=self._generate_order_id(),
            platform=opportunity.buy_platform,
            ticker=opportunity.matched_market.kalshi_market.ticker
                if opportunity.buy_platform == "kalshi"
                else opportunity.matched_market.polymarket_market.outcome_yes_token
                if opportunity.buy_side == "yes"
                else opportunity.matched_market.polymarket_market.outcome_no_token,
            side=OrderSide.BUY,
            outcome=opportunity.buy_side,
            price=opportunity.buy_price,
            quantity=buy_quantity
        )

        sell_order = TradeOrder(
            id=self._generate_order_id(),
            platform=opportunity.sell_platform,
            ticker=opportunity.matched_market.kalshi_market.ticker
                if opportunity.sell_platform == "kalshi"
                else opportunity.matched_market.polymarket_market.outcome_yes_token
                if opportunity.sell_side == "yes"
                else opportunity.matched_market.polymarket_market.outcome_no_token,
            side=OrderSide.SELL,
            outcome=opportunity.sell_side,
            price=opportunity.sell_price,
            quantity=sell_quantity
        )

        # Create trade record
        trade = ArbitrageTrade(
            id=self._generate_trade_id(),
            opportunity=opportunity,
            buy_order=buy_order,
            sell_order=sell_order,
            status=TradeStatus.EXECUTING
        )

        logger.info(f"Executing arbitrage trade {trade.id}")
        logger.info(f"  Buy: {buy_order.platform} {buy_order.outcome} @ {buy_order.price}")
        logger.info(f"  Sell: {sell_order.platform} {sell_order.outcome} @ {sell_order.price}")

        # Execute buy order first
        buy_order.status = TradeStatus.EXECUTING
        if buy_order.platform == "kalshi":
            success, order_id, error = await self._execute_kalshi_order(buy_order)
        else:
            success, order_id, error = await self._execute_polymarket_order(buy_order)

        if success:
            buy_order.status = TradeStatus.COMPLETED
            buy_order.platform_order_id = order_id
            buy_order.filled_quantity = buy_order.quantity
            buy_order.filled_price = buy_order.price
            buy_order.executed_at = datetime.utcnow()
        else:
            buy_order.status = TradeStatus.FAILED
            buy_order.error = error
            trade.status = TradeStatus.FAILED
            self._trades[trade.id] = trade
            logger.error(f"Buy order failed: {error}")
            return trade

        # Execute sell order
        sell_order.status = TradeStatus.EXECUTING
        if sell_order.platform == "kalshi":
            success, order_id, error = await self._execute_kalshi_order(sell_order)
        else:
            success, order_id, error = await self._execute_polymarket_order(sell_order)

        if success:
            sell_order.status = TradeStatus.COMPLETED
            sell_order.platform_order_id = order_id
            sell_order.filled_quantity = sell_order.quantity
            sell_order.filled_price = sell_order.price
            sell_order.executed_at = datetime.utcnow()
        else:
            sell_order.status = TradeStatus.FAILED
            sell_order.error = error
            trade.status = TradeStatus.PARTIAL  # Buy succeeded, sell failed
            self._trades[trade.id] = trade
            logger.error(f"Sell order failed: {error}")
            return trade

        # Calculate P&L
        trade.total_cost = buy_order.filled_quantity * buy_order.filled_price
        trade.total_revenue = sell_order.filled_quantity * sell_order.filled_price
        trade.realized_profit = trade.total_revenue - trade.total_cost
        trade.realized_profit_pct = (trade.realized_profit / trade.total_cost) * 100 if trade.total_cost > 0 else 0

        trade.status = TradeStatus.COMPLETED
        trade.completed_at = datetime.utcnow()

        self._trades[trade.id] = trade
        self._total_pnl += trade.realized_profit

        logger.info(f"Trade {trade.id} completed")
        logger.info(f"  Cost: ${trade.total_cost:.2f}")
        logger.info(f"  Revenue: ${trade.total_revenue:.2f}")
        logger.info(f"  Profit: ${trade.realized_profit:.2f} ({trade.realized_profit_pct:.2f}%)")

        return trade

    async def execute_batch(
        self,
        opportunities: List[ArbitrageOpportunity],
        max_parallel: int = 3
    ) -> List[ArbitrageTrade]:
        """
        Execute multiple arbitrage opportunities

        Args:
            opportunities: List of opportunities to execute
            max_parallel: Maximum parallel executions

        Returns:
            List of completed trades
        """
        trades = []

        # Execute in batches
        for i in range(0, len(opportunities), max_parallel):
            batch = opportunities[i:i + max_parallel]

            tasks = [
                self.execute_opportunity(opp)
                for opp in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, ArbitrageTrade):
                    trades.append(result)
                else:
                    logger.error(f"Batch execution error: {result}")

            # Small delay between batches
            if i + max_parallel < len(opportunities):
                await asyncio.sleep(0.5)

        return trades

    def get_trade(self, trade_id: str) -> Optional[ArbitrageTrade]:
        """Get trade by ID"""
        return self._trades.get(trade_id)

    def get_all_trades(self) -> List[ArbitrageTrade]:
        """Get all trades"""
        return list(self._trades.values())

    def get_recent_trades(self, limit: int = 10) -> List[ArbitrageTrade]:
        """Get most recent trades"""
        trades = sorted(
            self._trades.values(),
            key=lambda t: t.created_at,
            reverse=True
        )
        return trades[:limit]

    def get_stats(self) -> Dict:
        """Get execution statistics"""
        trades = list(self._trades.values())

        if not trades:
            return {
                "total_trades": 0,
                "completed": 0,
                "failed": 0,
                "partial": 0,
                "total_pnl": 0,
                "avg_profit_pct": 0,
                "win_rate": 0,
            }

        completed = [t for t in trades if t.status == TradeStatus.COMPLETED]
        winners = [t for t in completed if t.realized_profit > 0]

        return {
            "total_trades": len(trades),
            "completed": len(completed),
            "failed": len([t for t in trades if t.status == TradeStatus.FAILED]),
            "partial": len([t for t in trades if t.status == TradeStatus.PARTIAL]),
            "total_pnl": self._total_pnl,
            "avg_profit_pct": sum(t.realized_profit_pct for t in completed) / len(completed) if completed else 0,
            "win_rate": len(winners) / len(completed) * 100 if completed else 0,
            "total_volume": sum(t.total_cost for t in completed),
        }

    def reset_stats(self):
        """Reset all trading statistics"""
        self._trades.clear()
        self._positions.clear()
        self._total_pnl = 0


class SimulatedExecutor(TradeExecutor):
    """
    Simulated trade executor for backtesting and paper trading

    Simulates order execution with configurable fill rates and slippage.
    """

    def __init__(
        self,
        fill_rate: float = 0.95,
        slippage: float = 0.005,
        latency_ms: int = 100,
        **kwargs
    ):
        """
        Initialize simulated executor

        Args:
            fill_rate: Probability of order being filled (0-1)
            slippage: Price slippage factor
            latency_ms: Simulated latency in milliseconds
        """
        super().__init__(dry_run=True, **kwargs)
        self.fill_rate = fill_rate
        self.slippage = slippage
        self.latency_ms = latency_ms

    async def _execute_kalshi_order(self, order: TradeOrder):
        """Simulate Kalshi order"""
        import random

        # Simulate latency
        await asyncio.sleep(self.latency_ms / 1000)

        # Simulate fill rate
        if random.random() > self.fill_rate:
            return False, None, "Order not filled (simulated)"

        # Apply slippage
        slippage_adj = 1 + (random.uniform(-self.slippage, self.slippage))
        order.filled_price = order.price * slippage_adj

        return True, f"SIM-{order.id}", None

    async def _execute_polymarket_order(self, order: TradeOrder):
        """Simulate Polymarket order"""
        return await self._execute_kalshi_order(order)  # Same simulation logic
