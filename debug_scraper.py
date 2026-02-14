#!/usr/bin/env python3
"""Standalone diagnostic tool for the VW used car scraper.

Loads the VW search pages in Playwright and captures comprehensive debug
artifacts WITHOUT attempting to parse listings. Use this to understand
what the page looks like before tuning the scraper.

Usage:
    python debug_scraper.py                         # Use URLs from .env config
    python debug_scraper.py --url <custom_url>      # Test a specific URL
    python debug_scraper.py --all-models             # Force all-models URL

Output is written to debug/ with one subfolder per URL containing:
    screenshot.png      Full-page screenshot
    page_source.html    Complete rendered HTML
    network_log.json    Every HTTP request/response (URL, status, content-type, size)
    api_responses.json  All intercepted JSON API payloads (full content)
    console_log.json    Browser console messages (errors, warnings, info)
    dom_analysis.json   Detailed page structure analysis:
                        - Framework detection (React, Angular, Vue, Next.js)
                        - All link href patterns and samples
                        - Price elements found on page
                        - Card-like selector matches
                        - Card candidate elements (text preview, classes, tags)
                        - Cookie/consent banner detection
                        - Text input fields (postcode prompts)
                        - HTML/body classes and attributes
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Allow running without package install (direct script execution)
sys.path.insert(0, str(Path(__file__).parent))
from src.config import Config

DEBUG_DIR = Path("debug")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("debug_scraper")


def _url_to_label(url: str) -> str:
    path = url.split("?")[0].rstrip("/")
    slug = path.split("/")[-1] if "/" in path else "page"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return slug[:60]


def diagnose_url(page, url: str) -> dict:
    """Load a URL and capture everything about the resulting page state."""
    label = _url_to_label(url)
    url_dir = DEBUG_DIR / label
    url_dir.mkdir(parents=True, exist_ok=True)

    # --- Capture network traffic and JSON APIs ---
    network_log = []
    json_responses = []
    console_messages = []

    def on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            entry = {
                "url": response.url,
                "status": response.status,
                "content_type": ct,
            }
            try:
                entry["size"] = len(response.body())
            except Exception:
                entry["size"] = -1
            network_log.append(entry)

            if "application/json" in ct:
                data = response.json()
                json_responses.append({"url": response.url, "data": data})
        except Exception:
            pass

    def on_console(msg):
        console_messages.append({"type": msg.type, "text": msg.text})

    page.on("response", on_response)
    page.on("console", on_console)

    # --- Navigate ---
    logger.info(f"Navigating to: {url}")
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except PlaywrightTimeout:
        logger.warning(f"Timeout navigating to {url} — saving partial state")

    final_url = page.url
    redirected = final_url != url
    if redirected:
        logger.warning(f"REDIRECT detected: {url} → {final_url}")
    else:
        logger.info(f"Final URL: {final_url} (no redirect)")

    # --- Wait for content ---
    page.wait_for_timeout(3000)
    price_visible = False
    try:
        page.wait_for_selector("text=£", timeout=10000)
        price_visible = True
        logger.info("Price text (£) detected on page")
    except PlaywrightTimeout:
        logger.warning("No £ price text found within 10s")

    # --- Try dismissing cookie banner ---
    cookie_dismissed = False
    for sel in [
        "#onetrust-accept-btn-handler",
        "button[id*='accept']",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1500)
                cookie_dismissed = True
                logger.info(f"Dismissed cookie banner via: {sel}")
                break
        except Exception:
            continue

    # Wait a bit after cookie dismissal for content to settle
    if cookie_dismissed:
        page.wait_for_timeout(2000)

    # --- Scroll to trigger lazy content ---
    logger.info("Scrolling page to trigger lazy-loaded content...")
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

    page.remove_listener("response", on_response)
    page.remove_listener("console", on_console)

    # --- Save artifacts ---
    # 1. Screenshot
    try:
        page.screenshot(path=str(url_dir / "screenshot.png"), full_page=True)
        logger.info(f"Saved screenshot to {url_dir}/screenshot.png")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")

    # 2. HTML source
    html = page.content()
    (url_dir / "page_source.html").write_text(html)
    logger.info(f"Saved HTML source ({len(html):,} chars)")

    # 3. Network log
    (url_dir / "network_log.json").write_text(
        json.dumps(network_log, indent=2, default=str)
    )
    logger.info(f"Saved network log ({len(network_log)} requests)")

    # 4. API responses
    api_dump = []
    for resp in json_responses:
        entry = {"url": resp["url"]}
        data = resp["data"]
        if isinstance(data, dict):
            entry["top_level_keys"] = list(data.keys())
        data_str = json.dumps(data, default=str)
        if len(data_str) < 500000:
            entry["data"] = data
        else:
            entry["note"] = f"Data too large ({len(data_str)} chars), truncated"
            if isinstance(data, dict):
                entry["sample_keys"] = {k: type(v).__name__ for k, v in data.items()}
        api_dump.append(entry)
    (url_dir / "api_responses.json").write_text(
        json.dumps(api_dump, indent=2, default=str)
    )
    logger.info(f"Saved {len(api_dump)} API responses")

    # 5. Console log
    (url_dir / "console_log.json").write_text(
        json.dumps(console_messages, indent=2, default=str)
    )
    errors = [m for m in console_messages if m["type"] == "error"]
    warnings = [m for m in console_messages if m["type"] == "warning"]
    logger.info(f"Saved console log ({len(console_messages)} messages, "
                f"{len(errors)} errors, {len(warnings)} warnings)")

    # 6. DOM analysis
    dom_analysis = page.evaluate("""() => {
        const result = {};

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

        // Script sources (to identify bundled frameworks)
        const scripts = document.querySelectorAll('script[src]');
        result.framework.scriptSources = Array.from(scripts)
            .map(s => s.getAttribute('src'))
            .filter(s => s && s.length > 0)
            .slice(0, 20);

        // __NEXT_DATA__ content (if Next.js)
        const nextData = document.querySelector('#__NEXT_DATA__');
        if (nextData) {
            try {
                const parsed = JSON.parse(nextData.textContent);
                result.framework.nextDataKeys = Object.keys(parsed);
                if (parsed.props && parsed.props.pageProps) {
                    result.framework.nextPagePropsKeys = Object.keys(parsed.props.pageProps);
                }
            } catch(e) {}
        }

        // Link patterns
        const allLinks = document.querySelectorAll('a[href]');
        const hrefCounts = {};
        const hrefSamples = [];
        for (const a of allLinks) {
            const href = a.getAttribute('href') || '';
            if (href.length > 1) {
                if (hrefSamples.length < 80) hrefSamples.push(href);
                const parts = href.split('/').filter(Boolean);
                const key = parts.length > 1
                    ? '/' + parts[0] + '/' + parts[1]
                    : href.substring(0, 60);
                hrefCounts[key] = (hrefCounts[key] || 0) + 1;
            }
        }
        result.links = { total: allLinks.length, patterns: hrefCounts, samples: hrefSamples };

        // Prices
        const body = document.body.innerText || '';
        const priceMatches = body.match(/£[\\d,]+/g) || [];
        result.prices = { totalOnPage: priceMatches.length, values: priceMatches.slice(0, 50) };

        // Selector matches
        const selectorTests = [
            '[class*="vehicle"]', '[class*="car"]', '[class*="listing"]',
            '[class*="result"]', '[class*="product"]', '[class*="card"]',
            '[class*="tile"]', '[class*="item"]', '[class*="offer"]',
            '[data-testid*="vehicle"]', '[data-testid*="listing"]',
            '[data-testid*="car"]', '[data-testid*="result"]',
            'article', '[role="listitem"]', '[role="article"]',
            '[class*="srp"]', '[class*="search-result"]',
            '[class*="inventory"]', '[class*="stock"]',
            '[class*="grid"]', '[class*="gallery"]',
        ];
        result.selectorMatches = {};
        for (const sel of selectorTests) {
            try {
                const count = document.querySelectorAll(sel).length;
                if (count > 0) result.selectorMatches[sel] = count;
            } catch(e) {}
        }

        // Card candidates — elements with both a price and car keyword
        const carKw = /volkswagen|vw|id[\\.-]\\s?[34567]|golf|polo|tiguan|t-roc|t-cross|touareg|touran|tayron/i;
        const pricePat = /£[\\d,]+/;
        const candidates = document.querySelectorAll('div, section, li, article, a');
        const cardCandidates = [];
        for (const el of candidates) {
            const text = el.innerText || '';
            if (text.length >= 30 && text.length <= 3000
                && pricePat.test(text) && carKw.test(text)) {
                const milesCount = (text.match(/miles/gi) || []).length;
                if (milesCount <= 3) {
                    // Get all links inside this card
                    const innerLinks = Array.from(el.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href'))
                        .filter(h => h && h.length > 1);
                    cardCandidates.push({
                        tag: el.tagName,
                        className: (el.className || '').toString().substring(0, 300),
                        id: el.id || '',
                        dataTestId: el.getAttribute('data-testid') || '',
                        textPreview: text.substring(0, 500),
                        textLength: text.length,
                        innerLinks: innerLinks.slice(0, 5),
                        childCount: el.children.length,
                    });
                }
            }
            if (cardCandidates.length >= 30) break;
        }
        result.cardCandidates = cardCandidates;

        // Cookie banners
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

        // Text inputs
        const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
        result.textInputs = [];
        for (const inp of inputs) {
            result.textInputs.push({
                name: inp.name || '',
                id: inp.id || '',
                placeholder: inp.placeholder || '',
                ariaLabel: inp.getAttribute('aria-label') || '',
                visible: inp.offsetParent !== null,
                value: inp.value || '',
            });
        }

        // Page-level attributes
        result.bodyClasses = document.body.className || '';
        result.htmlClasses = document.documentElement.className || '';
        const htmlAttrs = {};
        for (const attr of document.documentElement.attributes) {
            if (attr.name !== 'class') htmlAttrs[attr.name] = attr.value;
        }
        result.htmlAttributes = htmlAttrs;

        // First 5000 chars of visible text (for manual inspection)
        result.pageTextSample = (document.body.innerText || '').substring(0, 5000);

        return result;
    }""")

    (url_dir / "dom_analysis.json").write_text(
        json.dumps(dom_analysis, indent=2, default=str)
    )
    logger.info(f"Saved DOM analysis")

    # --- Print summary ---
    summary = {
        "url_requested": url,
        "url_final": final_url,
        "redirected": redirected,
        "price_visible": price_visible,
        "cookie_dismissed": cookie_dismissed,
        "page_title": dom_analysis.get("title", ""),
        "page_text_length": dom_analysis.get("pageTextLength", 0),
        "total_links": dom_analysis.get("links", {}).get("total", 0),
        "prices_on_page": dom_analysis.get("prices", {}).get("totalOnPage", 0),
        "price_samples": dom_analysis.get("prices", {}).get("values", [])[:10],
        "framework": dom_analysis.get("framework", {}),
        "selector_matches": dom_analysis.get("selectorMatches", {}),
        "card_candidates_found": len(dom_analysis.get("cardCandidates", [])),
        "network_requests": len(network_log),
        "json_api_responses": len(json_responses),
        "console_errors": len(errors),
        "console_warnings": len(warnings),
        "artifacts_dir": str(url_dir),
    }

    (url_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose VW used car scraper — captures page state without parsing."
    )
    parser.add_argument(
        "--url", type=str, nargs="*",
        help="Specific URL(s) to diagnose. Defaults to URLs from config.",
    )
    parser.add_argument(
        "--all-models", action="store_true",
        help="Force using the all-models URL.",
    )
    args = parser.parse_args()

    if args.url:
        urls = args.url
    elif args.all_models:
        urls = [f"{Config.VW_BASE_URL}/en/vehicle_search/volkswagen/all-models"]
    else:
        urls = Config.get_search_urls()

    logger.info(f"URLs to diagnose: {urls}")
    DEBUG_DIR.mkdir(exist_ok=True)

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

        all_summaries = []
        for url in urls:
            logger.info(f"\n{'='*60}")
            logger.info(f"DIAGNOSING: {url}")
            logger.info(f"{'='*60}")
            summary = diagnose_url(page, url)
            all_summaries.append(summary)

            # Print key findings
            print(f"\n--- {url} ---")
            print(f"  Final URL:       {summary['url_final']}")
            print(f"  Redirected:      {summary['redirected']}")
            print(f"  Page title:      {summary['page_title']}")
            print(f"  Prices visible:  {summary['price_visible']}")
            print(f"  Prices found:    {summary['prices_on_page']}")
            print(f"  Price samples:   {summary['price_samples']}")
            print(f"  Card candidates: {summary['card_candidates_found']}")
            print(f"  Links total:     {summary['total_links']}")
            print(f"  Network reqs:    {summary['network_requests']}")
            print(f"  JSON APIs:       {summary['json_api_responses']}")
            print(f"  Console errors:  {summary['console_errors']}")
            print(f"  Framework:       {summary['framework']}")
            print(f"  Selectors hit:   {summary['selector_matches']}")
            print(f"  Artifacts:       {summary['artifacts_dir']}/")

        browser.close()

    # Save combined summary
    (DEBUG_DIR / "diagnosis_summary.json").write_text(
        json.dumps(all_summaries, indent=2, default=str)
    )

    print(f"\nAll artifacts saved to {DEBUG_DIR}/")
    print(f"Key files to inspect:")
    print(f"  debug/*/screenshot.png      — What the page looks like")
    print(f"  debug/*/dom_analysis.json   — Page structure & selector matches")
    print(f"  debug/*/api_responses.json  — JSON data from APIs")
    print(f"  debug/*/network_log.json    — All HTTP requests")
    print(f"  debug/diagnosis_summary.json — Combined summary")

    return 0


if __name__ == "__main__":
    sys.exit(main())
