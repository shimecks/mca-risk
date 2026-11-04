"""Unit tests for Part 3 resolution logic on small fixtures (METRICS.md Q1-Q4, L1, L3)."""

from __future__ import annotations

from datetime import date

import pytest

from mcarisk.resolve import matching, misposting, payoffs, renewals
from mcarisk.resolve.merchants import AliasIndex, DealNames, resolve_deals
from mcarisk.resolve.names import FUZZY_THRESHOLD, name_score, normalize_name

# ------------------------------------------------------------------------------------------ Q1 names


@pytest.mark.parametrize(
    ("variant", "legal"),
    [
        ("INDIGO SPA CORP", "Indigo Spa Corp"),  # upper case
        ("Ferreira Market, L.L.C.", "Ferreira Market LLC"),  # L.L.C.
        ("Ferreira Market L.L.C", "Ferreira Market LLC"),
        ("Achebe Concrete, Inc.", "Achebe Concrete Inc"),  # Inc.
        ("Ironwood Tire and Lube LLC", "Ironwood Tire & Lube LLC"),  # & -> and
        ("  lantern   hauling llc ", "Lantern Hauling LLC"),  # whitespace / case
        ("Granite Consulting Incorporated", "Granite Consulting Inc"),
        ("Bluebird Staffing Company", "Bluebird Staffing Co"),
    ],
)
def test_normalizer_equal_forms(variant: str, legal: str) -> None:
    assert normalize_name(variant).full == normalize_name(legal).full
    assert name_score(normalize_name(variant), normalize_name(legal)) == 100.0


def test_normalizer_suffix_dropped() -> None:
    a, b = normalize_name("Indigo Spa"), normalize_name("Indigo Spa Corp")
    assert a.suffix is None and b.suffix == "corp" and a.core == b.core == "indigo spa"
    assert name_score(a, b) == 100.0
    assert a.first == "indigo"


@pytest.mark.parametrize(
    ("typo", "legal"),
    [
        ("Westbrook Cocnrete Inc", "Westbrook Concrete Inc"),
        ("Indigo Spa oCrp", "Indigo Spa Corp"),  # typo inside the suffix: suffix unknown, core subset
        ("Highland Motosr Co", "Highland Motors Co"),
        ("Meadowbrook Barbershop LCL", "Meadowbrook Barbershop LLC"),
        ("Brightwater Transprot LLC", "Brightwater Transport LLC"),
    ],
)
def test_one_char_typo_scores_above_threshold(typo: str, legal: str) -> None:
    assert name_score(normalize_name(typo), normalize_name(legal)) >= FUZZY_THRESHOLD


def test_suffix_conflict_is_a_different_entity() -> None:
    assert (
        name_score(normalize_name("Amber Physical Therapy Co"), normalize_name("Amber Physical Therapy Corp"))
        == 0
    )
    assert name_score(normalize_name("Cedar Cafe LLC"), normalize_name("Cedar Cafe Inc")) == 0


def test_similar_but_different_names_stay_below_threshold() -> None:
    assert name_score(normalize_name("Keystone Market LLC"), normalize_name("Keystone Marketing LLC")) < 92
    assert name_score(normalize_name("Amber Salon Inc"), normalize_name("Amber Spa Inc")) < 92


def test_empty_name() -> None:
    n = normalize_name("")
    assert n.core == "" and n.first == "" and name_score(n, normalize_name("X Co")) == 0.0


# ------------------------------------------------------------------------------------------ Q1 clustering


def _dn(
    deal: int, ach: dict[str, int], tracker: str | None = None, ein: str | None = None, dba=None
) -> DealNames:
    d = DealNames(deal, tracker=tracker, ein=ein, dba=dba)
    d.ach.update(ach)
    return d


