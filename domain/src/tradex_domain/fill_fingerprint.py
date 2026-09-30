"""Single fill fingerprinting path."""

import hashlib

from tradex_domain.execution import Fill


def fingerprint(fill: Fill) -> str:
    key = "|".join(
        [
            str(fill.fill_id or ""),
            str(fill.order_id),
            str(fill.side),
            str(fill.quantity),
            str(fill.price),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]
