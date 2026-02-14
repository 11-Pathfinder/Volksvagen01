"""Scrape used car listings from usedcars.volkswagen.co.uk using Playwright."""

import json
import logging
import re
from dataclasses import dataclass, asdict
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class VWListing:
    title: str
    price: int  # GBP
    mileage: int  # miles
    year: int
    fuel_type: str
    transmission: str
    model: str
    trim: str
    registration: str
    dealer: str
    location: str
    url: str
    image_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_int(text: str) -> int:
    """Extract integer from text like '£18,995' or '23,456 miles'."""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _extract_year(text: str) -> int:
    """Extract 4-digit year from text."""
    match = re.search(r"(20\d{2})", text)
    return int(match.group(1)) if match else 0


def _try_intercept_api(page: Page) -> list[dict] | None:
    """Try to capture JSON responses from internal API calls during page load."""
    captured = []

    def handle_response(response):
        url = response.url
        if any(kw in url.lower() for kw in ["vehicle", "search", "stock", "inventory", "api"]):
            try:
                if "application/json" in response.headers.get("content-type", ""):
                    data = response.json()
                    captured.append({"url": url, "data": data})
            except Exception:
                pass

    page.on("response", handle_response)
    page.goto(Config.VW_SEARCH_URL, wait_until="networkidle", timeout=30000)
    page.off("response", handle_response)
    return captured if captured else None


def _parse_listings_from_api(api_data: list[dict]) -> list[VWListing]:
    """Parse car listings from intercepted API responses."""
    listings = []
    for item in api_data:
        data = item.get("data", {})
        # The API response structure varies - look for common patterns
        vehicles = []
        if isinstance(data, dict):
            for key in ["vehicles", "results", "items", "data", "content", "hits"]:
                if key in data:
                    candidate = data[key]
                    if isinstance(candidate, list):
                        vehicles = candidate
                        break
            if not vehicles and isinstance(data.get("data"), dict):
                for key in ["vehicles", "results", "items"]:
                    if key in data["data"]:
                        candidate = data["data"][key]
                        if isinstance(candidate, list):
                            vehicles = candidate
                            break
        elif isinstance(data, list):
            vehicles = data

        for v in vehicles:
            if not isinstance(v, dict):
                continue
            try:
                listing = VWListing(
                    title=v.get("title", v.get("name", v.get("headline", ""))),
                    price=_extract_int(str(v.get("price", v.get("retailPrice", v.get("cashPrice", "0"))))),
                    mileage=_extract_int(str(v.get("mileage", v.get("odometer", "0")))),
                    year=int(v.get("year", v.get("modelYear", v.get("registrationYear", 0)))),
                    fuel_type=v.get("fuelType", v.get("fuel", "")),
                    transmission=v.get("transmission", v.get("gearbox", "")),
                    model=v.get("model", v.get("modelName", "")),
                    trim=v.get("trim", v.get("trimLevel", v.get("derivative", ""))),
                    registration=v.get("registration", v.get("vrm", v.get("regNumber", ""))),
                    dealer=v.get("dealer", {}).get("name", v.get("dealerName", "")) if isinstance(v.get("dealer"), dict) else v.get("dealer", ""),
                    location=v.get("location", v.get("dealerLocation", "")),
                    url=v.get("url", v.get("detailUrl", "")),
                    image_url=v.get("imageUrl", v.get("image", "")),
                )
                if listing.price > 0:
                    listings.append(listing)
            except (ValueError, TypeError, KeyError):
                continue
    return listings


def _parse_listings_from_html(page: Page) -> list[VWListing]:
    """Parse car listings from the rendered HTML DOM."""
    listings = []

    # Common selectors for car listing cards across dealer platforms
    card_selectors = [
        "[data-testid='vehicle-card']",
        ".vehicle-card",
        ".car-card",
        ".search-result-item",
        ".vehicle-listing",
        ".result-card",
        "article[class*='vehicle']",
        "article[class*='car']",
        "div[class*='VehicleCard']",
        "div[class*='vehicleCard']",
        "a[href*='/vehicle/']",
    ]

    cards = []
    for selector in card_selectors:
        cards = page.query_selector_all(selector)
        if cards:
            logger.info(f"Found {len(cards)} listings using selector: {selector}")
            break

    if not cards:
        # Fallback: look for any repeated elements that look like car cards
        logger.warning("No listings found with known selectors, attempting heuristic detection")
        all_links = page.query_selector_all("a[href*='/vehicle/']")
        if all_links:
            cards = all_links
            logger.info(f"Found {len(cards)} vehicle links via href heuristic")

    for card in cards:
        try:
            # Extract text content and links
            text = card.inner_text()
            href = card.get_attribute("href") or ""

            # Find nested link if the card itself isn't a link
            if not href:
                link_el = card.query_selector("a[href*='/vehicle/']") or card.query_selector("a")
                if link_el:
                    href = link_el.get_attribute("href") or ""

            # Build full URL
            if href and not href.startswith("http"):
                href = Config.VW_BASE_URL + href

            # Extract image
            img_el = card.query_selector("img")
            img_url = img_el.get_attribute("src") or "" if img_el else ""

            # Parse structured data from text
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            title = lines[0] if lines else ""

            price = 0
            mileage = 0
            year = 0
            fuel_type = ""
            transmission = ""

            for line in lines:
                line_lower = line.lower()
                if "£" in line and not price:
                    price = _extract_int(line)
                if "mile" in line_lower and not mileage:
                    mileage = _extract_int(line)
                if not year:
                    year = _extract_year(line)
                if any(f in line_lower for f in ["petrol", "diesel", "electric", "hybrid"]):
                    fuel_type = line.strip()
                if any(t in line_lower for t in ["manual", "automatic", "auto", "dsg"]):
                    transmission = line.strip()

            # Try to extract model from title
            model = ""
            for m in ["golf", "polo", "tiguan", "t-roc", "t-cross", "id.3", "id.4",
                       "id.5", "id.7", "touareg", "passat", "arteon", "up", "taigo"]:
                if m in title.lower():
                    model = m.title()
                    break

            if price > 0:
                listings.append(VWListing(
                    title=title,
                    price=price,
                    mileage=mileage,
                    year=year,
                    fuel_type=fuel_type,
                    transmission=transmission,
                    model=model,
                    trim="",
                    registration="",
                    dealer="",
                    location="",
                    url=href,
                    image_url=img_url,
                ))
        except Exception as e:
            logger.debug(f"Failed to parse card: {e}")
            continue

    return listings


