"""Pydantic request models for the gateway REST surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HistoryRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    instrument_id: str
    timeframe: str = "1m"
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    fill_gaps: bool | None = None


class HistoryBatchRequest(BaseModel):
    requests: list[HistoryRequest] = Field(min_length=1, max_length=200)
    fill_gaps: bool = False


class QuotesRequest(BaseModel):
    instruments: list[str] = Field(min_length=1, max_length=500)


class SubscribeMessage(BaseModel):
    op: Literal["subscribe"]
    instruments: list[str] = Field(min_length=1)
    mode: Literal["quote", "depth"] = "quote"
    levels: int = Field(default=5, ge=1, le=30)


class UnsubscribeMessage(BaseModel):
    op: Literal["unsubscribe"]
    subscription_id: str | None = None
    instruments: list[str] | None = None
