"""Part 3 on the real standardized build: row accounting, lineage, link fields, DQ rows, manifest."""

from __future__ import annotations

import json

import duckdb
import pytest

LINEAGE = ("src_file", "src_sheet", "src_row")


@pytest.fixture(scope="module")
def con(resolve_build):
    c = duckdb.connect(str(resolve_build["db"]), read_only=True)
    yield c
    c.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_every_deal_with_ach_has_a_resolved_row(con) -> None:
    n_std = one(con, "SELECT COUNT(*) FROM deals_std")
    n_ach = one(con, "SELECT COUNT(DISTINCT deal_id_posted) FROM payments_std")
    n = one(con, "SELECT COUNT(*) FROM deals_resolved")
    missing = one(con, "SELECT COUNT(*) FROM deals_resolved WHERE tracker_missing")
    assert n == n_std + missing and missing == 44
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM (SELECT DISTINCT deal_id_posted FROM payments_std "
            "EXCEPT SELECT deal_id FROM deals_resolved)",
        )
        == 0
    )
    assert n >= n_ach
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM deals_resolved WHERE tracker_missing AND "
            "(funding_net_cents IS NULL OR std_flags <> 'tracker_missing' OR advance_cents IS NOT NULL)",
        )
        == 0
    )
    assert one(con, "SELECT COUNT(*) FROM deals_resolved WHERE merchant_key IS NULL") == 0


def test_payments_resolved_accounts_for_every_payment(con) -> None:
    assert one(con, "SELECT COUNT(*) FROM payments_resolved") == one(con, "SELECT COUNT(*) FROM payments_std")
    # posted deal and amounts are untouched; only DEBITs move, each with a repair log row
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM payments_resolved r JOIN payments_std s USING (payment_id) "
            "WHERE r.deal_id_posted <> s.deal_id_posted OR r.amount_cents <> s.amount_cents "
            "OR r.src_row <> s.src_row",
        )
        == 0
    )
    moved = one(con, "SELECT COUNT(*) FROM payments_resolved WHERE deal_id <> deal_id_posted")
    assert moved == one(con, "SELECT COUNT(*) FROM payments_resolved WHERE repaired")
    assert moved == one(con, "SELECT COUNT(*) FROM misposting_repairs") > 200
    assert one(con, "SELECT COUNT(*) FROM payments_resolved WHERE repaired AND txn_type <> 'DEBIT'") == 0
    # a repaired debit goes to the renewal of the deal it was posted to
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM misposting_repairs m LEFT JOIN deal_links l "
            "ON l.deal_id = m.deal_id_repaired AND l.prior_deal_id = m.deal_id_posted "
            "WHERE l.deal_id IS NULL",
        )
        == 0
    )
    kinds = dict(con.execute("SELECT ledger_kind, COUNT(*) FROM payments_resolved GROUP BY 1").fetchall())
    assert set(kinds) == {
        "funding_out",
        "debit",
        "return",
        "internal_payoff",
        "early_payoff_cash",
        "settlement",
    }
    assert (
        one(con, "SELECT COUNT(*) FROM payments_resolved WHERE is_cash = (ledger_kind = 'internal_payoff')")
        == 0
    )


def test_every_payoff_classified_with_method(con) -> None:
    assert one(con, "SELECT COUNT(*) FROM payoff_classes") == one(
        con, "SELECT COUNT(*) FROM payments_std WHERE txn_type = 'PAYOFF'"
    )
    assert one(con, "SELECT COUNT(*) FROM payoff_classes WHERE method IS NULL OR confidence IS NULL") == 0
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM payoff_classes WHERE payoff_class = 'internal_payoff' "
            "AND successor_deal_id IS NULL",
        )
        == 0
    )
    # the description is only a hint: plain 'PAYOFF' rows are classified both ways
    both = con.execute("SELECT COUNT(DISTINCT payoff_class) FROM payoff_classes WHERE description = 'PAYOFF'")
    assert both.fetchone()[0] == 2