def test_resolve_exact_and_variants_and_aliases() -> None:
    deals = {
        1: _dn(
            1,
            {"Lantern Hauling LLC": 50, "LANTERN HAULING LLC": 3, "Lantern Hauling": 2},
            "Lantern Hauling LLC",
        ),
        2: _dn(2, {"Lantern Hauling LLC": 40, "Lantern Freight": 1}, "Lantern Freight"),  # tracker uses DBA
        3: _dn(3, {"Lantern Motors LLC": 30}, "Lantern Motors LLC"),
    }
    res = resolve_deals(deals)
    assert res.deal_key[1] == res.deal_key[2] != res.deal_key[3]
    assert [m["method"] for m in res.merges] == ["exact"]
    kinds = {(a["name_raw"], a["alias_kind"]) for a in res.aliases}
    assert ("Lantern Freight", "dba") in kinds and ("LANTERN HAULING LLC", "legal_variant") in kinds
    c = res.index.lookup("Lantern Freight")
    assert [x.merchant_key for x in c] == [res.deal_key[1]] and c[0].method == "dba_alias"


def test_resolve_ein_merges_different_anchor() -> None:
    deals = {
        1: _dn(1, {"Westbrook Concrete Inc": 9}, ein="88-4123476"),
        2: _dn(2, {"Westbrook Builders": 9}, ein="88-4123476"),  # DBA as account name; EIN links it
    }
    res = resolve_deals(deals)
    assert res.deal_key[1] == res.deal_key[2]
    assert res.merges[0]["method"] == "ein" and res.deal_method[2] == ("ein", 100.0)


def test_resolve_fuzzy_merge_and_ein_veto() -> None:
    deals = {
        1: _dn(1, {"Westbrook Concrete Inc": 9}),
        2: _dn(2, {"Westbrook Cocnrete Inc": 9}),  # typo anchor
        3: _dn(3, {"Harbor Grill LLC": 9}, ein="11-1111111"),
        4: _dn(4, {"Harbor Grilll LLC": 9}, ein="22-2222222"),  # similar, but different EIN
    }
    res = resolve_deals(deals)
    assert res.deal_key[1] == res.deal_key[2]
    fz = [m for m in res.merges if m["method"] == "fuzzy"]
    assert len(fz) == 1 and fz[0]["score"] >= 92
    assert res.deal_key[3] != res.deal_key[4]
    assert any(e["reason"] == "fuzzy_match_vetoed_by_ein" for e in res.exceptions)


def test_suffix_conflict_never_merges_deals() -> None:
    deals = {1: _dn(1, {"Amber Physical Therapy Co": 5}), 2: _dn(2, {"Amber Physical Therapy Corp": 5})}
    res = resolve_deals(deals)
    assert res.deal_key[1] != res.deal_key[2]


def test_merchant_key_is_stable() -> None:
    deals = {1: _dn(1, {"Lantern Hauling LLC": 5}), 2: _dn(2, {"Lantern Motors LLC": 5})}
    assert resolve_deals(deals).deal_key == resolve_deals(dict(reversed(list(deals.items())))).deal_key


def test_alias_lookup_tiers() -> None:
    idx = AliasIndex()
    idx.add("Amber Salon Inc", "K1", "ach")
    idx.add("Amber Salon", "K2", "dba")  # DBA of another merchant
    idx.add("Amber Concrete Corp", "K3", "ach")
    idx.add("Highland Motors Co", "K4", "ach")
    assert [c.merchant_key for c in idx.lookup("AMBER SALON INC")] == ["K1"]
    # suffix dropped: ambiguous between the legal-name merchant and the DBA merchant
    assert {c.merchant_key for c in idx.lookup("Amber Salon")} == {"K1", "K2"}
    assert idx.lookup("Hihgland Motors Co")[0].method == "fuzzy_cross_block"  # first-token typo
    c = idx.lookup("AmberrConcrete Corp")  # glued words
    assert c and c[0].merchant_key == "K3" and c[0].method == "fuzzy_compact"
    assert idx.lookup("Totally Unknown LLC") == []


# ------------------------------------------------------------------------------------------ Q2


@pytest.mark.parametrize(
    ("text", "prior", "pattern"),
    [
        ("renewal of 1014", 1014, "renewal_of"),
        ("RENEWAL - paid off #1010", 1010, "paid_off"),
        ("refi #1036; recon approved pmt $150.66", 1036, "refi"),
        ("ren. prev deal 1001", 1001, "ren_prev_deal"),
        ("renewal", None, None),
        ("rnwl", None, None),
        ("new", None, None),
        (None, None, None),
    ],
)
def test_parse_link_note(text, prior, pattern) -> None:
    n = renewals.parse_link_note(text)
    assert (n.prior_deal_id, n.pattern) == (prior, pattern)


