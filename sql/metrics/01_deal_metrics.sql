-- Deal-level metrics D1-D19 and the METRICS.md §2.1 outcome, as of a date.
-- SQL mirror of src/mcarisk/metrics/deal.py (tests/test_metrics_sql.py asserts Python == SQL).
-- Inputs: deals_resolved, ledger, deal_status_events, cal_sched.
--   deal_metrics_sql(as_of DATE, attempt_basis BOOLEAN)
--   attempt_basis = false: D17 uses only returns posted on or before the renewal date (literal reading)
--   attempt_basis = true : D17 also counts later returns (up to as_of) of debits attempted by then
--   deal_metrics_at_sql(as_of DATE, attempt_basis BOOLEAN, horizon_bdays INTEGER)
--   horizon_bdays < 0 : every deal is evaluated as of `as_of` (deal_metrics_sql is this with -1)
--   horizon_bdays >= 0: each deal is evaluated as of its own date, funding + horizon_bdays Fed ACH business
--                       days (output column `as_of`), only for deals whose horizon date is on or before
--                       `as_of` (METRICS.md §5 T1 uses 180). Mirror of mcarisk.metrics.targets.
CREATE OR REPLACE MACRO deal_metrics_at_sql(as_of, attempt_basis, horizon_bdays) AS TABLE
WITH
dd AS (  -- deal inputs (tracker terms, or the ACH reconstruction for tracker-missing deals)
    SELECT deal_id, merchant_key, funding_date, advance_cents, rtr_cents AS rtr_tracker, fee_cents,
           commission_rate, COALESCE(payment_cents, payment_cents_ach) AS payment_cents,
           COALESCE(frequency, frequency_ach) AS frequency, COALESCE(tracker_missing, false) AS tracker_missing
    FROM deals_resolved
),
d0 AS (  -- schedule code: daily, or w<weekday of the first business day after funding>; per-deal as-of aod
    SELECT dd.*,
           CASE WHEN horizon_bdays < 0 THEN CAST(as_of AS DATE) ELSE ch.d END AS aod,
           CASE WHEN dd.frequency = 'weekly' THEN 'w' || CAST(isodow(c1.d) - 1 AS VARCHAR) ELSE 'daily' END AS code,
           CASE WHEN dd.frequency = 'weekly' THEN 4 ELSE 20 END AS k16,
           CASE WHEN dd.frequency = 'weekly' THEN 5 ELSE 1 END AS div
    FROM dd
    JOIN cal_sched c0 ON c0.code = 'daily' AND c0.d = dd.funding_date
    JOIN cal_sched c1 ON c1.code = 'daily' AND c1.is_sched AND c1.n = c0.n + 1
    LEFT JOIN cal_sched ch ON horizon_bdays >= 0 AND ch.code = 'daily' AND ch.is_sched
         AND ch.n = c0.n + horizon_bdays
    WHERE dd.funding_date <= as_of
),
d AS (SELECT * FROM d0 WHERE aod IS NOT NULL AND aod <= as_of),
e AS (SELECT l.* FROM ledger l JOIN d USING (deal_id) WHERE l.event_date <= d.aod),
st AS (SELECT s.* FROM deal_status_events s JOIN d USING (deal_id) WHERE s.event_date <= d.aod),
ret AS (SELECT matched_event_id AS debit_id FROM e WHERE kind = 'return' AND matched_event_id IS NOT NULL),
deb AS (
    SELECT e.deal_id, e.event_id, e.event_date, e.amount_cents, (r.debit_id IS NOT NULL) AS returned,
           row_number() OVER (PARTITION BY e.deal_id ORDER BY e.event_date DESC, e.event_id DESC) AS rn_desc
    FROM e LEFT JOIN ret r ON r.debit_id = e.event_id
    WHERE e.kind = 'debit'
),
firsts AS (  -- first event of each kind (by date, then event id)
    SELECT * FROM (
        SELECT deal_id, kind, event_date, successor_deal_id,
               row_number() OVER (PARTITION BY deal_id, kind ORDER BY event_date, event_id) AS rn
        FROM e WHERE kind IN ('internal_payoff', 'early_payoff_cash', 'write_off')
    ) WHERE rn = 1
),
agg AS (
    SELECT d.deal_id,
           -COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind = 'funding_out'), 0) AS d4,
           -COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind = 'commission_out'), 0) AS comm,
           COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind IN ('debit', 'return', 'early_payoff_cash', 'settlement')), 0) AS d7,
           COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind = 'internal_payoff'), 0) AS d8,
           COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind = 'settlement'), 0) AS settle_recv,
           COALESCE(SUM(e.amount_cents) FILTER (WHERE e.kind = 'write_off'), 0) AS wo_sum,
           COALESCE(BOOL_OR(e.kind = 'modification'), false) AS has_mod
    FROM d LEFT JOIN e USING (deal_id) GROUP BY d.deal_id
),
dstat AS (
    SELECT d.deal_id,
           COUNT(deb.event_id) AS n_debits,
           MAX(deb.event_date) FILTER (WHERE NOT deb.returned) AS last_good,
           COUNT(*) FILTER (WHERE deb.rn_desc <= 20) AS att20,
           COUNT(*) FILTER (WHERE deb.rn_desc <= 20 AND deb.returned) AS ret20,
           MAX(deb.event_date) FILTER (WHERE deb.rn_desc = 1) AS last_debit_date,
           BOOL_OR(deb.returned) FILTER (WHERE deb.rn_desc = 1) AS last_debit_returned
    FROM d LEFT JOIN deb USING (deal_id) GROUP BY d.deal_id
),
base AS (
    SELECT d.*, agg.* EXCLUDE (deal_id), dstat.* EXCLUDE (deal_id),
           ipo.event_date AS ipo_date, ipo.successor_deal_id, epo.event_date AS epo_date, wo.event_date AS wo_date,
           (SELECT COUNT(*) FROM st WHERE st.deal_id = d.deal_id) AS n_status,
           stop.d AS stop_date
    FROM d
    JOIN agg USING (deal_id)
    JOIN dstat USING (deal_id)
    LEFT JOIN firsts ipo ON ipo.deal_id = d.deal_id AND ipo.kind = 'internal_payoff'
    LEFT JOIN firsts epo ON epo.deal_id = d.deal_id AND epo.kind = 'early_payoff_cash'
    LEFT JOIN firsts wo ON wo.deal_id = d.deal_id AND wo.kind = 'write_off'
    LEFT JOIN cal_sched s0 ON s0.code = d.code AND s0.d = dstat.last_debit_date
    LEFT JOIN cal_sched stop ON stop.code = d.code AND stop.is_sched AND stop.n = s0.n + d.k16
),
b AS (  -- effective RTR (tracker, or the ledger-identity inference for tracker-missing deals)
    SELECT base.*,
        CASE
            WHEN rtr_tracker IS NOT NULL THEN rtr_tracker
            WHEN ipo_date IS NOT NULL THEN d7 + d8
            WHEN epo_date IS NOT NULL OR n_debits = 0 THEN NULL
            WHEN n_status > 0 THEN NULL
            WHEN NOT last_debit_returned AND stop_date IS NOT NULL AND stop_date <= aod
                 AND ret20 * 20 < 3 * att20 THEN d7
        END AS rtr,
        CASE
            WHEN rtr_tracker IS NOT NULL THEN 'tracker'
            WHEN ipo_date IS NOT NULL THEN 'inferred_internal_payoff'
            WHEN epo_date IS NOT NULL OR n_debits = 0 THEN NULL
            WHEN n_status > 0 THEN NULL
            WHEN NOT last_debit_returned AND stop_date IS NOT NULL AND stop_date <= aod
                 AND ret20 * 20 < 3 * att20 THEN 'inferred_paid_in_full'
        END AS rtr_source,
        LEAST(ipo_date, epo_date) AS close_limit
    FROM base
),
cum AS (  -- cumulative cash from merchant per deal per event date
    SELECT DISTINCT deal_id, event_date,
           SUM(amount_cents) OVER (PARTITION BY deal_id ORDER BY event_date RANGE UNBOUNDED PRECEDING) AS cfm_to
    FROM e WHERE kind IN ('debit', 'return', 'early_payoff_cash', 'settlement')
),
anch AS (  -- D16 anchors: the funding date and every good debit date
    SELECT DISTINCT deal_id, g FROM (
        SELECT deal_id, funding_date AS g FROM d
        UNION ALL SELECT deal_id, event_date FROM deb WHERE NOT returned
    )
),
anch2 AS (
    SELECT a.deal_id, a.g, lead(a.g) OVER (PARTITION BY a.deal_id ORDER BY a.g) AS nxt, r.d AS r
    FROM anch a
    JOIN b ON b.deal_id = a.deal_id
    JOIN cal_sched s0 ON s0.code = b.code AND s0.d = a.g
    LEFT JOIN cal_sched r ON r.code = b.code AND r.is_sched AND r.n = s0.n + b.k16
),
fire AS (
    SELECT a.deal_id, MIN(a.r) AS rec_b
    FROM anch2 a
    JOIN b ON b.deal_id = a.deal_id
    ASOF LEFT JOIN cum ON cum.deal_id = a.deal_id AND cum.event_date <= a.r
    WHERE a.r IS NOT NULL AND a.r <= b.aod
      AND (a.nxt IS NULL OR a.nxt > a.r)
      AND (b.close_limit IS NULL OR a.r < b.close_limit)
      AND (b.rtr IS NULL OR b.rtr - COALESCE(cum.cfm_to, 0) > 1)  -- $0.01 tolerance, as paid_in_full
    GROUP BY a.deal_id
),
loga AS (
    SELECT deal_id, MIN(event_date) AS rec_a FROM st WHERE event_type IN ('default', 'bankruptcy') GROUP BY deal_id
),
d16a AS (
    SELECT b.deal_id, loga.rec_a, fire.rec_b,
        CASE WHEN fire.rec_b IS NULL OR (loga.rec_a IS NOT NULL AND loga.rec_a <= fire.rec_b) THEN loga.rec_a
             ELSE fire.rec_b END AS rec,
        CASE WHEN loga.rec_a IS NULL AND fire.rec_b IS NULL THEN NULL
             WHEN fire.rec_b IS NULL OR (loga.rec_a IS NOT NULL AND loga.rec_a <= fire.rec_b) THEN 'collections_log'
             ELSE 'missed_payments' END AS trig
    FROM b LEFT JOIN loga USING (deal_id) LEFT JOIN fire USING (deal_id)
),
d16 AS (
    SELECT x.deal_id, x.rec, x.trig, nb.d AS default_date
    FROM (
        SELECT d16a.*, COALESCE(
                   (SELECT MAX(deb.event_date) FROM deb WHERE deb.deal_id = d16a.deal_id AND NOT deb.returned
                      AND deb.event_date <= d16a.rec), b.funding_date) AS lg
        FROM d16a JOIN b USING (deal_id) WHERE d16a.rec IS NOT NULL
    ) x
    JOIN cal_sched s0 ON s0.code = 'daily' AND s0.d = x.lg
    LEFT JOIN cal_sched nb ON nb.code = 'daily' AND nb.is_sched AND nb.n = s0.n + 1
),
agreed AS (
    SELECT deal_id, arg_max(agreed_amount_cents, (event_date, status_id)) AS agreed_amt
    FROM st WHERE event_type = 'settlement' AND agreed_amount_cents IS NOT NULL GROUP BY deal_id
),
sett AS (
    SELECT deal_id, event_date,
           SUM(amount_cents) OVER (PARTITION BY deal_id ORDER BY event_date, event_id ROWS UNBOUNDED PRECEDING) AS run,
           row_number() OVER (PARTITION BY deal_id ORDER BY event_date, event_id) AS n
    FROM e WHERE kind = 'settlement'
),
sdone AS (
    SELECT s.deal_id, MIN(s.event_date) AS settle_done
    FROM sett s JOIN agreed a USING (deal_id) WHERE s.run >= a.agreed_amt - s.n GROUP BY s.deal_id
),
-- D17 at the renewal date t (the first internal payoff)
rv AS (  -- the view at t: events dated <= t, plus (attempt basis) later returns of debits dated <= t
    SELECT e.*, b.ipo_date AS t FROM e JOIN b USING (deal_id)
    WHERE b.ipo_date IS NOT NULL AND e.event_date <= b.ipo_date
    UNION ALL
    SELECT e.*, b.ipo_date FROM e JOIN b USING (deal_id)
    JOIN ledger dbt ON dbt.event_id = e.matched_event_id
    WHERE attempt_basis AND b.ipo_date IS NOT NULL AND e.kind = 'return'
      AND e.event_date > b.ipo_date AND dbt.kind = 'debit' AND dbt.event_date <= b.ipo_date
),
rv_deb AS (
    SELECT x.deal_id, x.event_id, (r.matched_event_id IS NOT NULL) AS returned,
           row_number() OVER (PARTITION BY x.deal_id ORDER BY x.event_date DESC, x.event_id DESC) AS rn_desc
    FROM rv x LEFT JOIN (SELECT deal_id, matched_event_id FROM rv WHERE kind = 'return' AND matched_event_id IS NOT NULL) r
      ON r.deal_id = x.deal_id AND r.matched_event_id = x.event_id
    WHERE x.kind = 'debit'
),
rv_agg AS (
    SELECT b.deal_id,
           (SELECT COALESCE(SUM(amount_cents), 0) FROM rv WHERE rv.deal_id = b.deal_id
               AND kind IN ('debit', 'return', 'early_payoff_cash', 'settlement')) AS cfm_t,
           (SELECT COUNT(*) FROM rv_deb WHERE rv_deb.deal_id = b.deal_id AND rn_desc <= 20) AS att_t,
           (SELECT COUNT(*) FROM rv_deb WHERE rv_deb.deal_id = b.deal_id AND rn_desc <= 20 AND returned) AS ret_t,
           (SELECT COUNT(*) > 0 FROM rv WHERE rv.deal_id = b.deal_id AND kind = 'modification') AS mod_t,
           (SELECT arg_max(new_payment_cents, (event_date, event_id)) FROM rv WHERE rv.deal_id = b.deal_id
               AND kind = 'modification' AND new_payment_cents IS NOT NULL AND new_payment_cents <> 0) AS mod_pay,
           GREATEST(0, ct.n - cf.n) AS n_t
    FROM b
    JOIN cal_sched cf ON cf.code = 'daily' AND cf.d = b.funding_date
    JOIN cal_sched ct ON ct.code = 'daily' AND ct.d = b.ipo_date
    WHERE b.ipo_date IS NOT NULL
),
d17 AS (
    SELECT ra.deal_id, ra.cfm_t, ra.att_t, ra.ret_t, ra.mod_t,
           CASE WHEN b.payment_cents IS NULL THEN NULL
                ELSE CASE WHEN b.rtr IS NULL THEN (2 * ra.n_t * b.payment_cents + b.div) // (2 * b.div)
                          ELSE LEAST(b.rtr, (2 * ra.n_t * b.payment_cents + b.div) // (2 * b.div)) END
           END AS d12_t,
           COALESCE(ra.mod_pay, b.payment_cents) AS pay_t,
           sx.payment_cents AS succ_pay, sx.div AS succ_div
    FROM rv_agg ra JOIN b USING (deal_id)
    LEFT JOIN (SELECT deal_id, COALESCE(payment_cents, payment_cents_ach) AS payment_cents,
                      CASE WHEN COALESCE(frequency, frequency_ach) = 'weekly' THEN 5 ELSE 1 END AS div
               FROM deals_resolved) sx ON sx.deal_id = b.successor_deal_id
),
o AS (  -- outcome (§2.1 precedence)
    SELECT b.*, d16.rec AS d16_rec, d16.trig AS d16_trig, d16.default_date, agreed.agreed_amt, sdone.settle_done,
           d17.cfm_t, d17.att_t, d17.ret_t, d17.mod_t, d17.d12_t, d17.pay_t, d17.succ_pay, d17.succ_div,
           (d17.att_t > 0 AND d17.ret_t * 20 >= 3 * d17.att_t) AS trig14,
           (COALESCE(d17.d12_t, 0) <> 0 AND d17.cfm_t * 100 < 85 * d17.d12_t) AS trig13,
           CASE WHEN d16.rec IS NOT NULL AND sdone.settle_done IS NULL
                     AND CAST(d16.default_date + INTERVAL 180 DAY AS DATE) <= b.aod
                THEN CAST(d16.default_date + INTERVAL 180 DAY AS DATE) END AS auto_wo
    FROM b
    LEFT JOIN d16 USING (deal_id) LEFT JOIN agreed USING (deal_id) LEFT JOIN sdone USING (deal_id)
    LEFT JOIN d17 USING (deal_id)
),
o2 AS (
    SELECT o.*,
        (o.ipo_date IS NOT NULL AND (o.trig14 OR o.trig13 OR o.mod_t)) AS distressed,
        CASE
            WHEN o.wo_date IS NOT NULL OR o.auto_wo IS NOT NULL THEN 'written_off'
            WHEN o.settle_done IS NOT NULL THEN 'settled'
            WHEN o.d16_rec IS NOT NULL THEN 'defaulted'
            WHEN o.ipo_date IS NOT NULL AND (o.trig14 OR o.trig13 OR o.mod_t) THEN 'refinanced_distressed'
            WHEN o.ipo_date IS NOT NULL THEN 'renewed_healthy'
            WHEN o.epo_date IS NOT NULL THEN 'paid_in_full_early'
            WHEN o.rtr IS NOT NULL AND o.d7 >= o.rtr - 1 THEN 'paid_in_full'
            WHEN o.has_mod THEN 'active_modified'
            ELSE 'active'
        END AS outcome
    FROM o
),
o3 AS (
    SELECT o2.*,
        CASE outcome
            WHEN 'written_off' THEN LEAST(wo_date, auto_wo)
            WHEN 'settled' THEN settle_done
            WHEN 'defaulted' THEN d16_rec
            WHEN 'refinanced_distressed' THEN ipo_date
            WHEN 'renewed_healthy' THEN ipo_date
            WHEN 'paid_in_full_early' THEN epo_date
            WHEN 'paid_in_full' THEN last_good
        END AS outcome_date,
        outcome IN ('written_off', 'settled', 'refinanced_distressed', 'renewed_healthy',
                    'paid_in_full_early', 'paid_in_full') AS is_closed
    FROM o2
),
o4 AS (
    SELECT o3.*, GREATEST(0, ce.n - cf.n) AS n_end
    FROM o3
    JOIN cal_sched cf ON cf.code = 'daily' AND cf.d = o3.funding_date
    JOIN cal_sched ce ON ce.code = 'daily'
         AND ce.d = CASE WHEN o3.is_closed AND o3.outcome_date IS NOT NULL THEN o3.outcome_date ELSE o3.aod END
),
o5 AS (
    SELECT o4.*,
        CASE WHEN payment_cents IS NULL THEN NULL
             WHEN rtr IS NULL THEN (2 * n_end * payment_cents + div) // (2 * div)
             ELSE LEAST(rtr, (2 * n_end * payment_cents + div) // (2 * div)) END AS d12,
        (NOT tracker_missing AND commission_rate IS NOT NULL) AS commission_known
    FROM o4
)
SELECT
    o5.deal_id,
    o5.merchant_key,
    o5.aod AS as_of,
    o5.funding_date,
    CAST(year(o5.funding_date) AS VARCHAR) || 'Q' || CAST(quarter(o5.funding_date) AS VARCHAR) AS vintage,
    o5.frequency,
    o5.tracker_missing,
    o5.commission_known,
    o5.rtr_source,
    o5.advance_cents AS d1_advance_cents,
    o5.rtr AS d2_rtr_cents,
    o5.fee_cents AS d3_fee_cents,
    o5.d4 AS d4_net_cash_to_merchant_cents,
    CASE WHEN o5.commission_known THEN o5.comm END AS d5_commission_cents,
    o5.d4 + CASE WHEN o5.commission_known THEN o5.comm ELSE 0 END AS d6_funder_cash_out_cents,
    o5.d7 AS d7_cash_from_merchant_cents,
    o5.d8 AS d8_internal_payoff_out_cents,
    o5.d7 + o5.d8 AS d9_reported_collected_cents,
    CASE WHEN COALESCE(o5.rtr, 0) <> 0 THEN (o5.d7 + o5.d8) / o5.rtr END AS d10_reported_collection_rate,
    CASE WHEN COALESCE(o5.rtr, 0) <> 0 THEN o5.d7 / o5.rtr END AS d11_adjusted_collection_rate,
    o5.d12 AS d12_expected_collected_cents,
    CASE WHEN COALESCE(o5.d12, 0) <> 0 THEN o5.d7 / o5.d12 END AS d13_pace,
    CASE WHEN o5.att20 > 0 THEN o5.ret20 / o5.att20 END AS d14_return_rate_last20,
    o5.ret20 AS d14_returns,
    o5.att20 AS d14_attempts,
    CASE WHEN o5.last_good IS NOT NULL THEN ca.n - cg.n END AS d15_bdays_since_good_payment,
    o5.last_good AS last_good_payment_date,
    (o5.d16_rec IS NOT NULL) AS d16_default,
    o5.default_date AS d16_default_date,
    o5.d16_rec AS d16_recognition_date,
    o5.d16_trig AS d16_trigger,
    o5.ipo_date AS renewal_date,
    o5.successor_deal_id,
    CASE WHEN o5.ipo_date IS NOT NULL THEN o5.distressed END AS d17_distressed,
    CASE WHEN o5.ipo_date IS NOT NULL THEN COALESCE(o5.trig14, false) END AS d17_d14_trigger,
    CASE WHEN o5.ipo_date IS NOT NULL THEN COALESCE(o5.trig13, false) END AS d17_d13_trigger,
    CASE WHEN o5.ipo_date IS NOT NULL THEN o5.mod_t END AS d17_mod_trigger,
    CASE WHEN o5.att_t > 0 THEN o5.ret_t / o5.att_t END AS d17_d14_value,
    o5.ret_t AS d17_d14_returns,
    o5.att_t AS d17_d14_attempts,
    CASE WHEN COALESCE(o5.d12_t, 0) <> 0 THEN o5.cfm_t / o5.d12_t END AS d17_d13_value,
    CASE WHEN o5.ipo_date IS NOT NULL AND COALESCE(o5.pay_t, 0) <> 0 AND o5.succ_pay IS NOT NULL
         THEN (o5.succ_pay / o5.succ_div) / (o5.pay_t / o5.div) END AS d17_payment_ratio,
    o5.outcome,
    o5.outcome_date,
    o5.is_closed,
    o5.outcome IN ('defaulted', 'settled', 'written_off') AS d18_bad_naive,
    o5.outcome IN ('defaulted', 'settled', 'written_off', 'refinanced_distressed') AS d19_bad_economic,
    o5.agreed_amt AS settlement_agreed_cents,
    o5.settle_recv AS settlement_received_cents,
    o5.wo_sum AS written_off_cents,
    CASE WHEN o5.rtr IS NOT NULL THEN o5.rtr - o5.d7 - o5.d8 END AS remaining_rtr_cents,
    o5.has_mod AS has_modification
FROM o5
JOIN cal_sched ca ON ca.code = 'daily' AND ca.d = o5.aod
LEFT JOIN cal_sched cg ON cg.code = 'daily' AND cg.d = o5.last_good
ORDER BY o5.deal_id;

CREATE OR REPLACE MACRO deal_metrics_sql(as_of, attempt_basis) AS TABLE
SELECT * FROM deal_metrics_at_sql(as_of, attempt_basis, -1);
