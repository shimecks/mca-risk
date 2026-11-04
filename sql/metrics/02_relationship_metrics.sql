-- Relationship-level metrics R1-R10 (METRICS.md §3), as of a date.
-- SQL mirror of src/mcarisk/metrics/relationship.py. Input: deal_metrics_sql(as_of, attempt_basis).
CREATE OR REPLACE MACRO relationship_metrics_sql(as_of, attempt_basis) AS TABLE
WITH
dm AS (
    SELECT *, row_number() OVER (PARTITION BY merchant_key ORDER BY funding_date DESC, deal_id DESC) AS rn_last
    FROM deal_metrics_sql(as_of, attempt_basis)
),
g AS (
    SELECT merchant_key,
           MIN(funding_date) AS first_funding_date,
           SUM(d6_funder_cash_out_cents) AS r1,
           SUM(d7_cash_from_merchant_cents) AS r2,
           COUNT(*) AS n_deals,
           COUNT(*) FILTER (WHERE COALESCE(d17_distressed, false)) AS n_distressed,
           COUNT(*) FILTER (WHERE NOT commission_known) AS n_commission_unknown,
           SUM(d9_reported_collected_cents) AS reported_collected
    FROM dm GROUP BY merchant_key
),
x AS (
    SELECT g.*, l.deal_id AS last_deal_id, l.outcome AS last_outcome, l.outcome_date AS last_outcome_date,
           l.remaining_rtr_cents AS last_remaining,
           CASE WHEN l.outcome IN ('paid_in_full', 'paid_in_full_early', 'settled', 'written_off')
                THEN 'resolved' ELSE 'open' END AS r5
    FROM g JOIN dm l ON l.merchant_key = g.merchant_key AND l.rn_last = 1
)
SELECT
    merchant_key,
    CAST(as_of AS DATE) AS as_of,
    first_funding_date,
    CAST(year(first_funding_date) AS VARCHAR) || 'Q' || CAST(quarter(first_funding_date) AS VARCHAR)
        AS first_funding_quarter,
    last_deal_id,
    last_outcome,
    r1 AS r1_funder_cash_out_cents,
    r2 AS r2_cash_from_merchant_cents,
    CASE WHEN r1 <> 0 THEN r2 / r1 END AS r3_cash_multiple,
    r2 - r1 AS r4_net_cash_position_cents,
    r5 AS r5_status,
    CASE WHEN r5 = 'open' THEN GREATEST(0, r1 - r2) END AS r6_economic_exposure_cents,
    CASE WHEN r5 = 'open' THEN last_remaining END AS r7_contractual_balance_cents,
    CASE WHEN r5 = 'resolved' THEN GREATEST(0, r1 - r2) END AS r8_realized_loss_cents,
    (r5 = 'resolved' AND r1 <> 0 AND r2 / r1 < 1.0) AS r9_relationship_bad,
    n_deals AS r10_n_deals,
    n_distressed AS r10_n_distressed_refi,
    CASE WHEN r5 = 'resolved' AND last_outcome_date IS NOT NULL
         THEN date_diff('day', first_funding_date, last_outcome_date) END AS r10_days_to_resolution,
    n_commission_unknown,
    reported_collected AS reported_collected_cents
FROM x
ORDER BY merchant_key;