def test_parse_link_note_flags() -> None:
    assert renewals.parse_link_note("renewal - upsell").says_renewal
    assert renewals.parse_link_note("rnwl").says_renewal
    assert renewals.parse_link_note("NEW - ISO referral").says_new
    # the recon payment amount is not a deal number
    assert renewals.parse_link_note("recon approved pmt $3,986.54").prior_deal_id is None


def test_tracker_links_priority_and_validation() -> None:
    d = date(2024, 5, 1)
    known = {1001: date(2024, 1, 2), 1002: d, 1003: d, 1004: d, 1005: date(2024, 9, 1), 1006: d}
    mk = {1001: "A", 1002: "A", 1003: "A", 1004: "B", 1005: "A", 1006: "A"}
    deals = [
        renewals.TrackerDeal(1002, d, 1001, "renewal of 1001"),  # explicit + agreeing notes
        renewals.TrackerDeal(1003, d, None, "refi #1001"),  # notes only
        renewals.TrackerDeal(1004, d, 1001, None),  # crosses merchants: kept at 0.7
        renewals.TrackerDeal(1006, d, 1005, None),  # prior funded later: dropped
        renewals.TrackerDeal(1007, d, 999, None),  # prior unknown: dropped
    ]
    links, exc = renewals.tracker_links(deals, known, mk)
    assert links[1002]["method"] == "explicit" and "notes agree" in links[1002]["evidence"]
    assert links[1003]["method"] == "regex" and links[1003]["confidence"] == 0.95
    assert links[1004]["confidence"] == 0.7
    assert 1006 not in links and 1007 not in links
    assert {e["reason"] for e in exc} == {
        "link_crosses_merchants",
        "prior_deal_not_earlier",
        "prior_deal_unknown",
    }


def test_explicit_beats_conflicting_notes() -> None:
    d = date(2024, 5, 1)
    links, exc = renewals.tracker_links(
        [renewals.TrackerDeal(1003, d, 1001, "renewal of 1002")],
        {1001: date(2024, 1, 1), 1002: date(2024, 2, 1), 1003: d},
        {1001: "A", 1002: "A", 1003: "A"},
    )
    assert links[1003]["prior_deal_id"] == 1001 and exc[0]["reason"] == "explicit_vs_notes_conflict"


# ------------------------------------------------------------------------------------------ L1

FRI = date(2024, 8, 30)  # Labor Day is Monday 2024-09-02


def _deal(i, key="A", f=FRI, adv=None, fee=None, net=None, pamt=None, missing=False):
    return payoffs.DealInfo(i, key, f, adv, fee, net, pamt, missing)


def _po(deal=1, amt=2_000_000, t=FRI, desc="PAYOFF", pid=10):
    return payoffs.Payoff(pid, deal, t, amt, desc)


def test_l1_internal_via_tracker_link() -> None:
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, adv=5_000_000, fee=100_000, net=1)}
    rows, _ = payoffs.classify_payoffs([_po()], deals, {2: 1})
    assert rows[0]["payoff_class"] == payoffs.INTERNAL and rows[0]["successor_deal_id"] == 2
    assert rows[0]["method"] == "tracker_link" and rows[0]["l1_funding_formula"] is False


def test_l1_payoff_without_payoff_amt_uses_funding_formula() -> None:
    # advance 50,000 - fee 1,000 - net funding 29,000.50 = 20,000 - 0.50 -> within $1
    deals = {
        1: _deal(1, f=date(2024, 3, 1)),
        2: _deal(2, adv=5_000_000, fee=100_000, net=2_900_050, pamt=None),
    }
    rows, _ = payoffs.classify_payoffs([_po(amt=2_000_000)], deals, {})
    r = rows[0]
    assert r["payoff_class"] == payoffs.INTERNAL and r["method"] == "funding_formula"
    assert r["l1_payoff_amt"] is None and r["l1_tracker_link"] is False


def test_l1_payoff_amt_tolerance() -> None:
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, pamt=2_000_100)}
    assert payoffs.classify_payoffs([_po()], deals, {})[0][0]["method"] == "payoff_amt"  # +$1.00 ok
    deals[2] = _deal(2, pamt=2_000_101)  # +$1.01 too far, no other criterion
    assert payoffs.classify_payoffs([_po()], deals, {})[0][0]["payoff_class"] == payoffs.CASH


