"""Single CorrelationId minting path."""

import hashlib


def mint_correlation_id(prefix: str, n: int) -> str:
    digest = hashlib.sha256(f"{prefix}:{n}".encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"
