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


def _try_intercept_api(page: Page, url: str) -> tuple[list[dict], list[str], list[dict]]:
    """Capture JSON responses, XHR HTML, and a full network log during page load.

    Returns (json_responses, xhr_html_bodies, network_log).
    Uses a two-phase approach: first load with domcontentloaded (fast),
    then wait for networkidle with a shorter timeout (best-effort).
    Retries once on timeout.
    """
    json_responses: list[dict] = []
    xhr_html_bodies: list[str] = []
    network_log: list[dict] = []

    def handle_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            status = response.status
            resp_url = response.url
            entry = {
                "url": resp_url,
                "status": status,
                "content_type": content_type,
            }
            try:
                entry["size"] = len(response.body())
            except Exception:
                entry["size"] = -1
            network_log.append(entry)

            if "application/json" in content_type:
                data = response.json()
                json_responses.append({"url": resp_url, "data": data})

            # Capture XHR HTML from the results endpoint (VW loads listings via AJAX)
            if "xhr-results" in resp_url and "text/html" in content_type and status == 200:
                try:
                    xhr_html_bodies.append(response.text())
                    logger.info(f"Captured XHR HTML from {resp_url[:100]} ({entry.get('size', '?')} bytes)")
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", handle_response)

    # Try up to 2 attempts with increasing timeout
    last_error = None
    for attempt in range(2):
        try:
            timeout = 45000 if attempt == 0 else 60000
            logger.info(f"Navigating to: {url} (attempt {attempt + 1})")
            # Phase 1: Wait for DOM to be ready (more reliable than networkidle)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            # Phase 2: Best-effort wait for network to settle
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                logger.warning("networkidle wait timed out — continuing with partially loaded page")
            last_error = None
            break
        except PlaywrightTimeout as e:
            last_error = e
            if attempt == 0:
                logger.warning(f"Page load timed out, retrying... ({e})")
                page.wait_for_timeout(2000)

    if last_error:
        logger.error(f"Page load failed after retries: {last_error}")
        page.remove_listener("response", handle_response)
        raise last_error  # noqa: will be caught by _scrape_single_url caller

    # Check for redirect
    final_url = page.url
    if final_url != url:
        logger.warning(f"URL redirected: {url} → {final_url}")

    page.remove_listener("response", handle_response)
    return json_responses, xhr_html_bodies, network_log


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


