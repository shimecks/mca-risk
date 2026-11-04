"""Part 4 on the real build (session fixture ``metrics_build``): ledger completeness and identity,
Python == SQL for every metric, as-of discipline, order invariance, M0219 from the built tables, Q6,
manifest, and E6 / E7 behaviour (including error detection)."""

from __future__ import annotations

import json
import random
import shutil
from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from evals.common import EvalContext
from evals.suites import e6_definitions, e7_metrics
from mcarisk.ledger import CASH_KINDS, commission_cents
from mcarisk.metrics.deal import OUTCOMES, all_deal_metrics
from mcarisk.metrics.portfolio import portfolio_metrics
from mcarisk.metrics.relationship import all_relationship_metrics
from mcarisk.metrics.stage import compute, install_sql, load_model
from tests.test_metrics_unit import assert_m0219_section7

M0219 = "MK1A8B5FBF55"


@pytest.fixture(scope="module")
def con(metrics_build):
    c = duckdb.connect(str(metrics_build["db"]), read_only=True)
    yield c
    c.close()


def df(con, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


# ------------------------------------------------------------------------------------------ ledger
def test_every_ach_row_is_exactly_one_ledger_event(con):
    pr = df(con, "SELECT * FROM payments_resolved ORDER BY payment_id")
    lg = df(con, "SELECT * FROM ledger WHERE source = 'ach' ORDER BY source_id")
    assert len(lg) == len(pr) and lg.source_id.is_unique
    assert (lg.source_id.values == pr.payment_id.values).all()
    for a, b in (("deal_id", "deal_id"), ("amount_cents", "amount_cents"), ("kind", "ledger_kind"),
                 ("event_date", "txn_date"), ("deal_id_posted", "deal_id_posted"),
                 ("src_file", "src_file"), ("src_row", "src_row")):  # fmt: skip
        assert (lg[a].values == pr[b].values).all(), a
    assert set(lg.attribution) == {"posted", "l3_repair"}
    assert (lg.attribution == "l3_repair").sum() == pr.repaired.sum() == 227


def test_ledger_kinds_signs_cash_flags_and_lineage(con):
    lg = df(con, "SELECT * FROM ledger")
    assert lg.event_id.is_unique
    assert set(lg.kind) == {"funding_out", "commission_out", "debit", "return", "early_payoff_cash",
                            "internal_payoff", "settlement", "modification", "write_off"}  # fmt: skip
    assert (lg.is_cash == lg.kind.isin(CASH_KINDS)).all()
    assert not lg[lg.kind.isin(["internal_payoff", "modification", "write_off"])].is_cash.any()
    sign = lg.groupby("kind").amount_cents.agg(["min", "max"])
    for k in ("funding_out", "commission_out", "return"):
        assert sign.loc[k, "max"] < 0, k
    for k in ("debit", "early_payoff_cash", "settlement", "internal_payoff"):
        assert sign.loc[k, "min"] > 0, k
    assert (lg[lg.kind == "modification"].amount_cents == 0).all()
    assert lg[["src_file", "src_sheet", "src_row"]].notna().all().all()
    # every internal payoff names its successor (L1)
    assert lg[lg.kind == "internal_payoff"].successor_deal_id.notna().all()


def test_commission_out_from_tracker(con):
    d = df(con, "SELECT deal_id, advance_cents, commission_rate, tracker_missing FROM deals_resolved")
    c = df(con, "SELECT deal_id, amount_cents FROM ledger WHERE kind = 'commission_out'").set_index("deal_id")
    exp = {
        int(r.deal_id): commission_cents(int(r.advance_cents), float(r.commission_rate))
        for r in d.itertuples()
        if not r.tracker_missing and commission_cents(int(r.advance_cents), float(r.commission_rate))
    }
    assert set(c.index) == set(exp)
    assert all(-c.at[k, "amount_cents"] == v for k, v in exp.items())
    assert not set(c.index) & set(d[d.tracker_missing].deal_id)  # no commission for tracker-missing deals


def test_collections_rows_fully_accounted(con):
    acc = df(con, "SELECT * FROM ledger_accounting").set_index("source_table")
    a = acc.loc["collections_terms"]
    assert a.n_ledger + a.n_status_events + a.n_rejected == a.n_source
    assert a.n_source == con.execute("SELECT COUNT(*) FROM collections_terms").fetchone()[0]
    n_col = con.execute("SELECT COUNT(*) FROM ledger WHERE source = 'collections'").fetchone()[0]
    n_st = con.execute("SELECT COUNT(*) FROM deal_status_events").fetchone()[0]
    rej = df(con, "SELECT * FROM ledger_rejects")
    assert n_col == a.n_ledger and n_st == a.n_status_events and len(rej) == a.n_rejected
    assert rej.reason.notna().all() and rej.src_row.notna().all()
    assert acc.loc["payments_resolved"].n_ledger == acc.loc["payments_resolved"].n_source


def test_returns_matched_one_to_one(con):
    lg = df(con, "SELECT event_id, deal_id, event_date, kind, amount_cents, matched_event_id FROM ledger")
    by = lg.set_index("event_id")
    ret = lg[lg.kind == "return"]
    assert ret.matched_event_id.notna().all()  # every return in this book finds its debit
    deb = by.loc[ret.matched_event_id.astype(int)]
    assert (deb.kind.values == "debit").all()
    assert (deb.deal_id.values == ret.deal_id.values).all()
    assert (deb.amount_cents.values == -ret.amount_cents.values).all()
    assert (deb.event_date.values < ret.event_date.values).all()
    assert ret.matched_event_id.is_unique
    assert (by.loc[deb.index, "matched_event_id"].values == ret.event_id.values).all()


def test_ledger_identity_per_deal(con):
    """RTR = net debits (+ cash payoff + settlement) + internal payoff + balance (+ discount / forgiven)."""
    m = df(con, "SELECT * FROM deal_metrics WHERE d2_rtr_cents IS NOT NULL")
    bal = m.d2_rtr_cents - m.d7_cash_from_merchant_cents - m.d8_internal_payoff_out_cents
    assert (bal == m.remaining_rtr_cents).all()
    oc = m.outcome
    renewed = oc.isin(["renewed_healthy", "refinanced_distressed"])
    assert renewed.sum() > 700 and (bal[renewed] == 0).all()  # payoff = remaining balance, to the cent
    pif = oc == "paid_in_full"
    assert pif.sum() > 500 and bal[pif].between(-0, 1).all()
    early = oc == "paid_in_full_early"  # discount = RTR - CFM > 0, small (25 early payoffs, 1 has no RTR)
    assert early.sum() == 24 and (bal[early] > 0).all() and (bal[early] < 0.15 * m.d2_rtr_cents[early]).all()
    wo_log = (oc == "written_off") & (m.written_off_cents > 0)  # the log's written-off balance
    assert wo_log.sum() > 150 and (bal[wo_log] == m.written_off_cents[wo_log]).all()
    st = oc == "settled"  # forgiven remainder >= 0, instalments complete
    assert st.sum() > 50 and (bal[st] >= 0).all()
    assert (m.settlement_received_cents[st] >= m.settlement_agreed_cents[st] - 6).all()
    op = oc.isin(["active", "active_modified", "defaulted"])
    assert (bal[op] > 0).all()


# ------------------------------------------------------------------------------------------ Python == SQL
def _norm(v) -> str:
    """Compare values by text, with dates and timestamps both rendered as an ISO date."""
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()[:10]
    return str(v)


def assert_frames_equal(py: pd.DataFrame, sql: pd.DataFrame, key: list[str], rtol: float = 1e-12) -> None:
    assert list(py.columns) == list(sql.columns)
    py = py.sort_values(key, na_position="first").reset_index(drop=True)
    sql = sql.sort_values(key, na_position="first").reset_index(drop=True)
    assert len(py) == len(sql)
    for c in py.columns:
        a, b = py[c], sql[c]
        if a.dtype.kind == "f" or b.dtype.kind == "f":
            af, bf = a.astype(float).to_numpy(), b.astype(float).to_numpy()
            both_nan = np.isnan(af) & np.isnan(bf)
            close = np.abs(af - bf) <= rtol * np.maximum(1.0, np.abs(af))
            assert (both_nan | close).all(), c
        else:
            sa = a.astype(object).where(a.notna(), None).map(_norm)
            sb = b.astype(object).where(b.notna(), None).map(_norm)
            assert (sa == sb).all(), (c, list(zip(sa[sa != sb][:3], sb[sa != sb][:3], strict=False)))


TABLES = [
    ("deal_metrics", "v_deal_metrics", "deal_metrics_sql", ["deal_id"]),
    ("relationship_metrics", "v_relationship_metrics", "relationship_metrics_sql", ["merchant_key"]),
    (
        "portfolio_metrics",
        "v_portfolio_metrics",
        "portfolio_metrics_sql",
        ["metric_id", "metric", "segment", "k"],
    ),
]


@pytest.mark.parametrize(("table", "view", "macro", "key"), TABLES)
def test_python_equals_sql_on_the_build(con, table, view, macro, key):
    """The persisted (Python) tables equal the SQL views: exact for cents, ids, dates, flags, labels."""
    assert_frames_equal(df(con, f"SELECT * FROM {table}"), df(con, f"SELECT * FROM {view}"), key)


def test_python_equals_sql_other_as_of_and_basis(metrics_build, tmp_path):
    db = tmp_path / "p.duckdb"
    shutil.copy(metrics_build["db"], db)
    c = duckdb.connect(str(db))
    try:
        install_sql(c)
        for as_of, basis in (
            (date(2025, 6, 30), "asof"),
            (date(2026, 9, 30), "asof"),
            (date(2024, 3, 15), "attempt"),
        ):
            dm, rm, pm = compute(c, as_of=as_of, d17_basis=basis)
            flag = "true" if basis == "attempt" else "false"
            for rows, (_, _, macro, key) in zip((dm, rm, pm), TABLES, strict=True):
                sql = c.execute(f"SELECT * FROM {macro}(DATE '{as_of}', {flag})").df()
                py = pd.DataFrame(rows, columns=sql.columns)
                for col in sql.columns:  # same dtypes as the SQL side before comparing
                    if sql[col].dtype.kind in "iu" and py[col].isna().any():
                        sql[col] = sql[col].astype("Int64")
                assert_frames_equal(py, sql, key)
    finally:
        c.close()


# ------------------------------------------------------------------------------------------ as-of, order
def test_nothing_after_as_of_is_used(con):
    m = load_model(con)
    as_of = date(2025, 6, 30)
    full = all_deal_metrics(m["deals"], m["events"], m["status"], as_of=as_of)
    cut = all_deal_metrics(
        [d for d in m["deals"] if d.funding_date <= as_of],
        [e for e in m["events"] if e.event_date <= as_of],
        [s for s in m["status"] if s.event_date <= as_of],
        as_of=as_of,
    )
    # successors funded later are only used for D17's payment ratio, which they also are known at renewal
    assert full == cut
    assert all(r["funding_date"] <= as_of for r in full)
    assert all(r["outcome_date"] is None or r["outcome_date"] <= as_of for r in full)


def test_order_invariance_on_the_real_ledger(con):
    m = load_model(con)
    ref = all_deal_metrics(m["deals"], m["events"], m["status"])
    rng = random.Random(7)
    ev, de, stt = m["events"][:], m["deals"][:], m["status"][:]
    rng.shuffle(ev)
    rng.shuffle(de)
    rng.shuffle(stt)
    got = all_deal_metrics(de, ev, stt)
    assert got == ref
    rm = all_relationship_metrics(ref)
    shuffled = ref[:]
    rng.shuffle(shuffled)
    assert all_relationship_metrics(shuffled) == rm
    cash = m["cash"][:]
    p1 = portfolio_metrics(ref, rm, cash)
    rng.shuffle(cash)
    assert portfolio_metrics(shuffled, list(reversed(rm)), cash) == p1


# ------------------------------------------------------------------------------------------ outcomes, M0219
def test_outcomes_and_flags_consistent(con):
    m = df(con, "SELECT * FROM deal_metrics")
    assert set(m.outcome) <= set(OUTCOMES)
    assert (m.d18_bad_naive == m.outcome.isin(["defaulted", "settled", "written_off"])).all()
    assert (m.d19_bad_economic == (m.d18_bad_naive | (m.outcome == "refinanced_distressed"))).all()
    assert (
        m.d9_reported_collected_cents == m.d7_cash_from_merchant_cents + m.d8_internal_payoff_out_cents
    ).all()
    ren = m.renewal_date.notna()
    assert (m.d17_distressed.notna() == ren).all()
    assert (
        m[m.tracker_missing].commission_known.eq(False).all()
        and m[m.tracker_missing].d5_commission_cents.isna().all()
    )
    r = df(con, "SELECT * FROM relationship_metrics")
    assert len(r) == con.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
    assert r.r10_n_deals.sum() == len(m)


def _m0219(con):
    m = df(con, "SELECT * FROM deal_metrics WHERE deal_id IN (1102, 1150)").set_index("deal_id")
    rel = df(con, f"SELECT * FROM relationship_metrics WHERE merchant_key = '{M0219}'").iloc[0].to_dict()
    to_d = lambda s: {k: (None if (not isinstance(v, str) and pd.isna(v)) else v) for k, v in s.items()}  # noqa: E731
    m1102, m1150 = to_d(m.loc[1102].to_dict()), to_d(m.loc[1150].to_dict())
    for x in (m1102, m1150):
        x["funding_date"] = pd.Timestamp(x["funding_date"]).date()
    return m1102, m1150, rel


def test_m0219_from_the_built_tables_observable_numbers(con):
    """Every §7 number the raw data can support, from the pipeline tables. Deal 1102 has no tracker row:
    its RTR comes from the ledger identity (D7 + D8) and its commission is unknown (flagged)."""
    m1102, m1150, rel = _m0219(con)
    assert_m0219_section7(m1102, m1150, rel, check_commission=False)
    assert m1102["tracker_missing"] and m1102["rtr_source"] == "inferred_internal_payoff"
    assert not m1102["commission_known"] and rel["n_commission_unknown"] == 1


def test_m0219_from_the_built_tables_commission_dependent_lower_bounds(con):
    """METRICS.md v1.2 §7 data note: deal 1102 has no tracker row, so its commission is unknown and the
    pipeline's D6 / R1 / R3 / R8 are the documented lower bounds (R3 0.80, loss $6,648)."""
    m1102, _m1150, rel = _m0219(con)
    assert m1102["d1_advance_cents"] is None and m1102["d5_commission_cents"] is None
    assert round(m1102["d6_funder_cash_out_cents"] / 100) == 30_625  # D6 = D4 + unknown commission
    assert round(rel["r1_funder_cash_out_cents"] / 100) == 33_370
    assert round(rel["r3_cash_multiple"], 2) == 0.80
    assert round(rel["r8_realized_loss_cents"] / 100) == 6_648
    # each lower bound sits below the §7 full-terms value by exactly 1102's $3,424 commission
    assert rel["r1_funder_cash_out_cents"] + 342_400 == 3_679_440  # §7: $36,794.40
    assert rel["r8_realized_loss_cents"] + 342_400 == 1_007_152  # §7: $10,071.52 -> $10,072


# ------------------------------------------------------------------------------------------ Q6, report, manifest
def test_q6_dq_report_and_manifest(metrics_build, con):
    q6 = con.execute(
        "SELECT status, hits, denominator, detail FROM data_quality WHERE rule_id = 'Q6'"
    ).fetchone()
    assert q6[0] == "evaluated" and 0 < q6[1] < q6[2] == 1967 and "typed paid off" in q6[3]
    rep = metrics_build["report"].read_text(encoding="utf-8")
    assert "Ledger accounting (Part 4)" in rep and "Derived outcomes" in rep and "| Q6 |" in rep
    man = json.loads(metrics_build["manifest"].read_text(encoding="utf-8"))
    assert man["stages"]["ledger"]["status"] == "ok" and man["stages"]["metrics"]["status"] == "ok"
    t = man["stages"]["metrics"]["tables"]
    assert t["deal_metrics"]["accepted"] == 2011 and t["relationship_metrics"]["accepted"] == 1200
    assert man["stages"]["metrics"]["extra"]["d17_basis"] == "attempt"
    assert man["stages"]["ledger"]["tables"]["ledger_rejects"]["rejected"] == 1


# ------------------------------------------------------------------------------------------ E6 / E7
def test_e6_e7_on_the_build(metrics_build):
    ctx = EvalContext(db_path=metrics_build["db"])
    e6 = e6_definitions.run(ctx).metrics
    assert e6["outcome_accuracy"] >= 0.97 and e6["d17_precision"] >= 0.95
    assert e6["d17_basis_of_build"] == "attempt" and set(e6["by_d17_basis"]) == {"asof", "attempt"}
    assert e6["default_date_within_25bd_rate"] > 0.95
    e7 = e7_metrics.run(ctx).metrics
    assert e7["deal_metric_max_abs_err_usd"] <= 1.0 and e7["relationship_metric_max_abs_err_usd"] <= 1.0
    assert e7["linked_deals"] >= 2000 and "end_to_end" in e7


def test_e6_e7_detect_errors(metrics_build, tmp_path):
    db = tmp_path / "bad.duckdb"
    shutil.copy(metrics_build["db"], db)
    c = duckdb.connect(str(db))
    c.execute("UPDATE deal_metrics SET outcome = 'active' WHERE outcome IN ('paid_in_full', 'written_off')")
    c.execute(
        "UPDATE deal_metrics SET d7_cash_from_merchant_cents = d7_cash_from_merchant_cents + 500 WHERE deal_id = 1500"
    )
    c.execute("UPDATE relationship_metrics SET r1_funder_cash_out_cents = r1_funder_cash_out_cents + 500 "
              "WHERE merchant_key = (SELECT merchant_key FROM deal_metrics WHERE deal_id = 1500)")  # fmt: skip
    c.close()
    ctx = EvalContext(db_path=db)
    assert e6_definitions.run(ctx).metrics["outcome_accuracy"] < 0.8
    e7 = e7_metrics.run(ctx).metrics
    assert e7["deal_metric_max_abs_err_usd"] == pytest.approx(5.0)
    assert e7["relationship_metric_max_abs_err_usd"] >= 5.0