def test_l1_cash_payoff_same_week_as_unrelated_new_deal() -> None:
    # same merchant funds a new deal the next business day, but nothing ties it to the payoff
    deals = {
        1: _deal(1, f=date(2024, 3, 1)),
        2: _deal(2, f=date(2024, 9, 3), adv=3_000_000, fee=60_000, net=2_940_000),  # new money: adv-fee-net=0
        3: _deal(3, key="B", f=FRI, pamt=2_000_000),  # other merchant, same day, matching amount
    }
    rows, exc = payoffs.classify_payoffs([_po(desc="RENEWAL PAYOFF")], deals, {})
    assert rows[0]["payoff_class"] == payoffs.CASH and rows[0]["n_candidates"] == 1
    reasons = {e["reason"] for e in exc}
    assert "same_merchant_funding_in_window_not_linked" in reasons
    assert "description_hint_disagrees" in reasons  # the hint never decides


def test_l1_business_day_window_spans_holiday() -> None:
    base = {1: _deal(1, f=date(2024, 3, 1))}
    ok = {**base, 2: _deal(2, f=date(2024, 9, 4), pamt=2_000_000)}  # Fri -> Wed over Labor Day = 2 bd
    late = {**base, 2: _deal(2, f=date(2024, 9, 5), pamt=2_000_000)}  # 3 bd
    before = {**base, 2: _deal(2, f=date(2024, 8, 28), pamt=2_000_000)}  # 2 bd before
    assert payoffs.classify_payoffs([_po()], ok, {})[0][0]["bday_gap"] == 2
    assert payoffs.classify_payoffs([_po()], ok, {})[0][0]["payoff_class"] == payoffs.INTERNAL
    assert payoffs.classify_payoffs([_po()], late, {})[0][0]["payoff_class"] == payoffs.CASH
    assert payoffs.classify_payoffs([_po()], before, {})[0][0]["payoff_class"] == payoffs.INTERNAL
    assert payoffs.bdays_apart(FRI, date(2024, 9, 3)) == 1


def test_l1_tracker_missing_successor_policy() -> None:
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, net=1_000_000, missing=True)}
    strict, exc = payoffs.classify_payoffs([_po()], deals, {}, payoffs.POLICY_STRICT)
    assert (
        strict[0]["payoff_class"] == payoffs.CASH and strict[0]["method"] == "l1_unevaluable_tracker_missing"
    )
    assert exc[0]["reason"] == "l1_successor_tracker_missing_classified_cash"
    infer, exc = payoffs.classify_payoffs([_po()], deals, {}, payoffs.POLICY_INFER)
    assert infer[0]["payoff_class"] == payoffs.INTERNAL and infer[0]["confidence"] == 0.6
    assert exc[0]["reason"] == "l1_successor_tracker_missing_inferred_internal"


def test_l1_default_policy_is_v11_infer() -> None:
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, net=1_000_000, missing=True)}
    rows, exc = payoffs.classify_payoffs([_po()], deals, {})  # no policy argument
    assert rows[0]["payoff_class"] == payoffs.INTERNAL and rows[0]["method"] == "inferred_tracker_missing"
    assert rows[0]["successor_deal_id"] == 2 and [e["reason"] for e in exc] == [
        "l1_successor_tracker_missing_inferred_internal"
    ]


@pytest.mark.parametrize("policy", [payoffs.POLICY_STRICT, payoffs.POLICY_INFER])
def test_l1_cash_payoff_with_other_merchants_tracker_missing_deal_stays_cash(policy) -> None:
    # a tracker-missing deal of ANOTHER merchant funds the same day: never a successor
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, key="B", net=1_000_000, missing=True)}
    rows, exc = payoffs.classify_payoffs([_po(desc="EARLY PAYOFF")], deals, {}, policy)
    assert rows[0]["payoff_class"] == payoffs.CASH and rows[0]["method"] == "no_successor"
    assert rows[0]["n_candidates"] == 0 and exc == []


