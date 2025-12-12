"""
Market Matcher Module

Matches similar markets between Kalshi and Polymarket using fuzzy string matching
and semantic similarity analysis.
"""

import re
import logging
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass
from datetime import datetime
from fuzzywuzzy import fuzz, process

from ..api.kalshi_client import KalshiMarket
from ..api.polymarket_client import PolymarketMarket

logger = logging.getLogger(__name__)


@dataclass
class MatchedMarket:
    """Represents a matched market pair between Kalshi and Polymarket"""
    kalshi_market: KalshiMarket
    polymarket_market: PolymarketMarket
    similarity_score: float
    match_type: str  # 'exact', 'fuzzy', 'semantic'
    matched_at: datetime

    @property
    def identifier(self) -> str:
        """Unique identifier for the match"""
        return f"{self.kalshi_market.ticker}:{self.polymarket_market.condition_id}"

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "kalshi": {
                "ticker": self.kalshi_market.ticker,
                "title": self.kalshi_market.title,
                "yes_bid": self.kalshi_market.yes_bid,
                "yes_ask": self.kalshi_market.yes_ask,
                "no_bid": self.kalshi_market.no_bid,
                "no_ask": self.kalshi_market.no_ask,
                "volume": self.kalshi_market.volume,
            },
            "polymarket": {
                "condition_id": self.polymarket_market.condition_id,
                "question": self.polymarket_market.question,
                "yes_price": self.polymarket_market.yes_price,
                "no_price": self.polymarket_market.no_price,
                "volume": self.polymarket_market.volume,
            },
            "similarity_score": self.similarity_score,
            "match_type": self.match_type,
            "matched_at": self.matched_at.isoformat(),
        }


