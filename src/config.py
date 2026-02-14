import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # VW Scraper
    VW_BASE_URL = "https://usedcars.volkswagen.co.uk"
    VW_SEARCH_URL = f"{VW_BASE_URL}/en/vehicle_search/volkswagen/all-models"
    VW_POSTCODE = os.getenv("VW_POSTCODE", "SW1A 1AA")
    VW_RADIUS_MILES = int(os.getenv("VW_RADIUS_MILES", "50"))
    VW_MAX_PAGES = int(os.getenv("VW_MAX_PAGES", "10"))

    # Optional filters
    VW_MAX_PRICE = os.getenv("VW_MAX_PRICE")  # e.g. "25000"
    VW_MAX_MILEAGE = os.getenv("VW_MAX_MILEAGE")  # e.g. "50000"
    VW_MIN_YEAR = os.getenv("VW_MIN_YEAR")  # e.g. "2020"
    VW_MODELS = os.getenv("VW_MODELS", "")  # comma-separated, e.g. "golf,polo,tiguan"

    # AutoTrader valuation
    AT_BASE_URL = "https://www.autotrader.co.uk"
    AT_POSTCODE = os.getenv("AT_POSTCODE", VW_POSTCODE)

    # Email
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
    EMAIL_TO = os.getenv("EMAIL_TO", "")

    @classmethod
    def get_model_filters(cls) -> list[str]:
        if not cls.VW_MODELS:
            return []
        return [m.strip().lower() for m in cls.VW_MODELS.split(",") if m.strip()]
