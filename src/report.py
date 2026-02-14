"""Generate and send HTML email reports with valuation results."""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Template

from src.autotrader import MarketValuation
from src.config import Config

logger = logging.getLogger(__name__)

RATING_LABELS = {
    "great_deal": "Great Deal",
    "good_deal": "Good Deal",
    "fair": "Fair Price",
    "overpriced": "Overpriced",
    "no_data": "No Data",
}

RATING_COLORS = {
    "great_deal": "#059669",
    "good_deal": "#10b981",
    "fair": "#f59e0b",
    "overpriced": "#ef4444",
    "no_data": "#6b7280",
}

EMAIL_TEMPLATE = Template("""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f3f4f6; color: #1f2937; }
    .container { max-width: 800px; margin: 0 auto; }
    .header { background: #001e50; color: white; padding: 24px; border-radius: 8px 8px 0 0; }
    .header h1 { margin: 0; font-size: 22px; }
    .header p { margin: 8px 0 0; opacity: 0.8; font-size: 14px; }
    .summary { background: white; padding: 20px 24px; border-bottom: 1px solid #e5e7eb; }
    .summary-grid { display: flex; gap: 16px; flex-wrap: wrap; }
    .summary-stat { flex: 1; min-width: 120px; text-align: center; padding: 12px; background: #f9fafb; border-radius: 6px; }
    .summary-stat .number { font-size: 24px; font-weight: 700; color: #001e50; }
    .summary-stat .label { font-size: 12px; color: #6b7280; margin-top: 4px; }
    .car-card { background: white; padding: 20px 24px; border-bottom: 1px solid #e5e7eb; }
    .car-card:last-child { border-radius: 0 0 8px 8px; border-bottom: none; }
    .car-title { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
    .car-title a { color: #001e50; text-decoration: none; }
    .car-title a:hover { text-decoration: underline; }
    .car-details { font-size: 13px; color: #6b7280; margin: 0 0 12px; }
    .price-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    .vw-price { font-size: 20px; font-weight: 700; color: #1f2937; }
    .market-price { font-size: 14px; color: #6b7280; }
    .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; color: white; }
    .diff-text { font-size: 13px; margin-top: 6px; }
    .compare-link { font-size: 12px; color: #2563eb; text-decoration: none; margin-top: 4px; display: inline-block; }
    .footer { text-align: center; padding: 16px; font-size: 12px; color: #9ca3af; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>VW Used Car Price Monitor</h1>
      <p>Daily Report &mdash; {{ date }}</p>
    </div>

    <div class="summary">
      <div class="summary-grid">
        <div class="summary-stat">
          <div class="number">{{ total_listings }}</div>
          <div class="label">Cars Found</div>
        </div>
        <div class="summary-stat">
          <div class="number">{{ great_deals }}</div>
          <div class="label">Great Deals</div>
        </div>
        <div class="summary-stat">
          <div class="number">{{ good_deals }}</div>
          <div class="label">Good Deals</div>
        </div>
        <div class="summary-stat">
          <div class="number">{{ avg_saving }}</div>
          <div class="label">Avg Below Market</div>
        </div>
      </div>
    </div>

    {% for v in valuations %}
    <div class="car-card">
      <h3 class="car-title">
        {% if v.listing.url %}<a href="{{ v.listing.url }}">{{ v.listing.title }}</a>{% else %}{{ v.listing.title }}{% endif %}
      </h3>
      <p class="car-details">
        {% if v.listing.year %}{{ v.listing.year }} &bull; {% endif %}
        {% if v.listing.mileage %}{{ "{:,}".format(v.listing.mileage) }} miles &bull; {% endif %}
        {% if v.listing.fuel_type %}{{ v.listing.fuel_type }} &bull; {% endif %}
        {% if v.listing.transmission %}{{ v.listing.transmission }}{% endif %}
        {% if v.listing.dealer %} &bull; {{ v.listing.dealer }}{% endif %}
      </p>
      <div class="price-row">
        <span class="vw-price">&pound;{{ "{:,}".format(v.listing.price) }}</span>
        {% if v.rating != "no_data" %}
        <span class="market-price">Market avg: &pound;{{ "{:,}".format(v.market_avg_price) }}
          ({{ v.market_sample_size }} comparable)</span>
        {% endif %}
        <span class="badge" style="background: {{ rating_colors[v.rating] }}">{{ rating_labels[v.rating] }}</span>
      </div>
      <p class="diff-text">{{ v.savings_text }}</p>
      <a class="compare-link" href="{{ v.comparable_url }}">Compare on AutoTrader &rarr;</a>
    </div>
    {% endfor %}

    {% if not valuations %}
    <div class="car-card">
      <p style="text-align:center; color:#6b7280; padding:40px 0;">
        No listings found matching your criteria today.
      </p>
    </div>
    {% endif %}

    <div class="footer">
      <p>Prices scraped from usedcars.volkswagen.co.uk &bull; Market data from autotrader.co.uk</p>
      <p>This is an automated report. Prices may have changed since this scan.</p>
    </div>
  </div>
</body>
</html>
""")


