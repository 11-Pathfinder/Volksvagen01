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


    # Map of user-friendly model names to VW API MODEL_TYPE_LST values
    MODEL_SLUG_MAP = {
        "id.3": "VOLKSWAGEN_ID_3",
        "id.4": "VOLKSWAGEN_ID_4",
        "id.5": "VOLKSWAGEN_ID_5",
        "id.7": "VOLKSWAGEN_ID_7",
        "id.7 tourer": "VOLKSWAGEN_ID_7_TOURER",
        "id. buzz": "VOLKSWAGEN_ID_BUZZ",
        "golf": "VOLKSWAGEN_GOLF",
        "golf gti": "VOLKSWAGEN_GOLF_GTI",
        "golf gte": "VOLKSWAGEN_GOLF_GTE",
        "golf gtd": "VOLKSWAGEN_GOLF_GTD",
        "golf r": "VOLKSWAGEN_GOLF_R",
        "golf estate": "VOLKSWAGEN_GOLF_ESTATE",
        "golf sv": "VOLKSWAGEN_GOLF_SV",
        "polo": "VOLKSWAGEN_POLO",
        "t-roc": "VOLKSWAGEN_T_ROC",
        "t-roc cabriolet": "VOLKSWAGEN_T_ROC_CABRIOLET",
        "t-cross": "VOLKSWAGEN_T_CROSS",
        "tiguan": "VOLKSWAGEN_TIGUAN",
        "tiguan allspace": "VOLKSWAGEN_TIGUAN_ALLSPACE",
        "touareg": "VOLKSWAGEN_TOUAREG",
        "touran": "VOLKSWAGEN_TOURAN",
        "tayron": "VOLKSWAGEN_TAYRON",
        "taigo": "VOLKSWAGEN_TAIGO",
        "passat": "VOLKSWAGEN_PASSAT",
        "passat estate": "VOLKSWAGEN_PASSAT_ESTATE",
        "arteon": "VOLKSWAGEN_ARTEON",
        "arteon shooting brake": "VOLKSWAGEN_ARTEON_SHOOTING_BRAKE",
        "multivan": "VOLKSWAGEN_MULTIVAN",
        "up!": "VOLKSWAGEN_UP",
        "e-up!": "VOLKSWAGEN_E_UP",
        "e-golf": "VOLKSWAGEN_E_GOLF",
        "sharan": "VOLKSWAGEN_SHARAN",
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
    def get_search_url(cls) -> str:
        """Build the VW search URL, filtering by model if configured."""
        model_filters = cls.get_model_filters()
        if not model_filters:
            return f"{cls.VW_BASE_URL}/en/vehicle_search/volkswagen/all-models"

        # Build MODEL_TYPE_LST query params for server-side filtering
        model_params = []
        for name in model_filters:
            slug = MODEL_SLUG_MAP.get(name)
            if slug:
                model_params.append(f"MODEL_TYPE_LST={slug}")

        if model_params:
            params = "&".join(model_params)
            return f"{cls.VW_BASE_URL}/en/vehicle_search/volkswagen/all-models?{params}"

        return f"{cls.VW_BASE_URL}/en/vehicle_search/volkswagen/all-models"
