#!/usr/bin/env python3
"""
Polymarket-Kalshi Arbitrage Bot

Main entry point for the arbitrage trading application.
Provides CLI interface for running the server, scanning markets,
and executing trades.
"""

import asyncio
import argparse
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.utils.config import Config
from src.utils.logger import setup_logging


def run_server(args):
    """Run the web server with terminal interface"""
    from src.server import ArbitrageServer

    config = Config.from_env(args.env)

    if args.port:
        config.server.port = args.port
    if args.host:
        config.server.host = args.host
    if args.debug:
        config.server.debug = True
    if args.live:
        config.trading.dry_run = False

    server = ArbitrageServer(config)
    server.run()


async def run_scan(args):
    """Run a one-time market scan"""
    from src.api.kalshi_client import KalshiClient
    from src.api.polymarket_client import PolymarketClient
    from src.core.market_matcher import MarketMatcher
    from src.core.arbitrage_detector import ArbitrageDetector

    config = Config.from_env(args.env)
    logger = setup_logging(config.log_level)

    logger.info("Starting market scan...")

    # Initialize clients
    async with KalshiClient(
        api_key=config.kalshi.api_key,
        api_secret=config.kalshi.api_secret,
        demo_mode=config.kalshi.demo_mode
    ) as kalshi:
        async with PolymarketClient(
            private_key=config.polymarket.private_key,
            web3_provider=config.web3.provider_url
        ) as polymarket:

            # Fetch markets
            logger.info("Fetching Kalshi markets...")
            kalshi_markets = await kalshi.get_all_markets()
            logger.info(f"Found {len(kalshi_markets)} Kalshi markets")

            logger.info("Fetching Polymarket markets...")
            poly_markets = await polymarket.get_all_markets()
            logger.info(f"Found {len(poly_markets)} Polymarket markets")

            # Match markets
            logger.info("Matching markets...")
            matcher = MarketMatcher(fuzzy_threshold=args.threshold or 65)
            matches = matcher.match_markets(kalshi_markets, poly_markets)
            logger.info(f"Found {len(matches)} matched market pairs")

            # Detect arbitrage
            logger.info("Detecting arbitrage opportunities...")
            detector = ArbitrageDetector(
                min_profit_pct=args.min_profit or config.trading.min_profit_pct
            )
            opportunities = detector.scan_all(matches)

            # Output results
            profitable = [o for o in opportunities if o.is_profitable]
            logger.info(f"Found {len(opportunities)} opportunities ({len(profitable)} profitable)")

            if args.output == 'json':
                import json
                result = {
                    "matched_markets": len(matches),
                    "opportunities": [o.to_dict() for o in opportunities[:50]],
                    "stats": detector.get_stats()
                }
                print(json.dumps(result, indent=2))
            else:
                # Print table
                print("\n" + "=" * 80)
                print("ARBITRAGE OPPORTUNITIES")
                print("=" * 80)

                if not profitable:
                    print("\nNo profitable opportunities found.")
                else:
                    for i, opp in enumerate(profitable[:20], 1):
                        print(f"\n{i}. {opp.id}")
                        print(f"   Market: {opp.matched_market.kalshi_market.title[:50]}...")
                        print(f"   Buy: {opp.buy_platform.upper()} {opp.buy_side.upper()} @ ${opp.buy_price:.2f}")
                        print(f"   Sell: {opp.sell_platform.upper()} {opp.sell_side.upper()} @ ${opp.sell_price:.2f}")
                        print(f"   Profit: {opp.net_profit_pct:.2f}% | Risk: {opp.risk_score:.0f}")

                print("\n" + "=" * 80)
                print(f"Total: {len(profitable)} profitable opportunities")
                print("=" * 80)


async def run_trade(args):
    """Execute a specific trade"""
    from src.api.kalshi_client import KalshiClient
    from src.api.polymarket_client import PolymarketClient
    from src.core.trade_executor import TradeExecutor

    config = Config.from_env(args.env)
    logger = setup_logging(config.log_level)

    if not args.opportunity_id:
        logger.error("Opportunity ID required. Run 'scan' first to find opportunities.")
        return

    logger.info(f"Executing trade for opportunity: {args.opportunity_id}")
    logger.warning("Trade execution from CLI not yet implemented. Use web interface.")


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket-Kalshi Arbitrage Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py server                    # Start web server
  python main.py server --port 3000        # Start on custom port
  python main.py server --live             # Enable live trading (no dry run)
  python main.py scan                      # Run market scan
  python main.py scan --min-profit 2.0     # Scan with 2% minimum profit
  python main.py scan --output json        # Output as JSON
        """
    )

    parser.add_argument(
        '--env', '-e',
        default='.env',
        help='Path to .env configuration file'
    )

    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Server command
    server_parser = subparsers.add_parser('server', help='Run web server')
    server_parser.add_argument('--host', default=None, help='Server host')
    server_parser.add_argument('--port', '-p', type=int, default=None, help='Server port')
    server_parser.add_argument('--debug', '-d', action='store_true', help='Enable debug mode')
    server_parser.add_argument('--live', '-l', action='store_true', help='Enable live trading')

    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan markets for arbitrage')
    scan_parser.add_argument('--min-profit', type=float, help='Minimum profit percentage')
    scan_parser.add_argument('--threshold', type=float, help='Market matching threshold')
    scan_parser.add_argument('--output', choices=['table', 'json'], default='table')

    # Trade command
    trade_parser = subparsers.add_parser('trade', help='Execute a trade')
    trade_parser.add_argument('opportunity_id', nargs='?', help='Opportunity ID to execute')
    trade_parser.add_argument('--size', type=float, default=100, help='Trade size in USD')

    args = parser.parse_args()

    if args.command == 'server':
        run_server(args)
    elif args.command == 'scan':
        asyncio.run(run_scan(args))
    elif args.command == 'trade':
        asyncio.run(run_trade(args))
    else:
        # Default to server
        args.command = 'server'
        args.host = None
        args.port = None
        args.debug = False
        args.live = False
        run_server(args)


if __name__ == "__main__":
    main()
