"""
Polymarket API Client.

Read-only market data is fetched through the public Gamma API
directly (async via httpx). Trading goes through the official
``py-clob-client`` Python SDK, which handles the EIP-712 typed
data order signing, L1/L2 auth derivation and tick-size lookups
that real Polymarket CLOB orders require. The synchronous SDK
calls are wrapped with ``asyncio.to_thread`` so they do not block
the event loop.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

try:  # The SDK is required for trading; reading still works without it.
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds,
        BalanceAllowanceParams,
        OrderArgs,
        OrderType,
    )
    from py_clob_client.constants import POLYGON
    _CLOB_SDK_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - SDK missing in some envs
    ClobClient = None  # type: ignore[assignment]
    ApiCreds = None  # type: ignore[assignment]
    BalanceAllowanceParams = None  # type: ignore[assignment]
    OrderArgs = None  # type: ignore[assignment]
    OrderType = None  # type: ignore[assignment]
    POLYGON = 137
    _CLOB_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class PolymarketMarket:
    """Represents a Polymarket market/condition"""
    condition_id: str
    question_id: str
    question: str
    description: str
    outcome_yes_token: str
    outcome_no_token: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    end_date: Optional[str]
    category: str
    active: bool
    closed: bool

    @property
    def mid_price_yes(self) -> float:
        """Mid price for YES outcome"""
        return self.yes_price

    @property
    def mid_price_no(self) -> float:
        """Mid price for NO outcome"""
        return self.no_price


@dataclass
class OrderbookLevel:
    """Single level in orderbook"""
    price: float
    size: float


class PolymarketClient:
    """
    Async client for Polymarket CLOB API

    Handles market data fetching and order execution via Web3 wallet.
    """

    CLOB_URL = "https://clob.polymarket.com"
    GAMMA_URL = "https://gamma-api.polymarket.com"
    STRAPI_URL = "https://strapi-matic.poly.market"

    # Polygon network config
    POLYGON_CHAIN_ID = 137
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        web3_provider: Optional[str] = None,
        clob_url: Optional[str] = None,
        gamma_url: Optional[str] = None,
        chain_id: int = POLYGON_CHAIN_ID,
    ):
        """Initialize Polymarket client.

        Args:
            private_key: Ethereum wallet private key (hex). Required
                for trading; optional for read-only usage.
            api_key, api_secret, api_passphrase: Pre-derived CLOB
                L2 credentials. If omitted but ``private_key`` is
                supplied, the SDK will derive them on first connect.
            web3_provider: Polygon RPC URL (e.g. Alchemy / Infura).
            clob_url, gamma_url: API base overrides (handy for tests).
            chain_id: EVM chain id (defaults to Polygon mainnet).
        """
        self.private_key = private_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.clob_url = clob_url or self.CLOB_URL
        self.gamma_url = gamma_url or self.GAMMA_URL
        self.chain_id = chain_id

        self._client: Optional[httpx.AsyncClient] = None
        self._address: Optional[str] = None

        # CLOB SDK (sync) — instantiated lazily on first auth use.
        self._clob: Optional[ClobClient] = None

        # Initialize Web3
        if web3_provider:
            self.w3 = Web3(Web3.HTTPProvider(web3_provider))
        else:
            self.w3 = None

        # Derive address from private key
        if private_key:
            account = Account.from_key(private_key)
            self._address = account.address

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def connect(self):
        """Initialize HTTP client (and the CLOB SDK if we have a key)."""
        self._client = httpx.AsyncClient(timeout=30.0)
        signing = "wallet" if self.private_key else "anonymous"
        logger.info("Polymarket client connected (auth=%s)", signing)

    async def disconnect(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Polymarket client disconnected")

    # ------------------------------------------------------------------ CLOB SDK

    @property
    def has_signing_key(self) -> bool:
        return bool(self.private_key)

    def _ensure_clob(self) -> ClobClient:
        """Lazily initialise the synchronous CLOB SDK and ensure it
        has L2 API credentials so it can place signed orders.

        Raises ``RuntimeError`` if either the SDK isn't installed or
        the operator did not provide a private key.
        """
        if not _CLOB_SDK_AVAILABLE:
            raise RuntimeError(
                "py-clob-client is not installed; trading is disabled. "
                "Add `py-clob-client>=0.0.7` to your environment."
            )
        if not self.private_key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY is required to place CLOB orders."
            )

        if self._clob is not None:
            return self._clob

        if self.api_key and self.api_secret and self.api_passphrase:
            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            client = ClobClient(
                host=self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
                creds=creds,
            )
        else:
            # Derive L2 creds from the wallet on first use. This calls
            # the CLOB ``/auth/derive-api-key`` endpoint under the hood.
            client = ClobClient(
                host=self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
            )
            try:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                self.api_key = creds.api_key
                self.api_secret = creds.api_secret
                self.api_passphrase = creds.api_passphrase
                logger.info(
                    "Derived Polymarket CLOB API credentials for %s",
                    self._address,
                )
            except Exception as exc:
                logger.error("Failed to derive Polymarket CLOB credentials: %s", exc)
                raise

        self._clob = client
        return client

    async def _run_clob(self, fn, *args, **kwargs):
        """Run a synchronous CLOB SDK call without blocking the loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    @property
    def address(self) -> Optional[str]:
        """Get wallet address"""
        return self._address

    def _sign_message(self, message: str) -> str:
        """Sign a message with private key"""
        if not self.private_key:
            raise ValueError("Private key required for signing")

        message_hash = encode_defunct(text=message)
        signed = Account.sign_message(message_hash, self.private_key)
        return signed.signature.hex()

    def _get_headers(self, signed: bool = False) -> Dict[str, str]:
        """Generate API headers"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if signed and self.api_key:
            timestamp = str(int(time.time()))
            headers.update({
                "POLY_ADDRESS": self._address or "",
                "POLY_SIGNATURE": self._sign_message(timestamp),
                "POLY_TIMESTAMP": timestamp,
                "POLY_NONCE": str(int(time.time() * 1000)),
            })

        return headers

    async def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        signed: bool = False
    ) -> Any:
        """Make API request"""
        if not self._client:
            await self.connect()

        headers = self._get_headers(signed)

        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=data
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Polymarket API error: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Polymarket request failed: {e}")
            raise

    # ==================== Market Data (Gamma API) ====================

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get markets from Gamma API

        Args:
            limit: Maximum markets to return
            offset: Pagination offset
            active: Filter active markets
            closed: Include closed markets

        Returns:
            List of market dictionaries
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }

        return await self._request(
            "GET",
            f"{self.GAMMA_URL}/markets",
            params=params
        )

    async def get_all_markets(self, active_only: bool = True) -> List[PolymarketMarket]:
        """
        Fetch all markets with pagination

        Args:
            active_only: Only return active markets

        Returns:
            List of PolymarketMarket objects
        """
        all_markets = []
        offset = 0
        limit = 100

        while True:
            try:
                markets = await self.get_markets(
                    limit=limit,
                    offset=offset,
                    active=active_only,
                    closed=not active_only
                )

                if not markets:
                    break

                for m in markets:
                    # Extract token prices from CLOB data if available
                    clob_token_ids = m.get("clobTokenIds", [])
                    outcomes = m.get("outcomes", [])
                    outcome_prices = m.get("outcomePrices", [])

                    yes_token = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
                    no_token = clob_token_ids[1] if len(clob_token_ids) > 1 else ""

                    # Parse prices
                    yes_price = 0.0
                    no_price = 0.0
                    if outcome_prices:
                        try:
                            yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0
                            no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0
                        except (ValueError, TypeError):
                            pass

                    market = PolymarketMarket(
                        condition_id=m.get("conditionId", ""),
                        question_id=m.get("questionId", ""),
                        question=m.get("question", ""),
                        description=m.get("description", ""),
                        outcome_yes_token=yes_token,
                        outcome_no_token=no_token,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=float(m.get("volume", 0) or 0),
                        liquidity=float(m.get("liquidity", 0) or 0),
                        end_date=m.get("endDate"),
                        category=m.get("category", ""),
                        active=m.get("active", False),
                        closed=m.get("closed", False)
                    )
                    all_markets.append(market)

                offset += limit
                await asyncio.sleep(0.1)  # Rate limiting

                if len(markets) < limit:
                    break

            except Exception as e:
                logger.error(f"Error fetching Polymarket markets at offset {offset}: {e}")
                break

        logger.info(f"Fetched {len(all_markets)} Polymarket markets")
        return all_markets

    async def get_market(self, condition_id: str) -> Optional[PolymarketMarket]:
        """Get single market by condition ID"""
        try:
            result = await self._request(
                "GET",
                f"{self.GAMMA_URL}/markets/{condition_id}"
            )

            if result:
                m = result
                clob_token_ids = m.get("clobTokenIds", [])
                outcome_prices = m.get("outcomePrices", [])

                yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0
                no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0

                return PolymarketMarket(
                    condition_id=m.get("conditionId", ""),
                    question_id=m.get("questionId", ""),
                    question=m.get("question", ""),
                    description=m.get("description", ""),
                    outcome_yes_token=clob_token_ids[0] if clob_token_ids else "",
                    outcome_no_token=clob_token_ids[1] if len(clob_token_ids) > 1 else "",
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0),
                    end_date=m.get("endDate"),
                    category=m.get("category", ""),
                    active=m.get("active", False),
                    closed=m.get("closed", False)
                )
        except Exception as e:
            logger.error(f"Error fetching market {condition_id}: {e}")
            return None

    async def search_markets(self, query: str) -> List[Dict[str, Any]]:
        """Search markets by query string"""
        return await self._request(
            "GET",
            f"{self.GAMMA_URL}/markets",
            params={"_q": query, "limit": 50}
        )

    # ==================== CLOB API ====================

    async def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Get orderbook for a token from CLOB

        Args:
            token_id: CLOB token ID

        Returns:
            Orderbook with bids and asks
        """
        return await self._request(
            "GET",
            f"{self.CLOB_URL}/book",
            params={"token_id": token_id}
        )

    async def get_price(self, token_id: str, side: str = "buy") -> Dict[str, Any]:
        """
        Get current price for token

        Args:
            token_id: CLOB token ID
            side: "buy" or "sell"

        Returns:
            Price data
        """
        return await self._request(
            "GET",
            f"{self.CLOB_URL}/price",
            params={"token_id": token_id, "side": side}
        )

    async def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for token"""
        try:
            result = await self._request(
                "GET",
                f"{self.CLOB_URL}/midpoint",
                params={"token_id": token_id}
            )
            return float(result.get("mid", 0))
        except Exception:
            return 0.0

    async def get_spread(self, token_id: str) -> Dict[str, float]:
        """Get bid-ask spread for token"""
        return await self._request(
            "GET",
            f"{self.CLOB_URL}/spread",
            params={"token_id": token_id}
        )

    # ==================== Trading (via py-clob-client SDK) ====================

    async def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        fee_rate_bps: int = 0,
    ) -> Dict[str, Any]:
        """Sign and submit a CLOB limit order.

        The SDK handles EIP-712 typed data signing, salt/nonce
        generation, tick-size lookup and L2 auth headers.

        Args:
            token_id: CLOB ERC-1155 token id (numeric string).
            side: "BUY" or "SELL".
            price: Limit price in [0, 1].
            size: Number of contracts (not USDC notional).
            order_type: "GTC", "GTD" or "FOK".
            fee_rate_bps: Maker fee in basis points; usually 0.
        """
        clob = self._ensure_clob()

        # Map our string order types to the SDK enum.
        order_type_map = {"GTC": OrderType.GTC, "GTD": OrderType.GTD, "FOK": OrderType.FOK}
        ot = order_type_map.get(order_type.upper(), OrderType.GTC)

        order_args = OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(size),
            side=side.upper(),
            fee_rate_bps=int(fee_rate_bps),
        )

        signed_order = await self._run_clob(clob.create_order, order_args)
        return await self._run_clob(clob.post_order, signed_order, ot)

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an existing order via the SDK."""
        clob = self._ensure_clob()
        return await self._run_clob(clob.cancel, order_id)

    async def cancel_all(self) -> Dict[str, Any]:
        """Cancel every resting order for the wallet."""
        clob = self._ensure_clob()
        return await self._run_clob(clob.cancel_all)

    async def get_orders(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders for the wallet."""
        clob = self._ensure_clob()
        if market:
            return await self._run_clob(clob.get_orders, market=market)
        return await self._run_clob(clob.get_orders)

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get a single order by id (used to poll for fills)."""
        clob = self._ensure_clob()
        return await self._run_clob(clob.get_order, order_id)

    async def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> Dict[str, Any]:
        """Read on-chain USDC balance + approval for the proxy wallet.

        ``asset_type`` is "COLLATERAL" for USDC or "CONDITIONAL" for
        outcome tokens. Used by the trade executor to skip arbs the
        wallet cannot actually fund.
        """
        clob = self._ensure_clob()
        params = BalanceAllowanceParams(asset_type=asset_type)
        return await self._run_clob(clob.get_balance_allowance, params)

    async def get_trades(
        self,
        market: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get trade history"""
        params = {"limit": limit}
        if market:
            params["market"] = market

        return await self._request(
            "GET",
            f"{self.CLOB_URL}/trades",
            params=params,
            signed=True
        )

    # ==================== Events API ====================

    async def get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get events/series from Gamma API"""
        return await self._request(
            "GET",
            f"{self.GAMMA_URL}/events",
            params={"limit": limit, "active": "true"}
        )


# Example usage
async def main():
    """Test Polymarket client"""
    async with PolymarketClient() as client:
        # Get markets (no auth required for reading)
        markets = await client.get_all_markets()
        print(f"Found {len(markets)} markets")

        for market in markets[:5]:
            print(f"\n  {market.question[:60]}...")
            print(f"    YES: ${market.yes_price:.2f}")
            print(f"    NO: ${market.no_price:.2f}")
            print(f"    Volume: ${market.volume:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