class MarketMatcher:
    """
    Matches markets between Kalshi and Polymarket

    Uses multiple matching strategies:
    1. Exact matching on normalized titles
    2. Fuzzy string matching with configurable threshold
    3. Keyword extraction and comparison
    4. Category-based filtering
    """

    # Common words to ignore during matching
    STOP_WORDS = {
        'will', 'the', 'be', 'to', 'in', 'on', 'at', 'by', 'for', 'of', 'a', 'an',
        'or', 'and', 'is', 'it', 'this', 'that', 'with', 'as', 'from', 'has', 'have',
        'before', 'after', 'during', 'until', 'while', 'above', 'below', 'between',
        'into', 'through', 'over', 'under', 'again', 'further', 'then', 'once',
        'yes', 'no', 'what', 'which', 'who', 'whom', 'when', 'where', 'why', 'how',
    }

    # Category mappings between platforms
    CATEGORY_MAPPINGS = {
        # Kalshi categories -> Polymarket categories
        'Politics': ['politics', 'elections', 'government'],
        'Economics': ['economics', 'finance', 'markets', 'crypto'],
        'Climate': ['climate', 'environment', 'weather'],
        'Science': ['science', 'technology', 'space'],
        'Entertainment': ['entertainment', 'sports', 'culture'],
        'COVID': ['covid', 'health', 'pandemic'],
    }

    def __init__(
        self,
        fuzzy_threshold: float = 70.0,
        exact_threshold: float = 95.0,
        use_category_filter: bool = True
    ):
        """
        Initialize the market matcher

        Args:
            fuzzy_threshold: Minimum fuzzy match score (0-100)
            exact_threshold: Score threshold to consider exact match
            use_category_filter: Filter by category before matching
        """
        self.fuzzy_threshold = fuzzy_threshold
        self.exact_threshold = exact_threshold
        self.use_category_filter = use_category_filter
        self._matched_markets: Dict[str, MatchedMarket] = {}

    def normalize_text(self, text: str) -> str:
        """
        Normalize text for comparison

        - Lowercase
        - Remove special characters
        - Remove extra whitespace
        - Remove common words
        """
        # Lowercase
        text = text.lower()

        # Remove special characters but keep alphanumeric and spaces
        text = re.sub(r'[^a-z0-9\s]', ' ', text)

        # Split into words and remove stop words
        words = text.split()
        words = [w for w in words if w not in self.STOP_WORDS and len(w) > 1]

        # Rejoin with single spaces
        return ' '.join(words)

    def extract_keywords(self, text: str) -> Set[str]:
        """Extract important keywords from text"""
        normalized = self.normalize_text(text)
        words = set(normalized.split())

        # Also extract potential entity patterns (capitalized sequences in original)
        entities = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', text)
        for entity in entities:
            words.add(entity.lower())

        return words

    def extract_numbers(self, text: str) -> List[str]:
        """Extract numbers and percentages from text"""
        # Match numbers, percentages, and dates
        numbers = re.findall(r'\d+(?:\.\d+)?%?', text)
        # Match year patterns
        years = re.findall(r'20\d{2}', text)
        return numbers + years

    def calculate_keyword_overlap(
        self,
        keywords1: Set[str],
        keywords2: Set[str]
    ) -> float:
        """Calculate Jaccard similarity between keyword sets"""
        if not keywords1 or not keywords2:
            return 0.0

        intersection = keywords1 & keywords2
        union = keywords1 | keywords2

        return len(intersection) / len(union) * 100 if union else 0.0

    def match_single(
        self,
        kalshi_market: KalshiMarket,
        polymarket_markets: List[PolymarketMarket]
    ) -> Optional[MatchedMarket]:
        """
        Find the best matching Polymarket market for a Kalshi market

        Args:
            kalshi_market: Kalshi market to match
            polymarket_markets: List of Polymarket markets to search

        Returns:
            MatchedMarket if found, None otherwise
        """
        kalshi_text = f"{kalshi_market.title} {kalshi_market.subtitle}"
        kalshi_normalized = self.normalize_text(kalshi_text)
        kalshi_keywords = self.extract_keywords(kalshi_text)
        kalshi_numbers = set(self.extract_numbers(kalshi_text))

        best_match: Optional[PolymarketMarket] = None
        best_score = 0.0
        match_type = 'fuzzy'

        for poly_market in polymarket_markets:
            poly_text = f"{poly_market.question} {poly_market.description}"
            poly_normalized = self.normalize_text(poly_text)
            poly_keywords = self.extract_keywords(poly_text)
            poly_numbers = set(self.extract_numbers(poly_text))

            # Skip if important numbers don't match (dates, percentages)
            if kalshi_numbers and poly_numbers:
                if not (kalshi_numbers & poly_numbers):
                    continue

            # Calculate fuzzy match score
            fuzzy_score = fuzz.token_sort_ratio(kalshi_normalized, poly_normalized)

            # Calculate keyword overlap bonus
            keyword_score = self.calculate_keyword_overlap(kalshi_keywords, poly_keywords)

            # Combined score (weighted average)
            combined_score = (fuzzy_score * 0.7) + (keyword_score * 0.3)

            if combined_score > best_score and combined_score >= self.fuzzy_threshold:
                best_score = combined_score
                best_match = poly_market

                if combined_score >= self.exact_threshold:
                    match_type = 'exact'

        if best_match:
            return MatchedMarket(
                kalshi_market=kalshi_market,
                polymarket_market=best_match,
                similarity_score=best_score,
                match_type=match_type,
                matched_at=datetime.utcnow()
            )

        return None

    def match_markets(
        self,
        kalshi_markets: List[KalshiMarket],
        polymarket_markets: List[PolymarketMarket],
        min_volume: float = 0
    ) -> List[MatchedMarket]:
        """
        Match all markets between platforms

        Args:
            kalshi_markets: List of Kalshi markets
            polymarket_markets: List of Polymarket markets
            min_volume: Minimum combined volume to consider

        Returns:
            List of matched market pairs
        """
        matches = []
        used_poly_ids = set()

        # Sort Kalshi markets by volume (most active first)
        sorted_kalshi = sorted(
            kalshi_markets,
            key=lambda m: m.volume,
            reverse=True
        )

        # Filter Polymarket markets by activity
        active_poly = [
            m for m in polymarket_markets
            if m.active and not m.closed and m.volume >= min_volume
        ]

        logger.info(
            f"Matching {len(sorted_kalshi)} Kalshi markets "
            f"against {len(active_poly)} Polymarket markets"
        )

        for kalshi in sorted_kalshi:
            # Skip already matched
            available_poly = [
                m for m in active_poly
                if m.condition_id not in used_poly_ids
            ]

            match = self.match_single(kalshi, available_poly)

            if match:
                matches.append(match)
                used_poly_ids.add(match.polymarket_market.condition_id)
                self._matched_markets[match.identifier] = match

                logger.debug(
                    f"Matched: {kalshi.title[:50]}... <-> "
                    f"{match.polymarket_market.question[:50]}... "
                    f"(score: {match.similarity_score:.1f})"
                )

        logger.info(f"Found {len(matches)} matched market pairs")
        return matches

    def get_matched_markets(self) -> Dict[str, MatchedMarket]:
        """Get all matched markets"""
        return self._matched_markets.copy()

    def find_by_kalshi_ticker(self, ticker: str) -> Optional[MatchedMarket]:
        """Find matched market by Kalshi ticker"""
        for match in self._matched_markets.values():
            if match.kalshi_market.ticker == ticker:
                return match
        return None

    def find_by_polymarket_id(self, condition_id: str) -> Optional[MatchedMarket]:
        """Find matched market by Polymarket condition ID"""
        for match in self._matched_markets.values():
            if match.polymarket_market.condition_id == condition_id:
                return match
        return None


# Example usage
def main():
    """Test market matcher"""
    from ..api.kalshi_client import KalshiMarket
    from ..api.polymarket_client import PolymarketMarket

    # Create sample markets
    kalshi = KalshiMarket(
        ticker="PRES-24-DEM",
        title="Will a Democrat win the 2024 Presidential Election?",
        subtitle="US Presidential Election 2024",
        category="Politics",
        status="open",
        yes_bid=0.45,
        yes_ask=0.47,
        no_bid=0.53,
        no_ask=0.55,
        volume=100000,
        open_interest=50000,
        expiration_time="2024-11-05T00:00:00Z",
        result=None
    )

    poly = PolymarketMarket(
        condition_id="0x1234",
        question_id="question-1",
        question="Will the Democratic candidate win the 2024 US Presidential Election?",
        description="This market resolves to Yes if the Democratic party nominee wins the 2024 presidential election.",
        outcome_yes_token="token-yes",
        outcome_no_token="token-no",
        yes_price=0.46,
        no_price=0.54,
        volume=500000,
        liquidity=100000,
        end_date="2024-11-05",
        category="politics",
        active=True,
        closed=False
    )

    matcher = MarketMatcher(fuzzy_threshold=60)
    match = matcher.match_single(kalshi, [poly])

    if match:
        print(f"Match found! Score: {match.similarity_score:.1f}")
        print(f"  Kalshi: {match.kalshi_market.title}")
        print(f"  Polymarket: {match.polymarket_market.question}")


if __name__ == "__main__":
    main()
