"""Shared pydantic models for the structured query produced by query_parser.py
and consumed by source_router.py, scraper.py, connectors, and dataset_export.py.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

DataType = Literal["image", "text", "structured"]
OutputMode = Literal["preview", "dataset"]
Resolution = Literal["low", "medium", "high"]
Orientation = Literal["landscape", "portrait", "square"]


class DateRange(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None

    model_config = {"populate_by_name": True}


class QueryFilters(BaseModel):
    resolution: Optional[Resolution] = None
    orientation: Optional[Orientation] = None
    no_watermark: bool = False
    date_range: Optional[DateRange] = None
    domain_allowlist: list[str] = Field(default_factory=list)
    language: Optional[str] = None
    # Off by default: each run gets a fair, independent shot at the full
    # result pool, unaffected by anything fetched in earlier runs. Turning
    # this on skips anything ever fetched before (any run, any query) too —
    # useful for deliberately building one non-repeating library over time,
    # but means a repeated/refined query can return far fewer results, since
    # APIs like Pexels return the same top-ranked items for a similar search
    # every time.
    dedupe_across_runs: bool = False


class StructuredQuery(BaseModel):
    data_type: DataType
    keywords: list[str] = Field(default_factory=list)
    count: int = Field(default=20, gt=0, le=1000)
    filters: QueryFilters = Field(default_factory=QueryFilters)
    output_mode: OutputMode = "preview"
    label: str = "dataset"
    notes: Optional[str] = None

    def search_terms(self) -> str:
        return " ".join(self.keywords) if self.keywords else (self.notes or "")


class SplitRatios(BaseModel):
    train: float = 0.8
    val: float = 0.1
    test: float = 0.1

    def normalized(self) -> "SplitRatios":
        total = self.train + self.val + self.test
        if total <= 0:
            return SplitRatios()
        return SplitRatios(train=self.train / total, val=self.val / total, test=self.test / total)


class ExportOptions(BaseModel):
    split: SplitRatios = Field(default_factory=SplitRatios)
    resize: Optional[tuple[int, int]] = None
    seed: int = 42