def generate_report_html(valuations: list[MarketValuation]) -> str:
    """Generate an HTML email report from valuation results."""
    great_deals = sum(1 for v in valuations if v.rating == "great_deal")
    good_deals = sum(1 for v in valuations if v.rating == "good_deal")

    # Calculate average saving for deals below market
    below_market = [v for v in valuations if v.price_difference < 0]
    if below_market:
        avg_saving = f"£{abs(sum(v.price_difference for v in below_market) // len(below_market)):,}"
    else:
        avg_saving = "N/A"

    return EMAIL_TEMPLATE.render(
        date=datetime.now().strftime("%d %B %Y"),
        total_listings=len(valuations),
        great_deals=great_deals,
        good_deals=good_deals,
        avg_saving=avg_saving,
        valuations=valuations,
        rating_labels=RATING_LABELS,
        rating_colors=RATING_COLORS,
    )


def generate_report_text(valuations: list[MarketValuation]) -> str:
    """Generate a plain text fallback report."""
    lines = [
        "VW Used Car Price Monitor - Daily Report",
        f"Date: {datetime.now().strftime('%d %B %Y')}",
        f"Total listings: {len(valuations)}",
        "=" * 60,
        "",
    ]

    for v in valuations:
        lines.append(f"{v.listing.title}")
        details = []
        if v.listing.year:
            details.append(str(v.listing.year))
        if v.listing.mileage:
            details.append(f"{v.listing.mileage:,} miles")
        if v.listing.fuel_type:
            details.append(v.listing.fuel_type)
        if details:
            lines.append(f"  {' | '.join(details)}")
        lines.append(f"  VW Price: £{v.listing.price:,}")
        if v.rating != "no_data":
            lines.append(f"  Market Avg: £{v.market_avg_price:,} ({v.market_sample_size} comparable)")
        lines.append(f"  Rating: {RATING_LABELS[v.rating]} - {v.savings_text}")
        lines.append(f"  Compare: {v.comparable_url}")
        lines.append("")

    return "\n".join(lines)


def send_email(valuations: list[MarketValuation]) -> bool:
    """Send the valuation report via SMTP email."""
    if not Config.SMTP_USER or not Config.EMAIL_TO:
        logger.error("Email not configured: SMTP_USER and EMAIL_TO are required")
        return False

    html_body = generate_report_html(valuations)
    text_body = generate_report_text(valuations)

    great_deals = sum(1 for v in valuations if v.rating == "great_deal")
    good_deals = sum(1 for v in valuations if v.rating == "good_deal")

    subject = f"VW Car Monitor: {len(valuations)} cars found"
    if great_deals:
        subject += f" ({great_deals} great deal{'s' if great_deals > 1 else ''}!)"
    elif good_deals:
        subject += f" ({good_deals} good deal{'s' if good_deals > 1 else ''})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.EMAIL_FROM
    msg["To"] = Config.EMAIL_TO

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        logger.info(f"Sending email to {Config.EMAIL_TO} via {Config.SMTP_HOST}:{Config.SMTP_PORT}")
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.sendmail(Config.EMAIL_FROM, Config.EMAIL_TO.split(","), msg.as_string())
        logger.info("Email sent successfully")
        return True
    except smtplib.SMTPException as e:
        logger.error(f"Failed to send email: {e}")
        return False
