"""Scrape used car listings from usedcars.volkswagen.co.uk using Playwright."""

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

from src.config import Config

DEBUG_DIR = Path("debug")

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


def _detect_model(text: str) -> str:
    """Detect VW model name from text."""
    text_lower = text.lower()
    models = [
        "id. buzz", "id.7 tourer", "id.3", "id.4", "id.5", "id.7",
        "golf gti", "golf gte", "golf gtd", "golf r", "golf estate",
        "golf sv", "golf",
        "t-roc cabriolet", "t-roc", "t-cross",
        "tiguan allspace", "tiguan", "touareg", "touran", "tayron",
        "polo", "taigo", "passat estate", "passat", "arteon",
        "multivan", "up!", "e-up!", "e-golf", "sharan", "caravelle",
    ]
    for m in models:
        if m in text_lower:
            return m.title()
    return ""


def _try_intercept_api(page: Page) -> list[dict]:
    """Capture ALL JSON responses during page load."""
    captured = []

    def handle_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                data = response.json()
                captured.append({"url": response.url, "data": data})
        except Exception:
            pass

    page.on("response", handle_response)
    page.goto(Config.VW_SEARCH_URL, wait_until="networkidle", timeout=45000)
    page.remove_listener("response", handle_response)
    return captured


def _parse_listings_from_api(api_responses: list[dict]) -> list[VWListing]:
    """Try to find and parse vehicle listing data from any intercepted API response."""
    listings = []

    for resp in api_responses:
        url = resp.get("url", "")
        data = resp.get("data")

        # Skip the solr-response.json facet endpoint - it only has filter metadata
        if "solr-response" in url:
            continue

        # Recursively search for arrays of objects that look like vehicle listings
        candidates = _find_vehicle_arrays(data)
        for vehicles in candidates:
            for v in vehicles:
                listing = _try_parse_vehicle_dict(v)
                if listing:
                    listings.append(listing)

    return listings


def _find_vehicle_arrays(data, depth=0) -> list[list[dict]]:
    """Recursively search JSON data for arrays that look like vehicle listings."""
    if depth > 5:
        return []
    results = []
    if isinstance(data, list) and len(data) > 0:
        # Check if this looks like a list of vehicle objects
        if isinstance(data[0], dict) and any(
            k in data[0] for k in [
                "price", "Price", "PRICE", "retailPrice", "cashPrice",
                "PRICE_RETAIL_CUR", "mileage", "MILEAGE", "model", "MODEL",
                "title", "headline", "vehicleId", "vin", "VIN",
            ]
        ):
            results.append(data)
    if isinstance(data, dict):
        for val in data.values():
            results.extend(_find_vehicle_arrays(val, depth + 1))
    return results


def _try_parse_vehicle_dict(v: dict) -> VWListing | None:
    """Try to parse a dict as a vehicle listing. Returns None if it doesn't look like one."""
    if not isinstance(v, dict):
        return None

    # Try various key patterns for price
    price = 0
    for k in ["price", "Price", "PRICE", "retailPrice", "cashPrice",
              "PRICE_RETAIL_CUR", "PRICE_B2B_CUR", "salePrice"]:
        val = v.get(k)
        if val is not None:
            price = _extract_int(str(val))
            if price > 0:
                break

    if price == 0:
        return None

    title = ""
    for k in ["title", "name", "headline", "TITLE", "displayName", "vehicleTitle"]:
        if v.get(k):
            title = str(v[k])
            break

    mileage = 0
    for k in ["mileage", "MILEAGE", "odometer", "MILEAGE_MIL_INT", "miles"]:
        val = v.get(k)
        if val is not None:
            mileage = _extract_int(str(val))
            if mileage > 0:
                break

    year = 0
    for k in ["year", "modelYear", "registrationYear", "INITIAL_REGISTRATION_DTE"]:
        val = v.get(k)
        if val is not None:
            year = _extract_int(str(val))
            if 2000 <= year <= 2030:
                break
            year = 0

    fuel = ""
    for k in ["fuelType", "fuel", "FUEL_TYPE_LST"]:
        if v.get(k):
            fuel = str(v[k])
            break

    transmission = ""
    for k in ["transmission", "gearbox", "TRANSMISSION_LST"]:
        if v.get(k):
            transmission = str(v[k])
            break

    model = ""
    for k in ["model", "modelName", "MODEL_TYPE_LST"]:
        if v.get(k):
            model = str(v[k])
            break
    if not model and title:
        model = _detect_model(title)

    trim = ""
    for k in ["trim", "trimLevel", "derivative", "TRIM_STR"]:
        if v.get(k):
            trim = str(v[k])
            break

    dealer_name = ""
    dealer = v.get("dealer")
    if isinstance(dealer, dict):
        dealer_name = dealer.get("name", "")
    elif isinstance(dealer, str):
        dealer_name = dealer
    for k in ["dealerName", "DEALER"]:
        if not dealer_name and v.get(k):
            dealer_name = str(v[k])

    url = ""
    for k in ["url", "detailUrl", "link", "href"]:
        if v.get(k):
            url = str(v[k])
            break

    img = ""
    for k in ["imageUrl", "image", "mainImage", "thumbnailUrl"]:
        if v.get(k):
            img = str(v[k])
            break

    return VWListing(
        title=title,
        price=price,
        mileage=mileage,
        year=year,
        fuel_type=fuel,
        transmission=transmission,
        model=model,
        trim=trim,
        registration=v.get("registration", v.get("vrm", "")),
        dealer=dealer_name,
        location=v.get("location", v.get("dealerLocation", "")),
        url=url,
        image_url=img,
    )


