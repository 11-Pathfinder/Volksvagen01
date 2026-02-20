# Build a VW Approved Used Car Price Monitor

## Goal

Build a Python tool that scrapes used car listings from **usedcars.volkswagen.co.uk**, compares prices against AutoTrader UK market data, and sends a daily HTML email report. Runs as a GitHub Actions workflow.

---

## Architecture

```
main.py                  # CLI entry point (--save-html, --no-email, --debug-only, -v)
src/
  config.py              # Env-var config (VW_MODELS, postcode, SMTP, filters)
  vw_scraper.py          # Playwright scraper → list[VWListing]
  autotrader.py          # requests + BeautifulSoup market comparison
  report.py              # Jinja2 HTML email report + SMTP sending
debug_scraper.py         # Standalone diagnostic tool (captures page artifacts)
.github/workflows/daily_scan.yml  # Runs daily at 07:00 UTC
requirements.txt         # playwright, requests, beautifulsoup4, python-dotenv, jinja2
```

---

## CRITICAL: VW Site Technical Details (learned from extensive debugging)

The VW approved used cars site (usedcars.volkswagen.co.uk) is built on the **Modix platform** with a **Solr** search backend. You MUST understand these details to build a working scraper:

### 1. Data Source Priority (most reliable first)

| Priority | Source | URL Pattern | Content-Type | Why |
|----------|--------|------------|--------------|-----|
| **1st** | Solr JSON | `/en/intern/solr-response.json?...` | `application/json` | **Structured data with named fields. ~114KB. This is the primary data source.** |
| 2nd | XHR HTML | `/en/vehicle_search/xhr-results/1?...` | `text/html` | AJAX-loaded HTML fragment (~1.1MB). Prices are CSS-rendered so harder to parse. |
| 3rd | Live DOM | (the rendered page) | — | Least reliable. Prices rendered via CSS `::before` pseudo-elements. |

### 2. Solr Response Structure

The `solr-response.json` endpoint returns standard Solr output:

```json
{
  "responseHeader": { "status": 0, "QTime": 12 },
  "response": {
    "numFound": 243,
    "start": 0,
    "docs": [
      {
        "CHIFFRE_STR": "TDD8K78",
        "PRICE_RETAIL_CUR": 8495,
        "MODEL_TYPE_LST": "up!",
        "MILEAGE_MIL_INT": 29485,
        "FUEL_TYPE_LST": "Petrol",
        "TRANSMISSION_LST": "Manual",
        "INITIAL_REGISTRATION_DTE": "2020-01-15T00:00:00Z",
        "TRIM_STR": "Move Up",
        "HEADLINE_STR": "1.0 Move up! 3dr",
        "DEALER_NAME_STR": "Lookers Volkswagen Newcastle",
        "DEALER_CITY_STR": "Newcastle",
        "IMAGE_URL_STR": "...",
        "DETAIL_URL_STR": "..."
      }
    ]
  },
  "facet_counts": { "facet_fields": { ... } }
}
```

**Key field mappings (Modix/Solr → your dataclass):**

| Solr Field | Maps To | Type | Notes |
|-----------|---------|------|-------|
| `PRICE_RETAIL_CUR` | price | int or float | May be float like `8495.0` — use `int(round(val))` |
| `MODEL_TYPE_LST` | model | string | e.g. "up!", "Polo", "ID.4" |
| `MILEAGE_MIL_INT` | mileage | int or float | Miles |
| `INITIAL_REGISTRATION_DTE` | year | date string | Format: `"2020-01-15T00:00:00Z"` — extract year with regex `(20\d{2})`, do NOT use generic int extraction on this |
| `FUEL_TYPE_LST` | fuel_type | string | "Petrol", "Diesel", "Electric" |
| `TRANSMISSION_LST` | transmission | string | "Manual", "Automatic" |
| `TRIM_STR` | trim | string | |
| `HEADLINE_STR` | title | string | |
| `CHIFFRE_STR` | registration/id | string | VW internal vehicle ID |
| `DEALER_NAME_STR` | dealer | string | |
| `DEALER_CITY_STR` | location | string | |
| `IMAGE_URL_STR` | image_url | string | |
| `DETAIL_URL_STR` | url | string | |

