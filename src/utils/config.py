"""
Configuration Management

Loads configuration from environment variables and .env files.
"""

import os
from typing import Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass
class KalshiConfig:
    """Kalshi API configuration.

    The current Kalshi API requires RSA-PSS-SHA256 signing. Either
    point ``private_key_path`` at a PEM file or set
    ``private_key_pem`` to the inline PEM contents (e.g. when storing
    the secret in a vault that materialises env vars at runtime).
    The legacy ``api_secret`` HMAC field is retained only for tests
    that mock the auth flow; production deployments should leave it
    unset.
    """
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_mode: bool = False
    private_key_path: Optional[str] = None
    private_key_pem: Optional[str] = None
    private_key_password: Optional[str] = None


@dataclass
class PolymarketConfig:
    """Polymarket configuration"""
    api_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    private_key: Optional[str] = None


@dataclass
class Web3Config:
    """Web3 configuration"""
    provider_url: str = "https://polygon-rpc.com"
    chain_id: int = 137


@dataclass
class TradingConfig:
    """Trading parameters"""
    min_profit_pct: float = 1.0
    max_trade_size: float = 100.0
    max_position: float = 1000.0
    auto_trade: bool = False
    dry_run: bool = True
    slippage_factor: float = 0.01


@dataclass
class ServerConfig:
    """Server configuration"""
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False


@dataclass
class Config:
    """Main configuration container"""
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    web3: Web3Config = field(default_factory=Web3Config)
    trading: TradingConfig = field(default_factory=TradingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    log_level: str = "INFO"
    log_file: Optional[str] = None

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Config":
        """
        Load configuration from environment variables

        Args:
            env_file: Path to .env file

        Returns:
            Config instance
        """
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        return cls(
            kalshi=KalshiConfig(
                api_key=os.getenv("KALSHI_API_KEY"),
                api_secret=os.getenv("KALSHI_API_SECRET"),
                api_url=os.getenv("KALSHI_API_URL", KalshiConfig.api_url),
                demo_mode=os.getenv("KALSHI_DEMO_MODE", "false").lower() == "true",
                private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH"),
                private_key_pem=os.getenv("KALSHI_PRIVATE_KEY_PEM"),
                private_key_password=os.getenv("KALSHI_PRIVATE_KEY_PASSWORD"),
            ),
            polymarket=PolymarketConfig(
                api_url=os.getenv("POLYMARKET_API_URL", PolymarketConfig.api_url),
                gamma_url=os.getenv("POLYMARKET_GAMMA_API_URL", PolymarketConfig.gamma_url),
                private_key=os.getenv("POLYMARKET_PRIVATE_KEY")
            ),
            web3=Web3Config(
                provider_url=os.getenv("WEB3_PROVIDER_URL", Web3Config.provider_url),
                chain_id=int(os.getenv("POLYGON_CHAIN_ID", "137"))
            ),
            trading=TradingConfig(
                min_profit_pct=float(os.getenv("MIN_ARBITRAGE_PROFIT_PCT", "1.0")),
                max_trade_size=float(os.getenv("MAX_TRADE_SIZE_USD", "100")),
                max_position=float(os.getenv("MAX_POSITION_USD", "1000")),
                auto_trade=os.getenv("AUTO_TRADE_ENABLED", "false").lower() == "true",
                dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
                slippage_factor=float(os.getenv("SLIPPAGE_FACTOR", "0.01"))
            ),
            server=ServerConfig(
                host=os.getenv("HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8080")),
                debug=os.getenv("DEBUG", "false").lower() == "true"
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE")
        )

    def to_dict(self) -> dict:
        """Convert to dictionary (hiding sensitive values)"""
        return {
            "kalshi": {
                "api_url": self.kalshi.api_url,
                "demo_mode": self.kalshi.demo_mode,
                "has_api_key": bool(self.kalshi.api_key),
                "has_private_key": bool(
                    self.kalshi.private_key_pem or self.kalshi.private_key_path
                ),
            },
            "polymarket": {
                "api_url": self.polymarket.api_url,
                "gamma_url": self.polymarket.gamma_url,
                "has_private_key": bool(self.polymarket.private_key),
            },
            "web3": {
                "provider_url": self.web3.provider_url,
                "chain_id": self.web3.chain_id,
            },
            "trading": {
                "min_profit_pct": self.trading.min_profit_pct,
                "max_trade_size": self.trading.max_trade_size,
                "max_position": self.trading.max_position,
                "auto_trade": self.trading.auto_trade,
                "dry_run": self.trading.dry_run,
            },
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "debug": self.server.debug,
            },
            "log_level": self.log_level,
        }
