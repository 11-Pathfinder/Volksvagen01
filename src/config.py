import os
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    """Get env var, treating empty strings as unset (falls back to default)."""
    value = os.getenv(key, "")
    return value if value else default


def _env_int(key: str, default: int) -> int:
    """Get env var as int, treating empty/missing as the default."""
    value = os.getenv(key, "")
    return int(value) if value else default


# Map of user-friendly model names to VW website URL path slugs.
# The VW site uses hyphens in URL paths (e.g. /volkswagen/id-4, not /volkswagen/id.4).
MODEL_URL_SLUGS = {
    "id.3": "id-3",
    "id.4": "id-4",
    "id.5": "id-5",
    "id.7": "id-7",
    "id.7 tourer": "id-7-tourer",
    "id. buzz": "id-buzz",
    "golf": "golf",
    "golf gti": "golf-gti",
    "golf gte": "golf-gte",
    "golf gtd": "golf-gtd",
    "golf r": "golf-r",
    "golf estate": "golf-estate",
    "golf sv": "golf-sv",
    "polo": "polo",
    "t-roc": "t-roc",
    "t-roc cabriolet": "t-roc-cabriolet",
    "t-cross": "t-cross",
    "tiguan": "tiguan",
    "tiguan allspace": "tiguan-allspace",
    "touareg": "touareg",
    "touran": "touran",
    "tayron": "tayron",
    "taigo": "taigo",
    "passat": "passat",
    "passat estate": "passat-estate",
    "arteon": "arteon",
    "arteon shooting brake": "arteon-shooting-brake",
    "multivan": "multivan",
    "up!": "up",
    "e-up!": "e-up",
    "e-golf": "e-golf",
    "sharan": "sharan",
    "caravelle": "caravelle",
}


class Config:
    # VW Scraper
    VW_BASE_URL = "https://usedcars.volkswagen.co.uk"
    VW_POSTCODE = _env("VW_POSTCODE", "SW1A 1AA")
    VW_RADIUS_MILES = _env_int("VW_RADIUS_MILES", 50)
    VW_MAX_PAGES = _env_int("VW_MAX_PAGES", 10)

    # Optional filters
    VW_MAX_PRICE = _env("VW_MAX_PRICE")
    VW_MAX_MILEAGE = _env("VW_MAX_MILEAGE")
    VW_MIN_YEAR = _env("VW_MIN_YEAR")
    VW_MODELS = _env("VW_MODELS")

    # AutoTrader valuation
    AT_BASE_URL = "https://www.autotrader.co.uk"
    AT_POSTCODE = _env("AT_POSTCODE", VW_POSTCODE)

    # Email
    SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = _env_int("SMTP_PORT", 587)
    SMTP_USER = _env("SMTP_USER")
    SMTP_PASSWORD = _env("SMTP_PASSWORD")
    EMAIL_FROM = _env("EMAIL_FROM", _env("SMTP_USER"))
    EMAIL_TO = _env("EMAIL_TO")

    @classmethod
    def get_model_filters(cls) -> list[str]:
        if not cls.VW_MODELS:
            return []
        return [m.strip().lower() for m in cls.VW_MODELS.split(",") if m.strip()]

    @classmethod
    def get_search_urls(cls) -> list[str]:
        """Build VW search URLs - one per model if filters are set, otherwise all-models."""
        base = f"{cls.VW_BASE_URL}/en/vehicle_search/volkswagen"
        model_filters = cls.get_model_filters()
        if model_filters:
            urls = []
            for model in model_filters:
                slug = MODEL_URL_SLUGS.get(model, model.replace(".", "-").replace(" ", "-"))
                urls.append(f"{base}/{slug}")
            # Also add all-models as a fallback in case model-specific URLs don't work
            urls.append(f"{base}/all-models")
            return urls
        return [f"{base}/all-models"]