**IMPORTANT**: The `facet_counts` section contains filter metadata (model counts, etc.), NOT vehicle listings. Your recursive array finder will correctly skip it because facet values are `["model", count, ...]` (alternating strings/ints), not arrays of dicts.

### 3. Price Rendering (£ is invisible in DOM text)

The VW site renders the `£` symbol via CSS pseudo-elements:
```css
.price-value::before { content: "£"; }
```

This means:
- `element.innerText` on the LIVE page: shows "£18,995" (CSS is applied)
- `element.textContent` on the LIVE page: shows "18,995" (no £)
- `DOMParser.textContent` (parsing XHR HTML): shows "18,995" (no CSS at all)
- `DOMParser.innerHTML`: shows `<span class="price-value">18,995</span>` (no £ entity)
- `body.innerText.match(/£[\d,]+/g)`: returns **0 matches** on this site

**Consequence**: Any price extraction from HTML/DOM must NOT rely on finding `£`. Use the Solr JSON instead, or fall back to extracting numeric content from elements with `class*="price"`.

### 4. URL Patterns

```
Search page:  /en/vehicle_search/volkswagen/{model-slug}
Detail page:  /en/vehicle_search/volkswagen/{model-slug}/{description}-{chiffre_lower}
XHR results:  /en/vehicle_search/xhr-results/1?POOLS_CSV=...&search=passenger&MANUFACTURER_LST=VOLKSWAGEN
Solr API:     /en/intern/solr-response.json?POOLS_CSV=...&search=passenger&MANUFACTURER_LST=VOLKSWAGEN
```

Detail pages have **5+ path segments** (split by `/`, filtered empty). Search pages have 4.

### 5. Page Behavior

- **Cookie consent**: OneTrust banner (`#onetrust-accept-btn-handler`). Must dismiss.
- **Postcode prompt**: Input with `id="mdx-zip_loc-passenger"`, placeholder "Postcode or location". May need to fill.
- **Lazy loading**: Images use `lazysizes` library. Scroll to trigger.
- **Pagination**: Links like `/all-brands/all-models/page2?...`. Also "Load more" buttons possible.
- **Embedded videos**: Vimeo + CitNow iframes (ignore these, they generate ~50% of network traffic).
- **Network volume**: ~150+ requests per page load including tracking, fonts, video embeds.

---

## Scraper Design

### Network Interception Strategy

Use Playwright's `page.on("response", handler)` to capture responses DURING page navigation:

```python
def handle_response(response):
    content_type = response.headers.get("content-type", "")
    resp_url = response.url
    status = response.status

    # Capture JSON (includes solr-response.json)
    if "application/json" in content_type:
        json_responses.append({"url": resp_url, "data": response.json()})

    # Capture XHR HTML (backup data source)
    if "xhr-results" in resp_url and "text/html" in content_type and status == 200:
        xhr_html_bodies.append(response.text())
```

### Extraction Order

```
1. Try Solr JSON (api_responses) → _parse_listings_from_api()
   - Recursively search JSON for arrays of dicts with PRICE_RETAIL_CUR etc.
   - Handle float values: int(round(val)) for numeric fields
   - Handle date strings: extract year via regex from INITIAL_REGISTRATION_DTE

2. If no listings → Try XHR HTML → _parse_listings_from_xhr_html()
   - Use page.evaluate() with DOMParser to parse the captured HTML
   - Find <a href*="/vehicle_search/volkswagen/"> with 5+ path segments
   - Walk up DOM to find card container
   - Extract price from [class*="price"] elements (raw numbers, no £)

3. If no listings → Try live DOM → _parse_listings_from_dom()
   - Similar to XHR HTML but on the live rendered page
   - innerText available here (may have £ from CSS on live page)
   - Multiple strategies: VW-specific links → generic vehicle links → broad element search
```