def _parse_listings_from_xhr_html(page: Page, xhr_html_bodies: list[str]) -> list[VWListing]:
    """Parse vehicle listings from XHR HTML responses using the browser's DOMParser.

    The VW site loads its search results via an AJAX call to /xhr-results/
    which returns HTML (not JSON). This function parses that HTML.
    """
    all_listings: list[VWListing] = []

    for html_body in xhr_html_bodies:
        if not html_body or len(html_body) < 500:
            continue

        raw_listings = page.evaluate(r"""(html) => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const results = [];
            const processedHrefs = new Set();

            // Find all links to individual vehicle detail pages.
            // VW pattern: /en/vehicle_search/volkswagen/{model}/{description}-{id}
            const allLinks = doc.querySelectorAll('a[href*="/vehicle_search/volkswagen/"]');

            for (const link of allLinks) {
                const href = link.getAttribute('href') || '';
                const path = href.split('?')[0];
                const segments = path.split('/').filter(Boolean);

                // Detail pages have 5+ segments:
                //   en / vehicle_search / volkswagen / model / detail-id
                // Search/filter pages have 4:
                //   en / vehicle_search / volkswagen / model
                if (segments.length < 5) continue;

                // Skip pagination links (page1, page2, ...)
                if (/^page\d+$/.test(segments[segments.length - 1])) continue;

                // Deduplicate by path
                if (processedHrefs.has(path)) continue;
                processedHrefs.add(path);

                // Walk up to find the card container
                let card = link;
                for (let i = 0; i < 8; i++) {
                    if (!card.parentElement) break;
                    const parent = card.parentElement;
                    const parentText = parent.textContent || '';
                    if (parentText.length > 4000) break;
                    card = parent;
                }

                // Use textContent (not innerText — works on parsed docs and
                // includes text from CSS-hidden elements like prices)
                const text = card.textContent || '';
                if (text.length < 20) continue;

                const img = card.querySelector('img');
                const imgSrc = img
                    ? (img.getAttribute('src') || img.getAttribute('data-src')
                       || img.getAttribute('data-lazy') || '')
                    : '';

                results.push({ text, href: path, imgSrc });
            }

            return results;
        }""", html_body)

        logger.info(f"XHR HTML parser found {len(raw_listings)} vehicle cards")

        for raw in raw_listings:
            text = raw.get("text", "")
            href = raw.get("href", "")
            img_src = raw.get("imgSrc", "")

            if href and not href.startswith("http"):
                href = Config.VW_BASE_URL + href

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

            title = ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "volkswagen" in line.lower() or _detect_model(line):
                    title = line
                    break
            if not title:
                for line in text.split("\n"):
                    line = line.strip()
                    if line and len(line) > 5:
                        title = line
                        break

            model = _detect_model(title) or _detect_model(text)

            if price > 500:
                all_listings.append(VWListing(
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

    logger.info(f"XHR HTML total: {len(all_listings)} listings with valid prices")
    return all_listings


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
    diagnostics = page.evaluate(r"""() => {
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
        // Count elements with £ sign (check both innerText and textContent)
        const body = document.body.innerText || '';
        const bodyTC = document.body.textContent || '';
        const priceCount = (body.match(/£[\d,]+/g) || []).length;
        const priceCountTC = (bodyTC.match(/£[\d,]+/g) || []).length;
        const pageTextLen = body.length;

        return {
            totalLinks: allLinks.length,
            hrefSamples,
            hrefPatterns,
            priceCount,
            priceCountTC,
            pageTextLen,
            title: document.title,
        };
    }""")

    logger.info(f"Page diagnostics: title='{diagnostics.get('title', '')}', "
                f"links={diagnostics.get('totalLinks', 0)}, "
                f"prices(innerText)={diagnostics.get('priceCount', 0)}, "
                f"prices(textContent)={diagnostics.get('priceCountTC', 0)}, "
                f"page text length={diagnostics.get('pageTextLen', 0)}")
    for pattern, count in sorted(diagnostics.get("hrefPatterns", {}).items(), key=lambda x: -x[1])[:15]:
        logger.debug(f"  Link pattern: {pattern} ({count}x)")
    for href in diagnostics.get("hrefSamples", [])[:10]:
        logger.debug(f"  Sample href: {href}")

    raw_listings = page.evaluate(r"""() => {
        const results = [];
        const processedHrefs = new Set();

        // ── Strategy 0: VW-specific — vehicle detail page links ──
        // VW uses: /en/vehicle_search/volkswagen/{model}/{description}-{id}
        // Detail pages have 5+ path segments; search pages have 4.
        const vwLinks = document.querySelectorAll(
            'a[href*="/vehicle_search/volkswagen/"]'
        );

        for (const link of vwLinks) {
            const href = link.getAttribute('href') || '';
            const path = href.split('?')[0];
            const segments = path.split('/').filter(Boolean);

            // Detail pages: en/vehicle_search/volkswagen/model/detail = 5+
            if (segments.length < 5) continue;
            // Skip pagination (page1, page2, ...)
            if (/^page\d+$/.test(segments[segments.length - 1])) continue;
            // Skip finance section links
            if (href.includes('#vdpSection')) continue;

            if (processedHrefs.has(path)) continue;
            processedHrefs.add(path);

            // Walk up to find the card container
            let card = link;
            for (let i = 0; i < 8; i++) {
                if (!card.parentElement) break;
                const parent = card.parentElement;
                if ((parent.textContent || '').length > 4000) break;
                card = parent;
            }

            // Use textContent (works even when prices are CSS-rendered)
            const text = card.textContent || '';
            if (text.length < 20) continue;

            const img = card.querySelector('img');
            const imgSrc = img
                ? (img.getAttribute('src') || img.getAttribute('data-src')
                   || img.getAttribute('data-lazy') || '')
                : '';
            results.push({ text, href: path, imgSrc, strategy: 0 });
        }

        if (results.length > 0) return results;

        // ── Strategy 1: Generic link-based ──
        // Find links that point to individual vehicle detail pages on non-VW sites.
        const vehicleLinks = document.querySelectorAll(
            'a[href*="/vehicle/"], a[href*="/car/"], a[href*="/detail/"], '
          + 'a[href*="/listing/"], a[href*="/used-car/"], a[href*="/approved/"]'
        );

        const processedTexts = new Set();
        for (const link of vehicleLinks) {
            const href = link.getAttribute('href') || '';
            if (href.includes('vehicle_search') || href.includes('vehicle-search')) continue;

            let card = link;
            const maxTextLen = 3000;
            for (let i = 0; i < 6; i++) {
                if (!card.parentElement) break;
                const parent = card.parentElement;
                const parentText = parent.textContent || '';
                if (parentText.length > maxTextLen) break;
                card = parent;
            }

            const text = card.textContent || '';
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
        // Look for card-like elements that contain car-related keywords.
        const carKeywords = /\b(volkswagen|vw|id[.\-]\s?[34567]|id\. buzz|golf|polo|tiguan|t-roc|t-cross|touareg|touran|tayron|taigo|passat|arteon|up!|e-up|multivan)\b/i;
        // Look for prices with £ sign OR standalone large numbers (in case £ is CSS-rendered)
        const pricePattern = /(?:£[\d,]+|[\d,]{4,}\s*(?:price|£))/i;

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
            const text = el.textContent || '';
            if (text.length < 30 || text.length > 2000) continue;

            // Must contain a car-related keyword
            if (!carKeywords.test(text)) continue;

            // Skip if this contains multiple "miles" entries (likely a container of multiple cards)
            const milesCount = (text.match(/\bmiles\b/gi) || []).length;
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


def _url_to_label(url: str) -> str:
    """Convert a URL to a safe, short label for filenames."""
    # Extract the last meaningful path segment
    path = url.split("?")[0].rstrip("/")
    slug = path.split("/")[-1] if "/" in path else "page"
    # Sanitize for filename
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return slug[:60]


def _save_debug_for_url(
    url: str,
    api_responses: list[dict],
    network_log: list[dict],
    console_messages: list[dict],
    page: Page | None,
) -> None:
    """Save comprehensive debug artifacts for a single URL.

    Creates a subfolder per URL under debug/ with:
    - screenshot.png: full-page screenshot
    - page_source.html: complete HTML
    - api_responses.json: all intercepted JSON API responses
    - network_log.json: all HTTP requests/responses with status & content-type
    - console_log.json: browser console messages (errors, warnings, info)
    - dom_analysis.json: detailed page structure analysis for debugging selectors
    """
    label = _url_to_label(url)
    url_dir = DEBUG_DIR / label
    try:
        url_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save API responses (full content for diagnosis)
        api_dump = []
        for resp in api_responses:
            resp_url = resp.get("url", "")
            data = resp.get("data")
            entry = {"url": resp_url}
            if isinstance(data, dict):
                entry["top_level_keys"] = list(data.keys())
                data_str = json.dumps(data, default=str)
                if len(data_str) < 200000:
                    entry["data"] = data
                else:
                    entry["note"] = f"Data too large ({len(data_str)} chars)"
                    entry["sample_keys"] = {k: type(v).__name__ for k, v in data.items()}
            elif isinstance(data, list):
                entry["array_length"] = len(data)
                data_str = json.dumps(data, default=str)
                if len(data_str) < 200000:
                    entry["data"] = data
                else:
                    entry["note"] = f"Array too large ({len(data_str)} chars)"
                    if data:
                        entry["first_item_sample"] = data[0] if isinstance(data[0], (str, int, float)) else str(data[0])[:500]
            else:
                entry["data"] = data
            api_dump.append(entry)
        (url_dir / "api_responses.json").write_text(
            json.dumps(api_dump, indent=2, default=str)
        )

        # 2. Save network log
        (url_dir / "network_log.json").write_text(
            json.dumps(network_log, indent=2, default=str)
        )

        # 3. Save console messages
        (url_dir / "console_log.json").write_text(
            json.dumps(console_messages, indent=2, default=str)
        )

        # 4. Save screenshot and HTML
        if page:
            try:
                page.screenshot(
                    path=str(url_dir / "screenshot.png"), full_page=True
                )
            except Exception as e:
                logger.debug(f"Screenshot failed: {e}")

            html = page.content()
            (url_dir / "page_source.html").write_text(html)

            # 5. Comprehensive DOM analysis
            dom_analysis = page.evaluate(r"""() => {
                const result = {};

                // Basic page info
                result.title = document.title;
                result.url = window.location.href;
                result.pageTextLength = (document.body.innerText || '').length;

                // Framework detection
                result.framework = {};
                result.framework.hasNextData = !!document.querySelector('#__NEXT_DATA__');
                result.framework.hasReactRoot = !!document.querySelector('#__next') || !!document.querySelector('[data-reactroot]');
                result.framework.hasAngular = !!document.querySelector('[ng-version]') || !!document.querySelector('[_nghost]');
                result.framework.hasVue = !!document.querySelector('[data-v-]');
                result.framework.hasNuxt = !!document.querySelector('#__nuxt');
                const meta = document.querySelector('meta[name="generator"]');
                result.framework.generator = meta ? meta.getAttribute('content') : null;

                // All link href patterns (full list)
                const allLinks = document.querySelectorAll('a[href]');
                const hrefCounts = {};
                const hrefSamples = [];
                for (const a of allLinks) {
                    const href = a.getAttribute('href') || '';
                    if (href.length > 1) {
                        if (hrefSamples.length < 50) hrefSamples.push(href);
                        // Group by first 2 path segments
                        const parts = href.split('/').filter(Boolean);
                        const key = parts.length > 1
                            ? '/' + parts[0] + '/' + parts[1]
                            : href.substring(0, 60);
                        hrefCounts[key] = (hrefCounts[key] || 0) + 1;
                    }
                }
                result.links = {
                    total: allLinks.length,
                    patterns: hrefCounts,
                    samples: hrefSamples,
                };

                // Price elements — find every element containing a £ price
                const body = document.body.innerText || '';
                const priceMatches = body.match(/£[\\d,]+/g) || [];
                result.prices = {
                    totalOnPage: priceMatches.length,
                    values: priceMatches.slice(0, 30),
                };

                // Card-like selectors — test which ones find elements
                const selectorTests = [
                    '[class*="vehicle"]', '[class*="car"]', '[class*="listing"]',
                    '[class*="result"]', '[class*="product"]', '[class*="card"]',
                    '[class*="tile"]', '[class*="item"]',
                    '[data-testid*="vehicle"]', '[data-testid*="listing"]',
                    '[data-testid*="car"]', '[data-testid*="result"]',
                    'article', '[role="listitem"]', '[role="article"]',
                    // VW-specific guesses
                    '[class*="srp"]', '[class*="search-result"]',
                    '[class*="inventory"]', '[class*="stock"]',
                ];
                result.selectorMatches = {};
                for (const sel of selectorTests) {
                    try {
                        const count = document.querySelectorAll(sel).length;
                        if (count > 0) result.selectorMatches[sel] = count;
                    } catch(e) {}
                }

                // Specifically find elements that contain BOTH a price AND car keywords
                const carKw = /volkswagen|vw|id[\\.-]\\s?[34567]|golf|polo|tiguan|t-roc|t-cross/i;
                const pricePat = /£[\\d,]+/;
                const candidates = document.querySelectorAll('div, section, li, article, a');
                const cardCandidates = [];
                for (const el of candidates) {
                    const text = el.innerText || '';
                    if (text.length >= 30 && text.length <= 3000
                        && pricePat.test(text) && carKw.test(text)) {
                        // Check it's not a huge container
                        const milesCount = (text.match(/miles/gi) || []).length;
                        if (milesCount <= 3) {
                            cardCandidates.push({
                                tag: el.tagName,
                                className: (el.className || '').toString().substring(0, 200),
                                id: el.id || '',
                                textPreview: text.substring(0, 300),
                                textLength: text.length,
                                childLinks: el.querySelectorAll('a[href]').length,
                            });
                        }
                    }
                    if (cardCandidates.length >= 20) break;
                }
                result.cardCandidates = cardCandidates;

                // Cookie/consent banner detection
                result.cookieBanners = {};
                const cookieSelectors = [
                    '#onetrust-banner-sdk', '#onetrust-accept-btn-handler',
                    '[class*="cookie"]', '[class*="consent"]',
                    '[id*="cookie"]', '[id*="consent"]',
                ];
                for (const sel of cookieSelectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            result.cookieBanners[sel] = {
                                count: els.length,
                                visible: Array.from(els).some(e => {
                                    const r = e.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0;
                                }),
                            };
                        }
                    } catch(e) {}
                }

                // Postcode/location inputs
                const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                result.textInputs = [];
                for (const inp of inputs) {
                    result.textInputs.push({
                        name: inp.name || '',
                        id: inp.id || '',
                        placeholder: inp.placeholder || '',
                        ariaLabel: inp.getAttribute('aria-label') || '',
                        visible: inp.offsetParent !== null,
                    });
                }

                // Body classes and data attributes (often reveal app state)
                result.bodyClasses = document.body.className || '';
                result.htmlClasses = document.documentElement.className || '';
                const htmlAttrs = {};
                for (const attr of document.documentElement.attributes) {
                    if (attr.name !== 'class') htmlAttrs[attr.name] = attr.value;
                }
                result.htmlAttributes = htmlAttrs;

                return result;
            }""")

            (url_dir / "dom_analysis.json").write_text(
                json.dumps(dom_analysis, indent=2, default=str)
            )

        logger.info(f"Debug: saved artifacts to {url_dir}/ "
                     f"(APIs: {len(api_responses)}, "
                     f"network: {len(network_log)}, "
                     f"console: {len(console_messages)})")

    except Exception as e:
        logger.error(f"Failed to save debug data for {url}: {e}", exc_info=True)


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
    # Capture console messages for this URL
    console_messages: list[dict] = []

    def on_console(msg):
        console_messages.append({
            "type": msg.type,
            "text": msg.text,
        })

    page.on("console", on_console)

    # Step 1: Load page and intercept JSON/HTML responses + full network log
    api_responses, xhr_html_bodies, network_log = _try_intercept_api(page, url)
    logger.info(f"Intercepted {len(api_responses)} JSON responses, "
                f"{len(xhr_html_bodies)} XHR HTML responses, "
                f"{len(network_log)} total network requests")
    for resp in api_responses:
        resp_url = resp.get("url", "")
        logger.info(f"  API: {resp_url[:120]}")

    # Log final URL to detect redirects
    final_url = page.url
    logger.info(f"Final page URL: {final_url}")

    # Step 2a: Try to parse vehicle data from XHR HTML (primary method for VW site)
    listings = []
    if xhr_html_bodies:
        listings = _parse_listings_from_xhr_html(page, xhr_html_bodies)

    # Step 2b: Try API JSON responses as fallback
    if not listings:
        listings = _parse_listings_from_api(api_responses)
    if listings:
        logger.info(f"Parsed {len(listings)} listings from intercepted data")

    # Step 3: Handle cookie consent (only on first URL)
    if not cookie_handled:
        _handle_cookie_consent(page)
        _handle_postcode_entry(page)
        cookie_handled = True

    # Step 4: Wait for content to render (SPA may load asynchronously)
    # Try to wait for elements that indicate vehicle listings are visible
    page.wait_for_timeout(3000)
    content_found = False
    # Try multiple selectors — VW site may not show £ in DOM text
    for selector in ["text=£", "[class*='vehicle']", "[class*='result'] a[href*='vehicle_search']"]:
        try:
            page.wait_for_selector(selector, timeout=5000)
            logger.info(f"Content detected on page via: {selector}")
            content_found = True
            break
        except PlaywrightTimeout:
            continue
    if not content_found:
        logger.warning("No vehicle content found on page within timeout — "
                       "page may be empty, blocked, or structured differently")

    # Step 5: If API didn't have vehicle data, extract from DOM
    if not listings:
        logger.info("No vehicle data in API responses, extracting from rendered DOM...")
        _scroll_for_lazy_content(page)
        listings = _parse_listings_from_dom(page)
        logger.info(f"Extracted {len(listings)} listings from DOM (page 1)")

    # Step 6: Save debug artifacts for THIS URL (before pagination changes the page)
    _save_debug_for_url(url, api_responses, network_log, console_messages, page)

    page.remove_listener("console", on_console)

    # Step 7: Try to load more results (pagination / infinite scroll / load-more)
    if listings:
        for page_num in range(2, Config.VW_MAX_PAGES + 1):
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
                "Chrome/131.0.0.0 Safari/537.36"
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
                try:
                    listings, api_responses, cookie_handled = _scrape_single_url(
                        page, url, cookie_handled
                    )
                    all_listings.extend(listings)
                    all_api_responses.extend(api_responses)
                    logger.info(f"Got {len(listings)} listings from {url}")
                except PlaywrightTimeout:
                    logger.error(f"Timeout loading {url} — skipping to next URL")
                    # Save debug artifacts even on timeout
                    _save_debug_for_url(url, [], [], [], page)
                except Exception as e:
                    logger.error(f"Error scraping {url}: {e}", exc_info=True)
                    _save_debug_for_url(url, [], [], [], page)

        except Exception as e:
            logger.error(f"Fatal error in scraper: {e}", exc_info=True)
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