def _parse_listings_from_dom(page: Page) -> list[VWListing]:
    """Extract vehicle listings by running JavaScript in the browser to find car cards."""
    # Use JavaScript to find all elements containing a £ price and a vehicle link
    raw_listings = page.evaluate("""() => {
        const results = [];

        // Strategy 1: Find all links that point to vehicle detail pages
        const vehicleLinks = document.querySelectorAll('a[href*="/vehicle/"], a[href*="/car/"], a[href*="/detail/"]');
        const processed = new Set();

        for (const link of vehicleLinks) {
            // Walk up to find the card container (usually 2-4 levels up)
            let card = link;
            for (let i = 0; i < 5; i++) {
                if (card.parentElement) card = card.parentElement;
            }

            // Skip if we've already processed this card
            const cardId = card.getAttribute('data-id') || card.innerHTML.substring(0, 100);
            if (processed.has(cardId)) continue;
            processed.add(cardId);

            const text = card.innerText || '';
            const href = link.getAttribute('href') || '';
            const img = card.querySelector('img');
            const imgSrc = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';

            // Only include if it has a price
            if (text.includes('£')) {
                results.push({ text, href, imgSrc });
            }
        }

        // Strategy 2: If strategy 1 found nothing, find all elements with £ prices
        if (results.length === 0) {
            // Find elements that contain price-like text
            const allElements = document.querySelectorAll('div, article, section, li');
            for (const el of allElements) {
                const text = el.innerText || '';
                // Look for elements that have a price AND car-related text
                if (text.includes('£') && text.length > 50 && text.length < 2000) {
                    const hasCarInfo = /(?:mile|petrol|diesel|electric|hybrid|manual|automatic)/i.test(text);
                    const hasModel = /(?:golf|polo|tiguan|t-roc|t-cross|id\\.3|id\\.4|id\\.5|id\\.7|touareg|passat|arteon|taigo|tayron)/i.test(text);
                    if (hasCarInfo || hasModel) {
                        const link = el.querySelector('a[href*="/vehicle/"], a[href*="/car/"], a');
                        const href = link ? link.getAttribute('href') || '' : '';
                        const img = el.querySelector('img');
                        const imgSrc = img ? (img.getAttribute('src') || '') : '';

                        const elId = text.substring(0, 100);
                        if (!processed.has(elId)) {
                            processed.add(elId);
                            results.push({ text, href, imgSrc });
                        }
                    }
                }
            }
        }

        return results;
    }""")

    logger.info(f"DOM extraction found {len(raw_listings)} potential vehicle cards")

    listings = []
    for raw in raw_listings:
        text = raw.get("text", "")
        href = raw.get("href", "")
        img_src = raw.get("imgSrc", "")

        if href and not href.startswith("http"):
            href = Config.VW_BASE_URL + href

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        title = ""
        price = 0
        mileage = 0
        year = 0
        fuel_type = ""
        transmission = ""

        for line in lines:
            line_lower = line.lower()
            if "£" in line and not price:
                # Extract first price (main price, not monthly)
                price_match = re.search(r"£[\d,]+", line)
                if price_match:
                    candidate = _extract_int(price_match.group())
                    if candidate > 500:  # Ignore monthly payment amounts
                        price = candidate
            if "mile" in line_lower and not mileage:
                mileage = _extract_int(line)
            if not year:
                year = _extract_year(line)
            if any(f in line_lower for f in ["petrol", "diesel", "electric", "hybrid"]):
                fuel_type = line.strip()
            if any(t in line_lower for t in ["manual", "automatic", "dsg", "single speed"]):
                transmission = line.strip()

        # Title: use first meaningful line or detect from content
        for line in lines:
            if "volkswagen" in line.lower() or any(
                m in line.lower() for m in ["golf", "polo", "tiguan", "t-roc", "id."]
            ):
                title = line.strip()
                break
        if not title and lines:
            title = lines[0]

        model = _detect_model(title) or _detect_model(text)

        if price > 500:
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
                image_url=img_src,
            ))

    return listings