### Price Extraction Fallback Chain

```python
def _extract_price(text: str) -> int:
    # 1. Look for £ / &pound; / &#163; prefix
    prices = re.findall(r"(?:£|&pound;|&#163;)([\d,]+)", text)
    for p in prices:
        val = _extract_int(p)
        if val > 500: return val

    # 2. Comma-formatted numbers (e.g. "18,995") not followed by "miles"
    for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})+)\b", text):
        val = _extract_int(m.group(1))
        if 2000 < val < 200000:
            after = text[m.end():m.end()+10].strip().lower()
            if after.startswith("mile"): continue
            return val

    # 3. Plain 5-6 digit numbers (e.g. "18995") — last resort
    for m in re.finditer(r"\b([1-9]\d{4,5})\b", text):
        val = int(m.group(1))
        if 2000 < val < 200000:
            after = text[m.end():m.end()+10].strip().lower()
            if after.startswith("mile"): continue
            before = text[max(0,m.start()-15):m.start()].lower()
            if "mile" in before or "km" in before: continue
            return val

    return 0
```

---

## Debug & Diagnostics

**Save comprehensive debug artifacts for EVERY URL scraped** (critical for remote debugging in CI):

```
debug/{url-slug}/
  screenshot.png          # Full-page screenshot
  page_source.html        # Complete rendered HTML
  api_responses.json      # All intercepted JSON payloads
  network_log.json        # Every HTTP request (URL, status, content-type, size)
  console_log.json        # Browser console messages
  dom_analysis.json       # Page structure analysis:
                          #   - Link patterns and samples
                          #   - Price elements found (count + values)
                          #   - Selector matches ([class*="vehicle"], etc.)
                          #   - Card candidates (elements with car keywords + prices)
                          #   - Cookie banners, text inputs, framework detection
```

Upload `debug/` as a GitHub Actions artifact (7-day retention) so failures can be investigated.

---

## GitHub Actions Workflow

```yaml
name: Daily VW Car Scan
on:
  schedule:
    - cron: '0 7 * * *'
  workflow_dispatch:

jobs:
  scan:
    runs-on: ubuntu-latest
    env:
      VW_POSTCODE: ${{ secrets.VW_POSTCODE }}
      VW_MODELS: ${{ secrets.VW_MODELS }}
      VW_MAX_PRICE: ${{ secrets.VW_MAX_PRICE }}
      VW_MAX_MILEAGE: ${{ secrets.VW_MAX_MILEAGE }}
      VW_MIN_YEAR: ${{ secrets.VW_MIN_YEAR }}
      SMTP_HOST: ${{ secrets.SMTP_HOST }}
      SMTP_PORT: ${{ secrets.SMTP_PORT }}
      SMTP_USER: ${{ secrets.SMTP_USER }}
      SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
      EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
      EMAIL_TO: ${{ secrets.EMAIL_TO }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: python -m playwright install --with-deps chromium
      - run: python main.py -v --save-html report.html
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: report
          path: report.html
          retention-days: 30
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: debug
          path: debug/
          retention-days: 7
```

### IMPORTANT: Exit Code Handling

- If `--save-html` is provided and the HTML file is saved, **email failure should NOT cause exit code 1**. The HTML artifact is the primary deliverable. Log a warning, return 0.
- Only return exit code 1 if: (a) scraper found 0 listings AND (b) no HTML was saved, OR (c) an unhandled exception occurs.

---

## Config (env vars)

| Var | Default | Description |
|-----|---------|-------------|
| `VW_POSTCODE` | `SW1A 1AA` | Search postcode |
| `VW_MODELS` | (empty = all) | Comma-separated: `golf,polo,id.4` |
| `VW_MAX_PRICE` | (empty) | Max price filter |
| `VW_MAX_MILEAGE` | (empty) | Max mileage filter |
| `VW_MIN_YEAR` | (empty) | Min year filter |
| `VW_MAX_PAGES` | `10` | Max pagination pages |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | | Gmail address |
| `SMTP_PASSWORD` | | Gmail app password |
| `EMAIL_FROM` | | Sender address |
| `EMAIL_TO` | | Recipient(s), comma-separated |
| `AT_POSTCODE` | (from VW_POSTCODE) | AutoTrader search postcode |

