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


def _extract_mileage(text: str) -> int:
    """Extract mileage from text like '12,345 miles' or '12345 miles'."""
    match = re.search(r"([\d,]+)\s*miles?\b", text, re.IGNORECASE)
    if match:
        return _extract_int(match.group(1))
    return 0


def _extract_price(text: str) -> int:
    """Extract the main GBP price from text (ignoring monthly payments)."""
    # Find all £ prices
    prices = re.findall(r"£([\d,]+)", text)
    for p in prices:
        val = _extract_int(p)
        if val > 500:  # Skip monthly payment amounts
            return val
    return 0


# Ordered longest-first so "id.7 tourer" matches before "id.7"
VW_MODELS = [
    "id. buzz", "id.7 tourer", "id.3", "id.4", "id.5", "id.7",
    "golf gti", "golf gte", "golf gtd", "golf r", "golf estate",
    "golf sv", "golf",
    "t-roc cabriolet", "t-roc", "t-cross",
    "tiguan allspace", "tiguan", "touareg", "touran", "tayron",
    "polo", "taigo", "passat estate", "passat", "arteon",
    "multivan", "up!", "e-up!", "e-golf", "sharan", "caravelle",
]


def _detect_model(text: str) -> str:
    """Detect VW model name from text."""
    text_lower = text.lower()
    for m in VW_MODELS:
        if m in text_lower:
            # Preserve canonical casing for ID models
            if m.startswith("id."):
                return "ID." + m[3:].upper().strip()
            if m == "id. buzz":
                return "ID. Buzz"
            return m.title()
    return ""