def _handle_cookie_consent(page: Page) -> None:
    """Dismiss cookie consent banners if present."""
    consent_selectors = [
        "#onetrust-accept-btn-handler",
        "button[id*='accept']",
        "button[class*='accept']",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
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
                submit = page.query_selector(
                    "button[type='submit'], button:has-text('Search'), button:has-text('Go')"
                )
                if submit and submit.is_visible():
                    submit.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                return
        except Exception:
            continue


def _save_debug(api_responses: list[dict], page: Page | None) -> None:
    """Save debug artifacts."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)

        # Save API responses
        for i, resp in enumerate(api_responses):
            url = resp.get("url", "")
            data = resp.get("data")
            path = DEBUG_DIR / f"api_response_{i}.json"
            debug_info = {"url": url}
            if isinstance(data, dict):
                debug_info["top_level_keys"] = list(data.keys())
                # Only save full data for smaller responses
                data_str = json.dumps(data, default=str)
                if len(data_str) < 50000:
                    debug_info["raw_data"] = data
                else:
                    debug_info["note"] = f"Data too large ({len(data_str)} chars), truncated"
                    debug_info["sample_keys"] = {k: type(v).__name__ for k, v in data.items()}
            else:
                debug_info["raw_data"] = data
            path.write_text(json.dumps(debug_info, indent=2, default=str))
            logger.info(f"Debug: saved API response {i} ({url[:80]})")

        # Save page screenshot and HTML
        if page:
            page.screenshot(path=str(DEBUG_DIR / "page_screenshot.png"), full_page=True)
            html = page.content()
            (DEBUG_DIR / "page_source.html").write_text(html)
            logger.info(f"Debug: saved screenshot and HTML ({len(html)} chars)")

    except Exception as e:
        logger.debug(f"Failed to save debug data: {e}")


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
            # Step 1: Load page and intercept ALL JSON responses
            logger.info("Loading search page and intercepting API calls...")
            api_responses = _try_intercept_api(page)
            logger.info(f"Intercepted {len(api_responses)} JSON responses")

            # Log what we captured
            for resp in api_responses:
                url = resp.get("url", "")
                logger.info(f"  API: {url[:120]}")

            # Step 2: Try to parse vehicle data from API responses
            all_listings = _parse_listings_from_api(api_responses)
            if all_listings:
                logger.info(f"Parsed {len(all_listings)} listings from API data")

            # Step 3: Handle cookie consent and postcode
            _handle_cookie_consent(page)
            _handle_postcode_entry(page)
            page.wait_for_timeout(3000)

            # Step 4: If API didn't have vehicle data, extract from DOM
            if not all_listings:
                logger.info("No vehicle data in API responses, extracting from rendered DOM...")
                all_listings = _parse_listings_from_dom(page)
                logger.info(f"Extracted {len(all_listings)} listings from DOM")

            # Step 5: Handle pagination
            if all_listings:
                for page_num in range(2, Config.VW_MAX_PAGES + 1):
                    next_selectors = [
                        "a[aria-label='Next']",
                        "button[aria-label='Next']",
                        "a:has-text('Next')",
                        "button:has-text('Next')",
                        "[class*='pagination'] a:last-child",
                        "a[rel='next']",
                        "button:has-text('>')",
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

                    page_listings = _parse_listings_from_dom(page)
                    if not page_listings:
                        break
                    all_listings.extend(page_listings)
                    logger.info(f"Page {page_num}: {len(page_listings)} listings (total: {len(all_listings)})")

            # Save debug artifacts
            _save_debug(api_responses, page)

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
