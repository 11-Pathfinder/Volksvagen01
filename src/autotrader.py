"""Compare VW listing prices against AutoTrader UK market data."""

import logging
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from src.config import Config
from src.vw_scraper import VWListing

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
})

# Map common VW model names to AutoTrader search slugs
VW_MODEL_SLUGS = {
    "golf": "golf",
    "polo": "polo",
    "tiguan": "tiguan",
    "t-roc": "t-roc",
    "t-cross": "t-cross",
    "id.3": "id.3",
    "id.4": "id.4",
    "id.5": "id.5",
    "id.7": "id.7",
    "touareg": "touareg",
    "passat": "passat",
    "arteon": "arteon",
    "up": "up",
    "taigo": "taigo",
    "id. buzz": "id.-buzz",
}


@dataclass
class MarketValuation:
    listing: VWListing
    market_avg_price: int
    market_min_price: int
    market_max_price: int
    market_sample_size: int
    price_difference: int  # positive = VW overpriced, negative = VW underpriced
    price_difference_pct: float
    rating: str  # "great_deal", "good_deal", "fair", "overpriced", "no_data"
    comparable_url: str  # AutoTrader search URL for manual comparison

    @property
    def savings_text(self) -> str:
        if self.rating == "no_data":
            return "No market data available"
        diff = abs(self.price_difference)
        if self.price_difference < 0:
            return f"£{diff:,} below market avg ({abs(self.price_difference_pct):.1f}% cheaper)"
        elif self.price_difference > 0:
            return f"£{diff:,} above market avg ({self.price_difference_pct:.1f}% more expensive)"
        else:
            return "At market average"


def _build_search_url(listing: VWListing) -> str:
    """Build an AutoTrader search URL for comparable vehicles."""
    model_slug = ""
    model_lower = listing.model.lower()
    for name, slug in VW_MODEL_SLUGS.items():
        if name in model_lower or name in listing.title.lower():
            model_slug = slug
            break

    params = {
        "postcode": Config.AT_POSTCODE.replace(" ", ""),
        "make": "Volkswagen",
        "price-from": str(max(0, listing.price - 3000)),
        "price-to": str(listing.price + 3000),
        "year-from": str(max(2000, listing.year - 1)) if listing.year else "",
        "year-to": str(listing.year + 1) if listing.year else "",
    }

    if model_slug:
        params["model"] = model_slug

    if listing.fuel_type:
        fuel_lower = listing.fuel_type.lower()
        if "petrol" in fuel_lower:
            params["fuel-type"] = "Petrol"
        elif "diesel" in fuel_lower:
            params["fuel-type"] = "Diesel"
        elif "electric" in fuel_lower:
            params["fuel-type"] = "Electric"

    query = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return f"{Config.AT_BASE_URL}/car-search?{query}"


def _build_api_search_url(listing: VWListing) -> str:
    """Build an AutoTrader API-style search URL."""
    model_slug = ""
    model_lower = listing.model.lower()
    for name, slug in VW_MODEL_SLUGS.items():
        if name in model_lower or name in listing.title.lower():
            model_slug = slug
            break

    base = f"{Config.AT_BASE_URL}/results-car-search"
    params = [
        f"postcode={Config.AT_POSTCODE.replace(' ', '')}",
        "make=Volkswagen",
        "sort=relevance",
        "page=1",
    ]

    if model_slug:
        params.append(f"model={model_slug}")
    if listing.year:
        params.append(f"year-from={max(2000, listing.year - 1)}")
        params.append(f"year-to={listing.year + 1}")
    if listing.fuel_type:
        fuel_lower = listing.fuel_type.lower()
        if "petrol" in fuel_lower:
            params.append("fuel-type=Petrol")
        elif "diesel" in fuel_lower:
            params.append("fuel-type=Diesel")
        elif "electric" in fuel_lower:
            params.append("fuel-type=Electric")

    return f"{base}?{'&'.join(params)}"


def _extract_prices_from_html(html: str) -> list[int]:
    """Extract car prices from AutoTrader search results HTML."""
    prices = []
    soup = BeautifulSoup(html, "html.parser")

    # Try multiple price selectors
    price_selectors = [
        "[data-testid='search-listing-price']",
        ".product-card-pricing__price",
        "span[class*='price']",
        "h3[class*='price']",
        "p[class*='price']",
    ]

    for selector in price_selectors:
        elements = soup.select(selector)
        if elements:
            for el in elements:
                text = el.get_text()
                if "£" in text:
                    price = int(re.sub(r"[^\d]", "", text))
                    if 500 < price < 200000:  # sanity check
                        prices.append(price)
            if prices:
                break

    # Fallback: regex scan for prices in the page
    if not prices:
        price_matches = re.findall(r"£([\d,]+)", html)
        for match in price_matches:
            price = int(match.replace(",", ""))
            if 500 < price < 200000:
                prices.append(price)

    return prices


def get_market_valuation(listing: VWListing, delay: float = 2.0) -> MarketValuation:
    """Get market valuation for a single VW listing by searching AutoTrader."""
    search_url = _build_search_url(listing)
    api_url = _build_api_search_url(listing)

    prices = []

    # Try the API-style URL first, then the regular search URL
    for url in [api_url, search_url]:
        try:
            time.sleep(delay)  # rate limiting
            resp = SESSION.get(url, timeout=15)
            if resp.status_code == 200:
                prices = _extract_prices_from_html(resp.text)
                if prices:
                    break
        except requests.RequestException as e:
            logger.warning(f"Request failed for {url}: {e}")
            continue

    if not prices:
        return MarketValuation(
            listing=listing,
            market_avg_price=0,
            market_min_price=0,
            market_max_price=0,
            market_sample_size=0,
            price_difference=0,
            price_difference_pct=0.0,
            rating="no_data",
            comparable_url=search_url,
        )

    avg_price = sum(prices) // len(prices)
    min_price = min(prices)
    max_price = max(prices)
    diff = listing.price - avg_price
    diff_pct = (diff / avg_price * 100) if avg_price > 0 else 0

    # Determine rating
    if diff_pct <= -10:
        rating = "great_deal"
    elif diff_pct <= -3:
        rating = "good_deal"
    elif diff_pct <= 5:
        rating = "fair"
    else:
        rating = "overpriced"

    return MarketValuation(
        listing=listing,
        market_avg_price=avg_price,
        market_min_price=min_price,
        market_max_price=max_price,
        market_sample_size=len(prices),
        price_difference=diff,
        price_difference_pct=round(diff_pct, 1),
        rating=rating,
        comparable_url=search_url,
    )


def valuate_listings(listings: list[VWListing], delay: float = 2.0) -> list[MarketValuation]:
    """Get market valuations for a list of VW listings."""
    logger.info(f"Starting market valuation for {len(listings)} listings")
    valuations = []

    for i, listing in enumerate(listings):
        logger.info(f"Valuating {i + 1}/{len(listings)}: {listing.title} (£{listing.price:,})")
        valuation = get_market_valuation(listing, delay=delay)
        valuations.append(valuation)
        logger.info(f"  -> {valuation.rating}: {valuation.savings_text}")

    # Sort by best deals first
    valuations.sort(key=lambda v: v.price_difference)

    logger.info(f"Valuation complete: {len(valuations)} results")
    return valuations
