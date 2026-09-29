"""Single order ID minting path."""
import uuid


def mint_order_id() -> str:
    return str(uuid.uuid4())
