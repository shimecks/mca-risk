"""Figure data: read-only queries over the persisted metric tables (Part 4 + T1).

Nothing here defines a metric. Every value is a column of ``portfolio_metrics``, ``target_metrics`` or
``relationship_metrics`` (written by ``mcarisk.metrics``); the only additions are Wilson intervals on
counts that the tables already hold, and the P7 full-cohort horizon (a censoring marker for plotting).
Each frame is written to ``build/figures/data/<name>.csv`` so the Part 7 app can re-render it.
"""

from __future__ import annotations

from collections.abc import Callable

import duckdb
import pandas as pd

from mcarisk.analytics.stats import wilson_ci

MASKING_SQL = """
SELECT segment,
       MAX(value) FILTER (WHERE metric = 'reported_collection_rate') AS reported_rate,
       MAX(value) FILTER (WHERE metric = 'adjusted_collection_rate') AS adjusted_rate,
       MAX(value) FILTER (WHERE metric = 'renewal_masking_gap_pp') AS masking_gap_pp,
       MAX(value) FILTER (WHERE metric = 'internal_payoff_share') AS internal_payoff_share,
       MAX(denominator) FILTER (WHERE metric = 'reported_collection_rate') AS closed_rtr_cents
FROM portfolio_metrics
WHERE metric_id IN ('P1', 'P2', 'P3') AND k IS NULL
GROUP BY segment
ORDER BY segment = 'all', segment
"""

BAD_RATE_SQL = """
WITH t AS (
    SELECT segment AS vintage, maturity, n_deals,
           MAX(value) FILTER (WHERE metric = 'bad_rate_economic') AS t1_economic,
           MAX(numerator) FILTER (WHERE metric = 'bad_rate_economic') AS t1_economic_bad,
           MAX(value) FILTER (WHERE metric = 'bad_rate_naive') AS t1_naive,
           MAX(numerator) FILTER (WHERE metric = 'bad_rate_naive') AS t1_naive_bad,
           MAX(denominator) AS t1_mature_deals
    FROM target_metrics WHERE metric_id = 'T1' AND dimension = 'vintage'
    GROUP BY segment, maturity, n_deals
),
p AS (
    SELECT segment AS vintage,
           MAX(value) FILTER (WHERE metric = 'bad_rate_economic') AS closed_economic,
           MAX(numerator) FILTER (WHERE metric = 'bad_rate_economic') AS closed_economic_bad,
           MAX(value) FILTER (WHERE metric = 'bad_rate_naive') AS closed_naive,
           MAX(numerator) FILTER (WHERE metric = 'bad_rate_naive') AS closed_naive_bad,
           MAX(denominator) AS closed_deals
    FROM portfolio_metrics WHERE metric_id = 'P4' AND segment <> 'all'
    GROUP BY segment
)
SELECT * FROM t FULL JOIN p USING (vintage) ORDER BY vintage
"""

EXPOSURE_SQL = """
SELECT metric, value AS cents, denominator AS relationships
FROM portfolio_metrics WHERE metric_id = 'P6' ORDER BY metric DESC
"""

CURVES_SQL = """
WITH coh AS (  -- months every relationship of the cohort has been observed (censoring marker)
    SELECT first_funding_quarter AS segment, COUNT(*) AS relationships,
           MAX(first_funding_date) AS last_first_funding, MAX(as_of) AS as_of
    FROM relationship_metrics GROUP BY 1
)
SELECT p.segment AS vintage, p.k AS month, p.value AS cash_multiple, p.numerator AS cash_in_cents,
       p.denominator AS cash_out_cents, coh.relationships,
       p.k <= date_diff('month', coh.last_first_funding, coh.as_of)
         - CASE WHEN day(coh.as_of) < day(coh.last_first_funding) THEN 1 ELSE 0 END AS full_cohort
FROM portfolio_metrics p JOIN coh USING (segment)
WHERE p.metric_id = 'P7'
ORDER BY vintage, month
"""

REFI_SQL = """
SELECT metric_id, metric, segment, value, numerator, denominator
FROM portfolio_metrics WHERE metric_id IN ('P5', 'P8', 'P9') AND unit = 'ratio'
  AND metric IN ('resolved_bad_share', 'distressed_share_of_renewals', 'bad_rate_chains_with_distressed',
                 'bad_rate_chains_without_distressed', 'modification_cure_rate')
ORDER BY metric_id, metric, segment
"""

SEGMENTS_SQL = """
SELECT dimension, segment, value AS t1_economic, numerator AS bad, denominator AS mature_deals, n_deals
FROM target_metrics
WHERE metric_id = 'T1' AND metric = 'bad_rate_economic' AND dimension IN ('industry', 'iso', 'positions', 'burden')
ORDER BY dimension, segment
"""


def _with_wilson(df: pd.DataFrame, k: str, n: str, prefix: str) -> pd.DataFrame:
    lo, hi = [], []
    for kk, nn in zip(df[k], df[n], strict=True):
        if pd.isna(kk) or pd.isna(nn):
            a, b = None, None
        else:
            a, b = wilson_ci(int(kk), int(nn))
        lo.append(a)
        hi.append(b)
    df[f"{prefix}_ci_lo"] = [float("nan") if x is None else x for x in lo]
    df[f"{prefix}_ci_hi"] = [float("nan") if x is None else x for x in hi]
    return df


def masking(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(MASKING_SQL).df()


def bad_rate_vintage(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute(BAD_RATE_SQL).df()
    df = _with_wilson(df, "t1_economic_bad", "t1_mature_deals", "t1_economic")
    return _with_wilson(df, "closed_economic_bad", "closed_deals", "closed_economic")


def exposure(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(EXPOSURE_SQL).df()


def vintage_curves(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(CURVES_SQL).df()


def refi_quality(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return _with_wilson(con.execute(REFI_SQL).df(), "numerator", "denominator", "rate")


def segments(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return _with_wilson(con.execute(SEGMENTS_SQL).df(), "bad", "mature_deals", "t1_economic")


DATASETS: dict[str, Callable[[duckdb.DuckDBPyConnection], pd.DataFrame]] = {
    "masking": masking,
    "bad_rate_vintage": bad_rate_vintage,
    "exposure": exposure,
    "vintage_curves": vintage_curves,
    "refi_quality": refi_quality,
    "segments": segments,
}
