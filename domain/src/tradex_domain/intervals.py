"""Single interval normalization path."""


def normalize_interval(raw: str) -> str:
    if raw == "1d":
        return "D"
    return raw
