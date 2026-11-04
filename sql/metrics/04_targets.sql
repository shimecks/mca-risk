-- T1 fixed-horizon target (METRICS.md §5) and the origination segment attributes, as SQL table macros.
-- Mirror of src/mcarisk/metrics/targets.py and attributes.py (tests assert Python == SQL).
--   deal_attributes_sql()                                   one row per deal: industry, iso, positions, burden
--   t1_targets_sql(as_of, attempt_basis, horizon_bdays)     one row per deal funded on or before as_of
--   t1_metrics_sql(as_of, attempt_basis, horizon_bdays)     T1 bad rates by dimension x segment (long)
-- T1 = D19 of the deal evaluated as of funding + horizon_bdays Fed ACH business days (deal_metrics_at_sql);
-- only deals whose horizon date is on or before as_of are mature and carry a target.

CREATE OR REPLACE MACRO deal_attributes_sql() AS TABLE
WITH x AS (
    SELECT d.deal_id,
           COALESCE(d.payment_cents, d.payment_cents_ach) AS pay,
           CASE WHEN COALESCE(d.frequency, d.frequency_ach) = 'weekly' THEN 5 ELSE 1 END AS div,
           COALESCE(d.industry, s.industry) AS industry,
           d.iso_name,
           COALESCE(d.positions, s.positions) AS positions,
           COALESCE(d.avg_monthly_rev_cents, s.avg_monthly_rev_cents) AS rev,
           CASE WHEN COALESCE(d.tracker_missing, false) THEN 'none'
                WHEN s.scorecard_id IS NOT NULL THEN 'tracker+scorecard'
                ELSE 'tracker' END AS attr_source
    FROM deals_resolved d
    LEFT JOIN scorecard_links l ON l.deal_id = d.deal_id
    LEFT JOIN scorecards_std s ON s.scorecard_id = l.scorecard_id
)
SELECT deal_id,
       COALESCE(industry, 'unknown') AS industry,
       COALESCE(iso_name, 'unknown') AS iso,
       CASE WHEN positions IS NULL THEN 'unknown' WHEN positions >= 3 THEN '3+'
            ELSE CAST(positions AS VARCHAR) END AS positions,
       CASE WHEN pay IS NULL OR COALESCE(rev, 0) = 0 THEN 'unknown'
            WHEN pay * 21 * 100 < 15 * div * rev THEN '<15%'
            WHEN pay * 21 * 100 < 20 * div * rev THEN '15-20%'
            WHEN pay * 21 * 100 < 25 * div * rev THEN '20-25%'
            WHEN pay * 21 * 100 < 35 * div * rev THEN '25-35%'
            ELSE '>=35%' END AS burden,
       positions AS positions_n,
       rev AS avg_monthly_rev_cents,
       CASE WHEN pay IS NULL OR COALESCE(rev, 0) = 0 THEN NULL
            ELSE CAST(pay * 21 AS DOUBLE) / CAST(div * rev AS DOUBLE) END AS burden_value,
       attr_source
FROM x
ORDER BY deal_id;

CREATE OR REPLACE MACRO t1_targets_sql(as_of, attempt_basis, horizon_bdays) AS TABLE
SELECT d.deal_id,
       d.merchant_key,
       d.funding_date,
       CAST(year(d.funding_date) AS VARCHAR) || 'Q' || CAST(quarter(d.funding_date) AS VARCHAR) AS vintage,
       CAST(horizon_bdays AS BIGINT) AS horizon_bdays,
       ch.d AS horizon_date,
       (ch.d <= as_of) AS mature,
       m.outcome AS outcome_at_horizon,
       m.d19_bad_economic AS t1_bad_economic,
       m.d18_bad_naive AS t1_bad_naive,
       CAST(as_of AS DATE) AS as_of
FROM deals_resolved d
JOIN cal_sched c0 ON c0.code = 'daily' AND c0.d = d.funding_date
LEFT JOIN cal_sched ch ON ch.code = 'daily' AND ch.is_sched AND ch.n = c0.n + horizon_bdays
LEFT JOIN deal_metrics_at_sql(as_of, attempt_basis, horizon_bdays) m ON m.deal_id = d.deal_id
WHERE d.funding_date <= as_of
ORDER BY d.deal_id;

CREATE OR REPLACE MACRO t1_metrics_sql(as_of, attempt_basis, horizon_bdays) AS TABLE
WITH t AS (SELECT * FROM t1_targets_sql(as_of, attempt_basis, horizon_bdays)),
a AS (SELECT * FROM deal_attributes_sql()),
g AS (
    SELECT 'all' AS dimension, 'all' AS segment, t.* FROM t
    UNION ALL SELECT 'vintage', t.vintage, t.* FROM t
    UNION ALL SELECT 'industry', COALESCE(a.industry, 'unknown'), t.* FROM t LEFT JOIN a USING (deal_id)
    UNION ALL SELECT 'iso', COALESCE(a.iso, 'unknown'), t.* FROM t LEFT JOIN a USING (deal_id)
    UNION ALL SELECT 'positions', COALESCE(a.positions, 'unknown'), t.* FROM t LEFT JOIN a USING (deal_id)
    UNION ALL SELECT 'burden', COALESCE(a.burden, 'unknown'), t.* FROM t LEFT JOIN a USING (deal_id)
),
agg AS (
    SELECT dimension, segment, COUNT(*) AS n_deals,
           COUNT(*) FILTER (WHERE mature) AS n_m,
           COUNT(*) FILTER (WHERE mature AND t1_bad_economic) AS eb,
           COUNT(*) FILTER (WHERE mature AND t1_bad_naive) AS nb
    FROM g GROUP BY dimension, segment
),
rows_ AS (
    SELECT 'bad_rate_economic' AS metric, dimension, segment, eb AS num, n_m, n_deals FROM agg
    UNION ALL SELECT 'bad_rate_naive', dimension, segment, nb, n_m, n_deals FROM agg
)
SELECT 'T1' AS metric_id, metric, dimension, segment,
       CASE WHEN n_m > 0 THEN CAST(num AS DOUBLE) / n_m END AS value,
       CAST(num AS BIGINT) AS numerator, CAST(n_m AS BIGINT) AS denominator, CAST(n_deals AS BIGINT) AS n_deals,
       CASE WHEN n_m = n_deals THEN 'mature' WHEN n_m = 0 THEN 'immature' ELSE 'partial' END AS maturity,
       CAST(horizon_bdays AS BIGINT) AS horizon_bdays,
       CAST(as_of AS DATE) AS as_of
FROM rows_
ORDER BY dimension, segment, metric;