def _try_intercept_api(page: Page, url: str) -> list[dict]:
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
    logger.info(f"Navigating to: {url}")
    page.goto(url, wait_until="networkidle", timeout=45000)
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

    # First, gather diagnostics about what's actually on the page
    diagnostics = page.evaluate("""() => {
        const allLinks = document.querySelectorAll('a[href]');
        const hrefSamples = [];
        const hrefPatterns = {};
        for (const a of allLinks) {
            const href = a.getAttribute('href') || '';
            // Collect first 30 unique href samples
            if (hrefSamples.length < 30 && href.length > 1) {
                hrefSamples.push(href);
            }
            // Count href patterns
            const parts = href.split('/').filter(Boolean);
            const key = parts.length > 1 ? '/' + parts[0] + '/' + parts[1] : href.substring(0, 50);
            hrefPatterns[key] = (hrefPatterns[key] || 0) + 1;
        }
        // Count elements with £ sign
        const body = document.body.innerText || '';
        const priceCount = (body.match(/£[\d,]+/g) || []).length;
        const pageTextLen = body.length;

        return {
            totalLinks: allLinks.length,
            hrefSamples,
            hrefPatterns,
            priceCount,
            pageTextLen,
            title: document.title,
        };
    }""")

    logger.info(f"Page diagnostics: title='{diagnostics.get('title', '')}', "
                f"links={diagnostics.get('totalLinks', 0)}, "
                f"prices on page={diagnostics.get('priceCount', 0)}, "
                f"page text length={diagnostics.get('pageTextLen', 0)}")
    for pattern, count in sorted(diagnostics.get("hrefPatterns", {}).items(), key=lambda x: -x[1])[:15]:
        logger.debug(f"  Link pattern: {pattern} ({count}x)")
    for href in diagnostics.get("hrefSamples", [])[:10]:
        logger.debug(f"  Sample href: {href}")

    raw_listings = page.evaluate("""() => {
        const results = [];
        const processedTexts = new Set();

        // ── Strategy 1: Link-based ──
        // Find all links that point to individual vehicle detail pages.
        const vehicleLinks = document.querySelectorAll(
            'a[href*="/vehicle/"], a[href*="/car/"], a[href*="/detail/"], '
          + 'a[href*="/listing/"], a[href*="/used-car/"], a[href*="/approved/"]'
        );

        for (const link of vehicleLinks) {
            const href = link.getAttribute('href') || '';
            if (href.includes('vehicle_search') || href.includes('vehicle-search')) continue;

            let card = link;
            const maxTextLen = 3000;
            for (let i = 0; i < 6; i++) {
                if (!card.parentElement) break;
                const parent = card.parentElement;
                const parentText = parent.innerText || '';
                if (parentText.length > maxTextLen) break;
                card = parent;
            }

            const text = card.innerText || '';
            const fromPriceCount = (text.match(/From\\s+£/gi) || []).length;
            if (fromPriceCount > 2) continue;
            if (!text.includes('£')) continue;
            if (text.length < 30 || text.length > maxTextLen) continue;

            // Dedup by text content (first 200 chars)
            const textKey = text.substring(0, 200);
            if (processedTexts.has(textKey)) continue;
            processedTexts.add(textKey);

            const img = card.querySelector('img');
            const imgSrc = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
            results.push({ text, href, imgSrc, strategy: 1 });
        }

        if (results.length > 0) return results;

        // ── Strategy 2: Broad element search ──
        // If link-based strategy found nothing, look for card-like elements
        // that contain both a price (£) and car-related keywords.
        const carKeywords = /\\b(volkswagen|vw|id\\.3|id\\.4|id\\.5|id\\.7|id\\. buzz|golf|polo|tiguan|t-roc|t-cross|touareg|touran|tayron|taigo|passat|arteon|up!|e-up|multivan)\\b/i;
        const pricePattern = /£[\\d,]+/;

        // Try common card/tile selectors first
        const cardSelectors = [
            '[class*="vehicle-card"]', '[class*="car-card"]', '[class*="listing-card"]',
            '[class*="result-card"]', '[class*="product-card"]', '[class*="tile"]',
            '[data-testid*="vehicle"]', '[data-testid*="listing"]', '[data-testid*="car"]',
            'article', '[role="listitem"]',
        ];

        let candidateElements = [];
        for (const sel of cardSelectors) {
            const els = document.querySelectorAll(sel);
            if (els.length > 0) {
                candidateElements = Array.from(els);
                break;
            }
        }

        // If no card selectors matched, search all container elements
        if (candidateElements.length === 0) {
            candidateElements = Array.from(
                document.querySelectorAll('div, section, li, article')
            );
        }

        for (const el of candidateElements) {
            const text = el.innerText || '';
            if (text.length < 30 || text.length > 2000) continue;

            // Must contain a price
            if (!pricePattern.test(text)) continue;

            // Must contain a car-related keyword
            if (!carKeywords.test(text)) continue;

            // Skip navigation/model-picker elements
            const fromPriceCount = (text.match(/From\\s+£/gi) || []).length;
            if (fromPriceCount > 2) continue;

            // Skip if this contains multiple "miles" entries (likely a container of multiple cards)
            const milesCount = (text.match(/\\bmiles\\b/gi) || []).length;
            if (milesCount > 3) continue;

            // Dedup by text content (first 200 chars)
            const textKey = text.substring(0, 200);
            if (processedTexts.has(textKey)) continue;
            processedTexts.add(textKey);

            // Try to find a link inside
            const linkEl = el.querySelector('a[href]');
            const href = linkEl ? (linkEl.getAttribute('href') || '') : '';

            const img = el.querySelector('img');
            const imgSrc = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';

            results.push({ text, href, imgSrc, strategy: 2 });
        }

        return results;
    }""")

    strategies_used = set(r.get("strategy", 0) for r in raw_listings)
    logger.info(f"DOM extraction found {len(raw_listings)} potential vehicle cards "
                f"(strategies: {strategies_used})")

    listings = []
    for raw in raw_listings:
        text = raw.get("text", "")
        href = raw.get("href", "")
        img_src = raw.get("imgSrc", "")

        if href and not href.startswith("http"):
            href = Config.VW_BASE_URL + href

        # Extract structured data using targeted regex (not _extract_int on whole lines)
        price = _extract_price(text)
        mileage = _extract_mileage(text)
        year = _extract_year(text)

        fuel_type = ""
        transmission = ""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            line_lower = line.lower()
            if not fuel_type and any(f in line_lower for f in ["petrol", "diesel", "electric", "hybrid"]):
                fuel_type = line
            if not transmission and any(t in line_lower for t in ["manual", "automatic", "dsg", "single speed"]):
                transmission = line

        # Title: find a line mentioning VW or a model name
        title = ""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if "volkswagen" in line.lower() or _detect_model(line):
                title = line
                break
        if not title:
            # Use the first non-empty line
            for line in text.split("\n"):
                line = line.strip()
                if line and not line.startswith("£"):
                    title = line
                    break

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


def _scroll_for_lazy_content(page: Page) -> None:
    """Scroll the page to trigger lazy-loaded content, then scroll back to top."""
    page.evaluate("""() => {
        return new Promise(resolve => {
            let totalHeight = 0;
            const distance = 500;
            const timer = setInterval(() => {
                window.scrollBy(0, distance);
                totalHeight += distance;
                if (totalHeight >= document.body.scrollHeight || totalHeight > 15000) {
                    clearInterval(timer);
                    window.scrollTo(0, 0);
                    resolve();
                }
            }, 200);
        });
    }""")
    page.wait_for_timeout(2000)


def _try_load_more(page: Page) -> bool:
    """Try to click 'Load More' / 'Show More' buttons or paginate. Returns True if more loaded."""
    # Try "Load More" / "Show More" buttons (common on modern sites)
    load_more_selectors = [
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "button:has-text('load more')",
        "button:has-text('show more')",
        "a:has-text('Load more')",
        "a:has-text('Show more')",
        "[class*='load-more']",
        "[class*='show-more']",
        "[data-testid*='load-more']",
    ]
    for sel in load_more_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info(f"Found 'load more' button: {sel}")
                btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue

    # Try pagination "Next" buttons
    next_selectors = [
        "a[aria-label='Next']",
        "button[aria-label='Next']",
        "a:has-text('Next')",
        "button:has-text('Next')",
        "[class*='pagination'] a:last-child",
        "a[rel='next']",
        "button:has-text('>')",
        "[class*='next-page']",
        "a[class*='next']",
    ]
    for sel in next_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info(f"Found 'next' button: {sel}")
                btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue

    return False


