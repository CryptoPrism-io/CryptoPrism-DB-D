"""CP-011 Phase B — point-in-time (PIT) asset universe.

No historical DMV row may be joined to today's ``crypto_listings_latest_1000``.
The universe is represented as per-asset [first_seen, last_seen] intervals and
queried per date.

Sources:
  - ``PIT_APPROX``  : intervals derived from OHLCV first/last seen. Chosen here
                      because no dated CMC listing snapshots exist in the DB/lake
                      (audited Phase A/B: only crypto_listings_latest_1000 +
                      FE_FEAR_GREED_CMC exist).
  - ``CMC_SNAPSHOT``: reserved for genuine dated CMC snapshots when they exist
                      (interface provided; not used yet).
"""

from __future__ import annotations

import pandas as pd

UNIVERSE_METHODOLOGY = "pit-approx-v1"
SOURCE_APPROX = "PIT_APPROX"
SOURCE_SNAPSHOT = "CMC_SNAPSHOT"


class PITUniverse:
    """Per-date asset universe from [first_seen, last_seen] intervals."""

    def __init__(
        self,
        intervals: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
        source: str = SOURCE_APPROX,
        version: str = UNIVERSE_METHODOLOGY,
        note: str = "",
    ) -> None:
        self.intervals = intervals
        self.source = source
        self.version = version
        self.note = note

    @property
    def assets(self) -> list[str]:
        return sorted(self.intervals)

    @property
    def n_assets(self) -> int:
        return len(self.intervals)

    def active(self, d) -> set[str]:
        """Slugs active on date ``d`` (first_seen <= d <= last_seen)."""
        d = pd.Timestamp(d).normalize()
        return {
            slug
            for slug, (first, last) in self.intervals.items()
            if first <= d <= last
        }

    def coverage(self) -> dict:
        """Coverage summary: assets, date span, per-year active counts."""
        years: dict[int, int] = {}
        if self.intervals:
            lo = min(first for first, _ in self.intervals.values())
            hi = max(last for _, last in self.intervals.values())
            for y in range(lo.year, hi.year + 1):
                start = pd.Timestamp(year=y, month=1, day=1)
                end = pd.Timestamp(year=y, month=12, day=31)
                years[y] = sum(
                    1
                    for _, (first, last) in self.intervals.items()
                    if first <= end and last >= start
                )
            span = f"{lo.date()}..{hi.date()}"
        else:
            span = None
        return {
            "source": self.source,
            "version": self.version,
            "n_assets": self.n_assets,
            "span": span,
            "active_by_year": years,
            "note": self.note,
        }

    @classmethod
    def from_ohlcv(
        cls,
        df: pd.DataFrame,
        *,
        slug_col: str = "slug",
        date_col: str = "timestamp",
    ) -> "PITUniverse":
        """PIT_APPROX: per-asset [first_seen, last_seen] from OHLCV."""
        work = df.copy()
        work[date_col] = pd.to_datetime(work[date_col]).dt.tz_localize(None)
        agg = (
            work.groupby(slug_col)[date_col]
            .agg(["min", "max"])
            .reset_index()
        )
        intervals = {
            row[slug_col]: (
                pd.Timestamp(row["min"]).normalize(),
                pd.Timestamp(row["max"]).normalize(),
            )
            for row in agg.to_dict("records")
        }
        return cls(intervals, source=SOURCE_APPROX, version=UNIVERSE_METHODOLOGY)

    @classmethod
    def from_cmc_snapshots(
        cls,
        snapshots: pd.DataFrame,
        *,
        slug_col: str = "slug",
        date_col: str = "snapshot_date",
    ) -> "PITUniverse":
        """Reserved: build intervals from dated CMC listing snapshots when available."""
        agg = (
            snapshots.groupby(slug_col)[date_col]
            .agg(["min", "max"])
            .reset_index()
        )
        intervals = {
            row[slug_col]: (
                pd.Timestamp(row["min"]).normalize(),
                pd.Timestamp(row["max"]).normalize(),
            )
            for row in agg.to_dict("records")
        }
        return cls(intervals, source=SOURCE_SNAPSHOT, version="cmc-snapshot-v1")