@pytest.mark.parametrize("policy", [payoffs.POLICY_STRICT, payoffs.POLICY_INFER])
def test_l1_tracker_missing_outside_window_stays_cash(policy) -> None:
    deals = {1: _deal(1, f=date(2024, 3, 1)), 2: _deal(2, f=date(2024, 9, 5), net=1_000_000, missing=True)}
    rows, _ = payoffs.classify_payoffs([_po()], deals, {}, policy)  # 3 bd after Fri 8/30 (Labor Day)
    assert rows[0]["payoff_class"] == payoffs.CASH


def test_l1_policy_env(monkeypatch) -> None:
    monkeypatch.delenv(payoffs.POLICY_ENV, raising=False)
    assert payoffs.policy_from_env() == "infer" == payoffs.DEFAULT_POLICY  # METRICS.md v1.1
    monkeypatch.setenv(payoffs.POLICY_ENV, "strict")
    assert payoffs.policy_from_env() == "strict"
    monkeypatch.setenv(payoffs.POLICY_ENV, "maybe")
    with pytest.raises(ValueError):
        payoffs.policy_from_env()


def test_final_links_infers_from_l1() -> None:
    d = date(2024, 5, 1)
    rows = [
        {
            "payment_id": 9,
            "deal_id": 1,
            "txn_date": d,
            "payoff_class": payoffs.INTERNAL,
            "successor_deal_id": 2,
            "method": "funding_formula",
            "confidence": 0.95,
        }
    ]
    links, exc = renewals.final_links({}, rows, [renewals.TrackerDeal(2, d, None, "rnwl")])
    assert links == [
        {
            "deal_id": 2,
            "prior_deal_id": 1,
            "method": "inferred",
            "confidence": 0.95,
            "evidence": "L1 internal payoff payment 9 on 2024-05-01 via funding_formula",
        }
    ]
    assert exc == []
    _, exc = renewals.final_links({}, [], [renewals.TrackerDeal(2, d, None, "rnwl")])
    assert exc[0]["reason"] == "notes_say_renewal_but_no_link"


# ------------------------------------------------------------------------------------------ L3


def test_misposting_repair() -> None:
    t = date(2024, 5, 1)
    debits = [
        misposting.Debit(1, 10, date(2024, 4, 30)),  # P's own debit before payoff
        misposting.Debit(2, 10, t),  # on the payoff date: not "after"
        misposting.Debit(3, 10, date(2024, 5, 2)),  # misposted -> 11
        misposting.Debit(4, 10, date(2024, 5, 3)),  # misposted -> 11
        misposting.Debit(5, 10, date(2024, 9, 3)),  # after N closed -> left, review
        misposting.Debit(6, 20, date(2024, 7, 1)),  # after a cash payoff -> left, review
        misposting.Debit(7, 11, date(2024, 5, 6)),  # N's own debit
    ]
    repaired, log, exc = misposting.repair_mispostings(
        debits, {10: (t, 11)}, {20: date(2024, 6, 3)}, {11: t}, {11: date(2024, 8, 1)}
    )
    assert repaired == {3: 11, 4: 11}
    assert [r["reason"] for r in log] == [misposting.REASON] * 2
    assert sorted(int(e["entity_id"]) for e in exc) == [5, 6]


# ------------------------------------------------------------------------------------------ Q3 / Q4


def _idx(*pairs: tuple[str, str]) -> AliasIndex:
    idx = AliasIndex()
    for name, key in pairs:
        idx.add(name, key, "ach")
    return idx


def test_q3_closest_deal_and_window() -> None:
    idx = _idx(("Amber Salon Inc", "K1"))
    deals = [
        matching.DealRef(1, "K1", date(2024, 1, 10)),
        matching.DealRef(2, "K1", date(2024, 1, 5)),
        matching.DealRef(3, "K1", date(2024, 3, 1)),
    ]
    rows, exc = matching.match_scorecards(
        [
            (1, "Amber Salon Inc", date(2024, 1, 3)),
            (2, "Amber Salon Inc", date(2024, 2, 19)),
            (3, "Amber Salon Inc", date(2024, 3, 1)),
        ],
        idx,
        deals,
    )
    by = {r["scorecard_id"]: r for r in rows}
    assert by[1]["deal_id"] == 2 and by[1]["day_gap"] == 2  # closest of two in window
    assert by[2]["deal_id"] is None  # 11 days: outside 0-10
    assert by[3]["deal_id"] == 3 and by[3]["day_gap"] == 0  # same day is allowed
    assert [e["reason"] for e in exc] == ["scorecard_unmatched"]