def _handle_cookie_consent(page: Page) -> None:
    """Dismiss cookie consent banners if present."""
    consent_selectors = [
        "button[id*='accept']",
        "button[class*='accept']",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "#onetrust-accept-btn-handler",
        ".cookie-accept",
    ]
    for selector in consent_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def _handle_postcode_entry(page: Page) -> None:
    """Enter postcode if prompted."""
    postcode_selectors = [
        "input[placeholder*='postcode' i]",
        "input[name*='postcode' i]",
        "input[id*='postcode' i]",
        "input[aria-label*='postcode' i]",
        "input[type='text'][placeholder*='location' i]",
    ]
    for selector in postcode_selectors:
        try:
            inp = page.query_selector(selector)
            if inp and inp.is_visible():
                inp.fill(Config.VW_POSTCODE)
                # Look for a submit button near the input
                submit = page.query_selector(
                    "button[type='submit'], button:has-text('Search'), button:has-text('Go')"
                )
                if submit and submit.is_visible():
                    submit.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                return
        except Exception:
            continue


def scrape_vw_listings() -> list[VWListing]:
    """Main entry point: scrape VW used car listings and return structured data."""
    logger.info("Starting VW used car scraper")
    all_listings: list[VWListing] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        try:
            # First try: intercept API calls
            logger.info("Attempting to intercept API calls...")
            api_data = _try_intercept_api(page)

            if api_data:
                logger.info(f"Intercepted {len(api_data)} API responses")
                all_listings = _parse_listings_from_api(api_data)
                if all_listings:
                    logger.info(f"Parsed {len(all_listings)} listings from API data")

            # If API interception didn't yield results, parse HTML
            if not all_listings:
                logger.info("Falling back to HTML parsing...")
                _handle_cookie_consent(page)
                _handle_postcode_entry(page)

                # Wait for content to load
                page.wait_for_timeout(3000)

                # Parse current page
                all_listings = _parse_listings_from_html(page)
                logger.info(f"Found {len(all_listings)} listings on first page")

                # Handle pagination
                for page_num in range(2, Config.VW_MAX_PAGES + 1):
                    if len(all_listings) == 0:
                        break  # No listings found at all, don't paginate

                    next_selectors = [
                        "a[aria-label='Next']",
                        "button[aria-label='Next']",
                        "a:has-text('Next')",
                        "button:has-text('Next')",
                        "[class*='pagination'] a:last-child",
                        "a[rel='next']",
                    ]
                    clicked = False
                    for sel in next_selectors:
                        try:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click()
                                page.wait_for_load_state("networkidle", timeout=15000)
                                page.wait_for_timeout(2000)
                                clicked = True
                                break
                        except Exception:
                            continue

                    if not clicked:
                        logger.info(f"No more pages after page {page_num - 1}")
                        break

                    page_listings = _parse_listings_from_html(page)
                    if not page_listings:
                        break
                    all_listings.extend(page_listings)
                    logger.info(f"Page {page_num}: found {len(page_listings)} listings (total: {len(all_listings)})")

        except PlaywrightTimeout:
            logger.error("Timeout loading VW used cars page")
        except Exception as e:
            logger.error(f"Error scraping VW listings: {e}")
        finally:
            browser.close()

    # Apply filters
    model_filters = Config.get_model_filters()
    if model_filters:
        all_listings = [
            l for l in all_listings
            if l.model.lower() in model_filters or any(m in l.title.lower() for m in model_filters)
        ]

    if Config.VW_MAX_PRICE:
        max_price = int(Config.VW_MAX_PRICE)
        all_listings = [l for l in all_listings if l.price <= max_price]

    if Config.VW_MAX_MILEAGE:
        max_mileage = int(Config.VW_MAX_MILEAGE)
        all_listings = [l for l in all_listings if l.mileage <= max_mileage]

    if Config.VW_MIN_YEAR:
        min_year = int(Config.VW_MIN_YEAR)
        all_listings = [l for l in all_listings if l.year >= min_year]

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for l in all_listings:
        if l.url and l.url not in seen_urls:
            seen_urls.add(l.url)
            unique.append(l)
        elif not l.url:
            unique.append(l)
    all_listings = unique

    logger.info(f"Scraping complete: {len(all_listings)} listings after filtering")
    return all_listings
