"""ISO 4217 alpha -> numeric. MarketPay wants the numeric code, as a string."""

ALPHA_TO_NUMERIC: dict[str, str] = {
    "SEK": "752",
    "EUR": "978",
    "DKK": "208",
    "NOK": "578",
}


def to_numeric(alpha: str) -> str:
    try:
        return ALPHA_TO_NUMERIC[alpha]
    except KeyError:
        raise ValueError(f"unsupported currency {alpha!r}") from None