def _scrape_single_url(page: Page, url: str, cookie_handled: bool) -> tuple[list[VWListing], list[dict], bool]:
    """Scrape a single URL and return (listings, api_responses, cookie_handled)."""
    # Step 1: Load page and intercept JSON responses
    api_responses = _try_intercept_api(page, url)
    logger.info(f"Intercepted {len(api_responses)} JSON responses")
    for resp in api_responses:
        resp_url = resp.get("url", "")
        logger.info(f"  API: {resp_url[:120]}")

    # Step 2: Try to parse vehicle data from API responses
    listings = _parse_listings_from_api(api_responses)
    if listings:
        logger.info(f"Parsed {len(listings)} listings from API data")

    # Step 3: Handle cookie consent (only on first URL)
    if not cookie_handled:
        _handle_cookie_consent(page)
        _handle_postcode_entry(page)
        cookie_handled = True
    page.wait_for_timeout(3000)

    # Step 4: If API didn't have vehicle data, extract from DOM
    if not listings:
        logger.info("No vehicle data in API responses, extracting from rendered DOM...")
        _scroll_for_lazy_content(page)
        listings = _parse_listings_from_dom(page)
        logger.info(f"Extracted {len(listings)} listings from DOM (page 1)")

    # Step 5: Try to load more results (pagination / infinite scroll / load-more)
    if listings:
        for page_num in range(2, Config.VW_MAX_PAGES + 1):
            prev_count = len(listings)

            if not _try_load_more(page):
                logger.info(f"No more pages/load-more buttons found after page {page_num - 1}")
                break

            _scroll_for_lazy_content(page)
            page_listings = _parse_listings_from_dom(page)

            if not page_listings:
                logger.info(f"Page {page_num}: no new listings found, stopping")
                break

            # Deduplicate against existing listings
            existing_texts = {l.title[:50] for l in listings}
            new_listings = [l for l in page_listings if l.title[:50] not in existing_texts]

            if not new_listings:
                logger.info(f"Page {page_num}: all listings are duplicates, stopping")
                break

            listings.extend(new_listings)
            logger.info(f"Page {page_num}: +{len(new_listings)} new listings (total: {len(listings)})")

    return listings, api_responses, cookie_handled


def scrape_vw_listings() -> list[VWListing]:
    """Main entry point: scrape VW used car listings and return structured data."""
    logger.info("Starting VW used car scraper")

    # Log active filters
    model_filters = Config.get_model_filters()
    search_urls = Config.get_search_urls()
    logger.info(f"Model filters: {model_filters or 'none (all models)'}")
    logger.info(f"Search URLs to try: {search_urls}")

    all_listings: list[VWListing] = []
    all_api_responses: list[dict] = []

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
        cookie_handled = False

        try:
            for url in search_urls:
                # If we already have listings from a model-specific URL,
                # skip the all-models fallback
                if all_listings and "all-models" in url:
                    logger.info(f"Skipping all-models URL (already have {len(all_listings)} listings)")
                    break

                logger.info(f"--- Scraping: {url} ---")
                listings, api_responses, cookie_handled = _scrape_single_url(
                    page, url, cookie_handled
                )
                all_listings.extend(listings)
                all_api_responses.extend(api_responses)
                logger.info(f"Got {len(listings)} listings from {url}")

            # Save debug artifacts from last page visited
            _save_debug(all_api_responses, page)

        except PlaywrightTimeout:
            logger.error("Timeout loading VW used cars page")
            _save_debug(all_api_responses, page)
        except Exception as e:
            logger.error(f"Error scraping VW listings: {e}", exc_info=True)
            _save_debug(all_api_responses, page)
        finally:
            browser.close()

    logger.info(f"Total raw listings before filtering: {len(all_listings)}")

    # Log model distribution before filtering
    model_counts: dict[str, int] = {}
    for l in all_listings:
        m = l.model or "(unknown)"
        model_counts[m] = model_counts.get(m, 0) + 1
    logger.info(f"Models found: {model_counts}")

    # Apply model filter (safety net - model-specific URLs should already filter)
    if model_filters:
        before = len(all_listings)
        all_listings = [
            l for l in all_listings
            if l.model.lower() in model_filters
            or any(m in l.title.lower() for m in model_filters)
            or any(m in l.model.lower() for m in model_filters)
        ]
        logger.info(f"Model filter: {before} → {len(all_listings)} listings "
                     f"(kept models matching {model_filters})")

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

    logger.info(f"Scraping complete: {len(all_listings)} listings after filtering and dedup")
    return all_listings