def test_links_have_method_and_confidence(con) -> None:
    methods = dict(con.execute("SELECT method, COUNT(*) FROM deal_links GROUP BY 1").fetchall())
    assert set(methods) == {"explicit", "regex", "inferred"}
    assert one(con, "SELECT COUNT(*) FROM deal_links WHERE confidence IS NULL OR confidence <= 0") == 0
    assert one(con, "SELECT COUNT(*) FROM merchant_merges WHERE method IS NULL OR score IS NULL") == 0
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM deal_links l JOIN deals_resolved n USING (deal_id) "
            "JOIN deals_resolved p ON p.deal_id = l.prior_deal_id WHERE p.funding_date > n.funding_date",
        )
        == 0
    )
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM deals_resolved d JOIN deal_links l USING (deal_id) "
            "WHERE d.prior_deal_id <> l.prior_deal_id",
        )
        == 0
    )
    assert one(con, "SELECT COUNT(*) FROM deals_resolved WHERE prior_deal_id_explicit IS NOT NULL") == 634


@pytest.mark.parametrize(
    "table",
    [
        "deals_resolved",
        "deal_links",
        "payoff_classes",
        "payments_resolved",
        "misposting_repairs",
        "scorecard_links",
        "collections_links",
        "collections_terms",
    ],
)
def test_lineage_not_null(con, table) -> None:
    cond = " OR ".join(f"{c} IS NULL" for c in LINEAGE)
    assert one(con, f"SELECT COUNT(*) FROM {table} WHERE {cond}") == 0


def test_scorecards_and_collections_linked(con) -> None:
    assert one(con, "SELECT COUNT(*) FROM scorecard_links") == one(con, "SELECT COUNT(*) FROM scorecards_std")
    assert one(con, "SELECT COUNT(*) FROM collections_links") == one(
        con, "SELECT COUNT(*) FROM collections_std"
    )
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM collections_links WHERE deal_id_raw IS NOT NULL AND deal_id <> deal_id_raw",
        )
        == 0
    )
    assert (
        one(
            con,
            "SELECT COUNT(*) FROM scorecard_links s JOIN deals_resolved d USING (deal_id) "
            "WHERE date_diff('day', s.app_date, d.funding_date) NOT BETWEEN 0 AND 10",
        )
        == 0
    )


def test_exceptions_queue_has_reasons(con) -> None:
    assert one(con, "SELECT COUNT(*) FROM exceptions") > 0
    assert one(con, "SELECT COUNT(*) FROM exceptions WHERE reason IS NULL OR rule_id IS NULL") == 0


def test_data_quality_rows_updated(con, resolve_build) -> None:
    rows = dict(con.execute("SELECT rule_id, status FROM data_quality").fetchall())
    for r in ("Q1", "Q2", "Q3", "Q4", "Q5", "L1", "L3"):
        assert rows[r] == "evaluated", r
    assert rows["Q6"].startswith("pending")
    report = resolve_build["report"].read_text(encoding="utf-8")
    assert "Review queue" in report and "Parts 2-3" in report


def test_manifest_stage(resolve_build) -> None:
    m = json.loads(resolve_build["manifest"].read_text(encoding="utf-8"))
    st = m["stages"]["resolve"]
    assert st["status"] == "ok"
    assert st["tables"]["payments_resolved"]["repaired"] > 200
    assert st["tables"]["deals_resolved"]["reasons"]["repaired"] == {"tracker_missing": 44}
    assert st["extra"]["l1_tracker_missing_policy"] == "strict"
    assert m["llm"]["calls"] == 0  # cached run: no network


def test_raw_and_std_tables_untouched(con, std_build) -> None:
    c2 = duckdb.connect(str(std_build["db"]), read_only=True)
    try:
        for t in ("deals_std", "payments_std", "collections_std", "scorecards_std"):
            assert (
                one(con, f"SELECT COUNT(*) FROM {t}") == c2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            )
    finally:
        c2.close()


def test_resolution_is_deterministic(resolve_build, std_build, tmp_path) -> None:
    import shutil

    from mcarisk.resolve.stage import resolve

    db = tmp_path / "p.duckdb"
    shutil.copy(std_build["db"], db)
    resolve(db, dq_report_path=None, l1_policy="strict", notes_mode="auto")
    q = (
        "SELECT string_agg(deal_id || ':' || merchant_key || ':' || coalesce(prior_deal_id, 0), ',' "
        "ORDER BY deal_id) FROM deals_resolved"
    )
    a = duckdb.connect(str(db), read_only=True)
    b = duckdb.connect(str(resolve_build["db"]), read_only=True)
    try:
        assert a.execute(q).fetchone()[0] == b.execute(q).fetchone()[0]
    finally:
        a.close()
        b.close()