def test_q3_ambiguous_dba_resolved_by_date_and_tie() -> None:
    idx = AliasIndex()
    idx.add("Amber Salon", "K1", "dba")
    idx.add("Amber Salon", "K2", "dba")
    deals = [matching.DealRef(1, "K1", date(2024, 1, 9)), matching.DealRef(2, "K2", date(2024, 5, 9))]
    rows, exc = matching.match_scorecards([(1, "Amber Salon", date(2024, 5, 5))], idx, deals)
    assert rows[0]["deal_id"] == 2 and not exc
    deals.append(matching.DealRef(3, "K1", date(2024, 5, 9)))  # same gap, other merchant
    rows, exc = matching.match_scorecards([(1, "Amber Salon", date(2024, 5, 5))], idx, deals)
    assert rows[0]["deal_id"] == 2 and rows[0]["confidence"] <= 0.6  # tie -> lower deal id, review
    assert exc[0]["reason"] == "scorecard_tie"


def test_q4_deal_number_then_open_deal() -> None:
    idx = _idx(("Driftwood Auto Care LLC", "K1"), ("Other Co", "K2"))
    deals = [
        matching.DealRef(1, "K1", date(2024, 1, 2), close_date=date(2024, 6, 3)),
        matching.DealRef(2, "K1", date(2024, 6, 3)),
        matching.DealRef(5, "K2", date(2024, 1, 2)),
    ]
    rows, exc = matching.match_collections(
        [
            (1, "Driftwood Auto Care", 1, date(2024, 9, 1)),  # deal number wins even if closed
            (2, "Driftwood Auto Care", None, date(2024, 3, 1)),  # deal 1 open then
            (3, "Driftwood Auto Care", None, date(2024, 9, 1)),  # deal 2 open
            (4, "Driftwood Auto Care", None, date(2023, 1, 1)),  # nothing funded yet
            (5, "Other Co", 1, date(2024, 9, 1)),  # deal number disagrees with name
        ],
        idx,
        deals,
    )
    by = {r["collection_id"]: r for r in rows}
    assert by[1]["deal_id"] == 1 and by[1]["method"] == "deal_number"
    assert by[2]["deal_id"] == 1 and by[3]["deal_id"] == 2
    assert by[4]["deal_id"] is None
    assert {e["reason"] for e in exc} == {"collection_unmatched", "name_disagrees_with_deal_number"}


def test_q4_tie_breaks() -> None:
    idx = _idx(("Driftwood Auto Care LLC", "K1"))
    both_open = [matching.DealRef(1, "K1", date(2024, 1, 2)), matching.DealRef(2, "K1", date(2024, 3, 4))]
    rows, exc = matching.match_collections(
        [(1, "Driftwood Auto Care LLC", None, date(2024, 5, 1))], idx, both_open
    )
    assert rows[0]["deal_id"] == 2 and exc[0]["reason"] == "several_open_deals"
    closed = [
        matching.DealRef(1, "K1", date(2024, 1, 2), date(2024, 2, 1)),
        matching.DealRef(2, "K1", date(2024, 2, 1), date(2024, 4, 1)),
    ]
    rows, exc = matching.match_collections(
        [(1, "Driftwood Auto Care LLC", None, date(2024, 5, 1))], idx, closed
    )
    assert rows[0]["deal_id"] == 2 and rows[0]["confidence"] == 0.5
    assert exc[0]["reason"] == "no_open_deal_on_log_date"


def test_close_dates() -> None:
    out = matching.close_dates(
        {1: date(2024, 5, 1)},
        {1: 1000, 2: 1000},
        [
            (2, date(2024, 1, 2), 600),
            (2, date(2024, 1, 3), -100),
            (2, date(2024, 1, 4), 500),
            (1, date(2024, 1, 2), 998),
        ],  # 1 cent tolerance: 999 would count
    )
    assert out == {1: date(2024, 5, 1), 2: date(2024, 1, 4)}
