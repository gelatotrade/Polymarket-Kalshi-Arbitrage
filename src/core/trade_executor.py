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
    """Individual order in a trade.

    ``ticker`` is the Kalshi market identifier (used only for Kalshi
    orders); ``token_id`` is the Polymarket CLOB ERC-1155 outcome
    token id (used only for Polymarket orders). Exactly one of the
    two should be populated for any given order.
    """
    id: str
    platform: str
    ticker: Optional[str]
    side: OrderSide
    outcome: str  # 'yes' or 'no'
    price: float
    quantity: float
    token_id: Optional[str] = None
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
            "token_id": self.token_id,
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

    @staticmethod
    def _resolve_identifiers(
        opportunity: ArbitrageOpportunity,
        platform: str,
        side: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(kalshi_ticker, polymarket_token_id)`` for one leg.

        Each side of an arbitrage trade lives on exactly one venue;
        this helper looks up the right identifier for that venue and
        leaves the other one ``None`` so the executor cannot silently
        send a Kalshi ticker into a Polymarket order (the original
        bug).
        """
        matched = opportunity.matched_market
        if platform == "kalshi":
            return matched.kalshi_market.ticker, None

        # Polymarket: pick the token id that corresponds to the side
        # we're trading.
        token_id = (
            matched.polymarket_market.outcome_yes_token
            if side == "yes"
            else matched.polymarket_market.outcome_no_token
        )
        return None, token_id

    async def _execute_kalshi_order(
        self,
        order: TradeOrder
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute order on Kalshi

        Returns:
            Tuple of (success, platform_order_id, error_message)
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Kalshi {order.side.value} {order.outcome} "
                       f"{order.quantity} @ {order.price}")
            return True, f"DRY-{order.id}", None

        if not self.kalshi:
            return False, None, "Kalshi client not configured"

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
        if self.dry_run:
            if not order.token_id:
                logger.warning(
                    "[DRY RUN] Polymarket order missing token_id (would fail in live mode)",
                )
            logger.info(
                "[DRY RUN] Polymarket %s %s token=%s qty=%.4f @ %.4f",
                order.side.value, order.outcome,
                order.token_id, order.quantity, order.price,
            )
            return True, f"DRY-{order.id}", None

        if not self.polymarket:
            return False, None, "Polymarket client not configured"

        if not order.token_id:
            return False, None, (
                "TradeOrder is missing token_id; cannot place a Polymarket "
                "CLOB order without the outcome token (set token_id from "
                "MatchedMarket.polymarket_market.outcome_yes_token / outcome_no_token)."
            )

        try:
            response = await self.polymarket.create_order(
                token_id=order.token_id,
                side=order.side.value.upper(),
                price=order.price,
                size=order.quantity,
            )
            order_id = response.get("orderID") or response.get("id") or response.get("orderId")
            if not order_id:
                return False, None, f"Polymarket response missing order id: {response}"
            return True, str(order_id), None

        except Exception as e:
            logger.error(f"Polymarket order failed: {e}")
            return False, None, str(e)

    async def _rollback_buy_leg(self, buy_order: TradeOrder) -> None:
        """Best-effort cancel/reverse of a filled buy leg when the sell
        leg fails. Without this we'd be left with naked exposure on
        whichever venue accepted the buy.

        For resting (unfilled) orders we cancel; for already-filled
        orders we submit a market sell at a wide tolerance. This is a
        last-ditch hedge — operators should still inspect partial
        trades manually.
        """
        if buy_order.platform == "polymarket" and buy_order.platform_order_id:
            try:
                await self.polymarket.cancel_order(buy_order.platform_order_id)
                logger.warning(
                    "Rollback: cancelled Polymarket order %s",
                    buy_order.platform_order_id,
                )
                return
            except Exception as exc:
                logger.error("Failed to cancel Polymarket buy leg: %s", exc)

        if buy_order.platform == "kalshi" and buy_order.platform_order_id:
            try:
                await self.kalshi.cancel_order(buy_order.platform_order_id)
                logger.warning(
                    "Rollback: cancelled Kalshi order %s",
                    buy_order.platform_order_id,
                )
                return
            except Exception as exc:
                logger.error("Failed to cancel Kalshi buy leg: %s", exc)

        logger.error(
            "Could not roll back buy leg %s on %s; manual intervention required",
            buy_order.id,
            buy_order.platform,
        )

    async def _check_polymarket_balance(self, required_usdc: float) -> Tuple[bool, str]:
        """Confirm the Polymarket-funded wallet holds enough USDC."""
        if self.dry_run:
            return True, "dry-run"
        if not self.polymarket or not getattr(self.polymarket, "has_signing_key", False):
            return False, "polymarket client not authenticated"
        try:
            data = await self.polymarket.get_balance_allowance(asset_type="COLLATERAL")
            balance = float(data.get("balance", 0))
            if balance < required_usdc:
                return False, f"insufficient USDC: have {balance:.2f}, need {required_usdc:.2f}"
            return True, f"balance ok: {balance:.2f} USDC"
        except Exception as exc:
            return False, f"balance check failed: {exc}"

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

        # Resolve the venue identifiers (Kalshi ticker vs Polymarket
        # CLOB token id). Mixing them up was the long-standing bug
        # that made every Polymarket leg fail.
        buy_ticker, buy_token_id = self._resolve_identifiers(
            opportunity, opportunity.buy_platform, opportunity.buy_side
        )
        sell_ticker, sell_token_id = self._resolve_identifiers(
            opportunity, opportunity.sell_platform, opportunity.sell_side
        )

        # Create orders
        buy_order = TradeOrder(
            id=self._generate_order_id(),
            platform=opportunity.buy_platform,
            ticker=buy_ticker,
            token_id=buy_token_id,
            side=OrderSide.BUY,
            outcome=opportunity.buy_side,
            price=opportunity.buy_price,
            quantity=buy_quantity
        )

        sell_order = TradeOrder(
            id=self._generate_order_id(),
            platform=opportunity.sell_platform,
            ticker=sell_ticker,
            token_id=sell_token_id,
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

        # Pre-trade balance gate for Polymarket legs (saves a wasted
        # signature + RPC round-trip when the wallet is underfunded).
        if opportunity.buy_platform == "polymarket":
            ok, msg = await self._check_polymarket_balance(trade_size)
            if not ok:
                buy_order.status = TradeStatus.FAILED
                buy_order.error = msg
                trade.status = TradeStatus.FAILED
                self._trades[trade.id] = trade
                logger.error("Aborting trade %s: %s", trade.id, msg)
                return trade

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
            logger.error(f"Sell order failed: {error}; attempting rollback of buy leg")
            await self._rollback_buy_leg(buy_order)
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