### Filter Edge Cases

When applying `VW_MIN_YEAR` or `VW_MAX_MILEAGE` filters, **keep listings where the field is 0** (meaning it couldn't be extracted). Don't discard valid listings just because metadata extraction failed:

```python
# WRONG: drops listings with unextracted year
all_listings = [l for l in all_listings if l.year >= min_year]

# RIGHT: preserves listings with unknown year
all_listings = [l for l in all_listings if l.year == 0 or l.year >= min_year]
```

---

## AutoTrader Market Comparison

For each VW listing, search AutoTrader for comparable vehicles:
- Build search URL with make=Volkswagen, model, year ±1, price ±£3000, fuel type
- Extract prices from search results HTML using BeautifulSoup
- Calculate: market avg, min, max, sample size, price difference, % difference
- Rate as: great_deal (≤-10%), good_deal (≤-3%), fair (≤+5%), overpriced (>+5%), no_data
- Rate limit: 2 second delay between AutoTrader requests

Map VW model names to AutoTrader search slugs:
```python
{"golf": "golf", "polo": "polo", "tiguan": "tiguan", "t-roc": "t-roc",
 "id.3": "id.3", "id.4": "id.4", "id.5": "id.5", "id.7": "id.7",
 "touareg": "touareg", "passat": "passat", "up": "up", "id. buzz": "id.-buzz", ...}
```

---

## VW Model Name Formatting

Detect models from text using a longest-first ordered list to avoid partial matches ("id.7 tourer" before "id.7", "golf gti" before "golf"):

```python
VW_MODELS = [
    "id. buzz", "id.7 tourer", "id.3", "id.4", "id.5", "id.7",
    "golf gti", "golf gte", "golf gtd", "golf r", "golf estate", "golf sv", "golf",
    "t-roc cabriolet", "t-roc", "t-cross",
    "tiguan allspace", "tiguan", "touareg", "touran", "tayron",
    "polo", "taigo", "passat estate", "passat", "arteon",
    "multivan", "up!", "e-up!", "e-golf", "sharan", "caravelle",
]
```

Canonical casing:
- `"id. buzz"` → `"ID. Buzz"` (special case, check first)
- `"id.3"` → `"ID.3"`, `"id.7 tourer"` → `"ID.7 Tourer"` (use `.title()` on suffix)
- Everything else → `.title()` (e.g. "golf gti" → "Golf Gti")

---

## HTML Email Report

Styled HTML email with:
- Header: "VW Used Car Price Monitor" + date
- Summary stats: cars found, great deals, good deals, avg savings
- Per-car cards: title (linked to VW listing URL), year/mileage/fuel/transmission, VW price, market avg price, deal rating badge (color-coded), savings text, AutoTrader compare link
- If no listings: show "No listings found matching your criteria today."
- Plain-text fallback for email clients that don't render HTML

---

## Key Pitfalls to Avoid

1. **Never skip solr-response.json** — it's the primary structured data source, not just "filter metadata"
2. **Never rely on `£` being in DOM text** — it's CSS-rendered on this site
3. **Handle Solr float values** — `int(round(val))` not `_extract_int(str(val))` which turns `8495.0` into `849500`
4. **Handle Solr date strings** — `INITIAL_REGISTRATION_DTE: "2020-01-15T00:00:00Z"` needs regex year extraction, not generic int extraction
5. **DOMParser has no innerText** — only textContent, and it has no linebreaks
6. **Don't fail workflow on email error** when HTML report was already saved
7. **Preserve listings with missing metadata** — year=0 or mileage=0 should pass through filters
8. **Log everything** — intercepted API count, XHR HTML count, network request count, extraction strategy used, listings per source, model distribution, filter counts
