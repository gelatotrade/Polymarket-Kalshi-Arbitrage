"""
Kalshi API Client

Based on Kalshi Trading API v2
Documentation: https://docs.kalshi.com/welcome
"""

import time
import hmac
import hashlib
import base64
import json
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone
from dataclasses import dataclass
import httpx
import asyncio

logger = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    """Represents a Kalshi market"""
    ticker: str
    title: str
    subtitle: str
    category: str
    status: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume: int
    open_interest: int
    expiration_time: Optional[str]
    result: Optional[str]

    @property
    def mid_price_yes(self) -> float:
        """Calculate mid price for YES"""
        if self.yes_bid and self.yes_ask:
            return (self.yes_bid + self.yes_ask) / 2
        return self.yes_ask or self.yes_bid or 0

    @property
    def mid_price_no(self) -> float:
        """Calculate mid price for NO"""
        if self.no_bid and self.no_ask:
            return (self.no_bid + self.no_ask) / 2
        return self.no_ask or self.no_bid or 0


class KalshiClient:
    """
    Async client for Kalshi Trading API v2

    Handles authentication, market data fetching, and order execution.
    """

    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        demo_mode: bool = False
    ):
        """
        Initialize Kalshi client

        Args:
            api_key: Kalshi API key
            api_secret: Kalshi API secret (private key)
            demo_mode: Use demo API endpoint
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = self.DEMO_URL if demo_mode else self.BASE_URL
        self.demo_mode = demo_mode
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def connect(self):
        """Initialize HTTP client"""
        self._client = httpx.AsyncClient(timeout=30.0)
        logger.info(f"Kalshi client connected (demo={self.demo_mode})")

    async def disconnect(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Kalshi client disconnected")

    def _generate_signature(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """
        Generate RSA-SHA256 signature for API authentication

        Args:
            timestamp: Unix timestamp in milliseconds
            method: HTTP method (GET, POST, etc.)
            path: API endpoint path
            body: Request body (empty string for GET)

        Returns:
            Base64 encoded signature
        """
        message = f"{timestamp}{method}{path}{body}"

        # For API key auth, use HMAC-SHA256
        if self.api_secret:
            signature = hmac.new(
                self.api_secret.encode(),
                message.encode(),
                hashlib.sha256
            ).digest()
            return base64.b64encode(signature).decode()
        return ""

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate authenticated headers"""
        timestamp = str(int(time.time() * 1000))

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self.api_key:
            headers["KALSHI-ACCESS-KEY"] = self.api_key
            headers["KALSHI-ACCESS-TIMESTAMP"] = timestamp
            headers["KALSHI-ACCESS-SIGNATURE"] = self._generate_signature(
                timestamp, method, path, body
            )

        return headers

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Make authenticated API request

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            params: Query parameters
            data: Request body data

        Returns:
            API response as dictionary
        """
        if not self._client:
            await self.connect()

        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data) if data else ""
        headers = self._get_headers(method, endpoint, body)

        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                content=body if body else None
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Kalshi API error: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"Kalshi request failed: {e}")
            raise

    # ==================== Market Data ====================

    async def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        status: Optional[str] = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of markets

        Args:
            limit: Maximum number of markets to return
            cursor: Pagination cursor
            status: Market status filter (open, closed, settled)
            series_ticker: Filter by series
            event_ticker: Filter by event

        Returns:
            Dictionary with markets list and pagination cursor
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker

        return await self._request("GET", "/markets", params=params)

    async def get_all_markets(self, status: str = "open") -> List[KalshiMarket]:
        """
        Fetch all markets with pagination

        Args:
            status: Market status filter

        Returns:
            List of KalshiMarket objects
        """
        all_markets = []
        cursor = None

        while True:
            response = await self.get_markets(
                limit=200,
                cursor=cursor,
                status=status
            )

            markets = response.get("markets", [])
            for m in markets:
                market = KalshiMarket(
                    ticker=m.get("ticker", ""),
                    title=m.get("title", ""),
                    subtitle=m.get("subtitle", ""),
                    category=m.get("category", ""),
                    status=m.get("status", ""),
                    yes_bid=m.get("yes_bid", 0) / 100 if m.get("yes_bid") else 0,
                    yes_ask=m.get("yes_ask", 0) / 100 if m.get("yes_ask") else 0,
                    no_bid=m.get("no_bid", 0) / 100 if m.get("no_bid") else 0,
                    no_ask=m.get("no_ask", 0) / 100 if m.get("no_ask") else 0,
                    volume=m.get("volume", 0),
                    open_interest=m.get("open_interest", 0),
                    expiration_time=m.get("expiration_time"),
                    result=m.get("result")
                )
                all_markets.append(market)

            cursor = response.get("cursor")
            if not cursor or len(markets) == 0:
                break

            await asyncio.sleep(0.1)  # Rate limiting

        logger.info(f"Fetched {len(all_markets)} Kalshi markets")
        return all_markets

    async def get_market(self, ticker: str) -> KalshiMarket:
        """
        Get single market by ticker

        Args:
            ticker: Market ticker symbol

        Returns:
            KalshiMarket object
        """
        response = await self._request("GET", f"/markets/{ticker}")
        m = response.get("market", {})

        return KalshiMarket(
            ticker=m.get("ticker", ""),
            title=m.get("title", ""),
            subtitle=m.get("subtitle", ""),
            category=m.get("category", ""),
            status=m.get("status", ""),
            yes_bid=m.get("yes_bid", 0) / 100 if m.get("yes_bid") else 0,
            yes_ask=m.get("yes_ask", 0) / 100 if m.get("yes_ask") else 0,
            no_bid=m.get("no_bid", 0) / 100 if m.get("no_bid") else 0,
            no_ask=m.get("no_ask", 0) / 100 if m.get("no_ask") else 0,
            volume=m.get("volume", 0),
            open_interest=m.get("open_interest", 0),
            expiration_time=m.get("expiration_time"),
            result=m.get("result")
        )

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        """
        Get market orderbook

        Args:
            ticker: Market ticker
            depth: Orderbook depth

        Returns:
            Orderbook data with bids and asks
        """
        return await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            params={"depth": depth}
        )

    async def get_events(self, limit: int = 100, status: str = "open") -> Dict[str, Any]:
        """Get list of events"""
        return await self._request(
            "GET",
            "/events",
            params={"limit": limit, "status": status}
        )

    # ==================== Account ====================

    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance"""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> Dict[str, Any]:
        """Get current positions"""
        return await self._request("GET", "/portfolio/positions")

    # ==================== Trading ====================

    async def create_order(
        self,
        ticker: str,
        side: str,
        count: int,
        type: str = "market",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        expiration_ts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Create a new order

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            count: Number of contracts
            type: Order type ("market" or "limit")
            yes_price: Limit price for yes in cents (1-99)
            no_price: Limit price for no in cents (1-99)
            expiration_ts: Order expiration timestamp

        Returns:
            Order response with order_id
        """
        data = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": type
        }

        if type == "limit":
            if yes_price:
                data["yes_price"] = yes_price
            elif no_price:
                data["no_price"] = no_price

        if expiration_ts:
            data["expiration_ts"] = expiration_ts

        return await self._request("POST", "/portfolio/orders", data=data)

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an existing order"""
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: Optional[str] = None,
        status: str = "resting"
    ) -> Dict[str, Any]:
        """Get orders"""
        params = {"status": status}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/orders", params=params)

    async def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """Get order fills/trades"""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/fills", params=params)


# Example usage
async def main():
    """Test Kalshi client"""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    async with KalshiClient(
        api_key=os.getenv("KALSHI_API_KEY"),
        api_secret=os.getenv("KALSHI_API_SECRET"),
        demo_mode=True
    ) as client:
        # Get markets
        markets = await client.get_all_markets()
        print(f"Found {len(markets)} markets")

        for market in markets[:5]:
            print(f"  {market.ticker}: {market.title}")
            print(f"    YES: {market.yes_bid:.2f}/{market.yes_ask:.2f}")
            print(f"    NO: {market.no_bid:.2f}/{market.no_ask:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
