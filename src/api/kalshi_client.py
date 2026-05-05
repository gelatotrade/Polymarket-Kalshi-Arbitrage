"""
Kalshi API Client

Based on Kalshi Trading API v2.

Authenticates requests using **RSA-PSS-SHA256**, which is what
Kalshi's current API spec requires (the legacy HMAC scheme was
retired in 2024). Each request signs the string

    f"{timestamp_ms}{METHOD}{path}"

with the operator's private RSA key (loaded once from a PEM file
configured via ``KALSHI_PRIVATE_KEY_PATH``) and sends the
base64-encoded signature in the ``KALSHI-ACCESS-SIGNATURE`` header
along with ``KALSHI-ACCESS-KEY`` (the API key id) and
``KALSHI-ACCESS-TIMESTAMP``.

Documentation: https://trading-api.readme.io/reference/getting-started
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)


def _load_rsa_private_key(
    key_pem: Optional[str] = None,
    key_path: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[rsa.RSAPrivateKey]:
    """Load an RSA private key from inline PEM text or a file path.

    Returns ``None`` if no key material is available — callers should
    treat that as "read-only mode" and skip signing.
    """
    pem_bytes: Optional[bytes] = None
    if key_pem and "BEGIN" in key_pem:
        pem_bytes = key_pem.encode()
    elif key_path:
        try:
            with open(os.path.expanduser(key_path), "rb") as f:
                pem_bytes = f.read()
        except OSError as exc:
            logger.error("Failed to read Kalshi RSA key from %s: %s", key_path, exc)
            return None

    if not pem_bytes:
        return None

    pwd = password.encode() if password else None
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=pwd)
    except Exception as exc:  # invalid PEM, wrong password, etc.
        logger.error("Failed to parse Kalshi RSA key: %s", exc)
        return None

    if not isinstance(key, rsa.RSAPrivateKey):
        logger.error("Kalshi private key must be RSA (got %s)", type(key).__name__)
        return None
    return key


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
    Async client for Kalshi Trading API v2.

    Handles RSA-PSS-SHA256 authentication, market data fetching, and
    order execution. Read-only methods work without credentials but
    will be subject to Kalshi's stricter rate limits / 403s for
    unauthenticated traffic; trading methods raise unless an API key
    + RSA private key are configured.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        demo_mode: bool = False,
        private_key_pem: Optional[str] = None,
        private_key_path: Optional[str] = None,
        private_key_password: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Initialize Kalshi client.

        Args:
            api_key: Kalshi API key id (sent in ``KALSHI-ACCESS-KEY``).
            api_secret: Legacy HMAC secret. Kept for backwards
                compatibility but ignored when an RSA key is supplied
                (which the current Kalshi API requires).
            demo_mode: If true, use the demo API endpoint.
            private_key_pem: Optional inline PEM-encoded RSA key.
            private_key_path: Optional path to a PEM file holding the
                RSA private key.
            private_key_password: Password for an encrypted PEM, if any.
            base_url: Override the API base URL (useful for tests).
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo_mode = demo_mode
        self.base_url = base_url or (self.DEMO_URL if demo_mode else self.BASE_URL)
        self._private_key = _load_rsa_private_key(
            key_pem=private_key_pem,
            key_path=private_key_path,
            password=private_key_password,
        )
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    # ------------------------------------------------------------------ context

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def connect(self):
        """Initialize HTTP client"""
        self._client = httpx.AsyncClient(timeout=30.0)
        signing = "rsa" if self._private_key else "anonymous"
        logger.info(
            "Kalshi client connected (demo=%s, auth=%s)",
            self.demo_mode,
            signing,
        )

    async def disconnect(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Kalshi client disconnected")

    # ------------------------------------------------------------------ signing

    @property
    def has_signing_key(self) -> bool:
        return self._private_key is not None

    def _generate_signature(self, timestamp_ms: str, method: str, path: str) -> str:
        """Generate an RSA-PSS-SHA256 signature for the request.

        The signed string follows Kalshi's spec exactly:
        ``"{timestamp_ms}{METHOD}{path}"`` — note that ``path`` must
        be the full path **including** the API prefix
        (``/trade-api/v2/...``) but **without** the query string.
        """
        if not self._private_key:
            return ""
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _path_for_signature(self, endpoint: str) -> str:
        """Return the path portion that should be signed for ``endpoint``.

        ``endpoint`` is the path relative to ``base_url`` (e.g.
        ``/markets``). Kalshi signs the full path, so we re-join it
        with the base URL's path component.
        """
        base_path = urlsplit(self.base_url).path.rstrip("/")
        return f"{base_path}{endpoint}"

    def _get_headers(self, method: str, endpoint: str) -> Dict[str, str]:
        """Generate authenticated headers"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self.api_key and self._private_key:
            timestamp_ms = str(int(time.time() * 1000))
            signature = self._generate_signature(
                timestamp_ms,
                method,
                self._path_for_signature(endpoint),
            )
            headers["KALSHI-ACCESS-KEY"] = self.api_key
            headers["KALSHI-ACCESS-TIMESTAMP"] = timestamp_ms
            headers["KALSHI-ACCESS-SIGNATURE"] = signature
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
        headers = self._get_headers(method, endpoint)

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
