# VW Used Car Price Monitor

Automated tool to scan [Volkswagen UK used car listings](https://usedcars.volkswagen.co.uk/en/home), compare prices against AutoTrader market data, and send daily email reports with valuation insights.

## Features

- **Daily Scraping**: Uses Playwright to scrape VW's used car website
- **Market Comparison**: Searches AutoTrader for comparable vehicles and calculates market average pricing
- **Smart Valuation**: Identifies great deals, good deals, fair prices, and overpriced listings
- **Email Reports**: Beautiful HTML email reports with pricing insights
- **GitHub Actions**: Runs automatically every day at 7 AM UTC
- **Flexible Filtering**: Filter by model, price, mileage, year, and location

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Settings

Copy the example config and add your settings:

```bash
cp .env.example .env
```

Edit `.env` with your preferences:

```bash
# Location and search preferences
VW_POSTCODE=SW1A 1AA
VW_RADIUS_MILES=50
VW_MODELS=golf,polo,tiguan  # comma-separated

# Optional filters
VW_MAX_PRICE=25000
VW_MAX_MILEAGE=50000
VW_MIN_YEAR=2020

# Email (use Gmail App Password, not your regular password)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your.email@gmail.com
SMTP_PASSWORD=your-app-password
EMAIL_TO=your.email@gmail.com
```

**Gmail Setup**: Generate an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

### 3. Run Locally

```bash
# Full scan and email report
python main.py

# Print to console (don't send email)
python main.py --no-email

# Save HTML report to file
python main.py --save-html report.html

# Output JSON data
python main.py --json

# Verbose logging
python main.py -v
```

## GitHub Actions Setup

The tool runs automatically every day at 7 AM UTC via GitHub Actions.

### Configure GitHub Secrets

Add these secrets to your repository (`Settings → Secrets and variables → Actions → New repository secret`):

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `VW_POSTCODE` | Your UK postcode | `SW1A 1AA` |
| `VW_RADIUS_MILES` | Search radius | `50` |
| `VW_MAX_PRICE` | Max price filter (optional) | `25000` |
| `VW_MAX_MILEAGE` | Max mileage filter (optional) | `50000` |
| `VW_MIN_YEAR` | Min year filter (optional) | `2020` |
| `VW_MODELS` | Comma-separated models (optional) | `golf,polo,tiguan` |
| `AT_POSTCODE` | AutoTrader postcode (defaults to VW_POSTCODE) | `SW1A 1AA` |
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` | Your email address | `you@gmail.com` |
| `SMTP_PASSWORD` | Gmail App Password | `xxxx xxxx xxxx xxxx` |
| `EMAIL_FROM` | From address | `you@gmail.com` |
| `EMAIL_TO` | Recipient(s) | `you@gmail.com` |

### Manual Trigger

Trigger a scan manually from the **Actions** tab → **Daily VW Car Price Scan** → **Run workflow**.

## How It Works

1. **Scrape VW Listings**: Playwright loads the VW used cars site, handles cookie consent, enters your postcode, and extracts all car listings (model, price, mileage, year, fuel type, etc.)

2. **Get Market Data**: For each VW listing, searches AutoTrader for comparable vehicles (same model, similar year, similar price range) and calculates average market price

3. **Calculate Value**:
   - **Great Deal**: 10%+ below market average
   - **Good Deal**: 3-10% below market average
   - **Fair Price**: Within ±3% of market average
   - **Overpriced**: 5%+ above market average

4. **Send Report**: Generates HTML email with all listings sorted by best deals first, including market comparison and AutoTrader links

## Project Structure

```
Volksvagen01/
├── src/
│   ├── vw_scraper.py       # Scrapes VW used car listings
│   ├── autotrader.py       # AutoTrader market comparison
│   ├── report.py           # Email report generation
│   └── config.py           # Configuration management
├── .github/
│   └── workflows/
│       └── daily_scan.yml  # GitHub Actions daily cron
├── main.py                 # CLI entry point
├── requirements.txt        # Python dependencies
├── .env.example            # Example config file
└── README.md
```

## Troubleshooting

### No listings found

- The VW site may have changed structure. Check logs with `python main.py -v`
- Verify your `VW_POSTCODE` is valid
- Try reducing filters (remove `VW_MAX_PRICE`, `VW_MODELS`, etc.)

### Email not sending (Gmail)

- Use an **App Password**, not your regular Gmail password
- Enable 2-factor authentication first
- Generate App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

### Rate limiting / 403 errors

- Increase `--delay` between AutoTrader requests: `python main.py --delay 3.0`
- The default is 2 seconds between requests

## Legal & Ethics

- This tool is for **personal use only**
- Respects reasonable rate limiting (2s delay between requests)
- Does not overwhelm servers or violate ToS
- Always verify prices on the actual websites before making decisions

## License

MIT

---

Built with Playwright, BeautifulSoup, and Python 3.12+
