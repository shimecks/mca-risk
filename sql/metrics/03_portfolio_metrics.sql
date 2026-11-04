-- Portfolio metrics P1-P9 (METRICS.md §4), as of a date. Long format, one row per
-- (metric_id, metric, segment, k). SQL mirror of src/mcarisk/metrics/portfolio.py.
CREATE OR REPLACE MACRO portfolio_metrics_sql(as_of, attempt_basis) AS TABLE
WITH
dm AS (SELECT * FROM deal_metrics_sql(as_of, attempt_basis)),
rm AS (SELECT * FROM relationship_metrics_sql(as_of, attempt_basis)),
closed AS (SELECT * FROM dm WHERE is_closed),
p1 AS (
    SELECT SUM(d9_reported_collected_cents) AS rep, SUM(d7_cash_from_merchant_cents) AS adj, SUM(d2_rtr_cents) AS rtr
    FROM closed WHERE d2_rtr_cents IS NOT NULL
),
p3 AS (SELECT SUM(d8_internal_payoff_out_cents) AS d8, SUM(d9_reported_collected_cents) AS d9 FROM dm),
p4g AS (
    SELECT 'all' AS segment, * FROM closed
    UNION ALL SELECT vintage, * FROM closed
),
p4 AS (
    SELECT segment, COUNT(*) AS n, COUNT(*) FILTER (WHERE d18_bad_naive) AS nb,
           COUNT(*) FILTER (WHERE d19_bad_economic) AS eb
    FROM p4g GROUP BY segment
),
res AS (SELECT * FROM rm WHERE r5_status = 'resolved'),
p5 AS (
    SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE r9_relationship_bad) AS nbad,
           COALESCE(SUM(r1_funder_cash_out_cents), 0) AS r1, COALESCE(SUM(r2_cash_from_merchant_cents), 0) AS r2,
           COALESCE(SUM(r8_realized_loss_cents), 0) AS r8
    FROM res
),
p6 AS (
    SELECT COUNT(*) AS n, COALESCE(SUM(r6_economic_exposure_cents), 0) AS r6,
           COALESCE(SUM(r7_contractual_balance_cents), 0) AS r7,
           COUNT(*) FILTER (WHERE r7_contractual_balance_cents IS NULL) AS n_unk
    FROM rm WHERE r5_status = 'open'
),
ev AS (
    SELECT l.merchant_key, l.event_date, l.kind, l.amount_cents
    FROM ledger l WHERE l.event_date <= as_of AND l.is_cash
),
months AS (
    SELECT rm.merchant_key, rm.first_funding_quarter AS q, k.k,
           CAST(rm.first_funding_date + to_months(CAST(k.k AS INTEGER)) AS DATE) AS cut
    FROM rm, range(0, 1200) k(k)
    WHERE CAST(rm.first_funding_date + to_months(CAST(k.k AS INTEGER)) AS DATE) <= as_of
),
p7 AS (
    SELECT m.q, m.k,
           COALESCE(SUM(ev.amount_cents) FILTER (WHERE ev.kind IN ('debit', 'return', 'early_payoff_cash', 'settlement')), 0) AS cin,
           -COALESCE(SUM(ev.amount_cents) FILTER (WHERE ev.kind IN ('funding_out', 'commission_out')), 0) AS cout
    FROM months m LEFT JOIN ev ON ev.merchant_key = m.merchant_key AND ev.event_date <= m.cut
    GROUP BY m.q, m.k
),
ren AS (
    SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE d17_distressed) AS nd FROM dm WHERE d17_distressed IS NOT NULL
),
p8 AS (
    SELECT 'bad_rate_chains_with_distressed' AS metric, COUNT(*) AS n, COUNT(*) FILTER (WHERE r9_relationship_bad) AS nb
    FROM res WHERE r10_n_distressed_refi > 0
    UNION ALL
    SELECT 'bad_rate_chains_without_distressed', COUNT(*), COUNT(*) FILTER (WHERE r9_relationship_bad)
    FROM res WHERE r10_n_distressed_refi = 0
),
p9 AS (
    SELECT 'all' AS segment, COUNT(*) AS n,
           COUNT(*) FILTER (WHERE outcome IN ('paid_in_full', 'renewed_healthy')) AS nc
    FROM dm WHERE has_modification
    UNION ALL
    SELECT 'closed', COUNT(*), COUNT(*) FILTER (WHERE outcome IN ('paid_in_full', 'renewed_healthy'))
    FROM dm WHERE has_modification AND is_closed
),
rows_ AS (
    SELECT 'P1' AS metric_id, 'reported_collection_rate' AS metric, 'all' AS segment, CAST(NULL AS BIGINT) AS k,
           CASE WHEN rtr <> 0 THEN rep / rtr END AS value, rep AS numerator, rtr AS denominator, 'ratio' AS unit
    FROM p1
    UNION ALL SELECT 'P1', 'adjusted_collection_rate', 'all', NULL, CASE WHEN rtr <> 0 THEN adj / rtr END, adj, rtr, 'ratio' FROM p1
    UNION ALL SELECT 'P2', 'renewal_masking_gap_pp', 'all', NULL,
                     CASE WHEN rtr <> 0 THEN (rep / rtr - adj / rtr) * 100 END, rep - adj, rtr, 'pp' FROM p1
    UNION ALL SELECT 'P3', 'internal_payoff_share', 'all', NULL, CASE WHEN d9 <> 0 THEN d8 / d9 END, d8, d9, 'ratio' FROM p3
    UNION ALL SELECT 'P4', 'bad_rate_naive', segment, NULL, CASE WHEN n <> 0 THEN nb / n END, nb, n, 'ratio' FROM p4
    UNION ALL SELECT 'P4', 'bad_rate_economic', segment, NULL, CASE WHEN n <> 0 THEN eb / n END, eb, n, 'ratio' FROM p4
    UNION ALL SELECT 'P5', 'resolved_bad_share', 'all', NULL, CASE WHEN n <> 0 THEN nbad / n END, nbad, n, 'ratio' FROM p5
    UNION ALL SELECT 'P5', 'capital_weighted_loss_rate', 'all', NULL, CASE WHEN r1 <> 0 THEN r8 / r1 END, r8, r1, 'ratio' FROM p5
    UNION ALL SELECT 'P5', 'resolved_cash_multiple', 'all', NULL, CASE WHEN r1 <> 0 THEN r2 / r1 END, r2, r1, 'ratio' FROM p5
    UNION ALL SELECT 'P6', 'economic_exposure_cents', 'all', NULL, r6, r6, n, 'cents' FROM p6
    UNION ALL SELECT 'P6', 'contractual_balance_cents', 'all', NULL, r7, r7, n - n_unk, 'cents' FROM p6
    UNION ALL SELECT 'P7', 'cumulative_cash_multiple', q, k, CASE WHEN cout <> 0 THEN cin / cout END, cin, cout, 'ratio' FROM p7
    UNION ALL SELECT 'P8', 'distressed_share_of_renewals', 'all', NULL, CASE WHEN n <> 0 THEN nd / n END, nd, n, 'ratio' FROM ren
    UNION ALL SELECT 'P8', metric, 'all', NULL, CASE WHEN n <> 0 THEN nb / n END, nb, n, 'ratio' FROM p8
    UNION ALL SELECT 'P9', 'modification_cure_rate', segment, NULL, CASE WHEN n <> 0 THEN nc / n END, nc, n, 'ratio' FROM p9
)
SELECT metric_id, metric, segment, k, CAST(value AS DOUBLE) AS value, CAST(numerator AS BIGINT) AS numerator,
       CAST(denominator AS BIGINT) AS denominator, unit, CAST(as_of AS DATE) AS as_of
FROM rows_
ORDER BY metric_id, metric, segment, k;
