#!/usr/bin/env python3
"""VW Used Car Price Monitor - Daily scanner and valuation tool."""

import argparse
import json
import logging
import sys
from pathlib import Path

from src.vw_scraper import scrape_vw_listings
from src.autotrader import valuate_listings
from src.report import send_email, generate_report_html, generate_report_text


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan VW used cars, compare with market prices, and send email report."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-email", action="store_true", help="Skip sending email (print report instead)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--save-html", type=str, metavar="FILE", help="Save HTML report to file")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between AutoTrader requests in seconds (default: 2.0)")

    args = parser.parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger("main")

    # Step 1: Scrape VW listings
    logger.info("Step 1/3: Scraping VW used car listings...")
    listings = scrape_vw_listings()

    if not listings:
        logger.warning("No listings found. The website may have changed or blocked the scraper.")
        logger.info("Check VW_POSTCODE and filter settings in your .env file.")
        return 1

    logger.info(f"Found {len(listings)} VW listings")

    # Step 2: Get market valuations
    logger.info("Step 2/3: Getting market valuations from AutoTrader...")
    valuations = valuate_listings(listings, delay=args.delay)

    # Step 3: Report
    logger.info("Step 3/3: Generating report...")

    if args.json:
        output = []
        for v in valuations:
            output.append({
                "listing": v.listing.to_dict(),
                "market_avg_price": v.market_avg_price,
                "market_min_price": v.market_min_price,
                "market_max_price": v.market_max_price,
                "market_sample_size": v.market_sample_size,
                "price_difference": v.price_difference,
                "price_difference_pct": v.price_difference_pct,
                "rating": v.rating,
                "comparable_url": v.comparable_url,
            })
        print(json.dumps(output, indent=2))
        return 0

    if args.save_html:
        html = generate_report_html(valuations)
        Path(args.save_html).write_text(html)
        logger.info(f"HTML report saved to {args.save_html}")

    if args.no_email:
        print(generate_report_text(valuations))
        return 0

    # Send email
    success = send_email(valuations)
    if not success:
        logger.error("Failed to send email. Use --no-email to print report to console instead.")
        print(generate_report_text(valuations))
        return 1

    logger.info("Done! Email report sent successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
