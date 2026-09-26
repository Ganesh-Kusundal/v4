from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from tradex_trading.interface.fastapi_app import create_app


class _DatalakeStore:
    def symbols(self) -> list[str]:
        return ["ACME", "OTHER"]

    def read(self, *, symbols: list[str]) -> pd.DataFrame:
        assert symbols == ["ACME"]
        return pd.DataFrame(
            [
                {"symbol": "ACME", "exchange": "BSE"},
                {"symbol": "ACME", "exchange": "BSE"},
                {"symbol": "ACME", "exchange": "NFO"},
            ]
        )


def test_datalake_symbol_search_preserves_recorded_exchanges() -> None:
    with patch(
        "tradex_interfaces.routes.chart._get_store",
        return_value=_DatalakeStore(),
    ):
        response = TestClient(create_app(session=None)).get(
            "/api/charts/symbols", params={"q": "ac", "source": "datalake"}
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "symbols": [
            {"symbol": "ACME", "exchange": "BSE"},
            {"symbol": "ACME", "exchange": "NFO"},
        ],
        "source": "datalake",
    }
